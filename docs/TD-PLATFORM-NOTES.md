# TouchDesigner Platform Notes

A distilled reference for maintainers of this port. Target build: **2025.32820** (Official).
Sources at the bottom; facts cross-checked against the GLSL TOP probe and the Homebrew cask.

## Install (macOS, Apple Silicon)

- **Homebrew cask:** `brew install --cask touchdesigner` → `2025.32820`, `TouchDesigner.app`,
  arm64-native (no Rosetta), requires macOS ≥ 13. Bundled **Python 3.11**
  (`Contents/Frameworks/Python.framework`). CLI tools `toeexpand`/`toecollapse` in `Contents/MacOS`.
- **Manual DMG** (cask preferred): `https://download.derivative.ca/TouchDesigner.2025.32820.arm64.dmg`
  (~695 MiB) → `hdiutil attach` → `cp -R TouchDesigner.app /Applications` → `hdiutil detach`.
- **Licensing:** free **Non-Commercial** runs and renders with **no watermark** but a hard
  **1280×1280** image cap (1–2px-high ramps exempt). Excludes some Pro OPs. **First launch requires a
  one-time Derivative account + license key via the GUI**; runs offline afterward. (Each account gets
  10 free single-use keys.)

## GLSL TOP shader contract

The GLSL TOP runs a **fragment** shader over the output raster (vertex stage supplied). Conventions:
- **No `#version`/precision line** — TD prepends `#version` (target **4.60**, selectable to 1.20) and a
  preamble. Emit bare declarations + `main()`.
- **Inputs:** `uniform sampler2D sTD2DInputs[TD_NUM_2D_INPUTS];` sampled via the auto-declared `vUV`:
  `texture(sTD2DInputs[0], vUV.st)`. Parallel arrays `sTD3DInputs`, `sTD2DArrayInputs`, `sTDCubeInputs`.
  >3 inputs → use the **GLSL Multi TOP** (identical, no 3-input limit).
- **Output:** declare `layout(location = 0) out vec4 fragColor;` and write
  `fragColor = TDOutputSwizzle(color);` (the swizzle abstracts cross-platform channel order).
- **Built-in uniforms:** `uTD2DInfos[i].res = (1/w, 1/h, w, h)`; `uTDOutputInfo` (output raster);
  `uTDPass` (current pass, from 0); `uTDCurrentDepth`. **No built-in time** — feed a custom `uTime`.
- **Custom uniforms:** declare by name; feed via the **Vectors** page (`vec0name`, `vec0valuex/y/z/w`,
  `vec1name`, …) or the **Arrays** page (`array0name/type/chop`) from a CHOP. Set from Python:
  `g.par.vec0name='uTime'; g.par.vec0valuex=0.25`.
- **Mode:** `g.par.mode = 'vertexpixel'` (default) or `'compute'`. Compute uses
  `TDImageStoreOutput(index, ivec3(coord), color)` and does **not** apply `TDOutputSwizzle`.

## Multi-pass, feedback, resolution, time

- **Intra-frame iteration:** the GLSL TOP **`Passes`** param duplicates the op N times, feeding output
  → input 1 each iteration; `uTDPass` increments. Good for iterative effects.
- **Cross-shader:** chain GLSL TOPs (pull dataflow).
- **Cross-frame:** the **Feedback TOP** outputs its **Target TOP**'s previous-frame result (one-frame
  delay) — accumulation, trails, reaction-diffusion. Params: `target`, `reset`/`resetpulse`.
- **Resolution:** per-TOP Common page `outputresolution` (`'custom'` + `resolutionw`/`resolutionh`, or
  `'useinput'`). Format menu: `rgba8fixed`/`rgba16float`/`rgba32float` (linear).
- **Time:** `absTime.seconds` (process-monotonic) or `me.time.seconds` (timeline). For deterministic
  offline render set `project.realTime = False` and drive `uTime`/frame explicitly.

## GPU point scatter (deposit / drawMode:"points"|"billboards")

The deposit pass (agents SCATTER — each writes its own pixel) can't be a fullscreen GLSL TOP; it
needs geometry. Validated recipe (`td/points_probe.py` — a 4-agent known-answer probe lands all 4
on their predicted pixels with exact colors + additive sum):

