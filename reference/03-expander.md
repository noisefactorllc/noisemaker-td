# 03 — Expander (Logical Graph → Render Graph)

Reference for re-implementing `shaders/src/runtime/expander.js` and
`shaders/src/runtime/palette-expansion.js` in C#/HLSL with bit-identical
graph construction. The expander is **pure data transformation** — no GPU
calls, no floating-point math on pixel data. It rewrites a compiled DSL
program (the *Logical Graph*) into a flat list of GPU passes plus program
specs, texture specs, and a render-surface name (the *Render Graph*). Get
this exactly right and downstream backends produce identical output.

---

## 1. Entry point and return shape

```js
export function expand(compilationResult, options = {}) -> {
  passes,         // Array<Pass>          ordered GPU passes
  errors,         // Array<{message, step?}>
  programs,       // { [programName]: Program }
  textureSpecs,   // { [virtualTexId]: TextureSpec }
  renderSurface   // string | null   surface name to present, e.g. "o0"
}
```

`options.shaderOverrides` (optional): `{ [stepTemp:number]: shadersSource }`,
a per-step override of `effectDef.shaders`. Keyed by `step.temp` (NOT by chain
index). Default `{}`.

### 1.1 Input: `compilationResult`

```
compilationResult = {
  plans:       Array<Plan>,          // one entry per top-level chain
  diagnostics: ...,                  // unused by expander
  render:      string | null         // explicit render() target surface name, e.g. "o2"
}
```

```
Plan = {
  chain: Array<Step>,                // ordered effect steps
  write: (string | { name, kind }) | null   // final .write(oN) target
}
```
`plan.write` may be a bare string (`"o0"`) or an object `{ name, kind }`
where `kind ∈ {"output","feedback"}` (default treated as `"output"`).

```
Step = {
  temp:    number,             // UNIQUE step id across all plans; basis for nodeId
  from:    number | null,      // temp of upstream step feeding this one; null = generator
  op:      string,             // effect name (e.g. "blur") OR builtin op ("_read","_write",...)
  builtin: boolean | undefined,// true for builtin ops
  args:    { [argName]: ArgValue } | undefined
}
```

`ArgValue` is one of:
- a primitive (number / string / bool) — a literal uniform value, OR
- an object. Object args carry a `kind` and/or a `value`:
  - **Surface/texture refs**: `{ kind, name }` with
    `kind ∈ {"temp","output","source","feedback","vol","geo","xyz","vel","rgba"}`.
    For `kind:"temp"`, the object also has `index` (the upstream step temp).
    For others, `name` is the surface name (`"o0"`, `"vol3"`, `"none"`, …).
  - **Wrapped literal**: `{ value: <primitive> }` (and possibly other fields).
    Detected by `('value' in arg)`. Used by member/enum args too.
- `step.args._skip === true` is a sentinel meaning "skip this effect"
  (passthrough, no passes).

> Effect definitions are looked up by name via `getEffect(effectName)` from the
> registry. The expander never inspects shader text; it only reads structured
> fields. See §3 for the effect-definition shape.

---

## 2. Output object shapes

### 2.1 `Pass` (effect-generated)

```js
pass = {
  id,             // `${nodeId}_pass_${i}`
  program,        // program key into `programs`: `${nodeId}_${passDef.program}${defineSuffix}`
  entryPoint,     // passDef.entryPoint (multi-entry compute) | undefined
  drawMode,       // passDef.drawMode  ("points" for deposit passes) | undefined
  drawBuffers,    // passDef.drawBuffers (MRT) | undefined
  count,          // passDef.count (vertex/instance count) | undefined
  countUniform,   // passDef.countUniform (dynamic count from a uniform) | undefined
  repeat,         // passDef.repeat (iterations per frame) | undefined
  blend,          // passDef.blend | undefined
  workgroups,     // passDef.workgroups (compute) | undefined
  storageBuffers, // passDef.storageBuffers | undefined
  storageTextures,// passDef.storageTextures | undefined
  inputs:  { [samplerUniformName]: virtualTexId|'none' },
  outputs: { [attachment]: virtualTexId },   // attachment "color","color1"... per MRT
  uniforms:{ [uniformName]: value },         // flat name->value map (NOT vec4-packed here)
  // metadata:
  effectKey,        // effectName (the op string)
  effectFunc,       // effectDef.func || effectName
  effectNamespace,  // effectDef.namespace || null
  nodeId,           // `node_${step.temp}`
  stepIndex,        // step.temp
  inheritsVolumeSize, // true when currentInput3d set AND pipelineUniforms.volumeSize defined
  uniformSpecs,     // { [uniformName]: { min, max } } for %-automation scaling
  scopedParams      // { [origParam]: scopedParam } present only when scopedParamMap nonempty
}
```
Note: effect passes do **not** carry a `type` field; the backend infers
render vs compute from the program/passDef. Blit passes (§2.2) DO carry
`type:"render"`.

### 2.2 `Pass` (blit — internal copy)

Emitted by `_write`/`_write3d` and the final-chain blit. Uses the shared
`"blit"` program.

```js
blitPass = {
  id,            // `${nodeId}_write_blit` | `${nodeId}_write3d_vol_blit` |
                 // `${nodeId}_write3d_geo_blit` | `final_blit_${outName}`
  program: 'blit',
  type: 'render',
  inputs:  { src: <currentInput virtualTexId> },
  outputs: { color: <targetSurface> },
  uniforms: {},
  nodeId,        // present except on final_blit
  stepIndex      // = step.temp; present except on final_blit
}
```

### 2.3 `Program`

For effect programs:
```js
programs[uniqueProgName] = {
  ...shaders,             // spread of the per-program shader object (fragment/wgsl/vertex/
                          //   entryPoint/fragmentEntryPoint etc. as authored)
  uniformLayout,          // effectDef.uniformLayouts[progName] || effectDef.uniformLayout
  defines                 // { ...compileTimeDefines }  (shallow copy)
}
```
`uniqueProgName = `${nodeId}_${progName}${programDefineSuffix}``.

