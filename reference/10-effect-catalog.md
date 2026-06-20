# Effect Catalog — Reference Specification for C#/HLSL Port

**Subsystem:** Effect Catalog  
**Source:** `shaders/effects/{namespace}/{effectName}/definition.js`  
**Total effects enumerated:** 175  
**Generated from:** Live codebase scan, 2026-06-17

---

## 1. Overview of the Catalog

The effect catalog is the complete set of renderable nodes in the Noisemaker pipeline. Each effect is defined by a `definition.js` file under `shaders/effects/{namespace}/{effectName}/`. Every definition instantiates an `Effect` object (either via `new Effect({...})` or as an ES class extending `Effect`), and declares:

- `name` / `namespace` / `func` — identity fields
- `description` — human-readable one-liner
- `globals` — user-facing parameters (uniforms, compile-time defines, surface refs)
- `passes[]` — ordered render-pass chain
- `textures` — internal (private) texture allocations
- Optional: `uniformLayout` or `uniformLayouts` — explicit WGSL vec4-packing maps
- Optional: `paramAliases` — legacy DSL name → current param name
- Optional: `outputXyz/outputVel/outputRgba/outputGeo/outputTex3d` — outputs exposed to downstream pipeline nodes
- Optional: `externalTexture` / `externalMesh` — signals that CPU-side data must be uploaded each frame

Shader programs live in `glsl/` and `wgsl/` subdirectories. Every effect has exactly one GLSL file and one WGSL file per pass program (no exceptions found in the full scan). Some effects have additional WGSL-only variants (e.g., a `.compute.wgsl`) when the WebGPU path needs a separate compute shader.

---

## 2. Effect Definition Schema

### 2.1 Pass Object

```js
{
  name: string,          // Unique within this effect
  program: string,       // Shader filename without extension (resolved from glsl/ and wgsl/)
  inputs: {              // tex sampler name → pipeline texture reference
    [samplerName: string]: string  // "inputTex" | "global_*" | "_localTex" | effect param name
  },
  outputs: {             // output attachment name → pipeline texture reference
    [outputName: string]: string   // "outputTex" | "global_*" | "_localTex"
  },
  uniforms?: {           // optional: explicit uniform → param binding overrides
    [uniformName: string]: string
  },
  drawMode?: "points",   // If present, rendered as gl_Points / WGSL point topology
  drawBuffers?: number,  // MRT: how many color attachments (default 1)
  blend?: boolean,       // Additive blending for deposit passes
  count?: "input",       // Point count derived from xyzTex dimensions
  repeat?: string,       // Param name whose integer value sets loop count
  viewport?: {           // Custom viewport for 3D atlas rendering
    width: ..., height: ...
  }
}
```

### 2.2 Global Parameter Object

```js
{
  type: "int" | "float" | "boolean" | "color" | "vec2" | "vec3" | "surface" | "palette" | "string",
  default: value,        // Canonical default value
  uniform?: string,      // If present, written to the WGSL uniform buffer each frame
  define?: string,       // If present, a compile-time #define / WGSL override constant
  min?: number,          // Slider/input range
  max?: number,
  step?: number,
  zero?: number,         // Value that means "zero/off" in param normalization
  randMin?: number,      // Randomization range (narrower than min/max)
  randMax?: number,
  randChance?: number,   // Probability of being randomized (0 = never, default ~1)
  choices?: {[label]: value},  // Enum dropdown mapping
  randChoices?: value[], // Subset of choices used for randomization
  ui: { label, control, category, hidden, enabledBy }
}
```

### 2.3 Texture Allocation Object

```js
{
  width: "100%" | "input" | "resolution" | "6.25%" | number | { param, default, power? } | { screenDivide, default },
  height: same,
  format?: "rgba8unorm" | "rgba8" | "rgba16f" | "rgba16float" | "rgba32f"
}
```

Width/height semantics:
- `"100%"` — full render resolution
- `"input"` — same as input texture size
- `"resolution"` — square at current resolution
- `"6.25%"` — 1/16th of resolution (GPGPU pyramid)
- `{ param: 'volumeSize', default: 64 }` — driven by a global param
- `{ param: 'volumeSize', power: 2, default: 4096 }` — volumeSize² (3D atlas height)
- `{ screenDivide: 'zoom', default: N }` — resolution divided by zoom param

### 2.4 Texture Name Conventions

| Prefix | Scope | Semantics |
|--------|-------|-----------|
| `global_` | Pipeline-global | Shared across effects (agent state textures, NS velocity, etc.) |
| `_` | Effect-local, persistent | Survives frame to frame (feedback, history) |
| _(no prefix)_ | Effect-local, transient | Intermediate/temp within a single frame |
| `outputTex` | Reserved | Pipeline output to downstream effect |
| `inputTex` | Reserved | Pipeline input from upstream effect |
| `inputTex3d` | Reserved | 3D volume from upstream synth3d effect |
| `inputGeo` | Reserved | Geometry buffer from upstream 3D effect |

---

## 3. Complete Effect Catalog Table

Legend for columns:
- **Passes** = number of render passes in `passes[]`
- **GLSL/WGSL** = shader file count (always equal unless WGSL-only compute variant present)
- **Points** = has a `drawMode: "points"` pass
- **State** = has persistent inter-frame state (global_ or _ textures)

### 3.1 Namespace: `synth` — Generators (no input required)

