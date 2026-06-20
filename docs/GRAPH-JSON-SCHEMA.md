# Render Graph JSON Schema (the C# ↔ exporter contract)

This is the **normalized** graph format that both producers emit and the C# runtime
consumes. It is the reference `compileGraph` output (`reference/03`, `reference/04`)
with Maps serialized as objects and a few convenience fields added so the HLSL
loader never has to re-derive them from program-id string encodings.

```jsonc
{
  "id": "abc123",                  // hashSource(dsl)
  "source": "search synth\nnoise().write(o0)\nrender(o0)",
  "renderSurface": "o0",           // surface presented to screen / output RT (or null)

  "passes": [ /* Pass[] in execution order */ ],

  // allocations: virtual pooled texId -> physical slot id. globals are NOT here.
  "allocations": { "node_0_outputTex": "phys_0" },

  // textures: texId -> TextureSpec (pooled + effect-declared + global_ overrides)
  "textures": {
    "global_o0": { "width": "screen", "height": "screen", "format": "rgba16f" },
    "node_0_outputTex": { "width": "screen", "height": "screen", "format": "rgba16f" }
  },

  // programs: optional, raw reference program specs (glsl/wgsl/uniformLayout/defines).
  // The HLSL loader does NOT need shader source from here — it resolves a Unity
  // Shader by (namespace, func) and pass name. Kept for traceability / golden diff.
  "programs": { "node_0_noise": { "uniformLayout": { /*...*/ }, "defines": {} } }
}
```

## Pass (normalized)

```jsonc
{
  "id": "node_0_pass_0",
  "passType": "effect",            // "effect" | "blit"  (added; reference uses absence/type:"render")

  // --- shader resolution (added convenience fields) ---
  "namespace": "synth",            // = pass.effectNamespace
  "func": "noise",                 // = pass.effectFunc        -> Unity Shader "Noisemaker/synth/noise"
  "progName": "noise",             // bare program basename     -> Unity pass name within that shader
  "defines": { "NOISE_TYPE": 10, "LOOP_OFFSET": 300 },  // compile-time consts -> bound as int uniforms

  // --- pass wiring (from reference Pass) ---
  "inputs":  { "inputTex": "node_0_outputTex" },   // samplerName -> texId | "none"
  "outputs": { "color": "global_o0" },             // attachment  -> texId  (MRT: color,color1,...)
  "uniforms":     { "scaleX": 75, "seed": 1 },     // name -> literal value
  "uniformSpecs": { "scaleX": { "min": 1, "max": 100 } },

  // --- optional execution modifiers ---
  "drawMode": "points",            // scatter pass -> DrawProcedural(Points, count)
  "count": 4096,                   // or
  "countUniform": "stateSize",     // dynamic count from a uniform (count = value*value for points)
  "drawBuffers": 2,                // MRT attachment count
  "blend": true,                   // additive deposit -> Blend One One
  "repeat": "iterations",          // int or uniform-name: run pass N times/frame
  "clear": null,

  // --- metadata ---
  "effectKey": "noise",
  "nodeId": "node_0",
  "stepIndex": 0,
  "inheritsVolumeSize": false,
  "scopedParams": null             // { origParam: scopedParam } when present
}
```

Blit passes use `"passType":"blit"`, `"func":"blit"`, `inputs:{src:...}`,
`outputs:{color:...}`, empty uniforms.

## TextureSpec & dimensions (`reference/04 §9`)

```jsonc
{ "width": <Dim>, "height": <Dim>, "depth"?: <Dim>, "is3D"?: bool, "format"?: "rgba16f" }
```
`Dim` is one of: a number; `"screen"`/`"auto"`; a percent string `"6.25%"`; or an object
`{param, paramDefault?, multiply?, power?, default?}` | `{screenDivide, default?}` |
`{scale, clamp?}`. Resolve with the exact rounding rules in `reference/04 §9`
(`floor` for param/percent/scale, `round` for screenDivide, always `max(1, …)`).

## texId conventions

- `global_<name>` — double-buffered global surface (`o0..o7`, `geo*`, `vol*`, dynamic).
  Excluded from pooling/liveness. Maps to a `SurfaceManager` RT pair.
- `phys_N` — a pooled physical slot (from the liveness allocator).
- everything else — a virtual pooled texId mapped via `allocations` to a `phys_N`.

## Formats

`rgba16f`/`rgba16float` → `RenderTextureFormat.ARGBHalf` (linear). `rgba32f` →
`ARGBFloat`. `rgba8`/`rgba8unorm` → `ARGB32` (linear). **Never sRGB.** All RTs created
with `RenderTextureReadWrite.Linear`.

### TouchDesigner formats

GLSL TOP `format` (Common page), all **linear** — TD GLSL TOPs never apply sRGB:
`rgba16f`/`rgba16float` → **16-bit float (RGBA)** (`rgba16float`); `rgba32f` → **32-bit
float (RGBA)** (`rgba32float`); `rgba8`/`rgba8unorm` → **8-bit fixed (RGBA)** (`rgba8fixed`).
Set per-TOP with `outputresolution='custom'` + `resolutionw`/`resolutionh` + `format`. The
builder (`td/noisemaker/runtime/td_backend.py`) applies these from each pass's primary-output
`TextureSpec`.

## Producers

- **Golden:** `tools/export-graph.mjs` runs the unchanged reference `compileGraph`,
  then normalizes (Maps→objects, adds `passType/namespace/func/progName/defines`).
- **Live:** the C# `Expander` emits this shape directly.

Both must produce byte-identical normalized JSON for the same DSL (modulo `compiledAt`),
which is the live-path parity test.
