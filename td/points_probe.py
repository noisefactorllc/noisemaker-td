"""points_probe.py — ISOLATED known-answer probe for the GPU point-scatter (deposit) pipeline.

The deposit pass (drawMode:points/billboards) is the one flagship piece the GLSL-TOP dataflow
can't express: agents must SCATTER (each writes to its own pixel), which in TD means geometry —
a Geo COMP + GLSL MAT + Render TOP, not a fullscreen fragment pass. Before wiring the real
1024x1024-agent deposit into td_backend, this probe validates the whole mechanism in isolation
with a tiny KNOWN ANSWER so every unknown is pinned independently of the rest of the flagship:

  * SOP -> renderable points (Grid SOP -> Convert SOP "Render as Point Sprites"),
  * recover each agent's state texel from the point POSITION (TDPos(); the point-sprite path
    overwrites texcoords, so position is the index channel that survives),
  * bind a TOP as a sampler2D on the GLSL MAT (Samplers page),
  * map agent normalized position -> NDC -> the exact target pixel,
  * gl_PointSize=1 single-pixel deposit,
  * additive blend (srcblend=one destblend=one blendop=add) onto a transparent-black target,
  * float readback + its row orientation.

The unknown with the biggest blast radius is whether a TD MAT vertex shader may write gl_Position
DIRECTLY in NDC (reference-faithful) or must route through TDWorldToProj()+camera. So we build BOTH
branches in one launch and report which one lands the 4 known agents on their 4 predicted pixels.

KNOWN ANSWER (stateSize=2 -> 4 agents; target 8x8; deposit pixel = normalizedPos * 8):
    agent texel (0,0) pos (0.1875,0.1875) -> pixel (1,1)  RED
    agent texel (1,0) pos (0.8125,0.1875) -> pixel (6,1)  GREEN
    agent texel (0,1) pos (0.1875,0.8125) -> pixel (1,6)  BLUE
    agent texel (1,1) pos (0.8125,0.8125) -> pixel (6,6)  WHITE

Driven by an Execute DAT (build_points_probe_toe.py) onStart -> probe_main(); writes a verbose
log to parity/out/_probe_log.txt and renders branch PNGs, then quits.
"""
import os
import sys
import traceback

_lines = []


def _repo():
    try:
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    except NameError:
        env = os.environ.get('NM_TD_REPO')
        if env:
            return env
        raise


REPO = _repo()
OUT = os.path.join(REPO, 'parity', 'out')
LOG = os.path.join(OUT, '_probe_log.txt')


def _td(name):
    import td as _t
    if hasattr(_t, name):
        return getattr(_t, name)
    import builtins
    if hasattr(builtins, name):
        return getattr(builtins, name)
    raise NameError('TD global %r not found' % name)


def log(m):
    _lines.append(str(m))
    try:
        print('[probe]', m)
    except Exception:
        pass
    try:
        with open(LOG, 'w') as f:
            f.write('\n'.join(_lines) + '\n')
    except Exception:
        pass


STATE = 2          # stateSize: 2x2 = 4 agents
RES = 8            # render target 8x8

# A GLSL TOP that paints a STATE x STATE rgba32float texture of agent POSITIONS: texel (x,y) ->
# normalized pos placing that agent on a clean target-pixel center ((k*5+1.5)/8 for k in {0,1}).
XYZ_FRAG = """layout(location = 0) out vec4 fragColor;
void main(){
    ivec2 t = ivec2(gl_FragCoord.xy);
    float px = (float(t.x) * 5.0 + 1.5) / 8.0;
    float py = (float(t.y) * 5.0 + 1.5) / 8.0;
    fragColor = TDOutputSwizzle(vec4(px, py, 0.0, 1.0));
}
"""