The built-in `"blit"` program (registered lazily via `ensureBlitProgram`):
```js
programs['blit'] = {
  fragment: `#version 300 es ... fragColor = texture(src, v_texCoord);`,
  wgsl: `... let uv = vec2<f32>(in.uv.x, 1.0 - in.uv.y); return textureSample(src, srcSampler, uv);`,
  fragmentEntryPoint: 'main'
}
```
**PARITY HAZARD (Y-flip):** the blit WGSL flips V (`1.0 - in.uv.y`) but the
GLSL does **not**. This encodes the WebGPU↔WebGL2 framebuffer-origin
difference at the blit boundary. A Unity/HLSL port must decide a single
canonical texture origin and apply the equivalent flip exactly where the
WGSL path does, not where GLSL does. (HLSL/D3D and Unity render textures are
top-left origin like WebGPU NDC-flipped; OpenGL is bottom-left.)

### 2.4 `TextureSpec`

```js
textureSpecs[virtualTexId] = {
  ...spec,         // copy of effectDef.textures[name] or textures3d[name]
  // for 3D: is3D: true added
  // width/height may be rewritten to scoped param refs (see §6.3)
}
```
A dimension (`width`/`height`/`depth`) is one of:
- a number,
- `"screen"` / `"auto"` (→ canvas size),
- a `"N%"` string,
- `{ param: string, paramDefault?, multiply?, power?, default? }`,
- `{ screenDivide: string, default? }`,
- `{ scale: number, clamp?: { min?, max? } }`.

`spec.format` is a backend format token (`"rgba16f"`, `"rgba32f"`, …);
default when unspecified is `"rgba16f"` (applied by pipeline, not expander).

`resolveDimension(spec, screenSize, uniforms)` (in pipeline.js, reproduced
here for parity since the scoping logic in the expander only matters once you
know how it is consumed):
1. number → `max(1, floor(spec))`.
2. `"screen"|"auto"` → `screenSize`.
3. `"N%"` → `max(1, floor(screenSize * parseFloat(N) / 100))`.
4. `{param}`: `value = uniforms[param] ?? (paramDefault ?? 64)`; if `multiply`
   present `value *= multiply`; if `power` present `value = pow(value,power)`;
   if a transform was applied AND param missing from uniforms AND `default`
   set, `value = default`; return `max(1, floor(value))`.
5. `{screenDivide}`: `divisor = uniforms[screenDivide] ?? default ?? 1`;
   return `max(1, round(screenSize / divisor))`. **Note `round` here vs
   `floor` elsewhere — replicate exactly.**
6. `{scale}`: `floor(screenSize*scale)`, clamp min/max, then `max(1,…)`.
7. fallback → `screenSize`.

---

## 3. Effect definition fields consumed by the expander

```
effectDef = {
  namespace,       // string | undefined  -> pass.effectNamespace
  func,            // string | undefined  -> pass.effectFunc (fallback effectName)
  globals,         // { [paramName]: GlobalDef }
  uniformLayout,   // legacy single layout (per-program fallback)
  uniformLayouts,  // { [progName]: layout }   per-program; takes precedence
  shaders,         // { [progName]: ShaderSource }
  passes,          // Array<PassDef>
  textures,        // { [texName]: TextureSpec }
  textures3d,      // { [texName]: TextureSpec3D }
  externalTexture, // string | undefined (camera/video input sampler name)
  // passthrough output declarations (no pass needed):
  outputTex,  outputTex3d, outputGeo, outputXyz, outputVel, outputRgba
}
```

```
GlobalDef = {
  uniform,         // shader uniform name (defaults to the param name if absent)
  default,         // default value
  type,            // 'float'|'int'|'member'|'surface'|'palette'| ...
  min, max,        // numeric range (for uniformSpecs; default min=0,max=100)
  choices,         // truthy => excluded from uniformSpecs
  define,          // string MACRO_NAME => compile-time #define instead of uniform
  colorModeUniform,// name of an int uniform set to 0/1 based on surface=='none'
  // for type:'palette' the uniform holds a 1-based index expanded via §7
}
```

```
PassDef = {
  program,         // program key suffix (joined with nodeId)
  entryPoint, drawMode, drawBuffers, count, countUniform, repeat, blend,
  workgroups, storageBuffers, storageTextures,
  inputs:  { [samplerUniform]: texRef },   // texRef strings, see §5
  outputs: { [attachment]:    texRef },     // attachment "color","color1"…
  uniforms:{ [shaderUniform]: globalParamName }  // pass-level uniform wiring, see §4.4
}
```

```
ShaderSource (per program) = { fragment?, glsl?, source?, wgsl?, vertex?,
                               entryPoint?, fragmentEntryPoint?, ... }
