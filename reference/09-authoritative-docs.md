# 09 — Authoritative Docs (Prose Semantics) — HLSL/Unity Port Reference

This document distills the *authoritative prose specs* for the Noisemaker shader
engine, extracted verbatim-where-load-bearing from:

- `docs/shaders/language.rst` — Polymorphic DSL grammar & semantics
- `docs/shaders/compiler.rst` — compiler stages & error codes
- `docs/shaders/pipeline.rst` — pipeline architecture, surfaces, uniforms, determinism
- `docs/shaders/effects.rst` — Effect definition schema
- `docs/shaders/features.rst` — feature index (toctree only)

> SCOPE NOTE: These are *specification documents*, not the code. The code is the
> ground truth for pixel-identical output; this doc captures **intent, guarantees,
> defaults, and edge cases** to cross-check the code-reading specs (`04`, `05`,
> ...). Where docs and code might diverge, that is flagged under **DISCREPANCY**.

---

## 0. Top-Level Mental Model

The system is a **declarative GPU render-graph compiler**. Two layers:

1. **Polymorphic DSL** — a high-level live-coding language. A program is a list of
   *chains* of function calls that compile to a **DAG of render passes**. Source:
   `language.rst`.
2. **Pipeline** — executes the DAG every frame on **WebGL2 or WebGPU**, fully
   GPU-resident ("zero CPU copies; the CPU only orchestrates dispatch").
   Source: `pipeline.rst`.

The compiler (`compiler.rst`) transforms DSL → AST → Logical Graph → Render Graph →
Execution Plan in **four stages**:

```
Source --Lexer/Parser--> AST
AST    --Semantic Analyzer--> Logical Graph   (Nodes=Effects, Edges=data flow)
Logical Graph --Effect Expander--> Render Graph (Nodes=Passes, Edges=texture deps)
Render Graph --Resource Allocator--> Execution Plan (linear sorted command list)
```

For the Unity/HLSL port: the **DSL parser/compiler is offline/authoring-time** —
Unity needs the *Execution Plan* semantics (pass order, texture lifetimes,
double-buffering, uniform packing). The DSL grammar matters only if the Unity tool
must parse `.dsl` text. The **pipeline execution semantics are the load-bearing
part for pixel-identical output.**

---

## 1. Polymorphic DSL — Grammar (EBNF, verbatim)

```
Program        ::= SearchDirective? Statement* RenderDirective?
SearchDirective::= 'search' Ident ( ',' Ident )*
Statement      ::= VarAssign | ChainStmt | IfStmt | Break | Continue | Return
RenderDirective::= 'render' '(' OutputRef ')'
Block          ::= '{' Statement* '}'
IfStmt         ::= 'if' '(' Expr ')' Block ('elif' '(' Expr ')' Block)* ('else' Block)?
Break          ::= 'break'
Continue       ::= 'continue'
Return         ::= 'return' Expr?
VarAssign      ::= 'let' Ident '=' Expr
ChainStmt      ::= Chain
Chain          ::= ChainElement ( '.' ChainElement )*
ChainElement   ::= Call | WriteCall | Write3DCall | SubchainCall
SubchainCall   ::= 'subchain' '(' ArgList? ')' '{' ( '.' Call )+ '}'
WriteCall      ::= 'write' '(' OutputRef ')'
Write3DCall    ::= 'write3d' '(' ( VolRef | Ident ) ',' ( GeoRef | Ident ) ')'
Expr           ::= Chain | NumberExpr | String | Boolean | Color | Ident | Member
                 | OutputRef | SourceRef | VolRef | GeoRef | XyzRef | VelRef
                 | RgbaRef | MeshRef | Func | '(' Expr ')'
Call           ::= Ident '(' ArgList? ')'
ArgList        ::= Arg ( ',' Arg )* ','?
Arg            ::= NumberExpr | String | Boolean | Color | Ident | Member
                 | OutputRef | VolRef | GeoRef | XyzRef | VelRef | RgbaRef
                 | MeshRef | Func
NumberExpr     ::= Number | 'Math.PI' | '(' NumberExpr ')'
                 | NumberExpr ( '+' | '-' | '*' | '/' ) NumberExpr
Member         ::= Ident ( '.' Ident )+
Func           ::= '(' ')' '=>' Expr
OutputRef      ::= 'o' Digit+
VolRef         ::= 'vol' Digit+
GeoRef         ::= 'geo' Digit+
XyzRef         ::= 'xyz' Digit+
VelRef         ::= 'vel' Digit+
RgbaRef        ::= 'rgba' Digit+
MeshRef        ::= 'mesh' Digit+
SourceRef      ::= 's' Digit+
Ident          ::= Letter ( Letter | Digit | '_' )*
Number         ::= Digit+ ( '.' Digit+ )?
String         ::= '"' [^"\n]* '"' | '"""' .* '"""'
Digit          ::= '0'…'9'
Letter         ::= 'A'…'Z' | 'a'…'z'
Boolean        ::= 'true' | 'false'
Color          ::= '#' HexDigit HexDigit HexDigit ( HexDigit HexDigit HexDigit )? ( HexDigit HexDigit )?
HexDigit       ::= Digit | 'A'…'F' | 'a'…'f'
```

### 1.1 Operator precedence / associativity
- `*`, `/` bind tighter than `+`, `-`.
- Left-associative.
- `()` overrides.
- **Numeric arithmetic is evaluated at compile/parse time** (constant folding into a
  single number bound to the uniform). `Math.PI` is the only named constant.

### 1.2 Reference token namespaces (surface aliases)
| Prefix | Meaning | Range | Notes |
|---|---|---|---|
| `oN`   | 2D global surface | `o0`..`o7` | double-buffered, screen-sized, `rgba16f` |
| `volN` | 3D volume | `vol0`..`vol7` | default **64³**, `rgba16f` |
| `geoN` | geometry buffer | `geo0`..`geo7` | screen-sized; xyz=surface normal, w=depth |
| `xyzN` | agent position | `xyz0`..`xyz7` | SMRTicles: xyz=pos, w=lifecycle state |
| `velN` | agent velocity | `vel0`..`vel7` | SMRTicles |
| `rgbaN`| agent color | `rgba0`..`rgba7` | SMRTicles |
| `meshN`| mesh geometry | `mesh0`..`mesh7` | texture pair (positions XYZW + normals XYZ+UV) |
| `sN`   | source ref | `s0`..`s7` | `SourceRef`, used as Expr only (not chain target) |

