# Noisemaker Reference Spec — 02: Validator (Semantic Analysis)

Source of truth:
- `shaders/src/lang/validator.js` (the `validate(ast)` entry point + all helpers)
- `shaders/src/lang/transform.js` (`replaceEffect`, `listSteps`, `getCompatibleReplacements`)
- Supporting: `shaders/src/lang/diagnostics.js`, `shaders/src/lang/enumPaths.js`,
  `shaders/src/lang/paramAliases.js`, `shaders/src/lang/effectAliases.js`, `shaders/src/lang/ops.js`.

The validator is the **second compiler stage**: it consumes a parsed `ast` and produces a
**validated, flattened plan list** (`{plans, diagnostics, render, vars, searchNamespaces}`) that the
**expander** then consumes. This document is the exact contract between parser → validator → expander.

> SCOPE NOTE. This validator is purely a **CPU-side, structural/semantic** stage. It does *no*
> pixel math, no texture sampling, no coordinate transforms, no matrices, no RNG. Therefore most
> "pixel-identical HLSL parity hazards" (Y-flip, sRGB, wrap modes, row/column-major, float
> sampling) **do not arise inside this subsystem**. The parity hazards here are about producing a
> **byte-identical plan structure / numeric arg resolution** so that the downstream renderer
> receives identical inputs. See the PARITY HAZARDS section. The functions `random()`,
> `random_int()`, `random_member()` named in the task brief do **NOT** exist in this DSL/validator
> (they live in the Python/JS noisemaker *preset* layer: `js/noisemaker/dsl/builtins.js`,
> `js/noisemaker/presets.js`). The validator's nearest analogues are runtime-deferred `Func`,
> `Oscillator`, `Midi`, and `Audio` nodes. See "RNG / runtime-deferred values".

---

## 1. Top-level entry point: `validate(ast) -> result`

```js
export function validate(ast) { ... }
```

### 1.1 Return shape (the contract the expander consumes)

```ts
type ValidateResult = {
  plans: PlanOrControl[]          // see §4 — one entry per top-level statement in ast.plans
  diagnostics: Diagnostic[]       // see §7
  render: string | null           // ast.render?.name ?? null  (name of the render surface directive)
  vars: VarDecl[]                 // ast.vars ?? []  (ORIGINAL parsed var decls, passed through untouched)
  searchNamespaces: string[]      // programSearchOrder (ast.namespace.searchOrder) — never empty (see §1.2)
  trailingComments?: any          // present only if ast.trailingComments present (round-trip metadata)
}
```

- `plans` is built by iterating `ast.plans` (see §1.4) and pushing the compiled result of each statement.
- `vars` and `searchNamespaces` are attached for the **unparser/transform** layer; the expander
  primarily uses `plans` + `render`.
- `result.trailingComments` is only added when `ast.trailingComments` is truthy.

### 1.2 Mandatory search directive (HARD THROW)

```js
const programSearchOrder = ast.namespace?.searchOrder
if (!programSearchOrder || programSearchOrder.length === 0) {
    throw new Error("Missing required 'search' directive. Every program must start with 'search <namespace>, ...' ...")
}
```

This is the **only place the validator throws** instead of emitting a diagnostic. A program with no
`search` directive is a fatal error, not a recoverable diagnostic. `programSearchOrder` is an ordered
array of namespace strings (e.g. `["basics","filter","render"]`). It is used for **op-name resolution**
(§5.1) and is echoed back as `searchNamespaces`.

### 1.3 Module-level state & singletons

- `const symbols = new Map()` — per-`validate()`-call symbol table for `let`-bound variables (vars).
  Built from `ast.vars` (§3). Maps `name -> AST-node-or-resolved-value`.
- `let tempIndex = 0` — **monotonic counter** for temp surface indices, scoped to the whole
  `validate()` call (shared across ALL plans/statements). Every step that produces output gets a
  unique `temp` id via `tempIndex++`. **The ordering and exact integer values of these temp ids are
  load-bearing** — the expander wires `from` references by these integers. See PARITY HAZARD H1.
- `const STARTER_OPS = new Set()` — global registry populated by `registerStarterOps(names)` at load
  time (from effect manifests). Determines which ops may begin a chain (§5.2).
- `const validatorHooks = {}` — global registry of per-op-name hooks installed by
  `registerValidatorHook(name, hook)` (§5.6).
- `const ALLOWED_STRING_PARAMS = new Set(['text.text','text.font','text.justify'])` — the ONLY string
  params accepted anywhere (§6, `string` type). Hard allowlist; do not expand.
- `const stateSurfaces = new Set(['time','frame','mouse','resolution','seed','a'])` — identifiers
  usable as **surface** state inputs.
- `const stateValues = new Set(['time','frame','mouse','resolution','seed','a','u1','u2','u3','u4','s1','s2','b1','b2','a1','a2','deltaTime'])`
  — identifiers usable as runtime-deferred **scalar** values (produce `{fn}` closures).
- `const SURFACE_PASSTHROUGH_CALLS = new Set(['read'])` — calls treated as inline surface refs.

### 1.4 Overall control flow

```
1. Read ast.render -> render name.
2. Validate programSearchOrder exists (throw if not).
3. Process ast.vars into `symbols` (§3).
4. For each stmt in ast.plans: compiled = compileStmt(stmt); if compiled push to plans.
5. Return {plans, diagnostics, render, vars: ast.vars ?? [], searchNamespaces, [trailingComments]}.
```

---

## 2. Helper functions (exact semantics)

### 2.1 `clamp(value, min, max)` — exported

