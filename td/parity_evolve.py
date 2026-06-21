"""parity_evolve.py — multi-frame STATEFUL render driver (sims / particles / the flagship).

`parity_render_all.py` force-cooks ONE frame — fine for stateless effects, useless for stateful
ones (navierStokes, particle agents, trails) whose Feedback TOPs only latch on a REAL engine
frame tick. This driver instead evolves the network over N real frames of non-real-time timeline
playback: each frame advances engine time by `timestep`, the graph cooks, the Feedback TOPs latch
their cross-frame state, and at the requested sample frames we save the presented surface.

It reproduces the reference golden protocol (parity/batch-golden.mjs): a CLEAN zeroed start (a
fresh network's Feedback TOPs are black) then exactly N-frames-from-zero with time advancing
`(base + i*timestep) % 1` per frame — the same contract the reference WebGL2 golden uses.

Driven by an Execute DAT authored by build_evolve_toe.py:
    onStart      -> evolve_setup()          build network, start non-real-time playback
    onFrameStart -> evolve_frame_start(f)    advance the engine `time` uniform
    onFrameEnd   -> evolve_frame_end(f)      cook + capture at sample frames; quit at the end

Env knobs:
    NM_PROGRAM   program name; dsl at parity/corpus/<name>.dsl (default 'navierStokes')
    NM_SIZE      render size (default 256)
    NM_FRAMES    total frames to evolve (default 1800 = 30s at 60fps)
    NM_TIMESTEP  normalized time advance per frame (default 0.0016667 = 1/600)
    NM_TIME      base normalized time (default 0.25)
    NM_SAMPLES   comma-separated frames to capture (default = NM_FRAMES)

Writes parity/out/<name>.f<NNNN>.candidate.png + logs to parity/out/_evolve_log.txt.
"""
import os
import sys
import traceback


def _find_repo():
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
LOG = os.path.join(OUT, '_evolve_log.txt')
if TD_DIR not in sys.path:
    sys.path.insert(0, TD_DIR)

PROG = os.environ.get('NM_PROGRAM', 'navierStokes')
SIZE = int(os.environ.get('NM_SIZE', '256'))
FRAMES = int(os.environ.get('NM_FRAMES', '1800'))
TIMESTEP = float(os.environ.get('NM_TIMESTEP', '0.0016667'))
BASE_TIME = float(os.environ.get('NM_TIME', '0.25'))
SAMPLES = sorted({int(x) for x in (os.environ.get('NM_SAMPLES') or str(FRAMES)).split(',') if x.strip()})

_lines = []
_state = {}


def log(m):
    _lines.append(str(m))
    try:
        print('[evolve]', m)
    except Exception:
        pass
    _flush()


def _flush():
    try:
        with open(LOG, 'w') as f:
            f.write('\n'.join(_lines) + '\n')
    except Exception:
        pass