### 1.3 Colors
`#RGB`, `#RRGGBB`, `#RRGGBBAA`. **Alpha defaults to `FF` (1.0) when omitted.**
3-digit form is expanded per-nibble (standard CSS-style). Hex codes may appear
unquoted as arguments.

> PARITY HAZARD: How `#RGB` → float is computed (divide by 255 vs by 15/255 nibble
> doubling) is not specified in prose — **must be read from code**. Whether color
> args are sRGB or linear is unspecified here (see §7 sRGB).

### 1.4 Strings
Double-quote `"..."` (no embedded newline allowed: `[^"\n]*`), or triple-quote
`"""..."""` which **preserves embedded newlines**. Used by `text` effect.

### 1.5 Arrow functions
`() => expr` only (zero-arg expression lambdas). Treated as **lazy/deferred
expressions** — passed *as-is*, evaluated by the consumer (control flow / future
callbacks). Not evaluated immediately.

---

## 2. DSL Semantics (load-bearing rules)

### 2.1 Output materialization (the core guarantee)
- A chain that **begins with a generator MUST terminate with `.write(<surface>)`**.
  Omitting it → diagnostic **S006** (`Starter chain missing write() call`).
- Chains extending an existing surface (e.g. `read(o0).blur()`) may omit `.write()`
  *only when nested inside another chain that eventually writes*.

### 2.2 Chainable / mid-chain writes
- `.write(<surface>)` may appear **anywhere**, including mid-chain.
- Mid-chain write = "write current result to surface **and pass the texture
  through** to the next node."
- Multiple writes in one chain → multiple surfaces written.
- A chain MUST STILL terminate with a `.write()`; mid-chain writes alone insufficient.
- Example: `noise().write(o0).blur().write(o1)` → `o0`=noise, `o1`=blurred noise.
- Example: `noise().write(o0).invert().write(o1)` → `o0`=original, `o1`=inverted.

### 2.3 Generators
A chain MUST start with a **Generator** (effect with no inputs).
- Standard: `osc`, `noise`, `voronoi`, `solid`, `image`, `video`, `camera`.
- Custom: any effect with `inputs: {}` or `meta.generator`.
- Generators may NOT appear mid-chain → diagnostic **S005** (`Illegal chain
  structure`). Inside subchains, contained effects may NOT be generators.

### 2.4 Variables, aliases, partial application
- `let x = noise` → `x` is an **alias** for the function.
- `let y = noise(10)` → `y` is a **partial application** (an Effect Instance with
  some params bound). It does NOT execute.
- `y(0.5)` → new instance merging stored (`freq:10`) + new (`sync:0.5`). **`y`
  remains immutable.**

**Partial merge rules:**
- Positional args: **appended** to stored args.
- Named args: **merged**; call-site overrides stored on key conflict.
- Duplicate keys in one call: **last value wins**.

> PARITY HAZARD (evaluation order): positional appending + named-override means the
> final bound parameter set depends on merge order. The Unity authoring tool must
> replicate this exactly when flattening partials.

### 2.5 Functions & arguments
- Positional `noise(10, 0.1, 1)` OR keyword `noise(freq:10, sync:0.1, amp:1)`.
  The two forms are **mutually exclusive within a single call**.
- Vector constructors: `vec2(x,y)`, `vec3(x,y,z)`, `vec4(x,y,z,w)`.
- **Array literals** `[a, b, ...]` are an equivalent input form for any
  vector-valued arg; parsed/validated identically to `vecN()`. They **round-trip
  through the unparser as `[…]`** (vecN constructors remain canonical for programs
  already using them). Elements may be any numeric expression (negatives,
  arithmetic, `Math.PI`). **Array length is NOT enforced by the validator** — passed
  through to runtime as declared.

### 2.6 Subchains
First-class grouping of contiguous effects (atomic, identifiable units).
```
.subchain(name: "group", id: "uid") { .effect1() .effect2(param: v) }
```
- `name` optional (may be positional); `id` optional.
- Rules: must contain ≥1 effect (no empty); cannot be first element (needs preceding
  input); contained effects cannot be generators; chainable (output flows through
  after `}`); same arg syntax as regular effects.
- Purpose: organization, UI grouping, programmatic manipulation. **No semantic
  effect on rendering** — purely structural/metadata.

### 2.7 Control flow
`if`/`elif`/`else` exist in **parser + validator only**. **Runtime branching is NOT
implemented** — programs using these will not execute until pipeline gains support.
`break`/`continue`/`return` likewise parse but `ERR_CONTROL_FLOW_INVALID` guards
invalid contexts. → For Unity: treat as no-op / unsupported.

---

## 3. Namespaces & symbol resolution

### 3.1 `io` namespace (always implicit)
Pipeline-level I/O, never needs `search`:
- `read(surface)` — read a 2D surface
- `write(surface)` — write a 2D surface
- `read3d(vol, geo)` — read volume + geometry (chain starter)
- `read3d(vol)` / `read3d(geo)` — single-arg form for *param* passing (mirrors
  2D `read(o0)`)
- `write3d(vol, geo)` — write volume + geometry
- `render(surface)` — set final output (program directive)
- `render3d()` — render 3D volume to 2D output

### 3.2 Active namespaces
`synth` (2D generators), `filter` (2D single-input), `mixer` (2-input combine),
`render` (rendering utils, feedback loops, points emit/render), `points` (particle
sims: physarum, life, flock, flow), `synth3d` (3D generators), `filter3d` (3D
processors).

`effects.rst` lists a slightly different set (`sim` namespace appears there; not in
`language.rst`). **DISCREPANCY** — see §10.

### 3.3 Classic + custom
- `classicNoisedeck`: ported "Classic" shaders (complex/slower).
- Runtime `registerNamespace(id, descriptor)` / `unregisterNamespace(id)` for
  external integrations. Reserved `user` namespace available without registration.

