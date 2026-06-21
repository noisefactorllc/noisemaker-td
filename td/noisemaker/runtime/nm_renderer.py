"""Host COMP extension — the public Noisemaker API for a TouchDesigner component.

Attach as an extension on the `noisemaker` Base COMP (the .tox). It owns a Pipeline and exposes
a small host-facing surface. Mirrors `NMRenderer` (Unity) / `nm_renderer.gd` (Godot).

Public API:
    nm.set_graph(path)         build the network from a golden graph JSON (offline producer)
    nm.set_graph_dict(d)       build from an in-memory normalized graph dict
    nm.set_dsl(src)            Phase 6 — compile DSL live in-engine, then build
    nm.resize(w, h)            change render resolution (rebuilds)
    nm.Output                  the presented TOP (renderSurface) — wire to a Null/Out for display
    nm.render_to(path, time)   deterministic single-frame render (parity / export)

Touches the TouchDesigner Python API — only runs inside a TD process.
"""
import os

from .graph_loader import load_graph, load_graph_str
from .render_graph import RenderGraph
from .pipeline import Pipeline


class NMRenderer:
    def __init__(self, owner_comp, *, shaders_root=None, width=256, height=256, time=0.25):
        self.owner = owner_comp
        # default: shaders ship beside this package at ../shaders/effects
        self.shaders_root = shaders_root or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'shaders', 'effects')
        self.width = width
        self.height = height
        self.time = time
        self.pipeline = None
        self._graph = None

    # -- build paths -------------------------------------------------------
    def set_graph(self, path):
        return self._build(load_graph(path))

    def set_graph_dict(self, d):
        return self._build(RenderGraph.from_dict(d))

    def set_graph_str(self, text):
        return self._build(load_graph_str(text))

    def set_dsl(self, src):
        """Phase 6: compile the Polymorphic DSL live in-engine to a normalized graph, then build.
        Until the Python frontend (compiler/, ports of reference/01–03) lands, use the offline
        golden producer (`tools/export-graph.mjs`) + set_graph()."""
        try:
            from ..compiler import compile_graph  # noqa: F401 — Phase 6
        except Exception:
            raise NotImplementedError(
                'Live DSL compile is Phase 6. For now: '
                'node tools/export-graph.mjs --file prog.dsl out.graph.json  →  nm.set_graph(out)')
        return self._build(compile_graph(src))

    # -- host controls -----------------------------------------------------
    def resize(self, width, height):
        self.width = int(width)
        self.height = int(height)
        if self._graph is not None:
            self._build(self._graph)

    def render_to(self, path, time=0.25):
        if self.pipeline is None:
            return None
        return self.pipeline.render_to(path, time=time)

    @property
    def Output(self):
        return self.pipeline.output if self.pipeline else None

    # -- internal ----------------------------------------------------------
    def _build(self, graph):
        if self.pipeline is not None:
            self.pipeline.teardown()
        self._graph = graph
        self.pipeline = Pipeline(self.owner, self.shaders_root, width=self.width, height=self.height,
                                 time=self.time)
        return self.pipeline.build(graph)
