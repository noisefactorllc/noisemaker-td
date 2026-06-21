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
  run.sh                  build .toe → render in TD → compare          (NEW)
  .venv/                  numpy+pillow for compare.py (gitignored)
  out/                    generated artifacts (gitignored)
td/
  noisemaker/
    runtime/              the network builder (Python): render_graph, graph_loader, dim,
                          engine_uniforms, uniform_binder, td_backend, surface_manager,
                          pipeline, nm_renderer
    compiler/             live DSL frontend (Phase 6 — staged)
    shaders/effects/<ns>/<effect>/<prog>.frag   per-program TD GLSL (generated)
    effects/<ns>/<func>.json                    effect definitions (generated)
  parity_render_all.py    batch renderer: build each graph → render → save candidate (NEW)
  build_parity_toe.py     authors the bootstrap nm_parity.toe via toeexpand/toecollapse (NEW)
  make_bootstrap.py       builds an interactive host noisemaker.toe (live use)
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

## Parity — ✅ 72/72 gateable PASS (+3 deferred)

```bash
parity/sweep.sh            # stage DSL+golden pairs, render all in TD, grade → 72/72 PASS, 3 deferred
parity/sweep.sh --compare-only   # re-grade existing candidates (no TD render)
parity/run.sh solid        # render+diff one program (or a "a b c" batch) at a uniform tolerance
```
`run.sh` builds a bootstrap `.toe` (`td/build_parity_toe.py`), launches TouchDesigner to render
the candidates (`td/parity_render_all.py`), and diffs each against the reference golden
(`parity/compare.py`, via `parity/.venv`). `sweep.sh` stages every reusable DSL+golden pair from
the sibling `noisemaker-godot` port (`stage_coverage.py` — identical DSL + same reference WebGL2
renderer ⇒ byte-identical goldens), renders them all, and grades with a **per-effect tolerance**.

**All 72 gateable effects pass** — every single-pass `synth`/`filter`/`mixer`/`classicNoisedeck`
effect the sibling ports cover, plus the multi-input `channelCombine` and single-step `feedback`:
66 at the strict gate (SSIM ≥ 0.99998, max-diff ≤ 1) and 6 discontinuity-heavy effects (`newton`,
`shadow`, `edge`, `crt`, `uvRemap`, `distortion`) gated on structural **SSIM ≥ 0.98** (a few pixels
at fractal basins / `step()` thresholds / NEAREST coord tie-breaks can't be bit-exact cross-device
— MoltenVK/Metal vs ANGLE/WebGL2; same set the godot port relaxes, with the same physics). This
validates the transpiler, runtime, uniform feed, the no-Y-flip thesis, **NEAREST input sampling**,
time, multi-pass (`blur`), two-input (`blendMode`), and the **feedback back-edge → Feedback TOP**.

**Deferred (`[DEFER]`):** `cellularAutomata`, `reactionDiffusion`, `motionBlur` need multi-frame
**accumulation** — TD's Feedback TOP latches only on a real engine frame tick, which the synchronous
render loop can't drive (needs an async realTime / Movie-File-Out frame loop; Phase 5.5). The 21
**MRT/points/3D** programs are transpiled but unvalidatable locally (no sibling goldens and the
reference's headless WebGL2 readback fails here). See `docs/IMPLEMENTATION-PLAN.md` §5.5.

TouchDesigner has no headless startup hook, so rendering runs via an Execute DAT inside a `.toe`
(authored offline with `toeexpand`/`toecollapse`); see `docs/TD-PLATFORM-NOTES.md`.

## ⚠ Prerequisites

TouchDesigner **2025.32820** (`brew install --cask touchdesigner`; arm64-native). The free
**Non-Commercial** tier needs a **one-time Derivative account + license activation** through the
GUI (1280×1280 render cap, no watermark — parity renders are 256²). A fresh install blocks at the
activation modal; once activated, everything is automated — `parity/run.sh all` runs to green with
no further manual steps.

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

Status: scaffold + tooling + runtime + generated assets complete; **72/72 gateable effects
pixel-parity-validated** in TouchDesigner (every single-pass `synth`/`filter`/`mixer`/
`classicNoisedeck` effect the sibling ports cover + `channelCombine` + single-step `feedback`).
Deferred: 3 multi-frame-accumulation effects (Feedback-TOP wiring done; needs an async engine
frame loop) and the 21 MRT/points/3D programs (transpiled; need a working golden source). Then the
**live Python DSL compiler** (Phase 6). See `docs/IMPLEMENTATION-PLAN.md` §5.5.

## License

MIT (port scaffolding). The Noisemaker engine and effects are the reference project's;
TouchDesigner is Derivative's. See `../noisemaker` for upstream terms.
