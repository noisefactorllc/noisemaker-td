# 08 — Shared Math Primitives (Bit-Exact Parity Reference)

Scope: every reusable math primitive embedded in Noisemaker shader effects
(`shaders/effects/**/glsl/*.glsl` and `**/wgsl/*.wgsl`) that must be reproduced
bit-for-bit by an HLSL/Unity re-implementation. Covers PRNG/hash, noise bases,
interpolation, color-space conversions, distance metrics, blend modes,
rotate/kaleidoscope helpers.

---

## 0. CRITICAL STRUCTURAL FACT — Primitives Are COPY-PASTED, Not Shared

There is **no shader include / `#include` / chunk-injection system**. The
runtime compiler (`shaders/src/runtime/compiler.js`) does **not** inject helper
libraries; its only "hash" is an unrelated 32-bit source-cache key
(`hashSource`). The only `_shared` directory is
`shaders/effects/filter/_shared/{glsl,wgsl}/overlayBlend.{glsl,wgsl}` and it is
used by exactly the fibers/worm filters, not globally.

Consequence: **each effect file embeds its own private copy** of `pcg`, `prng`,
`hsv2rgb`, `rgb2hsv`, oklab matrices, `rotate2D`, etc. Measured counts:

- `pcg` defined in **76** files (35 WGSL `fn pcg`, 41 GLSL `uvec3 pcg`).
- `prng` in ~27 files; `hsv2rgb` in 27; `rgb2hsv` in 23; `rotate2D` in 21.
- `hsv2rgb` has **7 distinct** whitespace-normalized variants (the dominant
  branchy MOD6 one in 16 files; an iq branchless `K`-vector one elsewhere).
- `rotate2D` has **11+ distinct** bodies with **different angle conventions**.

**Parity strategy for the port:** Do NOT assume a single canonical version.
Port each primitive *per call-site family*. This document catalogs the distinct
variants so you can match each effect to the exact one it uses. When a primitive
has multiple variants, the variant choice changes pixels.

### Coordinate origin / Y-flip (applies to ALL primitives that take `st`)

- GLSL fragment shaders read `gl_FragCoord.xy` (origin **bottom-left**, pixel
  centers at +0.5). WGSL reads `@builtin(position).xy` (origin **top-left**).
- Most effects compute `globalCoord = gl_FragCoord.xy + tileOffset` (GLSL) and
  `position.xy + tileOffset` (WGSL), then `st = globalCoord / fullResolution.y`
  (note: divided by **height**, giving `aspectRatio`-wide X in `[0, w/h]`).
- The WebGL2 backend uses `gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, flipY)` with
  **`flipY = true` by default** for texture uploads
  (`shaders/src/runtime/backends/webgl2.js` ~L429/478). The WebGPU backend has
  the same `flipY` default and "internal blits now handle Y-flip"
  (`webgpu.js` ~L3245). **HLSL/Unity has top-left texture origin like WGSL but
  D3D-style; you MUST decide a single origin and apply the same per-pass Y
  handling the two backends already reconcile, or noise lattices and any
  `gl_FragCoord`-derived term will be mirrored vertically.** This is the single
  largest cross-backend hazard. The two existing backends already agree
  pixel-wise, so use the WGSL math (top-left position) as the porting reference
  and add a Y-flip only where the WebGPU backend's blit does.

---

## 1. PRNG / HASH

### 1.1 `pcg` — PCG 3D hash (THE universal PRNG)

Source: riccardoscalco/glsl-pcg-prng (MIT). Algorithmically **identical in all
76 copies** (GLSL/WGSL differ only in whitespace and `uint(1664525)` vs
`1664525u`, which are semantically equal in GLSL ES 3.00).

GLSL (canonical, `shaders/effects/synth/noise/glsl/noise.glsl` L49):
```glsl
uvec3 pcg(uvec3 v) {
    v = v * uint(1664525) + uint(1013904223);
    v.x += v.y * v.z;
    v.y += v.z * v.x;
    v.z += v.x * v.y;
    v ^= v >> uint(16);
    v.x += v.y * v.z;
    v.y += v.z * v.x;
    v.z += v.x * v.y;
    return v;
}
```
WGSL (`synth/noise/wgsl/noise.wgsl` L42):
```wgsl
fn pcg(v_in: vec3<u32>) -> vec3<u32> {
    var v = v_in * 1664525u + 1013904223u;
    v.x += v.y * v.z; v.y += v.z * v.x; v.z += v.x * v.y;
    v = v ^ (v >> vec3<u32>(16u));
    v.x += v.y * v.z; v.y += v.z * v.x; v.z += v.x * v.y;
    return v;
}
```
Magic numbers: multiplier `1664525`, increment `1013904223`, right-shift `16`.