- **Geometry:** **Grid SOP** (`rows`/`cols` = stateSize, `sizex`/`sizey` 2, `orient` `xy`) → **Convert
  SOP** (`totype` `part`, `prtype` `pointsprites`) makes particle prims → renders as **GL_POINTS**.
  Grid alone can't emit points (`surftype` has no Points option). **A fresh Geometry COMP ships with a
  default `torus1` SOP whose render flag is ON — destroy all `geo.children` before adding yours**, or it
  renders through your MAT (collapses into a filled quad). Set flags: grid `render`/`display` = False,
  convert = True.
- **Material:** type global is **`glslMAT`** (not `glslmaterialMAT`). Params: `vdat` (vertex DAT),
  `pdat` (pixel DAT), `glslversion` `4.60`. Samplers via the Samplers page: `sampler0name`='xyzTex'
  + `sampler0top`=TOP, `sampler1name`/`sampler1top`, … Custom uniforms via `vec0name`+`vec0valuex..w`.
  Additive blend on the Common page: `blending`=True, `srcblend`=`one`, `destblend`=`one`,
  `blendop`=`add`; `depthtest`/`depthwriting`=False.
- **Vertex shader:** a TD MAT VS may **write `gl_Position` DIRECTLY in NDC** (reference-faithful;
  `TDWorldToProj(TDDeform(...))` gives the identical result with an ortho camera, so direct is fine).
  Point-sprite conversion overwrites texcoords, so recover each agent's state texel from the point
  **position**: `ivec2(floor((TDPos().xy*0.5+0.5)*(ss-1)+0.5))` → `texelFetch(xyzTex, texel, 0)`. Write
  `gl_PointSize` (1.0 for points; the billboard size for sprites). Pass `out vec4 vColor` to the frag.
- **Pixel shader:** `layout(location=0) out vec4 fragColor; fragColor = TDOutputSwizzle(c);`. For
  billboard SDFs use `TDPointCoord()` (auto 0..1 across the sprite, (0,0)=bottom-left) as the sprite UV.
- **Render TOP:** `geometry`=Geo COMP, `camera`=a (dummy/ortho) Camera COMP — required even when the VS
  writes gl_Position directly; `outputresolution`/`resolutionw`/`resolutionh`/`format`; transparent bg
  `bgcolora`=0; `antialias`='1' (off). It clears to bg, so to ACCUMULATE onto an existing trail,
  composite `priorTrail + pointsRender` (additive **Composite TOP**) — associativity == the reference's
  "draw additively into the trail FBO without clearing".
- `count:"input"` → stateSize² where stateSize = the xyz state-texture width. **`numpyArray`/`save` row
  0 = BOTTOM** (GL origin; verify-anchored by a GLSL-TOP `gl_FragCoord.y` ramp) — consistent with the
  rest of the port, so the deposit needs no Y-flip vs the WebGL2 reference.
- Point sprites are screen-aligned (no per-vertex rotation), so `rotationVar`>0 billboards would need
  real quads; the flagship uses `rotationVar:0`, so point sprites are exact for it.

## File & component model

- **OP families:** TOP (textures/GPU), CHOP (channels), SOP (geometry), MAT (3D materials, incl. GLSL
  MAT), COMP (containers; the `.tox` unit), DAT (text/tables/scripts — **Text DAT holds GLSL/Python**).
- **`.toe`/`.tox` are proprietary BINARY** — no public format, no save-as-text toggle. Don't author
  offline. `toeexpand`/`toecollapse` convert to/from an undocumented ASCII tree (diffing/recovery only).
  `TDJSON` serializes **custom parameters only** (presets, not topology).
- **Recommended build path (this port):** keep GLSL in on-disk `.frag` files; a Text DAT references one
  via `file` + `syncfile`; the GLSL TOP's `pixeldat` points at the DAT. **Build the network from Python
  at startup** (Execute DAT `onStart`/`onCreate`). Ship a near-empty bootstrap `.toe`.

## Programmatic construction & automation