# distinct color per agent texel: (0,0)=red (1,0)=green (0,1)=blue (1,1)=white.
RGBA_FRAG = """layout(location = 0) out vec4 fragColor;
void main(){
    ivec2 t = ivec2(gl_FragCoord.xy);
    vec3 c = vec3(1.0);
    if (t == ivec2(0,0))      c = vec3(1.0, 0.0, 0.0);
    else if (t == ivec2(1,0)) c = vec3(0.0, 1.0, 0.0);
    else if (t == ivec2(0,1)) c = vec3(0.0, 0.0, 1.0);
    fragColor = TDOutputSwizzle(vec4(c, 1.0));
}
"""

# MAT vertex shader. {GLPOS} swapped per branch: direct NDC vs TDWorldToProj(TDDeform()).
VERT_TMPL = """uniform sampler2D xyzTex;
uniform sampler2D rgbaTex;
uniform float uStateSize;
out vec4 vColor;
void main(){
    int ss = int(uStateSize + 0.5);
    vec3 P = TDPos();
    int col = int(floor((P.x * 0.5 + 0.5) * float(ss - 1) + 0.5));
    int row = int(floor((P.y * 0.5 + 0.5) * float(ss - 1) + 0.5));
    col = clamp(col, 0, ss - 1);
    row = clamp(row, 0, ss - 1);
    vec4 pos = texelFetch(xyzTex, ivec2(col, row), 0);
    vec4 rgba = texelFetch(rgbaTex, ivec2(col, row), 0);
    if (pos.w < 0.5) { gl_Position = vec4(2.0, 2.0, 0.0, 1.0); gl_PointSize = 0.0; vColor = vec4(0.0); return; }
    vec2 clipPos = pos.xy * 2.0 - 1.0;
    gl_PointSize = 1.0;
    {GLPOS}
    vColor = rgba;
}
"""

PIX = """in vec4 vColor;
layout(location = 0) out vec4 fragColor;
void main(){
    fragColor = TDOutputSwizzle(vColor);
}
"""


def _glsl_top(parent, name, frag, w, h):
    g = parent.create(_td('glslTOP'), name)
    dat = parent.create(_td('textDAT'), name + '_src')
    dat.text = frag
    g.par.pixeldat = dat
    try:
        g.par.glslversion = '4.60'
    except Exception:
        pass
    g.par.outputresolution = 'custom'
    g.par.resolutionw = w
    g.par.resolutionh = h
    g.par.format = 'rgba32float'
    return g