### 3.4 Search resolution rules
1. Every program **MUST** begin with `search <ns>, ...`. **No implicit default in
   prose.** BUT `compiler.rst §2.1` says "If omitted, the default order
   `['synth','filter']` is used." **DISCREPANCY** — see §10.
2. Unqualified calls walk the search order until a match is found (first-match wins).
3. `from(ns, fn())` sources an op from a specific namespace temporarily.
4. **Inline namespace prefixes (`synth.noise()`) are FORBIDDEN** in chains.

---

## 4. Enums, palettes, oscillators, live input

### 4.1 Enums
Defined globally in `std_enums.js` as categories (`color`, `blend`, `wrap`, ...).
Reference 3 ways:
- Shorthand: `colorMode: rgb` (validator auto-prefixes → `color.rgb`)
- Full path: `colorMode: color.rgb`
- Member expr via variable: `let mode = color.mono; noise(colorMode: mode)`

Runtime **resolves enum refs to integers** before binding to the shader. Enums are
**always numeric `int`** at the binding layer (`pipeline.rst §10`).
`ERR_ENUM_INVALID` for unknown strings.

Effect-local enums use `choices: { label: int }` map (string label → integer), or
`enum: "<globalKey>"` referencing the global registry; `default` may be the string
key (e.g. `"linear"`) which the runtime resolves to its int (e.g. `1`).

**Documented global enum example (`effects.rst §4`):**
```js
interpolation: { nearest:0, linear:1, hermite:2, cubic:3 }
wrapMode:      { clamp:0, repeat:1, mirror:2 }
```
> PARITY HAZARD: these specific integer mappings must match the real `std_enums.js`
> values when ported — verify against code. Wrap-mode integers directly drive
> sampler addressing (clamp/repeat/mirror) and must match Unity `TextureWrapMode`.

### 4.2 Palettes
`palette` enum = named cosine-gradient functions mapping scalar (typically
luminance) → RGB. Used by `palette(paletteIndex: <name>)` in `filter`. ~60 named
palettes (full list in `language.rst`; e.g. `vaporwave`, `heatmap`, `silvermane`
(OkLab), `spooky` (OkLab), `tropicalia` (OkLab)). Note several are computed in
**OkLab** color space.
> PARITY HAZARD: cosine-gradient coefficients and OkLab conversion are NOT in prose;
> must come from code. OkLab math (cube roots, matrix) is precision-sensitive.

### 4.3 Oscillators (`osc()`)
Generate time-varying values for animating params. **Loop synchronized to animation
duration (default 10 seconds).**

| Param | Type | Default | Meaning |
|---|---|---|---|
| `type` | oscKind | (required) | waveform |
| `min` | number | 0 | output min |
| `max` | number | 1 | output max |
| `speed` | int | 1 | cycles per animation loop (divides evenly into duration) |
| `offset` | number | 0 | phase offset 0..1 |
| `seed` | number | 1 | random seed (`noise` type only) |

**oscKind values:** `sine` (0→1→0 smooth), `tri` (linear 0→1→0), `saw` (0→1),
`sawInv` (1→0), `square` (0 or 1), `noise` (periodic 2D noise, seamless loop).

**Runtime:** per-frame, time normalized to 0..1 over duration, then apply `speed`
multiplier and `offset`, compute waveform, then **map internal 0..1 → min..max**.

> PARITY HAZARD: exact waveform formulas (esp. `sine` phase convention, `tri`
> peak position, `square` threshold, and the `noise` 2D-noise generator + seed) are
> NOT in prose. Animation `time` origin and 10s duration constant are load-bearing
> for reproducing animated frames — must match code.

### 4.4 Live input
- `midi(channel, mode?, min?, max?, sensitivity?)`: channel 1–16; `mode` default
  `velocity`; min/max default 0..1; `sensitivity` (decay for trigger modes) default 1.
- `audio(band, min?, max?)`: band ∈ `low|mid|high|vol`; min/max default 0..1.
These are *external-signal-driven* (non-deterministic) — out of scope for offline
pixel parity unless host injects fixed values.

---

## 5. Pipeline Architecture (the part that determines pixels)

### 5.1 Six core philosophy guarantees
1. Declarative effects (JSON graphs, not imperative code).
2. Graph-based execution (whole frame = DAG of passes).
3. Multi-pass by design (layering + feedback first-class).
4. Backend agnostic (definition abstract; runtime handles WebGL2 vs WebGPU).
5. **GPU-Resident: "the entire render loop runs on the GPU with zero CPU copies;
   the CPU only orchestrates dispatch."** → No per-frame readback.
6. Compute-first (native WebGPU; GPGPU-emulated on WebGL2).

### 5.2 Three phases
- **Phase 1 Graph Compilation** (on DSL change): parse → analyze → expand effects →
  **scope state textures** → topo sort → resource analysis.
- **Phase 2 Resource Allocation** (before exec / on resize): texture pool +
  allocation with reuse.
- **Phase 3 Execution** (every frame): update globals → iterate passes → dispatch.

### 5.3 State-texture scoping (CRITICAL for stateful effects)
Stateful effects use `global_*` textures (`global_rd_state`, `global_ca_state`,
`global_accum`, ...). During expansion:
- Scoped **per-chain** → `global_rd_state_chain_0`, so multiple instances of the same
  stateful effect in *separate* chains get independent state.
- Particle textures (`global_xyz`, `global_vel`, ...) further scoped
  **per-pipeline to the node that creates them.**
- **Effects within the same chain SHARE state** (required for `loopBegin`/`loopEnd`
  sharing `global_accum`).

### 5.4 Per-frame globals (uniforms) — Phase 3 step 1
Refreshed each frame:
- `time` — seconds since start
- `deltaTime` — frame-to-frame delta
- `frame` — integer tick
- `resolution` — `vec2` pixels
- `aspect` — width ÷ height

> PARITY HAZARD: `time`/`frame`/`deltaTime` origin and whether they're double or
> float32. Animations and frame-counter-driven RNG depend on these exactly. For
> deterministic offline rendering Unity must drive `time`/`frame` identically (fixed
> timestep, same start epoch).