```

`uniformLayout` shape (vec4 packing — consumed by backends, not by expander
math, but the expander forwards it onto the program object):
```
uniformLayout = { [uniformName]: { slot: number, components: 'x'|'y'|'z'|'w'|'xy'|'xyz'|'xyzw'|... } }
```
Each `slot` is one `vec4` register; `components` selects which lanes that
uniform occupies. A scalar → one component; a vec3 → `'xyz'`. Multiple
uniforms can share a slot (e.g. `bgColor:'xyz'` + `bgAlpha:'w'`). Backends pack
the flat `pass.uniforms` map into a `vec4[]` UBO/array using this layout.

---

## 4. Top-level control flow

`expand` iterates `compilationResult.plans` (index = `planIndex`). For each
plan it walks `plan.chain` left→right, maintaining mutable "current" pipeline
cursors and accumulators (all reset per plan):

```
currentInput, currentInput3d, currentInputGeo, currentInputXyz,
currentInputVel, currentInputRgba   // virtualTexId of the live texture per pipeline lane
lastInlineWriteTarget               // {kind,name} or null — to dedupe final blit
currentParticlePipelineId           // nodeId of the active particle-pipeline owner, or null
pipelineUniforms = {}               // accumulates uniform values for downstream inheritance
chainScopeId = `chain_${planIndex}` // scope tag for chain-global textures
```

Global accumulators (persist across plans): `passes`, `errors`, `programs`,
`textureSpecs`, `textureMap` (Map: logical id → virtual texture id),
`lastWrittenSurface`.

`resolveEnum(path)` walks `stdEnums` by dotted path, returning
`node.value` (else null). Used to turn enum member strings into ints.

For each `step`:

### 4.1 Builtin ops (handled first, `continue` after each)

- **`_read`**: if `step.args.tex.kind === 'output'`, set
  `currentInput = `global_${tex.name}`` (e.g. `global_o0`). Register
  `textureMap[`${nodeId}_out`] = currentInput`.
- **`_read3d`**: read `tex3d` and `geo` from args. If `tex3d.kind==='vol'`
  or `tex3d.type==='VolRef'` → `currentInput3d = `global_${tex3d.name}``,
  else `currentInput3d = tex3d.name || tex3d`. Same for `geo`
  (`kind==='geo'`/`type==='GeoRef'` → `global_${name}`). Register
  `_out3d`/`_outGeo`.
- **`_write`**: target = `step.args.tex`. If `tex.name !== 'none'` AND
  `currentInput` set: `targetSurface = `global_${tex.name}``; **only if**
  `currentInput !== targetSurface`, push a blit pass copying `currentInput`→
  `targetSurface`, call `ensureBlitProgram`, set `lastWrittenSurface = tex.name`,
  set `lastInlineWriteTarget = { kind: tex.kind, name: tex.name }`. Then
  passthrough: `textureMap[`${nodeId}_out`] = currentInput` (currentInput
  unchanged — write is chainable).
- **`_write3d`**: blits `currentInput3d`→`global_${tex3d.name}` and
  `currentInputGeo`→`global_${geo.name}` (each skipped on `name==='none'` or
  already-equal). Passes through all three lanes (`_out`,`_out3d`,`_outGeo`).
- **`_subchain_begin` / `_subchain_end`**: pure metadata; call
  `registerPassthrough` (copies all live lane textures into this node's
  `_out*` map keys) and continue. Subchains/loops produce NO passes
  themselves — looping is expressed via `passDef.repeat` and via the chain
  topology, not via expander iteration.

### 4.2 `lastInlineWriteTarget` reset

After the builtin block, before processing a real effect:
`lastInlineWriteTarget = null`. (Any non-write step invalidates the
final-blit dedupe.)

### 4.3 `_skip` flag

If `step.args._skip === true`: `registerPassthrough(...)`, continue. The node
gets `_out*` entries but emits no passes/programs/textures.

### 4.4 Real effect — ordered phases

For `effectName = step.op`, `effectDef = getEffect(effectName)` (push error
`"Effect '<name>' not found"` and continue if missing). `nodeId =
`node_${step.temp}``. Then, **in this exact order**:

1. **Particle-pipeline scope detection.**
   `createsParticleTextures = effectDef.textures && effectDef.textures.global_xyz`.
   If true: `currentParticlePipelineId = nodeId`, and reset
   `currentInputXyz/Vel/Rgba = null` (new particle pipeline starts fresh).
2. **Compile-time defines** (§4.5).
3. **Program collection** (§4.6).
4. **Texture-spec collection** 2D (§6) then 3D.
5. **Resolve input cursor:** if `step.from !== null`,
   `currentInput = textureMap.get(`node_${step.from}_out`)`.
6. **Globals → pipelineUniforms (defaults + colorMode)** (§4.7).
7. **Args first pass (surface colorMode)** (§4.8).
8. **Args second pass (non-surface uniforms)** (§4.8).
9. **Per-pass expansion loop** (§4.9) — builds each `Pass`.
10. **Update pipeline cursors** from this node's `_out*` map entries, then
    apply explicit `outputTex/outputTex3d/outputGeo/outputXyz/outputVel/
    outputRgba` passthrough declarations (§4.10).

### 4.5 Compile-time defines (#define injection)

```
compileTimeDefines = {}
for globalName in sort(keys(effectDef.globals)):        // SORTED — deterministic
  def = globals[globalName]; if !def.define continue
  value = def.default
  if step.args has globalName:
     argVal = step.args[globalName]
     value = (argVal is object && 'value' in argVal) ? argVal.value : argVal
  if def.type === 'member' && typeof value === 'string':
     resolved = resolveEnum(value); if resolved !== null value = resolved
  if value != null: compileTimeDefines[def.define] = value
programDefineSuffix = sortedEntries(compileTimeDefines)
                        .map(([k,v]) => `__${k}_${v}`).join('')
```
The suffix makes each distinct define-combination a distinct program cache
entry (e.g. `__NOISE_TYPE_2`). `LOOP_OFFSET`, `NOISE_TYPE` etc. are exactly
this mechanism: a `global` with a `define` field. Injection into source
(WebGL2 `injectDefines`):
```
"#version 300 es\nprecision highp float;\nprecision highp int;\n"
 + for each (k,v): "#define k v\n"
 + source-with-its-own-#version-line-stripped
```
**PARITY HAZARD:** `#version`/`precision` directives are prepended; the
original source's `#version` line is removed via regex `/^\s*#version.*$/m`.
WGSL has no preprocessor — defines become module-scope `const` (handled in
the WGSL backend / manifest). For HLSL, emit `#define`s identically and use
the same value stringification (`v` is JS `String(v)`; numbers print without
trailing zeros, e.g. `1` not `1.0`).

### 4.6 Program collection