| Effect | func | Description | Passes | GLSL | WGSL | Points | State |
|--------|------|-------------|--------|------|------|--------|-------|
| bitwise | `bitwise` | Bitwise operation patterns (XOR squares, AND, OR, etc.) | 1 | 1 | 1 | N | N |
| cell | `cell` | Cellular/Voronoi noise with distance metrics | 1 | 1 | 1 | N | N |
| cellularAutomata | `cellularAutomata` | 2D cellular automata with rule presets | 2 | 2 | 2 | N | Y |
| curl | `curl` | 3D curl noise using simplex noise | 1 | 1 | 1 | N | N |
| gabor | `gabor` | Anisotropic bandlimited noise via sparse Gabor convolution | 1 | 1 | 1 | N | N |
| gradient | `gradient` | Multi-color gradient generator with various styles | 1 | 1 | 1 | N | N |
| julia | `julia` | Julia set explorer with deep zoom | 1 | 1 | 1 | N | N |
| mandala | `mandala` | N-fold symmetric mandala generator | 1 | 1 | 1 | N | N |
| mandelbrot | `mandelbrot` | Mandelbrot explorer with deep zoom | 1 | 1 | 1 | N | N |
| media | `media` | Video/camera/image input | 1 | 1 | 1 | N | N |
| mnca | `mnca` | Multi-neighborhood cellular automata | 2 | 2 | 2 | N | Y |
| modPattern | `modPattern` | Interference patterns from modulo operations | 1 | 1 | 1 | N | N |
| navierStokes | `navierStokes` | Stable-fluids Navier-Stokes solver | 7 | 7 | 7 | N | Y |
| newton | `newton` | Newton fractal explorer with deep zoom | 1 | 1 | 1 | N | N |
| noise | `noise` | Value noise with multiple interpolation types | 1 | 1 | 1 | N | N |
| osc2d | `osc2d` | 2D oscillator pattern | 1 | 1 | 1 | N | N |
| pattern | `pattern` | Geometric pattern generator | 1 | 1 | 1 | N | N |
| perlin | `perlin` | Perlin-like noise with optional warping | 1 | 1 | 1 | N | N |
| polygon | `polygon` | Geometric shape generator | 1 | 1 | 1 | N | N |
| reactionDiffusion | `reactionDiffusion` | Gray-Scott reaction-diffusion | 2 | 2 | 2 | N | Y |
| remap | `remap` | Polygon zones routed to engine surfaces | 1 | 1 | 1 | N | N |
| roll | `roll` | MIDI piano roll visualizer | 2 | 2 | 2 | N | N |
| sacredGeometry | `sacredGeometry` | Flower-of-life and sacred-geometry lattices | 1 | 1 | 1 | N | N |
| scope | `scope` | Audio waveform oscilloscope | 1 | 1 | 1 | N | N |
| shape | `shape` | Interference patterns from geometric shapes | 1 | 1 | 1 | N | N |
| solid | `solid` | Solid color fill | 1 | 1 | 1 | N | N |
| spectrum | `spectrum` | Audio spectrum analyzer | 1 | 1 | 1 | N | N |
| subdivide | `subdivide` | Recursive grid subdivision with shapes | 1 | 1 | 1 | N | N |
| testPattern | `testPattern` | Test patterns for debugging and calibration | 1 | 1 | 1 | N | N |

### 3.2 Namespace: `filter` — Single-input Filters