**HLSL port:** use `uint3`. All arithmetic is **modulo-2^32 unsigned wraparound**
— HLSL `uint` already wraps identically. `v.x += v.y * v.z` etc. are sequential
mutations of the same vector; **evaluation order matters** — `v.x` uses the
*already-updated* multiply but the *old* y,z within the same statement group;
then `v.y` uses the *new* `v.x`. Reproduce the three statements in exact order,
do NOT vectorize them into one SIMD op.

**Parity hazards:**
- `v >> 16u` is a **logical** (unsigned) shift. HLSL `>>` on `uint` is logical —
  OK. Never use `int`.
- `uint(p)` / `vec3<u32>(p)` casting a `float` to `uint`: WGSL/GLSL truncate
  toward zero; **negative floats produce implementation-defined values**
  (often wrap to large uint). HLSL `(uint)f` for negative `f` is also UB-ish but
  in practice both ANGLE/Tint and FXC produce `f - floor(f/2^32)*2^32`-style
  results. Because `prng` typically pre-folds negatives (see 1.2), the common
  path passes non-negative floats; but variants that DON'T fold (subdivide,
  zoomBlur, colorLab, shapeMixer) can pass negatives — **match the cast behavior
  exactly** by replicating the same fold/no-fold per file.

### 1.2 `prng` — float wrapper around `pcg` (MULTIPLE VARIANTS — HAZARD)

Maps `vec3 p -> vec3 in [0,1)` via `pcg`. **At least 5 distinct variants.** The
divisor is always `0xffffffff` (= 4294967295), written either `float(uint(0xffffffff))`
or `float(0xffffffffu)` (identical). The **difference that changes pixels** is
whether the negative-fold transform is applied:

Fold transform (per component): `p = p >= 0 ? p*2 : -p*2 + 1`. This maps the
integer lattice so negative coords don't collide with positive ones after the
truncating uint cast.

Variant A — **with fold** (most noise effects: noise, gradient, gabor, cell,
caustic, splat, perlin):
```glsl
vec3 prng(vec3 p) {
    p.x = p.x >= 0.0 ? p.x * 2.0 : -p.x * 2.0 + 1.0;
    p.y = p.y >= 0.0 ? p.y * 2.0 : -p.y * 2.0 + 1.0;
    p.z = p.z >= 0.0 ? p.z * 2.0 : -p.z * 2.0 + 1.0;
    return vec3(pcg(uvec3(p))) / float(uint(0xffffffff));
}
```
Variant B — **no fold** (subdivide, zoomBlur, colorLab, shapeMixer):
```glsl
vec3 prng(vec3 p) { return vec3(pcg(uvec3(p))) / float(0xffffffffu); }
```
Variant B' — subdivide casts each component explicitly:
`pcg(uvec3(uint(p.x), uint(p.y), uint(p.z)))` (same result as `uvec3(p)`).

WGSL note — `synth/perlin/wgsl/perlin.wgsl` divides by `f32(0xffffffff)`
**without the `u` suffix**. In WGSL an unsuffixed `0xffffffff` is an
`abstract-int` that converts to `f32` as `4294967295.0` — same value, but flag
it: if a tool ever treated it as `i32` it'd be `-1`. Treat divisor as
`4294967295.0` everywhere.

WGSL noise3d variant flips the predicate: `select(q*2, -q*2+1, q < 0.0)`.
`q >= 0 ? q*2` and `q < 0 ? ...:q*2` are equivalent at the boundary `q == 0`
(both give `0`). Safe.

**`random(vec2 st)` helper** also differs: noise uses `prng(vec3(st, 0.0)).x`;
colorLab uses `prng(vec3(st, 1.0)).x` (z = 1.0, not 0.0!) — different stream.

**HLSL port:** Match fold/no-fold and the `z` seed (0.0 vs 1.0) **per effect**.

### 1.3 Per-lattice jitter hash (inside value noise) — HAZARD

