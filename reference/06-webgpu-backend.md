# Reference Spec 06 — WebGPU Backend

Source of truth: `shaders/src/runtime/backends/webgpu.js` (3640 lines) plus
`shaders/src/runtime/default-shaders.js`. This document describes the **canonical
intended rendering behavior** of Noisemaker as implemented for WebGPU. Where WebGPU
and WebGL2 differ, WebGPU/Dawn (D3D-style clip space, top-left texel origin) is the
closer match to Unity HLSL/D3D11. **A Unity port should mirror WGSL/WebGPU semantics,
not WebGL2.**

This backend extends a generic `Backend` base class (`../backend.js`). It holds the
texture registry (`this.textures`, a `Map<id, texRecord>`), program registry
(`this.programs`), etc. The base `Backend` provides `this.textures`, `this.programs`,
`this.capabilities`.

---

## 1. Class shape and lifetime

```
class WebGPUBackend extends Backend {
  device, context, queue
  pipelines      : Map<programId, pipeline>     // largely unused; see programs
  bindGroups     : Map<passId, bindGroup>       // declared, not the hot path
  samplers       : Map<string, GPUSampler>      // 'default','nearest','repeat'
  storageBuffers : Map<bufferName, GPUBuffer>   // persistent compute buffers
  commandEncoder : GPUCommandEncoder | null     // one per frame
  defaultVertexModule : GPUShaderModule | null  // lazy
  canvasFormat   : navigator.gpu.getPreferredCanvasFormat() (often 'bgra8unorm')
  depthTexture, depthTextureSize {width,height}
  uniformBufferPool : GPUBuffer[]               // free list
  activeUniformBuffers : GPUBuffer[]            // in-flight this frame
  // pre-allocated scratch for hot path (avoid GC):
  _mergedUniforms {}, _mergedUniformKeys []
  _singleUniformFloat32 = Float32Array(4)
  _singleUniformInt32   = Int32Array(4)
  _uniformBufferData = ArrayBuffer(512), _uniformDataView = DataView, _uniformBufferSize = 512
  dummyTextureView  // 1x1 transparent-black rgba8unorm
}
```

### Texture record shape (`this.textures.get(id)`)
```
{ handle: GPUTexture, view: GPUTextureView, width, height, depth?,
  format,        // logical e.g. 'rgba16f'
  gpuFormat,     // resolved e.g. 'rgba16float'
  usage,         // numeric GPUTextureUsage flags
  is3D?: bool, isExternal?: bool }
```

### init() — defaults created once
- `capabilities`: `{ isMobile, floatBlend:true, floatLinear:true, colorBufferFloat:true,
  maxDrawBuffers:8, maxTextureSize: device.limits.maxTextureDimension2D || 8192,
  maxStateSize: isMobile ? 512 : 2048 }`. **WebGPU always has float blend + linear
  filtering** — unlike WebGL2 which gates these on extensions. Parity note: if a WebGL2
  path lacked `OES_texture_float_linear`, its float surfaces sampled NEAREST; on WebGPU
  they sample LINEAR. Canonical = WebGPU (LINEAR available).
- Three samplers (see §8).
- `dummyTextureView`: 1×1 `rgba8unorm` written to `[0,0,0,0]`.
- `detectMobile()`: true if UA matches `iPhone|iPad|iPod|Android|webOS|BlackBerry|IEMobile|Opera Mini`,
  or (touch device AND `screen.width <= 1024`).

---

## 2. Coordinate conventions — CRITICAL FOR PIXEL PARITY

### 2.1 Default fullscreen-triangle vertex shader (`DEFAULT_VERTEX_SHADER_WGSL`)
Used for every render pass whose WGSL has no `@vertex` and provides no `vertexWGSL`:
```wgsl
@vertex
fn vs_main(@builtin(vertex_index) vertexIndex: u32) -> VertexOutput {
    let positions = array<vec2<f32>, 3>(
        vec2<f32>(-1.0, -1.0),
        vec2<f32>( 3.0, -1.0),
        vec2<f32>(-1.0,  3.0)
    );
    let pos = positions[vertexIndex];
    var out: VertexOutput;
    out.position = vec4<f32>(pos, 0.0, 1.0);
    out.uv = pos * 0.5 + vec2<f32>(0.5, 0.5);   // NO Y-FLIP
    return out;
}
```
The WebGL2 default vertex shader is byte-identical in math: `v_texCoord = a_position*0.5+0.5`,
same triangle positions. **So `uv` in fragment shaders has the same value across both
backends.** The difference is purely in how each API maps `uv`→pixel and clip→pixel.

### 2.2 Clip space / framebuffer Y direction — THE key hazard
- **WebGPU/D3D (and Unity/D3D11):** NDC `y = +1` is the **TOP** of the framebuffer;
  framebuffer/texel origin `(0,0)` is **top-left**. `@builtin(position)` in the fragment
  shader (used by the buffer→texture blit, line ~3356: `let y = u32(input.position.y)`)
  is in pixels measured from the **top**.
- **WebGL2/OpenGL:** NDC `y = +1` is the **BOTTOM**; framebuffer origin is bottom-left;
  `gl_FragCoord.y` is measured from the bottom.

Because the fullscreen triangle uses the **same** clip positions and **same**
`uv = pos*0.5+0.5` in both APIs, a given clip vertex lands at the **opposite** vertical
screen location, but the interpolated `uv` it carries is the same number. Net effect: a
fragment shader that derives its output purely from `uv` (the common Noisemaker case)
produces a **vertically mirrored image between WebGL2 and WebGPU** unless one side
compensates. The repo's stance: WebGPU output (top-left origin) is canonical for the
Unity port. **For Unity (top-left, D3D), reproduce the WGSL math verbatim and do NOT
add a Y-flip.** If a specific effect's reference images were authored under WebGL2,
that is a cross-subsystem question (see §15).

