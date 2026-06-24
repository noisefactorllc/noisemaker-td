# noisemaker-td

Run **Noisemaker**'s Polymorphic shading DSL **live inside Derivative TouchDesigner** — feed a
DSL string (or a compiled render-graph), get a cooking network of **GLSL TOP** operators. The whole
2D effect catalog, the full 3D volume/cubemap namespace, particle-agent simulations, and the live
"blaster" composition corpus all render at tolerance-parity with Noisemaker's JS/WebGL2 reference.

It is a structural port that mirrors the sibling Unity/HLSL (`noisemaker-hlsl`) and Godot
(`noisemaker-godot`) ports: the same DSL, the same effects, the same render-graph seam. Because
TouchDesigner's GLSL TOP is OpenGL GLSL with the **same bottom-left raster origin** as the
reference's WebGL2 backend, the per-effect shaders are translated **directly from the reference
GLSL** (not WGSL) by a mechanical transpiler — no Y-flip, no math edits.

**Last verified:** 2026-06-24.

## Quickstart

1. **Install TouchDesigner 2025.32820** (`brew install --cask touchdesigner`; arm64-native, macOS).
   The free **Non-Commercial** tier needs a **one-time Derivative account + license activation through
   the GUI** (a fresh install blocks at the activation modal). TouchDesigner is **not headless** — it
   needs a logged-in, GPU-capable desktop session.
2. **Nothing else to install to render.** The 184 effect-definition JSONs and 249 transpiled `.frag`
   shaders are **committed**; the runtime and the live compiler import only the Python standard library
   and TD's built-in `td` module. (`Node` and `NM_REFERENCE_ROOT` are needed *only* to regenerate
   assets or mint parity goldens — see [Regenerating assets](#regenerating-assets-maintainers).
   `numpy`/`pillow` are needed *only* by `parity/compare.py`.)
3. **See it render — no reference engine required.** This builds the flagship `present_hero.dsl`
   (particles → navierStokes → solaris palette → lighting → lens) as a live TD network, evolves 30 s,
   and writes a render to `parity/out/present_hero.f1800.candidate.png`:
   ```bash
   parity/present_screenshot.sh            # ~minutes at 1024²; NM_FRAMES=300 for a faster look
   ```
   It compiles the DSL with the in-engine compiler (no `NM_REFERENCE_ROOT`) and leaves TouchDesigner
   open showing the result. (The final on-screen screenshot step needs macOS Screen-Recording
   permission for your terminal; the PNG render is produced regardless.)

(Optional, only for the Python parity tooling: `python3 -m venv parity/.venv &&
parity/.venv/bin/pip install -r requirements.txt`.)

## Use in your own TouchDesigner project

The runtime is one Python class, `NMRenderer`, that builds a TD network under a Base COMP from a DSL
string or a compiled render-graph. From an **Execute DAT** (`onStart`/`onCreate`) inside your COMP:

```python
import sys
sys.path.insert(0, '/path/to/noisemaker-td/td')        # add the package dir (package name: noisemaker)
from noisemaker.runtime.nm_renderer import NMRenderer

def onStart():
    comp = parent()                                     # a Base COMP to build the network under
    nm = NMRenderer(comp, width=1280, height=1280)
    comp.store('nm', nm)                                # keep a reference alive
    nm.set_dsl('search synth\nsolid(color: [0.9, 0.3, 0.5]).write(o0)\nrender(o0)')
    out = op('../out')                                  # your Null/Out TOP for display or export
    if out is not None and nm.Output is not None:
        out.inputConnectors[0].connect(nm.Output)
```

**Every DSL program must begin with a `search <namespace>` directive** (e.g.
`search synth, filter, render`) declaring which effect namespaces to resolve — `set_dsl` raises
`DslSyntaxError: Missing required search directive` without it.

### NMRenderer API

| Member | Purpose |
|---|---|
| `NMRenderer(owner_comp, *, shaders_root=None, width=256, height=256, time=0.25)` | Build under `owner_comp` (a Base COMP). `shaders_root` relocates the effect JSON / `.frag` assets (defaults to the in-repo `td/noisemaker/shaders`). |
| `set_dsl(src)` | Compile a DSL string live in-engine (lex → parse → validate → expand → resources) and build the network. |
| `set_graph(path)` | Build from a render-graph JSON file (e.g. a `tools/export-graph.mjs` golden). |
| `set_graph_dict(d)` / `set_graph_str(text)` | Build from an in-memory dict / JSON string. |
| `Output` | Property — the terminal TOP of the built network; wire it into your own network. |
| `resize(width, height)` | Re-cook at a new resolution. |
| `render_to(path, time=0.25)` | Cook one frame and save a PNG. |

