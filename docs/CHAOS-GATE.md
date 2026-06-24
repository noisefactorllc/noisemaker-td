# The Chaos Gate — why two effect classes aren't bit-parity on the TouchDesigner port

**TL;DR.** Almost everything in this port is bit-for-bit identical to the reference WebGL2 golden
(`max-diff 0`). Two narrow classes are not, and *cannot* be on TD/Metal:

1. **Chaotic agent flows** — the north-star `target.dsl` (1 M-agent `points/flow` → deposit → blur →
   `o0`, then `o0` drives a chaotic `navierStokes`). Full-chain SSIM **0.5–0.71** over the 30 s / 5 s
   sampling.
2. **Continuous solvers at the stability limit** — `reactionDiffusion` (Gray-Scott). Seed + first
   ~2 frames bit-exact, then SSIM falls to ~0.88 by frame 4.

Both render correctly, deterministically, and stay bounded. They are a *different instance* of the
same chaos — not a port bug. The shaders are faithful (the discrete/static parts of each are
bit-exact; see the evidence tables). The divergence is a sub-ULP cross-backend difference that a
chaotic feedback loop amplifies. This is the project's standing **"continuous/chaotic systems
diverge cross-backend"** principle, now pinned per class with measured numbers.

The sibling ports agree on the boundary: **three.js and babylon get `target.dsl` byte-identical**
because they *are* WebGL2/ANGLE (the golden's own rasterizer); **godot** can't (its cause is a
`GLSL→SPIR-V→Metal` `pow` rounding — documented in the Godot port's own CHAOS-GATE notes); **this port**
can't either, for the rasterizer reason below.

---

## Class 1 — chaotic agent flows: the point-rasterizer ULP residual

The deposit (`render/pointsRender`, `pointsBillboardRender`) scatters one `GL_POINTS` primitive per
live agent and additively blends it into the trail (`docs/TD-PLATFORM-NOTES.md` "GPU point scatter").
**Point rasterization is not specified to the bit across GL implementations** — the sample/coverage
rule, the pixel-center convention, and `gl_PointCoord`/`TDPointCoord()` interpolation each have a few
ULP of latitude. TD runs native **Metal**; the reference golden is **ANGLE (WebGL2) → Metal**. The
two rasterize a sized point sprite to *almost* the same coverage — a sub-pixel, sub-ULP difference in
where a sprite's edge falls.

For a *static* deposit that residual is invisible (and the spawn/raster/deposit path is bit-exact —
the agent positions are integer-hash seeded). But `target.dsl` is a textbook chaotic loop:

> agent position → sample the field (oklab) → turn → step → `fract()` wrap → deposit → **feed the
> trail into navierStokes** → the nav velocity steers the next frame's field → repeat

Every frame the rasterizer residual perturbs the trail in the ~8th decimal; `fract()` position wraps
and integer-texel `texelFetch` turn that into a *different agent* crossing a texel boundary; and the
chaotic navierStokes (no dissipation at the target's `velocityDecay`) amplifies it. Over ~300 frames
`1e-8` snowballs into a visibly different — but equally valid — fluid. Same algorithm, palette,
dynamics, and *kind* of structure; different exact pixels. This is the Lorenz "butterfly" problem,
not a defect.

### Why three.js / babylon match but TD can't
- **three.js, babylon** *are* WebGL2/ANGLE — same rasterizer as the golden → same point coverage bits
  → `target.dsl` byte-identical.
- **TD** uses native Metal point rasterization, which is free (and correct) to cover the sprite
  edge one ULP differently. Unreachable from the network, the GLSL, or the MAT.

### The target is stable, not broken
No NaN, no white-out, bounded over 1800 frames. Two fixes ported from `noisemaker-hlsl@abb9578` /
`noisemaker-godot@58a1b88` keep the deposit rate faithful (without them Metal blows out):
- **density-cull precision** — `fract(particleID·GR)` loses float32 precision at ~1 M agents (the raw
  product ≈ 6.5e5, where float32's step ≈ 0.06 quantizes `fract` into ~16 buckets → Metal
  over-deposits ~8× vs ANGLE). A hi/lo split keeps the products small so `fract` stays exact
  (`td/noisemaker/runtime/deposit_shaders.py`, `nm_particleRandom`).
- **nav input clamp to [0,1]** — bounds the HDR particle-field surface this pipeline hands
  navierStokes (`td/noisemaker/shaders/effects/synth/navierStokes/nsSplat.frag`, `ns.frag`; re-apply
  after re-transpile).

### Evidence — the divergence is below the chaos, not in the port
| Test | What it isolates | Result |
| --- | --- | --- |
| `parity/sweep.sh` (single-pass 2D catalog) | every single-pass synth/filter/mixer/cnd + channelCombine | **~139 at parity**, most strict `max-diff ≤ 1` |
| navierStokes standalone (`parity/evolve.sh`) | the fluid solver alone, static seed, 30 s / 5 s | frame 1 ssim **0.99998**, steady-state corr **0.9994** |
| `target_particles` x128 (`parity/evolve.sh`) | the particle subchain without the nav feedback | ssim **0.92** (no chaos) |
| `target.dsl` f30 (pre-chaos) | the full chain before nav amplification dominates | ssim **0.986** |
| `target.dsl` f300–f1800 | the full chaotic chain | ssim **0.5–0.71** (the gate) |

The deposit/raster/blend and the nav-in-isolation are faithful; only the *coupled chaotic loop*
diverges, and only after enough frames for `1e-8` to grow.

## Class 2 — continuous solvers: reactionDiffusion

`reactionDiffusion` is a Gray-Scott reaction-diffusion sim run at its stability limit. Driven 8
frames-from-zero (`parity/accumulate.sh`):

| frame | max-diff | ssim | |
| --- | --- | --- | --- |
| f1 | 1 | 0.99977 | **bit-exact** — seed + first diffusion+reaction step |
| f2 | 1 | 0.99981 | **bit-exact** |
| f4 | 94 | 0.88 | chaotic amplification has taken over |
| f8 | 41 | 0.988 | divergent |

The f1/f2 bit-exactness proves the diffusion kernel and reaction term are a faithful port. The f4+
divergence is the per-frame iteration amplifying sub-ULP cross-backend fp differences — the same
mechanism as Class 1, without the rasterizer.

**There is no stable golden to hit.** The decisive evidence: *two reference WebGL2 harnesses* —
`parity/batch-golden.mjs` and the older `parity/export-and-render.mjs` — render the same DSL at the
same 8-frame/timestep-0 protocol and **diverge to ssim ≈ 0.47** at f8 (while `cellularAutomata` and
`motionBlur` come out byte-identical between them). reactionDiffusion is sensitive enough that even
two paths *into the same engine* disagree. A Metal port cannot match a target that the reference
can't reproduce against itself. Gated on the early frames; f4+ reported, not failed.

(Contrast `cellularAutomata`: a *discrete* CA self-corrects every generation and is **byte-identical
at every frame** cross-backend — discreteness is the cure for the chaos. It is not gated.)

## Scope — exactly these two classes; everything else is strict
Bit-exact / strict-gated (verified): the whole single-pass 2D catalog (~139 effects via `sweep.sh`),
`cellularAutomata` (all frames), `motionBlur` f1/f2 (then a mild SSIM-gated 8-bit-feedback re-quant
drift at f8, not chaos), the navierStokes solver in isolation, the full deposit/diffuse/blend path,
agent spawn/MRT state. The two 3D-volume statefuls `cellularAutomata3d`/`reactionDiffusion3d` join the
accumulate set (f1/f2 max-diff ≤ 1, `reactionDiffusion3d` bit-exact; then `cellularAutomata3d` f8 SSIM
~0.996 ssim-gated and `reactionDiffusion3d` f8 SSIM ~0.977 chaos-reported, Class 2).

Chaos-gated by design: `target.dsl` full chain (Class 1) and `reactionDiffusion` f4+ (Class 2).

## Reproduce
```bash
# Class 1 — pre-chaos faithful, full-chain gated:
NM_FRAMES=1800 NM_SAMPLES=30,300,600,1200,1800 parity/evolve.sh target
#   → f30 ssim 0.986   …   f300+ ssim ~0.5–0.71

# Class 2 — reactionDiffusion early-exact then divergent (accumulate.sh drives 5 feedback effects):
parity/accumulate.sh
#   → cellularAutomata 0/0/0 (byte-exact); motionBlur f8 ssim 0.99992; reactionDiffusion f1/f2
#     bit-exact then f8 chaos-gated; + cellularAutomata3d (f8 ssim ~0.996) and reactionDiffusion3d
#     (f8 ssim ~0.977), the two 3D-volume statefuls

# The golden is not reproducible against itself for reactionDiffusion (the proof it's intrinsic):
node parity/batch-golden.mjs <(echo "rd parity/programs/reactionDiffusion.dsl") /tmp/a --frames 8 --timestep 0
node parity/batch-golden.mjs <(echo "rd parity/programs/reactionDiffusion.dsl") /tmp/b --frames 8 --timestep 0
#   /tmp/a vs /tmp/b → byte-identical (deterministic within one harness)
#   either vs the export-and-render golden → ssim ~0.47 (divergent across harnesses)
```