### 5.5 Dispatch (per backend)
**WebGL2:**
- Activate compiled `WebGLProgram`; resolve target FBO (global surfaces → current
  write buffer).
- Viewport derived from target texture dims (or pass override).
- Bind declared input textures to successive units; upload merged
  (`globalUniforms` + pass uniforms) via `gl.uniform*`.
- Optional blending.
- Draw: **`gl.drawArrays(gl.TRIANGLES, 0, 3)`** (full-screen triangle) by default,
  or `gl.drawArrays(gl.POINTS, ...)` when `drawMode == 'points'`.

**WebGPU:**
- One command encoder per frame; per pass resolve output view (respecting
  double-buffer swaps).
- Bind group packs sampled textures + default sampler + freshly uploaded uniform
  buffer (`globalUniforms` merged with pass uniforms).
- Render pass: **clears target**, sets pipeline + bind group, `passEncoder.draw(3,1,0,0)`.
- Compute pass: `passEncoder.dispatchWorkgroups(...)` using explicit `workgroups` or
  dims derived from output texture.

> PARITY HAZARD (full-screen triangle): both backends draw a **3-vertex
> oversized triangle**, not a quad. The vertex shader's UV/clip mapping defines the
> coordinate origin. **Y-orientation must be verified against code** — WebGL has
> bottom-left framebuffer origin and `gl_FragCoord` origin at bottom-left, WebGPU
> has top-left framebuffer origin and texel `(0,0)` at top-left; Unity/D3D is
> top-left with `SV_Position` top-left and V often flipped. The pipeline must
> already reconcile WebGL vs WebGPU; the Unity port must match the **WebGPU/top-left
> convention** OR whatever the visual tests assert. This is the #1 pixel-parity risk.

### 5.6 Compute emulation contract (WebGL2)
- Full-screen tri/quad fragment shader substitutes invocation IDs.
- `gl_FragCoord.xy` maps to `GlobalInvocationID.xy`. **Formula:
  `GlobalInvocationID.xy = floor(gl_FragCoord.xy)`.**
- Emulated local size fixed at 1; `workgroups` only scales virtual coords.
- **Scatter writes FORBIDDEN** (only 1:1 mapping); static analysis for `imageStore`
  → `ERR_COMPUTE_UNSUPPORTED_FEATURE`.
- Multiple outputs require MRT; if backend lacks `MAX_DRAW_BUFFERS >= N` →
  `ERR_COMPUTE_MRT_UNSUPPORTED`.
- WebGPU default dispatch if `workgroups` omitted: **`[ceil(width/8), ceil(height/8),
  1]`** for 2D textures.
- Shared memory / subgroup ops / atomics: **disallowed** until `version >= 2.x`.

> PARITY HAZARD: `floor(gl_FragCoord.xy)`. In WebGL, fragment centers are at `.5`, so
> `floor` of a pixel center yields the integer index. In Unity HLSL, `SV_Position` is
> also `+0.5` pixel-centered but **Y may be flipped**. Compute → fragment emulation
> must preserve the exact integer cell index the agent/CA shaders read/write.

---

## 6. Surfaces, Buffering & Feedback (CRITICAL semantics)

### 6.1 Surface tables (defaults, verbatim)
```js
surfaceTable: o0..o7 = { format:'rgba16f', width:'screen', height:'screen', doubleBuffered:true }
volumeTable:  vol0..vol7 = { format:'rgba16f', width:64, height:64, depth:64, is3D:true }
geoBufferTable: geo0..geo7 = { format:'rgba16f', width:'screen', height:'screen', doubleBuffered:true }
```

### 6.2 Surface types
- **2D `o0..o7`**: standard double-buffered. Reading within a frame sees writes made
  *earlier in that same frame*.
- **3D `vol0..vol7`**: persistent volumes, default **64×64×64**.
- **`geo0..geo7`**: screen-sized; **xyz=surface normal, w=depth** — precomputed
  raymarching results, enabling downstream post without re-raymarching.
- **Agent surfaces (SMRTicles)**: `xyz` (pos+lifecycle), `vel`, `rgba` (color).
- **`mesh0..mesh7`**: pair of positions (XYZ+W) and normals (XYZ+UV) textures.

### 6.3 USER-ONLY surfaces (hard rule)
`o0..o7`, `vol0..vol7`, `geo0..geo7` are **reserved exclusively for user
composition** and **MUST NOT be hardwired inside effect definitions**. Effects
needing feedback/temp storage MUST allocate their own internal textures
(`_feedbackBuffer`, `_temp0`) in `textures`. Hardwiring corrupts the user graph.

### 6.4 Double-buffer indexing (load-bearing)
> Frame index `F` selects **read buffer = `(F-1) mod 2`**, **write buffer =
> `F mod 2`**. A chain `.write(o0)` targets the write buffer; chains reading `o0`
> *before* its write use the read buffer. After a write to `oN`, subsequent reads
> *in the same frame* see freshly written content.

- `doubleBuffered`: two physical textures swapped every frame (read prev frame while
  writing current).
- `persistent`: content preserved across frames + resizes. All global surfaces are
  effectively persistent. Internal textures may set `persistent: true` for feedback.
- Multiple writes to the same surface in one frame **forbidden** unless
  `compositeAllowed` (future) → `ERR_SURFACE_MULTIWRITE`.

> PARITY HAZARD (feedback / RNG / temporal): the `(F-1)%2 / F%2` rule means a Unity
> port must replicate exact ping-pong parity. An off-by-one in the frame counter, or
> reading the wrong buffer for the *first* frame (F=0 reads buffer index -1 mod 2 =
> 1, which is uninitialized/cleared), changes every feedback effect's output. Define
> initial buffer contents (cleared to transparent black) and the `frame` start value
> (0 vs 1) identically.

### 6.5 Feedback loops
A chain reading a surface **not yet written this frame** (or reading itself) reads
the **previous frame's** content. This is how feedback works **without** creating a
same-frame dependency cycle. Cycles in the *current frame* graph are strictly
forbidden (`ERR_CYCLE`); feedback must route through `persistent`/global surfaces.

