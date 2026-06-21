#!/usr/bin/env python3
"""check_graph.py — gate the Python compiler's normalized graph against the export-graph.mjs
oracle (the authoritative compiler-parity gate; mirrors hlsl parity/graph-diff.py).

For each DSL file: produce the reference graph via tools/export-graph.mjs (oracle) and compile
the same source with noisemaker.compiler.dsl_compiler (port), then structural deep-diff ignoring
top-level `id`/`source` (a hash + the input text). Numbers compare by value (5 == 5.0).

Classification:
  PASS   graph matches the oracle (0 deltas)
  DIFF   graph mismatch (prints the first delta path)
  STAGE  the port raised UnsupportedDsl (compute/MRT/3D-agent pass) — staged exactly as hlsl
  ERR    other port error / oracle failure

Usage:
  parity/compiler/check_graph.py                 # all corpus + programs
  parity/compiler/check_graph.py path/to.dsl ... # specific files
"""
import glob
import json
import os
import subprocess
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, 'td'))

from noisemaker.compiler.dsl_compiler import compile_graph, CompileError      # noqa: E402
from noisemaker.compiler.lang.validator import UnsupportedDsl                 # noqa: E402
from noisemaker.compiler.lang.effect_registry import EffectRegistry           # noqa: E402

EXPORT = os.path.join(REPO, 'tools', 'export-graph.mjs')
_IGNORE_TOP = {'id', 'source'}
_REG = None


def reg():
    global _REG
    if _REG is None:
        _REG = EffectRegistry.load_from_directory()
    return _REG


def oracle(path):
    # Inherit NM_REFERENCE_ROOT from the environment (no '..' default — no sibling assumed on clone).
    # export-graph.mjs errors clearly (exit 3) if it is unset.
    r = subprocess.run(['node', EXPORT, '--file', path], capture_output=True, text=True)
    if r.returncode != 0:
        return None, (r.stderr or r.stdout or 'node failed').strip().splitlines()[-1]
    return json.loads(r.stdout), None


def diff(a, b, path='$'):
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            extra = sorted(set(b) - set(a))
            missing = sorted(set(a) - set(b))
            return path, 'keys missing=%s extra=%s' % (missing, extra)
        for k in a:
            d = diff(a[k], b[k], path + '.' + k)
            if d:
                return d
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return path, 'list len ref=%d mine=%d' % (len(a), len(b))
        for idx in range(len(a)):
            d = diff(a[idx], b[idx], '%s[%d]' % (path, idx))
            if d:
                return d
        return None
    if isinstance(a, bool) != isinstance(b, bool):
        return path, 'ref=%r mine=%r' % (a, b)
    if a == b:
        return None
    return path, 'ref=%r mine=%r' % (a, b)


def compare(path):
    ref, err = oracle(path)
    if ref is None:
        # The oracle REJECTED the program (invalid DSL) vs a tooling failure. When the reference
        # rejects, the port must reject too — that's parity, not a port bug.
        if 'ERR_COMPILATION_FAILED' in err or 'ERR_EXPANSION_FAILED' in err:
            try:
                with open(path) as f:
                    compile_graph(f.read(), reg())
            except Exception:
                return 'SKIP', 'both reject (invalid program)'
            return 'DIFF', 'oracle rejects but port compiled it'
        return 'ERR', 'oracle: ' + err
    try:
        with open(path) as f:
            got = compile_graph(f.read(), reg())
    except UnsupportedDsl as e:
        return 'STAGE', str(e).splitlines()[0][:80]
    except CompileError as e:
        return 'ERR', 'CompileError: %s' % e
    except Exception:
        return 'ERR', traceback.format_exc().strip().splitlines()[-1]
    exp = {k: v for k, v in ref.items() if k not in _IGNORE_TOP}
    act = {k: v for k, v in got.items() if k not in _IGNORE_TOP}
    d = diff(exp, act)
    if d is None:
        return 'PASS', ''
    return 'DIFF', '%s  %s' % (d[0], d[1])


def main():
    files = sys.argv[1:]
    if not files:
        files = (sorted(glob.glob(os.path.join(REPO, 'parity', 'corpus', '*.dsl')))
                 + sorted(glob.glob(os.path.join(REPO, 'parity', 'programs', '*.dsl'))))
    counts = {'PASS': 0, 'DIFF': 0, 'STAGE': 0, 'SKIP': 0, 'ERR': 0}
    for f in files:
        status, info = compare(f)
        counts[status] += 1
        name = os.path.relpath(f, REPO)
        if status not in ('PASS', 'SKIP'):
            print('%-32s %-6s %s' % (name, status, info))
    print('=== graph parity: %d PASS / %d DIFF / %d STAGE / %d SKIP / %d ERR  (of %d) ===' % (
        counts['PASS'], counts['DIFF'], counts['STAGE'], counts['SKIP'], counts['ERR'], len(files)))
    sys.exit(1 if (counts['DIFF'] or counts['ERR']) else 0)


if __name__ == '__main__':
    main()