| Effect | func | Description | Passes | GLSL | WGSL | Points | State |
|--------|------|-------------|--------|------|------|--------|-------|
| adjust | `adjust` | Colorspace, hue/saturation, brightness/contrast | 1 | 1 | 1 | N | N |
| bc | `bc` | *Deprecated* — use adjust. Brightness/contrast | 1 | 1 | 1 | N | N |
| bloom | `bloom` | Multi-pass bloom: bright-pass + N-tap gather + composite | 3 | 3 | 3 | N | N |
| blur | `blur` | Separable Gaussian blur (H + V passes) | 2 | 2 | 2 | N | N |
| bulge | `bulge` | Bulge distortion from center | 1 | 1 | 1 | N | N |
| celShading | `celShading` | Cartoon-style shading with posterization and outlines | 3 | 3 | 3 | N | N |
| channel | `channel` | Channel isolation (r, g, b, or a) | 1 | 1 | 1 | N | N |
| chroma | `chroma` | Isolate specific hue range with feathering | 1 | 1 | 1 | N | N |
| chromaticAberration | `chromaticAberration` | Color fringing simulating lens aberration | 1 | 1 | 1 | N | N |
| clouds | `clouds` | Cloud texture overlay | 1 | 1 | 1 | N | N |
| colorReplace | `colorReplace` | Color replacement with alpha output | 1 | 1 | 1 | N | N |
| colorspace | `colorspace` | *Deprecated* — use adjust. HSV/OKLab/OKLCH interpret | 1 | 1 | 1 | N | N |
| convolutionFeedback | `convolutionFeedback` | Convolution feedback with blur and sharpen | 3 | 3 | 3 | N | Y |
| corrupt | `corrupt` | Scanline-based data corruption | 1 | 1 | 1 | N | N |
| crt | `crt` | CRT monitor simulation | 1 | 1 | 1 | N | N |
| degauss | `degauss` | CRT degauss effect | 1 | 1 | 1 | N | N |
| deriv | `deriv` | Derivative-based edge detection | 1 | 1 | 1 | N | N |
| dither | `dither` | Ordered dithering with classic patterns and palettes | 1 | 1 | 1 | N | N |
| edge | `edge` | Edge detection filter | 1 | 1 | 1 | N | N |
| emboss | `emboss` | Emboss effect creating raised relief appearance | 1 | 1 | 1 | N | N |
| feedback | `feedback` | Feedback loop with blend modes and transforms | 2 | 2 | 2 | N | Y |
| fibers | `fibers` | Chaotic fiber texture overlay | 1 | 1 | 1 | N | N |
| flipMirror | `flipMirror` | Flip and mirror image transformations | 1 | 1 | 1 | N | N |
| fxaa | `fxaa` | Fast approximate anti-aliasing | 1 | 1 | 1 | N | N |
| glowingEdge | `glowingEdge` | Glowing edge detection | 1 | 1 | 1 | N | N |
| glyphMap | `glyphMap` | ASCII/glyph art conversion using procedural glyphs | 1 | 1 | 1 | N | N |
| grade | `grade` | Professional multi-stage color grading pipeline | 6 | 6 | 6 | N | N |
| grain | `grain` | Film grain overlay | 1 | 1 | 1 | N | N |
| grime | `grime` | Grunge/grime texture overlay | 1 | 1 | 1 | N | N |
| historicPalette | `historicPalette` | Apply historical art color palettes | 1 | 1 | 1 | N | N |
| hs | `hs` | *Deprecated* — use adjust. Hue/saturation | 1 | 1 | 1 | N | N |
| invert | `invert` | Invert image luminance | 1 | 1 | 1 | N | N |
| lens | `lens` | Barrel or pincushion lens distortion | 1 | 1 | 1 | N | N |
| lensWarp | `lensWarp` | Noise-driven radial lens distortion | 1 | 1 | 1 | N | N |
| lightLeak | `lightLeak` | Film light leak overlay with colorful Voronoi regions | 1 | 1 | 1 | N | N |
| lighting | `lighting` | Applies 3D lighting effects | 1 | 1 | 1 | N | N |
| lowPoly | `lowPoly` | Low-polygon style render using Voronoi cells | 1 | 1 | 1 | N | N |
| motionBlur | `motionBlur` | Simple motion blur via frame blending | 2 | 2 | 2 | N | Y |
| normalMap | `normalMap` | Normal map generation | 1 | 1 | 1 | N | N |
| normalize | `normalize` | Value normalization (GPGPU pyramid reduction) | 4 | 4 | 4 | N | N |
| octaveWarp | `octaveWarp` | Per-octave noise warp distortion | 1 | 1 | 1 | N | N |
| osd | `osd` | On-screen display overlay | 1 | 1 | 1 | N | N |
| outline | `outline` | Outline/edge stroke | 3 | 3 | 3 | N | N |
| palette | `palette` | Apply cosine color palettes based on luminance | 1 | 1 | 1 | N | N |
| pinch | `pinch` | Pinch distortion toward center | 1 | 1 | 1 | N | N |
| pixels | `pixels` | Pixelation effect for retro look | 1 | 1 | 1 | N | N |
| pixelSort | `pixelSort` | Pixel sorting glitch effect (GPGPU 6-pass) | 6 | 6 | 6 | N | N |
| polar | `polar` | Polar and vortex coordinate transforms | 1 | 1 | 1 | N | N |
| posterize | `posterize` | Posterization/color reduction with gamma control | 1 | 1 | 1 | N | N |
| prismaticAberration | `prismaticAberration` | Prismatic aberration with hue controls | 1 | 1 | 1 | N | N |
| reindex | `reindex` | Palette reindexing | 3 | 3 | 3 | N | Y |
| repeat | `repeat` | Tiling repeat | 1 | 1 | 1 | N | N |
| reverb | `reverb` | Visual reverb/echo effect | 1 | 1 | 1 | N | N |
| ridge | `ridge` | Ridge/crease enhancement | 1 | 1 | 1 | N | N |
| rotate | `rotate` | Rotate image by specified angle | 1 | 1 | 1 | N | N |
| scale | `scale` | Scale transform | 1 | 1 | 1 | N | N |
| scanlineError | `scanlineError` | Scanline glitch / VHS tape artifacts | 1 | 1 | 1 | N | N |
| scratches | `scratches` | Film scratch overlay | 1 | 1 | 1 | N | N |
| scroll | `scroll` | Scrolling offset animation | 1 | 1 | 1 | N | N |
| seamless | `seamless` | Edge-blend cross-fade for seamless tiling | 1 | 1 | 1 | N | N |
| sharpen | `sharpen` | Sharpen using convolution | 1 | 1 | 1 | N | N |
| simpleAberration | `simpleAberration` | Chromatic aberration (spatial) | 1 | 1 | 1 | N | N |
| sine | `sine` | Sine wave color transform | 1 | 1 | 1 | N | N |
| skew | `skew` | Skew and rotate transform | 1 | 1 | 1 | N | N |
| smooth | `smooth` | Anti-aliasing (MSAA/SMAA/edge-blur) | 2 | 2 | 2 | N | N |
| smoothstep | `smoothstep` | Smooth Hermite interpolation between edges | 1 | 1 | 1 | N | N |
| snow | `snow` | TV snow/static noise | 1 | 1 | 1 | N | N |
| sobel | `sobel` | Classic Sobel edge detection | 1 | 1 | 1 | N | N |
| spatter | `spatter` | Paint spatter effect | 1 | 1 | 1 | N | N |
| spiral | `spiral` | Spiral distortion | 1 | 1 | 1 | N | N |
| spookyTicker | `spookyTicker` | Scrolling pseudo-text ticker overlay | 1 | 1 | 1 | N | N |
| step | `step` | Hard threshold at specified value | 1 | 1 | 1 | N | N |
| strayHair | `strayHair` | Stray hair overlay | 1 | 1 | 1 | N | N |
| temporalAberration | `temporalAberration` | Per-channel temporal frame delay aberration (8-stage shift register) | 9 | 2 | 2 | N | Y |
| tetraColorArray | `tetraColorArray` | Apply Tetra color array palettes based on luminance | 1 | 1 | 1 | N | N |
| tetraCosine | `tetraCosine` | Apply Tetra cosine palettes based on luminance | 1 | 1 | 1 | N | N |
| text | `text` | Overlay CPU-rendered text onto the image | 1 | 1 | 1 | N | N |
| texture | `texture` | Texture overlay blend | 1 | 1 | 1 | N | N |
| threshold | `threshold` | Threshold/step function | 1 | 1 | 1 | N | N |
| tile | `tile` | Symmetry-based kaleidoscope tiler | 1 | 1 | 1 | N | N |
| tint | `tint` | Colorize input texture with a color overlay | 1 | 1 | 1 | N | N |
| translate | `translate` | Translate image in X and Y | 1 | 1 | 1 | N | N |
| tunnel | `tunnel` | Perspective tunnel effect with shape options | 1 | 1 | 1 | N | N |
| vaseline | `vaseline` | Vaseline lens blur effect | 1 | 1 | 1 | N | N |
| vignette | `vignette` | Radial vignette darkening edges | 1 | 1 | 1 | N | N |
| warp | `warp` | Perlin noise-based warp distortion | 1 | 1 | 1 | N | N |
| waves | `waves` | Sine wave distortion | 1 | 1 | 1 | N | N |
| wobble | `wobble` | Wobble animation effect | 1 | 1 | 1 | N | N |
| wormhole | `wormhole` | Luminance-driven scatter displacement field | 3 | 2 | 3 | Y | N |
| zoomBlur | `zoomBlur` | Radial blur emanating from center | 1 | 1 | 1 | N | N |