`shadersSource = shaderOverrides[step.temp] || effectDef.shaders`. For each
`[progName, shaders]`:
```
uniqueProgName = `${nodeId}_${progName}${programDefineSuffix}`
if !programs[uniqueProgName]:
   programLayout = effectDef.uniformLayouts?.[progName] || effectDef.uniformLayout
   programs[uniqueProgName] = { ...shaders, uniformLayout: programLayout, defines: {...compileTimeDefines} }
```
ALL programs get the `nodeId` prefix to avoid cross-effect collisions (two
effects can both define a program named `"agent"`).

### 4.7 Globals → pipelineUniforms defaults

For each `[globalName, def]` in `effectDef.globals`:
- If `def.uniform && def.default !== undefined`: **only if**
  `pipelineUniforms[def.uniform]` is still undefined, compute `val =
  def.default`; if `def.type==='member' && typeof val==='string'` resolve via
  `resolveEnum`; set `pipelineUniforms[def.uniform] = val`. (Upstream values
  are preserved — this is how `volumeSize` flows noise3d → cellularAutomata3d.)
- If `def.type==='surface' && def.colorModeUniform`: **only if** `step.args`
  does NOT contain `globalName`, set
  `pipelineUniforms[def.colorModeUniform] = (def.default==='none') ? 0 : 1`.

### 4.8 Args processing (two passes)

`colorModeControlledUniforms = new Set()`.

**First pass** — surfaces only: for each `[argName, arg]` where `arg` is an
object with `kind ∈ {temp,output,source,feedback,xyz,vel,rgba}`: if
`effectDef.globals[argName].colorModeUniform` exists, set that uniform to
`(arg.name==='none') ? 0 : 1` and add it to `colorModeControlledUniforms`.

**Second pass** — non-surface: for each arg:
- Skip object args with a surface `kind` (handled above).
- `uniformName = effectDef.globals[argName]?.uniform || argName`.
- Skip if `colorModeControlledUniforms.has(uniformName)`.
- Skip if `uniformName === 'volumeSize' && currentInput3d &&
  pipelineUniforms.volumeSize !== undefined` (inherit upstream volume size).
- `resolvedValue = (isObject && arg.value !== undefined) ? arg.value : arg`.
- `pipelineUniforms[uniformName] = resolvedValue`.

### 4.9 Per-pass expansion

`effectPasses = effectDef.passes || []`. For `i` in `0..n-1`:
1. `passId = `${nodeId}_pass_${i}``;
   `programName = `${nodeId}_${passDef.program}${programDefineSuffix}``.
2. Build `pass` skeleton copying the `passDef` pass-through fields (§2.1) plus
   metadata (`effectKey/effectFunc/effectNamespace/nodeId/stepIndex`).
3. If `currentInput3d && pipelineUniforms.volumeSize !== undefined` set
   `pass.inheritsVolumeSize = true`.
4. `pass.uniforms = { ...pipelineUniforms }` (snapshot of inherited values).
5. **Defaults fill:** for each global with `uniform && default!==undefined`,
   if `pass.uniforms[uniform]` undefined: resolve member enum, set both
   `pass.uniforms[uniform]` and `pipelineUniforms[uniform]`.
6. **uniformSpecs:** `pass.uniformSpecs = {}`; for each global,
   `uniformName = def.uniform || argName`; if `(def.type==='float' ||
   def.type==='int') && !def.choices`: `pass.uniformSpecs[uniformName] =
   { min: def.min ?? 0, max: def.max ?? 100 }`.
7. **Args → uniforms** (same skip rules as §4.8 second pass, plus the
   colorMode-controlled skip computed inline): set `pass.uniforms[uniformName]`
   and `pipelineUniforms[uniformName] = resolvedValue`.
8. **Pass-level uniform wiring** (`passDef.uniforms`): for each
   `[uniformName, globalRef]`, look up the value in priority order:
   `pipelineUniforms[uniformName]` → `pipelineUniforms[globalRef]` →
   `effectDef.globals[globalRef].default` (with member-enum resolution). The
   first defined wins; sets `pass.uniforms[uniformName]`.
9. **Palette expansion** (§7): for each global with `type==='palette'`,
   `uniformName = def.uniform || argName`; `index = pass.uniforms[uniformName]`;
   if it's a number call `expandPalette(index)`; for each expanded uniform that
   **already exists** in `pass.uniforms`, overwrite it (arrays sliced) and mirror
   into `pipelineUniforms`.
10. **Inputs mapping** (§5).
11. **Outputs mapping** (§5).
12. **Scoped-param propagation** (§6.3): for `[orig,scoped]` in
    `scopedParamMap`, if `pass.uniforms[orig]` defined, copy to
    `pass.uniforms[scoped]` and `pipelineUniforms[scoped]`. If `scopedParamMap`
    nonempty, `pass.scopedParams = Object.fromEntries(scopedParamMap)`.
13. `passes.push(pass)`.

### 4.10 Cursor updates after passes

```
currentInput = textureMap.get(`${nodeId}_out`)
if effectDef.outputTex && !currentInput:           // explicit 2D passthrough
   if outputTex === 'inputTex': restore from node_${step.from}_out
   else virtualTexId = startsWith('global_') ? scopeChainTex(name) : `${nodeId}_${name}`
        register node_${nodeId}_out = virtualTexId; currentInput = it
out3d = textureMap.get(`${nodeId}_out3d`); if set currentInput3d = out3d
outXyz/outVel/outRgba similarly update their cursors
// then explicit declarations (only when the corresponding _out* not already set):
effectDef.outputTex3d (=='inputTex3d' => reuse currentInput3d; else node/chain-scope)
effectDef.outputGeo   (=='inputGeo'   => reuse currentInputGeo; else node-scope ONLY — no global handling)
effectDef.outputXyz/Vel/Rgba (=='inputX' => reuse; else global_->scopeChainTex else node-scope)
```

### 4.11 Final chain output (`plan.write`)