### Integrator notes

- **What ships vs. what regeneration needs:** the effect JSON + `.frag` are committed and
  `EffectRegistry.load_from_directory()` defaults to the in-repo path, so **rendering needs no Node,
  no `NM_REFERENCE_ROOT`, and no pip installs into TD's bundled Python**.
- **3D-volume effects** are clamped by `NM_MAX_VOLUME_SIZE` (default **32**) so the volume atlas stays
  under the free tier's 1280×1280 cook limit; the clamp is applied at `build()` time (changing it needs
  a rebuild, not a live re-cook). Raise it on a Commercial/Educational license (no 1280 cap).
- **Reusable host `.toe`:** `.toe`/`.tox` are binary and can't be authored offline. Run
  `td/make_bootstrap.py` once inside an activated TD (Textport: `exec(open('.../td/make_bootstrap.py').read())`)
  to materialize and save `td/noisemaker.toe`.

## How it works

The contract between "what to render" and "how this engine renders it" is the normalized **Render
Graph JSON** (`{passes, programs, textures, renderSurface}`, see `docs/GRAPH-JSON-SCHEMA.md`). Two
producers emit it; one consumer builds the network:

```
 DSL ──► compile ──► Render Graph JSON ──► TD network builder ──► GLSL TOP network
         │                  (the seam)        (Python, this repo)      (TD cooks it)
         ├─ reference JS  (tools/export-graph.mjs)            — golden graphs
         └─ live Python frontend (td/noisemaker/compiler/)    — in-engine, byte-identical
```

The live Python frontend is a complete `lex → parse → validate → expand → resources` port of the
reference Polymorphic DSL compiler; it emits the same normalized JSON, **graph-parity-clean 185/186**
against the `export-graph.mjs` oracle. See `ARCHITECTURE.md` for the design and `PORTING-GUIDE.md`
for the transpile rules.

## What works today (verified 2026-06-24)

- **Live in-engine DSL compiler** — `set_dsl` compiles in TD's Python; all four compiler-parity gates
  are byte-exact vs the reference oracle over a 186-program corpus: lexer/parser/validator **186/186**,
  graph **185/186** (1 skip = `B5oBsA`, a nonexistent effect the reference also rejects).
  `parity/compiler/check_{lex,parse,validate,graph}.py`.
- **2D catalog** — ~**139 single-pass** effects at parity (byte-exact, or SSIM-gated for
  cross-rasterizer discontinuities), plus multi-pass and stateful effects.
- **Stateful / feedback** — `cellularAutomata`, `reactionDiffusion`, `motionBlur`,
  `convolutionFeedback`, and the two 3D variants are driven 8-frames-from-zero through the evolve
  harness (`parity/accumulate.sh`): discrete CAs byte-exact every frame; continuous solvers bit-exact
  early then chaos-gated (`docs/CHAOS-GATE.md`).
- **Full 3D namespace** — volume raymarch (`render3d` / `renderLit3d`) at SSIM ~1.0 / max-diff 1;
  6-face cubemap bake (`parity/cubemap.sh`) max-diff ≤ 1; `flow3d` 3D-agent flow chaos-gated.
- **Agents / particles** — GPU point-scatter deposit (Geo COMP + GLSL MAT + Render TOP), MRT agent
  state.
- **std140 UBO** — `remap` byte-identical via the GLSL TOP Arrays page.
- **Live blaster corpus** — 24/24 renderable composition programs render end-to-end through the live
  compiler (`parity/corpus_sweep.sh`).

## Parity harnesses

These reproduce the parity numbers and **need `NM_REFERENCE_ROOT`** (to render reference goldens):

```bash
NM_REFERENCE_ROOT=/path/to/noisemaker parity/corpus_sweep.sh [--stateless]  # live DSL → graph → render the blaster corpus in TD (the end-to-end target)
NM_REFERENCE_ROOT=/path/to/noisemaker parity/sweep.sh                        # per-effect single-frame catalog: stage goldens + render + grade (per-effect tolerance)
NM_REFERENCE_ROOT=/path/to/noisemaker parity/accumulate.sh                   # multi-frame feedback effects (8-frames-from-zero) — see docs/CHAOS-GATE.md
NM_REFERENCE_ROOT=/path/to/noisemaker parity/cubemap.sh                      # 6-face cubemap bake parity
parity/sweep.sh --compare-only                                              # re-grade existing candidates (no TD render)
```

