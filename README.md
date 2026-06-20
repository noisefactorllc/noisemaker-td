# noisemaker-td

A structural port of the **Noisemaker** procedural-texture shader engine
(`../noisemaker/shaders`) to **Derivative TouchDesigner**. It mirrors the existing
Unity/HLSL (`../noisemaker-hlsl`) and Godot (`../noisemaker-godot`) ports: the same DSL,
the same effects, the same render-graph seam — rendered through a network of **GLSL TOP**
operators built programmatically in TouchDesigner Python, tolerance-parity to the JS/WebGL2
reference.

## How it works

The contract between "what to render" and "how this engine renders it" is the normalized
**Render Graph JSON** (`compileGraph(dsl) → {passes, programs, textures, renderSurface}`,
see `docs/GRAPH-JSON-SCHEMA.md`). Two producers emit it; one consumer builds the network:

```
 DSL ──► compileGraph ──► Render Graph JSON ──► TD network builder ──► GLSL TOP network
         (reference JS,         (the seam)        (Python, this repo)      (TD cooks it)
          or live Python
          frontend, Phase 6)
```

Because **TouchDesigner's GLSL TOP is OpenGL GLSL with the same bottom-left raster origin as
the reference's WebGL2 backend**, the per-effect shaders are translated **directly from the
reference GLSL** (not WGSL) by a mechanical transpiler — no Y-flip, no math edits. See
`ARCHITECTURE.md` for the full design and `PORTING-GUIDE.md` for the transpile rules.

## Layout

```
reference/             engine-agnostic specs 01–10 (copied verbatim; the shared brain)
tools/
  export-graph.mjs       reference compileGraph → golden graph JSON (reused, unchanged)
  convert-definitions.mjs effect definitions → effects/<ns>/<func>.json (reused, retargeted)
  convert-shaders.mjs     reference GLSL → TD .frag transpiler  (NEW — the port's centerpiece)
parity/
  programs/*.dsl          8 Tier-1 test programs
  compare.py              golden vs candidate (max-abs-diff + SSIM)   (reused)
  export-and-render.mjs   golden PNG via the reference WebGL2 engine  (reused)
  render-candidate.py     TD-side renderer: graph JSON → network → PNG (NEW)
  run.sh                  golden → candidate → compare                (NEW)
  out/                    generated artifacts (gitignored)
td/
  noisemaker/
    runtime/              the network builder (Python): render_graph, graph_loader, dim,
                          engine_uniforms, uniform_binder, td_backend, surface_manager,
                          pipeline, nm_renderer
    compiler/             live DSL frontend (Phase 6 — staged)
    shaders/effects/<ns>/<effect>/<prog>.frag   per-program TD GLSL (generated)
    effects/<ns>/<func>.json                    effect definitions (generated)
  make_bootstrap.py       builds the committed noisemaker.toe (run once inside TD)
docs/                   IMPLEMENTATION-PLAN, GRAPH-JSON-SCHEMA, TD-PLATFORM-NOTES
ARCHITECTURE.md  PORTING-GUIDE.md
```

## Build / regenerate

No new dependencies — reuses the reference Node tooling (Node 26) and Python 3 (numpy/pillow
for `compare.py`). All tools read the reference repo via `NM_REFERENCE_ROOT` (defaults to
`../noisemaker`).

```bash
# effect-definition JSON (182 effects)
NM_REFERENCE_ROOT=../noisemaker node tools/convert-definitions.mjs

# TD GLSL shaders (247 programs; 226 auto, 21 MRT flagged for manual finish)
NM_REFERENCE_ROOT=../noisemaker node tools/convert-shaders.mjs

# a golden graph JSON
NM_REFERENCE_ROOT=../noisemaker node tools/export-graph.mjs --file parity/programs/solid.dsl parity/out/solid.graph.json
```

## Parity

```bash
parity/run.sh solid        # one program     (needs an activated TouchDesigner — see below)
parity/run.sh all          # all 8 Tier-1
```
Targets, as on the sibling ports: **SSIM ≥ 0.98, max-abs-diff ≤ 1–2/255** (cross-device
bit-exactness is impossible: MoltenVK/Metal vs ANGLE/WebGL2). Golden PNGs and 8 golden graph
JSONs are checked into `parity/out/` for the Tier-1 set.

## ⚠ Prerequisites

TouchDesigner **2025.32820** is installed (`brew install --cask touchdesigner`; arm64-native).
**Before any render or parity step can run, TouchDesigner must be license-activated once**
through its GUI (free **Non-Commercial** tier: a Derivative account + key; 1280×1280 render
cap, no watermark — parity renders are 256², well under). A fresh install blocks at the
activation modal; `parity/run.sh` detects this and tells you. This is the single manual step;
everything else (transpile, codegen, build) is automated and complete.

Once activated, build the host project: open TouchDesigner and run
`td/make_bootstrap.py` (Textport) to materialize `td/noisemaker.toe`, then `parity/run.sh all`.

## Coverage

| Namespace | Effects | TD programs |
|---|---|---|
| synth | 29 | auto |
| synth3d | 7 | MRT (manual) |
| filter | 90 | auto |
| filter3d | 1 | MRT (manual) |
| mixer | 14 | auto |
| points | 10 | MRT/points (manual) |
| render | 11 | MRT/points (manual) |
| classicNoisedeck | 20 | auto |
| **total** | **182** | **247 programs — 226 auto-transpiled, 21 MRT flagged** |

Status: scaffold + tooling + runtime + generated assets complete; Tier-1 bring-up and parity
gated on the one-time TD activation. See `docs/IMPLEMENTATION-PLAN.md` for the staged plan.

## License

MIT (port scaffolding). The Noisemaker engine and effects are the reference project's;
TouchDesigner is Derivative's. See `../noisemaker` for upstream terms.
