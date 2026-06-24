"""make_bootstrap.py — create the committed `td/noisemaker.toe` host project.

TouchDesigner `.toe`/`.tox` files are binary and cannot be authored offline, so this is run
ONCE inside an activated TouchDesigner to materialize the bootstrap project. After that the
`.toe` can be opened directly and rebuilds itself on load.

Run it from TD's Textport (Alt/Option+T):
    >>> exec(open('/path/to/noisemaker-td/td/make_bootstrap.py').read())
(Note: there is NO headless startup hook — the `TOUCH_START_COMMAND` env var does not exist in build
2025.32820. Use the Textport method above, or an Execute DAT inside a `.toe` as the parity harness does.)

It builds:
    /project1/noisemaker        Base COMP, NMRenderer extension attached
    /project1/noisemaker/build   Execute DAT — onStart() builds a default graph
    /project1/out                Null TOP wired to the renderer Output (display/export)
and saves `td/noisemaker.toe`.
"""
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # noisemaker-td/
TD_DIR = os.path.join(REPO, 'td')
RUNTIME = TD_DIR
DEFAULT_GRAPH = os.path.join(REPO, 'parity', 'out', 'solid.graph.json')

BUILD_DAT_TEXT = '''# noisemaker bootstrap — rebuilds the network when the project loads.
import sys, os
RUNTIME = %r
if RUNTIME not in sys.path:
    sys.path.insert(0, RUNTIME)

def onStart():
    from noisemaker.runtime.nm_renderer import NMRenderer
    comp = parent()
    nm = NMRenderer(comp, width=256, height=256)
    comp.store('nm', nm)
    graph = %r
    if os.path.exists(graph):
        nm.set_graph(graph)
        out = op('../out')
        if out is not None and nm.Output is not None:
            out.inputConnectors[0].connect(nm.Output)
    return

def onCreate():
    onStart()
    return
''' % (RUNTIME, DEFAULT_GRAPH)


def build():
    root = op('/')                                   # noqa: F821
    proj = op('/project1') or root                   # noqa: F821
    # clean prior
    old = proj.op('noisemaker')
    if old:
        old.destroy()
    comp = proj.create(baseCOMP, 'noisemaker')       # noqa: F821
    comp.nodeX, comp.nodeY = 0, 0

    dat = comp.create(executeDAT, 'build')           # noqa: F821
    dat.text = BUILD_DAT_TEXT
    for p in ('start', 'create'):
        try:
            setattr(dat.par, p, True)
        except Exception:
            pass

    out = proj.op('out') or proj.create(nullTOP, 'out')   # noqa: F821
    out.nodeX, out.nodeY = 300, 0

    # build immediately too (so a save captures a cooked network)
    import sys
    if RUNTIME not in sys.path:
        sys.path.insert(0, RUNTIME)
    from noisemaker.runtime.nm_renderer import NMRenderer
    nm = NMRenderer(comp, width=256, height=256)
    comp.store('nm', nm)
    if os.path.exists(DEFAULT_GRAPH):
        nm.set_graph(DEFAULT_GRAPH)
        if nm.Output is not None:
            out.inputConnectors[0].connect(nm.Output)

    toe = os.path.join(TD_DIR, 'noisemaker.toe')
    project.save(toe)                                # noqa: F821
    try:
        debug('[make_bootstrap] saved ' + toe)      # noqa: F821
    except Exception:
        print('[make_bootstrap] saved', toe)


build()
