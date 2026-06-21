"""parity_render_all.py — render all Tier-1 candidates from TD's Textport.

Because TouchDesigner has no honored startup-script env var, the parity bring-up runs inside a
live TD session. Paste ONE line into the Textport (Alt/Option+T):

    exec(open('/Users/alex/platform/noisemaker-td/td/parity_render_all.py').read())

It builds each Tier-1 graph via the noisemaker runtime, renders one deterministic frame, saves
`parity/out/<prog>.candidate.png`, and logs per-effect results to `parity/out/_render_log.txt`
(also printed). It does NOT quit — your session stays open so we can iterate. Re-run the same
line after any fix.
"""
import os
import sys
import traceback

def _find_repo():
    # The bootstrap .toe execs this with __file__ set (see build_parity_toe.py); the Textport
    # path can instead set NM_TD_REPO. No hardcoded dev path in committed source.
    try:
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    except NameError:
        pass
    env = os.environ.get('NM_TD_REPO')
    if env and os.path.isdir(os.path.join(env, 'td', 'noisemaker')):
        return env
    raise RuntimeError('cannot locate the noisemaker-td repo: set NM_TD_REPO or run via the .toe')

REPO = _find_repo()
TD_DIR = os.path.join(REPO, 'td')
OUT = os.path.join(REPO, 'parity', 'out')
LOG = os.path.join(OUT, '_render_log.txt')
TIER1 = (os.environ.get('NM_PROGRAMS')
         or 'solid,noise,cell,gradient,shape,osc2d,blur,blendMode').split(',')
SIZE = int(os.environ.get('NM_SIZE', '256'))
TIME = float(os.environ.get('NM_TIME', '0.25'))

if TD_DIR not in sys.path:
    sys.path.insert(0, TD_DIR)

_lines = []
def log(m):
    _lines.append(str(m))
    try:
        print('[nm]', m)
    except Exception:
        pass


def render_all():
    os.makedirs(OUT, exist_ok=True)
    try:
        project.realTime = False                      # noqa: F821 — deterministic
    except Exception as e:
        log('realTime set failed: %s' % e)

    try:
        from noisemaker.runtime.nm_renderer import NMRenderer
    except Exception:
        log('FATAL import noisemaker.runtime:\n' + traceback.format_exc())
        _flush(); return

    root = op('/')                                     # noqa: F821
    holder = root.op('nm_parity') or root.create(baseCOMP, 'nm_parity')   # noqa: F821

    ok = 0
    for prog in TIER1:
        graph = os.path.join(OUT, '%s.graph.json' % prog)
        cand = os.path.join(OUT, '%s.candidate.png' % prog)
        if not os.path.exists(graph):
            log('%-10s SKIP (no graph json)' % prog); continue
        # fresh sub-container per effect so networks don't collide
        sub = holder.op(prog)
        if sub:
            sub.destroy()
        sub = holder.create(baseCOMP, prog)
        try:
            nm = NMRenderer(sub, width=SIZE, height=SIZE)
            nm.set_graph(graph)
            warns = getattr(nm.pipeline.backend, 'warnings', [])
            out = nm.Output
            if out is None:
                log('%-10s FAIL: no Output TOP   warns=%s' % (prog, warns)); continue
            out.cook(force=True)
            errs = out.errors() if hasattr(out, 'errors') else ''
            saved = out.save(cand)
            log('%-10s OK -> %s  (%dx%d)%s%s' % (
                prog, os.path.basename(str(saved)), out.width, out.height,
                ('  warns=%s' % warns) if warns else '',
                ('  ERRORS=%s' % errs) if errs else ''))
            ok += 1
        except Exception:
            tb = traceback.format_exc().strip().splitlines()
            loc = next((l.strip() for l in reversed(tb) if l.strip().startswith('File ')), '')
            log('%-10s EXC: %s   [%s]' % (prog, tb[-1], loc))
    log('=== DONE %d/%d rendered ===' % (ok, len(TIER1)))
    _flush()


def _flush():
    try:
        with open(LOG, 'w') as f:
            f.write('\n'.join(_lines) + '\n')
    except Exception:
        pass


render_all()