### 6.6 Resize behavior
1. Compare `(currentW,currentH)` to cached `(lastW,lastH)` before frame.
2. Mark all `dimension=='screen'` or `'%'` textures for reallocation.
3. Preserve persistent global surfaces: **blit→temp, resize, blit back**
   (`preserveAspect:false`). **Fallback:** incompatible format (channel-count change)
   → blit skipped, surface **cleared to transparent black**.
4. Rebuild pool (release mismatched entries).
5. Recompile dependent viewports/workgroups.

---

## 7. Uniform & Binding Conventions (DETERMINISM-CRITICAL)

From `pipeline.rst §10`:
- **Naming:** shader side may use `u_*`; adapter strips prefix for effect/global key
  mapping.
- **Packing:** all scalar/vec/mat uniforms grouped into a **single buffer per pass**;
  layout **std140** for both WebGPU & WebGL (16-byte boundaries).
- **Boolean:** `int` (0/1) in GLSL ES; WGSL native `bool` but a numeric mirror is
  provided for deterministic hashing.
- **Matrices: row-major in effect spec; adapter transposes if backend requires
  column-major.**
- **Enumeration:** always numeric `int`.
- Samplers vs storage: storage only if texture `usage` includes `storage` and backend
  supports.

### 7.1 std140 layout (verbatim alignment rules)
| Type | Align | Stride/size |
|---|---|---|
| `float`/`int`/`uint`/`bool` | 4 | 4 |
| `vec2` | 8 | 8 |
| `vec3` | 16 | 12 data (pad to 16) |
| `vec4` | 16 | 16 |
| `mat3` | 16 each | 3× `vec3` = 48 |
| `mat4` | 16 each | 4× `vec4` = 64 |

Packing: buffer starts at fixed size (e.g. 256 bytes); grows to next power of two
(512, 1024) when exceeded. `alignTo(offset, a) = ceil(offset/a)*a`. **Little-endian**
(`setFloat32(..., true)`). Floats packed as **IEEE-754 single (float32)**.

> PARITY HAZARD (matrix order): "row-major in effect spec; adapter transposes if
> backend requires column-major." HLSL defaults to **column-major** constant-buffer
> packing and **`mul(M, v)`** semantics differing from GLSL's `M * v`. The Unity port
> must decide: store matrices row-major (matching effect spec) and either transpose
> on upload or use `mul(v, M)`. Getting this wrong silently transposes every rotation/
> warp matrix. Also note the std140 `packUniforms` writes `mat3` as 3 columns of 16
> bytes each — verify the column-vs-row interpretation in code (the loop writes
> `value[col*3 + row]`, i.e. **column-major source array** indexing).

> PARITY HAZARD (vec3 padding): `vec3` occupies 16 bytes but only 12 are data. HLSL
> cbuffer packing also pads `float3` but a following scalar **can pack into the same
> 16-byte register** in HLSL, whereas std140 here advances offset by 16 after vec3.
> Mismatched packing corrupts all subsequent uniforms.

### 7.2 Implicit textures
- `inputTex`: canonical upstream chain output. Auto-created for non-generators; dims/
  format inherited from previous pass output or default **screen-sized `rgba16f`**.
  First pass reading `inputTex` with no upstream → `ERR_NO_INPUT`. Legacy alias
  `inputColor` still recognized.
- `outputColor`/`outputTex`: synthetic final output. Dims match largest output
  texture or `inputTex`; format from pass output decl or default `rgba16f`. Becomes
  next effect's `inputTex`.

---

## 8. Effect Definition Schema (authoring contract)

### 8.1 Constructor
`new Effect({ ... })` (recommended) or `class X extends Effect`.

**Required:** `name` (string), `passes` (array, minItems 1).
**Optional:** `namespace` (default `"synth"`), `func` (DSL name; defaults to
lowercase `name`), `tags`, `globals`, `textures`, `version` (default `"1.0.0"`),
`outputTex3d`, `outputGeo`, `meta`, lifecycle hooks.

`name` pattern `^[A-Za-z0-9_\-]{1,64}$`; `namespace` pattern `^[a-zA-Z0-9]+$`;
`version` pattern `^\d+\.\d+\.\d+$`.

### 8.2 `globals` (uniformSpec)
```
{ type: float|int|uint|bool|vec2|vec3|vec4|mat3|mat4,  // required
  default,            // fallback: 0, false, or identity matrix
  min, max, step,     // numbers
  choices: {label:int},     // dropdown map
  enum: "<globalKey>",      // ref global enum
  uniform: "shaderName",    // explicit shader uniform name override
  ui: { label, control: slider|dropdown|color|checkbox,
        category /*camelCase ^[a-z][a-zA-Z0-9]*$*/, hint, enabledBy },
  requires: {...} }
```
> **Default fallbacks (verbatim):** "Optional. Fallback: 0, false, or identity matrix."
> Unity must apply identical defaults when a param is unbound.

### 8.3 `textures` (textureSpec)
```
{ width: dimensionSpec, height: dimensionSpec, format,    // format required
  usage: [sample|storage|render|copySrc|copyDst],
  clear: [r,g,b,a],            // exactly 4 elements
  persistent: bool (default false) }
```
`additionalProperties:false`. Reserved names `inputTex, outputTex, inputTex3d,
inputGeo` are synthesized by runtime — must NOT be declared.

### 8.4 `passSpec`
```
{ name (required, ^[A-Za-z0-9_\-]{1,64}$),
  program (required),
  type: render|compute|transfer (default render),
  inputs: {samplerName: textureName},
  outputs: {location: textureName},
  iterations: int >=1 (default 1),
  pingpong: [texA, texB] (exactly 2),
  defines: {name: string|number|bool},
  uniforms: {name: uniformSpec},   // pass-specific, override globals
  workgroups: [x,(y),(z)] (1..3 ints >=1),
  viewport: {x,y,w,h},
  conditions: { skipIf:[{uniform,equals}], runIf:[{uniform,equals}] },
  barriers: ["texture:<name>:<stage>-><stage>"],  // stage∈fragment|compute
  readAfterWriteHazards: allow|forbid (default forbid) }
```