### 2.3 3D mesh winding (line ~1789)
`resolve3DRenderPipeline` sets `primitive.cullMode='back'`, **`frontFace:'cw'`**. Comment:
*"'cw' because WGSL shader flips Y which reverses winding order."* So mesh vertex shaders
apply a Y-flip internally; the resulting handedness flip is compensated by declaring
front faces clockwise. **Unity port:** if the mesh WGSL keeps the Y-flip, keep
`frontFace = clockwise` + back-face cull. If the Y-flip is removed for Unity, the cull
winding must be inverted to `ccw`.

### 2.4 readPixels Y orientation (test-only, §14)
WebGPU `readPixels` copies texture rows **top-to-bottom as stored** (no flip). WebGL2
`readPixels` is bottom-up and the WebGL2 backend explicitly flips rows
(`webgl2.js` ~line 611). So both test harnesses return **top-row-first** data, matching
WebGPU's native layout. Unity (D3D, top-left) matches WebGPU directly.

### 2.5 External media upload flip (line ~404, 458)
`updateTextureFromSource(id, source, {flipY=true})` defaults `flipY=true` and uses
`copyExternalImageToTexture({source, flipY}, ...)`. Video/image/canvas/ImageBitmap are
flipped on upload so that sampling with the standard `uv` gives an upright image.
WebGL2 does the same via `UNPACK_FLIP_Y_WEBGL`. **Both flip on upload; canonical =
upright after flip.**

---

## 3. Shader source resolution & type detection

### resolveWGSLSource(spec) — precedence
1. `spec.wgsl`
2. `spec.source`
3. `spec.fragment` only if it does **not** contain `#version` (i.e. not GLSL)
4. else `null` → throws `ERR_NO_WGSL_SOURCE`.

### injectDefines(source, defines)
Prepends WGSL `const` declarations (NOT `#define`):
- boolean → `const KEY: bool = true;`
- number, integer → `const KEY: i32 = N;`
- number, non-integer → `const KEY: f32 = N;`
- otherwise → `const KEY = VALUE;`
Concatenated **before** the source. **Hazard:** integer-valued floats become `i32`; if
the shader uses them in float context it may not compile or may behave differently than
GLSL `#define`. A Unity port using HLSL macros/`static const` must apply the same int/float
inference.

### compileProgram(id, spec)
1. `source = resolveWGSLSource(spec)`; inject defines.
2. `hasComputeEntry = /@compute\s/`, `hasFragmentEntry = /@fragment\s/`.
3. `detectedEntryPoints = detectEntryPoints()` (regex; see below) — overrides spec
   entry-point names when found.
4. **Routing:** if `@compute && !@fragment` → `compileComputeProgram`. Else if `@fragment`
   → `compileRenderProgram`. Else fallback → `compileRenderProgram`.

### detectEntryPoints(source) — regexes
- vertex: `/@vertex\s*\n?\s*fn\s+(\w+)/`
- fragment: `/@fragment\s*\n?\s*fn\s+(\w+)/`
- compute: `/@compute[^f]*fn\s+(\w+)/`  (the `[^f]*` skips `@workgroup_size(...)` up to the `f` of `fn`)

Default entry points (`default-shaders.js`): `DEFAULT_VERTEX_ENTRY_POINT = 'vs_main'`,
`DEFAULT_FRAGMENT_ENTRY_POINT = 'main'`. Default compute entry = `spec.computeEntryPoint
|| entryPoints[0] || 'main'`.

---

## 4. Binding parsing (`parseShaderBindings`) — the bind-group contract

Regex:
```
/@group\s*\(\s*(\d+)\s*\)\s*@binding\s*\(\s*(\d+)\s*\)\s*var(?:<([^>]+)>)?\s+(\w+)\s*:\s*([^;]+)/g
```
Produces `{ group, binding, type, name, storage, typeDecl }`. Type classification from `typeDecl`/`storage`:
1. contains `texture_storage_2d` → `'storage_texture'`
2. contains `texture_2d` or `texture_3d` → `'texture'`
3. `=== 'sampler'` → `'sampler'`
4. storage contains `uniform` → `'uniform'`
5. storage contains `storage` → `'storage'`
6. else `'unknown'`

**Dead-binding elimination (must replicate for layout parity):** strip `//` line comments,
then for each binding (except `storage_texture`/`storage`, always kept) count
word-boundary occurrences of its name (`\bname\b`). If count ≤ 1 (only the declaration)
the binding is **dropped**. Rationale: Dawn's `layout:'auto'` DCEs unused bindings; an
entry for a DCE'd binding throws *"binding index N not present in the bind group layout."*
Final list sorted by `(group, binding)`.

**Hazard:** This is a *textual* analysis. Unity (HLSL/D3D) uses explicit register binding
and the HLSL compiler will/will not strip unused resources differently. A Unity port must
build its constant-buffer/SRV layout from the *same* used-binding set, or supply dummy
resources for unused slots. Only `group(0)` is supported in bind-group creation
(line 2213: `if (binding.group !== 0) continue`).

`parseEntryPointBindings(source, bindings)`: brace-matches each entry-point body and
records which binding names appear (`\bname\b`) → `Map<entryPoint, Set<bindingIndex>>`.
Used to trim bindings per compute entry point.

