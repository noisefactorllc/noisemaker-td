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

## Sources

- Cask: `brew info --cask touchdesigner`; https://github.com/Homebrew/homebrew-cask/blob/HEAD/Casks/t/touchdesigner.rb
- Non-Commercial license: https://derivative.ca/UserGuide/TouchDesigner_Non-Commercial
- GLSL TOP: https://docs.derivative.ca/Write_a_GLSL_TOP · https://derivative.ca/UserGuide/GLSL_TOP · https://docs.derivative.ca/GLSL_Multi_TOP
- Uniforms: https://interactiveimmersive.io/blog/glsl/how-to-use-uniforms-in-the-glsl-top-in-touchdesigner/
- Feedback TOP: https://derivative.ca/UserGuide/Feedback_TOP · AbsTime: https://docs.derivative.ca/AbsTime_Class
- Files/tooling: https://docs.derivative.ca/.toe · /Toeexpand · /Toecollapse · /TDJSON · /Text_DAT
- Python build/automation: https://docs.derivative.ca/Working_with_OPs_in_Python · /OP_Class · /COMP_Class · /Connector_Class · /Execute_DAT · /TOP_Class · /Movie_File_Out_TOP · /Project_Class