### 3.3 Namespace: `filter3d` — 3D Filters

| Effect | func | Description | Passes | GLSL | WGSL | Points | State |
|--------|------|-------------|--------|------|------|--------|-------|
| flow3d | `flow3d` | 3D agent-based flow field | 5 | 4 | 5 | Y | Y |

### 3.4 Namespace: `mixer` — Two-input Mixers

| Effect | func | Description | Passes | GLSL | WGSL | Points | State |
|--------|------|-------------|--------|------|------|--------|-------|
| alphaMask | `alphaMask` | Alpha transparency blend | 1 | 1 | 1 | N | N |
| applyMode | `applyMode` | Apply brightness, hue, or saturation from B to A | 1 | 1 | 1 | N | N |
| blendMode | `blendMode` | Blend two inputs using selectable blend mode | 1 | 1 | 1 | N | N |
| cellSplit | `cellSplit` | Split between inputs using Voronoi cell regions | 1 | 1 | 1 | N | N |
| centerMask | `centerMask` | Blend edges(A) into center(B) using distance mask | 1 | 1 | 1 | N | N |
| channelCombine | `channelCombine` | Combine separate surfaces into R, G, B channels | 1 | 1 | 1 | N | N |
| distortion | `distortion` | Displace, reflect, and refract with two surfaces | 1 | 1 | 1 | N | N |
| focusBlur | `focusBlur` | Focus blur using luminance depth map | 1 | 1 | 1 | N | N |
| patternMix | `patternMix` | Mix inputs using geometric patterns | 1 | 1 | 1 | N | N |
| shadow | `shadow` | Cast shadow or glow from one input onto another | 1 | 1 | 1 | N | N |
| shapeMask | `shapeMask` | Composite inputs inside/outside a geometric shape | 1 | 1 | 1 | N | N |
| split | `split` | Split/wipe between two inputs | 1 | 1 | 1 | N | N |
| thresholdMix | `thresholdMix` | Blend using threshold masking | 1 | 1 | 1 | N | N |
| uvRemap | `uvRemap` | Remap UVs of one input using color channels of another | 1 | 1 | 1 | N | N |

### 3.5 Namespace: `points` — Agent/Particle Systems (middleware)

These effects read from and write to shared agent state textures (`global_xyz`, `global_vel`, `global_rgba`) established by `render/pointsEmit`. They are middleware nodes: they sit between `pointsEmit` and `pointsRender`.

| Effect | func | Description | Passes | GLSL | WGSL | Points | State |
|--------|------|-------------|--------|------|------|--------|-------|
| attractor | `attractor` | Strange attractors: chaotic dynamic systems | 2 | 2 | 2 | N | Y |
| buddhabrot | `buddhabrot` | Buddhabrot fractal via progressive orbit accumulation | 3 | 3 | 3 | N | Y |
| dla | `dla` | Diffusion-limited aggregation | 5 | 4 | 5 | Y | Y |
| flock | `flock` | 2D Boids flocking simulation | 2 | 2 | 2 | N | Y |
| flow | `flow` | Agent-based luminosity flow field with behaviors | 2 | 2 | 2 | N | Y |
| hydraulic | `hydraulic` | Hydraulic erosion flow simulation (gradient descent) | 2 | 2 | 2 | N | Y |
| lenia | `lenia` | Particle Lenia artificial life simulation | 5 | 4 | 5 | Y | Y |
| life | `life` | Type-based attraction/repulsion particle simulation | 3 | 3 | 3 | N | Y |
| physarum | `physarum` | Physarum slime mold simulation | 5 | 3 | 4 | Y | Y |
| physical | `physical` | Physics-based particle simulation with wind/gravity | 2 | 2 | 2 | N | Y |

### 3.6 Namespace: `render` — Render Infrastructure