Inside `constantFromLatticeWithOffset` (see §2.1), a secondary hash mixes a
`floatBitsToUint` of the seed fractional part:
```glsl
uint fracBits = floatBitsToUint(sFrac);
uvec3 jitter = uvec3(
    (fracBits * 374761393u) ^ 0x9E3779B9u,
    (fracBits * 668265263u) ^ 0x7F4A7C15u,
    (fracBits * 2246822519u) ^ 0x94D049B4u);
uvec3 state = uvec3(xBits, yBits, seedBits) ^ jitter;
uvec3 prngState = pcg(state);
float noiseValue = float(prngState.x) / float(0xffffffffu);
```
WGSL uses `bitcast<u32>(sFrac)` (identical to `floatBitsToUint`). Magic
constants: `374761393`, `668265263`, `2246822519` (multipliers);
`0x9E3779B9`, `0x7F4A7C15`, `0x94D049B4` (XOR — note `0x9E3779B9` is the golden
ratio constant). **HLSL:** `asuint(sFrac)` for the float→bits reinterpret;
multiplications wrap mod-2^32.

**DIVERGENCE — `bitEffects.glsl` (classicNoisedeck/bitEffects) uses a DIFFERENT
seed-bits source:** `uint seedBits = floatBitsToUint(s);` and
`uint fracBits = floatBitsToUint(seedFrac);` — i.e. it bit-reinterprets the
whole seed float, whereas `synth/noise` uses `uint seedBits = uint(seedInt)`
(integer truncation). These produce **completely different noise** for the same
seed. Port each effect's exact expression.

### 1.4 Other named hashes (single-use, low priority)

`hash`, `hash2`, `hash3`, `hash21`, `hash31`, `hash33`, `hash_uint`, `hash_mix`,
`random_scalar`, `rand` appear 1–15× but are localized to individual effects
(glitch, lowPoly, fibers, reaction-diffusion, etc.). Port them verbatim from the
specific effect file when you implement that effect; they are not cross-effect
shared. `hash_uint` (12 uses) is typically a single-output PCG-style integer
hash used by agent/points effects.

---

## 2. NOISE BASES

The canonical generator is `shaders/effects/synth/noise/{glsl,wgsl}` (VNoise).
GLSL uses **compile-time `#if NOISE_TYPE`** dispatch; WGSL uses runtime `if` on
a const `NOISE_TYPE` (dead-code-eliminated). Both inject `NOISE_TYPE` and
`LOOP_OFFSET` defines from `definition.js` (`globals.type.define`). Defaults:
`NOISE_TYPE 10` (simplex), `LOOP_OFFSET 300` (value-noise offset).

**NOISE_TYPE enum:** `0` constant, `1` linear, `2` hermite/cosine (smoothstep),
`3` catmullRom3x3, `4` catmullRom4x4, `5` cubic3x3, `6` bicubic, `10` simplex,
`11` sineNoise. (Note 1 vs 2 share a branch differing only via
`blendLinearOrCosine(...,NOISE_TYPE)`: `nType==1 → mix(a,b,amount)` linear, else
`mix(a,b,smoothstep(0,1,amount))`.)

### 2.1 Value noise lattice (`constantFromLatticeWithOffset`)

```glsl
float constantFromLatticeWithOffset(vec2 lattice, vec2 freq, float s,
                                    float blend, ivec2 offset) {
    vec2 baseFloor = floor(lattice);
    ivec2 base = ivec2(baseFloor) + offset;
    vec2 frac = lattice - baseFloor;
    int seedInt = int(floor(s));
    float sFrac = fract(s);
    float xCombined = frac.x + sFrac;
    int xi = base.x + int(floor(xCombined));
    int yi = base.y;
    if (wrap) {                               // tiling
        int freqX = int(freq.x + 0.5);
        int freqY = int(freq.y + 0.5);
        if (freqX > 0) xi = positiveModulo(xi, freqX);
        if (freqY > 0) yi = positiveModulo(yi, freqY);
    }
    // ... §1.3 jitter hash → noiseValue ...
    return periodicFunction(noiseValue - blend);
}
```
`positiveModulo(v,m)`: `r=v%m; return r<0? r+m : r;` (m==0 → 0). HLSL `%` on
`int` matches C truncated modulo, so this fix-up is needed identically.
`periodicFunction(p) = map(cos(p*TAU), -1, 1, 0, 1)` (GLSL) — **but WGSL uses
the same cos form**; note `colorLab` defines `periodicFunction` with `sin`
instead (`map(sin(TAU*p),-1,1,0,1)`) — different effect, different function.
`map(v,a,b,c,d) = c + (d-c)*(v-a)/(b-a)`.

