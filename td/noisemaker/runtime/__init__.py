# noisemaker.runtime — the Render Graph → TouchDesigner network builder + helpers.
#
# Modules:
#   render_graph    — data model (TextureSpec / Pass / RenderGraph), pure Python.
#   graph_loader    — graph.json → RenderGraph.
#   dim             — resolveDimension(), exact port of reference/04 §9.
#   engine_uniforms — per-frame engine globals (reference/04 §10.1).
#   uniform_binder  — feed a {name: value} uniform dict onto a GLSL TOP's Vectors page.
#   td_backend      — the static network builder: RenderGraph → GLSL TOP graph.
#   surface_manager — Feedback TOPs for o0..o7 / state surfaces (cross-frame).
#   pipeline        — build orchestration + deterministic time driving.
#   nm_renderer     — host COMP extension (public API: set_dsl/set_uniform/resize/Output).
#
# Everything except render_graph/graph_loader/dim touches the TouchDesigner Python API
# (`op`, `parent`, `glslTOP`, …) and therefore only runs inside a TouchDesigner process.