- **Create/wire (Python):** `parent().create(glslTOP, 'name')`; `op('a').outputConnectors[0].connect(op('b'))`
  or `op('b').inputConnectors[i].connect(op('a'))`; set `op.par.*`; `op.destroy()`. `create()` takes a
  **type object** (`glslTOP`), not a string.
- **Startup build:** Execute DAT `onStart()` (app launch) / `onCreate()` (on component load — both fire
  when TD opens a `.toe` from the CLI; this port uses them). The cook is **pull-based** — terminate the
  chain in a viewer/exporter (or call `op.cook(force=True)`) so it cooks. NOTE: `TOUCH_START_COMMAND` is
  **not present in the 2025.32820 build** (verified — not in any framework binary); there is no headless
  startup-script env var, so an Execute DAT inside a `.toe` is the mechanism. Also: TD operator globals
  (`op`, `glslTOP`, `baseCOMP`, …) are injected into DAT scopes via `from td import *` but **NOT into
  imported `.py` modules** — helper packages must `import td` and use `td.glslTOP` etc.
- **Render to file:** `op('x').save('f.png', createFolders=True)` (PNG/EXR/TIFF/…); or `TOP.numpyArray()`
  for in-process pixel diffing; or a Movie File Out TOP (`record`, `addframe.pulse()`). `project.quit(force=True)`
  to exit (flush `save()` before quitting).
- **Headless reality:** TD needs a **logged-in, GPU-capable desktop session** (Vulkan/MoltenVK→Metal on
  macOS). It is fully scriptable but **not** a true headless daemon/cron without a real-or-dummy display +
  auto-login. This port's `parity/run.sh` launches TD display-bound, scripted, auto-quit.

## 3D volume raymarch (render3d / renderLit3d) — WORKS, with two TD adaptations

The synth3d generators (`shape3d`, `noise3d`, `fractal3d`, `cell3d`) precompute a 3D volume as a **2D
atlas** (`atlasTexel(x,y,z) = (x, y + z·volSize)`, default volumeSize 64 → a **64×4096** rgba16f
texture); `render3d` / `renderLit3d` then raymarch it. Two TD-specific fixes (both in
`runtime/td_backend.py`) make the whole synth3d-volume → render path render at **ssim ~1.0**
(noise3d/fractal3d/cell3d/shape3d → render3d **and** renderLit3d all pass, max-diff 1):

1. **Bare `if (NAME)` bool defines — the real cause of the "magenta" 3D render (a COMPILE ERROR, not
   an MRT/channel/license issue).** `render3d.frag` steers the invert branch with `if (INVERT)`, and
   `INVERT` is injected as the int `0`. WebGL2/ANGLE accepts `if (0)`, but TD's strict `#version 460
   core` **rejects a non-bool `if` condition**, so the GLSL TOP fails to compile and TD shows its
   **magenta error texture** (which earlier looked like an "MRT G-channel drop" — it was the error
   texture all along; the readback-grayscale-vs-sample-magenta split was reading the producer vs the
   failed consumer). Fix: `_bool_define_keys` now treats a define used as a bare `if (NAME)` /
   `if (!NAME)` as a GLSL bool (injects `true`/`false`), not only those with a `#define K true|false`
   fallback. `if (FILTERING == 1)` stays an int. (Only one 2D effect, curl/`RIDGES`, hit the bare-if
   pattern and it already had a fallback — so zero 2D regression.)