**Hazards:** `floor`, `fract`, `int(floor())` truncation; `int(freq.x+0.5)`
rounding. HLSL `frac`/`floor` match GLSL for finite positive; for negatives GLSL
`fract(x)=x-floor(x)` is always in [0,1) — HLSL `frac` is the same. Good.

### 2.2 Interpolators used by value noise

- `quadratic3(p0,p1,p2,t)` (type 5 cubic): exact polynomial in file.
- `blendBicubic(p0,p1,p2,p3,t)` (type 6): B-spline basis /6.
- `catmullRom3(p0,p1,p2,t)` and `catmullRom4(p0,p1,p2,p3,t)`: **note
  `catmullRom3` has an apparent typo** — `2.0*p0 - 5.0*p1 + 4.0*p2 - p0` (the
  `-p0` cancels a `p0`) and `-p0 + 3p1 - 3p2 + p0`. This is **intentional to
  reproduce — replicate the algebra literally**, GLSL and WGSL both have it.
- Reconstruction order: 3×3 / 4×4 variants interpolate rows in X (`f.x`) then
  combine in Y (`f.y`). Evaluation order is fixed; keep it.

### 2.3 Simplex 2D (NOISE_TYPE 10, default)

Ashima Arts simplex (MIT). Constants:
`C = vec4(0.211324865405187, 0.366025403784439, -0.577350269189626, 0.024390243902439)`,
gradient normalize `1.79284291400159 - 0.85373472095314*(a0*a0+h*h)`, final
scale `130.0`. `mod289` and `permute` use `34.0`, `289.0`.
**Hazard:** WGSL writes `x12` reassembly as
`x12 = vec4<f32>(x12.xy - i1, x12.zw)` and computes `g.y/g.z` componentwise
(no swizzle assign) — algebraically identical to GLSL `g.yz = a0.yz*x12.xz + ...`.
Output wrapped through `periodicFunction(map(v,-1,1,0,1) - blend)`.

### 2.4 sineNoise (NOISE_TYPE 11) and `sineNoise` randoms

Uses `prng(vec3(s))*0.75 + 0.125` for r1, `prng(vec3(s+10.0))*...` for r2. WGSL
passes `vec3<f32>(s,s,s)` explicitly (same as GLSL `vec3(s)`).

### 2.5 Worley / cell noise (`synth/cell`)

5×5 neighborhood `for y in [-2,2], x in [-2,2]`. Per-cell point from
`prng(vec3(wrap, seed)).xy`, animated by `sin/cos(time*TAU*floor(speed)+r2)*r1`.
**Metric enum here:** `metric == 1` → Manhattan
(`abs(dx)+abs(dy)` scaled by cellSize); otherwise a polar **shape** distance
(circle/hex/oct/square/triangle via `polarShape`). Cells combined with
`smin(d,dist,cellSmooth*0.01)` (iq polynomial smin: `h=max(k-abs(a-b),0)/k;
return min(a,b) - h*h*k*0.25;`, k==0 → plain `min`). **NOTE the per-effect
metric enum is NOT the same as §5.**

---

## 3. INTERPOLATION CATALOG (cross-effect)

| name | signature | semantics |
|------|-----------|-----------|
| `catmullRom3` | `(p0,p1,p2,t)` | 3-pt CR with the duplicated-`p0` algebra (§2.2) |
| `catmullRom4` | `(p0,p1,p2,p3,t)` | Horner-form CR spline |
| `catmullRom3x3`/`4x4` | `(...,t)` value-noise grid combiners |
| `blendBicubic` | `(p0,p1,p2,p3,t)` | uniform cubic B-spline /6 |
| `quadratic3` | `(p0,p1,p2,t)` | quadratic B-spline |
| `cosineMix` | `(a,b,t)` | `mix(a,b,smoothstep(0,1,t))`-style |
| `blendLinearOrCosine` | `(a,b,amt,nType)` | linear if nType==1 else smoothstep |
| `cubic(t)` | `t*t*(3-2t)` | smoothstep kernel |
| `mix`/`lerp` | builtin | `a+(b-a)*t` |