| Effect | func | Description | Passes | GLSL | WGSL | Points | State |
|--------|------|-------------|--------|------|------|--------|-------|
| loopBegin | `loopBegin` | Start accumulator loop, read from feedback buffer | 1 | 1 | 1 | N | Y |
| loopEnd | `loopEnd` | End accumulator loop, write back to feedback buffer | 2 | 1 | 1 | N | Y |
| meshLoader | `meshLoader` | Load OBJ mesh data into GPU textures | 1 | 1 | 1 | N | Y |
| meshRender | `meshRender` | Render meshes with Blinn-Phong lighting | 2 | 1 | 2 | N | Y |
| pointsBillboardRender | `pointsBillboardRender` | Render particles as billboard sprites | 4 | 3 | 4 | N | Y |
| pointsEmit | `pointsEmit` | Initialize and maintain agent state textures | 2 | 2 | 2 | N | Y |
| pointsRender | `pointsRender` | Blend agent trails with input (diffuse+deposit+blend) | 4 | 3 | 4 | Y | Y |
| render3d | `render3d` | Universal 3D volume raymarcher (isosurface or voxel DDA) | 1 | 1 | 1 | N | N |
| renderLit3d | `renderLit3d` | Universal 3D volume raymarcher with advanced lighting | 1 | 1 | 1 | N | N |

### 3.7 Namespace: `synth3d` — 3D Volume Generators

These effects output to `outputTex3d` (a 2D atlas storing a 3D volume) and an optional `outputGeo` (normal/SDF data atlas). They are consumed by `render/render3d` or `render/renderLit3d`.

Volume storage layout: 2D atlas of size `(volumeSize × volumeSize²)`, i.e., all Z-slices stacked vertically. A 64³ volume = 64×4096 atlas.

| Effect | func | Description | Passes | GLSL | WGSL | Points | State |
|--------|------|-------------|--------|------|------|--------|-------|
| cell3d | `cell3d` | 3D cellular/Voronoi noise volume | 1 | 1 | 1 | N | N |
| cellularAutomata3d | `cellularAutomata3d` | 3D cellular automata simulation | 1 | 1 | 1 | N | Y |
| flythrough3d | `flythrough3d` | 3D fractal flythrough with camera-relative volume | 1 | 1 | 1 | N | N |
| fractal3d | `fractal3d` | 3D Mandelbulb/Mandelcube fractals | 1 | 1 | 1 | N | N |
| noise3d | `noise3d` | 3D simplex noise volume | 1 | 1 | 1 | N | N |
| reactionDiffusion3d | `reactionDiffusion3d` | 3D reaction-diffusion simulation | 1 | 1 | 1 | N | Y |
| shape3d | `shape3d` | 3D polyhedral shape generator | 1 | 1 | 1 | N | N |

### 3.8 Namespace: `classicNoisedeck` — Legacy Effects

These are older effects retained for backward DSL compatibility. They use ES class syntax for definitions (not object literal). Most are 1-pass. They share the `classicNoisedeck/` namespace and follow the older, more complex uniform packing pattern with explicit `uniformLayout` fields.

**PARITY HAZARD:** classicNoisedeck definitions extensively use compile-time `define:` fields to avoid ANGLE→D3D HLSL inlining pathologies that caused 35–85 second compile stalls on Windows Chrome. Each unique combination of define values produces a distinct compiled program. HLSL does not have the same inlining behavior; implement as runtime branches in HLSL unless profiling shows otherwise.

| Effect | func | Description | Passes | GLSL | WGSL |
|--------|------|-------------|--------|------|------|
| bitEffects | `bitEffects` | Bit field and bit mask effects | 1 | 1 | 1 |
| caustic | `caustic` | Dual-noise caustic pattern with reflect blend | 1 | 1 | 1 |
| cellNoise | `cellNoise` | Cellular noise patterns | 1 | 1 | 1 |
| cellRefract | `cellRefract` | Cell-based refraction | 1 | 1 | 1 |
| coalesce | `coalesce` | Coalescing blend effect | 1 | 1 | 1 |
| colorLab | `colorLab` | Color lab effects | 1 | 1 | 1 |
| composite | `composite` | Multi-layer compositing | 1 | 1 | 1 |
| effects | `effects` | Multi-effect processor | 1 | 1 | 1 |
| fractal | `fractal` | Fractal pattern generator | 1 | 1 | 1 |
| glitch | `glitch` | Digital glitch effects | 1 | 1 | 1 |
| kaleido | `kaleido` | Kaleidoscope effect | 1 | 1 | 1 |
| lensDistortion | `lensDistortion` | Lens distortion simulation | 1 | 1 | 1 |
| moodscape | `moodscape` | Refracted value noise with multiple color modes | 1 | 1 | 1 |
| noise | `noise` | Noise pattern generator | 1 | 1 | 1 |
| noise3d | `noise3d` | 3D noise volumes | 1 | 1 | 1 |
| refract | `refract` | Refraction distortion | 1 | 1 | 1 |
| shapeMixer | `shapeMixer` | Shape mixer/blend | 1 | 1 | 1 |
| shapes | `shapes` | Interference patterns from geometric shapes | 1 | 1 | 1 |
| shapes3d | `shapes3d` | 3D shape patterns | 1 | 1 | 1 |
| splat | `splat` | Splatter paint effect | 1 | 1 | 1 |

---

## 4. Special Effect Architectures

### 4.1 Agent/Particle Pipeline

The full particle pipeline requires these effects chained in order:

```
pointsEmit()  →  [physics middleware...]  →  pointsRender()
```

