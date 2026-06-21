"""matrix_probe.py — known-answer probe for binding a `uniform mat3` onto a TD GLSL TOP.

`uniform_binder` only packs 1–4 floats onto the Vectors page, so `render/renderCubemap3d`'s
`uniform mat3 cubeBasis` (9 floats) can't bind — and an UNBOUND mat3 in TD defaults to the ZERO
matrix, making `normalize(cubeBasis * dir)` NaN (degenerate render). The default DSL uses the
IDENTITY basis, which can't distinguish "bound correctly" from "happened to default", so we probe
with a NON-identity matrix whose every element is distinct.

Two unknowns, both pinned here in one launch:
  1. THE API — which GLSL TOP parameters set a custom matrix uniform. We introspect the live par
     list (names containing 'mat') and then try the candidate apis in order, reporting which one
     TD accepts and makes the shader read back non-zero.
  2. THE ORDERING — given a flat 9-float array D, does TD load it COLUMN-major (like WebGL's
     uniformMatrix3fv(.., transpose=false), which is how the reference feeds cubeBasis) or
     ROW-major? The shader outputs `cubeBasis * e_k` (= column k) into pixel x=k, so:
        pixel x=0 == (D0,D1,D2) -> column-major (bind D verbatim)
        pixel x=0 == (D0,D3,D6) -> row-major     (transpose D before binding)

Writes parity/out/_matrix_probe_log.txt and quits. Driven by an Execute DAT authored by
build_points_probe_toe.py with NM_PROBE_FILE=td/matrix_probe.py.
"""
import os
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
LOG = os.path.join(OUT, '_matrix_probe_log.txt')


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
        print('[mprobe]', m)
    except Exception:
        pass
    try:
        with open(LOG, 'w') as f:
            f.write('\n'.join(_lines) + '\n')
    except Exception:
        pass


# Distinct-element basis (all in [0,1] so it survives any readback format). Flat array is the
# reference's column-major convention: column0=(.11,.12,.13) column1=(.21,.22,.23) column2=(.31,.32,.33).
D = [0.11, 0.12, 0.13, 0.21, 0.22, 0.23, 0.31, 0.32, 0.33]

FRAG = """uniform mat3 cubeBasis;
layout(location = 0) out vec4 fragColor;
void main(){
    int x = int(gl_FragCoord.x);
    vec3 c;
    if (x == 0)      c = cubeBasis * vec3(1.0, 0.0, 0.0);  // column 0
    else if (x == 1) c = cubeBasis * vec3(0.0, 1.0, 0.0);  // column 1
    else             c = cubeBasis * vec3(0.0, 0.0, 1.0);  // column 2
    fragColor = TDOutputSwizzle(vec4(c, 1.0));
}
"""


def _make_top(holder):
    g = holder.create(_td('glslTOP'), 'mtest')
    dat = holder.create(_td('textDAT'), 'mtest_src')
    dat.text = FRAG
    g.par.pixeldat = dat
    try:
        g.par.glslversion = '4.60'
    except Exception:
        pass
    g.par.outputresolution = 'custom'
    g.par.resolutionw = 3
    g.par.resolutionh = 1
    g.par.format = 'rgba32float'
    return g


def _readback(g):
    """Return [(col0), (col1), (col2)] as (r,g,b) tuples, or None."""
    try:
        g.cook(force=True)
        a = g.numpyArray()              # H x W x 4, row 0 = bottom; W=3
        return [(float(a[0, x, 0]), float(a[0, x, 1]), float(a[0, x, 2])) for x in range(3)]
    except Exception:
        log('  readback FAILED: %s' % traceback.format_exc().strip().splitlines()[-1])
        return None


def _err(g):
    try:
        return ' '.join((g.errors() or '').split())[:200]
    except Exception:
        return '?'