```js
export function clamp(value, min, max) {
    if (typeof min === 'number' && value < min) return min
    if (typeof max === 'number' && value > max) return max
    return value
}
```
- `min`/`max` are **optional**; only applied when they are numbers. `NaN`/`undefined` bounds are no-ops.
- PARITY HAZARD: comparisons use JS `<`/`>` on doubles. An HLSL port resolving args must use the same
  numeric type (double, not float) for clamp comparisons, or boundary values may diverge (H2).

### 2.2 `toBoolean(value)`
```js
typeof value === 'number' ? value !== 0 : !!value
```
Number → `!= 0`; anything else → JS truthiness.

### 2.3 `toSurface(arg)` — AST node → surface descriptor
Returns one of these shapes (or `null`):
| arg.type            | result                                  |
|---------------------|-----------------------------------------|
| `OutputRef`         | `{kind:'output', name}`                 |
| `SourceRef`         | `{kind:'source', name}`                 |
| `XyzRef`            | `{kind:'xyz', name}`                    |
| `VelRef`            | `{kind:'vel', name}`                    |
| `RgbaRef`           | `{kind:'rgba', name}`                   |
| `MeshRef`           | `{kind:'mesh', name}`                   |
| `Ident` name=`none` | `{kind:'output', name:'none'}`          |
| `Ident` ∈ stateSurfaces | `{kind:'state', name}`              |
| anything else / falsy | `null`                                |

### 2.4 `callToSurface(node)`
Unwraps a single-element `Chain`, then if `node` is a `Call` whose name ∈ `SURFACE_PASSTHROUGH_CALLS`
(`read`), takes `node.args[0]` (else `node.kwargs.tex`) and runs it through `toSurface`. Used so an
inline `read(o0)` in a surface-typed param position resolves to a surface descriptor.

### 2.5 `resolveEnum(path)` — enum/symbol path resolution
Input: array `path = [head, ...rest]`.
1. If `symbols.has(head)`: `cur = symbols.get(head)`; if that node is `Number`/`Boolean`, unwrap to its `.value`.
2. Else if `enums` (project enums) own `head`: `cur = enums[head]`.
3. Else if `stdEnums` own `head` (e.g. `oscKind`, `oscType`, `palette`): `cur = stdEnums[head]`.
4. Else return `undefined`.
5. Walk `rest`: for each `part`, descend `cur = cur[part]` if own-property, else return `undefined`.
6. If final `cur` is a `Number`/`Boolean` node, return its `.value`; else return `cur`.

PARITY HAZARD H3: precedence is **symbols > enums > stdEnums**. A `let` var that shadows an enum name
wins. The HLSL port must replicate this lookup order exactly. `enums` and `stdEnums` are external
tables (not in this file) — their numeric values are a cross-subsystem dependency (§9).

### 2.6 `clone(node)` = `JSON.parse(JSON.stringify(node))`
Deep clone via JSON. Drops `undefined`, functions, and non-JSON values. Used before substitution so
the original AST is never mutated. PARITY HAZARD: JSON round-trip turns `-0` into `0` and cannot carry
`NaN`/`Infinity` (become `null`). Numeric literals from the parser are finite doubles so this is
normally safe (H4).

### 2.7 `canResolveOpName(name)`
True iff `ops[`${ns}.${name}`]` exists for some `ns` in `programSearchOrder`.

### 2.8 `resolveCall(call)` — variable substitution & partial application
If `symbols.has(call.name)`:
- symbol is `Ident`: return `{...call, name: symbol.name}` (rename).
- symbol is `Call` (stored partial): **APPEND** call-site positional args to stored args
  (`mergedArgs = storedArgs.slice(); for each callArg push`). kwargs are **merged with call-site
  winning** (`mergedKw[k]=v`). Namespace: call's own namespace wins, else stored symbol's namespace.
  Returns a fresh `{type:'Call', name: symbol.name, args, [kwargs], [namespace]}`.
- otherwise return `call` unchanged.

PARITY HAZARD H5: positional partial-application is **append**, not overlay-by-index. Documented from
LANGUAGE.md ("Positional Arguments: Appended to the stored arguments").

### 2.9 `firstChainCall`, `getStarterInfo`, `isStarterChain`
- `firstChainCall(node)`: returns the `Call` if node is a `Call`, or `node.chain[0]` if it is a `Call`,
  else `null`.
- `getStarterInfo(node)`: builds the namespaced name (`${namespace.resolved}.${name}` when
  `node.namespace.resolved` present) and returns `{call, index}` for the first chain entry that
  `isStarterOp(name)`. For a bare `Call` returns `{call:node, index:0}` if starter, else `null`.
- `isStarterChain(node)`: true iff `getStarterInfo` returns an entry with `index === 0`.

### 2.10 `substitute(node)` — recursive variable inlining
- `Ident` in `symbols`: clone the bound value, recurse `substitute`, and tag the result object with
  `result._varRef = node.name` (round-trip marker preserved downstream into resolved args).
