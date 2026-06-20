# Noisemaker → TouchDesigner Port — Implementation Plan

**Goal:** A structural port of the Noisemaker shader engine (`../noisemaker/shaders`) to
**TouchDesigner 2025.32820+**, mirroring the Unity/HLSL (`../noisemaker-hlsl`) and Godot
(`../noisemaker-godot`) ports: live procedural texture from the Polymorphic DSL, rendered
through a Python-built **GLSL TOP** network, tolerance-parity to the JS/WebGL2 reference.

**Architecture (see `../ARCHITECTURE.md`):** the seam is the **Render Graph JSON**
(`compileGraph(dsl) → {passes, programs, textures, renderSurface}`). Two producers: (a)
golden/offline — the *unchanged* reference JS via reused `tools/export-graph.mjs`; (b)
live/in-engine — a staged TD-Python DSL frontend (Phase 6). Both feed one consumer: the TD
**network builder** (`td/noisemaker/runtime/td_backend.py`), which translates the graph into a
network of GLSL TOPs that TouchDesigner cooks each frame.

**Key facts that shape the port:**
- TD's GLSL TOP is **OpenGL GLSL, bottom-left raster** = same as the reference WebGL2 backend →
  **port shaders directly from reference GLSL, no Y-flip** (verified at Task 2.3). This is why
  most shaders **auto-transpile** (`tools/convert-shaders.mjs`).
- TD `.toe`/`.tox` are binary → **don't author offline**; ship a bootstrap `.toe`, keep GLSL in
  on-disk `.frag`, build the network from Python at startup.
- TD is **not truly headless** but is fully scriptable on a logged-in GPU session; parity renders
  via `project.realTime=False` → `op.save(png)` → `project.quit`.
- Free **Non-Commercial** tier: 1280×1280 cap (parity is 256²), no watermark, but **first launch
  needs a one-time GUI license activation** — the only manual prerequisite.

**Reused engine-agnostic assets (copied, NOT re-authored):** `reference/01–10`,
`tools/export-graph.mjs`, `tools/convert-definitions.mjs` (OUT_DIR retargeted), `parity/compare.py`,
`parity/programs/*.dsl`, `parity/export-and-render.mjs`, `docs/GRAPH-JSON-SCHEMA.md`.

**Parity targets:** SSIM ≥ 0.98, max-abs-diff ≤ 1–2/255 (MoltenVK/Metal vs ANGLE/WebGL2).

---

## Phase 0 — Scaffold & reuse wiring  ✅ DONE

- [x] Project tree + `.gitignore`; `git init`.
- [x] Copy `reference/` (01–10, byte-identical to the sibling ports), `docs/GRAPH-JSON-SCHEMA.md`.
- [x] Copy reused tools (`export-graph.mjs`, `convert-definitions.mjs`, `package.json`) and parity
      assets (`compare.py`, `programs/*.dsl`, `export-and-render.mjs`).
- [x] Retarget `convert-definitions.mjs` OUT_DIR → `td/noisemaker/effects`.
- [x] **Verify** the golden producer runs in-repo: `export-graph.mjs --file parity/programs/solid.dsl`
      emits a schema-correct graph (effect pass + blit, `renderSurface:o0`, `phys_0`).

## Phase 1 — Generated assets  ✅ DONE

- [x] `convert-definitions.mjs` → **182** effect-definition JSONs (`td/noisemaker/effects/<ns>/*.json`),
      0 failures.
- [x] `convert-shaders.mjs` (NEW) → **247** TD `.frag` programs; **226 auto-transpiled**, **21 MRT
      flagged** (points/agents, 3D renderers, synth3d precompute — Phase 5.5).
- [x] 8 Tier-1 golden graph JSONs (`parity/out/*.graph.json`) + 8 golden PNGs (reference render,
      reused from the identical-DSL Godot port).

## Phase 2 — Bring-up: minimal builder, end-to-end on `solid`  ⛔ gated on TD activation

The runtime and `solid.frag` exist; this phase RUNS them for the first time. Riskiest-first.

- [x] Runtime core authored: `render_graph`, `graph_loader`, `dim` (reference/04 §9 exact),
      `engine_uniforms` (§10.1), `uniform_binder`, `td_backend`, `surface_manager`, `pipeline`,
      `nm_renderer`. Pure-Python core unit-smoke-tested under stock python3.
- [x] `parity/render-candidate.py` + `parity/run.sh` authored (scripted, auto-quit, activation-aware).
- [ ] **Activate TouchDesigner once** (Derivative account + key, GUI) — unblocks everything below.
- [ ] **Task 2.1 — verify the GLSL TOP contract:** render `solid.frag` in a GLSL TOP; confirm
      `TDOutputSwizzle`/`sTD2DInputs`/Vectors-page uniforms behave as documented. Confirm the
      `TOUCH_START_COMMAND="exec(open(render-candidate.py))"` launch path works (fallback:
      `td/make_bootstrap.py` Execute DAT).