**Hazard:** GLSL `smoothstep(0,1,x)` clamps x to [0,1] then `3x²-2x³`. HLSL
`smoothstep(0,1,x)` is identical. `mix(a,b,t)` = HLSL `lerp(a,b,t)` = `a*(1-t)+b*t`
in HLSL but **GLSL spec is `a*(1-t)+b*t` too** — match. Some drivers compute
`mix` as `a+(b-a)*t`; with FMA this differs in the last bit. For *true* bit
parity, pick `a+(b-a)*t` and disable FMA contraction (HLSL `precise` or
`#pragma fxc` no-fma) — see §7.

---

## 4. COLOR-SPACE CONVERSIONS

### 4.1 hsv2rgb / rgb2hsv — TWO FAMILIES (HAZARD)

**Family 1 — branchy MOD6** (colorLab, bitEffects, shapeMixer, cellNoise, most
classicNoisedeck; 16 files share the exact body). `hsv2rgb`:
```glsl
vec3 hsv2rgb(vec3 hsv) {
    float h = fract(hsv.x); float s = hsv.y; float v = hsv.z;
    float c = v * s;
    float x = c * (1.0 - abs(mod(h * 6.0, 2.0) - 1.0));
    float m = v - c;
    // 6-way if/else on h sextants → rgb; default vec3(0)
    return rgb + vec3(m,m,m);
}
```
`rgb2hsv` (family 1):
```glsl
float maxc=max(r,max(g,b)); float minc=min(r,min(g,b)); float delta=maxc-minc;
float h=0.0;
if (delta!=0.0){
  if (maxc==r) h = mod((g-b)/delta, 6.0)/6.0;
  else if (maxc==g) h = ((b-r)/delta+2.0)/6.0;
  else h = ((r-g)/delta+4.0)/6.0;
}
float s = (maxc==0.0)?0.0:delta/maxc; float v=maxc; return vec3(h,s,v);
```
**WGSL family-1 divergence:** WGSL lacks GLSL `mod` (always-positive remainder).
It uses `%` (sign-of-dividend) then compensates: `h = ((g-b)/delta) % 6.0 / 6.0;
... if (h < 0.0) { h = h + 1.0; }`. `hsv2rgb` WGSL uses `(h*6.0) % 2.0`.
**HLSL `%` on float follows sign-of-dividend like WGSL.** To match GLSL family-1
you must emulate `mod(a,b)=a-b*floor(a/b)` (always positive). Use the WGSL form
(`fmod` + `if (h<0) h+=1`) as the reference since it is the validated one.

**Family 2 — iq branchless `K`-vector** (mixer/applyMode, filter/adjust,
filter/chroma):
```glsl
vec3 rgb2hsv(vec3 c){
  vec4 K=vec4(0.0,-1.0/3.0,2.0/3.0,-1.0);
  vec4 p=mix(vec4(c.bg,K.wz),vec4(c.gb,K.xy),step(c.b,c.g));
  vec4 q=mix(vec4(p.xyw,c.r),vec4(c.r,p.yzx),step(p.x,c.r));
  float d=q.x-min(q.w,q.y); float e=1.0e-10;
  return vec3(abs(q.z+(q.w-q.y)/(6.0*d+e)), d/(q.x+e), q.x);
}
vec3 hsv2rgb(vec3 c){
  vec4 K=vec4(1.0,2.0/3.0,1.0/3.0,3.0);
  vec3 p=abs(fract(c.xxx+K.xyz)*6.0-K.www);
  return c.z*mix(K.xxx,clamp(p-K.xxx,0.0,1.0),c.y);
}
```
Families 1 and 2 are **NOT bit-identical** (different hue at desaturated/edge
colors; family 2 has the `e=1e-10` epsilon, no exact branch). **Match the family
per effect.**

### 4.2 sRGB ↔ linear (standard IEC 61966-2-1)

```glsl
vec3 linearToSrgb(vec3 l){ per-component:
  l<=0.0031308 ? l*12.92 : 1.055*pow(l,1.0/2.4)-0.055; }
vec3 srgbToLinear(vec3 s){ per-component:
  s<=0.04045 ? s/12.92 : pow((s+0.055)/1.055, 2.4); }
```
Constants: `0.0031308`, `12.92`, `1.055`, `1/2.4`, `0.055`, `0.04045`, `2.4`.
**Hazard:** GLSL iterates `for i in 0..3 { color[i] ... }`. HLSL: do
per-component, identical thresholds. `pow(x, 1.0/2.4)`: `1.0/2.4` should be
precomputed the same way (`0.41666...`); FXC may fold differently — use the
literal `1.0 / 2.4` expression, not a baked constant.