### 8.5 Dimension resolution (verbatim algorithm)
```
number          -> max(1, floor(n))
'screen'|'auto' -> screenSize
'input'         -> screenSize  (match input dims)
'NN%'           -> max(1, floor(screenSize * percent/100))
{param,default?,multiply?,power?,inputOverride?}:
   value = uniforms[param] ?? default ?? 64
   if multiply: value *= multiply
   if power:    value = pow(value, power)
   -> max(1, floor(value))
{scale, clamp?:{min,max}}:
   computed = floor(screenSize*scale)
   clamp.min -> max(min,computed); clamp.max -> min(max,computed)
   -> max(1, computed)
fallback -> screenSize
```
All dims MUST be positive integers; fractional rounds **down**; minimum 1px.

> PARITY HAZARD: `floor` rounding of `%` and `scale` dims. A `25%` texture at odd
> screen sizes (e.g. 1023) rounds down to 255, not 256. Mip/blur kernels reading at
> these sizes must use the identical rounded dimensions. Unity must use `Mathf.Floor`,
> not round-to-nearest.

### 8.6 Format support & negotiation
- WebGL required: `rgba8`, `rgba16f`, `rgba32f` (if `EXT_color_buffer_float`), `r8`.
- WebGPU required: `rgba8unorm`, `rgba16float`, `rgba32float`, `bgra8unorm`, depth as
  available.
- Negotiation: exact match → fallback table → precision downgrade (highest precision
  with same channel count) → `ERR_FORMAT_UNSUPPORTED`. Deterministic + cached per
  backend.
- WebGL fallback table:
  `rgba16float→rgba16f`, `rgba32float→(ext? rgba32f : rgba16f)`, `rgba8unorm→rgba8`.

> PARITY HAZARD (precision): default global surfaces are **`rgba16f` (half float)**.
> Intermediate accumulation in half precision differs from float32. Unity should use
> `RenderTextureFormat.ARGBHalf` to match, NOT `ARGBFloat`, unless code says
> otherwise. Half-float rounding differences accumulate across feedback frames.

### 8.7 Lifecycle hooks (CPU-side state)
- `onInit()` — once on load; init `this.state`.
- `onUpdate({time, delta, uniforms})` — **every frame before rendering**; returns an
  object of computed uniforms (merged in). E.g. Media effect returns `imageSize`.
- `onDestroy()` — cleanup.
> These run CPU-side JS each frame and can return uniform values (e.g.
> `Math.sin(phase)*intensity`). Unity must port equivalent C# per-frame compute for
> any effect using these, **including the exact `Math.sin` / phase accumulation** —
> `this.state.phase += delta * speed` is order/timestep dependent.

### 8.8 UI metadata (non-render)
`ui.category` (camelCase, default `"general"`, ordered by first occurrence),
`enabledBy` conditional system (string truthy OR object with
`eq/neq/gt/gte/lt/lte/in/notIn/or/and/not`; multiple ops AND'd). **UI-only — no
render effect.** PascalCase / spaces / underscores in category are BANNED.

> DISCREPANCY: schema `tags` enum lists `["color","distort","geometric","math",
> "noise","transform","util"]` but the prose tag table also includes `3d, agents,
> debug, gradient, sim`. Tag list authoritative source is `shaders/src/runtime/
> tags.js`. See §10.

---

## 9. Compiler Stages & Error Codes

### 9.1 Four stages (recap)
1. **Parsing** string → `ProgramNode` (AST). AST is **flat**: chains are flat arrays
   of `Call` nodes (NOT nested CallExpressions). `.write()` parsed separately into
   `out`. Root segregates `vars`, `plans` (ChainStmts), `render`.
2. **Analysis** AST → Logical Graph: symbol resolution (search order), chain analysis
   (root = first element = generator; instance creation; param binding incl.
   `int→float` coercion), graph construction (edges = `outputColor`→`inputColor`).
3. **Expansion** Logical Graph → Render Graph: texture allocation (resolve internal +
   implicit `inputColor`/`outputColor`, compute dims), pass generation (one Render
   Pass node per `passes` entry, map logical names → resource IDs, inject `defines`),
   shader program compilation (resolve `.glsl`/`.wgsl` by `program` key, prepend
   `#define`s, WebGL wraps compute in full-screen quad + generates frag boilerplate).
4. **Assembly** Render Graph → Execution Plan: **topological sort (Kahn's
   algorithm)** + cycle detection; resource optimization (liveness `[first_write,
   last_read]`, pooling: non-overlapping intervals share physical texture); command
   generation (`SetGlobal`, `BindTexture`, `BindProgram`, `Draw`).

### 9.2 Pass expansion (iterations & ping-pong) — verbatim
```js
expandPass(pass):
  if iterations==1: single step.
  if iterations>1 and (!pingpong || pingpong.length!=2): ERR_ITER_NO_PINGPONG
  [texA, texB] = pingpong
  for i in 0..iterations-1:
    isEven = i%2==0
    readTex  = (i==0) ? pass.inputs : { firstInputKey: isEven?texA:texB }
    writeTex = isEven ? texB : texA
    name = `${pass.name}#${i}`
    uniforms = { ...pass.uniforms, _iteration: i }
  // final output = last-written pingpong texture
```
Iteration `0` reads original inputs, writes `texB`. Iteration 1 reads `texB`, writes
`texA`. Etc. Subsequent passes reading the logical output get remapped to the correct
buffer.

> PARITY HAZARD (ping-pong parity): for **odd** iteration counts the final result is
> in `texB`; for **even** it's in `texA`. An off-by-one in the loop, or seeding
> iteration 0 from the wrong buffer, changes which texture holds the result. The
> `_iteration` uniform is injected and shaders may branch on it. Unity must reproduce
> the exact read/write swap schedule and the `#i` naming for determinism.

### 9.3 Topological sort & feedback (Kahn)
1. Edge A→B if B reads a texture written by A and not yet overwritten.
2. If P reads `oX` and nothing writes `oX` earlier this frame, add edge from synthetic
   `SURFACE_PREV_oX` → P.
3. Zero-in-degree queue; pop → append → decrement successors.
4. Remaining in-degree>0 → `ERR_CYCLE`.
5. Iteration passes expanded by duplicating logically N times, preserving resource
   indices.

> PARITY HAZARD (evaluation order): topo sort must be **deterministic** — ties in the
> zero-in-degree queue must break in a stable order (insertion order). Determinism
> guarantee §15 requires "hash of sorted pass list ... stable given identical
> effect + screen size." If Unity's sort breaks ties differently, pass order (and
> thus which intermediate buffer a pass reads) can change → different pixels for
> multi-write/pool-aliased graphs.