After the chain loop, if `plan.write && currentInput`:
```
outName = typeof plan.write==='object' ? plan.write.name : plan.write
lastWrittenSurface = outName
alreadyWritten = lastInlineWriteTarget &&
                 lastInlineWriteTarget.kind==='output' &&
                 lastInlineWriteTarget.name===outName
if alreadyWritten: continue (no blit)
targetSurface = `global_${outName}`
if currentInput !== targetSurface:
   push final_blit { id:`final_blit_${outName}`, program:'blit', type:'render',
                     inputs:{src:currentInput}, outputs:{color:targetSurface}, uniforms:{} }
```
Note: the final blit does **not** call `ensureBlitProgram`. It relies on a
prior `_write`/`_write3d` having registered `programs['blit']`, OR on the
last-pass output optimization (§5.3) which writes directly to the surface so
no blit is needed. If a chain ends with a passthrough into a global and no
write program was ever registered, the backend must still know the `blit`
program — re-implementations should register blit whenever any `final_blit`
is emitted (matching observed runtime behavior, where last-pass optimization
usually removes the need).

### 4.12 Render surface resolution

```
if compilationResult.render: renderSurface = compilationResult.render
elif lastWrittenSurface:     renderSurface = lastWrittenSurface
else: push error "No render surface specified..."; renderSurface = null
```

---

## 5. Texture reference resolution (inputs & outputs)

`SURFACE_REF_PATTERN = /^(?:o|vol|geo|xyz|vel|rgba)[0-7]$/` — matches user
surface names o0..o7, vol0..7, geo0..7, xyz0..7, vel0..7, rgba0..7.

`resolveGlobalSurfaceRef(name)`: `'none'`→`'none'`; `global_*`→unchanged;
matches `SURFACE_REF_PATTERN`→`global_${name}`; else unchanged.

### 5.1 Inputs (`passDef.inputs[uniformName] = texRef`)

Resolution order (first match wins):
1. **Pipeline 2D**: `texRef==='inputTex'` OR (`startsWith('o')` and
   `!isNaN(parseInt(texRef.slice(1)))`) → `currentInput || texRef`.
2. `'inputTex3d'` → `currentInput3d || texRef`.
3. `'inputGeo'` → `currentInputGeo || texRef`.
4. `'inputXyz' / 'inputVel' / 'inputRgba'` → corresponding cursor `|| texRef`.
5. `'noise'` → `'global_noise'`.
6. `'midiNoteGrid'` → `'midiNoteGrid'`.
7. `'feedback'` or `'selfTex'` (alias): if `plan.write` set, resolve to
   `${prefix}_${outName}` where `prefix = (outKind==='feedback') ? 'feedback'
   : 'global'`, `outKind = plan.write.kind || 'output'`; else
   `currentInput || 'global_inputTex'`. **This is the feedback / double-buffer
   path** — a pass samples the same surface it writes to; the backend must
   ping-pong (read previous frame, write current).
8. `effectDef.externalTexture && texRef===externalTexture` →
   `${texRef}_step_${step.temp}` (per-instance camera/video texture).
9. `step.args` has key `texRef`: the arg is a texture binding.
   - `arg == null` → skip (intentionally unbound input).
   - `arg.kind==='temp'` → `textureMap.get(`node_${arg.index}_out`)`.
   - `arg.kind ∈ {output,source,vol,geo,xyz,vel,rgba}` →
     `arg.name==='none' ? 'none' : `global_${arg.name}``.
   - `typeof arg === 'string'` → `resolveGlobalSurfaceRef(arg)`.
10. `effectDef.globals[texRef].default !== undefined`: resolve the default —
    `'none'`→`'none'`; `'inputTex'|'inputColor'`→`currentInput || default`;
    `SURFACE_REF_PATTERN`→`global_${default}`; `global_*`→`scopeChainTex`;
    else the literal default string.
11. `texRef.startsWith('global_')` → `scopeChainTex(texRef)`.
12. `texRef==='outputTex'` → `${nodeId}_out` (self-reference, feedback).
13. **fallback** → `${nodeId}_${texRef}` (node-local internal texture).

### 5.2 Outputs (`passDef.outputs[attachment] = texRef`)

`attachment` is the FBO/MRT slot key (`"color"`, `"color1"`, …; the order of
`drawBuffers` defines MRT layout). Resolution:
- `'outputTex'` → main 2D output. **Last-pass optimization (§5.3).** Otherwise
  `${nodeId}_out`. Registers both `textureMap[virtualTex]=virtualTex` and
  `textureMap[`${nodeId}_out`]=virtualTex`.
- `'outputTex3d'` → `${nodeId}_out3d` (registered).
- `'outputXyz'/'outputVel'/'outputRgba'` → `${nodeId}_outXyz/_outVel/_outRgba`.
- `'inputTex3d'` → `currentInput3d || `${nodeId}_inputTex3d`` (write-back).
- `'inputGeo'/'inputXyz'/'inputVel'/'inputRgba'` → corresponding cursor
  `|| `${nodeId}_input<X>``.
- `startsWith('global_')` → `scopeChainTex(texRef)`.
- `startsWith('feedback_')` → unchanged.
- fallback → `${nodeId}_${texRef}`.

**MRT:** an effect declares several `outputs` entries on one pass plus
`drawBuffers`; agent effects write multiple state textures (xyz/vel/rgba) in
one compute/MRT pass. The expander just maps each attachment independently.

### 5.3 Last-pass-to-surface optimization (CRITICAL for parity)

When `texRef==='outputTex'` AND this is the last step in the chain
(`step === plan.chain[len-1]`) AND last pass (`i===effectPasses.length-1`) AND
`plan.write` set:
```
outName = object? plan.write.name : plan.write
outKind = plan.write.kind || 'output'
prefix  = (outKind==='feedback') ? 'feedback' : 'global'
virtualTex = `${prefix}_${outName}`
lastWrittenSurface = outName
```
i.e. the final effect renders **directly** into the global surface, skipping a
copy. This means `final_blit` (§4.11) is usually elided. A port must reproduce
this fusion or pixel-identical timing/feedback semantics may diverge (a
feedback chain that double-buffers vs. blits behaves differently on frame 0).