### 4.3 OKLab (Björn Ottosson, MIT)

Forward (oklab→linear-srgb) and inverse, via 4 const 3×3 matrices. **All
matrices are stored COLUMN-MAJOR in both backends** — GLSL `mat3(a,b,c, d,e,f,
g,h,i)` fills column0=(a,b,c); WGSL `mat3x3f(vec3f(...),vec3f(...),vec3f(...))`
each `vec3f` is a column. Both backends use **`M * c`** (matrix-times-column-
vector). Values (GLSL `colorLab.glsl` L199-213):
```
fwdA = mat3(1,1,1,  0.3963377774,-0.1055613458,-0.0894841775,
            0.2158037573,-0.0638541728,-1.2914855480)
fwdB = mat3(4.0767245293,-1.2681437731,-0.0041119885,
            -3.3072168827,2.6093323231,-0.7034763098,
            0.2307590544,-0.3411344290,1.7068625689)
invB = mat3(0.4121656120,0.2118591070,0.0883097947,
            0.5362752080,0.6807189584,0.2818474174,
            0.0514575653,0.1074065790,0.6302613616)
invA = mat3(0.2104542553,1.9779984951,0.0259040371,
            0.7936177850,-2.4285922050,0.7827717662,
            -0.0040720468,0.4505937099,-0.8086757660)
oklab_from_linear_srgb(c){ lms=invB*c; return invA*(sign(lms)*pow(abs(lms),vec3(0.3333333333333))); }
linear_srgb_from_oklab(c){ lms=fwdA*c; return fwdB*(lms*lms*lms); }
```
**HLSL HAZARD (matrix layout):** HLSL `float3x3` is **row-major by default** and
`mul(M, v)` treats `M` rows as the dot-product partners — the **opposite**
convention from GLSL/WGSL column-major `M*v`. To reproduce `M*c` where the
constants are GLSL column-major, in HLSL you must either (a) transpose the
constant matrix when declaring it and use `mul(v, M)`, or (b) declare the matrix
with the same column layout and use `mul(M, v)` while compiling with
`#pragma pack_matrix(column_major)`. **Get this wrong and oklab output is
silently transposed.** Verify against the GLSL by computing one known triple.

**Cube-root precision HAZARD:** GLSL uses exponent `0.3333333333333` (13 threes);
the WGSL port (`shapeMixer.wgsl`) uses `0.333333` (6 threes). These differ in the
last ~1e-7 and `pow` amplifies it. **Pick the GLSL 13-digit value** (it is the
WebGL reference path) and use it everywhere; do not use `pow(x, 1.0/3.0)`.
`sign(lms)*pow(abs(lms),k)` is the cube-root-preserving-sign trick — HLSL `sign`
returns 0 at 0 (same as GLSL), so `sign(0)*... = 0` matches.

**Noisemaker oklab "magic" pre-scale** (py-noisemaker parity, colorMode==3 /
paletteMode==2): before `linear_srgb_from_oklab`:
`color.g = color.g*-0.509 + 0.276; color.b = color.b*-0.509 + 0.198;`
Reproduce exactly when porting colorLab/shapeMixer palette+oklab paths.

---

## 5. DISTANCE METRICS — ENUMS DIFFER PER FILE (HAZARD)

There is **no shared distance-metric function or enum**. Three incompatible
mappings observed:

- `mixer/centerMask` `distanceMetric(p,corner,m)`: `mm = ((m%3)+3)%3`; **0=
  euclidean** (`length/length(corner)`), **1=manhattan**, **2=chebyshev**. All
  normalized by corner.
- `filter/glowingEdge` `distance_metric(gx,gy,metric)`: **1=manhattan,
  2=chebyshev, 3=minkowski/octagram** (`max((|gx|+|gy|)/1.414, max(|gx|,|gy|))`),
  **0(default)=euclidean** `sqrt(gx²+gy²)`.
- `filter/outline` `distanceMetric(gx,gy,metric)`: **2=manhattan, 3=chebyshev,
  4=octagram, else=euclidean** — shifted by one vs glowingEdge!

The octagram magic divisor is `1.414` (≈√2, but **not** exactly √2 — use literal
`1.414`). The kaleido effect has `getMetric(st)` (its own). Port the metric enum
**from the specific effect**; never share.

---

## 6. BLEND / ROTATE / KALEIDO / SYMMETRY

