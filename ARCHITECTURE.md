# Noisemaker → TouchDesigner Port — Architecture

A structural port of the Noisemaker shader engine (its `shaders/`, reached via
`NM_REFERENCE_ROOT`) to **Derivative TouchDesigner** (2025.32820+), mirroring the existing
Unity/HLSL port (`noisemaker-hlsl`) and the Godot port (`noisemaker-godot`): live procedural
texture from the Polymorphic DSL, rendered through a network of **GLSL TOP**
operators built programmatically in TouchDesigner Python, **tolerance-parity** to the
JS/WebGL2 reference.

## The seam: Render Graph JSON

Identical to the other ports. The contract between "what to render" and "how this
engine renders it" is the normalized **Render Graph JSON**
(`compileGraph(dsl) → {passes, programs, textures, renderSurface}`, see
`docs/GRAPH-JSON-SCHEMA.md`, reference specs `03`/`04`). Two producers emit it:

- **Golden / offline** — the *unchanged* reference JS `compileGraph`, via the reused
  Node tool `tools/export-graph.mjs`. Zero parity risk: it is literally the reference.
- **Live / in-engine** — a staged TouchDesigner-Python DSL frontend (`td/noisemaker/compiler/`,
  Phase 6) that emits byte-identical normalized JSON.

Both feed **one consumer**: the TouchDesigner network builder.

## The consumer: a network builder, not an imperative executor

