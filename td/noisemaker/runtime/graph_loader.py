# graph_loader.py — graph.json -> RenderGraph. Pure Python (stock-python testable).
from __future__ import annotations
import json
from .render_graph import RenderGraph


def load_graph(path: str) -> RenderGraph:
    with open(path, "r") as f:
        return RenderGraph.from_dict(json.load(f))


def load_graph_str(text: str) -> RenderGraph:
    return RenderGraph.from_dict(json.loads(text))
