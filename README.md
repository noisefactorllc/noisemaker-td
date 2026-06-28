# noisemaker-td

> Run **Noisemaker**'s procedural visuals inside **Derivative TouchDesigner**.

## What is this?

**Noisemaker** is a procedural visual engine. You write tiny text programs — chains of
effects — and it renders live, animated GPU textures:

```
search synth, filter
noise(scaleX: 60).bloom().write(o0)
render(o0)
```

That little language is Noisemaker's **DSL** (a domain-specific language for visuals). The original
engine runs in the browser at [noisedeck.app](https://noisedeck.app).

**noisemaker-td** runs that same engine *inside TouchDesigner* — the same programs and the same ~180
effects, built as a live network of TouchDesigner's own GLSL operators. Use it to generate textures,
backgrounds, and animated source material from code, with no image files.

It is **self-contained**: the runtime compiles the DSL and renders it entirely inside TouchDesigner —
no internet, no Node.js, no separate engine to install.

## What you can do with it

- **Generate animated textures** from a short program — noise, gradients, patterns, color grades,
  blurs, warps.
- **Run simulations on the GPU** — particle/agent systems (flocking, slime/physarum,
  reaction-diffusion) and fluid (navier–stokes).
- **Render 3D, too** — volume raymarching, lit volumes, and six-face cubemaps, not just flat images.
- **Wire the result into any network** — the output is an ordinary **TOP**, so it feeds anything a TOP
  feeds: composites, materials, projection, output.

## Requirements

- **TouchDesigner 2025.32820** (arm64-native, macOS). The free **Non-Commercial** tier is enough.
- A **logged-in, GPU-capable desktop session**. TouchDesigner is **not headless** — there is no
  dedicated-server / CI rendering.
- Verified on **Apple Silicon / Metal**.

## Install

1. `brew install --cask touchdesigner`
2. One time only: create a **Derivative account** and **activate the license through the GUI**. A
   fresh install stops at the activation modal until you do.

That is everything needed to render. The effect data (184 JSON definitions) and shaders (249
translated `.frag` files) are **committed**, and the runtime imports only Python's standard library
and TouchDesigner's built-in `td` module. (Node and a reference-engine checkout are needed *only* to
regenerate assets or run the parity tests — see [STATUS.md](STATUS.md).)

## Your first render

The quickest way to see it work — build the flagship demo (`present_hero.dsl`: particles →
navier–stokes → palette → lighting → lens) as a live TouchDesigner network, evolve it 30 s, and write
a PNG to `parity/out/`:

```bash
parity/present_screenshot.sh            # a few minutes at 1024²; set NM_FRAMES=300 NM_SAMPLES=300 for a faster look
```

It compiles the DSL with the in-engine compiler (no reference engine required), writes the rendered
frame to `parity/out/<prog>.f1800.candidate.png`, and saves a screenshot to
`parity/out/<prog>.tdshot.png`. On a successful capture it closes TouchDesigner; if the screen grab is
blank (see the Screen-Recording note below) it leaves TouchDesigner open so you can view the result.

> **Note:** The on-screen screenshot needs macOS Screen Recording permission for your terminal
> (System Settings → Privacy & Security → Screen Recording) and numpy + Pillow
> (`python3 -m venv parity/.venv && parity/.venv/bin/pip install -r requirements.txt`); without them the
> render still saves to `parity/out/<prog>.f1800.candidate.png` but the screenshot step reports BLACK.

**Every DSL program has the same shape:** name the namespaces it uses (`search synth, filter`), chain
effects, write the result to an output surface (`.write(o0)`), then pick one to show (`render(o0)`).

## Use it in your own TouchDesigner project

The runtime is one Python class, `NMRenderer`, that builds the network under a **Base COMP** from a
DSL string. Drop this in an **Execute DAT** (`onStart`) inside your COMP:

```python
import sys
sys.path.insert(0, '/path/to/noisemaker-td/td')        # the package dir (package name: noisemaker)
from noisemaker.runtime.nm_renderer import NMRenderer

def onStart():
    comp = parent()                                     # a Base COMP to build the network under
    nm = NMRenderer(comp, width=1280, height=1280)
    comp.store('nm', nm)                                # keep a reference alive
    nm.set_dsl('search synth\nsolid(color: [0.9, 0.3, 0.5]).write(o0)\nrender(o0)')
    out = op('../out')                                  # your Null/Out TOP for display or export
    if out is not None and nm.Output is not None:
        out.inputConnectors[0].connect(nm.Output)       # nm.Output is an ordinary TOP
```

