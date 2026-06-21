"""parity_render_all.py — render all Tier-1 candidates from TD's Textport.

Because TouchDesigner has no honored startup-script env var, the parity bring-up runs inside a
live TD session. Paste ONE line into the Textport (Alt/Option+T):

    exec(open('/path/to/noisemaker-td/td/parity_render_all.py').read())

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


def _shader_errors(ops):
    """Collect compile errors from every op the backend built. The Output is a null TOP whose
    .errors() stays empty even when an upstream GLSL TOP fails to compile (and shows TD's red/blue
    placeholder), so the per-effect render line would falsely read OK without this. We walk the
    backend's own op list (not findChildren, whose depth semantics dropped the direct children)."""
    found = []
    try:
        for c in ops:
            try:
                err = (c.errors() or '') if hasattr(c, 'errors') else ''
            except Exception:
                err = ''
            if err:
                found.append('%s(%s) errors=%s' % (c.name, c.type, ' '.join(err.split())[:240]))
            # A GLSL TOP reports a *compile* failure only as a warning ("...has compile errors, use
            # Info DAT"); the actual error line lives in the Info DAT. Only dig there on that signal,
            # so a clean compile stays silent (no false ERR).
            if c.type in ('glsl', 'glslmulti'):
                try:
                    warn = (c.warnings() or '') if hasattr(c, 'warnings') else ''
                except Exception:
                    warn = ''
                if 'compile error' in warn.lower():
                    found.append('%s(%s) compile=%s' % (c.name, c.type, _glsl_compile_log(c)))
    except Exception:
        found.append('shader_errors raised: ' + traceback.format_exc().splitlines()[-1])
    return ' | '.join(found)


def _glsl_compile_log(glsl_top):
    """The real compile error for a GLSL TOP is only in an Info DAT (the TOP itself just warns).
    Spin one up, read the ERROR lines, tear it down. Called only when a compile error is known."""
    import td
    nfo = None
    try:
        nfo = glsl_top.parent().create(td.infoDAT, glsl_top.name + '_nfo')
        nfo.par.op = glsl_top.path
        nfo.cook(force=True)
        rows = [' '.join(c.val for c in row) for row in nfo.rows()]
        hits = [r for r in rows if 'ERROR' in r.upper()]
        return ' ⏎ '.join(hits or rows)[:400]
    except Exception as e:
        return 'info-dat failed: %s' % e
    finally:
        if nfo is not None:
            try:
                nfo.destroy()
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
        dsl = os.path.join(REPO, 'parity', 'programs', '%s.dsl' % prog)
        if not os.path.exists(dsl):  # blaster corpus comps live under parity/corpus/
            dsl = os.path.join(REPO, 'parity', 'corpus', '%s.dsl' % prog)
        cand = os.path.join(OUT, '%s.candidate.png' % prog)
        # NM_LIVE_DSL: render via the live in-engine Polymorphic compiler (nm.set_dsl) instead of
        # the offline golden graph JSON — validates the compiler runs in TD's embedded Python and
        # the live path renders identically (its graph is byte-clean vs export-graph.mjs).
        live = bool(os.environ.get('NM_LIVE_DSL'))
        if live and not os.path.exists(dsl):
            log('%-10s SKIP (no dsl)' % prog); continue
        if not live and not os.path.exists(graph):
            log('%-10s SKIP (no graph json)' % prog); continue
        # fresh sub-container per effect so networks don't collide
        sub = holder.op(prog)
        if sub:
            sub.destroy()
        sub = holder.create(baseCOMP, prog)
        try:
            nm = NMRenderer(sub, width=SIZE, height=SIZE)
            if live:
                with open(dsl) as _f:
                    nm.set_dsl(_f.read())
            else:
                nm.set_graph(graph)
            warns = getattr(nm.pipeline.backend, 'warnings', [])
            out = nm.Output
            if out is None:
                log('%-10s FAIL: no Output TOP   warns=%s' % (prog, warns)); continue
            # Cross-frame feedback effects must accumulate over the SAME frame count the golden
            # renderer used (export-and-render.mjs drives 8 frames); frame-invariant effects are
            # unchanged by extra cooks. Each step: cook the output (consumes the Feedback TOP's
            # stored previous-frame), then force-cook the Feedback TOPs to latch this frame's
            # producer output as the next previous-frame.
            backend = nm.pipeline.backend
            frames = int(os.environ.get('NM_FRAMES', '8')) if getattr(
                backend, 'has_feedback', False) else 1
            fbs = [o for o in backend.ops if o.type == 'feedback'] if frames > 1 else []
            base_frame = root.time.frame
            if os.environ.get('NM_DIAG'):
                log('  has_feedback=%s op-types=%s frames=%d base_frame=%s' % (
                    getattr(backend, 'has_feedback', '?'),
                    sorted(set(o.type for o in backend.ops)), frames, base_frame))
            for step in range(frames):
                # KNOWN LIMITATION (Phase 5.5): a TD Feedback TOP latches its target only on a real
                # engine frame tick (absTime.frame), which a synchronous onStart force-cook loop
                # never generates — stepping root.time.frame + force-cook is necessary but NOT
                # sufficient, so feedback effects render their frame-0 state (no accumulation).
                # Driving true accumulation needs an async realTime / Movie-File-Out frame loop.
                # The back-edge -> Feedback TOP topology is correct; only the offline driver is.
                if frames > 1:
                    try:
                        root.time.frame = base_frame + step
                    except Exception:
                        pass
                out.cook(force=True)
                for fb in fbs:
                    try:
                        fb.cook(force=True)
                    except Exception:
                        pass
                if os.environ.get('NM_DIAG') and fbs:
                    try:
                        a = out.numpyArray()
                        log('  frame %d (t=%s) out-mean=%.4f' % (step, root.time.frame, float(a.mean())))
                    except Exception as e:
                        log('  frame %d mean? %s' % (step, e))
            errs = out.errors() if hasattr(out, 'errors') else ''
            shader_errs = _shader_errors(nm.pipeline.backend.ops)   # GLSL compile errors (red/blue placeholder)
            saved = out.save(cand)
            log('%-10s %s -> %s  (%dx%d)%s%s%s' % (
                prog, 'ERR' if shader_errs else 'OK',
                os.path.basename(str(saved)), out.width, out.height,
                ('  warns=%s' % warns) if warns else '',
                ('  ERRORS=%s' % errs) if errs else '',
                ('  SHADER=%s' % shader_errs) if shader_errs else ''))
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
