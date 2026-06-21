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

def _td(name):
    """Resolve a TouchDesigner operator-type / API global (glslTOP, baseCOMP, textDAT, …).

    TD injects these into DAT scopes via `from td import *`, but NOT into imported .py modules,
    so inside this package we must fetch them from the `td` module (importable anywhere in a TD
    process) — with a builtins fallback. Off-platform (stock python3) it raises, by design."""
    import td as _tdmod
    if hasattr(_tdmod, name):
        return getattr(_tdmod, name)
    import builtins
    if hasattr(builtins, name):
        return getattr(builtins, name)
    raise NameError('TouchDesigner global %r not found' % name)


def _in_td():
    try:
        import td  # noqa: F401
        return True
    except Exception:
        return False


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
    def __init__(self, parent_comp, shaders_root, *, width=256, height=256, time=0.25,
                 surface_manager=None):
        self.parent = parent_comp
        self.shaders_root = shaders_root           # .../td/noisemaker/shaders/effects
        self.width = width
        self.height = height
        self.time = time                           # normalized 0..1 baked into engine uniforms
        self.surfaces = surface_manager            # optional SurfaceManager for feedback
        self.tex_top = {}                          # texId -> producing TOP (LAST writer so far)
        self.ops = []                              # everything we created (for teardown)
        self.warnings = []
        self._default_in = None                    # lazily-built 1x1 black TOP for 'none' inputs
        self._seq = 0                              # monotonic op-name counter (unrolled passes reuse p.id)
        self._feedback = {}                        # cross-frame texId -> Feedback TOP (lazy)
        self._effect_uniforms = []                 # [(glslTOP, {declared uniform: value})] for set_time
        self._prog_tops = {}                       # progName -> [TOPs] (debug: dump a specific pass)
        self.has_feedback = False                  # graph has a cross-frame cycle (drive N frames)

    # -- public ------------------------------------------------------------
    def build(self, graph):
        """Build the whole network for `graph`; return the TOP to present (renderSurface)."""
        if not _in_td():
            raise RuntimeError('TDBackend.build() must run inside TouchDesigner (no op API found).')
        self._detect_feedback(graph)
        for p in graph.passes:
            if p.is_blit:
                self._build_blit(p, graph)
            elif p.is_points:
                self._warn('points pass %s skipped (Phase 5.5: Geo COMP + GLSL MAT + Render TOP)' % p.id)
            else:
                # `repeat=N` is an intra-frame iterative solve (e.g. nsPressure Jacobi x40): each
                # iteration reads the previous one's output. UNROLL into N chained TOPs — building
                # the same pass N times in a row chains automatically through the last-writer
                # `tex_top` (iteration k reads iteration k-1). This sidesteps TD's GLSL TOP `Passes`
                # (whose previous-pass feedback semantics are unreliable) and is deterministic.
                for _ in range(self._resolve_repeat(p)):
                    self._build_effect(p, graph)
        # Wire each Feedback TOP's Target to the LAST writer of its texId (built after the first
        # reader, so only resolvable now). Output = that producer's PREVIOUS frame, which breaks
        # the cross-frame cycle and gives the reference's frame-to-frame state persistence.
        for tid, fb in self._feedback.items():
            if fb is None:
                continue
            writer = self.tex_top.get(tid)
            if writer is not None:
                _try(lambda fb=fb, writer=writer: setattr(fb.par, 'top', writer))
                # The feedback MUST match the source surface's RESOLUTION and FORMAT. A bare Feedback
                # TOP defaults to a small fixed resolution (128) and 8-bit fixed format — both fatal:
                #   * wrong resolution -> the consumer's textureSize(bufTex) disagrees with its own
                #     render size, so every fragCoord/texSize UV is off (seeds tile into a quadrant);
                #   * 8-bit fixed -> clamps float state (velocity ±12, positions) to [0,1].
                # Mirror the writer (already sized/formatted from the surface spec).
                spec = graph.spec_for(tid)
                fmt = FORMAT_MAP.get(spec.fmt, 'rgba16float') if spec is not None else 'rgba16float'
                try:
                    rw = int(writer.par.resolutionw.eval())
                    rh = int(writer.par.resolutionh.eval())
                    # A bare Feedback TOP (no input) ignores its custom-resolution param and cooks at
                    # a 128 default — so the FIRST-frame state (and textureSize seen by the consumer)
                    # is the wrong size, corrupting any seed/init that keys off textureSize(bufTex).
                    # Wire a correctly-sized ZERO Constant as the feedback input: it anchors the
                    # resolution AND provides the empty (a=0) initial state the reference relies on.
                    init = self.parent.create(_td('constantTOP'), fb.name + '_init')
                    self.ops.append(init)
                    init.par.outputresolution = 'custom'
                    init.par.resolutionw = rw
                    init.par.resolutionh = rh
                    init.par.format = fmt
                    for _p, _v in (('colorr', 0), ('colorg', 0), ('colorb', 0), ('alpha', 0)):
                        setattr(init.par, _p, _v)
                    fb.inputConnectors[0].connect(init)
                    fb.par.format = fmt
                    self._warn('feedback %s -> %dx%d fmt=%s' % (tid, rw, rh, fmt))
                except Exception as e:
                    self._warn('feedback %s init/format FAILED: %s' % (tid, e))
            else:
                self._warn('feedback %s has no producer to target' % tid)
        return self._present_top(graph)

    def _detect_feedback(self, graph):
        """Find texIds that need a cross-frame Feedback TOP (1-frame delay).

        A pass input is a CROSS-FRAME read when its texId's first WRITE is at a pass index >= the
        reading pass — i.e. the value is read before it is produced THIS frame, so it must come
        from last frame. This is purely structural and covers all three shapes:
          * j > i  — classic back-edge (`feedback`'s selfTex: read by blend, written later by copy);
          * j == i — same-pass read+write state self-loop (navierStokes velocity: nsSplat reads AND
                     writes ns_velocity; particle xyz/vel/rgba; trail surfaces);
        Reads where an EARLIER pass already wrote the texId (j < i) are WITHIN-frame and resolve
        through the last-writer `tex_top` chain instead (the natural ping-pong of a multi-pass
        solver). The Feedback target is the texId's LAST writer (its persisted end-of-frame state)."""
        first_write = {}
        for i, p in enumerate(graph.passes):
            for tid in p.outputs.values():
                first_write.setdefault(tid, i)
        self._feedback = {}                    # texId -> Feedback TOP (created lazily on first read)
        for i, p in enumerate(graph.passes):
            for tid in p.inputs.values():
                if not tid or tid == 'none':
                    continue
                j = first_write.get(tid)
                if j is not None and j >= i:
                    self._feedback.setdefault(tid, None)
        self.has_feedback = bool(self._feedback)

    def _resolve_repeat(self, p):
        """Concrete iteration count for an unrolled pass. `repeat` is an int or a uniform NAME
        (e.g. 'iterations' -> p.uniforms['iterations'] == 40 for navierStokes pressure)."""
        r = getattr(p, 'repeat', None)
        if r is None or isinstance(r, bool):
            return 1
        if isinstance(r, str):
            r = p.uniforms.get(r)
        try:
            return max(1, int(r))
        except (TypeError, ValueError):
            return 1

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
        # Boolean-typed defines (fallback `#define K true|false`) MUST be injected as true/false:
        # the reference emits `1`/`0` and leans on WebGL2/ANGLE accepting `if (1)`, but TD's strict
        # #version 460 core rejects a non-bool `if` condition (the curl `if (RIDGES)` compile error).
        bool_keys = _bool_define_keys(frag)
        header = ''.join(
            '#define %s %s\n' % (k, ('true' if _truthy(v) else 'false') if k in bool_keys else _glsl_lit(v))
            for k, v in p.defines.items())
        tag = self._unique_name(p.id)
        dat = self.parent.create(_td('textDAT'), tag + '_src')
        dat.text = header + frag
        self.ops.append(dat)

        n_inputs = len(input_order)
        top_type = _td('glslmultiTOP') if n_inputs > 3 else _td('glslTOP')   # Multi lifts the 3-input cap
        g = self.parent.create(top_type, tag)
        self.ops.append(g)
        self._prog_tops.setdefault(p.prog_name, []).append(g)
        g.par.pixeldat = dat
        _try(lambda: setattr(g.par, 'glslversion', '4.60'))

        # resolution + format from the (primary) output texture spec.
        spec = self._primary_output_spec(p, graph)
        self._apply_res_format(g, spec, p.uniforms)

        # MRT
        if p.is_mrt:
            n = p.draw_buffers or len(p.outputs)
            _try(lambda: setattr(g.par, 'numcolorbufs', n))
            self._warn('MRT pass %s (%d buffers): extra buffers need Render Select TOPs (Phase 5.5)' % (p.id, n))

        # NB: `repeat` is handled by UNROLLING in build() (N chained TOPs), not the Passes param.

        # uniforms: bind ONLY what the shader declares (engine globals ∪ pass uniforms), in one
        # pass — the binder sets the Vectors slot count. Binding undeclared names wastes slots
        # and (pre-fix) silently truncated many-uniform effects.
        merged = dict(engine_uniforms(self.width, self.height, self.time))
        merged.update(p.uniforms)
        declared = uniform_binder.declared_uniform_names(frag)
        bound = {k: v for k, v in merged.items() if k in declared}
        uniform_binder.bind_uniforms(g, bound)
        # Remember the FULL declared binding so set_time() can re-bind with the new engine `time`
        # WITHOUT dropping the per-effect uniforms (re-binding engine-only would reset the Vectors
        # slot count and silently wipe speed/dyeDecay/zoom/... — black/garbage output).
        self._effect_uniforms.append((g, bound))

        # wire inputs in NM_INPUTS order. A declared sampler with no/`none` texId binds the default
        # 1x1 black TOP — reference parity (unbound samplers read [0,0,0,0]) AND it makes TD declare
        # the sTD2DInputs array, which a filter-as-generator (e.g. subdivide, used with no input)
        # still references -> otherwise 'sTD2DInputs : undeclared identifier'.
        for idx, sampler in enumerate(input_order):
            tex_id = p.inputs.get(sampler)
            if not tex_id or tex_id == 'none':
                src = self._default_input_top()
            else:
                src = self._resolve_read(tex_id)
                if src is None:
                    self._warn('pass %s input %s -> unresolved texId %s (bound default black)' % (
                        p.id, sampler, tex_id))
                    src = self._default_input_top()
            _try(lambda src=src, idx=idx: g.inputConnectors[idx].connect(src))

        # register outputs (primary first). MRT extra buffers map to the same TOP for now.
        for attach, tex_id in p.outputs.items():
            self.tex_top[tex_id] = g
            if self.surfaces is not None:
                self.surfaces.note_write(tex_id, g)
        return g

    def _default_input_top(self):
        """A 1x1 transparent-black (0,0,0,0) Constant TOP, matching the reference's default texture
        for unbound/'none' inputs (webgl2.js binds a 1x1 RGBA [0,0,0,0]). Built once, shared."""
        if self._default_in is None:
            c = self.parent.create(_td('constantTOP'), 'nm_default_in')
            self.ops.append(c)
            _try(lambda: setattr(c.par, 'outputresolution', 'custom'))
            for par, val in (('resolutionw', 1), ('resolutionh', 1),
                             ('colorr', 0), ('colorg', 0), ('colorb', 0), ('alpha', 0)):
                _try(lambda p=par, v=val: setattr(c.par, p, v))
            self._default_in = c
        return self._default_in

    # -- blit pass ---------------------------------------------------------
    def _unique_name(self, pass_id):
        """A network-unique op base name. Unrolled iterative passes (nsPressure x40) reuse one
        pass id, so suffix a monotonic counter to avoid TD create() name collisions."""
        self._seq += 1
        return '%s_%d' % (_safe_name(pass_id), self._seq)

    def _build_blit(self, p, graph):
        src_id = p.inputs.get('src') or next(iter(p.inputs.values()), None)
        n = self.parent.create(_td('nullTOP'), self._unique_name(p.id))
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
        """texId -> the TOP to read it from.

        WITHIN-frame first: if an earlier pass already wrote this texId this build, read that
        producer (the last-writer ping-pong chain). Otherwise, if the texId needs a cross-frame
        value (read-before-write), resolve to a lazily-created Feedback TOP (1-frame delay)."""
        if tex_id in self.tex_top:
            return self.tex_top[tex_id]
        if tex_id in self._feedback:
            fb = self._feedback[tex_id]
            if fb is None:
                fb = self.parent.create(_td('feedbackTOP'), _safe_name(tex_id) + '_fb')
                self.ops.append(fb)
                self._feedback[tex_id] = fb
            return fb
        if self.surfaces is not None:
            t = self.surfaces.read_top(tex_id)
            if t is not None:
                return t
        return None

    def _primary_output_spec(self, p, graph):
        # primary attachment is `color` (or the first output); resolve its texId spec.
        tex_id = p.outputs.get('color') or next(iter(p.outputs.values()), None)
        return graph.spec_for(tex_id) if tex_id else None

    def _apply_res_format(self, g, spec, uniforms=None):
        w = self.width
        h = self.height
        fmt = 'rgba16float'
        if spec is not None:
            # Pass the pass's uniforms so dynamic dims resolve to the ACTUAL value, not the spec
            # default: ns_velocity is {screenDivide: zoom_chain_1} — without uniforms it falls back
            # to default 4 (64x64 instead of screen/zoom), running the whole sim at the wrong grid.
            w = _dim.resolve_dimension(spec.width, self.width, uniforms)
            h = _dim.resolve_dimension(spec.height, self.height, uniforms)
            fmt = FORMAT_MAP.get(spec.fmt, 'rgba16float')
        _try(lambda: setattr(g.par, 'outputresolution', 'custom'))
        _try(lambda: setattr(g.par, 'resolutionw', int(w)))
        _try(lambda: setattr(g.par, 'resolutionh', int(h)))
        _try(lambda: setattr(g.par, 'format', fmt))
        _match_reference_sampling(g)

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