| Member | Purpose |
|---|---|
| `NMRenderer(owner_comp, *, shaders_root=None, width=256, height=256, time=0.25)` | Build under `owner_comp` (a Base COMP). `shaders_root` relocates the committed effect assets. |
| `set_dsl(src)` | Compile a DSL string live in-engine and build the network. |
| `set_graph(path)` / `set_graph_dict(d)` / `set_graph_str(text)` | Build from a pre-compiled render graph (file / dict / JSON string). |
| `Output` | The terminal TOP of the built network — wire it into your own network. |
| `resize(width, height)` | Re-cook at a new resolution. |
| `render_to(path, time=0.25)` | Cook one frame and save a PNG. |

A couple of things worth knowing:

- **3D-volume effects** are clamped by `NM_MAX_VOLUME_SIZE` (default **32**) so the volume stays under
  the free tier's 1280×1280 cook limit. It is applied when the network is built. Raise it on a
  Commercial/Educational license.
- **`.toe` / `.tox` files are binary** and can't be authored offline. Run `td/make_bootstrap.py` once
  inside an activated TouchDesigner to materialize and save a reusable host `td/noisemaker.toe`.

## What works today

- The **whole 2D effect catalog** renders — noise, filters, mixers, classic generators. Most effects
  match the web reference **exactly** (within 8-bit rounding).
- The **full 3D namespace** renders too — volume raymarching, lit volumes, and six-face cubemaps.
- **Particle/agent sims and fluid (navier–stokes)** render and behave like the reference.
- **Chaotic** particle-and-fluid programs render correctly, but as a *different instance* of the same
  chaos — they match in look and behavior, not pixel-for-pixel (tiny GPU rounding differences get
  amplified by feedback).
- The **live "blaster" corpus** — real multi-effect compositions from noisedeck.app — renders
  end-to-end through the in-engine compiler.

Coverage table, parity numbers, and the full "chaos" explanation: **[STATUS.md](STATUS.md)** and
**[docs/CHAOS-GATE.md](docs/CHAOS-GATE.md)**. Why TouchDesigner, and the platform gotchas:
**[docs/TD-PLATFORM-NOTES.md](docs/TD-PLATFORM-NOTES.md)**.

## How it works

Noisemaker turns a DSL program into a **render graph** — a normalized list of GPU passes. That graph
is the shared seam every Noisemaker port targets. noisemaker-td ports the whole compiler to **Python**
(so it runs inside TouchDesigner) and builds the graph as a live network of **GLSL TOP** operators
that TD cooks.

Because TouchDesigner's GLSL TOP is OpenGL GLSL with the **same raster origin** as the reference's
WebGL2 backend, the per-effect shaders are translated **directly from the reference GLSL** — no
Y-flip, no math edits.

→ **[ARCHITECTURE.md](ARCHITECTURE.md)** (how it maps onto TouchDesigner) ·
**[PORTING-GUIDE.md](PORTING-GUIDE.md)** (translating a shader) ·
**[docs/GRAPH-JSON-SCHEMA.md](docs/GRAPH-JSON-SCHEMA.md)** (the render-graph format).

## Contributing

Rendering needs nothing external. The **parity tooling**, however, compares TouchDesigner's output
against the reference engine, so it needs a checkout of it via `NM_REFERENCE_ROOT`:

```bash
NM_REFERENCE_ROOT=/path/to/noisemaker parity/corpus_sweep.sh   # live DSL → graph → render the corpus in TD
NM_REFERENCE_ROOT=/path/to/noisemaker parity/sweep.sh          # per-effect single-frame catalog
```

→ **[STATUS.md](STATUS.md)** (coverage + gate results) ·
**[docs/TD-PLATFORM-NOTES.md](docs/TD-PLATFORM-NOTES.md)** (how parity runs without a headless TD) ·
`reference/01–10` (engine specs shared across all Noisemaker ports).

## Repo layout

```
td/noisemaker/   the package — runtime (network builder) + live compiler + committed effects & shaders
parity/          golden-image test harness + DSL programs (per-effect catalog + blaster corpus)
tools/           Node dev tooling (reference graph export, definition + shader codegen)
reference/       engine specs shared across all Noisemaker ports
docs/  ARCHITECTURE.md  PORTING-GUIDE.md   design, porting rules, platform notes
STATUS.md        coverage table, parity results, known limits
```

## License

MIT (see [LICENSE](LICENSE)). Use of the Noisemaker and Noise Factor names in derivative products is
subject to the [Trademark Policy](TRADEMARK.md).

Copyright © 2026 Noise Factor LLC