### 9.4 Dynamic pass skipping
`conditions.skipIf` / `runIf`: evaluated against current uniform values **before
dispatch**. Skipped pass outputs **retain previous content** (or cleared if not
persistent). Runtime check — does NOT alter compiled graph.

### 9.5 Resource pooling (liveness)
- `firstUse` = first pass that writes (or reads if input-only). Feedback synthetic
  nodes set `firstUse=0`. Persistent surfaces `oN`: `firstUse=0, lastUse=Infinity`.
- `lastUse` = last pass that reads.
- Group by `(format,widthPx,heightPx,usageSignature)`.
- Freelist per group; reuse when `releaseFrame <= currentFrameCompilationId`.
- Mark reusable after `lastUse` unless global/persistent.
- Pool compaction every **N=60** compilations (`config.poolCompactionInterval`).
- **Deterministic allocation → stable binding order for reproducibility.**

### 9.6 Binding slot assignment
- **WebGL:** slots 0..N in pass input **declaration order**; max =
  `gl.MAX_TEXTURE_IMAGE_UNITS` (≥16 guaranteed); exceed → `ERR_TOO_MANY_TEXTURES`.
- **WebGPU:** group0 = textures (sampled/storage, declaration order), group1 = single
  consolidated UBO, group2 = reserved.

> PARITY HAZARD: **input declaration order** defines binding slots. Object key order
> in `inputs` matters. Unity must preserve the same iteration order
> (`Object.entries` insertion order) when binding textures, or samplers get swapped.

### 9.7 Diagnostics (DSL, `language.rst`) vs Error codes (`compiler.rst`)
**DSL diagnostics:** L001 unexpected char, L002 unterminated string, P001 unexpected
token, P002 expected `)`, S001 unknown ident (Error), S002 arg out of range
(Warning), S003 var used before assignment, S004 cannot assign null/undefined, S005
illegal chain structure, S006 starter chain missing write(), S007 deprecated param
alias (Warning), S008 deprecated effect (Warning), R001 runtime error.
> NOTE: the diagnostics table in `language.rst` lists **S005 twice** and has no S009;
> S004 appears after S005 (out of numeric order). Likely a doc typo. See §10.

**Compiler/pipeline error codes** (`compiler.rst`/`pipeline.rst §11`): `ERR_SYNTAX`,
`ERR_UNKNOWN_IDENT`, `ERR_ARG_TYPE`, `ERR_SCHEMA`, `ERR_DUP_PASS_NAME`,
`ERR_BAD_TEX_REF`, `ERR_PINGPONG_UNDECL`, `ERR_ITER_NO_PINGPONG`, `ERR_CYCLE`,
`ERR_COMPUTE_UNSUPPORTED_FEATURE`, `ERR_VIEWPORT_BOUNDS`, `ERR_WORKGROUP_LIMIT`,
`ERR_UNIFORM_COERCE`, `ERR_SURFACE_MULTIWRITE`, `ERR_COMPUTE_MRT_UNSUPPORTED`,
`ERR_READBACK_FORBIDDEN`, `ERR_TOO_MANY_TEXTURES`, `ERR_DIMENSION_INVALID`,
`ERR_FORMAT_UNSUPPORTED`, `ERR_SHADER_COMPILE`, `ERR_SHADER_LINK`, `ERR_NO_INPUT`,
`ERR_ENUM_INVALID`, `ERR_CONDITION_SYNTAX`, `ERR_CONTROL_FLOW_INVALID`.
Error object shape: `{ code, message, pass?, texture?, detail? }`. **Errors MUST be
stable across versions for tooling.**

---

## 10. Validation Rules & Lifecycle

### 10.1 Validation phases (`pipeline.rst §6`)
1. Structure (JSON schema) → `ERR_SCHEMA`
2. Name uniqueness (`passes[].name`) → `ERR_DUP_PASS_NAME`
3. Texture refs (declared texture or `oN` alias) → `ERR_BAD_TEX_REF`
4. Ping-pong integrity (both in `textures`) → `ERR_PINGPONG_UNDECL`
5. Iterations>1 without pingpong must be purely functional → `ERR_ITER_NO_PINGPONG`
6. Dependency cycles → `ERR_CYCLE`
7. WebGL compute storage/scatter → `ERR_COMPUTE_UNSUPPORTED_FEATURE`
8. Viewport bounds → `ERR_VIEWPORT_BOUNDS`
9. Workgroup product vs device limits → `ERR_WORKGROUP_LIMIT`
10. Uniform coercion "without precision loss beyond IEEE-754 single" →
    `ERR_UNIFORM_COERCE`

### 10.2 Shader compilation lifecycle
Triggers: first use of `program` name; source change (hot reload); `defines` change.
**Cache key: `hash(programName, backend, sortedDefines, version)`.**
- WebGL: compile FULLSCREEN_QUAD_VERT + frag (`injectDefines`), link, extract
  uniforms/samplers.
- WebGPU: `createShaderModule(injectDefines(...))`, await `getCompilationInfo`, build
  render or compute pipeline.
- Invalidation: source change → recompile next frame; **previous version stays active
  (no glitch)**; compile errors block new graph but preserve old.

### 10.3 Effect lifecycle FSM
`UNLOADED → VALIDATING → VALIDATED → COMPILING → READY` (`READY --execute--> READY`,
`READY --sourceChange--> STALE --recompile--> COMPILING`). Failures → `ERROR`
(`ERROR --fix--> UNLOADED`).

### 10.4 Hot-reload & error recovery
- **Atomic swap on next frame boundary; a frame MUST execute entirely with one graph
  version (no mid-frame swaps).** Cleanup old resources after **2-frame delay**.
