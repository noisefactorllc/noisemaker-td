# Porting Guide — reference GLSL → TouchDesigner GLSL TOP

How an effect's reference shader becomes a TouchDesigner GLSL TOP program. Most of this is
automated by `tools/convert-shaders.mjs`; this document is the rulebook it implements and the
manual procedure for the cases it flags.

## Why GLSL → GLSL (not WGSL)

The reference ships both GLSL-ES-300 (`effects/<ns>/<name>/glsl/*.glsl`, the WebGL2 backend) and
WGSL. The HLSL and Godot ports translate from **WGSL** because their targets (D3D / Vulkan) have
top-left raster origins and non-GLSL syntax. **TouchDesigner's GLSL TOP is OpenGL GLSL with the
same bottom-left origin as WebGL2**, so the reference GLSL is the closest possible source and the
transform is purely structural — no math edits, no Y-flip. Source of truth for the TD port is the
reference **GLSL**; consult the WGSL only when a `.glsl` is missing.

## The automated transform (`convert-shaders.mjs`)

For each `effects/<ns>/<name>/glsl/<prog>.glsl` → `td/noisemaker/shaders/effects/<ns>/<name>/<prog>.frag`:

1. **Strip the header.** Remove `#version …`, standalone `precision …`, and `#ifdef GL_ES … #endif`
   guards that only wrap precision. TouchDesigner auto-prepends its own `#version` (4.60) and a
   preamble that declares `sTD2DInputs[]`, `vUV`, `uTD2DInfos[]`, `uTDOutputInfo`, `TDOutputSwizzle`,
   etc. — emitting your own `#version` is an error.

2. **Inputs → `sTD2DInputs`.** Drop every `uniform sampler2D <name>;` and emit, in declaration order,
   `#define <name> sTD2DInputs[i]`. A machine-readable header `// NM_INPUTS: <name>=i …` records the
   order; the network builder (`td_backend.py`) wires TOP inputs to match it. So `texture(inputTex, uv)`
   and `textureSize(inputTex,0)` keep working verbatim. (`blendMode`: `inputTex=0 tex=1`.) Effects with
   >3 inputs build on the **GLSL Multi TOP**, which lifts the 3-input cap. The declaration regex
   tolerates a **trailing `// comment`** after the `;` — without it, `feedback`'s commented
   `uniform sampler2D selfTex;   // …` were missed and left as unbound samplers reading black.

3. **Output → `TDOutputSwizzle`.** A single `out vec4 <name>;` (almost always `fragColor`) is kept;
   the effect's `main` is renamed `nm_main`, and a wrapper is appended:
   ```glsl
   void main() { nm_main(); fragColor = TDOutputSwizzle(fragColor); }
   ```
   so the swizzle is applied exactly once, regardless of how the body writes the output (early
   `return`s included). The `// NM_OUTPUT:` header records the output name.

4. **Everything else verbatim.** Uniforms, helpers (PCG/prng/map/…), `gl_FragCoord`, all math, and the
   `#ifndef X #define X default #endif` compile-time fallbacks are preserved unchanged.

5. **Define overrides are NOT baked.** Per-pass compile-time defines (`NOISE_TYPE`, `LOOP_OFFSET`, …)
   are injected by the builder at network-build time as `#define` lines **above** the source; the
   `#ifndef` fallbacks defer to them. This matches the reference `injectDefines` exactly.

6. **`v_texCoord` varying → `vUV.st`.** The reference's vertex-stage varying `v_texCoord` is not
   declared by TD's GLSL TOP preamble; emit `#define v_texCoord vUV.st` (TD's built-in texture-coord
   varying). Without it the handful of effects that read `v_texCoord` (`grime`, `spookyTicker`,
   `texture`, `wobble`) fail to LINK on the missing varying.

7. **std140 uniform block → `uniform vec4 data[N]`.** An effect that declares a
   `layout(std140) uniform <Block> { … }` (only `remap`) is rewritten to a flat `uniform vec4 data[N]`
   array (`RemapUniforms` → `vec4 data[267]`), fed at build time from the GLSL TOP **Arrays page** (a
   "Uniform Array" CHOP) and packed by `uniform_binder.pack_uniforms_with_layout` per the effect's
   `uniformLayout`. convert-shaders flags this as `UNIFORM_ARRAY` — the one **non-MRT** flagged
   program — because the Arrays-page wiring lives in `td_backend`, not in the `.frag`.

Result: **227 of 249 programs** convert cleanly. The **22 flagged = 21 MRT** (below) **+ 1 std140-UBO**
(`remap`, step 7).

## Y-origin

**No flip — CONFIRMED at bring-up.** The `gradient` effect (Y-sensitive) matches the golden at
SSIM 0.99999 with `gl_FragCoord` emitted verbatim: TD's GLSL TOP and the reference WebGL2 backend
are both OpenGL bottom-left (reference/04 §3: WebGL2 textures are bottom-left; only WGSL/D3D ports
flip). The `--flip-y` contingency (route `gl_FragCoord` through an `nm_FragCoord` that flips about
`uTDOutputInfo.res.w`) exists but is unused.

## Manual procedure — MRT programs (21 flagged)

Multi-output shaders are emitted verbatim with a `// NM_OUTPUT: MRT …` header and need hand-finishing.
They span the agent/particle **state** passes (`points/*/agent`, `pointsEmit`/`pointsInit`, `lenia`,
`agentField` — 3-buffer state), the 3D-volume **render** passes (`render/*/render3d`, `renderLit3d`,
`renderCubemap3D`, `renderCubemapSurface` — e.g. `fragColor + geoOut`), the 3D **precompute** passes
(`synth3d/*/precompute`), and the 3D-agent flow (`filter3d/flow3d/agent`). All are implemented and
gated. The procedure:

