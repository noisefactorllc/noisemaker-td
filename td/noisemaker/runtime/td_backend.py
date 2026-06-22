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
        self._effect_arrays = []                   # [(glslTOP, layout, merged, {arr: info})] for set_time
        self._layout_cache = {}                    # (namespace, func) -> std140 uniformLayout | None
        self._prog_tops = {}                       # progName -> [TOPs] (debug: dump a specific pass)
        self._mrt_diag_done = False                 # one-time Render Select param introspection log
        self._tex_res = {}                          # texId -> (w,h) resolved (for feedback sizing)
        self.has_feedback = False                  # graph has a cross-frame cycle (drive N frames)
        self._points_cam = None                    # shared dummy ortho Camera COMP for deposit renders

    # -- public ------------------------------------------------------------
    def build(self, graph):
        """Build the whole network for `graph`; return the TOP to present (renderSurface)."""
        if not _in_td():
            raise RuntimeError('TDBackend.build() must run inside TouchDesigner (no op API found).')
        self._detect_feedback(graph)
        self._cap_volume_size(graph)
        for p in graph.passes:
            if p.is_blit:
                self._build_blit(p, graph)
            elif p.is_scatter:
                self._build_points(p, graph)
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
                    rw, rh = self._tex_res.get(tid, (None, None))
                    if rw is None:
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

    def _effect_uniform_layout(self, namespace, func):
        """The effect's std140 `uniformLayout` ({name:{slot,components}}) from its JSON, cached.

        The reference attaches this to the pass spec at pipeline-build time from the effect def; the
        SERIALIZED graph drops it (both JS and Python carry only the 'blit' program), so the backend
        reads it straight from td/noisemaker/effects/<ns>/<func>.json here. Only synth/remap has one."""
        if not namespace or not func:
            return None
        key = (namespace, func)
        if key not in self._layout_cache:
            import json
            import os
            path = os.path.join(os.path.dirname(__file__), '..', 'effects', namespace, func + '.json')
            try:
                with open(path) as fh:
                    self._layout_cache[key] = json.load(fh).get('uniformLayout')
            except Exception:
                self._layout_cache[key] = None
        return self._layout_cache[key]

    def _cap_volume_size(self, graph):
        """Clamp the 3D-volume atlas size to fit TD's cook-resolution limit.

        synth3d volumes are 2D atlases of `volumeSize x volumeSize^2` (default 64 -> 64x4096). The TD
        Non-Commercial license caps cook resolution at 1280, which silently downscales a 64x4096 TOP
        to 20x1280 and breaks `atlasTexel` indexing. Clamp every `volumeSize*` uniform to
        NM_MAX_VOLUME_SIZE (default 32 -> a 32x1024 atlas, under the cap) so the texture size AND the
        shaders' `volumeSize` stay consistent. Platform adaptation only (NOT a compiler change): the
        render differs from the volumeSize-64 reference, but the 3D raymarch runs correctly within the
        license. Raise NM_MAX_VOLUME_SIZE on a Commercial/Educational license (which has no 1280 cap)."""
        cap = _MAX_VOLUME_SIZE
        capped = False
        for p in graph.passes:
            for k, v in list(p.uniforms.items()):
                if 'volumeSize' in k and isinstance(v, (int, float)) and not isinstance(v, bool) and v > cap:
                    p.uniforms[k] = cap
                    capped = True
        if capped:
            self._warn('3D volume atlas clamped to volumeSize<=%d (TD cook-resolution cap; raise '
                       'NM_MAX_VOLUME_SIZE on a Commercial license)' % cap)

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

        # MRT: render to N color buffers (the shader's layout(location=k)); each is read back via
        # its own Render Select TOP (registered in the output step below).
        if p.is_mrt:
            _try(lambda: setattr(g.par, 'numcolorbufs', p.draw_buffers or len(p.outputs)))

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

        # std140 UNIFORM ARRAY (synth/remap `vec4 data[267]`): the frag declares a uniform array and
        # the program carries a uniformLayout — pack the FULL flat uniforms (zone config etc., which
        # are NOT declared scalars in the frag) into the array via the layout, bind via the Arrays
        # page. Packs from `merged` (engine ∪ pass uniforms), not the declared-filtered `bound`.
        arrays = uniform_binder.declared_array_uniforms(frag)
        layout = self._effect_uniform_layout(p.namespace, p.func) if arrays else None
        if arrays and layout:
            packed = uniform_binder.pack_uniforms_with_layout(merged, layout)
            for arr_name, info in arrays.items():
                n4 = info['length'] * 4
                buf = (packed + [0.0] * n4)[:n4]
                uniform_binder.bind_uniform_array(g, arr_name, buf)
            self._effect_arrays.append((g, layout, dict(merged), arrays))

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

        # record the (resolved) render resolution per output texId — the producing GLSL TOP carries
        # it; a Render Select / feedback can't always report it, so we cache it for feedback sizing.
        try:
            _res = (int(g.par.resolutionw.eval()), int(g.par.resolutionh.eval()))
        except Exception:
            _res = (self.width, self.height)
        for _tid in p.outputs.values():
            self._tex_res[_tid] = _res

        # register outputs. MRT: each attachment is a distinct color buffer reached through a Render
        # Select TOP — the Select (not the GLSL TOP) is the readable producer for that texId.
        if p.is_mrt and len(p.outputs) > 1:
            for idx, (attach, tex_id) in enumerate(p.outputs.items()):
                self.tex_top[tex_id] = self._register_mrt_buffer(g, idx, tex_id, tag)
                if self.surfaces is not None:
                    self.surfaces.note_write(tex_id, self.tex_top[tex_id])
        else:
            for attach, tex_id in p.outputs.items():
                self.tex_top[tex_id] = g
                if self.surfaces is not None:
                    self.surfaces.note_write(tex_id, g)
        return g

    def _register_mrt_buffer(self, glsl_top, idx, tex_id, tag):
        """A Render Select TOP that exposes color-buffer `idx` of an MRT GLSL TOP as a readable TOP.
        TD's Render Select reads its source from the `top` PARAM (not an input wire) and picks the
        attachment with `bufferindex`."""
        sel = self.parent.create(_td('renderselectTOP'), '%s_b%d' % (tag, idx))
        self.ops.append(sel)
        _try(lambda: setattr(sel.par, 'top', glsl_top))
        _try(lambda: setattr(sel.par, 'bufferindex', idx))
        return sel

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

    # -- points / billboards deposit (agent scatter) -----------------------
    def _build_points(self, p, graph):
        """drawMode points/billboards: agents SCATTER into the trail surface. TD has no bufferless
        draw, so this is a Geo COMP (Grid SOP -> Convert "point sprites") + GLSL MAT + Render TOP
        with additive blend, then accumulated onto the trail's prior (copy-pass) content. The MAT
        recovers each agent's state texel from the Grid point position (validated by points_probe).
        See docs/TD-PLATFORM-NOTES.md 'GPU point scatter'."""
        from . import deposit_shaders
        # 2D deposits name the agent-state inputs xyzTex/rgbaTex; filter3d/flow3d's volume deposit
        # names them stateTex1/stateTex2 (position carries a 3D voxel, scattered into the trail
        # ATLAS) — same Geo/MAT/Render mechanism, different vertex math (deposit_shaders 'points3d').
        is_volume = 'stateTex1' in p.inputs
        xyz_id = p.inputs.get('xyzTex') or p.inputs.get('stateTex1')
        rgba_id = p.inputs.get('rgbaTex') or p.inputs.get('stateTex2')
        xyz = self._resolve_read(xyz_id) if xyz_id else None
        rgba = self._resolve_read(rgba_id) if rgba_id else None
        if xyz is None or rgba is None:
            self._warn('points %s: unresolved agent state (xyz=%s rgba=%s) — skipped' % (
                p.id, xyz_id, rgba_id))
            return
        # stateSize = the agent grid width (xyz texture width).
        ss = None
        rw = self._tex_res.get(xyz_id)
        if rw and rw[0]:
            ss = int(rw[0])
        if not ss:
            try:
                ss = int(xyz.par.resolutionw.eval())
            except Exception:
                ss = None
        if not ss:
            for k, v in p.uniforms.items():
                if k.startswith('stateSize') and v:
                    ss = int(v)
                    break
        if not ss:
            self._warn('points %s: could not resolve stateSize — skipped' % p.id)
            return

        trail_id = p.outputs.get('fragColor') or next(iter(p.outputs.values()), None)
        spec = graph.spec_for(trail_id)
        if spec is not None:
            tw = int(_dim.resolve_dimension(spec.width, self.width, p.uniforms))
            th = int(_dim.resolve_dimension(spec.height, self.height, p.uniforms))
            fmt = FORMAT_MAP.get(spec.fmt, 'rgba16float')
        else:
            tw, th, fmt = self.width, self.height, 'rgba16float'
        prior = self.tex_top.get(trail_id)        # copy-pass output we accumulate onto (or None)

        tag = self._unique_name(p.id)
        # -- geometry: ss x ss grid -> point sprites (one GL_POINT per agent) --
        geo = self.parent.create(_td('geometryCOMP'), tag + '_geo')
        self.ops.append(geo)
        for _c in list(geo.children):             # a fresh Geo COMP ships a default torus1 SOP
            _try(lambda _c=_c: _c.destroy())
        grid = geo.create(_td('gridSOP'), tag + '_grid')
        for _p, _v in (('rows', ss), ('cols', ss), ('sizex', 2), ('sizey', 2), ('orient', 'xy')):
            _try(lambda _p=_p, _v=_v: setattr(grid.par, _p, _v))
        conv = geo.create(_td('convertSOP'), tag + '_conv')
        conv.inputConnectors[0].connect(grid)
        for _p, _v in (('totype', 'part'), ('prtype', 'pointsprites')):
            _try(lambda _p=_p, _v=_v: setattr(conv.par, _p, _v))
        for _o, _r in ((grid, False), (conv, True)):
            for _f in ('render', 'display'):
                _try(lambda _o=_o, _f=_f, _r=_r: setattr(_o, _f, _r))

        # -- material: the deposit vertex/pixel shader (flow3d -> 3D volume-atlas variant) --
        vsrc, fsrc = deposit_shaders.shaders_for('points3d' if is_volume else p.draw_mode)
        mat = self.parent.create(_td('glslMAT'), tag + '_mat')
        self.ops.append(mat)
        vdat = self.parent.create(_td('textDAT'), tag + '_vert')
        vdat.text = vsrc
        pdat = self.parent.create(_td('textDAT'), tag + '_pix')
        pdat.text = fsrc
        self.ops.extend([vdat, pdat])
        _try(lambda: setattr(mat.par, 'glslversion', '4.60'))
        mat.par.vdat = vdat
        mat.par.pdat = pdat
        _try(lambda: setattr(mat.par, 'sampler0name', 'xyzTex'))
        _try(lambda: setattr(mat.par, 'sampler0top', xyz))
        _try(lambda: setattr(mat.par, 'sampler1name', 'rgbaTex'))
        _try(lambda: setattr(mat.par, 'sampler1top', rgba))
        if p.draw_mode == 'billboards':            # spriteTex must be bound even if shapeMode != 0
            sprite = self._resolve_read(p.inputs.get('spriteTex')) or self._default_input_top()
            _try(lambda: setattr(mat.par, 'sampler2name', 'spriteTex'))
            _try(lambda s=sprite: setattr(mat.par, 'sampler2top', s))
        # additive deposit (Blend One One), no depth.
        for _p, _v in (('blending', True), ('srcblend', 'one'), ('destblend', 'one'),
                       ('blendop', 'add'), ('depthtest', False), ('depthwriting', False)):
            _try(lambda _p=_p, _v=_v: setattr(mat.par, _p, _v))
        declared = uniform_binder.declared_uniform_names(vsrc + '\n' + fsrc)
        bound = {k: v for k, v in p.uniforms.items() if k in declared}
        uniform_binder.bind_uniforms(mat, bound)
        _try(lambda: setattr(geo.par, 'material', mat))

        # -- render the scatter (transparent bg; clears, then accumulates onto prior) --
        rnd = self.parent.create(_td('renderTOP'), tag + '_render')
        self.ops.append(rnd)
        rnd.par.geometry = geo
        rnd.par.camera = self._points_camera()
        rnd.par.outputresolution = 'custom'
        rnd.par.resolutionw = tw
        rnd.par.resolutionh = th
        rnd.par.format = fmt
        for _p, _v in (('bgcolorr', 0), ('bgcolorg', 0), ('bgcolorb', 0), ('bgcolora', 0)):
            _try(lambda _p=_p, _v=_v: setattr(rnd.par, _p, _v))
        _try(lambda: setattr(rnd.par, 'antialias', '1'))   # 1 = no AA

        out = rnd if prior is None else self._add_top(prior, rnd, tag + '_acc', tw, th, fmt)
        self._prog_tops.setdefault(p.prog_name, []).append(rnd)
        self._tex_res[trail_id] = (tw, th)
        self.tex_top[trail_id] = out
        if self.surfaces is not None:
            self.surfaces.note_write(trail_id, out)
        self._warn('points %s: %s ss=%d -> trail %s %dx%d %s%s' % (
            p.id, p.draw_mode, ss, trail_id, tw, th, fmt, '' if prior is None else ' (+prior)'))
        return out

    def _points_camera(self):
        """A shared dummy orthographic Camera COMP — a Render TOP requires a camera even though the
        deposit vertex shader writes gl_Position directly in NDC (so the camera matrices are unused)."""
        if self._points_cam is None:
            cam = self.parent.create(_td('cameraCOMP'), 'nm_points_cam')
            self.ops.append(cam)
            for _p, _v in (('projection', 'ortho'), ('orthowidth', 2.0), ('tz', 2.0),
                           ('near', 0.1), ('far', 10.0)):
                _try(lambda _p=_p, _v=_v: setattr(cam.par, _p, _v))
            self._points_cam = cam
        return self._points_cam

    def _add_top(self, a, b, name, w, h, fmt):
        """A 2-input GLSL TOP that outputs a+b — the additive accumulation of the scattered points
        onto the trail's prior content (== the reference deposit drawing onto the trail FBO without
        clearing; addition is associative)."""
        dat = self.parent.create(_td('textDAT'), name + '_src')
        # Saturate the accumulator to the float16 range. The reference's WebGL2/ANGLE float16 trail
        # FBO SATURATES additive overflow to 65504; TD's Metal float16 yields +Inf instead, which the
        # downstream alpha-composite blend (`/outAlpha`) turns into NaN that the blur then smears to a
        # black frame. clamp(...,0,65504) reproduces WebGL2's saturation (clamp(+Inf,...) == 65504),
        # bounding the deposit/diffuse/blend feedback loop exactly as the reference is bounded.
        dat.text = ('layout(location = 0) out vec4 fragColor;\n'
                    'void main(){ fragColor = TDOutputSwizzle(clamp('
                    'texture(sTD2DInputs[0], vUV.st) + texture(sTD2DInputs[1], vUV.st),'
                    ' 0.0, 65504.0)); }\n')
        g = self.parent.create(_td('glslTOP'), name)
        self.ops.extend([dat, g])
        g.par.pixeldat = dat
        _try(lambda: setattr(g.par, 'glslversion', '4.60'))
        g.par.outputresolution = 'custom'
        g.par.resolutionw = w
        g.par.resolutionh = h
        g.par.format = fmt
        _try(lambda: g.inputConnectors[0].connect(a))
        _try(lambda: g.inputConnectors[1].connect(b))
        _match_reference_sampling(g)
        return g

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