- Runtime error per pass: catch → log → skip pass (and dependents) → substitute
  **magenta-checkerboard error texture** for failed outputs → continue independent
  passes. Validation errors prevent execution entirely; runtime errors allow partial
  degraded frame.

### 10.5 Determinism guarantees (§15) — pixel-parity foundation
- Hash of sorted pass list + resource allocation signature stable given identical
  effect + screen size.
- Identical `program`+`defines` → identical pipeline keys.
- Ping-pong ordering deterministic: `<passName>#<i>`.

### 10.6 Performance (informative, not pixel-relevant)
Compile <5ms for 200 passes (reference); texture reuse ≥70%; **no synchronous GPU
readback ever** (`ERR_READBACK_FORBIDDEN` — `gl.readPixels`/WebGPU `mapAsync` on
effect outputs within frame is blocked). This reinforces the "Zero CPU Readback"
guarantee — Unity port must likewise keep all state GPU-resident across frames.

### 10.7 Versioning
`version` opt-in; reserved future fields `buffers`, `feedback`, `async`, `subgraphs`;
tooling MUST ignore unknown top-level keys starting with `_`. v2 features require
explicit `version = "2.0.0"`.

---

## 11. Features index
`features.rst` is a **toctree only**, linking three sub-docs (NOT read here, out of
listed scope):
- `smrticles` — SMRTicles particle/agent system (uses `xyz/vel/rgba` surfaces; emit/
  render wrappers; agent-based effects pattern). Referenced by `language.rst` as
  `:ref:`SMRTicles <shader-smrticles>``.
- `midi-audio` — MIDI/audio live-input host integration.
- `demo-ui` — demo UI (tag rendering, categories).
> ACTION FOR ORCHESTRATOR: SMRTicles and midi-audio sub-docs contain agent-state and
> live-input semantics relevant to parity but were **not in this task's file list**.
> Recommend a follow-up spec pass on `docs/shaders/smrticles.rst` and
> `docs/shaders/midi-audio.rst` plus `effect-reference.rst`.

---

## 12. DISCREPANCIES & OPEN QUESTIONS (cross-check against code)

1. **Search default order.** `language.rst`: "Every program MUST begin with a
   `search` directive. There are no implicit defaults." `compiler.rst §2.1`: "If
   omitted, the default order `['synth','filter']` is used." → **Resolve from code:
   is `search` truly mandatory, or is `['synth','filter']` the fallback?**
2. **Namespace set.** `language.rst` lists `points`; `effects.rst` lists `sim`
   (temporal-state simulations) but not `points`. `synth3d` member effect lists also
   differ. → Reconcile against actual effect directories under `shaders/effects/`.
3. **Tag enum.** Schema `tags` enum vs prose tag table vs `tags.js` differ. **`tags.js`
   is authoritative** per `effects.rst`.
4. **Diagnostics numbering.** `language.rst` table lists **S005 twice**, S004 out of
   order, no S009. Likely doc artifact.
5. **`func` default.** `effects.rst §6` says `func` "defaults to lowercase `name`".
   Verify the exact lowercasing (full lowercase? camel? first-letter only?) in code.
6. **`render(o0)` directive vs `.write(o0)`.** Both exist. `render` sets *final output
   surface*; `write` writes a surface. Confirm what surface is presented to screen
   when `render` is omitted (default `o0`?).
7. **Coordinate origin / Y-flip.** Prose never states the texel origin convention.
   **MUST be resolved from backend code (`webgl2.js`/`webgpu.js`) and visual tests.**
   This is the single largest pixel-parity risk for the HLSL port.
8. **sRGB vs linear.** No mention of color-space handling on surfaces or final
   present. `rgba16f` surfaces are presumably linear; whether the final blit applies
   sRGB encode is unspecified — **resolve from code / canvas present path.**
9. **Color literal → float conversion** (`#RGB` nibble expansion, /255) unspecified.
10. **Oscillator/`noise`-osc formulas, animation duration constant (10s), `time`
    epoch** — all unspecified in prose; load-bearing for animated parity.
11. **Matrix convention** (row-major spec, transpose-on-upload) — verify HLSL port
    uses matching `mul()` order; `mat3` packing loop indexes `value[col*3+row]`
    (column-major source array) — confirm against effect-spec authoring convention.
12. **First-frame buffer contents.** `(F-1)%2` for F=0 reads buffer 1 — confirm both
    double buffers are cleared to transparent black at init.

---

## 13. Pixel-Parity Hazard Summary (ranked)

1. **Coordinate origin / Y-flip** — WebGL bottom-left vs WebGPU/D3D top-left;
   full-screen-triangle UV mapping; `gl_FragCoord`→`floor` compute emulation.
   Resolve from code + visual tests. (§5.5, §5.6, §12.7)
2. **Surface precision `rgba16f` (half-float)** — match `ARGBHalf`, not `ARGBFloat`;
   half-float rounding compounds across feedback frames. (§8.6)
3. **Double-buffer parity `read=(F-1)%2, write=F%2`** + first-frame contents +
   `frame` start value. (§6.4, §12.12)
4. **Matrix row-major spec + std140 packing** vs HLSL column-major / `mul` order.
   (§7.1)
5. **std140 `vec3` 16-byte padding** vs HLSL cbuffer packing rules. (§7.1)
6. **Ping-pong iteration swap schedule** (odd/even final buffer; iteration-0 reads
   original input) + injected `_iteration` uniform. (§9.2)
7. **Deterministic topo-sort tie-breaking** + deterministic pool allocation
   (insertion order). (§9.3, §9.5)
8. **Texture binding slot = input declaration order** (object key order). (§9.6)
9. **Dimension `floor` rounding** for `%`/`scale` textures. (§8.5)
10. **Time/frame/deltaTime origin + animation duration (10s)** for oscillators and
    frame-driven RNG. (§4.3, §5.4)
11. **Enum/wrap-mode integer mappings** drive sampler addressing — must match
    `std_enums.js`. (§4.1)
12. **sRGB/linear present path** — unspecified in prose. (§12.8)