- [ ] **Task 2.2 — Y-origin determination:** render a `vUV.t` gradient and the real `gradient`
      effect; compare to golden. Expect **no flip**; if mismatched, re-run `convert-shaders.mjs --flip-y`
      (single control point) and document.
- [ ] **Task 2.3 — `solid` parity gate:** `parity/run.sh solid` → SSIM≥0.98, max-diff≤2. `solid` is
      a flat fill → near-exact. **First green gate.**
- [ ] **Task 2.4 — uniform-feed confirmation:** verify the Vectors-page feed scales to ~13 uniforms
      (`noise`) and int/bool uniforms bind; if not, switch `uniform_binder` to a CHOP/Arrays feed or
      add a transpiler int→float refinement (PORTING-GUIDE "uniform typing").

## Phase 3 — Full builder coverage  ⛔ gated on Phase 2

- [ ] 3.1 `dim`-driven per-TOP resolution + format across all texture specs (already wired; verify
      against `blur` pooled intermediates).
- [ ] 3.2 `surface_manager`: complete Feedback TOP wiring for o0..o7 / state surfaces — within-frame
      ping-pong + cross-frame persist (reference/04 §10.2/§10.6/§10.7). Tier-1 (display surfaces)
      already works; this is for sims.
- [ ] 3.3 `td_backend` MRT: Render Select TOP per extra color buffer (draw_buffers>1).
- [ ] 3.4 `td_backend` points scatter (`drawMode:"points"`): Geometry COMP + GLSL MAT + Render TOP.
- [ ] 3.5 `pipeline` live time driving for animated effects (osc2d) + host `resize`.

## Phase 4 — Tier-1 effect parity  ⛔ gated on Phase 2

Per effect: the `.frag` already exists (Phase 1); gate `parity/run.sh <prog>` vs golden.
- [ ] 4.1 `synth/noise` (PCG value/simplex; `NOISE_TYPE`/`LOOP_OFFSET` define overrides).
- [ ] 4.2 `synth/cell`  · [ ] 4.3 `synth/gradient` (Y sanity) · [ ] 4.4 `synth/shape` (SDF)
- [ ] 4.5 `synth/osc2d` (time) · [ ] 4.6 `filter/blur` (2-pass, pooled intermediate)
- [ ] 4.7 `mixer/blendMode` (two-input)
- [ ] **Milestone:** 8/8 Tier-1 parity-pass (`parity/run.sh all` green).

## Phase 5 — Expand coverage (templated)  ⛔ gated on Phase 4

Per-effect: `.frag` exists; parity-gate, fix any auto-transpile miss. Order by leverage/risk:
- [ ] 5.1 remaining `synth` (single-pass generators — highest yield).
- [ ] 5.2 `filter` (90; many single-pass).
- [ ] 5.3 `mixer` (14; two-input) · [ ] 5.4 `classicNoisedeck` (20).
- [ ] 5.5 the **21 MRT/points/3D** programs — finish by hand (agents: MRT state + points deposit +
      diffuse; 3D: volume atlas + raymarch + geoOut). Hardest; loosen tolerance for chaotic sims.
- [ ] Track coverage in README; **log** any effect skipped/over-tolerance — never silently.

## Phase 6 — Live TD-Python DSL compiler (staged)  ⛔ gated on Phase 4

Only after the golden-JSON path is proven. Port `reference/01–03` to Python under
`td/noisemaker/compiler/` (lexer→parser→validator→expander→resources), emitting the same
normalized graph JSON. Validate by diffing against `export-graph.mjs` output for
`parity/programs/*` (byte-identical modulo `id`). Constant-fold in Python `float` (IEEE double =
JS Number). Wire into `nm_renderer.set_dsl(src)`.

---

## Self-review
- **Spec coverage:** runtime (ref 04)→Phase 2/3; shader translation (ref 07/08)→Phase 1/4/5; golden
  seam (ref 03/04 + tools)→Phase 0/1; live compiler (ref 01–03)→Phase 6; parity harness→Phase 2/4.
- **Test-first:** goldens (Phase 1) precede any candidate; every effect has a parity gate.
- **Riskiest-first:** TD integration brought up on `solid` (Phase 2) before any complex effect;
  MRT/points/feedback each get a dedicated task.
- **The one external blocker:** TD license activation — isolated to the Phase 2 gate; all authoring
  is complete and independent of it.