---

## 6. Texture scoping (multi-pipeline / multi-chain isolation)

Virtual texture ids namespace textures so multiple chains/pipelines don't
collide on shared `global_` keys.

### 6.1 Particle textures

`isParticleTex` matches `/^global_(xyz|vel|rgba|points_trail|life_data)$/`.
`scopeParticleTex(name)`: if `currentParticlePipelineId` set and name matches,
→ `${name}_${currentParticlePipelineId}`; else unchanged.

### 6.2 Chain textures

`scopeChainTex(name)`: particle scoping takes priority; else if
`startsWith('global_')` → `${name}_${chainScopeId}` (`chain_${planIndex}`);
else unchanged.

Texture-spec collection (§4.4 step 4) uses these rules to compute
`virtualTexId`:
- `global_` + particle + active particle pipeline → `…_${pipelineId}`.
- `global_` non-particle → `…_${chainScopeId}` (always chain-scoped).
- non-`global_` → `${nodeId}_${texName}` (node-local).
3D specs: `global_` → `scopeChainTex`, else `${nodeId}_${name}`; `is3D:true` added.

### 6.3 Scoped param references (sizing parity)

When a texture is scoped, any `width/height` dimension that is a
`{param}` or `{screenDivide}` reference must also be scoped so each
scope looks up its OWN sizing uniform. `scopedParamMap: Map<orig,scoped>` is
populated, and the scope suffix is `currentParticlePipelineId` (particle) else
`chainScopeId`. `shouldScopeParams` is true when ANY of:
- particle-scoped, OR chain-scoped (`global_` non-particle), OR
- (`currentParticlePipelineId` set AND non-`global_` texture), OR
- the spec's width/height references a param (`hasParamRef`).

`scopeDimSpec` rewrites `{param:'stateSize'}` → `{param:'stateSize_node_7'}`
(or `_chain_0`) and records the mapping. Same for `screenDivide`. The recorded
mapping drives §4.9 step 12: the unscoped uniform value is copied to the
scoped name so `resolveDimension` finds it. **Within-chain inheritance still
works** because `pipelineUniforms` keeps propagating the unscoped value too.

---

## 7. Palette expansion (`palette-expansion.js`)

Legacy support for the `classicNoisedeck` namespace only. A `type:'palette'`
global holds a **1-based** integer index; `expandPalette(index)` maps it to
five concrete uniforms.

```js
expandPalette(index):
  if index <= 0 || index > 55 → return null
  entry = PALETTES[index - 1]
  return {
    paletteOffset: entry.offset.slice(),   // vec3
    paletteAmp:    entry.amp.slice(),      // vec3
    paletteFreq:   entry.freq.slice(),     // vec3
    palettePhase:  entry.phase.slice(),    // vec3
    paletteMode:   entry.mode              // int
  }
```

`PALETTES` is a fixed 55-entry array; each entry
`{ amp:[3], freq:[3], offset:[3], phase:[3], mode:int }`. `mode` is already in
classicNoisedeck convention: **0=none, 1=hsv, 2=oklab, 3=rgb**. (Mapping from
the modern filter/palette modes: filter 0 rgb→classic 3, filter 1 hsv→classic
1, filter 2 oklab→classic 2.)

The cosine-palette shader math is `color = offset + amp * cos(2π*(freq*t +
phase))` evaluated in the space named by `mode` — but that math lives in the
classicNoisedeck shaders, not here; the expander only supplies the constants.

**Full table (index : name (mode) : amp / freq / offset / phase):**
The 55 entries are reproduced verbatim from the source; copy these floats
EXACTLY (several are non-round, e.g. `0.56851584`).

