# Render Graph JSON Schema (the Python ↔ exporter contract)

This is the **normalized** graph format that both producers emit and the Python TD runtime
(`td/noisemaker/runtime/graph_loader.py` → `render_graph.py` → `td_backend.py`) consumes. It is the
reference `compileGraph` output (`reference/03`, `reference/04`) with Maps serialized as objects and a
few convenience fields added so the loader never has to re-derive them from program-id string
encodings. The concrete examples below are taken from a real `parity/out/*.graph.json` (the
`noise().write(o0); render(o0)` program).

```jsonc
{
  "id": "abc123",                  // hashSource(dsl)
  "source": "search synth\nnoise().write(o0)\nrender(o0)",
  "renderSurface": "o0",           // surface presented to screen / output (or null)

  "passes": [ /* Pass[] in execution order */ ],

  // allocations: virtual pooled texId -> physical slot id. global_* surfaces are NOT here.
  "allocations": { "node_0_out": "phys_0" },

  // textures: texId -> TextureSpec (pooled + effect-declared). global_* surfaces are NOT keys here.
  "textures": {
    "node_0_out": { "width": "screen", "height": "screen", "format": "rgba16f",
                    "usage": ["render", "sample", "copySrc", "copyDst"] }
  },

  // programs: optional metadata keyed by program id. The TD builder resolves the per-effect `.frag`
  // from disk by (namespace, func, progName), so effect shader SOURCE is not carried here. Typically
  // just "blit"; std140 effects (remap) carry a uniformLayout. Keys are "blit" or the full
  // define-suffixed id (e.g. "node_0_noise__LOOP_OFFSET_300__NOISE_TYPE_10").
  "programs": { "blit": { "uniformLayout": { /*...*/ }, "defines": {} } }
}
```

## Pass (normalized)

```jsonc
{
  "id": "node_0_pass_0",
  "passType": "effect",            // "effect" | "blit"

  // --- shader resolution (added convenience fields) ---
  "namespace": "synth",            // = pass.effectNamespace
  "func": "noise",                 // = pass.effectFunc
  "progName": "noise",             // bare basename -> td/noisemaker/shaders/effects/synth/noise/noise.frag
  "program": "node_0_noise__LOOP_OFFSET_300__NOISE_TYPE_10",  // full (define-suffixed) program id; keys into "programs" when present
  "defines": { "NOISE_TYPE": 10, "LOOP_OFFSET": 300 },        // compile-time consts -> injected as #define lines ABOVE the GLSL (bool-typed ones as true/false), never bound as uniforms

  // --- pass wiring (from reference Pass) ---
  "inputs":  {},                                   // samplerName -> texId | "none"   (e.g. {"inputTex":"node_0_out"})
  "outputs": { "fragColor": "node_0_out" },        // out-var name -> texId  (effect passes; see Output keys below)
  "uniforms":     { "scaleX": 75, "seed": 1 },     // name -> literal value (objects for vec/color args)
  "uniformSpecs": { "scaleX": { "min": 1, "max": 100 } },

  // --- optional execution modifiers ---
  "drawMode": "points",            // scatter pass -> Geo COMP + GLSL MAT + Render TOP
  "count": 4096,                   // or
  "countUniform": "stateSize",     // dynamic count from a uniform (count = value*value for points)
  "drawBuffers": 2,                // MRT attachment count
  "blend": true,                   // additive deposit -> blendFunc(ONE, ONE)
  "repeat": "iterations",          // int or uniform-name -> unrolled into N chained GLSL TOPs

  // --- metadata ---
  "effectKey": "synth.noise",      // ALWAYS namespace-qualified
  "nodeId": "node_0",
  "stepIndex": 0,
  "scopedParams": null             // { origParam: scopedParam } when present
}
```

**Output keys** depend on the pass kind:

- **effect** passes write the shader's `out vec4` name — almost always `outputs: { "fragColor": <texId> }`.
- **blit** passes write `outputs: { "color": "global_o*" }` — the *only* place a `color` → `global_*`
  mapping appears. The terminal `write(o0)` is a blit: `inputs:{src:node_0_out}`, `outputs:{color:global_o0}`.
- **MRT** passes write the effect's actual out-var names — e.g. `outXYZ,outVel,outRGBA(,outData)`
  (3D volume), `outState1,outState2,outState3` (agent state), or `color,geoOut` (lit/geo render) —
  not `color,color1,…`.

## TextureSpec & dimensions (reference/04 §9)

```jsonc
{ "width": <Dim>, "height": <Dim>, "depth"?: <Dim>, "is3D"?: bool, "format"?: "rgba16f", "usage"?: ["render","sample",…] }
```
`depth`/`is3D` are accepted but **unused** — TD "volumes" are **2D atlases** (`width × (height·volumeSize)`),
never `createTexture3D`. `Dim` is one of: a number; `"screen"`/`"auto"`; a percent string `"6.25%"`;
or an object `{param, paramDefault?, multiply?, power?, default?}` | `{screenDivide, default?}` |
`{scale, clamp?}`. Resolve with the exact rounding rules in `reference/04 §9` (`floor` for
param/percent/scale, `round` for screenDivide, always `max(1, …)`).

## texId conventions

- `global_<name>` — double-buffered global surface (`o0..o7`, `geo*`, `vol*`, dynamic). Excluded from
  pooling/liveness. Maps to a `surface_manager` double-buffered TOP pair. (Never a `textures`/`allocations` key.)
- `phys_N` — a pooled physical slot (from the liveness allocator).
- everything else (e.g. `node_0_out`) — a virtual pooled texId mapped via `allocations` to a `phys_N`.

## Formats (all linear — TD GLSL TOPs never apply sRGB)

GLSL TOP `format` (Common page): `rgba16f`/`rgba16float` → **16-bit float (RGBA)** (`rgba16float`);
`rgba32f` → **32-bit float (RGBA)** (`rgba32float`); `rgba8`/`rgba8unorm` → **8-bit fixed (RGBA)**
(`rgba8fixed`). Set per-TOP with `outputresolution='custom'` + `resolutionw`/`resolutionh` + `format`;
the builder (`td_backend.py`) applies these from each pass's primary-output `TextureSpec`.

## Producers

- **Golden:** `tools/export-graph.mjs` runs the unchanged reference `compileGraph`, then normalizes
  (Maps→objects, adds `passType/namespace/func/progName/defines`).
- **Live:** the Python `Expander` (`td/noisemaker/compiler/lang/expander.py`) emits this shape directly.

Both must produce byte-identical normalized JSON for the same DSL (modulo the top-level `id` and
`source`), asserted by `parity/compiler/check_graph.py` against the `export-graph.mjs` oracle — the
live-path parity test.
