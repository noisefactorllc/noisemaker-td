#!/usr/bin/env python3
"""corpus_classify.py — classify every blaster comp in parity/corpus/ for the live-corpus sweep.

Prints one `name<TAB>class` line per comp. Classes:
  stateless   single-pass (no agents / feedback / continuous solver) -> single-frame byte/SSIM parity
  agent       uses points/flow/smrticles -> chaotic agent flow, multi-frame, chaos-gated
  stateful    uses navierStokes / reactionDiffusion / mnca / cellularAutomata / feedback / motionBlur
  skip-ext    references an external-input effect (media/text/meshLoader/scope/spectrum/roll)
  skip-unknown  references an effect not in the catalog (a third-party / community effect)
  skip-error  compiles to nothing for another reason (the reference rejects it too)

Self-contained: uses the in-repo Python compiler + effect registry (no reference engine needed)."""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, 'td'))
from noisemaker.compiler.dsl_compiler import compile_graph          # noqa: E402
from noisemaker.compiler.lang.effect_registry import EffectRegistry  # noqa: E402

CORPUS = os.path.join(REPO, 'parity', 'corpus')
EXTERNAL = ('media', 'text', 'meshLoader', 'meshRender', 'scope', 'spectrum', 'roll')
AGENT = ('points', 'flow', 'smrticles', 'agents')
STATEFUL = ('navierStokes', 'reactionDiffusion', 'mnca', 'cellularAutomata', 'feedback', 'motionBlur')


def _uses(src, names):
    return any(re.search(r'\b%s\s*\(' % n, src) for n in names)


def classify():
    reg = EffectRegistry.load_from_directory()
    out = []
    for f in sorted(os.listdir(CORPUS)):
        if not f.endswith('.dsl'):
            continue
        name = f[:-4]
        src = open(os.path.join(CORPUS, f)).read()
        if _uses(src, EXTERNAL):
            out.append((name, 'skip-ext'))
            continue
        try:
            compile_graph(src, reg)
        except Exception as e:
            msg = (str(e).splitlines() or [''])[0].lower()
            unknown = 'unknown' in msg or 'no effect' in msg or 'not found' in msg
            out.append((name, 'skip-unknown' if unknown else 'skip-error'))
            continue
        cls = 'agent' if _uses(src, AGENT) else 'stateful' if _uses(src, STATEFUL) else 'stateless'
        out.append((name, cls))
    return out


if __name__ == '__main__':
    for name, cls in classify():
        print('%s\t%s' % (name, cls))