---

## 5. Uniform layout parsing (4 strategies, tried in order)

`parsePackedUniformLayout(source)` returns one of several shapes; `spec.uniformLayout`
(from the effect definition) **always wins** over parsing if provided.

### 5.1 Byte layout — `parseWgslStructByteLayout` (preferred when matches)
Returns `{ type:'byte', layout:[{name,offset,size,type,components}], layout.structSize }`.
- Finds first struct named `*(Params|Uniforms|Config|Settings)` (case-insensitive).
- **Skips** the struct if its body contains `array<` (different packing).
- **Skips** if a comment annotation matching `/\/\/\s*\(\s*\w+(?:\s*,\s*\w+)+\s*\)/`
  (a comma-separated identifier list, e.g. `// (width, height, channels, frequency)`) is
  present → handled by §5.2 instead. A prose comment with parens but no comma-list does
  not trigger this.
- Requires a `var<uniform> NAME: StructName;` binding AND `NAME.field` usage somewhere.
- Field type regex accepts `f32|i32|u32|vec2f|vec3f|vec4f|vec2..u/i/f<...>`.
- Per-field WGSL alignment via `getWgslTypeInfo` (TABLE BELOW). Offset is `ceil(offset/align)*align`,
  then `offset += size`. Fields whose name starts with `_` or `pad` (case-insensitive)
  are **skipped from the layout but still advance offset**. `structSize = ceil(offset/maxAlign)*maxAlign`.

`getWgslTypeInfo` table (size, align, baseType, components):
| type | size | align | base | comp |
|---|---|---|---|---|
| f32 | 4 | 4 | float | 1 |
| i32 | 4 | 4 | int | 1 |
| u32 | 4 | 4 | uint | 1 |
| vec2f / vec2<f32> | 8 | 8 | float | 2 |
| vec3f / vec3<f32> | 12 | 16 | float | 3 |
| vec4f / vec4<f32> | 16 | 16 | float | 4 |
| vec2i/u | 8 | 8 | int/uint | 2 |
| vec3i/u | 12 | 16 | int/uint | 3 |
| vec4i/u | 16 | 16 | int/uint | 4 |
(default for unknown: size 4, align 4, float, 1.)

### 5.2 Named struct with comment annotations — `parseNamedStructLayout`
Returns `[{name, slot, components}]` (slot-based, each struct field = one slot index,
incrementing regardless of field width). Field regex captures `fieldName : type [, // (n1,n2,n3,n4)]`.
- `numComponents`: f32/i32/u32 →1; vec2 →2; vec3 →3; vec4 →4 (default 4).
- With a comment: split names on `,`, map name[i] → component `['x','y','z','w'][i]` for
  `i < min(names.length, numComponents)`. **Skip placeholder names** `_`, anything starting
  with `pad`/`unused` (case-insensitive) or `_`.
- Without a comment: single-component fields use the field name at component `x`; vec fields
  with no comment are skipped (treated as internal packed storage).
- `slot++` per field. **Note: slot is field-index, NOT a vec4 byte slot** — so this format
  assumes each struct field occupies exactly one 16-byte vec4 slot.

### 5.3 params access patterns — `parseParamsAccessLayout`
Reads field order from the first `*Params/Uniforms/Config/Settings` struct
(`fieldSlots: name→slotIndex`), then scans for
`(?:let )?VAR (?::type)? = (?:i32( )?\()?params.FIELD.COMPONENTS` and emits
`{name:VAR, slot:fieldSlot, components}`. Sorted by `(slot, componentOrder)` where
`componentOrder = {x:0,y:1,z:2,w:3}`.

### 5.4 Array fallback — `uniforms.data[N].xyz`
Only if source contains `uniforms.data[`. Regex:
```
/(?:let\s+)?(\w+)(?:\s*:\s*[^\n=]+)?\s*=\s*(?:max\s*\([^,]+,\s*)?(?:i32\s*\(\s*)?uniforms\.data\[(\d+)\]\.([xyzw]+)/g
```
Emits `{name, slot:N, components}`; sorted by `(slot, componentOrder)`.

### 5.5 Declared minimum buffer size — `parseDeclaredUniformBufferSize` / `computeWgslStructSize`
For every `var<uniform> NAME: Struct;` finds `struct Struct {…}` and computes its
**std140-ish minimum byte size** via `computeWgslStructSize`:
- Split fields at top-level commas/semicolons (respecting `<>` nesting depth so
  `array<vec4<f32>, 2>` is not split).
- For each `name: type`, `computeWgslTypeSize(type)`: arrays `array<T,N>` → element size
  computed recursively, **stride = max(elemSize, 16)** (uniform-buffer array rule),
  `size = stride*N`, `align = max(elemAlign,16)`. Scalars/vectors from the same table as
  §5.1 plus `f16`{2,2}, `bool`{4,4}, `mat3x3<f32>`{48,16}, `mat4x4<f32>`{64,16}.
- `offset = ceil(offset/align)*align; offset += size; maxAlign = max(...)`.
- Final size = `ceil(offset/maxAlign)*maxAlign`.
Stored as `program.declaredUniformBufferSize`; used as a **floor** so the runtime never
allocates a uniform buffer smaller than the shader's declared struct (Dawn rejects
otherwise).

---

## 6. Program info objects