**`render/pointsEmit`** allocates three global state textures:
- `global_xyz`: `rgba32f`, `stateSize × stateSize` — position `[x, y, z, alive]`
- `global_vel`: `rgba32f`, `stateSize × stateSize` — velocity `[vx, vy, age, seed]`
- `global_rgba`: `rgba8`, `stateSize × stateSize` — color `[r, g, b, a]`

Default `stateSize = 256` (65,536 agents). Options: 64/128/256/512/1024/2048.

**`render/pointsRender`** allocates:
- `global_points_trail`: `rgba16f`, full resolution — accumulated trail texture

**Points middleware** (physarum, flock, flow, physical, attractor, life, lenia, dla, hydraulic, buddhabrot) all read/write `global_xyz`, `global_vel`, `global_rgba`. The `physarum` effect also allocates its own private `global_physarum_pheromone: rgba16f` at full resolution.

**`drawMode: "points"` pass mechanics:**
- Each point corresponds to one texel in `xyzTex` (i.e., `stateSize²` total draw calls)
- `count: 'input'` — point count is derived from `xyzTex` width × height
- `blend: true` — additive alpha blending enabled for deposit passes
- In WebGL2: rendered as `gl_POINTS`; fragment writes at the point's screen position
- In HLSL: rendered as a point topology draw; SV_Position set from `xyzTex` sample

### 4.2 3D Volume Pipeline

```
synth3d/*/  →  [synth3d/* chain...]  →  render/render3d  or  render/renderLit3d
```

**Volume storage:** A `volumeSize × volumeSize²` atlas texture (e.g., 64×4096 for a 64³ volume). Each row of `volumeSize` rows encodes one Z-slice. The atlas is `rgba16f` format.

**`render3d`** takes `inputTex3d` and renders to `outputTex` via raymarching. It also writes to a `screenGeoBuffer: rgba16f` at resolution×resolution.

**Compile-time defines in render3d:**
- `FILTERING = 0` (isosurface) or `1` (voxel DDA)
- `INVERT = 0|1` — whether to invert the threshold test

### 4.3 Feedback / State Effects

Effects with `_`-prefixed local textures (motionBlur, feedback, convolutionFeedback, temporalAberration, reindex) maintain inter-frame state. Their textures are NOT reset between frames. The render runtime must:
1. Allocate these textures with `persistent = true`
2. Never clear them between frames
3. Ping-pong where necessary (copy pass before overwriting source)

**`filter/temporalAberration`** is the most complex: 8 history textures `_h1`–`_h8` (all `rgba8unorm`) form a shift-register delay line. 9 passes: 1 read pass + 8 shift passes executed tail-first.

### 4.4 GPGPU Effects (No `type: "compute"` Flag)

The following effects use GPGPU render passes (full-screen quads writing to intermediate textures for data reduction) but are NOT marked `type: "compute"` — they use standard render passes. The WebGL2 backend handles them as ordinary draw calls to FBOs.

- `filter/normalize`: 4-pass pyramid reduction (1/16 → 1/256 → 1×1 → apply)
- `filter/pixelSort`: 6-pass GPU sort (prepare → luminance → findBrightest → computeRank → gatherSorted → finalize)
- `synth/navierStokes`: 7-pass fluid solver (splat → advect → divergence → pressure×N → gradient → smooth → render), `repeat: "iterations"` on pressure pass
- `synth/reactionDiffusion`: `repeat: "iterations"` on simulate pass
- `synth/cellularAutomata`: standard 2-pass (update → render)
- `synth/mnca`: standard 2-pass

### 4.5 Loop Begin/End

`render/loopBegin` and `render/loopEnd` implement an accumulator pattern:
- `loopBegin`: reads from a persistent feedback buffer and outputs it as `outputTex`
- `loopEnd`: composites the accumulated result back into the feedback buffer

Used for progressive rendering effects that compound across multiple iterations of a sub-graph.

### 4.6 Compile-time `define:` Parameters

Many effects declare parameters with `define: "SHADER_CONSTANT"` instead of (or in addition to) `uniform: "name"`. These become preprocessor `#define` values baked into the compiled shader rather than runtime-readable uniforms. Changing them triggers a shader recompile.

Rationale (from source comments): ANGLE→D3D (Windows Chrome) inlined entire if-cascade decision trees at compile time, producing 35–85 second stalls. Common patterns:

| Effect | Define | Purpose |
|--------|--------|---------|
| classicNoisedeck/noise | `NOISE_TYPE` | Selects from 9 noise variants |
| classicNoisedeck/noise | `REFRACT_MODE` | 3 refract modes (color/topo/colorTopo) |
| classicNoisedeck/noise | `LOOP_OFFSET` | 17 loop offset shapes |
| classicNoisedeck/noise | `METRIC` | 6 Voronoi distance metrics |
| classicNoisedeck/noise | `COLOR_MODE` | 6 colorization modes |
| classicNoisedeck/shapes | `LOOP_A_OFFSET`, `LOOP_B_OFFSET` | Shape A/B (35+ choices each) |
| synth/noise | `NOISE_TYPE`, `LOOP_OFFSET` | Same as above (modern port) |
| synth/perlin | `DIMENSIONS` | 2D vs 3D noise path |
| synth3d/noise3d | `OCTAVES`, `COLOR_MODE`, `RIDGES` | Loop unroll, dead-code elimination |
| render/render3d | `FILTERING`, `INVERT` | Isosurface vs voxel DDA |