### 6.1 Blend modes

- `_shared/overlayBlend` (alpha over): `a = overlay.a*alpha; result =
  base.rgb*(1-a)+overlay.rgb*a; out.a = base.a;` (`texelFetch`, integer coords).
- Common named: `blendOverlay`, `blendSoftLight`, `blendBicubic`,
  `blendLinearOrCosine`, `blendColors`, `mixInColorSpace`. These are per-effect;
  port from the effect.
- `mixer/applyMode` HSV channel mix: `mode 0` = brightness (H,S from A, V from B);
  `mode 1` = hue (H from B, S,V from A); `mode 2` = saturation (H,V from A, S
  from B). Then 2-segment crossfade on `amt = map(mixAmt,-100,100,0,1)`:
  `amt<0.5 → mix(A,middle,amt*2)` else `mix(middle,B,(amt-0.5)*2)`; output alpha
  `max(A.a,B.a)`.

### 6.2 `rotate2D` — 11+ VARIANTS, DIFFERENT ANGLE CONVENTIONS (HAZARD)

There is **no canonical rotate2D**. Variants seen, by angle convention:
- **bare** (`mandala`, `osc2d`, `shapes3d`): `vec2(p.x*c - p.y*s, p.x*s + p.y*c)`,
  angle in **radians** as-passed. (shapes3d takes precomputed `vec2 cs`.)
- **aspect-correct, radians** (`gradient`): scales x by aspect, recenters at
  `(aspect*0.5,0.5)`, rotates with `mat2(c,-s,s,c)`, un-scales.
- **`rot*PI`** (`noise3d`, `cellNoise`, `spiral`): `cellNoise` first
  `rot=map(rot,0,360,0,2)` → angle `rot*PI`. `spiral` `angle=rot*PI`.
- **`rot*TAU`** (`bitEffects`): `rot=map(rot,0,360,0,1)` → `rot*TAU`, recenters
  at `fullResolution*0.5` (pixel space!).
- **media** (`media`): `rot=map(rot,-180,180,0.5,-0.5); angle=rot*TAU*-1`.
- **feedback**: `rot=map(rot,0,360,0,2); angle=rot*PI`.
- **shapes** (`shapes`): note recenters at `(0.5 - aspectRatio, 0.5)` (minus!),
  a likely-quirk to reproduce.

`mat2(c,-s,s,c)` in GLSL is **column-major**: column0=(c,-s), column1=(s,c), so
`mat2*st = (c*x + s*y, -s*x + c*y)`. **HLSL `float2x2` row-major + `mul(M,v)`
will transpose this** unless you account for it (see §4.3 hazard). Match per
effect: the rotation **direction** (CW vs CCW) depends on this and on Y origin.

### 6.3 Kaleidoscope (`classicNoisedeck/kaleido`)

```glsl
vec2 kaleidoscope(vec2 st, float sides, float blendy){
  float r = getMetric(st) + blendy;
  st = st - vec2(0.5*aspectRatio, 0.5);
  float a = atan(st.y, st.x);           // note (y,x) order
  // DIRECTION define: 1→ -time, 2→ 1.0, else→ time
  float ma = mod(a + radians(90.0) - radians(360.0/sides * dir), TAU/sides);
  ma = abs(ma - PI/sides);
  st = r * vec2(cos(ma), sin(ma));
  return fract(st);
}
```
`DIRECTION` is a compile-time define. `atan(y,x)` = `atan2`; **GLSL `atan(y,x)`
arg order is (y,x); HLSL `atan2(y,x)` same order — OK.** `mod` here is GLSL
always-positive; HLSL needs `a - b*floor(a/b)`. `radians(d)=d*PI/180`.

### 6.4 mirror/symmetry

`mirrorWrap`, `mirrorFold` appear once each (filter waves/etc.) — port from the
effect. Typical `mirrorFold(x)=abs(fract(x*0.5)*2-1)` style.

---

## 7. GLOBAL PARITY HAZARDS (apply to everything)

1. **Y-origin / flip** (§0). Biggest risk. GLSL bottom-left vs WGSL/HLSL
   top-left. Backends reconcile via `UNPACK_FLIP_Y` (WebGL2, default true) and
   blit flips (WebGPU). Choose WGSL math as reference; replicate the same flips.