### Compute (`compileComputeProgram`)
```
{ module, pipeline (default), pipelines: Map<entryPoint, pipeline>, isCompute:true,
  entryPoint (default), entryPoints:[...], entryPointBindings: Map,
  bindings:[...], _sourceHasBindings: /@binding\s*\(/.test(source),
  packedUniformLayout, declaredUniformBufferSize }
```
Pipeline created with `layout:'auto'`, `compute:{module, entryPoint}`. Compilation errors
(`getCompilationInfo().messages` of type `'error'`) throw `ERR_SHADER_COMPILE` with
`Line N: msg` joined by newlines.

### Render (`compileRenderProgram`)
```
{ module:fragmentModule, pipeline, isCompute:false, vertexModule, fragmentModule,
  vertexEntryPoint, fragmentEntryPoint, outputFormat, pipelineCache:Map,
  bindings, _sourceHasBindings, packedUniformLayout, declaredUniformBufferSize }
```
Vertex module selection:
1. `spec.vertexWGSL || spec.vertexWgsl` → separate module; merge its bindings (vertex wins on
   `group:binding` conflict; re-sort).
2. else if source has `@vertex` → combined module (same module for vertex+fragment).
3. else → `getDefaultVertexModule()` (fullscreen triangle), entry `'vs_main'`.

`fragmentEntryPoint = spec.fragmentEntryPoint || spec.entryPoint || 'main'`.
Initial `outputFormat = resolveFormat(spec.outputFormat || 'rgba16float')`. **Default render
target format is `rgba16float`** (half-float, linear color, NOT sRGB). Initial pipeline:
`layout:'auto'`, `primitive.topology = spec.topology || 'triangle-list'`, one fragment target
`{format:outputFormat, blend:resolveBlendState(spec.blend)}`. Cached under
`getPipelineKey({topology,blend,format})`.

---

## 7. Format & usage resolution

`resolveFormat(format)` map (logical → WGSL):
```
rgba8→rgba8unorm, rgba16f→rgba16float, rgba32f→rgba32float,
r8→r8unorm, r16f→r16float, r32f→r32float,
rg8→rg8unorm, rg16f→rg16float, rg32f→rg32float,
(pass-through identities for *unorm/*float, plus bgra8unorm)
default = format || 'rgba8unorm'
```
**No sRGB formats are used anywhere.** All intermediate surfaces are linear half-float
(`rgba16float`); 8-bit textures are `*unorm` (linear). The canvas is presented via a blit
to `canvasFormat` (typically `bgra8unorm`, also linear). **Parity: treat all color math as
LINEAR throughout; no sRGB encode/decode on render targets.** Unity: use linear (non-sRGB)
`RenderTextureFormat.ARGBHalf` for intermediates to match.

`resolveUsage(['render','sample','storage','copySrc','copyDst'])` → OR of
`RENDER_ATTACHMENT | TEXTURE_BINDING | STORAGE_BINDING | COPY_SRC | COPY_DST`.

Texture creation defaults:
- `createTexture` default usage `['render','sample','copySrc','copyDst']`; always
  `depthOrArrayLayers:1`.
- `createTexture3D` default `['storage','sample','copySrc','copyDst']`, `dimension:'3d'`,
  view `{dimension:'3d'}`.
