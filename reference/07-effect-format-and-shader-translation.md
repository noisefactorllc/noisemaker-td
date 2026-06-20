# 07 — Effect Format & GLSL/WGSL → HLSL Shader Translation

Reference spec for re-implementing the Noisemaker **effect-definition contract** and porting its
fragment shaders to C#/HLSL (Unity) with pixel-identical output. Sources studied:

- `shaders/src/runtime/effect.js` (Effect base class, category helpers)
- `shaders/effects/synth/noise/{definition.js, glsl/noise.glsl, wgsl/noise.wgsl}`
- `shaders/effects/synth/cell/{definition.js, glsl/cell.glsl, wgsl/cell.wgsl}` — this is the
  cellular/**Voronoi** effect (there is **no** effect literally named `voronoi`; `synth/cell`,
  `func: "cell"`, is the Worley/Voronoi distance-field generator)
- `shaders/effects/filter/blur/{definition.js, glsl/{blurH,blurV}.glsl, wgsl/{blurH,blurV}.wgsl}`
- `shaders/effects/mixer/blendMode/{definition.js, glsl/blendMode.glsl, wgsl/blendMode.wgsl}` —
  this is the **blend** effect (`func: "blendMode"`; there is no `mixer/blend`)
- `shaders/src/runtime/backends/webgl2.js` `injectDefines()` (define-injection mechanism)

---

## 1. The Effect Definition Contract

### 1.1 `Effect` class (`effect.js`)

An effect definition is `export default new Effect({ ...config })`. The constructor copies these
config keys onto the instance **only if present** (truthy / defined):

| Field | Type | Semantics |
|---|---|---|
| `name` | string | Display name (e.g. `"Noise"`). |
| `namespace` | string | `synth` \| `filter` \| `mixer` \| `synth3d` \| `classicNoisedeck` … Acts as an implicit tag. |
| `func` | string | DSL function name (e.g. `noise`, `cell`, `blur`, `blendMode`). |
| `description` | string | Free text. |
| `tags` | string[] | Curated labels from `tags.js` (e.g. `["noise"]`, `["color"]`, `["noise","geometric"]`). |
| `globals` | object | Parameter/uniform spec map (see §1.2). |
| `passes` | Array | Render passes (see §1.3). |
| `textures` | object | Internal texture allocations (see §1.4). |
| `outputTex3d` | any | 3D output flag (unused by studied effects). |
| `outputGeo` | any | Geometry output flag (unused here). |
| `uniformLayout` | object | Maps uniform name → `{slot, components}` packing into `vec4[]` (WGSL backend; see §3). |
| `uniformLayouts` | object | Per-program layouts (plural) — not used by the studied effects. |
| `paramAliases` | object | Maps alias name → canonical global key (see §1.5). |
| `openCategories` | string[] | UI categories shown expanded by default. |
| `defaultProgram` | string | DSL snippet for the demo UI. |
| `hidden` | bool | If truthy sets `this.hidden = true`. |
| `deprecatedBy` | string | Replacement effect name. |
| `onInit` / `onUpdate` / `onDestroy` / `asyncInit` | Function | Lifecycle hooks; stored as `_configOn*` and invoked via the base methods. |

`onUpdate(context)` receives `{ time, delta, uniforms }` and **returns an object of uniforms to
bind**; default returns `{}`. `asyncInit(context)` gets `{ updateTexture, width, height, params,
isCancelled }` and returns a Promise (re-run on seed change). For an HLSL/Unity port, the studied
effects are **data-only** (no lifecycle hooks) — all behavior is in the shader + uniform packing.

`Effect` also initializes `this.state = {}` and `this.uniforms = {}`.

### 1.2 `globals` — the uniform/parameter spec

Each entry `globals[key] = spec`. Recognized spec fields:

| Field | Type | Semantics |
|---|---|---|
| `type` | `"float"` \| `"int"` \| `"boolean"` \| `"vec2"` \| `"vec3"` \| `"vec4"` \| `"surface"` | Data type. `surface` = a texture input selector (see blend `tex`). |
| `default` | number/bool/string | Initial value. For `surface`, default `"none"`. |
| `uniform` | string | Shader uniform name. **Absent ⇒ not a runtime uniform** (e.g. `define` params, or `surface` inputs wired via passes). |
| `min` / `max` | number | Range for numeric controls. |
| `step` | number | Slider step (blur uses `step: 1`). |
| `zero` | number | Value treated as the "off"/neutral center (e.g. blur `zero: 0`, speed `zero: 0`). |
| `choices` | object | name→value map for dropdowns. A value of `null` (e.g. `"Shapes:": null`) is a **non-selectable group header**. |
| `define` | string | Compile-time `#define NAME value` instead of a runtime uniform. **Changing it forces a shader recompile.** Used by `type→NOISE_TYPE`, `loopOffset→LOOP_OFFSET`. |
| `ui` | object | UI config (below). |

`ui` sub-fields: `label` (string), `control` (`"slider"` \| `"checkbox"` \| `"dropdown"` \|
`"color"` \| `"button"` \| `false`), `category` (string, default `"general"`), `hidden` (bool —
suppress control but keep it for automation/serialization; prefer `control:false`), plus
conditional-enable hints like `enabledBy: { param: "type", notIn: [10,11] }` and
`zero`. These are UI-only and **do not affect rendered pixels**, except `define` params and
`choices` enum values, which do.

**Category helpers** (`effect.js`):
- `DEFAULT_CATEGORY = 'general'`.
- `getUniformCategory(spec)` → `spec?.ui?.category || 'general'`.
- `groupGlobalsByCategory(globals, {includeHidden=false})` → ordered map category→`[[key,spec]…]`.
  Skips entries with `ui.control===false` or `ui.hidden===true` unless `includeHidden`. Order:
  `general` first, then other categories in first-occurrence order.

### 1.3 `passes` — render passes

`passes` is an ordered array. Each pass:

```js
{
  name: "render",            // pass id
  program: "noise",          // shader program key (file basename, no ext)
  type: "render",            // optional; "compute" => GPGPU. Default render.
  inputs:  { samplerName: "sourceTextureKey", ... },
  uniforms:{ shaderUniform: "globalKey", ... },   // optional explicit uniform remap
  outputs: { fragColor: "outputTextureKey" },
  drawMode: "points",        // optional, for agent-deposit passes
  storageTextures: {...}     // optional, triggers compute→render conversion in WebGL2
}
```

Texture key conventions:
- `"inputTex"` (input side) = the effect's incoming surface.
- `"outputTex"` = the effect's outgoing surface.
- `"o0".."o7"` are **USER-ONLY** surfaces; effects must never hardwire them.
- Internal temp textures are declared in `textures` (e.g. `"_blurTemp"`) and referenced by key.

Examples:
- **noise**: single pass `render`/`noise`, empty `inputs`, `outputs:{fragColor:"outputTex"}`.
- **cell**: single pass, `uniforms:{cellSmooth:"cellSmooth", speed:"speed"}` (explicit remap),
  `outputs:{fragColor:"outputTex"}`.
- **blur**: two passes — `blurH` (input `inputTex`→output `_blurTemp`) then `blurV`
  (input `_blurTemp`→output `outputTex`). Separable Gaussian.
- **blendMode**: single pass, `inputs:{inputTex:"inputTex", tex:"tex"}`,
  `uniforms:{mixAmt:"mix"}`, `outputs:{fragColor:"outputTex"}`.

### 1.4 `textures` — internal allocations

```js
textures: { _blurTemp: { width:"input", height:"input", format:"rgba8unorm" } }
```
`width`/`height` may be `"input"` (match input size). `format` default RGBA8 unorm. **All
textures are 4-channel RGBA.**

### 1.5 `paramAliases`

Maps external/legacy names → canonical global keys, applied before binding:
- noise: `{ noiseType:'type', loopAmp:'speed', xScale:'scaleX', yScale:'scaleY' }`
- cell: `{ cellVariation:'variation', loopAmp:'speed' }`
- blendMode: `{ mixAmt:'mix' }`

---

## 2. `injectDefines` — exact define-injection (WebGL2 backend)

`webgl2.js injectDefines(source, defines)`:
1. Builds a prefix string: `"#version 300 es\nprecision highp float;\nprecision highp int;\n"`.
2. Appends one `#define KEY VALUE\n` line per entry in `defines`.
3. Strips the **first** `#version …` line from the original source via regex
   `/^\s*#version.*$/m` (multiline, first match only).
4. Returns `prefix + defines + cleanedSource`.

**Consequences for HLSL port:**
- `NOISE_TYPE` and `LOOP_OFFSET` arrive as `#define` integers *before* the shader body. Each GLSL
  file also has its own `#ifndef NOISE_TYPE / #define NOISE_TYPE 10` (and `LOOP_OFFSET 300`)
  fallback, so the **defaults are 10 (simplex) and 300 (noise loop-offset)** if not injected.
- The WGSL twin does **not** use `#define`; instead the runtime's `injectDefines` (WGSL path)
  injects `const NOISE_TYPE` / `const LOOP_OFFSET` so the WGSL compiler dead-code-eliminates
  variants. WGSL files therefore reference `NOISE_TYPE`/`LOOP_OFFSET` as if pre-declared.
- In HLSL, model these as `#define` macros (or `static const int`) set at compile time per
  variant. Compiling a separate shader variant per (NOISE_TYPE, LOOP_OFFSET) pair matches both
  backends and avoids the giant runtime branch.

---

## 3. Uniform packing: `uniformLayout` → `vec4[]` (WGSL/HLSL cbuffer model)

The WGSL backend packs all scalar/vec uniforms into a single `array<vec4<f32>, N>` and the shader
unpacks by slot/component. **GLSL declares individual `uniform` variables instead**, but the
*values* are identical. For HLSL use a `cbuffer` laid out exactly like the WGSL `data[]` array.

**noise** (`Uniforms.data: array<vec4<f32>,5>`):

| slot.comp | name | GLSL type | notes |
|---|---|---|---|
| 0.xy | resolution | vec2 | render-target size |
| 0.z | time | float | seconds |
| 0.w | aspectRatio | float | **packed value unused** by shader; shader recomputes `aspectRatio = fullResolution.x/fullResolution.y` |
| 1.x | scaleX | float | |
| 1.y | scaleY | float | |
| 1.z | seed | float (stored), `int seed` in GLSL | |
| 1.w | loopScale | float | |
| 2.x | speed | float (GLSL `float speed`) | |
| 2.y | — | — | was `loopOffset`, now `LOOP_OFFSET` define |
| 2.z | — | — | was noiseType, now `NOISE_TYPE` define |
| 2.w | octaves | int (`i32(data[2].w)`) | |
| 3.x | ridges | bool (`data[3].x > 0.5`) | |
| 3.y | wrap | bool (`data[3].y > 0.5`) | |
| 3.z | colorMode | int (`i32(data[3].z)`) | |
| 4.xy | tileOffset | vec2 | tiled-render pixel offset |
| 4.zw | fullResolution | vec2 | full (untiled) size; **denominator for st** |

**cell** (`array<vec4<f32>,4>`):

| slot.comp | name | type |
|---|---|---|
| 0.xy | resolution | vec2 |
| 0.z | time | float |
| 0.w | seed | int (`i32(data[0].w)`) |
| 1.x | metric | int |
| 1.y | scale | float |
| 1.z | cellScale | float |
| 1.w | cellSmooth | float |
| 2.x | variation | float |
| 2.y | speed | float |
| 3.xy | tileOffset | vec2 |
| 3.zw | fullResolution | vec2 |

**blur**: WGSL `struct Uniforms { radiusX:f32, radiusY:f32, _pad1:f32, _pad2:f32 }` at
`@binding(2)`; sampler `@binding(0)`, `inputTex` `@binding(1)`. GLSL uses individual uniforms
`radiusX`, `radiusY`, plus **runtime-provided `renderScale`, `tileOffset`, `fullResolution`** (the
GLSL multiplies radius by `renderScale`; **the WGSL does NOT apply renderScale** — see hazard H1).

**blendMode**: WGSL binds `sampler@0`, `inputTex@1`, `tex@2`, `mode:i32@3`, `mixAmt:f32@4` as
separate uniforms (not packed). GLSL: `inputTex`, `tex` samplers + `mode:int`, `mixAmt:float`, plus
runtime `resolution/tileOffset/fullResolution`.

**Packing rule for HLSL:** lay out a `cbuffer` matching each effect's `data[N]` vec4 array exactly,
in slot order, std140-style (16-byte aligned vec4s). Booleans are floats compared `> 0.5`. Ints are
`i32(float)` truncation (`floor` toward zero for the non-negative values used here).

---

## 4. noise.glsl ⇄ noise.wgsl — line-by-line correspondence

Both implement the identical algorithm; differences are purely syntactic except where noted.

### 4.1 Constants & helpers
- `PI=3.14159265359`, `TAU=6.28318530718`.
- `map(v,inMin,inMax,outMin,outMax) = outMin + (outMax-outMin)*(v-inMin)/(inMax-inMin)`.
- `periodicFunction(p) = map(cos(p*TAU), -1, 1, 0, 1)` ⇒ `(cos(p*TAU)+1)*0.5`.
- `aspectRatio`: GLSL `#define aspectRatio fullResolution.x / fullResolution.y` (a **macro**, not
  a value — beware operator-precedence when substituted; here always used as a whole or
  parenthesized). WGSL passes `aspectRatio` as a var computed once. **HLSL: use a local
  `float aspectRatio = fullResolution.x/fullResolution.y;` to avoid the macro pitfall.**

### 4.2 PCG PRNG (bit-exact, the heart of determinism)
```glsl
uvec3 pcg(uvec3 v){
  v = v*1664525u + 1013904223u;
  v.x += v.y*v.z; v.y += v.z*v.x; v.z += v.x*v.y;
  v ^= v >> 16u;
  v.x += v.y*v.z; v.y += v.z*v.x; v.z += v.x*v.y;
  return v;
}
```
- All arithmetic is **unsigned 32-bit wraparound** (`uint`). HLSL `uint` matches.
- `prng(vec3 p)`: folds sign into magnitude: `p.c = p.c>=0 ? p.c*2 : -p.c*2+1` (per component),
  then `pcg(uvec3(p))` and divides by `float(0xffffffffu)` (= 4294967295.0).
  - GLSL `vec3(pcg(...))` and WGSL `vec3<f32>(u)` are **uint→float casts** (round-to-nearest).
  - `uvec3(p)` / `vec3<u32>(p)` is a **float→uint truncation** (toward zero). HLSL `(uint)` and
    `asuint` differ: use **`(uint)floatValue` (numeric truncation)**, NOT `asuint` (bit reinterpret)
    here. This is the conversion of the sign-folded float lattice coords.
- `random(st) = prng(vec3(st,0)).x`.

### 4.3 `constantFromLatticeWithOffset` — the lattice hash
1. `baseFloor = floor(lattice)`; `base = ivec2(baseFloor) + offset`; `frac = lattice - baseFloor`.
2. `seedInt = int(floor(s))`; `sFrac = fract(s)`.
3. `xCombined = frac.x + sFrac`; `xi = base.x + int(floor(xCombined))`; `yi = base.y`.
4. If `wrap`: `freqX=int(freq.x+0.5)`, `freqY=int(freq.y+0.5)`; if `>0`, wrap via
   `positiveModulo`.
5. Bit-mix jitter from `fracBits = floatBitsToUint(sFrac)` (GLSL) / `bitcast<u32>(sFrac)` (WGSL) —
   **this IS a bit-reinterpret**; HLSL use `asuint(sFrac)`:
   ```
   jitter = uvec3((fracBits*374761393u)^0x9E3779B9u,
                  (fracBits*668265263u)^0x7F4A7C15u,
                  (fracBits*2246822519u)^0x94D049B4u);
   state = uvec3(xBits,yBits,seedBits) ^ jitter;   // xBits=uint(xi) etc.
   noiseValue = float(pcg(state).x)/float(0xffffffffu);
   return periodicFunction(noiseValue - blend);
   ```
   Note `uint(xi)` where `xi` may be negative ⇒ **two's-complement reinterpret**; HLSL `(uint)int`
   is the same.
6. `positiveModulo(value,modulus)`: `if modulus==0 return 0; r=value%modulus; return r<0?r+modulus:r;`
   GLSL `%` and WGSL `%` on ints both truncate toward zero ⇒ HLSL `%` matches; the explicit
   `+modulus` fixes negatives.

### 4.4 Interpolation kernels (all share identical coefficients)
- `cubic(t)=t*t*(3-2t)` (GLSL only, unused by dispatch).
- `quadratic3(p0,p1,p2,t)` — uniform B-spline-ish quadratic.
- `blendBicubic(p0..p3,t)` — cubic B-spline basis (`/6.0`).
- `catmullRom3(p0,p1,p2,t)` and `catmullRom4(p0,p1,p2,p3,t)` — note `catmullRom3`'s last two terms
  have a deliberate `... 4*p2 - p0` / `... -3*p2 + p3 - p0` form that partially cancels (copied
  verbatim in both GLSL & WGSL — **replicate the literal expression, do not "simplify"**).
- `blendLinearOrCosine(a,b,amount,nType)`: `nType==1`→`mix(a,b,amount)` (linear),
  else `mix(a,b,smoothstep(0,1,amount))` (hermite/cosine).

### 4.5 `value()` dispatch by `NOISE_TYPE`
| NOISE_TYPE | branch |
|---|---|
| 0 | `constantFromLattice` (constant) |
| 1 | linear (`blendLinearOrCosine` nType=1) — uses 4-corner 2D bilerp |
| 2 | hermite (`smoothstep`) — same 4-corner path |
| 3 | `catmullRom3x3ValueNoise` |
| 4 | `catmullRom4x4ValueNoise` |
| 5 | `cubic3x3ValueNoise` |
| 6 | `bicubicValue` (4×4 B-spline) |
| 10 | `simplexValue` (default) |
| 11 | `sineNoise` |

GLSL gates each variant body in `#if NOISE_TYPE == N` so only the selected one compiles. WGSL keeps
all and uses runtime `if` (relying on const-folding). **HLSL: compile one variant.**

### 4.6 Simplex (Ashima 2D) — identical constants
`C = vec4(0.211324865405187, 0.366025403784439, -0.577350269189626, 0.024390243902439)`,
final scale `130.0`, gradient-norm `1.79284291400159 - 0.85373472095314*(a0²+h²)`. Result mapped
through `periodicFunction(map(v,-1,1,0,1)-blend)`. The WGSL writes the swizzle assignments
`x12.xy -= i1` as `x12 = vec4(x12.xy - i1, x12.zw)` and computes `g.yz` component-wise — semantically
identical.

### 4.7 sineNoise (NOISE_TYPE 11)
`r1 = prng(vec3(s))*0.75+0.125`, `r2 = prng(vec3(s+10))*0.75+0.125`. WGSL spells `vec3(s)` as
`vec3(s,s,s)`. Two nested `sin` sums, output `(x+y)*0.5+0.5`.

### 4.8 octave accumulation (`multires`)
```
for i in 1..=oct:
  multiplier = pow(2.0, i)
  baseFreq   = freq * 0.5 * multiplier
  multiplicand += 1/multiplier
  layer = generate_octave(st, baseFreq, s + 10*i, blend, i)   // r,g,b = value(s), value(s+10), value(s+20)
  color += layer / multiplier
color /= multiplicand
```
Then colorMode: `0` (mono) returns `vec3(color.b)`; `1` (rgb) returns color. `ridges` applies
`1 - abs(c*2-1)` per used channel. **Loop is 1-indexed, inclusive `<= oct`.** `pow(2.0,float(i))`
is `exp2` semantically; use `exp2((float)i)` in HLSL for exactness, or `pow(2.0, i)`.

### 4.9 `main()` flow (both backends)
1. `globalCoord = gl_FragCoord.xy + tileOffset` (GLSL) / `position.xy + tileOffset` (WGSL).
2. `st = globalCoord / fullResolution.y` — **divides by .y (height), not .x**; x is therefore in
   `[0, aspect]`.
3. `centered = st - vec2(aspectRatio*0.5, 0.5)`.
4. Compute `freq`/`lf` via `map(scale*, 1,100, …)` with per-NOISE_TYPE ranges:
   - type 11: `40→1` (freq), `10→1` (lf)
   - type 10: `6→0.5` (freq & lf)
   - else: `20→3` (freq), `12→3` (lf)
5. If `LOOP_OFFSET==300`: compute `base = map(75, …)` with the same per-type range, then
   `lf *= freq / vec2(base)` (nominalFreq normalization).
6. If `NOISE_TYPE != 4 && != 10 && wrap`: `freq = floor(freq)`, and if `LOOP_OFFSET==300`
   `lf = floor(lf)`.
7. `t = (speed < 0) ? time + offset(st,lf) : time - offset(st,lf)`.
8. `blend = periodicFunction(t) * abs(speed) * 0.01`.
9. `color.rgb = multires(centered, freq, octaves, float(seed), blend)`; alpha = 1.
- WGSL `offset()` takes an extra `pos` arg (used only for the `diamonds`/LOOP_OFFSET 410 path,
  which reads `pos / resolution.y`). GLSL `diamonds` reads global `globalCoord / fullResolution.y`
  — **subtle divergence: WGSL uses `resolution.y`, GLSL uses `fullResolution.y` for the diamonds
  offset** (hazard H2).

### 4.10 LOOP_OFFSET dispatch (values)
`10 circles, 20 triangle(shape sides=3), 30 diamond(|dx|+|dy|), 40 square(4), 50 pentagon(5),
60 hexagon(6), 70 heptagon(7), 80 octagon(8), 90 nonagon(9), 100 decagon(10), 110 hendecagon(11),
120 dodecagon(12), 200 horizontalScan(st.x), 210 verticalScan(st.y), 300 noise(value()),
400 rings(1-rings()), 410 sine(1-diamonds()), else 0`. `shape()` uses
`atan(st.x,st.y)` (GLSL `atan(y,x)` 2-arg) ⇄ WGSL `atan2(st.x,st.y)` — **arg order is (x,y) here,
matching `atan2(x,y)`; preserve exactly** (hazard H3).

---

## 5. cell (Voronoi) I/O & algorithm

**Inputs:** none (synth, generates from scratch). **Output:** `outputTex`, mono RGBA
(`color = vec3(d)`, base alpha 1; WGSL preserves initial `color.a` which is 1.0).
**Uniform layout:** §3. `metric` enum: `circle 0, diamond 1, hexagon 2, octagon 3, square 4,
triangle 6` (note **no value 5**).

Algorithm `cells(st,freq,cellSize,sides)`:
1. `st -= vec2(0.5*aspect,0.5); st *= freq; st += vec2(0.5*aspect,0.5); st += prng(vec3(seed)).xy;`
2. `i=floor(st); f=fract(st); d=1.0`.
3. Double loop `y,x ∈ [-2,2]` (5×5 neighborhood, inclusive):
   - `n=vec2(x,y); wrap=i+n; point=prng(vec3(wrap,seed)).xy;`
   - `r1 = prng(vec3(seed,wrap))*0.5 - 0.25; r2 = prng(vec3(wrap,seed))*2 - 1;`
   - `spd=floor(speed); point += vec2(sin(time*TAU*spd+r2.x)*r1.x, cos(time*TAU*spd+r2.y)*r1.y);`
   - `diff = n + point - f; dist = shape(vec2(diff.x,-diff.y), 0, sides, cellSize);`
   - if `metric==1` (diamond/Manhattan): `dist = (abs(n.x+point.x-f.x)+abs(n.y+point.y-f.y))*cellSize;`
   - `dist += r1.z * (variation*0.01); d = smin(d, dist, cellSmooth*0.01);`
4. Return `d`. `freq = map(scale,1,100,20,1)`, `cellSize = map(cellScale,1,100,3,0.75)`.
- `smin(a,b,k)`: `k==0 → min(a,b)`; else `h=max(k-|a-b|,0)/k; return min(a,b)-h*h*k*0.25;`
- `shape()`: type 0 circle `length(st*1.2)`; 2 hex `polarShape(st*1.2,6)`; 3 oct `*1.2,8`;
  4 square `*1.5,4`; 6 triangle `st.y+=0.05; polarShape(st*1.5,3)`. Types 1 & 5 fall through to
  `d=1.0` in `shape` (but metric 1 is handled before `shape` is used for distance).
- **Note `prng(vec3(seed,wrap))` vs `prng(vec3(wrap,seed))` — argument ORDER differs between r1 and
  point/r2; preserve exactly** (hazard H4). GLSL builds `vec3(float(seed), wrap)` (scalar then
  vec2) and `vec3(wrap, float(seed))`; WGSL `vec3<f32>(f32(seed), wrap)` etc. Component order is
  `(seed, wrap.x, wrap.y)` vs `(wrap.x, wrap.y, seed)`.

---

## 6. blur I/O & algorithm

Two-pass separable Gaussian. **blurH:** in `inputTex` → out `_blurTemp`. **blurV:** in `_blurTemp`
→ out `outputTex`. Uniforms `radiusX`/`radiusY` (float, default 5, range 0–50, step 1).

Per pass:
```
texSize = textureSize(inputTex,0)        // ivec2 / textureDimensions
uv = gl_FragCoord.xy / texSize           // pos.xy / texSize  (NO tileOffset here)
texelSize = 1/texSize
radius = int(radiusX * renderScale)      // GLSL;  WGSL: i32(radiusX)  ← renderScale OMITTED
if radius<=0: return texture(inputTex,uv)
sigma = radius/3.0; sigma2 = sigma²
for i in [-radius,radius]:
  w = exp(-(i²)/(2*sigma2)); sum += sample(uv + i*texelStep)*w; weightSum += w
return sum/weightSum
```
H-pass offsets along x (`texelSize.x,0`), V-pass along y. `uv` uses raw `gl_FragCoord/texSize`
(divides by the **input texture** size, not fullResolution).

---

## 7. blend (`blendMode`) I/O & algorithm

**Inputs:** `inputTex` (base, `color1`) and `tex` (`color2`, a user surface; global `tex` is
`type:"surface", default:"none"`). **Output:** `outputTex`. Uniforms: `mode:int` (16 modes),
`mixAmt:float` (range −100..100, aliased from `mix`).

Sampling: `color1 = sample(inputTex, gl_FragCoord.xy/textureSize(inputTex))`,
`color2 = sample(tex, …/textureSize(tex))`. WGSL uses `position.xy/textureDimensions(inputTex)` for
**both** (single `dims`) — assumes equal sizes.

Mode enum & formulas (`applyBlendMode`, operating on vec4 unless noted): `0 add` `min(c1+c2,1)`;
`1 burn` `1-min((1-c1)/max(c2,0.001),1)`; `2 darken` `min`; `3 diff` `|c1-c2|`; `4 dodge`
`min(c1/max(1-c2,0.001),1)`; `5 exclusion` `c1+c2-2*c1*c2`; `6 hardLight` per-rgb
`blendOverlay(c2,c1)` (a=1); `7 lighten` `max`; `8 mix` `(c1+c2)*0.5`; `9 multiply` `c1*c2`;
`10 negation` `1-|1-c1-c2|`; `11 overlay` per-rgb `blendOverlay(c1,c2)` (a=1); `12 phoenix`
`min-max+1`; `13 screen` `1-(1-c1)(1-c2)`; `14 softLight` per-rgb `blendSoftLight(c1,c2)` (a=1);
`15 subtract` `max(c1-c2,0)`.
- `blendOverlay(a,b)= a<0.5 ? 2ab : 1-2(1-a)(1-b)`.
- `blendSoftLight(base,blend)= blend<0.5 ? 2·base·blend+base²(1-2blend) : sqrt(base)(2blend-1)+2base(1-blend)`.

Final composite:
```
amt = map(mixAmt, -100,100, 0,1)              // → [0,1]
if amt<0.5: color = mix(color1, middle, amt*2)
else:       color = mix(middle, color2, (amt-0.5)*2)
color.rgb = mix(color1.rgb, color.rgb, color2.a)      // Porter-Duff "over"
color.a   = color2.a*amt + color1.a*(1 - color2.a*amt)
```
WGSL is algebraically identical (`alphaFactor = color2.a*amt`). **Do NOT fold `amt` into the
Porter-Duff RGB factor** — it's already applied in the mix branch.

---

## 8. GLSL/WGSL → HLSL Translation Rulebook

| Concept | GLSL (ES 3.00) | WGSL | HLSL (Unity) | Notes |
|---|---|---|---|---|
| Precision | `precision highp float/int;` | implicit f32/i32 | `float`/`int` (32-bit) | Force highp everywhere; **avoid `half`/`min16float`** (parity H5). |
| Vector | `vec2/3/4`, `ivec*`, `uvec*` | `vec2<f32>` etc. | `float2/3/4`, `int2`, `uint2` | |
| Construct | `vec3(x)` | `vec3<f32>(x)` | `float3(x,x,x)` (splat OK) | |
| `mix(a,b,t)` | linear lerp | `mix` | `lerp(a,b,t)` | |
| `fract(x)` | | `fract` | `frac(x)` | both = `x-floor(x)`; matches. |
| `mod(a,b)` (float) | `a-b*floor(a/b)` | helper `modulo()` | `a - b*floor(a/b)` — **do NOT use HLSL `fmod`** (truncated, wrong sign) (H6). |
| int `%` | trunc toward 0 | trunc toward 0 | `%` trunc toward 0 | matches; `positiveModulo` adds `+modulus`. |
| `floor/ceil/abs/min/max/clamp/pow/exp/sin/cos/sqrt/length/dot` | same | same | same names | |
| `atan(y,x)` 2-arg | `atan(a,b)` | `atan2(a,b)` | `atan2(a,b)` | **arg order in source is (x,y)** — copy literally. |
| `smoothstep(e0,e1,x)` | | `smoothstep` | `smoothstep` | identical cubic. |
| `exp2`/`pow(2,n)` | `pow(2.0,float(i))` | `pow(2.0,f32(i))` | `exp2((float)i)` or `pow` | |
| float→uint cast | `uint(f)` / `uvec3(p)` | `u32(f)` / `vec3<u32>(p)` | `(uint)f` **truncation** | NOT `asuint`. |
| int→uint reinterpret | `uint(i)` (i may be neg) | `u32(i)` | `(uint)i` (two's comp) | bit pattern preserved. |
| float bits→uint | `floatBitsToUint(f)` | `bitcast<u32>(f)` | `asuint(f)` | **bit reinterpret**, used for `sFrac` jitter. |
| uint bits→float | `uintBitsToFloat` | `bitcast<f32>` | `asfloat` | (not used here, but for completeness). |
| uint→float numeric | `float(u)` | `f32(u)` | `(float)u` | round-to-nearest. |
| uint literals | `1664525u`, `0x9E3779B9u` | same | `1664525u`, `0x9E3779B9u` | unsigned 32-bit wrap. HLSL uint arithmetic wraps mod 2³². |
| `>>` on uvec | `v >> uint(16)` | `v >> vec3<u32>(16u)` | `v >> 16u` | logical shift (unsigned). |
| Ternary / select | `c ? a : b` | `select(b,a,c)` (**note arg order!**) | `c ? a : b` | WGSL `select(falseVal,trueVal,cond)`. |
| Texture decl | `uniform sampler2D t;` | `texture_2d<f32>`+`sampler` | `Texture2D t; SamplerState s;` | |
| Texture sample | `texture(t,uv)` | `textureSample(t,s,uv)` | `t.Sample(s,uv)` | wrap/filter set by sampler (H7). |
| Texture size | `textureSize(t,0)`→ivec2 | `textureDimensions(t)` | `t.GetDimensions(w,h)` | |
| Frag coord | `gl_FragCoord.xy` (pixel center, **bottom-left origin in GL**) | `position.xy` (**top-left origin**) | `SV_Position.xy` (top-left, pixel-center +0.5) | **CRITICAL Y-FLIP** (H8). |
| Output | `out vec4 fragColor;` | `@location(0) vec4<f32>` return | `SV_Target` return | |
| `#define` injection | prepended by `injectDefines` | `const` injected | `#pragma`/`#define` per variant | §2. |
| Struct member sep | `;` | `,` (comma) | `;` | |

### 8.1 Per-component select pitfall
GLSL `(cond) ? vecA : vecB` requires scalar cond; the noise code uses scalar ternaries in `prng`.
The WGSL `select(falseVal, trueVal, cond)` has **reversed operand order** vs C ternary — verify
your HLSL `cond ? a : b` maps to the *true* branch correctly (in `prng`: `p>=0 ? p*2 : -p*2+1`).

### 8.2 Loop bounds
All loops are inclusive (`i <= radius`, `i <= oct`, `y/x <= 2`) and **fixed/constant after the
NOISE_TYPE/radius is known**. HLSL needs `[loop]`/`[unroll]` decisions; for radius-driven blur use
`[loop]` (dynamic). Loop index types: blur uses `int i` from `-radius`; octave `int i` 1-based.

---

## 9. PARITY HAZARDS (bit-for-bit risks)

- **H1 — `renderScale` divergence (blur):** GLSL multiplies `radius = int(radiusX*renderScale)`;
  WGSL uses `i32(radiusX)` with **no renderScale**. At `renderScale==1` they agree; otherwise they
  diverge. Decide which is canonical for Unity (recommend matching GLSL: multiply by your
  render-scale factor) and document.
- **H2 — diamonds offset denominator:** GLSL `diamonds` uses `fullResolution.y`; WGSL uses
  `resolution.y`. Only affects LOOP_OFFSET 410. Pick fullResolution for tiled-render correctness.
- **H3 — `atan2` argument order:** `shape()` calls `atan(st.x, st.y)` (GLSL `atan(y,x)` form but
  passing x first). Reproduce as `atan2(st.x, st.y)` exactly; swapping rotates all polygon shapes.
- **H4 — PRNG argument ordering in cell:** `r1=prng(vec3(seed,wrap))` vs
  `point/r2=prng(vec3(wrap,seed))`. The component order into PCG changes the hash. Replicate the
  exact ordering `(seed,wrap.x,wrap.y)` vs `(wrap.x,wrap.y,seed)`.
- **H5 — float precision:** every shader forces `highp`/f32. Use full 32-bit `float` in HLSL; do
  **not** let Unity downgrade to `half`. The PCG hash and `asuint(fract(s))` are bit-sensitive.
- **H6 — modulo sign:** use `a - b*floor(a/b)` for float mod, never `fmod`. Int `%` matches GLSL
  (trunc) but `positiveModulo` must add `+modulus` for negatives.
- **H7 — texture filtering / wrap / sRGB:** blur & blend sample with the runtime's default sampler.
  noise/cell do not sample. Use **bilinear, clamp-to-edge (or repeat where tiling intended), and
  LINEAR (non-sRGB) color** to match WebGL RGBA8 unorm with no sRGB-decode. If Unity applies sRGB
  read/write conversions, output will differ — render targets must be **linear/UNORM, not sRGB**.
- **H8 — coordinate origin / Y-flip:** GLSL `gl_FragCoord` is **bottom-left** origin; WGSL & HLSL
  `SV_Position` are **top-left**. Noisemaker's canonical output is the WebGL2 (bottom-left) image
  *as displayed*; the WebGPU port already compensates so the two match on screen. In Unity/HLSL
  (top-left, and Unity flips render-target V on some platforms), you must reconcile `st = (coord +
  tileOffset)/fullResolution.y` so the image is not vertically mirrored. Validate against a
  reference frame. Pixel-center is `+0.5` in all three.
- **H9 — `aspectRatio` macro substitution (GLSL):** `#define aspectRatio fullResolution.x /
  fullResolution.y` is textual; in expressions like `0.5 * aspectRatio` it expands to
  `0.5 * fullResolution.x / fullResolution.y` which (left-to-right) equals
  `(0.5*fullResolution.x)/fullResolution.y` — same value, but be careful if you parenthesize
  differently. Use a precomputed float.
- **H10 — evaluation/rounding order:** keep arithmetic associativity identical (e.g. the verbatim
  `catmullRom3` redundant terms, `multires` `color += layer/multiplier` then `/= multiplicand`).
  Reordering FMA/fold can change the last ULP. Compile without fast-math reassociation if exactness
  matters.
- **H11 — uint→float divisor:** divide PCG output by `4294967295.0` (`float(0xffffffffu)`), matching
  both backends. Do not use `2^32`.
- **H12 — bool packing:** booleans arrive as floats compared `> 0.5` in WGSL; GLSL receives true
  `bool` uniforms. In HLSL pack as float and test `> 0.5` to match WGSL exactly.
- **H13 — st divides by HEIGHT:** `st = coord / fullResolution.y` (not width). X spans `[0,aspect]`.
  A naive `coord/resolution` (both axes) breaks all aspect-dependent math.

---

## 10. Open questions / cross-subsystem dependencies

1. **Source of `renderScale`, `tileOffset`, `fullResolution`, `resolution`:** these are injected by
   the renderer/pipeline (canvas.js / backends), not by the effect definition. The HLSL port needs
   the tiling subsystem spec (separate reference) to know how `tileOffset`/`fullResolution` are set
   per tile and how `renderScale` is derived.
2. **WGSL `uniformLayout` packing vs WebGL2 individual uniforms:** confirm the JS host packs floats
   for *all* numeric uniforms (including ints/bools) into `data[]` for WGPU; the GLSL path sets
   typed uniforms. Verify int/bool conversion (`i32(data[..])`, `data[..]>0.5`) is the canonical
   contract for HLSL cbuffers.
3. **Default NOISE_TYPE/LOOP_OFFSET when define omitted:** GLSL fallback is 10/300; confirm the
   expander always injects, so HLSL variants must cover at least {0,1,2,3,4,5,6,10,11} ×
   {10,20,...,410, none}.
4. **`surface`-typed globals (blend `tex`):** how `"none"` resolves (likely a black/transparent
   texture) must come from the surface-binding subsystem.
5. **`tex` size mismatch in blend:** WGSL samples both textures with `inputTex` dimensions; GLSL
   uses each texture's own size. If `inputTex` and `tex` differ in size, the two backends diverge —
   clarify the canonical behavior before porting.
6. **`paramAliases` resolution timing** and DSL→uniform mapping live in the DSL/expander subsystem.