**PARITY HAZARD for HLSL:** HLSL's DXIL compiler handles dead-code elimination differently from ANGLE's GLSL→HLSL translator. In HLSL, runtime branches over large switch/if-else chains typically compile fast. The `#define`-driven separate shader variants may be unnecessary in an HLSL port. However, they are semantically equivalent to runtime branching, so either approach produces identical output.

---

## 5. Porting Order Recommendation

### Tier 1 — Foundational Generators and Simple Filters (Port First)

These effects are stateless, single-pass, have no external dependencies, and their shaders are used as inputs for testing all other effects.

| Priority | Namespace | Effect | Why |
|----------|-----------|--------|-----|
| 1 | synth | `solid` | Trivial baseline; validates pipeline plumbing |
| 2 | synth | `testPattern` | Debugging and calibration reference |
| 3 | synth | `gradient` | Simple generator; tests UV and color output |
| 4 | synth | `noise` | Core noise primitive; depended on by many effects |
| 5 | synth | `perlin` | Perlin/fBm noise; tests domain warp path |
| 6 | synth | `shape` | Geometric SDF; tests coordinate transforms |
| 7 | filter | `invert` | Trivial filter; validates filter pipeline plumbing |
| 8 | filter | `tint` | Color overlay; tests uniform color type |
| 9 | filter | `adjust` | HSV/brightness/contrast; core utility filter |
| 10 | filter | `blur` | 2-pass separable Gaussian; tests multi-pass FBO |
| 11 | filter | `vignette` | Common post filter; tests radial coord |
| 12 | mixer | `blendMode` | Two-input blend; validates mixer pipeline |
| 13 | filter | `threshold` | Hard step; validates step/smoothstep math |
| 14 | synth | `cell` | Voronoi noise; tests distance functions |
| 15 | filter | `sobel` | Edge detection; tests kernel convolution |

### Tier 2 — Common Multi-pass / Stateful Effects

Port after Tier 1 is validated. These introduce multi-pass FBO chains and inter-frame persistent state.

| Priority | Namespace | Effect | Why |
|----------|-----------|--------|-----|
| 16 | filter | `bloom` | 3-pass: bright-pass + gather + composite |
| 17 | filter | `grade` | 6-pass color grading; comprehensive color ops |
| 18 | filter | `feedback` | 2-pass with persistent self-texture |
| 19 | filter | `motionBlur` | 2-pass with persistent frame buffer |
| 20 | synth | `reactionDiffusion` | 2-pass with `repeat:` looping |
| 21 | synth | `cellularAutomata` | 2-pass with state texture + 18-rule enum |
| 22 | filter | `normalize` | 4-pass GPGPU pyramid reduction |
| 23 | filter | `pixelSort` | 6-pass GPGPU sort pipeline |
| 24 | synth | `navierStokes` | 7-pass fluid solver; tests `repeat:` mechanism |
| 25 | filter | `palette` | Cosine palette; tests palette parameter type |
| 26 | mixer | `distortion` | Two-surface UV displacement |
| 27 | filter | `temporalAberration` | 9-pass shift register; tests persistent `_` textures |
| 28 | filter | `warp` | Noise-driven UV warp |
| 29 | synth | `julia` | Complex fractal; tests double-precision UV path |
| 30 | synth | `mandelbrot` | Complex fractal |

### Tier 3 — Agent/Particle Systems, 3D, and Complex Simulations

Port last. These require the full agent infrastructure (global state textures, drawMode:points, blend passes) or the 3D volume pipeline.

| Group | Effects |
|-------|---------|
| Agent infrastructure | `render/pointsEmit`, `render/pointsRender`, `render/pointsBillboardRender` |
| Simple agents | `points/flock`, `points/flow`, `points/physical` |
| Complex agents | `points/physarum`, `points/attractor`, `points/lenia`, `points/life`, `points/dla`, `points/buddhabrot`, `points/hydraulic` |
| 3D generators | `synth3d/noise3d`, `synth3d/shape3d`, `synth3d/cell3d`, `synth3d/fractal3d` |
| 3D renderer | `render/render3d`, `render/renderLit3d` |
| 3D simulations | `synth3d/cellularAutomata3d`, `synth3d/reactionDiffusion3d`, `synth3d/flythrough3d` |
| 3D filter | `filter3d/flow3d` |
| Mesh | `render/meshLoader`, `render/meshRender` |
| Loop | `render/loopBegin`, `render/loopEnd` |
| Legacy | `classicNoisedeck/*` (all 20) |
| Media/external | `synth/media`, `filter/text` |

---

## 6. Parity Hazards

### 6.1 `drawMode: "points"` Coordinate System

**CRITICAL.** In WebGL2/WGSL, particle position is stored in `global_xyz` as normalized `[0,1]` coordinates. The deposit vertex shader reads one texel per point, maps `[0,1]→[-1,1]` NDC, and uses `gl_PointSize`/`@builtin(point_coord)` for the footprint. In HLSL/Unity:
- There is no `gl_PointSize` equivalent in standard rasterization. Unity uses `StructuredBuffer<float4>` + geometry shader or procedural point sprites.
- `@builtin(point_coord)` (WGSL) = `SV_PointCoord` in HLSL (available in D3D11+).
- Y-axis convention: in OpenGL/WGSL `point_coord.y = 0` is top. In D3D `SV_PointCoord.y = 0` is also top. **No flip needed** for point sprites.
- But UV origin for texture sampling is top-left in D3D vs bottom-left in OpenGL. All non-point UV sampling needs Y-flip awareness.

### 6.2 Texture UV Origin

OpenGL/GLSL: UV `(0,0)` = bottom-left of texture.  
HLSL/D3D: UV `(0,0)` = top-left.  
WGSL (WebGPU): UV `(0,0)` = top-left (same as D3D).