- Mesh textures (`uploadMeshData`): all **`rgba32float`** (positions, normals, uvs) with
  usage `TEXTURE_BINDING|COPY_DST|RENDER_ATTACHMENT|STORAGE_BINDING`. **Note:** WebGPU does
  NOT auto-convert Float32→Float16 (WebGL2's `gl.FLOAT` may); so mesh data is full f32 here.
- IDs: `global_${meshId}_positions|normals|uvs`.

---

## 8. Samplers (filtering & wrap) — parity-critical

Three pre-created samplers:
| key | min/mag | addressU/V |
|---|---|---|
| `'default'` | linear | clamp-to-edge |
| `'nearest'` | nearest | clamp-to-edge |
| `'repeat'`  | linear  | repeat |

**Surface-input filtering rule (lines ~2198-2209, mirrored in legacy path ~2690):**
WebGL2 creates every surface render target with NEAREST min/mag. To match, WebGPU binds
the **`'nearest'`** sampler for surface inputs **by default**. Exception: if a pass samples
an **external** (video/image) texture (`tex.isExternal === true`), it uses `'default'`
(LINEAR), because WebGL2 uploads external media with LINEAR. Per-binding override:
`pass.samplerTypes?.[bindingName]` (rarely populated). 
**Canonical = NEAREST for surface→surface sampling, LINEAR for external media.** Unity must
set the same `filterMode` per source (Point for intermediate RTs, Bilinear for imported
media) or filter-based effects will differ.

No mipmaps are created anywhere; all sampling is at LOD 0.

---

## 9. Pass execution

`executePass(pass, state)` → `executeComputePass` if `program.isCompute` else
`executeRenderPass`. Throws `ERR_PROGRAM_NOT_FOUND` if program missing.

### Pass object shape (consumed fields)
```
pass = {
  id, program,
  inputs:  { bindingName -> textureId },     // textureId may be 'global_o0', 'o0', 'none', a raw id, or chain-scoped 'id_chain_N'
  outputs: { color|fragColor|... -> textureId },
  uniforms: { name -> number|bool|number[] },
  clear: bool,                               // loadOp 'clear' vs 'load'
  blend: undefined | [src,dst] | truthy,     // see resolveBlendState
  drawMode: 'points'|'billboards'|'triangles'|undefined(fullscreen),
  drawBuffers: number,                       // >1 → MRT
  count, countUniform,                       // for points/triangles
  entryPoint,                                // compute entry override
  workgroups:[x,y,z] | size:{width,height,depth} ,
  storageTextures: { bindingName -> 'outputTex'|'o0'..'o7'|'global_*'|id },
  samplerTypes: { bindingName -> 'default'|'nearest'|'repeat' },
  viewport: {x,y,w,h}
}
```

### State object (consumed fields)
```
state = {
  globalUniforms: { time, resolution:[w,h], deltaTime, aspect, ... },
  surfaces:      { surfaceName -> texRecord(view,...) },   // current READ texture (ping-pong)
  writeSurfaces: { surfaceName -> textureId },             // current WRITE target id
  screenWidth, screenHeight,
  graph: { renderSurface: surfaceName }
}
```

### parseGlobalName(texId)
- `'global_foo'` → `'foo'`.
- `'globalFoo'` (len>6, char after `global` is `[A-Z0-9]`) → `'foo'` (first char lowercased).
- else `null`.
Used to resolve a pass output/input to a ping-pong surface name, then to its current
read/write texture.

### executeRenderPass (single output)
1. MRT check: `isMRT = pass.drawBuffers>1 || Object.keys(pass.outputs).length>1` → delegate.
2. `outputId = pass.outputs.color || first value`. Resolve global→`state.writeSurfaces[name]`.
3. Output texture from `this.textures` or `state.surfaces`. If `outputId==='screen'` and a
   context exists, target `context.getCurrentTexture()` directly (format = canvasFormat).
   Missing → `ERR_TEXTURE_NOT_FOUND`.
4. Color attachment: `clearValue {0,0,0,0}`, `loadOp = pass.clear ? 'clear' : 'load'`,
   `storeOp:'store'`.
5. `resolvedFormat = outputTex.gpuFormat || outputTex.format || program.outputFormat`.
6. If `pass.drawMode==='triangles'` (3D): attach depth (`getDepthTexture`,
   `depth24plus`, clear 1.0), pipeline = `resolve3DRenderPipeline` (cull back, frontFace cw,
   depthCompare 'less', depthWrite true). Else pipeline = `resolveRenderPipeline` with
   topology `'point-list'` for `drawMode==='points'` else `'triangle-list'`.
7. **Bind group is created AFTER the pipeline** (so it uses the actual pipeline's
   `getBindGroupLayout(0)`).
8. `setViewport(0,0,w,h,0,1)` from `resolveViewport` (prefers the output texture's full
   size; falls back to `pass.viewport` then canvas).
9. Draw:
   - `points`: `draw(resolvePointCount, 1, 0, 0)`
   - `billboards`: `draw(count*6, 1, 0, 0)` (6 verts = 2 tris per particle)
   - `triangles`: `draw(resolveMeshVertexCount, 1, 0, 0)`
   - default fullscreen: `draw(3, 1, 0, 0)`.

### executeMRTRenderPass
Resolves each `outputs[key]` (global→write surface), builds parallel `colorAttachments` +
`formats`. Each attachment `clearValue {0,0,0,0}`, `loadOp = clear?'clear':'load'`. Format
per output `tex.gpuFormat || resolveFormat(tex.format || 'rgba16float')`. Pipeline =
`resolveMRTRenderPipeline` (one target per format, all sharing `resolveBlendState(blend)`).
Viewport from first output texture. Same draw-mode switch. `ERR_NO_MRT_OUTPUTS` if no
attachments. Used by the agent-simulation pattern (multiple state textures).

### resolvePointCount(pass,…)
`count = pass.count || 1000`. If `'auto'|'screen'|'input'`: for `'input'` prefer
`pass.inputs.xyzTex || inputTex` (resolve global→surface, else textures map); else
`outputTex`. If a ref texture found, `count = w*h`; else canvas `w*h`.

### resolveMeshVertexCount
`count = pass.count || 3`. If `pass.countUniform` resolves to a number >0 (pass.uniforms
then state.globalUniforms) use it. Else auto-detect from `pass.inputs.meshPositions ||
inputTex` (try id, then chain-stripped `id.replace(/_chain_\d+$/,'')`, then global surface);
`count = w*h` or fallback 3.

---

## 10. Compute dispatch

### executeComputePass
1. `pipeline = getComputePipeline(program, pass.entryPoint)` (caches per entry point;
   default `pass.entryPoint || program.entryPoint || 'main'`).
2. `bindGroup = createBindGroup(...)`.
3. `workgroups = resolveWorkgroups(pass, state)`.
4. `beginComputePass(); setPipeline; setBindGroup(0); dispatchWorkgroups(x,y,z); end()`.
5. If a `storage` binding named `output_buffer`/`outputBuffer` exists and `pass.outputs`,
   call `copyBufferToTexture` (a fullscreen render pass that reads the buffer per-pixel).

### resolveWorkgroups(pass,state) — DISPATCH COUNT, not workgroup_size
1. `pass.workgroups` (literal `[x,y,z]`) if present.
2. `pass.size` → `[x||width, y||height, z||depth||1]` if x&&y.
3. Else output texture dims → `[ceil(w/8), ceil(h/8), 1]`.
4. Else `state.screenWidth/Height` → `[ceil(w/8), ceil(h/8), 1]`.
5. Else throw `ERR_COMPUTE_DISPATCH_UNRESOLVED`.

**CRITICAL:** the `/8` assumes the WGSL declares `@workgroup_size(8,8[,1])`. The backend
does NOT read the workgroup size from source — it is hard-coded as 8×8 in the auto-dispatch.
**Unity port:** every compute kernel intended for the auto-dispatch path must use
`[numthreads(8,8,1)]`, and the dispatch must be `ceil(w/8) × ceil(h/8) × 1`. If a kernel
uses a different `@workgroup_size`, the pass MUST supply `pass.workgroups` or `pass.size`
explicitly. Mismatched thread-group size ⇒ wrong coverage / out-of-bounds.

---

## 11. Bind group construction (`createBindGroup`)

Operates on `program.bindings`. For multi-entry-point **compute** shaders it first narrows
`bindings` to a `neededBindingNames` set = (all `pass.inputs` keys) ∪ (all `pass.outputs`
keys) ∪ `{'params'}` ∪ (all `storage` binding names).

Per binding (only `group===0`):

- **texture:** view = `textureMap.get(name)`; fallback for `texN` names → `inputKeys[N]`;
  else `dummyTextureView` (transparent black). `entry.resource = view`.
- **sampler:** `samplers.get(pass.samplerTypes?.[name] || inputSamplerDefault)` (see §8),
  fallback `'default'`.
- **uniform:** `isStruct` heuristic = `typeDecl` has no `<`, is not a scalar/vec/mat name.
  - struct → `createUniformBuffer(pass,state,program)`.
  - individual → `getUniform(name)` with defaults by typeDecl (i32/u32→0, vec2→[0,0],
    vec3→[0,0,0], vec4→[0,0,0,0], `array<...>`→zero flat buffer sized by element stride,
    else 0) → `createSingleUniformBuffer`.
- **storage:** `createStorageBuffer`.
- **storage_texture:** `createStorageTextureView`.

`getUniform(name)` precedence: `pass.uniforms[name]` (resolved automation values) THEN
`state.globalUniforms[name]`. **Pass uniforms win** (they hold oscillator/MIDI/audio results).

### textureMap building (input resolution)
For each `pass.inputs[name] = texId`:
1. If `texId` (chain-stripped) matches `^global_mesh\d+_(positions|normals|uvs)$` → bind the
   **uploaded mesh texture** (try scoped id then unscoped), NOT a ping-pong surface.
2. Else if `parseGlobalName(texId)` yields a surfaceName: if `state.surfaces[surfaceName].view`
   exists use it (ping-pong read); else try `this.textures.get(texId)` then chain-stripped id.
3. Else `this.textures.get(texId).view`.
4. If resolved and `name==='inputTex'`, also alias to `tex0` and `inputColor`.

### Empty / legacy fallback
- If `bindings.length===0`:
  - If `!program._sourceHasBindings` (zero `@binding(` in source) → `createLegacyBindGroup`.
  - Else (all bindings DCE'd) → empty bind group from `targetPipeline.getBindGroupLayout(0)`.
- Else create bind group; on a *"binding index N not present"* error, retry up to 10× each
  time stripping the offending binding entry (handles entry-point-specific DCE).

### createLegacyBindGroup (no `@binding` shaders)
Sequential binding counter from 0: for each input alternately push `{texture}` then
`{sampler}` (sampler chosen by NEAREST-vs-external rule), `'none'`→dummy texture; then one
`{uniform buffer}` if `pass.uniforms || state.globalUniforms`.

---

## 12. Uniform buffer packing — STD140-ISH, parity-critical numeric layout

All writes are **little-endian** (`DataView.set*(…, true)`). All scalars are 4 bytes.

### createUniformBuffer(pass,state,program)
1. Merge into `_mergedUniforms`: copy `pass.uniforms` first, then `state.globalUniforms`
   for keys not already present (pass wins). Skip `undefined`.
2. If no keys → `null` (no uniform buffer).
3. `data = program.packedUniformLayout ? packUniformsWithLayout(merged, layout) :
   packUniforms(merged)`.
4. `bufferSize = max(data.byteLength, program.declaredUniformBufferSize||0, 16)`.
5. Buffer from pool (first free buffer with `size >= required`) or new
   (`UNIFORM|COPY_DST`). `writeBuffer(buffer,0,data...)`. Pushed to `activeUniformBuffers`.

### packUniforms(uniforms) — UNORDERED, JS-object-key order
Iterates `for (name in uniforms)` (insertion order of the merged object) and tightly packs:
- bool → align 4, `setInt32(value?1:0)`.
- number → align 4; **int vs float decision:** `Number.isInteger(value) && name not in
  {'time','deltaTime','aspect'}` → `setInt32`, else `setFloat32`. **HAZARD:** an integer-valued
  float uniform (e.g. an `octaves=3` meant as f32) is written as an i32 with the same bit
  pattern as integer 3 — which is a *denormal float*, NOT 3.0. The shader must declare it
  `i32`. `time/deltaTime/aspect` are force-floated.
- array len 2 → align 8, two f32.
- array len 3 → align 16, three f32, advance 16 (vec3 padded to vec4).
- array len 4 → align 16, four f32.
- array len 9 → mat3 as 3× (vec3 padded to vec4) = 48 bytes (column-major: outer loop `col`,
  inner `row`, `value[col*3+row]`).
- array len 16 → align 16, 16 f32 = 64 bytes (mat4, **column-major as supplied** — values
  written sequentially `value[0..15]`).
- other arrays → each element align 4, f32.
Final `usedSize = max(256, alignTo(offset,16))`; returns `Uint8Array` view of scratch buffer.
**The minimum returned size is 256 bytes.** Reuses `_uniformBufferData` (512B scratch) unless
larger needed.
**HAZARD (ordering):** packUniforms relies on JS object key insertion order matching the
shader struct field order. There is no name→offset mapping here. Any layout-driven shader
should use `packedUniformLayout` (§12 below); raw `packUniforms` is a legacy/fallback path
and is order-fragile. A Unity port should prefer explicit per-uniform offsets.

### packUniformsWithLayout(uniforms, layout)
- If `layout.type==='byte'` → `packUniformsWithByteLayout`.
- Else normalize to array `[{name,slot,components}]`. `bufferSize = (maxSlot+1)*16` (each slot
  = one vec4 = 16 bytes). `componentOffset = {x:0,y:4,z:8,w:12}` (byte offsets within the slot).
  For each entry, `value = _resolveUniformAlias(name, uniforms)`; write at
  `slot*16 + componentOffset[firstComp]`:
  - 1 comp: bool→f32(0/1), number→f32.
  - 2/3/4 comp: array→f32 per element; scalar number→single f32 at start.
  **All values are written as f32 in this path** (no int support). Returns `Uint8Array`.

### packUniformsWithByteLayout(uniforms, layout)
`totalSize = layout.structSize || max(offset+size)`; `bufferSize = ceil(totalSize/16)*16`
(min 16). Per entry at `entry.offset`:
- `components===1`: bool→ (int/uint)`setInt32(0/1)` else `setFloat32(0/1)`; number→ `int`→
  `setInt32(round)`, `uint`→`setUint32(round)`, else `setFloat32`.
- array → per element at `offset+i*4`, int/uint rounded, else float.
- scalar→vector → first component only.

### _resolveUniformAlias(name, uniforms, {includeChannelCount})
Built-in fallbacks when a layout names a uniform not present:
- `uniforms[name]` if defined.
- `width` → `uniforms.resolution[0]`; `height` → `uniforms.resolution[1]`.
- `channels` → `4.0` (all textures are RGBA-4).
- (byte-layout path only) `channelCount` → `4.0`.
- else `undefined` (slot left as zero).

### createSingleUniformBuffer(value, typeDecl)
- bool → i32 0/1, 4 bytes.
- number, typeDecl i32/u32 → `setInt32(round)`, else f32; 4 bytes.
- array len 2 → 8B; len 3 → **padded to 16B** (4th=0); len 4 → 16B; else: if
  `typeDecl` is `array<vec4...>` build flat `count*4` f32 (count parsed or `ceil(len/4)`),
  else `Float32Array(value)`.
- Buffer size = `max(byteLength, 16)`; from pool or new (`UNIFORM|COPY_DST`).

**WGSL/std140 alignment summary used throughout:** scalar align 4, vec2 align 8, vec3/vec4
align 16, vec3 occupies 16 bytes (padded), mat3x3 = 48, mat4x4 = 64, **uniform array stride
rounded up to 16**, struct size rounded to max member alignment. **This matches HLSL
`cbuffer` packing closely but NOT exactly** — HLSL packs scalars into the trailing
components of a 16-byte register and forbids a vector from straddling a 16-byte boundary
(it bumps to the next register), whereas WGSL/std140 here uses simple per-type alignment
(vec2 at 8-byte boundary inside a 16-byte register is allowed). **A Unity HLSL `cbuffer`
port must reproduce the WGSL offsets explicitly** (e.g. via padding fields), not rely on
HLSL's default packing, or vectors will land at different offsets.

---

## 13. Storage buffers & storage textures

### createStorageBuffer(binding,pass,state) — persistent, keyed by binding name
Sizing heuristics (all RGBA-4 f32 = 16 B/pixel; default screen 1280×720):
- `output_buffer`/`outputBuffer` → `w*h*16`.
- `stats_buffer` → `(2 + ceil(w/8)*ceil(h/8)*2)*4` (final min/max + per-workgroup min/max).
- name contains `downsample` → `ceil(w/4)*ceil(h/4)*16`.
- else default `w*h*16`.
`byteSize = ceil(max(256,byteSize)/256)*256`. Usage `STORAGE|COPY_SRC|COPY_DST`. Cached in
`this.storageBuffers` (NOT cleared per frame — persists across passes/frames).

### createStorageTextureView(binding,pass,state)
Mapping from `pass.storageTextures[binding.name]`:
- absent + name `output_texture` → `getOutputStorageView`.
- value `'outputTex'` → `getOutputStorageView`.
- value matches `^o[0-7]$` → `state.writeSurfaces[value]` → texture view.
- value is a global → `state.writeSurfaces[surfaceName]` → view.
- else `this.textures.get(value).view`.

`getOutputStorageView(state)`: prefer `state.graph.renderSurface` →
`state.writeSurfaces[name]` → texture view. Fallback: cached temp `rgba16float`
`outputStorage_${w}x${h}` (usage STORAGE|TEXTURE|COPY_SRC|COPY_DST|RENDER_ATTACHMENT).

---

## 14. Frame loop, present, readPixels

- `beginFrame()`: drains `activeUniformBuffers` back into `uniformBufferPool`, then
  `commandEncoder = device.createCommandEncoder()`.
- `endFrame()`: `queue.submit([commandEncoder.finish()])`, clears encoder.
- `present(textureId)`: blits the named texture to the canvas via `getBlitPipeline`
  (fullscreen triangle, `textureSample(srcTex, srcSampler, uv)`, uv = `pos*0.5+0.5`, **no
  Y-flip** — comment: "internal blits now handle Y-flip"). Target format = canvasFormat ||
  `bgra8unorm`. Uses the `'default'` (LINEAR clamp) sampler. Separate command encoder,
  `loadOp:'clear'` to transparent black.
- `copyTexture(src,dst)`: `copyTextureToTexture` over `[w,h,1]`, own encoder, immediate submit.
- `clearTexture(id)`: render pass clearing to `{0,0,0,0}`, immediate submit.
- `readPixels(textureId)` (TEST ONLY, async): copies texture→staging buffer
  (`bytesPerRow = ceil(w*bpp/256)*256`, bpp 4/8/16 for rgba8/16f/32f). Converts to a
  `Uint8Array(w*h*4)` of 0–255:
  - rgba16float: each channel `float16ToFloat32` then `clamp(round(f*255),0,255)`.
  - rgba32float: `clamp(round(f*255),0,255)`.
  - rgba8unorm: direct copy stripping row padding.
  **No Y-flip** — rows are returned top-first as stored (WebGPU/D3D order). WebGL2's
  readPixels flips rows to match. `float16ToFloat32(h)` is the standard IEEE-754 half
  decode (sign bit 15, exp bits 14–10 bias 15, mantissa 10 bits; denormal = `m/1024*2^-14`,
  exp 31 = Inf/NaN). **A Unity port comparing against these reference images must read the
  RT top-row-first and apply the same `round(clamp(f*255))` quantization.**

`isAvailable()`: true iff `navigator.gpu` and a non-null adapter.

---

## 15. PARITY HAZARDS (consolidated, ranked)

1. **Vertical orientation / clip-space Y.** WebGPU & Unity/D3D: NDC +Y up, framebuffer
   origin top-left, `position.y` from top. WebGL2: +Y up in NDC but origin bottom-left,
   `gl_FragCoord.y` from bottom. Identical fullscreen UVs ⇒ images differ by vertical mirror
   between backends. **Canonical = WebGPU (top-left).** Unity: copy WGSL math verbatim, no
   extra flip. Watch any effect that reads `@builtin(position)` (pixel coords).
2. **Uniform integer-vs-float coercion in `packUniforms`.** Integer-valued JS numbers (not
   time/deltaTime/aspect) are written as `i32`; their bit pattern is meaningless if the
   shader reads f32. Layout-driven paths mostly avoid this. Unity must match the per-uniform
   type exactly.
3. **HLSL cbuffer packing ≠ WGSL std140-ish.** The backend packs vec2 at 8-byte boundaries
   inside a 16-byte slot; HLSL `cbuffer` would never straddle/pack the same way by default.
   Reproduce explicit offsets with padding fields in the Unity cbuffer.
4. **Compute dispatch hard-codes 8×8 thread groups.** Auto-dispatch is `ceil(w/8)×ceil(h/8)×1`.
   Kernels must declare `[numthreads(8,8,1)]` or pass explicit `workgroups`/`size`.
5. **Sampler filter parity.** Surface→surface = NEAREST (point); external media = LINEAR.
   Wrong filter mode changes filter-based effects bit-for-bit. No mipmaps, LOD 0 only.
6. **Linear color throughout, no sRGB.** All RTs are linear (`rgba16float`/`*unorm`); canvas
   blit to `bgra8unorm` (linear). Unity intermediates must be non-sRGB linear half-float.
7. **Matrix layout.** Both mat3 (9) and mat4 (16) arrays are written **column-major as
   supplied** (mat3 columns padded to vec4 → 48 B; mat4 → 64 B). WGSL matrices are
   column-major and multiply `M * v`. HLSL defaults to row-major storage and `mul(v, M)`
   semantics — a Unity port must transpose or set `#pragma pack_matrix(column_major)` / use
   `mul(M, v)` to match.
8. **Dead-binding elimination.** Textually-unused bindings are dropped to satisfy Dawn's
   `layout:'auto'` DCE. Unity's explicit register layout must mirror the *used* set or bind
   dummies; otherwise resource registers shift.
9. **Default render format `rgba16float`.** 16-bit precision intermediates. Using full f32
   or 8-bit RTs in Unity will diverge in rounding/banding. Mesh data textures are `rgba32float`.
10. **`channels`/`channelCount` alias = 4.0**, `width/height` alias from `resolution`. A
    layout naming these without a supplied value gets these constants.
11. **Default screen size fallback 1280×720** for storage-buffer sizing when state lacks dims.
12. **3D mesh winding = `frontFace:'cw'` + back cull**, depth `depth24plus`, compare `less`,
    clear 1.0, depthWrite on — predicated on the mesh WGSL flipping Y.
13. **readPixels quantization** = `clamp(round(f*255),0,255)`, top-row-first.

## 16. Open questions / cross-subsystem deps
- **Surface ping-pong (`state.surfaces`/`writeSurfaces`) is owned by the canvas/renderer
  orchestrator** (`shaders/src/renderer/canvas.js`), not this backend. The double-buffer
  swap, render-surface selection, and chain scoping (`_chain_N` suffix) come from there —
  needed to know read vs write texture per pass.
- **`pass.uniforms` resolution (oscillators/MIDI/audio/automation)** happens upstream; this
  backend only reads final numeric values. Their numeric exactness is a separate spec.
- **`spec.uniformLayout` from effect definitions** overrides all parsing — the authoritative
  per-effect uniform layout lives in `shaders/effects/.../definition.js`, not here.
- **Whether reference/golden images were captured under WebGL2 or WebGPU** determines which
  Y-orientation is "correct" for visual diffing; assume WebGPU is canonical unless told.
- **The WGSL→HLSL transpilation of effect bodies** (noise functions, `floor`/`fract`,
  integer bit ops, `textureSample` wrap behavior) is out of scope here and is the dominant
  per-pixel risk for the actual effect math.
