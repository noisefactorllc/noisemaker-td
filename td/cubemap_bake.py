"""cubemap_bake.py — bake the 6 cube faces of a renderCubemap DSL in TouchDesigner.

The single-face `renderCubemap3d`/`renderCubemapSurface` render uses the DSL-default cubeBasis
(identity). A full cubemap is HOST-DRIVEN (reference `Pipeline.renderCubemap`): render the SAME
graph 6 times, setting `cubeBasis` to each face's basis between renders, and read back each face.
That host loop is exactly what the new mat3 uniform binding (uniform_binder Matrices page) unlocks
here — set `bound['cubeBasis']` to the face basis, re-bind (refills the cubeBasis Table DAT), cook,
save. Only the cube-camera TOP + its downstream re-cook; the upstream volume is computed once.

Per-face bases mirror reference `renderer/cubeCamera.js` faceBasisMat3: column-major [right|up|
forward] with right = cross(up, forward); GL face order +X,-X,+Y,-Y,+Z,-Z. The shader computes
dir = normalize(cubeBasis * vec3(u, -v, 1)). uniform_binder transposes the column-major array into
the TD table, so we pass these flat arrays verbatim (same values the reference feeds uniformMatrix3fv).

Driven via build_points_probe_toe.py with NM_PROBE_FILE=td/cubemap_bake.py (its onStart calls
probe_main, aliased to bake_main below). Env: NM_PROGRAM (dsl in parity/programs or parity/corpus),
NM_SIZE (default 256). Writes parity/out/<prog>.face<k>.candidate.png (k=0..5) + a log.
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
TD_DIR = os.path.join(REPO, 'td')
OUT = os.path.join(REPO, 'parity', 'out')
LOG = os.path.join(OUT, '_cubemap_log.txt')
if TD_DIR not in sys.path:
    sys.path.insert(0, TD_DIR)

PROG = os.environ.get('NM_PROGRAM', 'synth3d_renderCubemapSurface')
SIZE = int(os.environ.get('NM_SIZE', '256'))


def log(m):
    _lines.append(str(m))
    try:
        print('[cubemap]', m)
    except Exception:
        pass
    try:
        with open(LOG, 'w') as f:
            f.write('\n'.join(_lines) + '\n')
    except Exception:
        pass


# GL cube face order: +X,-X,+Y,-Y,+Z,-Z. (forward, up) per face — mirrors cubeCamera.CUBE_FACES.
_CUBE_FACES = [
    ([1, 0, 0], [0, -1, 0]),
    ([-1, 0, 0], [0, -1, 0]),
    ([0, 1, 0], [0, 0, 1]),
    ([0, -1, 0], [0, 0, -1]),
    ([0, 0, 1], [0, -1, 0]),
    ([0, 0, -1], [0, -1, 0]),
]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _face_basis(face):
    """Column-major [right | up | forward] mat3, right = cross(up, forward)."""
    fwd, up = _CUBE_FACES[face]
    r = _cross(up, fwd)
    return [r[0], r[1], r[2], up[0], up[1], up[2], fwd[0], fwd[1], fwd[2]]


CUBE_FACE_BASES = [_face_basis(f) for f in range(6)]


def bake_main():
    os.makedirs(OUT, exist_ok=True)
    _lines[:] = []
    log('cubemap bake %s size=%d' % (PROG, SIZE))
    try:
        project.realTime = False                                # noqa: F821
    except Exception:
        pass

    try:
        from noisemaker.runtime.nm_renderer import NMRenderer
        from noisemaker.runtime import uniform_binder
    except Exception:
        log('FATAL import:\n' + traceback.format_exc())
        _quit()
        return

    dsl_path = os.path.join(REPO, 'parity', 'programs', '%s.dsl' % PROG)
    if not os.path.exists(dsl_path):
        dsl_path = os.path.join(REPO, 'parity', 'corpus', '%s.dsl' % PROG)
    if not os.path.exists(dsl_path):
        log('no dsl for %s' % PROG)
        _quit()
        return

    import td as _td
    root = op('/')                                              # noqa: F821
    holder = root.op('nm_cubemap') or root.create(_td.baseCOMP, 'nm_cubemap')   # noqa: F821
    safe = ''.join(c if (c.isalnum() or c == '_') else '_' for c in PROG) or 'prog'
    sub = holder.op(safe)
    if sub:
        sub.destroy()
    sub = holder.create(_td.baseCOMP, safe)

    try:
        nm = NMRenderer(sub, width=SIZE, height=SIZE)
        with open(dsl_path) as f:
            nm.set_dsl(f.read())
    except Exception:
        log('BUILD FAIL:\n' + traceback.format_exc())
        _quit()
        return

    backend = nm.pipeline.backend
    out = nm.Output
    if out is None:
        log('no Output TOP — abort')
        _quit()
        return

    # Find the cube-camera TOP: the effect TOP whose declared uniforms include cubeBasis.
    target = None
    for g, bound in getattr(backend, '_effect_uniforms', []):
        if 'cubeBasis' in bound:
            target = (g, bound)
            break
    if target is None:
        log('no cubeBasis uniform found — is this a renderCubemap DSL? abort')
        _quit()
        return
    g, bound = target
    log('cube-camera TOP = %s' % g.path)

    means = []
    for face in range(6):
        bound['cubeBasis'] = CUBE_FACE_BASES[face]
        try:
            uniform_binder.bind_uniforms(g, bound)              # refills the cubeBasis Table DAT
            g.cook(force=True)                                  # re-render this face
            out.cook(force=True)                                # pull it through to the output
        except Exception:
            log('face %d cook FAIL:\n%s' % (face, traceback.format_exc()))
            continue
        path = os.path.join(OUT, '%s.face%d.candidate.png' % (PROG, face))
        try:
            saved = out.save(path)
            try:
                a = out.numpyArray()
                m = (float(a[..., 0].mean()), float(a[..., 1].mean()), float(a[..., 2].mean()))
            except Exception:
                m = None
            means.append(m)
            log('face %d basis=%s -> %s (%dx%d) mean=%s' % (
                face, CUBE_FACE_BASES[face], os.path.basename(str(saved)), out.width, out.height, m))
        except Exception:
            log('face %d save FAIL: %s' % (face, traceback.format_exc().strip().splitlines()[-1]))

    distinct = len({m for m in means if m is not None})
    log('=== BAKE DONE: 6 faces, %d distinct face-means ===' % distinct)
    _quit()


probe_main = bake_main   # build_points_probe_toe.py calls probe_main()


def _quit():
    try:
        project.realTime = True                                 # noqa: F821
    except Exception:
        pass
    try:
        project.quit(force=True)                                # noqa: F821
    except Exception:
        pass