2. **Non-Commercial license 1280 cook-resolution cap.** A 64×4096 atlas `glslTOP` (params verified at
   build: custom 64×4096) **cooks at 20×1280** (0.3125× = 1280/4096) under the license cap, breaking
   `atlasTexel`. (Square ≤1280 textures are fine — the agent state cooks at a full 1024×1024.)
   `_cap_volume_size` clamps every `volumeSize*` uniform to **NM_MAX_VOLUME_SIZE (default 32 → a
   32×1024 atlas, under the cap)** so the texture size and the shader `volumeSize` stay consistent. The
   render then differs from the volumeSize-64 reference (lower 3D resolution) but is **correct** —
   validated apples-to-apples against volumeSize-32 reference goldens (max-diff 1, ssim ~1.0). Raise
   `NM_MAX_VOLUME_SIZE` on a Commercial/Educational license (which has no 1280 cap).
   - The **orbit-camera** gens (noise3d/fractal3d/cell3d/shape3d/renderLit3d) view the volume from
     *outside* at a distance, so the raymarch averages the 64→32 cap away and their default-`volumeSize:64`
     parity DSLs still hit max-diff 1 against a 64 golden. **flythrough3d is the one exception**: its
     camera flies *inside* the volume, so the cap is directly visible (a default-64 golden vs the TD-32
     render is only ssim ~0.89, a bimodal 67%-exact / 25%-off split — the reference engine *itself*
     shifts by the identical mean when clamped 64→32, proving it's the cap, not the port). Its parity
     program therefore **pins `flythrough3d(volumeSize:32)`** so golden and candidate render at the
     resolution TD can actually cook → ssim 0.99992, mean-abs-diff 0.017 (one voxel-boundary outlier
     pixel; ssim-gated). This measures port-correctness rather than penalizing the license cap.

Isolation harness: `parity/evolve.sh <prog>` with `NM_DUMP_PROG=<prog>` and `NM_DUMP_TEXID=<texId>`.

**mat3 cubeBasis — RESOLVED (renderCubemap3d + renderCubemapSurface now max-diff 1, ssim ~1.0).**
Both cube-face renderers steer rays with `uniform mat3 cubeBasis`, and the old
`uniform_binder._as_components` truncated any list to 4 floats onto the GLSL TOP **Vectors** page —
a `mat3` (9 floats) can't live there, so `cubeBasis` stayed the TD default (the **zero** matrix →
`normalize(0)` = NaN → degenerate). TD binds a matrix uniform from the **Matrices** page instead:
`g.par.matrix` (count), `matrix{i}name`, and `matrix{i}value` ← **a Table DAT**. The table→GLSL
mapping and the reference's column-major flat-array convention were pinned by a known-answer probe
(`td/matrix_probe.py`): TD reads table row r as GLSL row r, so the binder transposes —
`table[r][c] = flat[order*c + r]` — making GLSL column k == the reference column k. `uniform_binder`
now routes any 9- or 16-element value (mat3/mat4) to `_bind_matrices` (a reused per-slot Table DAT,
so set_time re-binds cleanly); cubeBasis is the only catalog user today, but mat4 is handled too.

**6-FACE CUBEMAP BAKE — DONE (both renderers, all 6 faces + the cross at max-diff ≤ 1).** A full
cubemap is HOST-DRIVEN (reference `Pipeline.renderCubemap`): render the same graph 6×, setting
`cubeBasis` to each GL face basis (+X,-X,+Y,-Y,+Z,-Z) between renders — which is exactly what the
mat3 binding above unlocks (the single-face test used the DSL-default **identity** basis, so the
bake is the first exercise of the 6 NON-identity bases). `td/cubemap_bake.py` finds the cube-camera
TOP (the effect whose declared uniforms include `cubeBasis`), then per face sets
`bound['cubeBasis']` to the column-major `[right|up|forward]` basis (right = cross(up, forward);
mirrors `renderer/cubeCamera.faceBasisMat3`), re-binds (refills the Table DAT), cooks, saves — only
the cube-camera TOP + downstream re-cook (the upstream volume is computed once). The reference
goldens come from `export-and-render.mjs --cubemap` (calls `pipeline.renderCubemap`, whose
`backend.readPixels` is the SAME top-down float→8-bit encoding as the single-frame golden + TD's
`out.save()`, so faces compare directly). `parity/cube_cross.py` assembles the 6 into the canonical
horizontal cross (`cubeExport.crossLayout`). Driver: **`parity/cubemap.sh <prog>`** (default
`synth3d_renderCubemapSurface`; also `synth3d_renderCubemap3d`). Result: 6 distinct faces, every
face max-diff ≤ 1 (one byte-identical), cross ssim 1.0 — both cube renderers.

