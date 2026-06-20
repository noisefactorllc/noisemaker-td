"""parity/render-candidate.py — the TouchDesigner-side parity renderer.

Runs INSIDE TouchDesigner (launched headlessly-but-display-bound by parity/run.sh). It builds
the network for one golden graph JSON via the noisemaker runtime, renders one deterministic
frame, saves a candidate PNG, and quits. parity/run.sh then diffs candidate vs golden with
compare.py.

Invocation (run.sh sets these env vars, then launches TD with
  TOUCH_START_COMMAND="exec(open('.../parity/render-candidate.py').read())"):

  NM_RUNTIME  path to the `td/` dir (added to sys.path so `import noisemaker...` resolves)
  NM_GRAPH    path to the graph JSON to render
  NM_OUT      output candidate PNG path
  NM_SIZE     square render size (default 256; ≤1280 on the free Non-Commercial tier)
  NM_TIME     normalized 0..1 time (default 0.25; must match the golden's --time)
  NM_LOG      log file (default /tmp/nm_candidate_log.txt)
  NM_DONE     sentinel file written on success (default /tmp/nm_candidate_done.txt)

Why a startup script and not a prebuilt .toe: TD .toe files are binary and can't be authored
offline; building the network from Python at startup is the supported path (TD-PLATFORM-NOTES).
"""
import os
import sys
import traceback

_LOG = os.environ.get('NM_LOG', '/tmp/nm_candidate_log.txt')


def log(msg):
    try:
        with open(_LOG, 'a') as f:
            f.write(str(msg) + '\n')
    except Exception:
        pass


def _quit(code=0):
    try:
        project.quit(force=True)  # noqa: F821 — TD global
    except Exception as exc:
        log('quit-err %s' % exc)


def main():
    runtime = os.environ.get('NM_RUNTIME')
    graph = os.environ.get('NM_GRAPH')
    out = os.environ.get('NM_OUT')
    size = int(os.environ.get('NM_SIZE', '256'))
    t = float(os.environ.get('NM_TIME', '0.25'))
    done = os.environ.get('NM_DONE', '/tmp/nm_candidate_done.txt')

    log('--- render-candidate start ---')
    log('ver=%s graph=%s out=%s size=%d time=%s' % (getattr(app, 'version', '?'), graph, out, size, t))  # noqa: F821

    if not (runtime and graph and out):
        log('ERR missing NM_RUNTIME/NM_GRAPH/NM_OUT'); return _quit(2)
    if runtime not in sys.path:
        sys.path.insert(0, runtime)

    try:
        from noisemaker.runtime.nm_renderer import NMRenderer
    except Exception:
        log('ERR import noisemaker.runtime: ' + traceback.format_exc()); return _quit(3)

    try:
        # deterministic: render each frame to completion, no realtime frame-dropping.
        try:
            project.realTime = False  # noqa: F821
        except Exception as exc:
            log('realTime set failed: %s' % exc)

        root = op('/')  # noqa: F821
        container = root.create(baseCOMP, 'nm_candidate')  # noqa: F821
        nm = NMRenderer(container, width=size, height=size)
        nm.set_graph(graph)
        log('built; warnings=%s' % getattr(nm.pipeline.backend, 'warnings', []))
        if nm.Output is None:
            log('ERR no Output TOP after build'); return _quit(4)

        saved = nm.render_to(out, time=t)
        log('saved=%s' % saved)

        # settle a few frames then re-save (state/feedback effects need warm-up; harmless for Tier-1).
        with open(done, 'w') as f:
            f.write('ok %s' % saved)
        log('DONE-OK')
    except Exception:
        log('ERR build/render: ' + traceback.format_exc())
        return _quit(5)
    return _quit(0)


main()