def _classify(cols):
    """Given the 3 read-back columns, decide ordering vs the known D."""
    if cols is None:
        return 'no-readback'
    c0 = cols[0]
    colmajor = abs(c0[0] - 0.11) < 0.02 and abs(c0[1] - 0.12) < 0.02 and abs(c0[2] - 0.13) < 0.02
    rowmajor = abs(c0[0] - 0.11) < 0.02 and abs(c0[1] - 0.21) < 0.02 and abs(c0[2] - 0.31) < 0.02
    zero = all(abs(v) < 1e-4 for v in c0)
    if colmajor:
        return 'COLUMN-MAJOR (bind D verbatim)'
    if rowmajor:
        return 'ROW-MAJOR (transpose D before binding)'
    if zero:
        return 'ZERO (uniform never bound — api had no effect)'
    return 'UNKNOWN c0=%s' % (c0,)


def probe_main():
    os.makedirs(OUT, exist_ok=True)
    _lines[:] = []
    log('matrix_probe start — pin TD mat3 uniform api + ordering. known D(colmajor)=%s' % D)
    try:
        root = op('/')                                          # noqa: F821
        holder = root.op('nm_mprobe')
        if holder:
            holder.destroy()
        holder = root.create(_td('baseCOMP'), 'nm_mprobe')

        g = _make_top(holder)

        # (0) discover: every par whose name hints at matrices.
        try:
            mpars = sorted(p.name for p in g.pars() if 'mat' in p.name.lower())
            log('matrix-ish pars on glslTOP: %s' % mpars)
        except Exception as e:
            log('par introspection failed: %s' % e)

        results = []

        # Discovered API: g.par.matrix (count), matrix0name (uniform), matrix0value (a Table DAT).
        # The open question is how a table maps to the GLSL mat3. We fill a 3x3 table DIRECTLY from
        # D reshaped row-by-row (table row r = D[3r:3r+3]) and read back the 3 columns:
        #   col0 == (.11,.12,.13)  -> table ROW r becomes GLSL COLUMN r  (rows-are-columns)
        #   col0 == (.11,.21,.31)  -> table ROW r becomes GLSL ROW r     (standard math matrix)
        def _set_matrix(dat):
            setattr(g.par, 'matrix', 1)
            setattr(g.par, 'matrix0name', 'cubeBasis')
            for ref in (dat, dat.name, dat.path):
                try:
                    setattr(g.par, 'matrix0value', ref)
                    return 'ref=%r' % (ref,)
                except Exception:
                    continue
            return 'ALL-REFS-FAILED'

        def cand_table(tag, rows, w):
            dat = holder.op('mtx')
            if dat:
                dat.destroy()
            dat = holder.create(_td('tableDAT'), 'mtx')
            dat.clear()
            for row in rows:
                dat.appendRow([repr(v) for v in row])
            how = _set_matrix(dat)
            cols = _readback(g)
            verdict = _classify(cols)
            log('cand %-14s (%dx%d %s) -> %s   cols=%s   err=%s' % (
                tag, len(rows), w, how, verdict, cols, _err(g)))
            results.append((tag, verdict, cols))
            try:
                setattr(g.par, 'matrix', 0)
            except Exception:
                pass

        # 3x3, D reshaped row-by-row.
        cand_table('3x3-direct', [[D[0], D[1], D[2]], [D[3], D[4], D[5]], [D[6], D[7], D[8]]], 3)
        # 4x4, D in the upper-left 3x3 (rows-are-columns layout), homogeneous last row/col.
        cand_table('4x4-direct', [
            [D[0], D[1], D[2], 0.0],
            [D[3], D[4], D[5], 0.0],
            [D[6], D[7], D[8], 0.0],
            [0.0, 0.0, 0.0, 1.0]], 4)

        log('=== MATRIX PROBE SUMMARY ===')
        for tag, verdict, _ in results:
            log('  %-16s %s' % (tag, verdict))
    except Exception:
        log('PROBE EXCEPTION:\n' + traceback.format_exc())
    log('=== MATRIX PROBE DONE ===')
    try:
        project.quit(force=True)                                # noqa: F821
    except Exception:
        pass