def _build_branch(root, tag, xyz, rgba, cam, use_camera):
    """One full scatter branch: Grid->Convert(point sprites) inside a Geo COMP with a GLSL MAT,
    rendered by a Render TOP (additive). Returns the Render TOP. `use_camera` switches the vertex
    shader between direct gl_Position (False) and TDWorldToProj(TDDeform()) (True)."""
    geo = root.create(_td('geometryCOMP'), 'geo_' + tag)
    # a fresh Geo COMP ships with a default `torus1` SOP whose render flag is ON — destroy any such
    # pre-existing children so ONLY our converted points render.
    for _c in list(geo.children):
        try:
            _c.destroy()
        except Exception:
            pass
    # grid: STATE x STATE points in the XY plane spanning [-1,1] (positions carry the agent index).
    grid = geo.create(_td('gridSOP'), 'grid_' + tag)
    for p, v in (('rows', STATE), ('cols', STATE), ('sizex', 2), ('sizey', 2),
                 ('orient', 'xy')):
        try:
            setattr(grid.par, p, v)
        except Exception as e:
            log('  grid.%s set failed: %s' % (p, e))
    conv = geo.create(_td('convertSOP'), 'conv_' + tag)
    conv.inputConnectors[0].connect(grid)
    for p, v in (('totype', 'part'), ('prtype', 'pointprites')):
        try:
            setattr(conv.par, p, v)
        except Exception as e:
            log('  conv.%s=%r failed: %s' % (p, v, e))
    # render ONLY the convert output (particles -> GL_POINTS); hide the grid polygons.
    for o, r in ((grid, False), (conv, True)):
        for flag in ('render', 'display'):
            try:
                setattr(o, flag, r)
            except Exception as e:
                log('  %s.%s=%s failed: %s' % (o.name, flag, r, e))
    # INSTRUMENT: cook conv + log what geometry/flags actually exist (bisect mesh-vs-points).
    try:
        conv.cook(force=True)
        log('  [%s] conv totype=%r prtype=%r' % (tag, conv.par.totype.eval(), conv.par.prtype.eval()))
        log('  [%s] conv numPoints=%s numPrims=%s | grid numPoints=%s numPrims=%s' % (
            tag, conv.numPoints, conv.numPrims, grid.numPoints, grid.numPrims))
        try:
            pclass = type(conv.prims[0]).__name__ if conv.numPrims else 'NO-PRIMS(pure points)'
            log('  [%s] conv prim[0] class=%s' % (tag, pclass))
        except Exception as e:
            log('  [%s] conv prim class probe: %s' % (tag, e))
        for c in geo.children:
            log('  [%s] child %-10s render=%s display=%s' % (
                tag, c.name, getattr(c, 'render', '?'), getattr(c, 'display', '?')))
    except Exception as e:
        log('  [%s] instrument failed: %s' % (tag, e))

    mat = root.create(_td('glslMAT'), 'mat_' + tag)
    glpos = ('gl_Position = TDWorldToProj(TDDeform(vec4(clipPos, 0.0, 1.0)));' if use_camera
             else 'gl_Position = vec4(clipPos, 0.0, 1.0);')
    vdat = root.create(_td('textDAT'), 'vert_' + tag)
    vdat.text = VERT_TMPL.replace('{GLPOS}', glpos)
    pdat = root.create(_td('textDAT'), 'pix_' + tag)
    pdat.text = PIX
    try:
        mat.par.glslversion = '4.60'
    except Exception:
        pass
    mat.par.vdat = vdat
    mat.par.pdat = pdat
    # samplers
    mat.par.sampler0name = 'xyzTex'
    mat.par.sampler0top = xyz
    mat.par.sampler1name = 'rgbaTex'
    mat.par.sampler1top = rgba
    # uniform
    mat.par.vec0name = 'uStateSize'
    mat.par.vec0valuex = float(STATE)
    # additive blend, no depth.
    for p, v in (('blending', True), ('srcblend', 'one'), ('destblend', 'one'),
                 ('blendop', 'add'), ('depthtest', False), ('depthwriting', False)):
        try:
            setattr(mat.par, p, v)
        except Exception as e:
            log('  mat.%s=%r failed: %s' % (p, v, e))
    geo.par.material = mat

    rnd = root.create(_td('renderTOP'), 'render_' + tag)
    rnd.par.geometry = geo
    rnd.par.camera = cam
    rnd.par.outputresolution = 'custom'
    rnd.par.resolutionw = RES
    rnd.par.resolutionh = RES
    rnd.par.format = 'rgba32float'
    for p, v in (('bgcolorr', 0), ('bgcolorg', 0), ('bgcolorb', 0), ('bgcolora', 0)):
        try:
            setattr(rnd.par, p, v)
        except Exception:
            pass
    try:
        rnd.par.antialias = '1'      # 1 = no AA (menu index); logged below if it differs
    except Exception as e:
        log('  render.antialias failed: %s' % e)
    return rnd


# expected: agent texel (x,y) -> (pixel col, pixel row from BOTTOM, rgb)
EXPECT = {
    (1, 1): (1.0, 0.0, 0.0),
    (6, 1): (0.0, 1.0, 0.0),
    (1, 6): (0.0, 0.0, 1.0),
    (6, 6): (1.0, 1.0, 1.0),
}