# 3D volume atlas size cap (see TDBackend._cap_volume_size). Default 32 (a 32x1024 atlas) fits TD's
# Non-Commercial 1280 cook-resolution cap; raise on a Commercial/Educational license.
_MAX_VOLUME_SIZE = int(os.environ.get('NM_MAX_VOLUME_SIZE', '32'))

_BOOL_DEFINE_RE = re.compile(r'#define\s+(\w+)\s+(?:true|false)\b')
# A define used as a BARE boolean condition — `if (NAME)` / `if (!NAME)` — is a GLSL bool even when
# the shader carries no `#define NAME true|false` fallback (the reference's render3d/synth3d shaders
# rely on the expander always injecting it; WebGL2/ANGLE accepts `if (0)`, strict #version 460 does
# NOT). `if (FILTERING == 1)` is NOT matched (the `== 1` keeps FILTERING an int).
_BOOL_IF_RE = re.compile(r'\bif\s*\(\s*!?\s*([A-Za-z_]\w*)\s*\)')


def _bool_define_keys(frag_text):
    """Keys that are GLSL bools — declared via an in-shader `#define K true|false` fallback OR used
    as a bare `if (K)` / `if (!K)` condition. These must be injected as true/false (not 1/0) so a
    strict-core boolean condition stays a bool (else TD rejects `if (0)`)."""
    return set(_BOOL_DEFINE_RE.findall(frag_text)) | set(_BOOL_IF_RE.findall(frag_text))


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