Since the codebase already has both GLSL and WGSL implementations, the WGSL shaders should translate to HLSL more cleanly. The GLSL shaders will need Y-flip in `fragCoord.y` usages and any UV coordinates derived from screen position. Compare GLSL vs WGSL versions for each effect to determine which convention was used.

### 6.3 Float Precision

GLSL ES uses `mediump`/`highp` qualifiers. WebGL2 guarantees `highp` (32-bit float) in fragment shaders on most hardware. HLSL uses 32-bit float by default. The `rgba16f` / `rgba32f` texture formats are portable. The `rgba8unorm` format is 8-bit normalized — ensure your HLSL texture sample returns `[0,1]` without additional scaling.

### 6.4 Integer/Bitwise Operations

`synth/bitwise` uses integer bitwise ops (XOR, AND, OR). GLSL ES 3.0 and WGSL both support native `int` bitwise. HLSL supports `uint` bitwise natively. No parity issue if types are matched carefully. Signed vs unsigned integer underflow/overflow behavior differs.

### 6.5 `repeat: "paramName"` Pass Loops

Some effects use `repeat: "iterations"` on a pass. This means the pass is executed `iterations` times per frame, reading its own previous output as input (ping-pong). The runtime manages this loop. The shader itself is identical in each iteration — no iteration index is injected. Reimplement as a C# loop calling the same pass multiple times.

### 6.6 Compile-time Defines vs Runtime Uniforms

Effects with `define:` globals compile multiple shader variants. The Unity/HLSL port must either:
1. Compile static shader variants (via Unity's shader variant system / `#pragma multi_compile`), or
2. Use runtime `if`/`switch` branches in HLSL (acceptable since D3D's shader compiler handles these efficiently unlike ANGLE).

Option 2 is simpler and avoids combinatorial variant explosion. Functionally identical output in all cases.

### 6.7 WGSL Uniform Buffer Packing

WGSL packs uniforms as `array<vec4<f32>, N>` with explicit slot/component mappings. HLSL `cbuffer` uses HLSL packing rules (same as GLSL `std140` but with minor differences for array elements). When porting, use the `uniformLayout` / `uniformLayouts` maps as ground truth for how data is packed. Mismatch here will silently produce wrong values — the most common class of porting bugs.

### 6.8 Texture Format Mapping

| Noisemaker format | HLSL equivalent |
|-------------------|-----------------|
| `rgba8unorm` / `rgba8` | `DXGI_FORMAT_R8G8B8A8_UNORM` |
| `rgba16f` / `rgba16float` | `DXGI_FORMAT_R16G16B16A16_FLOAT` |
| `rgba32f` | `DXGI_FORMAT_R32G32B32A32_FLOAT` |

### 6.9 Blend Mode for Deposit Passes

Points deposit passes use `blend: true` with additive blending (source + dest). In WebGL2: `gl.blendFunc(gl.ONE, gl.ONE)`. In HLSL/Unity: `Blend One One` in shader BlendOps, or equivalent RenderTargetBlendDesc.

### 6.10 `global_` Texture Sharing Across Effects

Global textures (e.g., `global_xyz`, `global_ns_velocity`) are shared by name across effect nodes within the same pipeline frame. The HLSL port must implement a pipeline-level texture registry keyed by the `global_` name string. Multiple effects referencing the same global name must receive the same texture handle within a frame.

### 6.11 Atlas Height Formula for 3D Volumes

The 3D atlas height is `volumeSize²` (e.g., 64² = 4096). This is declared as:
```js
height: { param: 'volumeSize', power: 2, default: 4096 }
```
Unity Texture2D cannot exceed 16,384 pixels in any dimension. At `volumeSize = 128`, height = 16,384 — exactly at the D3D11 limit. `volumeSize = 256` would exceed it. Maximum safe value: `volumeSize = 128`.

---

## 7. Count Summary

| Namespace | Count | Primary Pattern |
|-----------|-------|-----------------|
| synth | 29 | Stateless single-pass generators |
| filter | 83 | Single-pass or simple multi-pass filters |
| filter3d | 1 | Agent-based 3D filter |
| mixer | 14 | Two-input single-pass mixers |
| points | 10 | Multi-pass agent middleware (drawMode:points) |
| render | 9 | Infrastructure nodes (agents, 3D, mesh) |
| synth3d | 7 | 3D volume generators |
| classicNoisedeck | 20 | Legacy single-pass effects (many define variants) |
| **Total** | **173** | |

> Note: two additional effects (synth/media, filter/text) use `externalTexture` (CPU upload each frame) — counted in synth/filter totals above.

---

## 8. Cross-Subsystem Dependencies

- **Effect format** (spec 07): Covers how `definition.js` is parsed, how `define:` triggers recompilation, and how `uniformLayout` drives WGSL buffer packing.
- **WebGL2 backend** (spec 05): Converts all passes to FBO draw calls; handles `drawMode:"points"`, `drawBuffers:N` MRT, `blend:true`, `repeat:N` loops.
- **WebGPU backend** (spec 06): Same semantics; `drawMode:"points"` maps to WGSL point list topology.
- **Resources/Pipeline** (spec 04): Manages `global_` texture registry; allocates `_`-prefixed persistent textures; resolves `inputTex` / `outputTex` connections.
- **Math primitives** (spec 08): Noise functions (simplex, value, fBm), Voronoi/cell distance, OKLAB/OKLCH, cosine palette encoding — all shared across many effects.

---

*End of effect catalog specification.*
