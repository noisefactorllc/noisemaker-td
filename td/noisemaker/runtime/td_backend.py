"""The network builder — RenderGraph -> a TouchDesigner TOP network.

This is the TD analogue of the imperative `NMRenderBackend`/`rd_backend` of the Unity/Godot
ports, but simpler: TouchDesigner has a pull-based cook graph, so we BUILD a static network
of operators ONCE and the engine re-cooks it each frame. Each render-graph pass becomes a
GLSL TOP (effect) or Null TOP (blit); `inputs` become TOP connections; `uniforms` become
Vectors-page entries; `defines` become injected `#define` lines; global surfaces o0..o7 /
state become Feedback TOPs (via `surface_manager`).

Touches the TouchDesigner Python API — only runs inside a TD process.

Coverage:
  * DONE (Tier-1 path): effect passes (single output), blit passes, pooled-texture wiring,
    per-pass define overrides, named-input -> sTD2DInputs ordering, custom resolution/format,
    uniform binding, `repeat` via the Passes param.
  * MARKED (Phase 5.5): MRT outputs (draw_buffers>1) need a Render Select TOP per extra
    buffer; `drawMode:"points"` needs a Geometry COMP + GLSL MAT + Render TOP; full
    cross-frame surface feedback is delegated to surface_manager.
"""
import os
import re

try:
    # These globals exist only inside TouchDesigner. Import guarded so the module is importable
    # (for static checks) under stock python3; build() will fail loudly if run outside TD.
    glslTOP            # noqa: F821
    glslmultiTOP       # noqa: F821
    nullTOP            # noqa: F821
    textDAT            # noqa: F821
    _IN_TD = True
except NameError:
    _IN_TD = False

from . import dim as _dim
from . import uniform_binder
from .engine_uniforms import engine_uniforms

# rgba* -> GLSL TOP `format` menu value. Linear, never sRGB (reference/04 §8).
FORMAT_MAP = {
    'rgba8': 'rgba8fixed', 'rgba8unorm': 'rgba8fixed',
    'rgba16f': 'rgba16float', 'rgba16float': 'rgba16float',
    'rgba32f': 'rgba32float', 'rgba32float': 'rgba32float',
}

_NM_INPUTS_RE = re.compile(r'^//\s*NM_INPUTS:\s*(.*)$', re.M)


def _safe_name(s):
    return re.sub(r'[^A-Za-z0-9_]', '_', str(s))


def _parse_input_order(frag_text):
    """Read the `// NM_INPUTS: name=0 other=1` header the transpiler emits.
    Returns an ordered list of sampler names (index = list position)."""
    m = _NM_INPUTS_RE.search(frag_text)
    if not m:
        return []
    body = m.group(1).strip()
    if body in ('', '(none)'):
        return []
    pairs = []
    for tok in body.split():
        if '=' in tok:
            name, idx = tok.split('=', 1)
            try:
                pairs.append((int(idx), name))
            except ValueError:
                pairs.append((len(pairs), name))
    pairs.sort()
    return [name for _, name in pairs]