def _analyze(tag, rnd):
    try:
        import numpy as np
        a = rnd.numpyArray()           # H x W x 4 float
    except Exception:
        log('%s: numpyArray FAILED: %s' % (tag, traceback.format_exc().strip().splitlines()[-1]))
        return False
    H, W = a.shape[0], a.shape[1]
    log('%s: readback %dx%d  alpha[min=%.3f max=%.3f sum=%.3f]' % (
        tag, W, H, float(a[..., 3].min()), float(a[..., 3].max()), float(a[..., 3].sum())))
    lit = []
    for r in range(H):
        for c in range(W):
            px = a[r, c]
            if float(px[3]) > 0.001 or float(px[0]) + float(px[1]) + float(px[2]) > 0.001:
                lit.append((r, c, float(px[0]), float(px[1]), float(px[2]), float(px[3])))
    log('%s: %d lit pixel(s) (numpy row r=0..%d):' % (tag, len(lit), H - 1))
    for (r, c, rr, gg, bb, aa) in lit:
        log('    numpy[r=%d c=%d] rgba=(%.2f,%.2f,%.2f,%.2f)  row_from_bottom=%d' % (
            r, c, rr, gg, bb, aa, r))
    # PASS check: TD numpyArray row 0 = BOTTOM (GL origin; verified via the xyzTex gl_FragCoord
    # dump), so row-from-bottom == the numpy row index r directly.
    got = {}
    for (r, c, rr, gg, bb, aa) in lit:
        got[(c, r)] = (round(rr, 2), round(gg, 2), round(bb, 2))
    ok = True
    for (col, rowb), (er, eg, eb) in EXPECT.items():
        v = got.get((col, rowb))
        match = v is not None and abs(v[0] - er) < 0.05 and abs(v[1] - eg) < 0.05 and abs(v[2] - eb) < 0.05
        log('    expect pixel(col=%d,rowFromBottom=%d)=rgb(%.0f,%.0f,%.0f) -> %s %s' % (
            col, rowb, er, eg, eb, v, 'OK' if match else 'MISS'))
        ok = ok and match
    log('%s: %s' % (tag, 'PASS' if ok else 'FAIL'))
    try:
        rnd.save(os.path.join(OUT, 'probe_%s.png' % tag))
    except Exception:
        pass
    return ok


def probe_main():
    os.makedirs(OUT, exist_ok=True)
    _lines[:] = []
    log('points_probe start  stateSize=%d res=%d' % (STATE, RES))
    try:
        root = op('/')                                          # noqa: F821
        holder = root.op('nm_probe')
        if holder:
            holder.destroy()
        holder = root.create(_td('baseCOMP'), 'nm_probe')

        xyz = _glsl_top(holder, 'xyzTex', XYZ_FRAG, STATE, STATE)
        rgba = _glsl_top(holder, 'rgbaTex', RGBA_FRAG, STATE, STATE)
        cam = holder.create(_td('cameraCOMP'), 'cam')
        # identity-ish orthographic: view spans [-1,1] in X and (square) Y; agents sit at z=0.
        for p, v in (('projection', 'ortho'), ('orthowidth', 2.0), ('tz', 2.0),
                     ('near', 0.1), ('far', 10.0)):
            try:
                setattr(cam.par, p, v)
            except Exception as e:
                log('  cam.%s=%r failed: %s' % (p, v, e))

        log('-- build branch DIRECT (gl_Position = NDC) --')
        r_direct = _build_branch(holder, 'direct', xyz, rgba, cam, use_camera=False)

        # cook the source textures + render.
        for t in (xyz, rgba, r_direct):
            try:
                t.cook(force=True)
            except Exception:
                pass

        # sanity: dump the agent-state source texels.
        try:
            import numpy as np
            xa = xyz.numpyArray()
            log('xyzTex texels (numpy r,c -> pos.xy,w):')
            for r in range(xa.shape[0]):
                for c in range(xa.shape[1]):
                    log('    [r=%d c=%d] pos=(%.4f,%.4f) w=%.2f' % (
                        r, c, float(xa[r, c, 0]), float(xa[r, c, 1]), float(xa[r, c, 3])))
        except Exception as e:
            log('xyz dump failed: %s' % e)

        ok_d = _analyze('direct', r_direct)
        log('=== PROBE RESULT direct=%s ===' % ('PASS' if ok_d else 'FAIL'))
    except Exception:
        log('PROBE EXCEPTION:\n' + traceback.format_exc())
    log('=== PROBE DONE ===')
    try:
        project.quit(force=True)                                # noqa: F821
    except Exception:
        pass