```
1  seventiesShirt (3) amp[.76,.88,.37] freq[1,1,1] off[.93,.97,.52] ph[.21,.41,.56]
2  fiveG (3)          amp[.56851584,.7740668,.23485267] freq[1,1,1] off[.5,.5,.5] ph[.727029,.08039695,.10427457]
3  afterimage (3)     amp[.5,.5,.5] freq[1,1,1] off[.5,.5,.5] ph[.3,.2,.2]
4  barstow (3)        amp[.45,.2,.1] freq[1,1,1] off[.7,.2,.2] ph[.5,.4,0]
5  bloob (3)          amp[.09,.59,.48] freq[1,1,1] off[.2,.31,.98] ph[.88,.4,.33]
6  blueSkies (3)      amp[.5,.5,.5] freq[1,1,1] off[.1,.4,.7] ph[.1,.1,.1]
7  brushedMetal (3)   amp[.5,.5,.5] freq[1,1,1] off[.5,.5,.5] ph[0,.1,.2]
8  burningSky (3)     amp[.7259015,.7004237,.9494409] freq[1,1,1] off[.63290054,.37883538,.29405284] ph[0,.1,.2]
9  california (3)     amp[.94,.33,.27] freq[1,1,1] off[.74,.37,.73] ph[.44,.17,.88]
10 columbia (3)       amp[1,.7,1] freq[1,1,1] off[1,.4,.9] ph[.4,.5,.6]
11 cottonCandy (3)    amp[.51,.39,.41] freq[1,1,1] off[.59,.53,.94] ph[.15,.41,.46]
12 darkSatin (1)      amp[0,0,.51] freq[1,1,1] off[0,0,.43] ph[0,0,.36]
13 dealerHat (3)      amp[.83,.45,.19] freq[1,1,1] off[.79,.45,.35] ph[.28,.91,.61]
14 dreamy (3)         amp[.5,.5,.5] freq[1,1,1] off[.5,.5,.5] ph[0,.2,.25]
15 eventHorizon (3)   amp[.5,.5,.5] freq[1,1,1] off[.22,.48,.62] ph[.1,.3,.2]
16 ghostly (1)        amp[.02,.92,.76] freq[1,1,1] off[.51,.49,.51] ph[.71,.23,.66]
17 grayscale (3)      amp[.5,.5,.5] freq[2,2,2] off[.5,.5,.5] ph[1,1,1]
18 hazySunset (3)     amp[.79,.56,.22] freq[1,1,1] off[.96,.5,.49] ph[.15,.98,.87]
19 heatmap (3)        amp[.75804377,.62868536,.2227562] freq[1,1,1] off[.35536355,.12935615,.17060602] ph[0,.25,.5]
20 hypercolor (3)     amp[.79,.5,.23] freq[1,1,1] off[.75,.47,.45] ph[.08,.84,.16]
21 jester (3)         amp[.7,.81,.73] freq[1,1,1] off[.1,.22,.27] ph[.99,.12,.94]
22 justBlue (3)       amp[.5,.5,.5] freq[0,0,1] off[.5,.5,.5] ph[.5,.5,.5]
23 justCyan (3)       amp[.5,.5,.5] freq[0,1,1] off[.5,.5,.5] ph[.5,.5,.5]
24 justGreen (3)      amp[.5,.5,.5] freq[0,1,0] off[.5,.5,.5] ph[.5,.5,.5]
25 justPurple (3)     amp[.5,.5,.5] freq[1,0,1] off[.5,.5,.5] ph[.5,.5,.5]
26 justRed (3)        amp[.5,.5,.5] freq[1,0,0] off[.5,.5,.5] ph[.5,.5,.5]
27 justYellow (3)     amp[.5,.5,.5] freq[1,1,0] off[.5,.5,.5] ph[.5,.5,.5]
28 mars (3)           amp[.74,.33,.09] freq[1,1,1] off[.62,.2,.2] ph[.2,.1,0]
29 modesto (3)        amp[.56,.68,.39] freq[1,1,1] off[.72,.07,.62] ph[.25,.4,.41]
30 moss (3)           amp[.78,.39,.07] freq[1,1,1] off[0,.53,.33] ph[.94,.92,.9]
31 neptune (3)        amp[.5,.5,.5] freq[1,1,1] off[.2,.64,.62] ph[.15,.2,.3]
32 netOfGems (3)      amp[.5,.5,.5] freq[1,1,1] off[.64,.12,.84] ph[.1,.25,.15]
33 organic (3)        amp[.42,.42,.04] freq[1,1,1] off[.47,.27,.27] ph[.41,.14,.11]
34 papaya (3)         amp[.65,.4,.11] freq[1,1,1] off[.72,.45,.08] ph[.71,.8,.84]
35 radioactive (3)    amp[.62,.79,.11] freq[1,1,1] off[.22,.56,.17] ph[.15,.1,.25]
36 royal (3)          amp[.5,.5,.5] freq[1,1,1] off[.41,.22,.67] ph[.2,.25,.2]
37 santaCruz (3)      amp[.5,.5,.5] freq[1,1,1] off[.5,.5,.5] ph[.25,.5,.75]
38 sherbet (3)        amp[.6059281,.17591387,.17166573] freq[1,1,1] off[.5224456,.3864609,.36020845] ph[0,.25,.5]
39 sherbetDouble (3)  amp[.6059281,.17591387,.17166573] freq[2,2,2] off[.5224456,.3864609,.36020845] ph[0,.25,.5]
40 silvermane (2)     amp[.42,0,0] freq[2,2,2] off[.45,.5,.42] ph[.63,1,1]
41 skykissed (3)      amp[.5,.5,.5] freq[1,1,1] off[.83,.6,.63] ph[.3,.1,0]
42 solaris (3)        amp[.5,.5,.5] freq[1,1,1] off[.6,.4,.1] ph[.3,.2,.1]
43 spooky (2)         amp[.46,.73,.19] freq[1,1,1] off[.27,.79,.78] ph[.27,.16,.04]
44 springtime (3)     amp[.67,.25,.27] freq[1,1,1] off[.74,.48,.46] ph[.07,.79,.39]
45 sproingtime (3)    amp[.9,.43,.34] freq[1,1,1] off[.56,.69,.32] ph[.03,.8,.4]
46 sulphur (3)        amp[.73,.36,.52] freq[1,1,1] off[.78,.68,.15] ph[.74,.93,.28]
47 summoning (3)      amp[1,0,.8] freq[1,1,1] off[0,0,0] ph[0,.5,.1]
48 superhero (3)      amp[1,.25,.5] freq[.5,.5,.5] off[0,0,.25] ph[.5,0,0]
49 toxic (3)          amp[.5,.5,.5] freq[1,1,1] off[.26,.57,.03] ph[0,.1,.3]
50 tropicalia (2)     amp[.28,.08,.65] freq[1,1,1] off[.48,.6,.03] ph[.1,.15,.3]
51 tungsten (3)       amp[.65,.93,.73] freq[1,1,1] off[.31,.21,.27] ph[.43,.45,.48]
52 vaporwave (3)      amp[.9,.76,.63] freq[1,1,1] off[0,.19,.68] ph[.43,.23,.32]
53 vibrant (3)        amp[.78,.63,.68] freq[1,1,1] off[.41,.03,.16] ph[.81,.61,.06]
54 vintage (3)        amp[.97,.74,.23] freq[1,1,1] off[.97,.38,.35] ph[.34,.41,.44]
55 vintagePhoto (3)   amp[.68,.79,.57] freq[1,1,1] off[.56,.35,.14] ph[.73,.9,.99]
```

---

## 8. Enum mapping (`stdEnums`, used by `resolveEnum`)

Member-type globals/defines store strings that resolve to ints via dotted
paths into `stdEnums`. Each leaf is `{ type:'Number', value:int }`; the
expander reads `.value`. Key tables:
- `channel`: r=0, g=1, b=2, a=3.
- `color`: mono=0, rgb=1, hsv=2.
- `oscType`: sine=0, linear=1, sawtooth=2, sawtoothInv=3, square=4,
  noise1d=5, noise2d=6.