class TDBackend:
    def __init__(self, parent_comp, shaders_root, *, width=256, height=256, surface_manager=None):
        self.parent = parent_comp
        self.shaders_root = shaders_root           # .../td/noisemaker/shaders/effects
        self.width = width
        self.height = height
        self.surfaces = surface_manager            # optional SurfaceManager for feedback
        self.tex_top = {}                          # texId -> producing TOP
        self.ops = []                              # everything we created (for teardown)
        self.warnings = []

    # -- public ------------------------------------------------------------
    def build(self, graph):
        """Build the whole network for `graph`; return the TOP to present (renderSurface)."""
        if not _IN_TD:
            raise RuntimeError('TDBackend.build() must run inside TouchDesigner (no op API found).')
        for p in graph.passes:
            if p.is_blit:
                self._build_blit(p, graph)
            elif p.is_points:
                self._warn('points pass %s skipped (Phase 5.5: Geo COMP + GLSL MAT + Render TOP)' % p.id)
            else:
                self._build_effect(p, graph)
        return self._present_top(graph)

    def teardown(self):
        for o in self.ops:
            try:
                o.destroy()
            except Exception:
                pass
        self.ops = []
        self.tex_top = {}

    # -- effect pass -------------------------------------------------------
    def _build_effect(self, p, graph):
        frag_path = os.path.join(self.shaders_root, p.namespace, p.func, '%s.frag' % p.prog_name)
        if not os.path.exists(frag_path):
            self._warn('missing frag %s for pass %s' % (frag_path, p.id))
            return
        with open(frag_path, 'r') as f:
            frag = f.read()
        input_order = _parse_input_order(frag)

        # define overrides injected ABOVE the source; the frag's `#ifndef` fallbacks defer to them.
        header = ''.join('#define %s %s\n' % (k, _glsl_lit(v)) for k, v in p.defines.items())
        dat = self.parent.create(textDAT, _safe_name(p.id) + '_src')
        dat.text = header + frag
        self.ops.append(dat)

        n_inputs = len(input_order)
        top_type = glslmultiTOP if n_inputs > 3 else glslTOP   # Multi lifts the 3-input cap
        g = self.parent.create(top_type, _safe_name(p.id))
        self.ops.append(g)
        g.par.pixeldat = dat
        _try(lambda: setattr(g.par, 'glslversion', '4.60'))

        # resolution + format from the (primary) output texture spec.
        spec = self._primary_output_spec(p, graph)
        self._apply_res_format(g, spec)

        # MRT
        if p.is_mrt:
            n = p.draw_buffers or len(p.outputs)
            _try(lambda: setattr(g.par, 'numcolorbufs', n))
            self._warn('MRT pass %s (%d buffers): extra buffers need Render Select TOPs (Phase 5.5)' % (p.id, n))

        # repeat -> Passes (intra-frame iteration; ping-pong feedback handled in Phase 3/5.5)
        if isinstance(p.repeat, int) and p.repeat > 1:
            _try(lambda: setattr(g.par, 'passes', p.repeat))

        # uniforms: engine globals first, then the pass's literal uniforms.
        eu = engine_uniforms(self.width, self.height, 0.0)
        slot = uniform_binder.bind_uniforms(g, eu)
        uniform_binder.bind_uniforms(g, p.uniforms, start_slot=slot)

        # wire inputs in NM_INPUTS order.
        for idx, sampler in enumerate(input_order):
            tex_id = p.inputs.get(sampler)
            if not tex_id or tex_id == 'none':
                continue
            src = self._resolve_read(tex_id)
            if src is None:
                self._warn('pass %s input %s -> unresolved texId %s' % (p.id, sampler, tex_id))
                continue
            _try(lambda src=src, idx=idx: g.inputConnectors[idx].connect(src))

        # register outputs (primary first). MRT extra buffers map to the same TOP for now.
        for attach, tex_id in p.outputs.items():
            self.tex_top[tex_id] = g
            if self.surfaces is not None:
                self.surfaces.note_write(tex_id, g)
        return g

    # -- blit pass ---------------------------------------------------------
    def _build_blit(self, p, graph):
        src_id = p.inputs.get('src') or next(iter(p.inputs.values()), None)
        n = self.parent.create(nullTOP, _safe_name(p.id))
        self.ops.append(n)
        src = self._resolve_read(src_id) if src_id else None
        if src is not None:
            _try(lambda: n.inputConnectors[0].connect(src))
        else:
            self._warn('blit %s has no resolvable src (%s)' % (p.id, src_id))
        for attach, tex_id in p.outputs.items():
            self.tex_top[tex_id] = n
            if self.surfaces is not None:
                self.surfaces.note_write(tex_id, n)
        return n

    # -- helpers -----------------------------------------------------------
    def _resolve_read(self, tex_id):
        """texId -> the TOP to read it from. Global surfaces may route through feedback."""
        if self.surfaces is not None:
            t = self.surfaces.read_top(tex_id)
            if t is not None:
                return t
        return self.tex_top.get(tex_id)

    def _primary_output_spec(self, p, graph):
        # primary attachment is `color` (or the first output); resolve its texId spec.
        tex_id = p.outputs.get('color') or next(iter(p.outputs.values()), None)
        return graph.spec_for(tex_id) if tex_id else None

    def _apply_res_format(self, g, spec):
        w = self.width
        h = self.height
        fmt = 'rgba16float'
        if spec is not None:
            w = _dim.resolve_dimension(spec.width, self.width)
            h = _dim.resolve_dimension(spec.height, self.height)
            fmt = FORMAT_MAP.get(spec.fmt, 'rgba16float')
        _try(lambda: setattr(g.par, 'outputresolution', 'custom'))
        _try(lambda: setattr(g.par, 'resolutionw', int(w)))
        _try(lambda: setattr(g.par, 'resolutionh', int(h)))
        _try(lambda: setattr(g.par, 'format', fmt))

    def _present_top(self, graph):
        if graph.render_surface:
            for cand in ('global_%s' % graph.render_surface, graph.render_surface):
                if cand in self.tex_top:
                    return self.tex_top[cand]
        # fallback: the last pass's primary output.
        if graph.passes:
            last = graph.passes[-1]
            tex_id = last.outputs.get('color') or next(iter(last.outputs.values()), None)
            return self.tex_top.get(tex_id)
        return None

    def _warn(self, msg):
        self.warnings.append(msg)
        try:
            debug('[td_backend] ' + msg)  # noqa: F821 — TD global
        except Exception:
            pass


def _glsl_lit(v):
    if isinstance(v, bool):
        return '1' if v else '0'
    return str(v)


def _try(fn):
    try:
        fn()
    except Exception:
        pass