2. **Matrix layout** (§4.3, §6.2). GLSL/WGSL column-major `M*v`; HLSL row-major
   `mul(M,v)`. Transpose constants or set `pack_matrix(column_major)`. Affects
   oklab and every `mat2`/`mat3` rotation.
3. **`mod` vs `%`**: GLSL `mod(a,b)=a-b*floor(a/b)` (sign of b, always positive
   for positive b). HLSL `%` and WGSL `%` follow sign of dividend. Anywhere GLSL
   uses `mod` with possibly-negative args (hue wrap, kaleido, positiveModulo,
   periodic), emulate `a-b*floor(a/b)`.
4. **uint wraparound**: pcg/jitter rely on mod-2^32 unsigned overflow — HLSL
   `uint` matches. Never use signed. `>>` must be logical (uint).
5. **float→uint cast of negatives** (§1.1): implementation-defined; the fold in
   `prng` usually avoids it. Match fold/no-fold per effect.
6. **`floatBitsToUint` / `bitcast` / `asuint`**: bit-exact reinterpret — same on
   all targets for finite values; NaN bit patterns may differ (avoid).
7. **FMA / fused multiply-add**: GLSL/WGSL don't contract by default in the
   reference (ANGLE/Tint generally no-contract); FXC/DXC may fuse `a*b+c`,
   changing the last bit and cascading through `pow`. For strict bit-parity mark
   accumulations `precise` in HLSL and avoid relying on `mad`.
8. **`pow` of slightly different exponents** (`0.3333333333333` vs `0.333333`;
   `1.0/2.4`): always use the 13-digit / literal-division forms from the GLSL
   reference path.
9. **`fract`/`frac`, `floor`, `smoothstep`, `clamp`, `step`, `sign`, `min/max`,
   `length`**: semantically identical across GLSL/WGSL/HLSL for finite inputs.
   `sign(0)=0` in all three. `step(edge,x)` = `x<edge?0:1` (GLSL/HLSL: returns 1
   when `x>=edge`).
10. **Texture sampling**: most lattice math uses `texelFetch`/`textureLoad`
    (integer, no filtering) or `texture(...)` with the texture's sampler
    (linear, clamp/repeat per pass). Color is stored **RGBA, 4-channel, treated
    as already-sRGB-encoded data in a non-sRGB (UNORM) target** — the shaders do
    sRGB math explicitly (§4.2); do NOT enable hardware sRGB on the render
    targets or you double-convert. Match Unity render-texture format to plain
    `RGBA` UNORM/float, not `sRGB`.
11. **Evaluation order** in pcg's three sequential `v.x/v.y/v.z` updates and in
    multi-row spline reconstruction — preserve statement order.
12. **Per-effect divergence**: the SAME-named helper is NOT the same code across
    effects (`prng` fold, `rgb2hsv` family, distance enum, `rotate2D`
    convention, jitter `seedBits`). **Always port from the specific effect file,
    not from a generic shared header.**

---

## 8. OPEN QUESTIONS / CROSS-SUBSYSTEM DEPENDENCIES

- **Uniform packing (WGSL):** WGSL effects pack uniforms into
  `array<vec4<f32>,N>` (`Uniforms.data`) and unpack by index (see noise.wgsl
  `main`). The Unity port must reproduce the exact field→slot mapping per effect
  (depends on the compiler's uniform-layout subsystem — out of scope here).
- **`NOISE_TYPE`/`LOOP_OFFSET`/`DIRECTION` define injection** comes from each
  `definition.js` `globals.*.define`; the value→variant mapping (§2) is fixed
  but the *selection* logic lives in the definitions/UI subsystem.
- **tileOffset / fullResolution vs resolution**: tiling renders pass a sub-tile
  `resolution` but `fullResolution` for global coordinates. The exact tile loop
  is in the canvas/pipeline subsystem; math here only needs `globalCoord =
  fragCoord + tileOffset`, `st = globalCoord / fullResolution.y`.
- **Which effects use family-1 vs family-2 color** and which `rotate2D`/metric
  variant: enumerate by grepping each effect when porting; this doc lists the
  representative groupings but not all 200+ effects exhaustively.
- **`renderScale`**: dither/bayer paths divide by `renderScale`; its source is
  the canvas subsystem.
- Backend reconciliation of `flipY` (WebGL2 `UNPACK_FLIP_Y_WEBGL` vs WebGPU
  blit) should be confirmed against the canvas orchestrator to pin the single
  authoritative orientation for the Unity target.
```