_BOOL_DEFINE_RE = re.compile(r'#define\s+(\w+)\s+(?:true|false)\b')


def _bool_define_keys(frag_text):
    """Keys whose in-shader `#ifndef`/`#define K true|false` fallback declares a GLSL bool. These
    must be injected as true/false (not 1/0) so a strict-core `if (K)` condition stays a bool."""
    return set(_BOOL_DEFINE_RE.findall(frag_text))


def _truthy(v):
    if isinstance(v, str):
        return v.strip().lower() not in ('0', 'false', '', 'none')
    return bool(v)


def _try(fn):
    try:
        fn()
    except Exception:
        pass


def _match_reference_sampling(top):
    """Make a GLSL TOP sample its inputs exactly like the reference WebGL2 backend, which creates
    every intermediate *surface* render target with NEAREST min/mag + CLAMP_TO_EDGE
    (`webgl2.js` texParameteri; the WebGPU backend mirrors it: "surface inputs sample NEAREST").

    Two TD defaults are wrong for this:
      - `inputextenduv` defaults to "zero" (transparent edge) -> set "hold" (= CLAMP_TO_EDGE).
        Menu: hold|zero|repeat|mirror. (Was the blur border-ring bug.)
      - `inputfiltertype` ("Input Smoothness") defaults to "linear" (Interpolate Pixels) -> set
        "nearest" (= NEAREST). 1:1 effects are unaffected (nearest == linear at texel centers),
        but anything that resamples at warped/fractional coords (polar, pinch, distortion, uvRemap,
        chromaticAberration, bloom upsample, ...) needs NEAREST to match the golden.
    """
    _try(lambda: setattr(top.par, 'inputextenduv', 'hold'))
    _try(lambda: setattr(top.par, 'inputfiltertype', 'nearest'))