- `Chain`: map each `Call`, recursively substitute each arg & kwarg, then `resolveCall`. Returns a NEW
  `{type:'Chain', chain}` whose entries are `{type:'Call', name, args, [kwargs]}` (note: this rebuild
  **drops any fields other than name/args/kwargs** on chain entries — e.g. `namespace`, `loc`,
  `leadingComments` survive only via `resolveCall`'s merge, NOT via this map). 
- `Call`: substitute args/kwargs, then `resolveCall`.
- otherwise: return node unchanged.

PARITY HAZARD H6: substitution rebuilds Chain/Call nodes from scratch, intentionally **losing some
parser metadata** on inner calls. Any port must mirror which fields survive substitution.

---

## 3. Variable processing (`ast.vars` → `symbols`)

For each `v` in `ast.vars` (each `{name, expr, ...}`):
1. `expr = substitute(clone(v.expr))`.
2. If `expr` is a starter chain → push `S006` on its head call (a var holding a starter chain without
   a write target is an error). Processing continues.
3. If `expr == null` OR `expr` is `Ident` named `'null'`/`'undefined'` → push `S004`, **continue** (do
   not bind).
4. If `expr` is `Ident` and the name is not a symbol, not a state value, not a bare op, and not
   resolvable via search order → push `S003`, **continue**.
5. Binding rules:
   - `Chain` with exactly one element → bind `symbols.set(v.name, expr.chain[0])` (the single Call).
   - `Member`: resolve via `resolveEnum(expr.path)`:
     - number → `{type:'Number', value}`
     - other defined → the resolved object
     - undefined → the raw `Member` node.
   - otherwise → bind the expr as-is.

So a `symbols` value is one of: a `Call` node, a `Number`/`Boolean` node, a resolved enum
object/scalar, or an arbitrary AST node.

---

## 4. Statement compilation: `compileStmt(stmt)`

Dispatch by `stmt.type`. `plans` entries are exactly these returned objects.

| stmt.type   | returned object                                                                 |
|-------------|----------------------------------------------------------------------------------|
| `IfStmt`    | `{type:'Branch', cond, then, elif, else}` (§4.1)                                  |
| `Break`     | `{type:'Break'}`                                                                 |
| `Continue`  | `{type:'Continue'}`                                                              |
| `Return`    | `{type:'Return', [value]}` — `value = evalExpr(stmt.value)` if present            |
| (default)   | result of `compileChainStatement(stmt)` — a **Plan** (§4.2) or `null`            |

`compileBlock(body)` maps `compileStmt` over a statement list, dropping `null` results.

### 4.1 `IfStmt` → Branch
- `cond = evalCondition(stmt.condition)` (§4.3)
- `then = compileBlock(stmt.then)`
- `elif`: array of `{cond: evalCondition(e.condition), then: compileBlock(e.then)}`
- `else = compileBlock(stmt.else)`

### 4.2 `compileChainStatement(stmt)` → Plan (the core)

Returns:
```ts
type Plan = {
  chain: Step[]                          // flat, ordered list of steps (see §5/§6)
  write: {kind:'output', name:string} | null   // 2D write target (from stmt.write.name)
  write3d: { tex3d:{kind:'vol',name}, geo:{kind:'geo',name} } | null  // from stmt.write3d
  final: number | null | undefined       // temp index of the last produced step (chain output)
  states: object[]                       // agent/feedback state descriptors added by hooks (§5.6)
  leadingComments?: any                  // from stmt.leadingComments (round-trip)
}
```

Steps:
1. Build `chainNode = {type:'Chain', chain: stmt.chain}`. `hasWrite = stmt.write || stmt.write3d`.
2. If `!hasWrite` AND `isStarterChain(chainNode)` → push `S006` on `stmt.chain[0]`.
3. If `!hasWrite` → push `S001` ("Chain must have explicit write() or write3d() target") and **return
   `null`** (no plan produced).
4. `writeName = stmt.write?.name ?? null`.
5. `write3dTarget`: when `stmt.write3d` present, `{tex3d:{kind:'vol',name:write3d.tex3d?.name||write3d.tex3d},
   geo:{kind:'geo',name:write3d.geo?.name||write3d.geo}}`, else `null`.
6. `states = []`.
7. `finalIndex = processChain(stmt.chain, null)` (§5).
8. `writeSurf = stmt.write ? {kind:'output', name:stmt.write.name} : null`.
9. Return Plan as above.

### 4.3 `evalCondition(node)` — returns boolean OR a deferred-closure object
1. `expr = evalExpr(node)` (substitute+clone, resolve `Member` enums).
2. `!expr` → `false`.
3. `Number` → `toBoolean(value)`. `Boolean` → `!!value`.
4. `Func` (an embedded JS expression `{src}`): compiles `new Function('state', 'with(state){ return <src>; }')`
   and returns **`{fn: (state)=>toBoolean(fn(state))}`**. On compile failure → `S001` + `false`.
5. `Ident`: if symbol → recurse `evalCondition(symbol)`; if state value → return
   `{fn:(state)=>toBoolean(state[key])}`; else `S003` + `false`.
6. `Member`: `cur = resolveEnum(path)`; number/defined → `toBoolean(cur)`; undefined → `S001` + `false`.
7. otherwise → `false`.

So a Branch `cond` is EITHER a literal boolean (statically decided) OR `{fn}` (runtime-evaluated
against a `state` object). PARITY HAZARD H7: `Func`/`with(state){...}` is arbitrary JS evaluated at
runtime. An HLSL/C# port CANNOT `eval` JS — it must either reject `Func` conditions or pre-evaluate
them. They are not pixel math; they gate which chains run. Cross-subsystem dependency (§9).

### 4.4 `evalExpr(node)`
`substitute(clone(node))`; if starter chain push `S006`; if `Member`, try `resolveEnum`:
number → `{type:'Number', value}`, other defined → resolved value; else return the substituted expr.

---

## 5. `processChain(calls, input, options)` — chain flattening

Signature: `processChain(calls, input, options = {})`.
- `options.allowStarterless === true` lets a non-starter op begin a (sub)chain **only** if it is a
  surface-passthrough call (`read`). Used when resolving a chain inside a surface-typed argument.
- `current` starts at `input` (the upstream temp index, or `null` for a chain root). Returns final
  `current` (a temp index or `null`).

For each `original` node in `calls`, dispatch in this order:

### 5.1 Built-in pipeline nodes (produce `builtin:true` steps)

All built-in steps share: `{op, args, from, temp, builtin:true, [leadingComments]}`.

- **`Read`** (`type:'Read'`): GUARD — if `current !== null`, push `S001` ("read() is a starter
  node...") and skip. `surface = toSurface(original.surface)`; if falsy push `S001` and skip. Emit
  `{op:'_read', args:{tex:surface, [_skip]}, from:null, temp, builtin:true}`. Sets `current = temp`.
  `_skip` copied from `original._skip === true`.
- **`Read3D` with `geo`** (two-arg starter form): GUARD same as Read. Build `tex3d` =
  `{kind: tex3d.type==='VolRef'?'vol':'tex3d', name}` and `geo` = `{kind:'geo', name}`. If either
  missing → `S001`+skip. Emit `{op:'_read3d', args:{tex3d, geo, [_skip]}, from:null, temp, builtin}`.
- **`Write`** (`type:'Write'`): `surface = toSurface(original.surface)`; falsy → `S001`+skip;
  `current===null` → `S005` ("write() requires an input")+skip. Emit
  `{op:'_write', args:{tex:surface}, from:current, temp, builtin}`. Sets `current = temp`.
- **`Write3D`** (`type:'Write3D'`): build tex3d/geo descriptors (same kind logic). Missing → `S001`;
  `current===null` → `S005`. Emit `{op:'_write3d', args:{tex3d, geo}, from:current, temp, builtin}`.
- **`Subchain`** (`type:'Subchain'`): `current===null` → `S005`+skip. Emit begin marker
  `{op:'_subchain_begin', args:{name:original.name||null, id:original.id||null}, from:current, temp, builtin}`;
  set `current=beginTemp`; then `current = processChain(original.body, current)` (RECURSE, no
  allowStarterless); then emit `{op:'_subchain_end', args:{name,id}, from:current, temp, builtin}`;
  set `current=endTemp`.

PARITY HAZARD H1 (restated): `_subchain_begin`/`_subchain_end` consume temp indices too. The exact
sequence of `tempIndex++` calls (read/write/subchain markers interleaved with effect steps) defines
every `temp`/`from` integer. Reproduce the exact allocation order.

### 5.2 Effect-call resolution
1. `call = resolveCall({...original})`.
2. `effectiveNamespace = call.namespace || {searchOrder: programSearchOrder}`.
3. Build `candidateNames` IN ORDER:
   - if `call.namespace.resolved` → `${resolved}.${call.name}` first.
   - then for each `ns` in `effectiveNamespace.searchOrder` → `${ns}.${call.name}`.
4. First candidate with `ops[candidate]` truthy wins → sets `opName`, `spec`. **First match wins;
   order matters** (H8). If none → `S001` ("Unknown effect: '<name>'") + skip.
5. `checkEffectAlias(opName)` → if returns a string, push `S008` (deprecated effect warning).

### 5.3 Special op `prev`
If `opName === 'prev'`: emit `{op:'prev', args:{tex:{kind:'output', name:writeName}}, from:current,
temp, [namespace], [leadingComments]}`, set `current=temp`, continue. (`prev` reads the chain's own
write target — feedback.)

### 5.4 Starter / chain-position legality
- `isStarter = isStarterOp(opName)`.
- `starterlessRoot = (current === null)`.
- `allowPassthroughRoot = allowStarterless && SURFACE_PASSTHROUGH_CALLS.has(opName)`.
- If `starterlessRoot && !isStarter && !allowPassthroughRoot` → `S005` + skip.
- `starterHasInput = isStarter && current !== null` → if true push `S005` (starter must be first) and
  set `fromInput = null`; else `fromInput = current`.

### 5.5 Argument resolution (the heart — §6)
Build `args = {}`, `argSources = null` (sidecar), `kw = call.kwargs`, `seen = new Set()`.
If `kw`: `resolveParamAliases(opName, kw)` (mutates kw in place, renames deprecated keys) and each
warning string → `S007`. Then iterate `spec.args` (the effect's param defs) — see §6.

`spec` (the op definition) and each `def` in `spec.args` have this shape (from effect `definition.js`):
```ts
type ParamDef = {
  name: string            // DSL parameter name (THE key used in compiled args)
  type: 'float'|'int'|'surface'|'color'|'vec3'|'vec4'|'boolean'|'member'|'volume'|'geometry'|'string'|...
  default?: any           // default value (already in final units)
  min?: number; max?: number
  uniform?: string        // GPU uniform name — NOT used as the args key (see PARITY H9)
  enum?: string|string[]; enumPath?: string|string[]   // enum prefix for member/enum resolution
  choices?: { [name:string]: number|string }            // inline named choices
  defaultFrom?: string    // fallback: copy resolved value of another param (by DSL name)
  randMin?, randMax?, ui?, ... // ignored by validator
}
```

### 5.6 Validator hooks (`validatorHooks[call.name]`)
After args are built (and before the default step push), if a hook is registered for `call.name`
(the **bare** name, not opName), it is called with:
```js
hook({
  call, originalCall: original, args, writeName, from: fromInput,
  allocateTemp: () => tempIndex++,
  addStep: (step) => chain.push(step),
  addState: (state) => states.push(state),
  pushDiagnostic: pushDiag,
  states,
  starter: getStarterInfo(original)
})
```
If `hookResult.handled` is truthy: if `hookResult.current != null` set `current = hookResult.current`,
then `continue` (the hook fully replaced default step emission). This is the **agent-effects multi-pass
mechanism** (deposit/diffuse/blend passes are added via `addStep`/`addState`). PARITY HAZARD H10: hooks
are arbitrary JS that can inject extra steps and consume temp indices via `allocateTemp`. A port must
re-implement each registered hook's step-emission logic identically.

### 5.7 Default step emission
```js
const idx = tempIndex++
const step = {op: opName, args, from: fromInput, temp: idx}
if (namespaceSnapshot) step.namespace = namespaceSnapshot   // §5.8
if (original.leadingComments) step.leadingComments = ...
if (original.kwargs && keys>0) step.rawKwargs = original.kwargs   // raw AST kwargs for automation UI
if (argSources) step.argSources = argSources                // {paramName:'array'} round-trip form
chain.push(step); current = idx
```

So a **resolved effect Step** is:
```ts
type Step = {
  op: string                 // fully-qualified op name, e.g. 'basics.voronoi'
  args: { [dslParamName: string]: ResolvedArg }   // see §6 value types
  from: number | null        // upstream temp index feeding this step (null for starter root)
  temp: number               // this step's output temp index
  namespace?: NamespaceSnapshot   // §5.8 (frozen)
  builtin?: true             // only on _read/_read3d/_write/_write3d/_subchain_* steps
  leadingComments?, rawKwargs?, argSources?   // round-trip/UI metadata
}
```

### 5.8 `buildNamespaceSnapshot(callNamespace)` (frozen)
Returns `Object.freeze`d:
```js
{ call: { name, resolved, explicit:boolean, source, [searchOrder:frozen slice], [fromOverride:true] },
  [resolved] }  // top-level `resolved` copied for downstream
```
Strings coerced to `null` when not strings. Returns `null` if `callNamespace` is not an object.

---

## 6. Per-type argument resolution (numbered, exhaustive)

For each `def` of `spec.args[i]`:
- `node = (kw && kw[def.name] !== undefined) ? kw[def.name] : call.args[i]`; then `node = substitute(node)`.
- `argKey = def.name`.
- If `kw && kw[def.name] !== undefined` → `seen.add(def.name)`.

**Color-splat special case** (BEFORE per-type branch): if NO kwargs (`!kw`) and `node.type==='Color'`
and `def.type !== 'color'` and `def.name==='r'` and the next two spec args are named `g` and `b`, then
splat: `args.r = value[0]; args.g = value[1]; args.b = value[2]; i += 2; continue`.

**ArrayLiteral** (any non-color/surface type): if `node.type==='ArrayLiteral'`, build numeric array
from elements; non-`Number` elements push `S002` and contribute `0`. Set `args[key]=value` and
`argSources[key]='array'`. (Lexer only emits `[` when source has it; invisible to legacy programs.)

Then per `def.type`:

### 6.1 `surface`
- `String` node → `S001` (no strings for surface); fall back to `def.default` via `toSurface({Ident, name:default})` or `null`. continue.
- Resolution order: (a) if node is `Read` with `.surface` → `toSurface`. (b) `inlineSurface =
  surf || callToSurface(node)`. (c) `node.type==='Chain'` → `idx = processChain(node.chain, null, {allowStarterless:true})`;
  if non-null `surf = {kind:'temp', index:idx}`. (d) `node.type==='Call'` → `processChain([node],
  null, {allowStarterless:true})` → `{kind:'temp', index:idx}`. (e) starter node → `S005`,
  `invalidStarterChain=true`. (f) else `surf = toSurface(node)`.
- If `!surf`: if `invalidStarterChain` set `args[key]=surf`(null) continue. Else if no `def.default`:
  - no node → `S001` ("Missing required surface argument"); `Ident` undefined var → `S003`; else `S001`
    ("Invalid surface reference").
  - if `def.default`: `surf = toSurface({Ident, name:default}) || {kind:'pipeline', name:default}`.
- `args[key] = surf` (a surface descriptor, possibly `{kind:'temp', index}` for inline subchains).

### 6.2 `color`
- `String` → `S001` + `def.default`.
- `Color` node → `node.hex || node.value` (**hex string kept as-is**, e.g. `"#ff0000"`).
- else: non-`Ident` typed node → `S002`; value = `def.default`.

### 6.3 `vec3`
- `String` → `S001`; default `def.default.slice()` or `[0,0,0]`.
- `Call name==='vec3'` with 3 args → array of the 3 numbers (non-number → `S002`, push `0`).
- `Color` → `node.value.slice(0,3)`.
- else → `def.default?.slice()` or `[0,0,0]` (non-`Ident` typed → `S002`).

### 6.4 `vec4`
- Like vec3 but `Call name==='vec4'` w/ 4 args; `Color` → `node.value.slice()` (4 comps); default
  fallback `[0,0,0,1]`.

### 6.5 `boolean`
- `String` → `S001`; default `!!def.default` (or `false`).
- `Boolean` → `!!node.value`. `Number` → `node.value !== 0`.
- `Func` → `{fn:(state)=>!!fn(state)}` (compile via `new Function`); fail → `S001` + default.
- `Ident` ∈ stateValues → `{fn:(state)=>!!state[key]}`.
- else: `Ident` not state → `S003`; non-`Ident` typed → `S002`; value = `!!def.default` (or false).

### 6.6 `member` (enum-typed)
- `String` → `S001` + `def.default`.
- `prefix = normalizeMemberPath(def.enumPath || def.enum)`.
- Determine `path`: `Member` → `normalizeMemberPath(node.path)`; `Number`/`Boolean` → set
  `args[key]= number (Boolean→1/0)` and continue; `Ident` ∈ stateValues → `args[key]={fn:state=>state[key]}`
  continue; `Ident` → `[node.name]`. If still no path → `normalizeMemberPath(def.default)`.
- `resolved = resolveEnum(path)` (unwrap Number/Boolean). If not a number: `path =
  applyEnumPrefix(path, prefix)`; if prefix present and path doesn't start with prefix → `S001` +
  reset `path = prefix.slice()`; re-resolve. If still not a number: resolve `def.default` path; if that
  yields a number use it, else **`resolved = 0`**.
- `args[key] = resolved` (a NUMBER). If node was `Member`, mutate `node.path = path.slice()` (canonicalized).

`normalizeMemberPath` (from enumPaths.js): arrays → filtered string segments; strings → split on `.`,
trim, filter empties; numbers → `[String(n)]`; else `null`. `applyEnumPrefix`: returns path if no
prefix; if path already starts with prefix returns copy; else tries to splice the longest non-empty
suffix overlap, finally `prefix.concat(path)`. `pathStartsWith`: empty prefix → true.

### 6.7 `volume`
- `String` → `S001`; default `{kind:'vol', name:def.default}` or null.
- `Read3D` single-arg (`tex3d && !geo`): name must match `/^vol[0-7]$/` → `{kind:'vol', name}`, else
  `S001` + default.
- `VolRef` → `{kind:'vol', name}`. `Ident`: `none`→`{kind:'vol',name:'none'}`; `/^vol[0-7]$/`→
  `{kind:'vol',name}`; else `S001` + default. No node but default → `{kind:'vol', name:default}`.

### 6.8 `geometry`
Mirror of volume with `/^geo[0-7]$/`, `GeoRef`, `kind:'geo'`.

### 6.9 `string` (STRICT)
- `funcName = opName.split('.').pop()`; `allowlistKey = `${funcName}.${def.name}``.
- If `allowlistKey` NOT in `ALLOWED_STRING_PARAMS` → `S001` + `def.default` + continue. (Only
  `text.text`, `text.font`, `text.justify` ever pass.)
- `String` node → `node.value`. `Ident` with `def.choices` → `def.choices[name]` or `S001`+default.
  Other node → `S001` ("requires a quoted string literal") + default. No node → default.

### 6.10 Numeric (the `else` / default branch — covers `float`, `int`, any unrecognized type)
- `String` → `S001` ("String literal not allowed for numeric parameter") + `def.default` + continue.
- `Number`/`Boolean`: `value = Boolean?1:0 : value`; `clamped = clamp(value, def.min, def.max)`; if
  changed → `S002` ("got X, clamped to Y"); `value=clamped`. If `node._varRef` set →
  `value = {_varRef, value}` (round-trip wrapper — downstream must read `.value`).
- `Func` → `{fn, min:def.min, max:def.max}` (compile `new Function('state','with(state){ return <src>; }')`);
  fail → `S001` + default.
- `Oscillator` → resolved object (§6.11).
- `Midi` → resolved object (§6.12).
- `Audio` → resolved object (§6.13).
- `Member` → `cur = resolveEnum(path)`; number → clamp(+`S002` if changed); boolean → 1/0 clamped;
  else `S001` + default.
- `Ident` ∈ stateValues → `{fn:(state)=>state[key], min, max}`.
- `Ident` with `def.enum` → path = `normalizeMemberPath(def.enum).concat([name])`; resolveEnum →
  clamp; fail → `S003` + default.
- `Ident` with `def.choices` → `def.choices[name]` (number) clamped; else `S003` + default.
- else: `Ident` not state → `S003`; non-`Ident` typed → `S002`; then **`defaultFrom`**: if
  `def.defaultFrom` set, look up the *DSL-named* arg already resolved in `args` (via
  `spec.args.find(d=>d.name===def.defaultFrom)`) and copy its value, else `def.default`. Otherwise
  `value = def.default`.

### 6.11 Oscillator resolved shape
```ts
{ type:'Oscillator',
  oscType: number,              // resolved oscType/oscKind enum (Member or Ident via resolveEnum(['oscKind',name])); default 0
  min:   clamp01(resolveOscParam(node.min) ?? 0),   // Math.max(0, Math.min(1, x))
  max:   clamp01(resolveOscParam(node.max) ?? 1),
  speed: resolveOscParam(node.speed) ?? 1,
  offset:resolveOscParam(node.offset) ?? 0,
  seed:  resolveOscParam(node.seed) ?? 1,           // <-- DEFAULT SEED = 1 (not 0)
  _ast: node, [_varRef] }
```
`resolveOscParam(p)`: `Number`→value; `Boolean`→1/0; `Member`→resolveEnum (number or `.value`); else
`undefined`. min/max are **clamped to [0,1]**; speed/offset/seed are NOT clamped.

### 6.12 Midi resolved shape
```ts
{ type:'Midi',
  channel: resolveMidiParam(node.channel) ?? 1,     // default channel 1
  mode: number,                                      // resolved midiMode enum; DEFAULT 4 (velocity)
  min:  clamp01(resolveMidiParam(node.min) ?? 0),
  max:  clamp01(resolveMidiParam(node.max) ?? 1),
  sensitivity: resolveMidiParam(node.sensitivity) ?? 1,
  _ast: node, [_varRef] }
```

### 6.13 Audio resolved shape
```ts
{ type:'Audio',
  band: number,                                      // resolved audioBand enum; DEFAULT 0 (low)
  min:  clamp01(resolveAudioParam(node.min) ?? 0),
  max:  clamp01(resolveAudioParam(node.max) ?? 1),
  _ast: node, [_varRef] }
```

### 6.14 `_skip` meta-arg & unknown-kwarg sweep
- If `kw._skip` present: `args._skip = (kw._skip.type==='Boolean') ? kw._skip.value : false`;
  `seen.add('_skip')`.
- After all spec args: for each key in `kw` not in `seen` → `S001` ("Unknown argument '<key>'").

---

## 7. Diagnostics (`pushDiag(code, node, message?)`)

`diagnostics` table (clean-room descriptions; codes + severity are the contract):
| code | stage    | severity | default message                          |
|------|----------|----------|------------------------------------------|
| L001 | lexer    | error    | Unexpected character                     |
| L002 | lexer    | error    | Unterminated string literal              |
| P001 | parser   | error    | Unexpected token                         |
| P002 | parser   | error    | Expected closing parenthesis             |
| S001 | semantic | error    | Unknown identifier                       |
| S002 | semantic | **warning** | Argument out of range                 |
| S003 | semantic | error    | Variable used before assignment          |
| S004 | semantic | error    | Cannot assign null or undefined          |
| S005 | semantic | error    | Illegal chain structure                  |
| S006 | semantic | error    | Starter chain missing write() call       |
| S007 | semantic | **warning** | Deprecated parameter alias            |
| S008 | semantic | **warning** | Deprecated effect                     |
| R001 | runtime  | error    | Runtime error                            |

`pushDiag` builds:
```ts
type Diagnostic = {
  code: string
  message: string         // enriched (see below)
  severity: 'error'|'warning'
  nodeId: any             // node?.id
  location?: {line, column}   // from node.loc.{line,column}
  identifier?: string         // extractIdentifierName(node)
}
```
**Message enrichment**: `extractIdentifierName(node)` returns: `Ident`→name; `Member`→`path.join('.')`;
`Call`→name; `Func`→`{<src first 30 chars>...}`; else `node.name||String(node.value)||[<type>]`. If an
identifier is found AND not already in the message AND the message contains no `'` → append `: '<ident>'`.

PARITY HAZARD H11: validator only **collects** diagnostics; it does not abort on errors (except the
missing-search throw). A program with `error`-severity diagnostics still returns `plans` (possibly with
skipped steps). The caller decides whether to render. The HLSL/C# port must replicate: errors are data,
not exceptions (apart from §1.2).

---

## 8. `transform.js` — plan-rewriting API (consumed by the editor/UI, NOT the renderer)

Operates on the **compiled program** object `{plans, searchNamespaces, ...}` (the validate result,
possibly post-expansion). `step.temp` is the stable handle.

### 8.1 `deepClone(obj)` — recursive structural clone (handles arrays + plain objects; primitives
passthrough). Used so transforms are immutable.

### 8.2 `findStepByIndex(compiled, stepIndex)` → `{planIndex, chainIndex, step} | null`
Linear scan over `compiled.plans[].chain[]` for `step.temp === stepIndex`.

### 8.3 `checkIsStarter(effectName, searchOrder=[])`
`isStarterOp(effectName)` OR (if bare name) `isStarterOp(`${ns}.${effectName}`)` for any ns.

### 8.4 `getEffectSpec(effectName, searchOrder=[])`
`ops[effectName]` OR (bare) first `ops[`${ns}.${name}`]`.

### 8.5 `replaceEffect(compiled, stepIndex, newEffectName, newArgs={}, options={})`
Returns `{success, program?, error?}`.
1. Guard `compiled.plans`. `searchOrder = options.searchOrder || compiled.searchNamespaces || []`.
2. `location = findStepByIndex`; missing → error.
3. `oldEffectName = step.op`. `isStarterPosition = (chainIndex === 0)`.
   `newIsStarter = checkIsStarter(newEffectName, searchOrder)`.
4. `newSpec = getEffectSpec`; missing → error "Effect '<name>' not found".
5. **Like-for-like enforcement**:
   - `isStarterPosition && !newIsStarter` → error (first effect must be a starter).
   - `!isStarterPosition && newIsStarter` → error (starters only at chain start).
6. `newProgram = deepClone(compiled)`.
7. Build `finalArgs`: FIRST seed defaults — for each `def` in `newSpec.args` with `def.default !==
   undefined`, `finalArgs[def.name] = def.default` (keyed by **DSL name**, not uniform). THEN apply
   `newArgs`: for numeric non-integer values, **round to 3 decimals** (`Math.round(v*1000)/1000`);
   others passthrough.
8. Resolve namespace: if `newEffectName` includes `.` → namespace = first segment (verify
   `ops[newEffectName]` else error). Else find first `ops[`${ns}.${name}`]` in searchOrder →
   `resolvedNewName`/`effectNamespace`; if none, scan ALL `ops` keys for one ending `.${name}`.
9. If `effectNamespace` not already in `newProgram.searchNamespaces` → append it (so the unparser can
   strip it).
10. Mutate the cloned step: `op = resolvedNewName`, `args = finalArgs`, `namespace = effectNamespace ?
    {resolved: effectNamespace} : null` (RESETS any stale `from`/override namespace state).
11. Return `{success:true, program:newProgram}`.

PARITY HAZARD H12: the **3-decimal rounding** on injected float args is a real, observable numeric
mutation. A C# port of the editor transform must round identically (banker's vs half-up matters —
JS `Math.round` is round-half-up toward +∞ for ties: `Math.round(0.0005*1000)=1` → `0.001`).

### 8.6 `listSteps(compiled, options={})` → array of:
```ts
{ stepIndex: step.temp, planIndex, chainIndex,
  effectName: step.op, isStarter, isStarterPosition,
  canReplaceWithStarter: isStarterPosition,
  canReplaceWithNonStarter: !isStarterPosition,
  args: step.args || {} }
```

### 8.7 `getCompatibleReplacements(compiled, stepIndex, options={})` →
`{success, compatible, incompatible}`. Partitions ALL `Object.keys(ops)` into `starters`/`nonStarters`
via `checkIsStarter`; if `chainIndex===0` returns `{compatible:starters, incompatible:nonStarters}`
else swapped.

---

## 9. `isStarterOp(name)` (exported) — exact logic

```js
if (name === 'particles' || name === 'render.particles') return false   // hard override
if (STARTER_OPS.has(name)) return true
const parts = name.split('.')
if (parts.length > 1) {
  const canonical = parts.at(-1)
  if (STARTER_OPS.has(canonical)) {
    for (const op of STARTER_OPS) if (op.endsWith('.'+canonical)) return false  // namespaced starter exists → THIS exact name is not it
    return true   // only bare canonical registered → applies
  }
}
return false
```
`registerStarterOps(names[])` adds each non-empty string to `STARTER_OPS`. `STARTER_OPS` is populated
at module load by the effect manifest layer (cross-subsystem dep §10).

---

## 10. Open questions / cross-subsystem dependencies

1. **`ops` registry & `spec.args` shapes** come from `shaders/effects/**/definition.js` (via
   `registerOp`). The validator's behavior is entirely parameterized by these specs — the HLSL port
   needs the SAME per-effect param tables (name/type/default/min/max/enum/choices/defaultFrom). NOT in
   this file.
2. **`STARTER_OPS`** is populated externally via `registerStarterOps`; the set of starter effects is a
   manifest-time decision. The `'particles'` hard-override is a workaround for a stale manifest.
3. **`enums` and `stdEnums`** numeric values (oscKind/oscType/midiMode/audioBand/palette/etc.) are
   external tables. `member`-typed and Oscillator/Midi/Audio enum resolution depends on them. The
   defaults baked into the validator: Oscillator seed=1, Midi mode=4 (velocity)/channel=1, Audio
   band=0 (low).
4. **`Func` nodes** carry raw JS source evaluated at runtime via `new Function('state', with(state){...})`.
   A C#/HLSL runtime cannot eval JS — these must be transpiled or rejected. They feed `cond`,
   boolean params, and numeric params as `{fn,...}` closures.
5. **`random()`/`random_int()`/`random_member()`**: NOT part of this DSL. They are in the
   Python/JS *preset/effects* layer (`js/noisemaker/dsl/builtins.js`, `presets.js`). If the Unity port
   needs them it must port that layer, not this validator. No RNG/seeding occurs in `validate()`.
6. The **expander** is the consumer of `plans` — it resolves `from`/`temp` wiring, surface allocation
   (o0..o7 are user-only; temps are internal), control-flow Branch/Break/Continue/Return, subchain
   markers, and runtime `{fn}` closures. See `03-expander` spec.

---

## PARITY HAZARDS (consolidated)

- **H1 — temp index allocation order.** Every `tempIndex++` (effect steps, `_read`/`_write`/`_read3d`/
  `_write3d`, `_subchain_begin`/`_subchain_end`, and hook `allocateTemp()`) must fire in the exact same
  order to produce identical `temp`/`from` integers. `tempIndex` is global across all plans in a call.
- **H2 — clamp uses double comparisons** with optional bounds (no clamp if bound is not a number).
- **H3 — enum resolution precedence:** symbols > enums > stdEnums; later `let` can shadow enums.
- **H4 — JSON deep-clone** drops undefined/functions and cannot carry NaN/Infinity/-0.
- **H5 — partial-application appends positional args** (not overlay-by-index); kwargs call-site wins.
- **H6 — `substitute()` rebuilds Chain/Call**, intentionally losing some parser metadata on inner calls.
- **H7 — `Func`/`with(state)` conditions** are arbitrary JS; cannot be eval'd in C#/HLSL.
- **H8 — op name resolution is first-match** over `[explicit-resolved, ...searchOrder]`; order decides
  which namespace's effect is bound.
- **H9 — args are keyed by `def.name` (DSL name), NOT `def.uniform`.** `defaultFrom` also references by
  DSL name. Keying by uniform breaks `defaultFrom` and downstream lookups.
- **H10 — validator hooks** inject extra steps/states and consume temp indices; must be ported per-op.
- **H11 — errors are collected, not thrown** (except missing `search`). Plans still returned.
- **H12 — `replaceEffect` rounds injected non-integer float args to 3 decimals** via `Math.round(v*1000)/1000`
  (JS round-half-up). Must match for editor parity.
- **H13 — clamp `S002` is a warning, not an error**; out-of-range numeric args are silently clamped and
  the clamped value is what reaches the renderer.
- **H14 — `member` resolution falls back to `0`** when nothing resolves (after trying value, prefixed
  path, and default). A bad enum silently becomes `0`.
- **H15 — Oscillator/Midi/Audio min/max are clamped to [0,1]** but speed/offset/seed/sensitivity are not.
- **H16 — `_varRef` wrapper:** a numeric arg sourced from a `let` var becomes `{_varRef, value}` not a
  bare number. Downstream/renderer must unwrap `.value`. (Round-trip metadata.)