1. Declare outputs with explicit locations: `layout(location = N) out vec4 <name>;`.
2. Apply `TDOutputSwizzle` per output (or none, for non-color state buffers — raw float state must
   NOT be swizzled).
3. In `td_backend`, set the GLSL TOP's color-buffer count and add a **Render Select TOP** per extra
   buffer so downstream passes can read buffers 1..N.
4. `points` scatter passes additionally need a Geometry COMP + GLSL MAT + Render TOP (the GLSL TOP is
   fragment-only); implemented in `deposit_shaders.py` + `td_backend._build_scatter`.

## Uniform contract

Effects declare uniforms by name (`uniform float scaleX; uniform int seed; uniform vec2 resolution;`).
The builder feeds them via the GLSL TOP **Vectors** page from Python (`uniform_binder.py`):
- The GLSL TOP's **`vec` parameter is the SLOT COUNT** — only `vec0` exists until you set
  `g.par.vec = N`; THEN `vec0name`/`vec0valuex/y/z/w` … `vec(N-1)*` exist. (Missing this was the
  Tier-1 bring-up bug: every effect got only its first uniform.)
- We bind **only the uniforms the shader declares** (parsed from the `.frag`) — engine globals
  (reference/04 §10.1: `time`, `resolution`, `tileOffset`, `fullResolution`, `aspectRatio`,
  `renderScale`; TD has no built-in time so we supply `time`, normalized 0..1) ∪ `pass.uniforms`,
  filtered. Binding undeclared names wastes slots.
- Packing: `float`→1 component, `vec2/3/4`/color→N, `bool`→`1.0/0.0`, `int`→`float`. **int/bool
  bind fine as floats — confirmed; no Arrays/CHOP feed or transpiler int→float change needed.**

## Parity hazards (adapted from reference/07, /08)

| Hazard | Status on the TD port |
|---|---|
| Y-origin / raster flip (reference/04 §3) | **MOOT — confirmed** (`gradient` ssim 0.99999, no flip). TD == WebGL2 (OpenGL bottom-left). |
| PCG bit-exactness (`pcg`, divisor `4294967295.0`) | **Preserved verbatim** — same GLSL `uvec3`/bit-ops; no `asuint`↔`floatBitsToUint` translation needed (unlike HLSL). |
| `floatBitsToUint` / bitcasts | **N/A** — reference GLSL already uses GLSL bitcasts; copied as-is. |
| Modulo sign (`mod`, `nm_positiveModulo`) | **Preserved verbatim** — GLSL `mod` semantics identical to the reference. |
| int/bool uniform packing | **RESOLVED** — bind as floats on the Vectors page; the `vec` count must be set first (see Uniform contract). |
| Coordinate convention (`globalCoord = gl_FragCoord.xy + tileOffset`, `st = …/fullResolution.y`) | Preserved verbatim; correct (Y-origin confirmed). |
| Texture edge sampling | **RESOLVED** — set GLSL TOP `inputextenduv = 'hold'` (clamp) to match the reference's `CLAMP_TO_EDGE`; TD defaults to `'zero'` (the `blur` border-ring bug). |
| Texture **filtering** (NEAREST vs linear) | **RESOLVED** — set GLSL TOP `inputfiltertype = 'nearest'`. The reference creates every intermediate *surface* with `NEAREST` min/mag (`webgl2.js` texParameteri; WebGPU mirrors it). TD defaults to linear ("Interpolate Pixels"). Identical for 1:1 effects (sample lands on a texel centre) but **every warp/resample** (`polar`, `pinch`, `distortion`, `uvRemap`, `chromaticAberration`, bloom upsample, …) diverges under linear — was a 10-effect cluster of small broad diffs. |
| Boolean `#define` injection | **RESOLVED** — a define whose in-shader `#ifndef` fallback is `true`/`false` (e.g. `RIDGES`) is injected as `true`/`false`, not `1`/`0`. The reference emits `1` and relies on WebGL2/ANGLE accepting `if (1)`; TD's strict `#version 460` core rejects a non-bool `if` condition (the `curl` compile error → red/blue placeholder). |
| `'none'` / unbound input sampler | **RESOLVED** — wired to a 1×1 transparent-black Constant TOP (reference binds a 1×1 `[0,0,0,0]`). Also makes TD declare `sTD2DInputs` for a filter-as-generator used with no input (`subdivide`: `'sTD2DInputs' : undeclared identifier`). |
| Feedback / cross-frame (`feedback`'s `selfTex`) | **WIRED** — a texId read by an earlier pass than the one that writes it is a back-edge; the read routes through a **Feedback TOP** (Target = the producer) to break the cook cycle, and the renderer drives the golden's frame count (8). Single-step `feedback` matches; multi-frame *accumulation* (trails/sims) is **RESOLVED** via the evolve harness (`parity/accumulate.sh`, 8-frames-from-zero — see `docs/CHAOS-GATE.md`). |
| Texture format / sRGB | Linear only (`rgba16f`→16-bit float RGBA, never sRGB), set per-TOP by the builder. |
| Cross-device float (MoltenVK/Metal vs ANGLE) | Inherent; absorbed by the SSIM≥0.98 / max-diff≤2 tolerance, same as the sibling ports. |

The GLSL→GLSL same-origin path makes the two biggest cross-backend hazards (Y-flip, bitcast
translation) **moot** — the main reason most of this port is mechanical.