def evolve_setup():
    """Build the network and start non-real-time playback (called from onStart)."""
    os.makedirs(OUT, exist_ok=True)
    _lines[:] = []
    log('setup %s size=%d frames=%d ts=%g base=%g samples=%s' % (
        PROG, SIZE, FRAMES, TIMESTEP, BASE_TIME, SAMPLES))
    try:
        from noisemaker.runtime.nm_renderer import NMRenderer
    except Exception:
        log('FATAL import noisemaker.runtime:\n' + traceback.format_exc())
        _quit()
        return

    dsl_path = os.path.join(REPO, 'parity', 'corpus', '%s.dsl' % PROG)
    if not os.path.exists(dsl_path):
        dsl_path = os.path.join(REPO, 'parity', 'programs', '%s.dsl' % PROG)
    if not os.path.exists(dsl_path):
        log('no dsl for %s (looked in parity/corpus + parity/programs)' % PROG)
        _quit()
        return

    root = op('/')                                              # noqa: F821
    holder = root.op('nm_evolve') or root.create(baseCOMP, 'nm_evolve')   # noqa: F821
    sub = holder.op(PROG)
    if sub:
        sub.destroy()
    sub = holder.create(baseCOMP, PROG)                         # noqa: F821

    try:
        nm = NMRenderer(sub, width=SIZE, height=SIZE, time=BASE_TIME)
        with open(dsl_path) as f:
            nm.set_dsl(f.read())
    except Exception:
        log('BUILD FAIL:\n' + traceback.format_exc())
        _quit()
        return

    out = nm.Output
    backend = nm.pipeline.backend
    # DEBUG: dump an intermediate surface (the TOP that last writes a texId) instead of the
    # presented surface, to bisect a multi-pass sim (e.g. NM_DUMP_TEXID=global_ns_velocity_chain_1).
    dump_prog = os.environ.get('NM_DUMP_PROG')   # dump the Nth TOP built from a prog (e.g. nsSplat)
    if dump_prog:
        name, _, idx = dump_prog.partition(':')
        tops = getattr(backend, '_prog_tops', {}).get(name, [])
        i = int(idx) if idx else 0
        if tops and i < len(tops):
            out = tops[i]
            log('DUMP prog %s[%d] -> %s' % (name, i, out.path))
        else:
            log('DUMP prog %s NOT FOUND; progs: %s' % (name, sorted(getattr(backend, '_prog_tops', {}).keys())))
    dump_tex = os.environ.get('NM_DUMP_TEXID')
    if dump_tex and dump_tex.startswith('fb:'):
        dump_tex = dump_tex[3:]
        t = getattr(backend, '_feedback', {}).get(dump_tex)
        out = t if t is not None else out
        log('DUMP feedback TOP for %s -> %s' % (dump_tex, t.path if t else 'NOT FOUND'))
    elif dump_tex:
        t = backend.tex_top.get(dump_tex)
        if t is None and dump_tex in getattr(backend, '_feedback', {}):
            t = backend._feedback.get(dump_tex)        # dump the Feedback TOP itself (prev-frame state)
            log('DUMP feedback TOP for %s' % dump_tex)
        if t is not None:
            out = t
            log('DUMP surface %s -> %s' % (dump_tex, t.path))
        else:
            log('DUMP surface %s NOT FOUND; tex_top keys: %s feedback keys: %s' % (
                dump_tex, sorted(backend.tex_top.keys())[:40], sorted(getattr(backend, '_feedback', {}).keys())))
    log('built: out=%s has_feedback=%s ops=%d feedbacks=%d' % (
        out.path if out else None, getattr(backend, 'has_feedback', '?'),
        len(backend.ops), len(getattr(backend, '_feedback', {}))))
    for w in getattr(backend, 'warnings', [])[:24]:
        log('  warn: ' + w)
    if out is None:
        log('no Output TOP — abort')
        _quit()
        return

    _state.update(nm=nm, out=out, done=False)

    # Non-real-time playback: cook EVERY frame (no realtime frame-dropping), so the evolution is
    # deterministic and the Feedback TOPs latch once per frame.
    try:
        project.realTime = False                                # noqa: F821
    except Exception as e:
        log('realTime set failed: %s' % e)
    try:
        t = root.time
        t.rangeStart = 1
        t.rangeEnd = FRAMES + 1
        t.start = 1
        t.end = FRAMES + 1
        t.frame = 1
        t.play = 1
    except Exception:
        log('timeline setup failed:\n' + traceback.format_exc())
    log('playback armed — evolving %d frames' % FRAMES)


def evolve_frame_start(frame):
    """Advance the engine `time` uniform before this frame cooks (called from onFrameStart)."""
    nm = _state.get('nm')
    if nm is None or _state.get('done'):
        return
    i = max(0, int(frame) - 1)
    tt = (BASE_TIME + i * TIMESTEP) % 1.0
    try:
        nm.pipeline.set_time(tt)
    except Exception:
        pass


def evolve_frame_end(frame):
    """Cook + capture at sample frames; quit after the last frame (called from onFrameEnd)."""
    if _state.get('done'):
        return
    out = _state.get('out')
    f = int(frame)
    if out is not None:
        try:
            out.cook(force=True)                # pull the graph (Feedback TOPs latch this frame)
        except Exception:
            pass
    if f in SAMPLES and out is not None:
        path = os.path.join(OUT, '%s.f%04d.candidate.png' % (PROG, f))
        try:
            saved = out.save(path)
            stats = ''
            try:
                a = out.numpyArray()           # HxWx4 float (TD: 0..1 for fixed, raw for float)
                import numpy as _np
                mag = _np.hypot(a[..., 0], a[..., 1])   # R,G = velocity for sim-state surfaces
                stats = ' R[%.3f,%.3f] G[%.3f,%.3f] B.mean=%.3f velMag(max=%.3f mean=%.4f)' % (
                    float(a[..., 0].min()), float(a[..., 0].max()),
                    float(a[..., 1].min()), float(a[..., 1].max()),
                    float(a[..., 2].mean()), float(mag.max()), float(mag.mean()))
            except Exception:
                pass
            log('sample f%04d -> %s  (%dx%d)%s' % (
                f, os.path.basename(str(saved)), out.width, out.height, stats))
        except Exception:
            log('sample f%04d save FAIL: %s' % (f, traceback.format_exc().strip().splitlines()[-1]))
    if f >= FRAMES:
        log('=== DONE %d frames, %d samples ===' % (FRAMES, len(SAMPLES)))
        _quit()


def _quit():
    _state['done'] = True
    try:
        project.realTime = True                                 # noqa: F821
    except Exception:
        pass
    _flush()
    try:
        project.quit(force=True)                                # noqa: F821
    except Exception:
        pass
