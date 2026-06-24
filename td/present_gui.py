"""present_gui.py — GUI variant of parity_evolve.py for an in-app TouchDesigner screenshot.

Same build + non-real-time-playback evolution as parity_evolve.py (so the displayed state is the
real N-frames-from-zero evolution of the flagship), but instead of quitting at the end it sets up a
clean view (network editor framed on the built graph + a floating viewer of the output TOP), writes
a READY marker, and LEAVES TD OPEN so the host can screencapture the actual application.

Driven by an Execute DAT authored by build_present_gui_toe.py:
    onStart      -> evolve_setup()        build network, start non-real-time playback
    onFrameStart -> evolve_frame_start(f)  advance the engine `time` uniform
    onFrameEnd   -> evolve_frame_end(f)    cook at sample frames; at the end set up the view + READY

Env knobs match parity_evolve.py: NM_PROGRAM, NM_SIZE, NM_FRAMES, NM_TIMESTEP, NM_TIME, NM_SAMPLES.
Writes parity/out/_present_log.txt and parity/out/_present_ready.txt.
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
LOG = os.path.join(OUT, '_present_log.txt')
READY = os.path.join(OUT, '_present_ready.txt')
if TD_DIR not in sys.path:
    sys.path.insert(0, TD_DIR)

PROG = os.environ.get('NM_PROGRAM', 'present_hero')
SIZE = int(os.environ.get('NM_SIZE', '1024'))
FRAMES = int(os.environ.get('NM_FRAMES', '1800'))
TIMESTEP = float(os.environ.get('NM_TIMESTEP', '0.0016667'))
BASE_TIME = float(os.environ.get('NM_TIME', '0.25'))
SAMPLES = sorted({int(x) for x in (os.environ.get('NM_SAMPLES') or str(FRAMES)).split(',') if x.strip()})

_lines = []
_state = {}


def log(m):
    _lines.append(str(m))
    try:
        print('[present]', m)
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
    try:
        if os.path.exists(READY):
            os.remove(READY)
    except Exception:
        pass
    log('setup %s size=%d frames=%d ts=%g base=%g samples=%s' % (
        PROG, SIZE, FRAMES, TIMESTEP, BASE_TIME, SAMPLES))
    try:
        from noisemaker.runtime.nm_renderer import NMRenderer
    except Exception:
        log('FATAL import noisemaker.runtime:\n' + traceback.format_exc())
        return

    dsl_path = os.path.join(REPO, 'parity', 'programs', '%s.dsl' % PROG)
    if not os.path.exists(dsl_path):
        dsl_path = os.path.join(REPO, 'parity', 'corpus', '%s.dsl' % PROG)
    if not os.path.exists(dsl_path):
        log('no dsl for %s (looked in parity/programs + parity/corpus)' % PROG)
        return

    root = op('/')                                              # noqa: F821
    holder = root.op('nm_present') or root.create(baseCOMP, 'nm_present')   # noqa: F821
    safe = ''.join(c if (c.isalnum() or c == '_') else '_' for c in PROG) or 'prog'
    sub = holder.op(safe)
    if sub:
        sub.destroy()
    sub = holder.create(baseCOMP, safe)                         # noqa: F821

    try:
        nm = NMRenderer(sub, width=SIZE, height=SIZE, time=BASE_TIME)
        with open(dsl_path) as f:
            nm.set_dsl(f.read())
    except Exception:
        log('BUILD FAIL:\n' + traceback.format_exc())
        return

    out = nm.Output
    backend = nm.pipeline.backend
    log('built: out=%s has_feedback=%s ops=%d feedbacks=%d' % (
        out.path if out else None, getattr(backend, 'has_feedback', '?'),
        len(backend.ops), len(getattr(backend, '_feedback', {}))))
    if out is None:
        log('no Output TOP — abort')
        return

    _state.update(nm=nm, out=out, sub=sub, holder=holder, done=False)

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
    """Cook at sample frames; at the final frame set up the view + READY (called from onFrameEnd)."""
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
            out.save(path)
            log('sample f%04d -> %s (%dx%d)' % (f, os.path.basename(path), out.width, out.height))
        except Exception:
            log('sample f%04d save FAIL: %s' % (f, traceback.format_exc().strip().splitlines()[-1]))
    if f >= FRAMES:
        _finish()


def _finish():
    """Stop playback at the evolved state, set up a clean in-app view, write READY, STAY OPEN."""
    _state['done'] = True
    out = _state.get('out')
    sub = _state.get('sub')
    try:
        root = op('/')                                          # noqa: F821
        root.time.play = 0                                      # freeze on the 30s-evolved frame
    except Exception:
        pass
    # the output TOP: viewer on, selected, current.
    try:
        out.viewer = True
        out.current = True
        out.selected = True
    except Exception as e:
        log('view: TOP flags failed: %s' % e)
    # frame the network editor on the built graph so the nodes (the compiled DSL) are visible.
    try:
        import td
        for p in ui.panes:                                     # noqa: F821
            if p.type == td.PaneType.NETWORKEDITOR:
                try:
                    p.owner = sub
                except Exception:
                    pass
                try:
                    p.home(zoom=True)
                except Exception:
                    try:
                        p.home()
                    except Exception:
                        pass
    except Exception as e:
        log('view: pane setup failed: %s' % e)
    # floating viewer window of the output TOP (the 30s render) over the network.
    try:
        out.openViewer(unique=True, borders=True)
    except Exception as e:
        log('view: openViewer failed: %s' % e)
    log('=== READY %s @ %d frames — view set, TD staying open ===' % (PROG, FRAMES))
    try:
        with open(READY, 'w') as f:
            f.write('%s %d %dx%d\n' % (PROG, FRAMES, SIZE, SIZE))
    except Exception:
        pass
    # restore real-time so the app feels live (the sim is frozen via time.play=0).
    try:
        project.realTime = True                                # noqa: F821
    except Exception:
        pass
