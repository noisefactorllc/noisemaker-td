"""array_probe.py — known-answer probe for binding a `uniform vec4 data[N]` onto a TD GLSL TOP.

The reference's std140 effects (synth/remap = `layout(std140) uniform { vec4 data[267]; }`) pack a
config into a large vec4 array. The Vectors page tops out at a handful of slots, so this needs the
GLSL TOP **Arrays** page (a "Uniform Array" sourced from a CHOP/DAT) — the documented fallback in
uniform_binder. Two unknowns, both pinned here in one launch:
  1. THE API — which GLSL TOP parameters declare a uniform-array binding and what feeds the values
     (a Table DAT? a CHOP? how many params). We introspect the live par list, then try candidates.
  2. THE LAYOUT — given a flat float feed, how do values map to data[i].xyzw? The shader writes
     data[i] into pixel i, so reading pixel i back tells us the row->vec4 mapping directly.

Known answer: data[i] = (i*0.1+0.01, i*0.1+0.02, i*0.1+0.03, i*0.1+0.04) for i in 0..3, fed as a flat
16-float sequence. pixel0==(0.01,0.02,0.03,0.04) ⇒ row-major flat (bind verbatim).

Driven via build_points_probe_toe.py with NM_PROBE_FILE=td/array_probe.py (onStart→probe_main).
Writes parity/out/_array_probe_log.txt and quits.
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
LOG = os.path.join(OUT, '_array_probe_log.txt')

N = 4   # array length (vec4)


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
        print('[aprobe]', m)
    except Exception:
        pass
    try:
        with open(LOG, 'w') as f:
            f.write('\n'.join(_lines) + '\n')
    except Exception:
        pass


# data[i] = (i*0.1 + 0.0{1..4}); flat row-major.
D = []
for i in range(N):
    D += [round(i * 0.1 + 0.01, 4), round(i * 0.1 + 0.02, 4), round(i * 0.1 + 0.03, 4), round(i * 0.1 + 0.04, 4)]

FRAG = """uniform vec4 data[%d];
layout(location = 0) out vec4 fragColor;
void main(){
    int i = int(gl_FragCoord.x);
    fragColor = TDOutputSwizzle(data[i]);
}
""" % N


def _make_top(holder):
    g = holder.create(_td('glslTOP'), 'atest')
    dat = holder.create(_td('textDAT'), 'atest_src')
    dat.text = FRAG
    g.par.pixeldat = dat
    try:
        g.par.glslversion = '4.60'
    except Exception:
        pass
    g.par.outputresolution = 'custom'
    g.par.resolutionw = N
    g.par.resolutionh = 1
    g.par.format = 'rgba32float'
    return g


def _readback(g):
    try:
        g.cook(force=True)
        a = g.numpyArray()    # H x W x 4 (row 0 = bottom); W=N
        return [(round(float(a[0, x, 0]), 3), round(float(a[0, x, 1]), 3),
                 round(float(a[0, x, 2]), 3), round(float(a[0, x, 3]), 3)) for x in range(N)]
    except Exception:
        log('  readback FAILED: %s' % traceback.format_exc().strip().splitlines()[-1])
        return None


def _err(g):
    try:
        return ' '.join((g.errors() or '').split())[:200]
    except Exception:
        return '?'


def _verdict(rows):
    if rows is None:
        return 'no-readback'
    r0 = rows[0]
    ok = abs(r0[0] - 0.01) < 0.005 and abs(r0[1] - 0.02) < 0.005 and abs(r0[2] - 0.03) < 0.005 and abs(r0[3] - 0.04) < 0.005
    if ok:
        return 'ROW-MAJOR-FLAT (bind D verbatim) ✓'
    if all(abs(v) < 1e-4 for v in r0):
        return 'ZERO (array never bound)'
    return 'UNKNOWN r0=%s' % (r0,)


def probe_main():
    os.makedirs(OUT, exist_ok=True)
    _lines[:] = []
    log('array_probe start — pin TD uniform-array api + layout. known D=%s' % D)
    try:
        root = op('/')                                          # noqa: F821
        holder = root.op('nm_aprobe')
        if holder:
            holder.destroy()
        holder = root.create(_td('baseCOMP'), 'nm_aprobe')

        g = _make_top(holder)

        # (0) discover: every par whose name hints at arrays/buffers.
        try:
            allp = sorted(p.name for p in g.pars())
            hits = [n for n in allp if any(k in n.lower() for k in ('aray', 'array', 'buffer', 'ubo', 'const'))]
            log('array-ish pars: %s' % hits)
            log('ALL par names (for reference): %s' % allp)
        except Exception as e:
            log('par introspection failed: %s' % e)

        # menu options for the array type pars (discovery).
        for pn in ('array0arraytype', 'array0type'):
            try:
                p = getattr(g.par, pn)
                log('%s menu: %s' % (pn, list(getattr(p, 'menuNames', []) or [])))
            except Exception as e:
                log('%s menu? %s' % (pn, e))

        results = []

        def _chop_4xN():
            """4 channels (x,y,z,w) x N samples. dattoCHOP reads each ROW as a channel with col0 as
            its name, so lay components out as rows: row c = [name, D[0*4+c], D[1*4+c], ...]."""
            dat = holder.op('adat')
            if dat:
                dat.destroy()
            dat = holder.create(_td('tableDAT'), 'adat')
            dat.clear()
            for c, nm in enumerate(('x', 'y', 'z', 'w')):
                dat.appendRow([nm] + [repr(D[i * 4 + c]) for i in range(N)])
            ch = holder.op('achop')
            if ch:
                ch.destroy()
            ch = holder.create(_td('dattoCHOP'), 'achop')
            ch.par.dat = dat
            for pn, pv in (('firstrow', 'values'), ('firstcol', 'names')):
                try:
                    setattr(ch.par, pn, pv)
                except Exception:
                    pass
            try:
                ch.cook(force=True)
                log('  chop chans=%d samples=%d names=%s' % (
                    ch.numChans, ch.numSamples, [c.name for c in ch.chans()][:6]))
            except Exception as e:
                log('  chop cook? %s' % e)
            return ch

        def bind_array(ch, eltype):
            setattr(g.par, 'array', 1)
            setattr(g.par, 'array0name', 'data')
            setattr(g.par, 'array0arraytype', 'uniformarray')   # mode
            setattr(g.par, 'array0type', eltype)                # element type
            setattr(g.par, 'array0chop', ch)
            return 'name=data mode=uniformarray type=%s chop=%s' % (eltype, ch.name)

        ch = _chop_4xN()
        try:
            how = bind_array(ch, 'vec4')
            rows = _readback(g)
            v = _verdict(rows)
            log('cand vec4/4chxNsamp -> %s | %s | rows=%s err=%s' % (how, v, rows, _err(g)))
            results.append(('vec4/4chxNsamp', v))
        except Exception as e:
            log('cand vec4/4chxNsamp EXC %s' % traceback.format_exc().strip().splitlines()[-1])

        log('=== ARRAY PROBE SUMMARY ===')
        for tag, v in results:
            log('  %-9s %s' % (tag, v))
    except Exception:
        log('PROBE EXCEPTION:\n' + traceback.format_exc())
    log('=== ARRAY PROBE DONE ===')
    try:
        project.quit(force=True)                                # noqa: F821
    except Exception:
        pass


probe_main_alias = probe_main
