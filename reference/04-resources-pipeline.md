# Reference Spec 04 — Resources & Pipeline (Runtime Executor)

Source of truth: `shaders/src/runtime/{resources,pipeline,registry,default-shaders,effect-validator,backend}.js`.

This document describes the **runtime executor**: how a compiled shader *graph* is allocated to physical textures (liveness/pooling), how frames execute (pass order, ping-pong, compute, scatter, presentation), per-frame uniform flow, texture formats, resize, and the abstract `Backend` contract that every backend (and the C# port) must implement. It does **not** cover the graph compiler (which produces `graph.passes`, `graph.programs`, `graph.textures`, `graph.renderSurface`) — those are inputs to this subsystem.

---

## 0. Inputs: the compiled `graph` object shape

The `Pipeline` is constructed from a `graph`. Fields consumed by this subsystem:

- `graph.passes`: `Array<Pass>` — executed in array order each frame.
- `graph.programs`: `Map<string, ProgramSpec>` **or** plain object keyed by program id. `resolveProgramSpec` checks `Map` first (`instanceof Map` + `.has`), then plain object (`programs[id]`).
- `graph.textures`: `Map<texId, TextureSpec>`. (`.get`, `.entries`, `.values` used. Code defensively uses `?.get?.`.)
- `graph.renderSurface`: `string` — name of the global surface to present to screen (e.g. `"o0"`). If absent, nothing is presented.

### Pass object (fields read by pipeline + passed to backend)
```
Pass = {
  id: string,                 // unique pass id, used for error reporting
  program: string,            // program id key into graph.programs
  nodeId?: string,            // effect instance id (for asyncInit lifecycle)
  effectKey?: string,         // effect registry key (for asyncInit lifecycle)
  inputs?:  { [samplerName]: virtualTexId },   // textures sampled by the pass
  outputs?: { [outName]: virtualTexId },       // render/compute targets
  uniforms?: { [name]: value | AutomationConfig },
  uniformSpecs?: { [name]: { min, max } },     // consumer range for automation scaling
  clear?: any,                // clear directive (backend-interpreted)
  blend?: any,                // blend mode (backend-interpreted)
  drawMode?: string,          // e.g. "points" for scatter passes
  count?: number,             // vertex/instance count (e.g. agent count for points)
  repeat?: number | string,   // iterations per frame; string = uniform name
  conditions?: { skipIf?: Condition[], runIf?: Condition[] },
  viewport?: any,
  drawBuffers?: any,          // MRT attachment list
  storageTextures?: any,      // compute storage bindings
  samplerTypes?: any,
  entryPoint?: string,        // WGSL entry point override
  inheritsVolumeSize?: bool,  // consumer inherits unscoped volumeSize from upstream
}
Condition = { uniform: string, equals: any }
```

### TextureSpec (entries of `graph.textures`)
```
TextureSpec = {
  width:  number | string | DimSpec,   // see resolveDimension
  height: number | string | DimSpec,
  depth?: number | string | DimSpec,   // for 3D
  is3D?: boolean,
  format?: string,   // default 'rgba16f'
  // ...other backend-passed fields spread into createTexture via {...spec}
}
```
NOTE on texId conventions in `graph.textures`: global double-buffered surfaces are keyed `global_<name>` (or legacy `global<Name>`); everything else is a regular pooled texture.

---

## 1. Resource Allocation (`resources.js`) — Liveness + Linear-Scan Pooling

This is a register-allocation analog: virtual texture ids are "values", physical slots `phys_N` are "registers". It is **pure** (no backend calls). The output `Map<virtualId, physicalId>` is consumed by the graph compiler/backend to map virtual ids to real GPU textures.

### 1.1 `analyzeLiveness(passes) -> Map<virtualId,{start,end}>`
Algorithm:
1. `lifetime = new Map()`.
2. Define `touch(texId, index)`:
   - If `texId` is falsy, return.
   - If `texId.startsWith('global_')`, return (globals are infinite-lived, excluded).
   - If not in `lifetime`: set `{start:index, end:index}`.
   - Else: `start = min(start,index)`, `end = max(end,index)`.
3. For each `pass` at array `index`:
   - For each value in `pass.inputs` (if present): `touch(tex, index)`.
   - For each value in `pass.outputs` (if present): `touch(tex, index)`.
4. Return `lifetime`.

Semantics: `start` = first pass index where the texture is read or written; `end` = last pass index. Inputs and outputs both count toward liveness at the same index.

### 1.2 `allocateResources(passes) -> Map<virtualId, physicalId>`
Linear scan over passes in order. State:
- `lifetime = analyzeLiveness(passes)`.
- `allocations = new Map()`.
- `freeList = Array<{ id: string, availableAfter: number }>`.
- `physicalCount = 0`.

For `i` from `0` to `passes.length-1` (pass = passes[i]):

**Step 1 — Allocate Outputs (definitions).** For each `texId` in `pass.outputs`:
   - If `texId.startsWith('global_')`: skip (globals pre-allocated).
   - If `allocations.has(texId)`: skip (already allocated).
   - Find a free slot: `freeIdx = freeList.findIndex(item => item.availableAfter < i)`. (A slot is reusable iff it was released in a **strictly earlier** pass than the current one.)
   - If found: `item = freeList.splice(freeIdx,1)[0]; allocations.set(texId, item.id)` (reuse).
   - Else: `id = "phys_" + physicalCount++; allocations.set(texId, id)` (new slot).

**Step 2 — Release Inputs (last uses).** For each `texId` in `pass.inputs`:
   - If `texId.startsWith('global_')`: skip.
   - `l = lifetime.get(texId)`. If `l && l.end === i` (this is the last use), and `physId = allocations.get(texId)` exists: `freeList.push({ id: physId, availableAfter: i })`.

Return `allocations`.

### 1.3 Parity / correctness notes (resources)
- **Allocation order within a pass is `Object.values(pass.outputs)` order**, i.e. JS object insertion order. The C# port MUST preserve the same key insertion order to produce identical `phys_N` numbering. Use an **ordered dictionary / insertion-ordered map**.
- **Output allocation happens before input release within the same pass**: a texture that is both read and written at pass `i` (in-place) will NOT reuse its own slot at `i` because release only enqueues to `freeList` after the output loop and `availableAfter=i` is not `< i`. This is intentional — prevents a pass from aliasing its own input as a fresh output.
- The free slot search picks the **first** index satisfying `availableAfter < i` (lowest freeList position), not lowest physical id. Reuse ordering depends on freeList push order (release order, i.e. `Object.values(pass.inputs)` order). Match exactly.
- Only `global_`-prefixed ids are excluded. The legacy `global<Name>` form is NOT excluded here (it would be pooled) — but in practice globals reaching this function use the `global_` form.
- Determinism: this is fully deterministic, no RNG, no float math. Safe to port to C# 1:1 with `int physicalCount` and `List<(string id,int availableAfter)>`.

---

## 2. Registry (`registry.js`)

A module-level `Map<string, EffectDefinition>` named `effects`.
- `registerEffect(name, definition)` → `effects.set`.
- `unregisterEffect(name)` → `effects.delete` (returns bool).
- `getEffect(name)` → `effects.get` (or `undefined`).
- `getAllEffects()` → returns the live `Map`.

Used by `pipeline.initAsyncEffects`/`checkAsyncRegen` to look up `effectDef` by `pass.effectKey`. C# port: a global registry keyed by string; lookup may return null.

---

## 3. Default shaders / fullscreen geometry (`default-shaders.js`)

These define the **fullscreen pass geometry** every render-type pass uses (unless a pass supplies its own draw mode like `points`).

- `FULLSCREEN_TRIANGLE_POSITIONS = Float32Array[ -1,-1,  3,-1,  -1,3 ]` — a single oversized triangle covering NDC, 3 vertices.
- `FULLSCREEN_TRIANGLE_VERTEX_COUNT = 3`.
- `DEFAULT_VERTEX_ENTRY_POINT = 'vs_main'`, `DEFAULT_FRAGMENT_ENTRY_POINT = 'main'`.

### WebGL2 vertex shader (`DEFAULT_VERTEX_SHADER`, GLSL ES 3.00)
```glsl
#version 300 es
precision highp float;
in vec2 a_position;
out vec2 v_texCoord;
void main() {
    v_texCoord = a_position * 0.5 + 0.5;   // NDC [-1,1] -> UV [0,1]
    gl_Position = vec4(a_position, 0.0, 1.0);
}
```

### WebGPU vertex shader (`DEFAULT_VERTEX_SHADER_WGSL`)
```wgsl
struct VertexOutput {
    @builtin(position) position: vec4<f32>,
    @location(0) uv: vec2<f32>,
}
@vertex
fn vs_main(@builtin(vertex_index) vertexIndex: u32) -> VertexOutput {
    let positions = array<vec2<f32>, 3>(
        vec2<f32>(-1.0, -1.0), vec2<f32>(3.0, -1.0), vec2<f32>(-1.0, 3.0)
    );
    let pos = positions[vertexIndex];
    var out: VertexOutput;
    out.position = vec4<f32>(pos, 0.0, 1.0);
    out.uv = pos * 0.5 + vec2<f32>(0.5, 0.5);
    return out;
}
```

### PARITY HAZARD — UV/Y origin
- UV is computed as `pos*0.5+0.5`. With NDC `y=+1` at top of screen in GL clip space but sampling textures bottom-up, **`v_texCoord.y=0` corresponds to NDC y=-1 (bottom)**. WebGL2 textures are bottom-left origin; WebGPU framebuffer/texture is top-left origin. Both backends share this same vertex math, so any Y-flip reconciliation happens **inside the backends**, not here. The HLSL/Unity port (top-left, row v=0 at top) MUST decide a single convention and apply Y-flip on either UV generation or on present/sample to match the reference output pixel-for-pixel. This is the #1 cross-backend parity hazard. Verify against actual reference frames; do not assume.
- The fullscreen triangle (not quad) means UVs/positions outside [0,1]/[-1,1] are interpolated and clipped; rasterization coverage is identical but the C# port should also use a single triangle (or full quad) — interpolation of `v_texCoord` is linear either way so a quad is acceptable visually, but to match exactly use the same primitive count if any shader reads `gl_VertexID`/`vertex_index`.

---

## 4. Effect validation (`effect-validator.js`)

`validateEffectDefinition(def) -> string[]` (empty = valid). Not in the hot render path; used at registration/authoring time. Rules:
1. `def` falsy → `['Effect definition is null or undefined']`.
2. `typeof def.name !== 'string' || !def.name` → error "Missing or invalid \"name\" property".
3. `def.passes` must be a non-empty `Array`; else error. If array, for each pass at `index`:
   - `pass.program` must be a non-empty string.
   - `pass.inputs`, if present, must be `typeof === 'object'`.
   - `pass.outputs`, if present, must be `typeof === 'object'`.
4. If `def.globals` present: must be object; each `[key, spec]` must have a `spec.type`, else "Global '<key>': Missing \"type\"".

C# port: optional authoring-time validator; mirror messages if tooling parity matters.

---

## 5. Backend abstraction (`backend.js`) — the C# executor interface

`class Backend` defines the contract. Constructor sets:
```
this.context = context
this.textures = new Map()        // physicalId/texId -> GPU texture handle
this.programs = new Map()        // programId -> compiled program/pipeline
this.uniformBuffers = new Map()  // bufferId -> buffer handle
this.capabilities = {
  isMobile: false,
  floatBlend: true,
  floatLinear: true,
  colorBufferFloat: true,
  maxDrawBuffers: 8,
  maxTextureSize: 4096,
  maxStateSize: 2048,   // max particle state texture edge
}
```
`this.textures` is the **authoritative texture registry**; the pipeline iterates and reads it directly (e.g. `backend.textures.get(id)`, `.keys()`). Each texture handle must expose at least `{ width, height, depth? }` because the pipeline reads `existingTex.width/height/depth` to decide whether to recreate.

Required methods (every backend MUST implement; base throws):
| Method | Contract |
|---|---|
| `async init()` | Initialize device/context. Awaited by `pipeline.init`. |
| `createTexture(id, spec)` | spec = `{ width, height, format, usage[] }`. Register in `this.textures`. Returns handle. |
| `createTexture3D(id, spec)` | spec adds `depth`. For `is3D` graph textures. |
| `destroyTexture(id)` | Free GPU + remove from `this.textures`. Must be idempotent/safe on unknown id (pipeline calls it broadly). |
| `async compileProgram(id, spec)` | spec = `{ source, type, defines }`. Register in `this.programs`. Awaited. |
| `executePass(pass, state)` | Run one pass (render or compute) against `state`. |
| `beginFrame(state)` | Per-frame setup. |
| `endFrame()` | Per-frame teardown/submit. |
| `copyTexture(srcId, dstId)` | Blit. |
| `clearTexture(id)` | Optional (base no-op). Clear to transparent black. |
| `getName()` | Backend name string. |
| `static isAvailable()` | Capability probe. |
| `destroy(options={})` | Optional. `options.skipTextures` honored by pipeline.dispose. |

Methods referenced by pipeline but NOT in the base abstract class (so they are **de-facto required** of concrete backends, port must implement):
- `present(textureId)` — blit a surface texture to the canvas/screen. Pipeline calls `if (this.backend.present) backend.present(presentId)`.
- `updateTextureFromSource(texId, canvasOrImage, { flipY })` — used by asyncInit (`flipY:true`).
- `uploadDataTexture(id, Float32Array, width, height)` — used for MIDI note grid (128×16) and empty grid.
- `uploadMeshData(...)` — referenced in comments (writes Float32Array into rgba32f mesh textures).

### Capabilities semantics
- `maxStateSize` caps particle `stateSize` uniforms (see §10.6).
- `getCapabilities()` on the pipeline returns `backend.capabilities` or the defaults above.

---

## 6. Pipeline object shape (`pipeline.js`)

```
Pipeline {
  graph, backend,
  frameIndex = 0,
  lastTime = 0,
  surfaces = Map<name, SurfaceRecord>,   // global surfaces (o0..o7, geo, vol, mesh, dynamic)
  globalUniforms = {},                   // mutated in place each frame
  width = 0, height = 0,
  frameReadTextures  = Map<name, texId>, // per-frame current read binding
  frameWriteTextures = Map<name, texId>, // per-frame current write binding
  animationDuration = 10,                // seconds; oscillator loop length
  _frameState = { frameIndex, time, globalUniforms, surfaces:{}, writeSurfaces:{}, graph, screenWidth, screenHeight },
  _surfaceKeys = [], _writeSurfaceKeys = [],
  _oscillatorPassProxy = { uniforms:{} },
  _resolvedUniforms = {},
  lastPassCount = 0,
  isCompiling = false,
  _tileOffset = null, _fullResolution = null, _renderScale = null,
  externalState = { midi: null, audio: null },
  _asyncRenders = Map<nodeId, cancelFn>,
}

SurfaceRecord (2D double-buffered) = { read: texId, write: texId, currentFrame: number }
SurfaceRecord (mesh)              = { positions, normals, uvs: texId, width, height }
```

### Frame state passed to backend (`getFrameState()` returns `_frameState`)
```
state = {
  frameIndex: number,
  time: number,             // = lastTime (normalized 0..1)
  globalUniforms: object,   // live reference (mutated)
  surfaces:      { [name]: textureHandle },  // CURRENT READ texture handles
  writeSurfaces: { [name]: texId },          // CURRENT WRITE target ids (strings, not handles!)
  graph,
  screenWidth, screenHeight,
}
```
IMPORTANT asymmetry: `surfaces[name]` are texture **handles** (`backend.textures.get(readId)`), but `writeSurfaces[name]` are **string ids** (`frameWriteTextures.get(name) ?? surface.write`). The backend resolves write ids to handles itself. Surfaces whose read handle is missing are omitted from `surfaces` but still present in `writeSurfaces`.

---

## 7. Initialization (`init`, `compilePrograms`, `resolveProgramSpec`)

`async init(width, height)`:
1. `await backend.init()`.
2. `await compilePrograms()`.
3. `resize(width, height)`.

`compilePrograms()`:
1. If no `graph.passes`, return.
2. `isCompiling = true` (render() early-returns while true).
3. `compiled = new Set()`. For each pass in order:
   - If `compiled.has(pass.program)` skip (dedupe).
   - `spec = resolveProgramSpec(pass)`; if null → throw `{ code:'ERR_PROGRAM_SPEC_MISSING', program, pass:pass.id }`.
   - `await backend.compileProgram(pass.program, spec)`; add to `compiled`.
4. `finally { isCompiling = false }`.

`createPipeline(graph, options)` (factory):
- If `options.preferWebGPU && WebGPUBackend.isAvailable()`: request adapter; if `float32-filterable` feature present, request it; `requestDevice` with `requiredLimits.maxColorAttachmentBytesPerSample = min(adapter.limit, 128)`. Configure canvas context `webgpu` with `format=getPreferredCanvasFormat()`, `usage=RENDER_ATTACHMENT|COPY_DST`, **`alphaMode:'premultiplied'`**.
- Else if canvas: `getContext('webgl2', { preserveDrawingBuffer: true })`.
- Construct `new Pipeline(graph, backend)`, `await init(width||800, height||600)`.

PARITY: WebGPU canvas uses **premultiplied alpha**. Unity present path must match alpha handling (premultiplied) to avoid edge halos. Default size 800×600.

---

## 8. Surface creation (`createSurfaces`) — the global surface set

Called by `resize`. Builds the `surfaces` map. Surface name groups:

1. **Display surfaces** — always created: `o0..o7`. Default size = `width × height`, format `rgba16f`. May be overridden by a `graph.textures.get('global_<name>')` spec (dimensions resolved via `resolveDimension`, format from spec).
2. **Dynamic globals** — scan all passes' `inputs`/`outputs`; any value matching `parseGlobalName` (i.e. `startsWith('global_')` → strip prefix) is added to `surfaceNames`, EXCEPT names matching the mesh-data pattern `^mesh\d+_(positions|normals|uvs)$`.
3. **Geometry buffers** `geo0..geo7`: always `width × height`, `rgba16f` (xyz=normal, w=depth). Double-buffered.
4. **Volume buffers** `vol0..vol7`: 2D atlas, **64 × 4096** (`volumeSliceSize=64`, atlasHeight=`64*64=4096`), `rgba16f`. Layout = 64 slices of 64×64. Double-buffered.
5. **Mesh surfaces** `mesh0..mesh7`: triplet of textures, each **256 × 256**, format **`rgba32f`**:
   - `positions` (xyz world pos, w=valid flag), `normals` (xyz, w unused), `uvs` (uv, zw unused).
   - SurfaceRecord = `{ positions, normals, uvs, width:256, height:256 }`. NOT ping-pong (static data uploaded by `loadOBJ`/`uploadMeshData`). Created only if not already present (never recreated on resize).

For each double-buffered surface (groups 1–4):
- Reuse-if-unchanged: if existing `surfaces.get(name)` and `backend.textures.get(oldSurface.read)` has matching width/height → `continue` (preserves sim state across recompile). Else destroy `global_<name>_read` and `global_<name>_write`.
- Create two textures `global_<name>_read` and `global_<name>_write` with `usage: ['render','sample','copySrc','copyDst','storage']` (storage included so compute passes can write).
- `surfaces.set(name, { read:'global_<name>_read', write:'global_<name>_write', currentFrame:0 })`.

`_needsMidiNoteGrid`: set true if any pass input texId === `'midiNoteGrid'`.

`parseGlobalName(texId)`: returns `texId.replace('global_','')` if it starts with `global_`, else null. (Only the `global_` form is recognized here.)

### Texture formats summary
| Surface | Size | Format |
|---|---|---|
| o0..o7 (display) | W×H (or spec) | rgba16f (or spec) |
| geo0..geo7 | W×H | rgba16f |
| vol0..vol7 | 64×4096 | rgba16f |
| mesh*_positions/normals/uvs | 256×256 | rgba32f |
| dynamic globals | spec-resolved | spec or rgba16f |
| pooled graph textures | spec | spec.format or rgba16f (in recreateTextures) |

PARITY: `rgba16f` = IEEE half-float RGBA, **linear** (no sRGB). Unity equivalent `RenderTextureFormat.ARGBHalf`. `rgba32f` = full float (`ARGBFloat`). All textures 4-channel RGBA (per CLAUDE.md). Mesh textures MUST be 32f to hold raw Float32 vertex data.

---

## 9. Resize & texture (re)creation

`resize(width, height)`:
1. Set `width/height`.
2. `createSurfaces()`.
3. `defaultUniforms = collectDefaultUniforms()`.
4. `recreateTextures(defaultUniforms)`.
5. `initAsyncEffects()`.

`collectDefaultUniforms()`: merge every `pass.uniforms` via `Object.assign` into one object (last-write-wins across passes). Chain-scoped names (`_chain_N`) keep per-chain values distinct.

`recreateTextures(uniforms)`: iterate `graph.textures.entries()`:
- `isGlobalSurface = texId.startsWith('global_') || texId.startsWith('global')`.
- For globals: only resize if `isDynamicDimension(spec.width)` or `height` true; fixed-size globals skipped.
- Resolve `width/height` via `resolveDimension`.
- **Global branch**: derive `surfaceName`:
  - `global_` form: find first `surfaces.keys()` name where `texId.includes(name) || texId.endsWith(name)`.
  - `global<Name>` form: `suffix = texId.slice(6)`, lowercase first char (`globalCaState` → `caState`).
  - If no matching surface → continue. If existing read tex matches size → continue. Else destroy read+write and recreate both with `format=spec.format||'rgba16f'`, full usage list.
- **Non-global branch**: if existing matches size (and for 3D, depth matches) → continue. Else destroy + recreate. 3D uses `createTexture3D({...spec, width, height, depth})`; 2D uses `createTexture({...spec, width, height})`.

`isDynamicDimension(spec)`: `number` → false (fixed); `string` → true; non-null `object` → true; else true.

### `resolveDimension(spec, screenSize, uniforms={})` — exact
1. `number` → `max(1, floor(spec))`.
2. `'screen'` or `'auto'` → `screenSize`.
3. string ending `'%'` → `max(1, floor(screenSize * parseFloat(spec) / 100))`.
4. object:
   - `spec.param !== undefined`: `hasTransform = (power!==undefined || multiply!==undefined)`; `paramDefault = spec.paramDefault ?? 64`; `value = uniforms[spec.param] ?? paramDefault`; if `multiply` → `value *= multiply`; if `power` → `value = pow(value, power)`; if `hasTransform && uniforms[param]===undefined && spec.default!==undefined` → `value = spec.default`; return `max(1, floor(value))`.
   - `spec.screenDivide !== undefined`: `divisor = uniforms[screenDivide] ?? spec.default ?? 1`; return `max(1, round(screenSize / divisor))` — **uses `round`, not `floor`**.
   - `spec.scale !== undefined`: `computed = floor(screenSize*scale)`; clamp to `spec.clamp.min/max` if present; return `max(1, computed)`.
5. fallback → `screenSize`.

PARITY: `floor` for param/percent/scale, `round` for screenDivide. `Math.pow` and `parseFloat` semantics. Port with `Math.Floor`/`Math.Round` on doubles, then cast to int; guard `>=1`. `??` is JS nullish (null/undefined only, NOT 0/'' /false).

---

## 10. Per-frame execution (`render(time)`) — exact control flow

`time` is **normalized 0..1** (set by the canvas renderer; wraps each animation loop).

1. If `isCompiling` → return (skip frame).
2. `deltaTime = lastTime>0 ? time-lastTime : 0`. If `deltaTime<0` (time wrapped) → `deltaTime = 1/60/10 = 0.001666…` (one 60fps frame normalized to a 10s loop). `lastTime = time`.
3. `updateGlobalUniforms(time, deltaTime)` (§10.1).
4. `frameReadTextures.clear(); frameWriteTextures.clear()`. For each `[name, surface]` in `surfaces`: `frameReadTextures.set(name, surface.read)`, `frameWriteTextures.set(name, surface.write)`.
5. `backend.beginFrame(getFrameState())`.
6. `passCount = 0`. For `i` in `graph.passes` (in order):
   - If `shouldSkipPass(originalPass)` → continue (§10.3).
   - `pass = resolvePassUniforms(originalPass, time)` (§10.4) — may return a reused proxy.
   - `repeatCount = resolveRepeatCount(pass)` (§10.5).
   - For `iter` in `0..repeatCount-1`:
     - `state = getFrameState()`.
     - `backend.executePass(pass, state)`; `passCount++`; `updateFrameSurfaceBindings(pass, state)` (§10.2 within-frame ping-pong).
     - If `repeatCount>1`: `swapIterationBuffers(pass)`.
   - Errors are logged with `pass.id` and re-thrown.
7. `backend.endFrame()`.
8. **Presentation**: `renderSurfaceName = graph.renderSurface`. If set and surface exists and `backend.present`: `presentId = frameReadTextures.get(name) ?? renderSurface.read`; `backend.present(presentId)`. (Presents the current frame-local read texture — the freshest written content.)
9. `swapBuffers()` (§10.7 end-of-frame double-buffer swap).
10. `lastPassCount = passCount`; `frameIndex++`.

### 10.1 `updateGlobalUniforms(time, deltaTime)` — mutates `globalUniforms` in place
- `aspectValue = width/height`.
- `g.time = time`, `g.deltaTime = deltaTime`, `g.frame = frameIndex`.
- `g.resolution = [width,height]` (array reused; element-wise updated).
- `g.tileOffset` default `[0,0]`; `g.fullResolution` default `[width,height]`.
- If `_tileOffset` set: `tileOffset = _tileOffset`; else `[0,0]`.
- If `_fullResolution` set: `fullResolution = _fullResolution`; `fullAspect = fr[0]/fr[1]`; `g.aspect = g.aspectRatio = fullAspect`. Else `fullResolution=[w,h]`, `g.aspect=g.aspectRatio=aspectValue`.
- `g.renderScale = _renderScale || 1.0`.
- Audio: if `externalState.audio.waveform`/`.spectrum` present → copy to `g.audioWaveform`/`g.audioSpectrum`.
- MIDI: if `externalState.midi`: `midi.updateNoteGrid()`; `backend.uploadDataTexture('midiNoteGrid', midi.noteGrid, 128, 16)`; `g.midiClockCount = midi.clockCount`. Else if `_needsMidiNoteGrid`: upload `_emptyNoteGrid` (lazily `new Float32Array(128*16*4)`). `g.midiClockCount = g.midiClockCount || 0`.

### 10.2 Within-frame ping-pong (`updateFrameSurfaceBindings(pass, state)`)
For each `outputName` in `pass.outputs` (string only): `surfaceName = parseGlobalName(outputName)`. If global:
- `writeId = state.writeSurfaces[surfaceName]`; if none, skip.
- `currentReadId = frameReadTextures.get(surfaceName)`.
- `frameReadTextures.set(surfaceName, writeId)` — subsequent passes sample the just-written texture.
- If `currentReadId`: `frameWriteTextures.set(surfaceName, currentReadId)` — next write goes to the old read buffer (ping-pong).

Effect: multiple passes writing the same surface in one frame alternate buffers so each reads the previous write. Match this exactly; it determines which buffer holds the final content presented in step 8.

### 10.3 `shouldSkipPass(pass)`
If no `conditions` → false.
- `skipIf[]`: skip (return true) if ANY `value === condition.equals`, where `value = globalUniforms[uniform] ?? pass.uniforms?.[uniform]`.
- `runIf[]`: must match ALL; if any `value !== condition.equals` → skip (return true).
PARITY: strict `===`/`!==` comparison; with `??` fallback. Numbers vs strings differ. In C# use exact typed equality matching JS loose-but-strict `===` (no type coercion).

### 10.4 `resolvePassUniforms(pass, time)` — automation resolution
If no `pass.uniforms` → return pass. Else, into reused `_resolvedUniforms`:
- Clear prior keys to `undefined` (avoid delete deopt).
- For each `name` in `pass.uniforms`: `value=pass.uniforms[name]`, `spec=pass.uniformSpecs?.[name]`, `resolved = resolveUniformValue(value, time, spec)`; store; if `resolved !== value` set `hasOscillators=true`.
- If none changed → return original pass (no allocation).
- Else copy all pass scalar/ref fields into `_oscillatorPassProxy` (id, program, inputs, outputs, clear, blend, drawMode, count, repeat, conditions, viewport, drawBuffers, storageTextures, samplerTypes, entryPoint) and **swap** `proxy.uniforms ↔ _resolvedUniforms` (double-buffered to avoid per-frame alloc). Return proxy.

`resolveUniformValue(value, time, paramSpec)`: if not object → return as-is. If `value.type==='Oscillator'||value._ast?.type==='Oscillator'` → `pct=evaluateOscillator(value, time)`. If Midi → `evaluateMidi(value, midi, Date.now())`. If Audio → `evaluateAudio(value, audio)`. Else return value. If `paramSpec` → return `paramSpec.min + pct*(paramSpec.max-paramSpec.min)` else `pct`.

### 10.5 `resolveRepeatCount(pass)`
- No `repeat` → 1.
- number → `max(1, floor(repeat))`.
- string → look up `globalUniforms[repeat] ?? pass.uniforms?.[repeat]`; if number → `max(1, floor)`; else fall through to 1.

### 10.6 `swapIterationBuffers(pass)` (between iterations of a repeated pass)
For each global output of `pass`: swap `surface.read ↔ surface.write` AND update `frameReadTextures`/`frameWriteTextures` to match. (Per-iteration ping-pong, distinct from within-frame §10.2 and end-of-frame §10.7.)

### 10.7 `swapBuffers()` (end of frame)
For each `[name, surface]`: set `surface.currentFrame = frameIndex`.
- If `isStateSurface(name)`: persist final bindings — `surface.read = frameReadTextures.get(name)`, `surface.write = frameWriteTextures.get(name)` (only if both present). NO swap; particles/sims continue from last frame's buffers.
- Else (display surface): swap `surface.read ↔ surface.write`.

`isStateSurface(name)` true iff: exact `xyz|vel|rgba|trail`; OR suffix `_xyz|_vel|_rgba|_trail`; OR `name.includes('state')||name.includes('State')`; OR regex `^(xyz|vel|rgba|points_trail)_node_\d+$`.

PARITY: This swap logic determines which physical buffer is read next frame — getting `isStateSurface` membership wrong desyncs feedback/particle simulations. Port the predicate exactly (case-sensitive substring tests on `'state'` and `'State'`).

### 10.8 `getFrameState()` rebuild (called multiple times per frame)
Reuses `_frameState`. Clears prior surface keys (set to undefined). For each surface: `readTextureId = frameReadTextures.get(name) ?? surface.read`; `tex = backend.textures.get(readTextureId)`; if tex → `surfaces[name]=tex`. `writeTarget = frameWriteTextures.get(name) ?? surface.write`; `writeSurfaces[name]=writeTarget`. Then set scalar fields (frameIndex, time=lastTime, globalUniforms, graph, screenWidth/Height).

---

## 11. Oscillators (deterministic; MUST match bit-for-bit if used)

`TAU = Math.PI*2`. `evaluateOscillator(osc, normalizedTime)` where `osc={oscType,min,max,speed,offset,seed}`:
- `t = normalizedTime*speed + offset`.
- value by `oscType`:
  - 0 sine: `(1 - cos(t*TAU)) * 0.5`.
  - 1 tri: `tf = t-floor(t); 1 - |tf*2 - 1|`.
  - 2 saw: `t - floor(t)`.
  - 3 sawInv: `1 - (t-floor(t))`.
  - 4 square: `(t-floor(t)) >= 0.5 ? 1 : 0`.
  - 5 noise: `oscNoise(t, seed)`.
  - default: 0.
- return `min + value*(max-min)`.

Noise chain (value noise on a circle for seamless loop):
- `hash21(px,py,s)`: `x=(px*234.34+s)%1; y=(py*435.345+s)%1`; if `x<0 x+=1`; if `y<0 y+=1`; `p=x+y+(x+y)*34.23`; return `(x*y*p)%1`.
- `noise2D(px,py,s)`: integer floors `ix,iy`; fract `fx,fy`; smoothstep `fx=fx*fx*(3-2*fx)` (same fy); bilinear of `hash21` at 4 corners.
- `oscNoise(t,seed)`: `temporal=t%1; angle=temporal*TAU; radius=2; loopX=cos*radius, loopY=sin*radius; n1=noise2D(loopX+seed, loopY+seed, seed); n2=noise2D(loopX+seed*2, loopY+seed*2, seed); return (n1+n2)/2`.

PARITY HAZARD — RNG/noise: `%` is JS float remainder (NOT integer mod). C# `%` on `double` matches JS for positive operands but the code explicitly fixes negatives (`if (x<0) x+=1`). Use `double` throughout, mirror the `%1` and sign-fix exactly. `Math.cos/sin/pow` must use the platform double trig — tiny ULP differences are possible between V8 and .NET; if exact oscillator parity matters, consider a shared lookup or accept sub-ULP drift. Magic constants: `234.34, 435.345, 34.23, radius=2`.

## 12. MIDI / Audio automation

`evaluateMidi(config, midiState, currentTime)` — `config={channel,mode,min,max,sensitivity}`. If no midiState → `config.min`. `channel = midiState.getChannel(config.channel)` with fields `{key, gate, velocity, time}`. Modes:
- 0 noteChange: `rawValue=channel.key` (always).
- 1 gateNote: if `gate===1` → `key`.
- 2 gateVelocity: if `gate===1` → `velocity`.
- 3 triggerNote: if `gate===1` → `key`, then `decay=min(1, (currentTime-channel.time)*sensitivity*0.001)`, `rawValue *= (1-decay)`.
- 4 (default) velocity: if `gate===1` → `velocity` with same falloff.
- `normalized = rawValue/127`; return `min + normalized*(max-min)`.
Uses real-time `Date.now()` (wall clock ms) for falloff — non-deterministic across runs; not part of reproducible render output unless MIDI is driven deterministically.

`evaluateAudio(config, audioState)` — `config={band,min,max}`. If no audioState → `config.min`. band 0 low,1 mid,2 high,3(default) vol → `rawValue` from `audioState.{low,mid,high,vol}`; `clamp [0,1]`; return `min + rawValue*(max-min)`.

---

## 13. Misc pipeline methods (host-facing)

- `setMidiState/setAudioState` — set `externalState.*`.
- `setAnimationDuration(s)` — sets `animationDuration` (note: `render` uses normalized time directly; duration mainly informs the host's normalization and the wrap delta constant context).
- `syncTime(t)` — set `lastTime=t` (so next paused frame has `deltaTime=0`).
- `setTileRegion({offset,fullResolution,renderScale})` / `clearTileRegion()` — tiled hi-res export; feeds `g.tileOffset/fullResolution/renderScale` (default renderScale 1).
- `setUniform(name, value)`:
  - If `name==='stateSize'` or `startsWith('stateSize_node_')` and number > `capabilities.maxStateSize (2048)` → cap with warning.
  - `globalUniforms[name]=value`.
  - If `name==='palette'` and number → `expandPalette(value)` then recursively `setUniform` each expanded entry; return early.
  - `isScopedUniform = /_node_\d+$/.test(name) || /_chain_\d+$/.test(name)`.
  - For each pass: if `name in pass.uniforms` and current value not an automation config → overwrite. If not scoped: also fan out to keys starting `name+'_node_'` / `name+'_chain_'` (skip automation configs), updating both `pass.uniforms[key]` and `globalUniforms[key]`.
  - If value changed and any texture spec references `name` (direct via `dimensionReferencesParam`: `spec.param===name || spec.screenDivide===name`; or scoped via `dimensionReferencesScopedParam`: ref `startsWith(name+'_node_')||name+'_chain_'`) → `updateParameterTextures(globalUniforms)` → `recreateTextures`.
- `isAutomationConfig(value)`: object with `type` or `_ast.type` in `{Oscillator,Midi,Audio}`.
- `broadcastChainScopedParam(sourcePass, uniformName, scopedName)`: copy `sourcePass.uniforms[uniformName]` to every other pass that has `scopedName`; if `uniformName==='volumeSize'` and other pass `inheritsVolumeSize` and has `volumeSize`, also update unscoped.
- `getOutput(name)`: returns `backend.textures.get(surface.read)` for `name||graph.renderSurface`.
- `clearSurface(name)`: `backend.clearTexture(surface.read & .write)`.
- `dispose()`: cancel all async renders; destroy **every** texture in `backend.textures` (single sweep); clear `surfaces`; `backend.destroy({skipTextures:true})`; null out `graph`, `frameReadTextures`, reset `globalUniforms`.

### Async init lifecycle (CPU-side texture generation)
- `initAsyncEffects()`: per unique `nodeId` with an effect whose `asyncInit` differs from `Effect.prototype.asyncInit` (or has `_configAsyncInit`), call `_startAsyncInit`.
- `_startAsyncInit(nodeId, effectDef, {debounce, params})`: optional 300ms debounce; cancel previous render for node; build `context = { updateTexture(texName,canvas){ backend.updateTextureFromSource(`${nodeId}_${texName}`, canvas, {flipY:true}) }, width, height, params, isCancelled }`; call `effectDef.asyncInit(context)`.
- `checkAsyncRegen(nodeId, effectKey, stepValues)`: regen if any non-`alpha`/non-`_`-prefixed param that exists in `effectDef.globals` changed vs `_asyncParamCache`.

PARITY: async texture uploads use `flipY:true` — the source canvas/image is flipped vertically on upload. Match in C# upload path.

---

## 14. Consolidated PARITY HAZARDS (top of list)

1. **Y / coordinate origin.** Default VS computes UV `pos*0.5+0.5`; WebGL2 (bottom-left) vs WebGPU (top-left) vs Unity/D3D (top-left, v=0 top). Choose one convention and Y-flip consistently at sample/present. Verify against reference PNGs. Async/media uploads use `flipY:true`.
2. **Texture formats are LINEAR half/float.** rgba16f (ARGBHalf) for surfaces, rgba32f (ARGBFloat) for mesh. No sRGB encode/decode in the pipeline. Don't let Unity auto-sRGB the render textures.
3. **WebGPU canvas alphaMode = premultiplied.** Match present compositing.
4. **Object/Map insertion order** governs both `phys_N` numbering (allocateResources) and uniform fan-out — use insertion-ordered containers in C#.
5. **Double-buffer / ping-pong semantics** are three-tiered: within-frame (§10.2), per-iteration (§10.6), end-of-frame (§10.7) with the `isStateSurface` predicate deciding swap vs persist. Reproduce the exact predicate and ordering or feedback sims desync.
6. **`resolveDimension` rounding**: `floor` for param/percent/scale, `round` for screenDivide; `max(1, …)` floor; `??` is nullish only (0 is valid).
7. **deltaTime wrap constant** `1/60/10` when normalized time wraps negative.
8. **Oscillator noise float ops**: JS `%` float remainder + explicit negative fix; magic constants `234.34/435.345/34.23/radius2`; `Math.pow/cos/sin` ULP differences between V8 and .NET.
9. **Liveness self-aliasing rule**: a pass cannot reuse its own input slot for its output at the same index (release `availableAfter=i` is not `< i`).
10. **`time` is normalized 0..1**, not seconds — oscillators consume it directly.
11. **MIDI falloff uses wall-clock `Date.now()`** → non-deterministic; isolate for reproducible renders.
12. Surface dimension reuse compares `existingTex.width/height(/depth)` — the C# texture handle must expose these so recreate decisions match (avoids spurious reallocation that could reset sim state).

---

## 15. Open questions / cross-subsystem dependencies

- **Graph compiler** (not in these files) produces `graph.passes/programs/textures/renderSurface` and presumably consumes `allocateResources` output to assign `phys_N` to virtual ids. Confirm where `allocateResources` is called and how `phys_N` maps into `backend.textures` keys (the runtime uses graph-scoped ids like `global_<name>_read`, not `phys_N`, so pooling applies to the compiler's intermediate textures).
- **`executePass` semantics** (render vs `type:'compute'`, `drawMode:'points'`/scatter, `clear`, `blend`, MRT `drawBuffers`, `storageTextures`, `viewport`, `count`) live in `backends/webgl2.js` and `webgpu.js` — see Backend spec for those backends. This file only routes the pass+state to `executePass`.
- **`present(textureId)`** screen blit (Y-flip, sRGB, scaling) is backend-specific — define precisely in the backend spec.
- `expandPalette` (palette-expansion.js), `Effect`/`asyncInit` (effect.js), `MidiState`/`AudioState` (external-input.js) are external dependencies referenced here.
- `graph.textures` may key globals as `global_<name>` or legacy `global<Name>` — the compiler choice affects `recreateTextures` surface-name matching (substring/`endsWith` heuristic is fuzzy; verify compiler emits unambiguous names).