`sweep.sh` classifies the in-repo DSLs (`stage_coverage.py`), renders reference goldens from the
upstream engine, renders candidates in TD, and grades with a **per-effect tolerance**. A few
discontinuity-heavy effects (fractal root basins, `step()` thresholds, NEAREST coord tie-breaks) cannot
be bit-exact cross-device (Metal vs ANGLE/WebGL2) and are gated on structural **SSIM ≥ 0.98** — the
same physics the godot port relaxes. TouchDesigner has no headless startup hook, so rendering runs via
an Execute DAT inside a `.toe` authored offline with `toeexpand`/`toecollapse`; see
`docs/TD-PLATFORM-NOTES.md`.

> Goldens, candidate PNGs, and `.toe` files are gitignored (generated). A bare clone renders via the
> live compiler immediately (Quickstart step 3); reproducing the *parity numbers* requires regenerating
> goldens, which needs the upstream engine via `NM_REFERENCE_ROOT`.

## Layout

```
reference/             engine-agnostic specs 01–10 (copied verbatim; the shared brain)
tools/
  export-graph.mjs        reference compileGraph → golden graph JSON (reused, unchanged)
  convert-definitions.mjs effect definitions → effects/<ns>/<func>.json (reused, retargeted)
  convert-shaders.mjs     reference GLSL → TD .frag transpiler  (NEW — the port's centerpiece)
parity/
  programs/*.dsl          161-program per-effect parity catalog
  corpus/*.dsl            25 live blaster composition programs
  compare.py              golden vs candidate (max-abs-diff + SSIM)
  corpus_sweep.sh         live DSL → graph → render in TD (the end-to-end target)
  sweep.sh run.sh accumulate.sh evolve.sh cubemap.sh   parity drivers
  present_screenshot.sh   open the flagship in TD, evolve 30 s, screenshot
  .venv/ out/             gitignored (deps for compare.py; generated artifacts)
td/
  noisemaker/
    runtime/              the network builder (Python): nm_renderer (NMRenderer), render_graph,
                          graph_loader, td_backend, surface_manager, uniform_binder, pipeline, …
    compiler/             live DSL frontend (lex→parse→validate→expand→resources; graph-parity 185/186)
    shaders/effects/<ns>/<effect>/<prog>.frag   per-program TD GLSL (generated, committed)
    effects/<ns>/<func>.json                    effect definitions (generated, committed)
  make_bootstrap.py       materialize the host noisemaker.toe (run once inside TD)
  build_*_toe.py          author the offline bootstrap .toe harnesses
docs/   ARCHITECTURE.md   PORTING-GUIDE.md
```

## Coverage

| Namespace | Effects | TD programs |
|---|---|---|
| synth | 29 | auto |
| synth3d | 7 | MRT (manual) |
| filter | 90 | auto |
| filter3d | 2 | MRT (manual) |
| mixer | 15 | auto (remap = std140 UBO) |
| points | 10 | MRT/points (manual) |
| render | 11 | MRT/points (manual) |
| classicNoisedeck | 20 | auto |
| **total** | **184** | **249 programs — 227 auto-transpiled, 22 flagged (21 MRT + 1 std140-UBO)** |

## Regenerating assets (maintainers)

The committed effect JSON and `.frag` shaders are generated from the upstream Noisemaker engine. You
only need this to update them or mint parity goldens — **not to render**. All codegen reads the engine
via `NM_REFERENCE_ROOT` (required; no default — point it at the upstream Noisemaker engine tree
containing `shaders/`; that engine is not included in this repo). `Node` 26.

```bash
NM_REFERENCE_ROOT=/path/to/noisemaker node tools/convert-definitions.mjs   # 184 effect JSONs
NM_REFERENCE_ROOT=/path/to/noisemaker node tools/convert-shaders.mjs       # 249 .frag (227 auto, 22 flagged)
NM_REFERENCE_ROOT=/path/to/noisemaker node tools/export-graph.mjs --file parity/programs/solid.dsl parity/out/solid.graph.json
```

## License

MIT (port scaffolding). The Noisemaker engine and effects are the reference project's; TouchDesigner is
Derivative's. See the upstream Noisemaker project for upstream terms.
