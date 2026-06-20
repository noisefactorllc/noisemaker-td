# 05 — WebGL2 Backend (GLSL conventions & GL execution model)

Reference source: `shaders/src/runtime/backends/webgl2.js` (sole authoritative file),
with `shaders/src/runtime/default-shaders.js` (vertex shader + fullscreen geometry)
and `shaders/src/runtime/backend.js` (abstract base contract).

This document is the implementation contract for a Unity/HLSL re-implementation that
must produce **pixel-identical** output. Everything that affects bits is called out.

---

## 0. Class hierarchy & object shapes

### 0.1 `Backend` base (abstract)
Constructed with a graphics `context`. Holds three Maps and a capabilities object:

```js
this.textures        = new Map()  // physicalId(string) -> texture record
this.programs        = new Map()  // programId(string)  -> compiled program record
this.uniformBuffers  = new Map()  // bufferId -> buffer handle  (UNUSED by WebGL2 backend)
this.capabilities    = { isMobile:false, floatBlend:true, floatLinear:true,
                         colorBufferFloat:true, maxDrawBuffers:8, maxTextureSize:4096,
                         maxStateSize:2048 }
```

Abstract methods each backend must implement: `init`, `createTexture`, `createTexture3D`,
`destroyTexture`, `compileProgram`, `executePass`, `beginFrame`, `endFrame`, `copyTexture`,
`clearTexture` (optional), `getName`, static `isAvailable`, `destroy`.

> **NOTE for HLSL port:** `uniformBuffers` exists in the base but the **WebGL2 backend never
> uses UBOs**. All shader uniforms are set as *individual* `glUniform*` calls (see §8). There
> is NO `uniformLayout` vec4-slot packing in this backend — that packing scheme (if it exists)
> belongs to the WebGPU backend (separate spec). In Unity you may use a cbuffer, but the
> *values and types* must come from the per-uniform path described in §8, not from a packed
> vec4 layout.

### 0.2 `WebGL2Backend` instance fields
```js
this.gl              = context              // WebGL2RenderingContext
this.canvas          = canvas || context.canvas || null
this.isContextLost   = false
this.fbos            = new Map()  // id(string) -> WebGLFramebuffer (single-tex AND mrt_* AND copy dst)
this.depthBuffers    = new Map()  // fbo(object) -> { buffer, width, height }
this.fullscreenVAO   = null       // VAO holding the fullscreen TRIANGLE
this.emptyVAO        = null       // attribute-less VAO for points/billboards/triangles draws
this.presentProgram  = null       // { handle, uniforms:{ texture } }
this.defaultTexture  = null       // 1x1 transparent-black RGBA8, NEAREST/CLAMP — fallback for missing inputs
this.maxTextureUnits = 16         // overwritten by GL query in init()
this._vec2Buf        = Float32Array(2)   // reused scratch (no per-frame alloc)
this._vec3Buf        = Float32Array(3)
this._vec4Buf        = Float32Array(4)
```

#### Texture record (value stored in `this.textures`)
```js
{ handle:WebGLTexture, width, height, [depth], format:string,
  glFormat:{internalFormat, format, type},  // see §6
  [is3D:true], [isExternal:true] }
```
Note two creation paths store `glFormat` with key `internal` instead of `internalFormat`
(`_uploadMeshTexture`, `uploadDataTexture` use `{internal, format, type}`). `readPixels` only
reads `glFormat.type`, so this inconsistency is harmless but must be reproduced if you mirror
the structure.

#### Compiled program record (value in `this.programs`)
```js
{ handle:WebGLProgram,
  uniforms:{ [name]:{ location, type:GLenum, size:int } },   // see §7
  attributes:{ a_position:int, aPosition:int } }
```

---

## 1. Coordinate conventions (CRITICAL — top parity hazard)

### 1.1 Vertex shader (the ONLY fullscreen VS), verbatim
```glsl
#version 300 es
precision highp float;
in vec2 a_position;
out vec2 v_texCoord;
void main() {
    v_texCoord = a_position * 0.5 + 0.5;
    gl_Position = vec4(a_position, 0.0, 1.0);
}
```

### 1.2 Fullscreen geometry — a single oversized TRIANGLE (not a quad)
```js
FULLSCREEN_TRIANGLE_POSITIONS = Float32Array([ -1,-1,  3,-1,  -1,3 ])
FULLSCREEN_TRIANGLE_VERTEX_COUNT = 3
```
The triangle covers clip space `[-1,1]²` plus overdraw outside; the rasterizer clips it.