**std140 UBO binding — DONE (synth/remap byte-identical via a Uniform Array).** `synth/remap` is the
sole `layout(std140) uniform { vec4 data[267]; }` effect — the projection-map config packed into a
vec4 array. A TD GLSL TOP has **no UBO parameter**, but it binds a large `uniform vec4 data[N]` from
the **Arrays** page, which is the std140-UBO equivalent. Recipe (pinned by `td/array_probe.py`):
`array{i}name` + `array{i}arraytype='uniformarray'` + `array{i}type='vec4'` + `array{i}chop`, where
the CHOP is **4 channels (x,y,z,w) × N samples** and `data[i] = (x[i],y[i],z[i],w[i])` — row-major,
so the packed array binds verbatim. Build the CHOP from a Table-DAT (4 rows, col0 = channel names) →
DAT-to-CHOP (`firstrow='values'`, `firstcol='names'`). Pipeline: `convert-definitions.mjs` serializes
the effect's `uniformLayout` into the JSON; `convert-shaders.mjs` rewrites the std140 block to
`uniform vec4 data[N];`; `uniform_binder.pack_uniforms_with_layout` (Python port of the reference
`packUniformsWithLayout`: value → `slot*4 + lane`) packs the flat uniforms, `bind_uniform_array` feeds
the CHOP. The layout is loaded from the effect JSON by `td_backend._effect_uniform_layout` (the
serialized graph DROPS it — both JS and Python graphs carry only the `blit` program; the reference
attaches it at pipeline-build). set_time re-packs (time/resolution live in the array). Validated
`remap(bgColor:[0.8,0.3,0.5])` → max-diff 0, ssim 1.0. (mashup, the other "UBO" effect, actually uses
FLAT GLSL uniforms — its `uniformLayout` is WGSL-only — so it needs no array path, only its layer
construct, which the reference itself gates.)

**filter3d flow3d — DONE (renders end-to-end, chaos-gated ssim ~0.84 at f8).** A stateful 3D
agent-flow filter: MRT agent pass (3 state buffers, 512² agents) → diffuse → copy → **points
deposit** → blend, output a volume atlas raymarched by `render3d`. Every piece already existed
except the deposit: flow3d's agents carry a 3D VOLUME position (`state1.xyz`, voxel units) and
scatter into the trail ATLAS (row = `y + floor(z)·volSize`), and its deposit pass names the
agent-state inputs `stateTex1/stateTex2` — so the 2D `_build_points` (which only looks for
`xyzTex/rgbaTex`) silently skipped it, leaving an empty trail → degenerate flat render. Fix:
`deposit_shaders.points3d` (the 3D-atlas vertex math, mirroring `filter3d/flow3d/glsl/deposit.vert`;
index-threshold cull, not the golden-ratio one) + `_build_points` resolves `stateTex1/2` and selects
it. Chaos-gated like every agent flow (Metal vs ANGLE point-raster ULP diverges over frames).

All synth3d/filter3d/render 3D effects now render; no remaining 3D TODOs.

The classicNoisedeck `noise3d`/`shapes3d` are 2D effects (`search classicNoisedeck`), not the synth3d
volume path.

## Sources

- Cask: `brew info --cask touchdesigner`; https://github.com/Homebrew/homebrew-cask/blob/HEAD/Casks/t/touchdesigner.rb
- Non-Commercial license: https://derivative.ca/UserGuide/TouchDesigner_Non-Commercial
- GLSL TOP: https://docs.derivative.ca/Write_a_GLSL_TOP · https://derivative.ca/UserGuide/GLSL_TOP · https://docs.derivative.ca/GLSL_Multi_TOP
- Uniforms: https://interactiveimmersive.io/blog/glsl/how-to-use-uniforms-in-the-glsl-top-in-touchdesigner/
- Feedback TOP: https://derivative.ca/UserGuide/Feedback_TOP · AbsTime: https://docs.derivative.ca/AbsTime_Class
- Files/tooling: https://docs.derivative.ca/.toe · /Toeexpand · /Toecollapse · /TDJSON · /Text_DAT
- Python build/automation: https://docs.derivative.ca/Working_with_OPs_in_Python · /OP_Class · /COMP_Class · /Connector_Class · /Execute_DAT · /TOP_Class · /Movie_File_Out_TOP · /Project_Class