- `oscKind`: sine=0, tri=1, saw=2, sawInv=3, square=4, noise=5, noise1d=5,
  noise2d=6.
- `midiMode`: noteChange=0, gateNote=1, gateVelocity=2, triggerNote=3,
  velocity=4.
- `audioBand`: low=0, mid=1, high=2, vol=3.
- `palette` enum is **0-based** and generated from `palettes.js` key order
  (index 0..N). **HAZARD:** this is a DIFFERENT, 0-based numbering than the
  classicNoisedeck `expandPalette` 1-based index in §7. Do not conflate.

---

## 9. PARITY HAZARDS (must-match list)

1. **Blit Y-flip asymmetry** (§2.3): WGSL flips V, GLSL doesn't. Pick one
   canonical origin (Unity/D3D = top-left, like WebGPU) and replicate exactly
   the same flip locations. A wrong flip silently mirrors output vertically.
2. **`resolveDimension` rounding** (§2.4): `{param}`/`{scale}` use `floor`;
   `{screenDivide}` uses `round`; `"N%"` uses `floor`. All clamp `max(1,…)`.
   Use integer-floor/round semantics identical to JS `Math.floor`/`Math.round`
   (round-half-up toward +∞: `Math.round(-0.5) === 0`, `Math.round(2.5)===3`).
3. **Define value stringification** (§4.5): JS `String(number)` — `1` not
   `1.0`, `0.5` stays `0.5`. The program cache key (`programDefineSuffix`) and
   the emitted `#define` both depend on this; an HLSL port must format
   identically or programs/cache-keys diverge.
4. **Deterministic ordering**: global names are **sorted** when building
   defines (§4.5); program/uniform iteration otherwise follows JS object
   insertion order (definition author order). Preserve definition order for
   `passes`, `inputs`, `outputs`, `uniforms`; sort only where the source sorts.
5. **`null` vs `0` and `??` semantics** (§4.7, dimension defaults): the code
   uses `!== undefined`, `?? `, and `!= null` distinctly. `0` is a valid value
   and must NOT be treated as "missing". `freq:[0,0,1]` palettes rely on this.
6. **Surface naming**: every user surface ref becomes `global_<name>` exactly
   once; feedback uses `feedback_<name>`. The `_chain_${planIndex}` and
   `_${pipelineId}`/`_${nodeId}` suffixes are part of the texture identity —
   two textures with the same base name but different scope are DIFFERENT
   GPU resources. Reproduce the suffix algebra precisely.
7. **Last-pass fusion** (§5.3) and **inline-write dedupe** (§4.11) change the
   number and identity of passes; getting them wrong adds/removes a copy that
   alters feedback timing on frame 0 and afterward.
8. **Particle-pipeline reset** (§4.4 step 1): only an effect that *creates*
   `global_xyz` opens a new pipeline scope AND resets xyz/vel/rgba cursors.
   Middleware (life/flow) inherits the existing pipeline.
9. **volumeSize inheritance guard** appears in THREE places (args first/second
   pass and pass build) — all gated on `currentInput3d && pipelineUniforms
   .volumeSize !== undefined`. Replicate all three.
10. **Palette float literals** (§7): copy the non-round constants byte-for-
    byte. These feed cosine-palette math; small drift shifts colors.
11. **Float type of uniforms**: all uniform values flow as JS doubles until
    the backend casts to f32 at upload. If a port keeps them as float32
    earlier, intermediate `multiply`/`power` in `resolveDimension` (done in
    JS double precision) must still match — do those size computations in
    double precision then floor.
12. **vec4 packing is NOT done in the expander**: `pass.uniforms` is a flat
    name→value map; the `uniformLayout` (`{name:{slot,components}}`) lives on
    the program and is applied by the backend. A port must pack using the
    SAME layout (column count = max slot+1; component letters map x=0,y=1,
    z=2,w=3). HLSL is row-major by default vs GLSL/WGSL column-major — this
    only matters for matrix uniforms, but verify any `mat*` uniform handling
    against the backend, not the expander.

---

## 10. Open questions / cross-subsystem dependencies

- **`stdEnums` completeness**: §8 lists the tables seen at file head; the full
  `stdEnums` object continues beyond line 60 of `std_enums.js` and includes
  the generated 0-based `palette` enum and others. A complete port needs the
  whole table — read `std_enums.js` fully.
- **Program compilation / `injectDefines`**: lives in
  `backends/webgl2.js` (`compileProgram`, `injectDefines`, lines ~732–830) and
  the WGSL equivalent in `backends/webgpu.js` (define→const, plus
  `parsePackedUniformLayout`/`parseNamedStructLayout` at ~1006–1190 which infer
  `uniformLayout` from struct comments when the definition omits one). The
  vec4-packing rules and WGSL std140 alignment (`vec4=16B align16`, scalars 4B)
  belong to those backends, not the expander.
- **`resolveDimension`** is defined in `pipeline.js` (~1129) and consumes the
  (possibly scoped) `textureSpecs` plus runtime uniforms; surface allocation,
  format defaulting (`'rgba16f'`), and double-buffer/feedback ping-pong are all
  pipeline concerns — document separately.
- **Plan/step production** (the DSL compiler) supplies `temp`, `from`, `op`,
  `args`, `builtin`, `plan.write`, `compilationResult.render`. The exact
  `args` object shapes (`kind`, `value`, `index`, `name`, VolRef/GeoRef) come
  from the lang layer (`src/lang/*`); confirm against that subsystem.
- **`getEffect`** registry: how effect definitions are keyed/loaded
  (`src/runtime/registry.js`) — the expander assumes a synchronous lookup.
- **Feedback frame-0 semantics**: when last-pass fusion writes directly to a
  surface that is also a `feedback`/`selfTex` input, what does the first frame
  read? Determined by pipeline double-buffering, not the expander.