### 1.3 Resulting UV mapping
`v_texCoord = a_position*0.5 + 0.5`, so clip `(-1,-1) -> uv(0,0)`, `(1,1) -> uv(1,1)`.
In WebGL/GLSL, **clip-space +Y is up** and **`gl_FragCoord` origin is bottom-left**.
Texture sampling `texture(s, uv)` treats `uv.y=0` as the **first row uploaded** to the texture
(bottom of the framebuffer when that texture was rendered). Because both the VS UV and
`gl_FragCoord` follow GL's bottom-left convention, the whole pipeline is internally consistent
and bottom-up.

> **HLSL/Unity Y-FLIP HAZARD (read carefully):**
> - GLSL clip space: +Y up; `gl_FragCoord.y` increases upward (bottom-left origin).
> - HLSL/D3D clip space: +Y up but `SV_Position.xy` has a **top-left** origin; sample UVs
>   conventionally have `v=0` at the top.
> - Unity renders to textures **bottom-up on OpenGL/Vulkan but top-down on D3D**, and exposes
>   `_ProjectionParams.x` / `UNITY_UV_STARTS_AT_TOP` to reconcile.
> - To match Noisemaker bit-for-bit you must reproduce a **bottom-left / bottom-up** sampling
>   space everywhere a shader reads `v_texCoord` or `gl_FragCoord`. The simplest robust port:
>   keep all internal render targets bottom-up (flip on final present only), and translate any
>   `gl_FragCoord.y` use to `(height - SV_Position.y)` if your platform gives top-left.
> - The present/readback paths reveal the intended *external* orientation as **top-down**
>   (see §1.4) — do NOT confuse the internal sampling space (bottom-up) with the external
>   readback space (top-down).

### 1.4 Y-flip on I/O boundaries
- `updateTextureFromSource` sets `gl.pixelStorei(UNPACK_FLIP_Y_WEBGL, flipY)` with
  `flipY = options.flipY !== false` (**default true**). So uploaded video/image/canvas frames
  are vertically flipped on upload, placing image-row-0 at the texture's *bottom*. This is
  what makes media appear upright given the bottom-up sampling. After upload it resets the
  flag to `false`.
- `readPixels` (test/parity path) calls `gl.readPixels` (which is **bottom-up**) then manually
  **flips rows to top-down** so output matches the WebGPU backend readback orientation:
  ```js
  for (y) flipped.set(out.subarray((H-1-y)*rowBytes,(H-y)*rowBytes), y*rowBytes)
  ```
  Float textures are read as `RGBA/FLOAT` then converted per channel
  `out[i] = clamp(round(buf[i]*255), 0, 255)`. So parity diffs are in **top-down RGBA8**.

---

## 2. Initialization (`init`)

1. `isMobile = detectMobile()` — UA regex `/iPhone|iPad|iPod|Android|webOS|BlackBerry|IEMobile|Opera Mini/i`,
   OR (`'ontouchstart' in window` AND `screen.width <= 1024`).
2. Acquire extensions (booleans tracked, warnings on missing):
   `EXT_color_buffer_float`, `OES_texture_float_linear`, `EXT_float_blend`.
3. Query limits: `maxTextureUnits = MAX_TEXTURE_IMAGE_UNITS`,
   `maxDrawBuffers = MAX_DRAW_BUFFERS`, `maxTextureSize = MAX_TEXTURE_SIZE`.
4. `capabilities = { isMobile, floatBlend, floatLinear, colorBufferFloat, maxDrawBuffers,
   maxTextureSize, maxStateSize: isMobile?512:2048 }`.
   (`maxStateSize` 512 ⇒ 262k particles ≈ 48MB; caps particle state textures on mobile.)
5. Create `fullscreenVAO` (§3), `emptyVAO = gl.createVertexArray()` (empty, no attribs),
   `presentProgram` (§9), `defaultTexture` (1×1 transparent black).

`isAvailable()` = can create a `'webgl2'` context.

---

## 3. Fullscreen VAO (`createFullscreenVAO`)
1. STATIC_DRAW `ARRAY_BUFFER` of `FULLSCREEN_TRIANGLE_POSITIONS`.
2. VAO: `enableVertexAttribArray(0)`, `vertexAttribPointer(0, 2, FLOAT, false, 0, 0)`.
   Attribute **location 0** is `a_position` (forced via `bindAttribLocation` at link, §5/§9).
3. Unbind.

`emptyVAO` has no attributes — used for `points`, `billboards`, `triangles` draws where vertex
data is read inside the VS via `gl_VertexID` / texture fetches (not vertex buffers).

---

## 4. Shader assembly & defines (`injectDefines`)