This is the one structural way the TD port differs from HLSL/Godot, and it is a
*simplification*. Unity (C# `CommandBuffer`) and Godot (GDScript `RenderingDevice`)
issue GPU passes **imperatively, every frame**. TouchDesigner instead has a
**pull-based cook graph**: you build a network of operators **once**, and the engine
re-cooks it each frame automatically. So the Noisemaker "render graph" maps almost
1:1 onto a TD operator network, and our runtime is a **builder** that translates
graph JSON → TOPs, plus a thin per-frame uniform/time feed.

| Render Graph concept            | TouchDesigner realization                                            |
|---------------------------------|----------------------------------------------------------------------|
| effect pass (`passType:effect`) | a **GLSL TOP** (`pixeldat` → the effect `.frag`; inputs wired; uniforms set) |
| blit pass (`passType:blit`)     | a **Null TOP** (or pass-through GLSL TOP)                             |
| pooled texture (`phys_N`)       | the upstream TOP's output (TD manages texture memory; pool is advisory) |
| global surface `o0..o7`, state  | a **Feedback TOP** pair (double-buffered, cross-frame persistence)   |
| `inputs{name:texId}`            | TOP input connections, in stable order → `sTD2DInputs[i]`            |
| `outputs{color,color1,...}` MRT | GLSL TOP **# of Color Buffers** > 1                                  |
| `uniforms{name:value}`          | GLSL TOP **Vectors** page (`vecNname`/`vecNvalue*`) or an **Arrays** CHOP |
| `defines{KEY:val}`              | `#define KEY val` baked into the `.frag` at transpile time           |
| `drawMode:"points"` (scatter)   | Geometry COMP + GLSL MAT + Render TOP (Phase 5, hardest)             |
| `repeat:"iterations"`           | GLSL TOP **Passes** param, or a chain / Feedback loop                |
| `renderSurface`                 | the presented **Out/Null TOP**                                       |

## Shader strategy: translate from the reference **GLSL**, not WGSL

The HLSL and Godot ports translate from the canonical **WGSL** because their targets
(D3D HLSL; Vulkan GLSL) are not GLSL-ES-compatible and have top-left/Y-down raster
origins. **TouchDesigner's GLSL TOP is OpenGL GLSL** — the *same family and the same
raster convention as the reference's WebGL2 backend.** Consequences:

1. **Source of truth for the TD port is the upstream engine's `shaders/effects/<ns>/<name>/glsl/*.glsl`**
   (the reference's shipping WebGL2 shaders, under `NM_REFERENCE_ROOT`), cross-checked against WGSL only when a
   GLSL file is absent. These are already parity-tested against WGSL by the reference.
2. The per-effect transform is **mechanical**, so most of the 182 effects are
   **auto-transpiled** by `tools/convert-shaders.mjs` rather than hand-ported. The
   transform (see `PORTING-GUIDE.md`):
   - strip the `#version 300 es` / `precision` header (TD prepends its own `#version`);
   - drop named input-sampler declarations (`uniform sampler2D inputTex;`) and emit
     `#define inputTex sTD2DInputs[i]` in stable input order;
   - normalize coordinate access (`gl_FragCoord`→`nm_FragCoord`, `v_texCoord`→`nm_uv`)
     through a single helper carrying the **Y-flip switch** (see below);
   - rename the effect `main()`→`nm_main()`, own the `out vec4 fragColor;` declaration,
     and wrap the result once: `void main(){ nm_main(); fragColor = TDOutputSwizzle(fragColor); }`;
   - bake compile-time `#define`s from the effect definition.
3. Emitted `.frag` files are **self-contained** (the reference inlines its own
   PCG/prng/helpers per effect), so there is no shared `NMCore` to keep in sync — the
   biggest source of parity drift in the other ports is absent here.

### Y-origin — the one hazard, reduced to a single switch

The reference computes `globalCoord = gl_FragCoord.xy + tileOffset` and
`st = globalCoord / fullResolution.y` in WebGL2's **bottom-left** raster space. TD's
GLSL TOP is OpenGL and is expected to share that convention — meaning **no per-effect
Y-flip** (unlike the HLSL port, which needed a flip at `NMBlit`). Because TD 2025 runs
on a Vulkan/MoltenVK backend, this is **verified empirically at bring-up** (Task 2.3:
render a `vUV.t` gradient and a real `gradient` effect, compare to the golden). The
transpiler routes all coordinate reads through `nm_FragCoord`/`nm_uv`, so if a flip is
needed it is a **one-line change in one helper** (`NM_FLIP_Y`), not a 182-shader edit.

## Parity strategy

Golden truth = the reference's own output. For each Tier-1 program:
`export-graph.mjs` (graph JSON) + `export-and-render.mjs` (golden PNG via the reference
WebGL2 engine) → then the TD candidate render → `compare.py` (max-abs-diff + SSIM).
Targets, as on the other ports: SSIM ≥ 0.98, max-abs-diff ≤ 1–2/255 (cross-device
bit-exactness is impossible: MoltenVK/Metal vs ANGLE/WebGL2). **Achieved: 8/8 Tier-1 at
SSIM ≥ 0.99998, max-diff ≤ 1.**

The TD candidate is produced **fully scripted, no GUI clicking** (`parity/run.sh`): build a
bootstrap `.toe` (`td/build_parity_toe.py` — an Execute DAT authored offline via
`toeexpand`/`toecollapse`, since TD has no headless startup hook), launch TD on it → the
Execute DAT (`onStart`/`onCreate`) execs `td/parity_render_all.py` → `project.realTime = False`
→ build each graph via the runtime → `op.save('candidate.png')` → `project.quit(force=True)`.

## Platform constraints (from research; see `docs/TD-PLATFORM-NOTES.md`)

- **GLSL TOP contract:** no `#version`/precision line; inputs `texture(sTD2DInputs[i], vUV.st)`;
  output `fragColor = TDOutputSwizzle(...)`; built-ins `uTD2DInfos[i].res = (1/w,1/h,w,h)`,
  `uTDOutputInfo`, `uTDPass`; **no built-in time** → custom `uTime` uniform.
- **Custom uniforms:** declared by name in the shader; fed from Python via the Vectors
  page (`g.par.vec0name='uTime'; g.par.vec0valuex=…`) or an Arrays-page CHOP.
- **No offline `.toe`/`.tox` authoring:** the binary format is undocumented; we ship a
  near-empty bootstrap `.toe`, keep GLSL in on-disk `.frag` files (Text DAT `file`+`syncfile`),
  and **build the network from Python at startup** (Execute DAT `onCreate`/`onStart`).
- **Not truly headless:** TD needs a logged-in GPU desktop session (it has one on this
  machine). It is fully scriptable but not a daemon/CI process without a display.
- **Licensing:** free Non-Commercial tier runs and renders with **no watermark** but a
  **1280×1280 cap** (parity renders are 256², well under). **First launch requires a
  one-time Derivative account+key activation via the GUI** — the single human-in-the-loop
  step before parity gates can execute. All build/transpile/codegen work is autonomous.

## Tech stack

TouchDesigner 2025.32820 (arm64-native, `/Applications/TouchDesigner.app`), its bundled
**Python 3.11**, GLSL **4.60** (GLSL TOP), Node 26 (reused reference tooling, no new
deps), Python 3 + numpy/pillow (reused `compare.py`). `toeexpand`/`toecollapse` available
as an escape hatch but not on the build path.

## Reused engine-agnostic assets (copied, NOT re-authored)

`reference/01–10` specs, `tools/export-graph.mjs`, `tools/convert-definitions.mjs`
(OUT_DIR retargeted), `parity/compare.py`, `parity/programs/*.dsl`,
`parity/export-and-render.mjs`, `docs/GRAPH-JSON-SCHEMA.md`.

See `docs/IMPLEMENTATION-PLAN.md` for the staged, parity-gated build and
`PORTING-GUIDE.md` for the reference-GLSL → TD-GLSL rulebook.
