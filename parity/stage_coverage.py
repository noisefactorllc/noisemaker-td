#!/usr/bin/env python3
"""stage_coverage.py — stage parity DSL+golden pairs from a sibling Noisemaker port and
classify each by whether this TD port can render it yet.

A sibling port (default ../noisemaker-godot) renders its goldens from the SAME reference
WebGL2 engine off the SAME Polymorphic DSL, so every `<name>.dsl` / `<name>.golden.png`
pair is reusable verbatim — exactly how the 8 Tier-1 goldens were seeded. For each pair:

  1. copy the DSL into `parity/programs/` and the golden into `parity/out/`,
  2. regenerate the graph JSON via the reference compileGraph (`tools/export-graph.mjs`),
  3. inspect every *effect* pass's program: `effects/<ns>/<func>/<progName>.frag` must
     exist and not be MRT-flagged (MRT programs need Phase 5.5 hand-finishing).

A program whose graph references a missing or MRT `.frag` is DEFERred — never silently
skipped (the parity contract: log everything we can't yet cover, with a reason).

Env:
  NM_SIBLING_PORT   sibling port to reuse from   (default ../noisemaker-godot)
  NM_REFERENCE_ROOT reference engine root        (default ../noisemaker)

Usage:
  parity/stage_coverage.py                 # stage + classify; print a report
  parity/stage_coverage.py --emit render   # print only the space-separated RENDER list
                                           # (feed straight to: parity/run.sh "<list>")
"""
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SIB = os.environ.get('NM_SIBLING_PORT') or os.path.join(REPO, '..', 'noisemaker-godot')
REF = os.environ.get('NM_REFERENCE_ROOT') or os.path.join(REPO, '..', 'noisemaker')
PROGRAMS = os.path.join(REPO, 'parity', 'programs')
OUT = os.path.join(REPO, 'parity', 'out')
FRAGS = os.path.join(REPO, 'td', 'noisemaker', 'shaders', 'effects')
EXPORT = os.path.join(REPO, 'tools', 'export-graph.mjs')


def sibling_pairs():
    """Every (name, dsl_path, golden_path) the sibling port has rendered a golden for."""
    pdir = os.path.join(SIB, 'parity', 'programs')
    odir = os.path.join(SIB, 'parity', 'out')
    pairs = []
    for f in sorted(os.listdir(pdir)):
        if not f.endswith('.dsl'):
            continue
        name = f[:-4]
        golden = os.path.join(odir, name + '.golden.png')
        if os.path.exists(golden):
            pairs.append((name, os.path.join(pdir, f), golden))
    return pairs


def export_graph(name):
    """Reference compileGraph → parity/out/<name>.graph.json. Returns (path|None, err)."""
    dst = os.path.join(OUT, name + '.graph.json')
    env = dict(os.environ, NM_REFERENCE_ROOT=REF)
    r = subprocess.run(
        ['node', EXPORT, '--file', os.path.join(PROGRAMS, name + '.dsl'), dst],
        env=env, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(dst):
        msg = (r.stderr or r.stdout or 'unknown error').strip()
        return None, (msg.splitlines()[-1] if msg else 'unknown error')
    return dst, None


def frag_status(graph_path):
    """(ok, reasons) — ok iff every effect pass has an existing, non-MRT .frag."""
    with open(graph_path) as fh:
        graph = json.load(fh)
    missing, mrt = [], []
    for p in graph.get('passes', []):
        if p.get('passType') != 'effect':
            continue  # blit/copy passes are built by the backend, no .frag
        key = '%s/%s/%s' % (p.get('namespace'), p.get('func'), p.get('progName'))
        frag = os.path.join(FRAGS, p.get('namespace'), p.get('func'), p.get('progName') + '.frag')
        if not os.path.exists(frag):
            missing.append(key)
            continue
        with open(frag) as fh:
            if 'NM_OUTPUT: MRT' in fh.read(400):
                mrt.append(key)
    reasons = []
    if missing:
        reasons.append('missing frag: ' + ', '.join(sorted(set(missing))))
    if mrt:
        reasons.append('MRT (Phase 5.5): ' + ', '.join(sorted(set(mrt))))
    return (not reasons), reasons


def main():
    emit = sys.argv[sys.argv.index('--emit') + 1] if '--emit' in sys.argv else None
    os.makedirs(OUT, exist_ok=True)
    render, defer = [], []
    for name, dsl, golden in sibling_pairs():
        shutil.copyfile(dsl, os.path.join(PROGRAMS, name + '.dsl'))
        shutil.copyfile(golden, os.path.join(OUT, name + '.golden.png'))
        graph, err = export_graph(name)
        if not graph:
            defer.append((name, 'export-graph failed: %s' % err))
            continue
        ok, reasons = frag_status(graph)
        if ok:
            render.append(name)
        else:
            defer.append((name, '; '.join(reasons)))

    if emit == 'render':
        print(' '.join(render))
        return

    with open(os.path.join(OUT, '_render_set.txt'), 'w') as fh:
        fh.write(' '.join(render) + '\n')
    print('=== staged %d reusable pairs from %s ===' % (
        len(render) + len(defer), os.path.relpath(SIB, REPO)))
    print('RENDER (%d):\n  %s' % (len(render), ' '.join(render)))
    print('DEFER  (%d):' % len(defer))
    for name, reason in defer:
        print('  %-18s %s' % (name, reason))


if __name__ == '__main__':
    main()