Fragment source is assembled by **prepending** a fixed header and stripping any existing
`#version`:
```js
let injected = '#version 300 es\nprecision highp float;\nprecision highp int;\n'
for ([key,value] of Object.entries(defines)) injected += `#define ${key} ${value}\n`
const cleaned = source.replace(/^\s*#version.*$/m, '')   // removes one #version line
return injected + cleaned
```
Hazards / rules:
- Forced **GLSL ES 3.00** (`#version 300 es`), **highp float and highp int** precision.
  In HLSL there is no `highp/mediump`; treat all as fp32. WebGL `highp float` is IEEE-754
  single precision on virtually all desktop GPUs — match with HLSL `float` (32-bit), and
  beware any shader relying on highp int (32-bit) vs HLSL `int`.
- The regex only removes the **first** `#version` line (`/m` + `^...$`). Authoring shaders must
  not contain stray version lines.
- Defines are injected as raw `#define KEY VALUE` text (string interpolation of the JS value),
  no quoting/escaping. Numeric defaults flow through verbatim.
- `compileProgram` reads source from `spec.source || spec.glsl || spec.fragment` (first
  truthy). Vertex source from `spec.vertex || DEFAULT_VERTEX_SHADER`. Note: defines are
  injected only into the FRAGMENT source via `injectDefines`; a custom `spec.vertex` is passed
  raw (must already carry its own `#version`).

---

## 5. Program compilation (`compileProgram`, `compileShader`)
1. `source = injectDefines(rawSource, spec.defines || {})`.
2. `vsSource = spec.vertex || DEFAULT_VERTEX_SHADER`; `usingDefaultVertex = !spec.vertex`.
3. Compile VS then FS via `compileShader` (throws `{code:'ERR_SHADER_COMPILE', detail, source}`
   on failure, logging the log + full source).
4. `createProgram`, attach both. If `usingDefaultVertex`: `bindAttribLocation(program,0,'a_position')`.
5. `linkProgram`; on failure throw `{code:'ERR_SHADER_LINK', detail:log, program:id}`.
6. `deleteShader` on both (program retains them).
7. `uniforms = extractUniforms(program)` (§7).
   `attributes = { a_position: getAttribLocation('a_position'), aPosition: getAttribLocation('aPosition') }`.
8. Store `{handle, uniforms, attributes}` in `programs[id]`.

---

## 6. Texture formats, filtering, wrap (`resolveFormat`, `createTexture`, `createTexture3D`)

### 6.1 Format table (string → GL triple). Fallback for unknown = `rgba8`.
| format    | internalFormat | format | type           |
|-----------|----------------|--------|----------------|
| `rgba8`   | RGBA8          | RGBA   | UNSIGNED_BYTE  |
| `rgba16f` | RGBA16F        | RGBA   | HALF_FLOAT     |
| `rgba32f` | RGBA32F        | RGBA   | FLOAT          |
| `r8`      | R8             | RED    | UNSIGNED_BYTE  |
| `r16f`    | R16F           | RED    | HALF_FLOAT     |
| `r32f`    | R32F           | RED    | FLOAT          |

All textures are conceptually 4-channel RGBA per project convention; `r*` formats exist but
core surfaces are RGBA.

### 6.2 2D texture creation (`createTexture`)
- `texImage2D(TEXTURE_2D,0,internalFormat,w,h,0,format,type,null)` (storage only).
- **Filtering: MIN=NEAREST, MAG=NEAREST.** Wrap: **S=CLAMP_TO_EDGE, T=CLAMP_TO_EDGE.**
- If `spec.usage` includes `'render'`, create a single-attachment FBO (§10) and clear it to
  transparent black (initializes storage; avoids `GL_INVALID_VALUE` on some drivers).

> **FILTERING HAZARD:** Surfaces sample with **point/NEAREST** filtering and **CLAMP** wrap by
> default. Effects that want smooth sampling must implement it in-shader; do not let Unity apply
> bilinear filtering implicitly. Match `FilterMode.Point` + `TextureWrapMode.Clamp` on internal
> RenderTextures. (External media textures and 3D textures differ — see below.)

### 6.3 3D textures (`createTexture3D`)
- `texImage3D(TEXTURE_3D,0,internalFormat,w,h,depth,0,format,type,null)`.
- **Filter is configurable:** `spec.filter === 'nearest' ? NEAREST : LINEAR` (default LINEAR /
  trilinear). Wrap S/T/R = CLAMP_TO_EDGE. WebGL2 cannot render to 3D textures (sample/lookup only).
- Record carries `is3D:true` (drives `TEXTURE_3D` binding target in §8).

### 6.4 External media textures (`updateTextureFromSource`)
- Created lazily / re-created on dimension change. **Filter MIN=MAG=LINEAR**, wrap CLAMP_TO_EDGE,
  format `rgba8`, `isExternal:true`.
- Upload: `pixelStorei(UNPACK_FLIP_Y_WEBGL, flipY)` (default true) then
  `texImage2D(TEXTURE_2D,0,RGBA,RGBA,UNSIGNED_BYTE, source)`, then reset flip to false.
- Dimension source: video→`videoWidth/Height`, image→`naturalWidth||width`, canvas/ImageBitmap→`width/height`.

### 6.5 Mesh / data textures
- `_uploadMeshTexture` & `uploadMeshData`: positions `RGBA32F`, normals `RGBA32F`, uvs `RGBA16F`.
  IDs: `global_${meshId}_positions|_normals|_uvs`. NEAREST/NEAREST, CLAMP/CLAMP. Created via
  `texImage2D(...,RGBA,FLOAT,data)` or updated via `texSubImage2D`.
- `uploadDataTexture`: `RGBA32F`, NEAREST/CLAMP, create-or-`texSubImage2D`.

> **No sRGB anywhere.** All internal formats are linear (`RGBA8`/`RGBA16F`/`RGBA32F`); none are
> `SRGB8_ALPHA8`. There is no `gl.enable` of `FRAMEBUFFER_SRGB` and WebGL has none. **Do all math
> in linear space; the canvas default backbuffer is non-sRGB.** In Unity, use **linear color
> space disabled OR ensure RenderTextures are NOT sRGB (`RenderTextureReadWrite.Linear`)** so no
> implicit sRGB encode/decode happens. Sampling and writes are raw byte/float values.

---

## 7. Uniform reflection (`extractUniforms`)
For each of `ACTIVE_UNIFORMS`:
```js
uniforms[info.name] = { location, type:info.type, size:info.size }
if (info.name.endsWith('[0]')) uniforms[info.name.slice(0,-3)] = same   // alias array base name
```
So `audioWaveform[0]` is also keyed as `audioWaveform`. Locations are GL-driver-assigned; in
HLSL you bind by name/register — the *type* (`info.type` GLenum) drives the setter (§8).

---

## 8. Uniform binding (`bindUniforms`, `_setUniform`, `bindTextures`)

### 8.1 Order & precedence (parity-critical evaluation order)
1. **Pass uniforms first** (`pass.uniforms` — DSL/effect defaults + *resolved* automation:
   oscillators, MIDI, audio). Skip `undefined`/`null`. Skip names not present in
   `program.uniforms`.
2. **Then `state.globalUniforms`** (time, resolution, etc.) — but **SKIP any name already in
   `pass.uniforms`**: `if (pass.uniforms && name in pass.uniforms) continue`.
   ⇒ **Pass uniforms win** because they hold resolved oscillator values.

> **HAZARD:** Reproduce this precedence exactly. If both a pass uniform and a global uniform
> share a name, the *pass* value is used and the global is ignored.

### 8.2 Per-type setter (`_setUniform`) — switch on GL `info.type`
| GL type     | Setter | Coercion rules |
|-------------|--------|----------------|
| FLOAT       | `uniform1fv` if value is Float32Array/Array else `uniform1f` | scalar or array |
| INT / BOOL  | `uniform1i` | boolean → `value?1:0`, else raw number |
| FLOAT_VEC2  | `uniform2fv(_vec2Buf)` | if not Array, splat scalar to `[v,v]`; missing comp → `?? 0` |
| FLOAT_VEC3  | `uniform3fv(_vec3Buf)` | splat to `[v,v,v]`; missing → `?? 0` |
| FLOAT_VEC4  | `uniform4fv(_vec4Buf)` | splat to `[v,v,v,v]`; **w defaults `?? 1`**, x/y/z `?? 0` |
| FLOAT_MAT3  | `uniformMatrix3fv(loc,false,value)` | **transpose=false** |
| FLOAT_MAT4  | `uniformMatrix4fv(loc,false,value)` | **transpose=false** |

> **VEC4 W-DEFAULT HAZARD:** A vec4 with a missing/`undefined` 4th component gets **w = 1**,
> while vec2/vec3 missing components and vec4 x/y/z default to **0**. Scalar-to-vector splat:
> passing a single number `n` to a vecN uniform yields `(n,n,…)` — except vec4 where the splat
> is `[n,n,n,n]` then w stays `n` (Array branch not taken, so `?? 1` does NOT override an
> explicit splat value). Trace carefully: for non-array `value`, vec4 uses `[value,value,value,value]`
> so w = value, not 1. The `?? 1` only applies when the array element is `undefined`.

> **MATRIX HAZARD (row vs column major):** GLSL matrices are **column-major**, and the backend
> uploads with **transpose=false**, i.e. the JS array is interpreted column-major as-is. HLSL
> matrices are **row-major by default** and `mul()` order differs. When porting:
> - Either upload matrices transposed and keep HLSL row-major semantics, OR
> - Mark HLSL matrices `column_major` and keep `mul(M, v)` vs `mul(v, M)` consistent with GLSL
>   `M * v`. GLSL `M * v` treats `v` as a column vector (post-multiply). Verify each matrix
>   uniform's storage order against the producing JS code; a silent transpose here breaks parity.

> Types NOT handled: SAMPLER types (handled in `bindTextures`), `INT_VECn`, `UINT*`,
> `BOOL_VECn`, `MAT2`, non-square mats. If a shader declares those, the value is silently
> dropped. A port must either avoid them or extend the switch.

### 8.3 Texture/sampler binding (`bindTextures`)
Iterate `pass.inputs` (object `{ samplerName: texId }`) in **insertion order**, assigning
texture units `0,1,2,…`:
1. Resolve `texId`:
   - If `parseGlobalName(texId)` non-null (a *global surface* — see §11):
     a. try `textures.get(texId)` (scoped name, e.g. `..._chain_0`);
     b. else strip trailing `_chain_\d+` and try unscoped (mesh uploads use base name);
     c. else `state.surfaces[globalName].handle` (ping-pong render targets).
   - Else `textures.get(texId).handle`.
2. If still missing → use `defaultTexture` (1×1 transparent black). **No warning** — reading an
   uninitialized surface (e.g. `o0` before write) silently yields transparent black.
3. `activeTexture(TEXTURE0+unit)`; `bindTexture(is3D?TEXTURE_3D:TEXTURE_2D, texture)`.
4. If `program.uniforms[samplerName]` exists: `uniform1i(location, unit)`.
5. `unit++`. Throw `ERR_TOO_MANY_TEXTURES` if `unit >= maxTextureUnits`.

> **HAZARD:** texture-unit assignment is by iteration order of `pass.inputs` keys. Preserve that
> ordering when porting (object key insertion order in JS == declaration order).

---

## 9. Present program (`createPresentProgram`, `present`)
Fragment shader (verbatim):
```glsl
#version 300 es
precision highp float;
in vec2 v_texCoord;
uniform sampler2D u_texture;
out vec4 fragColor;
void main() { fragColor = texture(u_texture, v_texCoord); }
```
- VS = `DEFAULT_VERTEX_SHADER`; `bindAttribLocation(program,0,'a_position')`.
- `present(textureId)`:
  1. Bind default framebuffer (`null`), `viewport(0,0,drawingBufferWidth,drawingBufferHeight)`.
  2. `clearColor(0,0,0,1)` + `clear(COLOR_BUFFER_BIT)` — **opaque black** screen clear (alpha 1).
  3. Use present program, bind source texture to unit 0, draw the fullscreen TRIANGLE
     (`drawArrays(TRIANGLES,0,3)`).
- Present uses the **same bottom-up VS mapping** — the on-screen result is the surface sampled
  straight through with no flip. (Readback path flips; present does not.)

---

## 10. Framebuffer setup

### 10.1 Single-attachment FBO (`createFBO`)
`framebufferTexture2D(FRAMEBUFFER, COLOR_ATTACHMENT0, TEXTURE_2D, texture, 0)`;
check `FRAMEBUFFER_COMPLETE`; clear to `(0,0,0,0)`; store in `fbos[id]`.

### 10.2 MRT FBO (`createMRTFBO(id, textures[])`)
- Cached by `id`. Attach `textures[i]` to `COLOR_ATTACHMENT0+i`. `drawBuffers([...attachments])`.
- Status checked; stored in `fbos[id]`.

### 10.3 Depth (`ensureDepthBuffer(fbo,w,h)`)
- Only for `drawMode:'triangles'`. Creates/reuses a `DEPTH_COMPONENT24` renderbuffer attached
  as `DEPTH_ATTACHMENT`; resizes on dimension change; cached `depthBuffers[fbo]={buffer,w,h}`.

### 10.4 Copy/blit (`copyTexture`)
Uses `blitFramebuffer(0,0,sw,sh, 0,0,dw,dh, COLOR_BUFFER_BIT, NEAREST)` — **NEAREST** filter,
straight copy (no flip; both bottom-up). Creates ad-hoc read/draw FBOs as needed (caches draw
FBO under `dstId`).

### 10.5 `clearTexture(id)` clears the texture's FBO to `(0,0,0,0)`.

---

## 11. Global-surface name resolution (`parseGlobalName`)
Given a string texId:
- `'global_NAME'` → returns `NAME` (strip `global_`).
- `'globalXxx'` (len>6, char at index 6 is `[A-Z0-9]`) → camelCase: lowercase first char of
  suffix, e.g. `globalFlowState` → `flowState`.
- otherwise `null`.

These names index `state.writeSurfaces[name]` (current write target id, output side) and
`state.surfaces[name]` (current read texture record `{handle,width,height}`, input side) — the
ping-pong surface system. Surfaces `o0..o7` are USER-ONLY; effects allocate their own internal
textures (project convention).

---

## 12. Pass execution (`executePass`) — full control flow

`pass` (object) relevant fields:
```
id, program, effectKey?, type?,
inputs?:{ sampler: texId },
outputs?:{ color|key: texId } | { outputBuffer: texId },
storageTextures?:{ key: texId },         // compute-style
drawBuffers?:int,                        // >1 ⇒ MRT
drawMode?: 'points'|'billboards'|'triangles'|undefined(fullscreen),
count?: number | 'auto' | 'screen' | 'input',
countUniform?: string,                   // triangles: read count from a uniform
viewport?:{x,y,w,h},
blend?: [src,dst] | truthy,
uniforms?:{...}
```
`state` fields used: `globalUniforms`, `writeSurfaces`, `surfaces`.

Steps:
1. **Drain GL errors** (up to 100) so only this pass's errors surface.
2. **Compute→render conversion** if `pass.storageTextures` OR `pass.outputs.outputBuffer`:
   `effectivePass = convertComputeToRender(pass)` (§13), else `effectivePass = pass`.
3. Look up `program = programs[effectivePass.program]`; throw `ERR_PROGRAM_NOT_FOUND` if absent.
   `gl.useProgram(handle)`.
4. **MRT detection:** `isMRT = effectivePass.drawBuffers > 1 || outputKeys.length > 1`
   (`outputKeys = Object.keys(outputs||{})`).
5. **If MRT:** for each output key, resolve global → `writeSurfaces[name]` if present; first
   resolved id becomes `outputId` (primary ref); collect texture handles (warn if missing);
   first found tex → `viewportTex`. Build `mrtId = mrt_${pass.id}_${ids.join('_')}`,
   `fbo = createMRTFBO(mrtId, textures)`, `mrtAttachmentCount = textures.length`.
6. **Else (single):** `outputId = outputs.color ?? Object.values(outputs)[0]`; resolve global
   via `writeSurfaces`; `fbo = fbos.get(outputId)` (warn if missing and not `'screen'`);
   `viewportTex = textures.get(outputId)`.
7. `bindFramebuffer(FRAMEBUFFER, fbo || null)` (null = default backbuffer).
8. If MRT and fbo: re-issue `drawBuffers([COLOR_ATTACHMENT0..count-1])` using
   `mrtAttachmentCount` (not key count — handles missing textures).
9. **Viewport:** `viewportTex` → `(0,0,w,h)`; else `pass.viewport` → `(x,y,w,h)`;
   else `(0,0,drawingBufferWidth,drawingBufferHeight)`.
10. `bindTextures` (§8.3), then `bindUniforms` (§8.1).
11. **Blend** (§14).
12. **Draw** by `drawMode` (§15).
13. Drain & log up to 100 GL errors with context (id, effectKey, program, output, inputs).
14. **Cleanup:** `bindFramebuffer(null)`, `useProgram(null)`, `disable(BLEND)`.

> Note: the pass does **NOT clear the color target** before drawing (the debug clear is
> commented out). Targets retain prior contents unless an effect clears them or blending is off
> and the fullscreen triangle fully overwrites. The only auto-clears are FBO creation (§10.1),
> `clearTexture`, present, and the per-pass DEPTH clear for triangles (§15.3).

---

## 13. Compute → GPGPU render conversion (`convertComputeToRender`)
WebGL2 has no compute shaders; passes are run as fragment shaders over the fullscreen triangle.
1. `renderPass = {...pass, type:'render', _originalType:'compute'}`.
2. If `pass.storageTextures`: `renderPass.outputs = {...mapping key→texId}` (1:1).
3. If `pass.outputs`: rebuild `renderPass.outputs`, renaming key `outputBuffer` → `color`,
   others unchanged. (This BLOCK RUNS AFTER the storageTextures block and overwrites it if both
   exist — `outputs` wins.)
4. If still no outputs: `renderPass.outputs = { color: 'outputTex' }`.

Then MRT detection/execution proceeds normally. Multi-output compute ⇒ MRT with
`COLOR_ATTACHMENT0..n`. So in HLSL these become multi-RT fragment passes, and the GLSL
"compute" body must be written/translated as a fragment shader writing `fragColor`/MRT outputs.

---

## 14. Blending (`resolveBlendFactor`)
```js
if (pass.blend) {
  enable(BLEND)
  if Array.isArray(blend): blendFunc(resolve(blend[0]), resolve(blend[1]))
  else: blendFunc(ONE, ONE)            // truthy-but-not-array ⇒ additive
} else { disable(BLEND) }
```
- **Default (no `blend`): blending DISABLED** (source overwrites dest).
- Truthy non-array `blend` ⇒ **additive `ONE,ONE`**.
- `blendFunc` only (no separate alpha func, no `blendEquation` change ⇒ equation is GL default
  `FUNC_ADD`). Blend is reset to disabled after every pass.
- Factor string map (both UPPER GL names and WebGPU-style lowercase aliases). Unknown → `ONE`.
  Numbers pass through. Key entries:
  `ZERO,ONE,SRC_COLOR,ONE_MINUS_SRC_COLOR,DST_COLOR,ONE_MINUS_DST_COLOR,SRC_ALPHA,
  ONE_MINUS_SRC_ALPHA,DST_ALPHA,ONE_MINUS_DST_ALPHA,CONSTANT_COLOR,...,SRC_ALPHA_SATURATE`
  plus lowercase: `zero,one,src,one-minus-src,dst,one-minus-dst,src-alpha,one-minus-src-alpha,
  dst-alpha,one-minus-dst-alpha`.

> **HAZARD:** the scatter/deposit passes (points/billboards) typically rely on **additive
> ONE,ONE** blending into a float accumulation target. Unity must use `Blend One One`,
> `BlendOp Add`, and the target must be float (RGBA16F/32F) with `EXT_float_blend` semantics.
> Float-target blending requires `EXT_float_blend` in WebGL; if unavailable, results differ.

---

## 15. Draw modes (`executePass` draw section)

### 15.1 `points` (scatter / deposit)
- `count = pass.count || 1000`. If `count ∈ {'auto','screen','input'}`:
  - `'input'`: refTex from `inputs.xyzTex || inputs.inputTex`; if global → `state.surfaces[name]`,
    else `textures.get(id)`.
  - else (`'auto'`/`'screen'`): refTex = `textures.get(outputId)`.
  - `count = refTex.width*refTex.height` if valid, else `drawingBufferWidth*drawingBufferHeight`.
- `bindVertexArray(emptyVAO)`; `drawArrays(POINTS, 0, count)`.
- VS computes per-point position via `gl_VertexID` and texture fetch of agent/particle state
  (one point per particle/texel). `gl_PointSize` must be set in the VS; points rasterize as
  1×N pixel sprites with `gl_PointCoord` in `[0,1]` (origin top-left in GL point sprites —
  another flip nuance vs HLSL which has no point sprites; emulate with quads/`billboards`).

> **HLSL HAZARD:** D3D11/Unity has **no GL point-sprite primitive**. Port `points` scatter as
> either a geometry/compute expansion to quads or use the `billboards` path (§15.2). `gl_PointSize`
> and `gl_PointCoord` have no direct HLSL equivalent. Match the deposit math by emitting 1-pixel
> quads at the same NDC location.

### 15.2 `billboards`
- Same count resolution as points but `'input'` only checks `inputs.xyzTex`.
- `drawArrays(TRIANGLES, 0, count*6)` — **6 vertices/particle** (2 tris = quad). `emptyVAO`.
- VS derives particle index = `gl_VertexID / 6`, corner = `gl_VertexID % 6`.

### 15.3 `triangles` (mesh rendering, the only 3D path)
- Viewport dims = `viewportTex?.width/height || drawingBuffer*`.
- If fbo: `ensureDepthBuffer(fbo,w,h)`.
- **State set:** `enable(DEPTH_TEST)`, `depthFunc(LESS)`, `depthMask(true)`,
  `enable(CULL_FACE)`, `frontFace(CCW)`, `cullFace(BACK)`. **Clear DEPTH_BUFFER_BIT** (only).
- Count: `pass.count || 3`. If `countUniform` set: read `pass.uniforms[name]` else
  `state.globalUniforms[name]`; use if `number > 0`. Else if `'auto'|'input'`: derive from mesh
  position texture (`inputs.meshPositions || inputs.inputTex`, with `_chain_\d+` strip and global
  fallback) as `w*h`, else fallback 3.
- `drawArrays(TRIANGLES,0,count)` with `emptyVAO`. After: `disable(DEPTH_TEST)`, `disable(CULL_FACE)`.

> **HLSL HAZARD (winding/cull/depth):** GL **CCW = front**, cull **BACK**; depth test **LESS**,
> NDC depth range `[-1,1]` in GL (clip) but `[0,1]` after viewport transform — Unity/D3D uses
> NDC depth `[0,1]` and (often) **CW front-facing** depending on flips. Match: front-face = CCW,
> cull back, depth compare = Less, and reconcile the depth-range/Y differences from the
> projection matrix you upload. Depth buffer is `DEPTH_COMPONENT24` (24-bit). Clear depth before
> the triangle pass only.

### 15.4 default (fullscreen)
- `bindVertexArray(fullscreenVAO)`; `drawArrays(TRIANGLES, 0, 3)` (single oversized triangle).

---

## 16. Frame lifecycle
- `beginFrame()`: only `clearColor(0,0,0,0)` (does **not** clear; sets clear color state).
- `endFrame()`: `gl.flush()`.
- `present(id)`: §9.

---

## 17. Teardown (`destroy`)
- Unless `options.skipTextures`: `destroyTexture` for all ids (deletes texture + its single FBO +
  any `mrt_*` FBO whose id `.includes(texId)`), then `textures.clear()`.
- Delete all program handles + present program; delete `fullscreenVAO`, `emptyVAO`; delete all
  depth renderbuffers; `fbos.clear()`. If `options.loseContext`: `WEBGL_lose_context.loseContext()`.
- `destroyTexture(id)`: also invalidates MRT FBOs by substring match `fboId.startsWith('mrt_') &&
  fboId.includes(id)`.

---

## 18. PARITY HAZARD CHECKLIST (consolidated)

1. **Y / coordinate origin.** Internal space is **bottom-up / bottom-left** (`gl_FragCoord`
   bottom-left; `v_texCoord=pos*0.5+0.5`). External readback is **top-down** (rows flipped).
   Media upload flips Y by default. HLSL/D3D defaults are top-left — reconcile explicitly.
2. **Matrix order:** column-major upload, `transpose=false`, GLSL `M*v` post-multiply. HLSL is
   row-major by default. A silent transpose breaks everything 3D.
3. **vec4 missing-w defaults to 1** (x/y/z and all of vec2/vec3 default 0). Scalar→vec splat semantics.
4. **No sRGB.** All formats linear (`RGBA8/16F/32F`); do all blending/math in linear; ensure
   Unity RTs are Linear read/write, backbuffer not sRGB-encoded twice.
5. **Filtering = NEAREST + CLAMP** for internal/data/mesh textures; **LINEAR** for external media
   and 3D textures (3D default trilinear, configurable). Do not let the engine apply bilinear.
6. **highp float/int** forced; treat as fp32/int32. Watch shaders depending on highp int.
7. **Uniform precedence:** pass.uniforms override globalUniforms of same name.
8. **Texture-unit order** = `pass.inputs` key insertion order. Missing input → 1×1 transparent
   black `defaultTexture` silently.
9. **Default blend = OFF**; truthy-non-array blend = additive ONE,ONE; equation always FUNC_ADD;
   float-target blending needs `EXT_float_blend` (parity vs hardware that lacks it).
10. **No point primitive in HLSL** — `points`/`gl_PointSize`/`gl_PointCoord` must be emulated.
11. **Depth/winding:** CCW front, cull back, depth LESS, 24-bit depth; depth cleared only in
    triangles pass; GL clip depth `[-1,1]`.
12. **Compute is emulated** as fullscreen/MRT fragment passes; `outputBuffer`→`color`; multi-output
    ⇒ MRT `COLOR_ATTACHMENT0..n`.
13. **No automatic color clear per pass** — targets carry prior contents; ping-pong via
    `writeSurfaces`/`surfaces`.
14. **NaN/Inf in float targets** are stored verbatim (no clamp) and only clamped in `readPixels`.
    HLSL must not auto-saturate intermediate float RTs.

---

## 19. Open questions / cross-subsystem dependencies
- **Surface / ping-pong manager:** `state.surfaces`, `state.writeSurfaces`, `state.globalUniforms`
  shapes and how `o0..o7` map to physical ping-pong textures live in the canvas orchestrator
  (`shaders/src/renderer/canvas.js`) — not in this file. Needed to know which texture a given
  `global_*` id resolves to per frame.
- **`uniformLayout` vec4 slot packing** mentioned in the task is **NOT present in this backend**;
  it is a WebGPU-backend concern. Confirm against the WebGPU spec; in WebGL2 uniforms are set
  individually by reflected name/type. A Unity cbuffer port must derive values from the
  per-uniform path here, matching the same names/types/defaults.
- **Automation resolution** (oscillators, MIDI, audio → `pass.uniforms`) happens upstream; this
  backend just consumes resolved scalars/vectors. RNG seeding / time semantics (`time`,
  `resolution` globals) are defined elsewhere; bit-exact RNG must match the GLSL hash functions
  inside individual effect shaders, not this backend.
- **DSL/manifest** define `pass.inputs/outputs/uniforms/drawMode/count/blend` — their schema and
  default `count=1000` / mesh count semantics depend on effect definitions.
- **`gl_PointSize`** is set inside effect VS code (not here); the deposit footprint must be read
  from those shaders to emulate scatter exactly.
