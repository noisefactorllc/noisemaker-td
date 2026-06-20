#!/usr/bin/env node
// export-and-render.mjs — GOLDEN renderer for a parity program.
//
// For one DSL program this:
//   1. exports the normalized render-graph JSON (via tools/export-graph.mjs), and
//   2. renders the reference GPU output to a GOLDEN PNG at a FIXED
//      width/height/seed/frame using the vendored shade-mcp Playwright harness
//      (BrowserSession driving the demo viewer at /demo/shaders/).
//
// The seed is encoded in the DSL itself (e.g. `noise(seed=1)`), so determinism is
// owned by the program. Time is a NORMALIZED 0..1 value (reference/04 §6); we PAUSE
// the demo and pin paused-time, so the capture is a single deterministic frame
// independent of wall clock / FPS.
//
// Default capture: 256x256, time 0.25 (matches scripts/image_regression.py).
//
// We do NOT use the high-level runDslProgram()/renderEffectFrame() helpers because
// neither combines (DSL load) + (paused fixed time) + (deterministic float
// readback of o0). Instead we open a BrowserSession and drive session.page
// directly, reusing the exact viewer hooks those helpers use (DEFAULT viewer
// globals are prefixed __noisemaker — see .mcp.json SHADE_GLOBALS_PREFIX).
//
// Usage:
//   node export-and-render.mjs <program.dsl> <outDir> [--time 0.25] [--size 256] \
//        [--backend webgl2|webgpu]
//
// Writes  <outDir>/<programName>.golden.png  and  <outDir>/<programName>.graph.json
//
// Prereqs: Node, Playwright + a system Chrome (the harness launches chromium),
// and the reference repo present as the sibling tree (../../shaders, ../../demo).
// See parity/README.md.

import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { dirname, resolve, basename, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
// Reference (golden) engine lives in the sibling `noisemaker` repo. Override
// with NM_REFERENCE_ROOT if it's elsewhere. (This repo was split out of
// noisemaker/noisemaker-hlsl/, where the default was just `../..`.)
const REFERENCE_ROOT = process.env.NM_REFERENCE_ROOT
  ? resolve(process.env.NM_REFERENCE_ROOT)
  : resolve(__dirname, '..', '..', 'noisemaker')

const HARNESS = join(REFERENCE_ROOT, 'vendor', 'shade-mcp', 'harness', 'index.js')
const EXPORT_GRAPH = join(__dirname, '..', 'tools', 'export-graph.mjs')

// Self-contained PNG encoder (Node built-in zlib only) so the harness has NO
// external npm dependency (pngjs is a reference devDep that may not be installed).
// Encodes a top-down RGBA8 buffer (row 0 = top) as a non-interlaced PNG.
import { deflateSync } from 'node:zlib'

function crc32 (buf) {
  let c = 0xffffffff
  for (let i = 0; i < buf.length; i++) {
    c ^= buf[i]
    for (let k = 0; k < 8; k++) c = (c & 1) ? (0xedb88320 ^ (c >>> 1)) : (c >>> 1)
  }
  return (c ^ 0xffffffff) >>> 0
}

function pngChunk (type, data) {
  const len = Buffer.alloc(4); len.writeUInt32BE(data.length, 0)
  const typeBuf = Buffer.from(type, 'ascii')
  const body = Buffer.concat([typeBuf, data])
  const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(body), 0)
  return Buffer.concat([len, body, crc])
}

// rgbaTopDown: Uint8 length width*height*4, row 0 = top.
function encodePng (width, height, rgbaTopDown) {
  const sig = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(width, 0)
  ihdr.writeUInt32BE(height, 4)
  ihdr[8] = 8   // bit depth
  ihdr[9] = 6   // color type RGBA
  ihdr[10] = 0  // compression
  ihdr[11] = 0  // filter
  ihdr[12] = 0  // interlace
  // Filtered scanlines: each row prefixed with filter byte 0 (None).
  const raw = Buffer.alloc(height * (1 + width * 4))
  for (let y = 0; y < height; y++) {
    const di = y * (1 + width * 4)
    raw[di] = 0
    rgbaTopDown.copy(raw, di + 1, y * width * 4, (y + 1) * width * 4)
  }
  const idat = deflateSync(raw)
  return Buffer.concat([
    sig,
    pngChunk('IHDR', ihdr),
    pngChunk('IDAT', idat),
    pngChunk('IEND', Buffer.alloc(0))
  ])
}

// Viewer wiring — matches .mcp.json (SHADE_VIEWER_ROOT='.', VIEWER_PATH,
// GLOBALS_PREFIX). The harness reads SHADE_* env, so we set them here.
const VIEWER_ROOT = REFERENCE_ROOT
const VIEWER_PATH = '/demo/shaders/'
const EFFECTS_DIR = join(REFERENCE_ROOT, 'shaders', 'effects')
const GLOBALS_PREFIX = '__noisemaker'
const STATUS_TIMEOUT = 300000

function parseArgs (argv) {
  const opts = { time: 0.25, size: 256, backend: 'webgl2' }
  const pos = []
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (a === '--time') opts.time = parseFloat(argv[++i])
    else if (a === '--size') opts.size = parseInt(argv[++i], 10)
    else if (a === '--backend') opts.backend = argv[++i]
    else pos.push(a)
  }
  opts.programPath = pos[0]
  opts.outDir = pos[1]
  return opts
}

// Decode the demo's data:image/png;base64 URI to a Buffer.
function dataUriToBuffer (uri) {
  const comma = uri.indexOf(',')
  return Buffer.from(uri.slice(comma + 1), 'base64')
}

async function main () {
  const opts = parseArgs(process.argv.slice(2))
  if (!opts.programPath || !opts.outDir) {
    process.stderr.write('usage: node export-and-render.mjs <program.dsl> <outDir> ' +
      '[--time 0.25] [--size 256] [--backend webgl2|webgpu]\n')
    process.exit(2)
  }

  const dsl = readFileSync(opts.programPath, 'utf8')
  const programName = basename(opts.programPath).replace(/\.dsl$/, '')
  mkdirSync(opts.outDir, { recursive: true })

  // ---- 1. Export the normalized graph JSON (no browser needed) -------------
  const { exportGraph } = await import(pathToFileURL(EXPORT_GRAPH).href)
  const graph = await exportGraph(dsl)
  const graphPath = join(opts.outDir, `${programName}.graph.json`)
  writeFileSync(graphPath, JSON.stringify(graph, null, 2) + '\n')
  process.stderr.write(`[parity] wrote ${graphPath}\n`)

  // ---- 2. Render the golden frame via the Playwright harness ---------------
  // Configure the harness via SHADE_* env (read by BrowserSession.getConfig()).
  process.env.SHADE_VIEWER_ROOT = VIEWER_ROOT
  process.env.SHADE_VIEWER_PATH = VIEWER_PATH
  process.env.SHADE_EFFECTS_DIR = EFFECTS_DIR
  process.env.SHADE_GLOBALS_PREFIX = GLOBALS_PREFIX
  process.env.SHADE_HEADLESS = process.env.SHADE_HEADLESS ?? '1'

  const harness = await import(pathToFileURL(HARNESS).href)
  const { BrowserSession } = harness

  const session = new BrowserSession({ backend: opts.backend })
  let pngBuffer
  try {
    await session.setup()
    const page = session.page
    await session.setBackend(opts.backend)
    const globals = session.globals

    await page.setViewportSize({ width: opts.size, height: opts.size })

    // Wait for the demo's initial pipeline + DSL editor to be ready (the demo
    // boots with a default effect; __noisemakerRenderingPipeline is set then).
    await page.waitForFunction(() => !!window.__noisemakerRenderingPipeline &&
      !!document.getElementById('dsl-editor') && !!document.getElementById('dsl-run-btn'),
    { timeout: STATUS_TIMEOUT })

    // Load OUR DSL via the editor + run button, then wait for the pipeline's
    // graph to actually SWAP to our program (graph.id change). Polling only the
    // status text races the default-effect "compiled" message and reads the
    // wrong surface (the bug that produced identical default goldens).
    const baselineId = await page.evaluate(() =>
      window.__noisemakerRenderingPipeline?.graph?.id ?? null)
    await page.evaluate((src) => {
      const editor = document.getElementById('dsl-editor')
      const runBtn = document.getElementById('dsl-run-btn')
      editor.value = src
      editor.dispatchEvent(new Event('input', { bubbles: true }))
      runBtn.click()
    }, dsl)
    await page.waitForFunction((base) => {
      const s = (document.getElementById('status')?.textContent || '').toLowerCase()
      if (s.includes('error') || s.includes('failed')) {
        throw new Error('DSL compile failed: ' + document.getElementById('status')?.textContent)
      }
      const p = window.__noisemakerRenderingPipeline
      return !!(p && p.graph && p.graph.id !== base)
    }, { timeout: STATUS_TIMEOUT }, baselineId)

    // PAUSE FIRST so the demo's requestAnimationFrame loop stops re-syncing the
    // canvas to its (small, letterboxed) layout size — that auto-resize is what
    // intermittently reverted our resize to ~90px.
    await page.evaluate(() => {
      if (window.__noisemakerSetPaused) window.__noisemakerSetPaused(true)
    })
    // Resize the render surface to the requested square (canvas backing + CSS + the
    // pipeline's own surfaces) so the readback is deterministic and matches Unity.
    //
    // The demo recomputes a (small, letterboxed) square canvas size from the
    // container layout inside a `resize`-event handler (demo/shaders/index.html
    // computeCanvasSize/handleResize). page.setViewportSize() dispatches that
    // `resize` event asynchronously, so its handler can fire AFTER this block and
    // revert canvas.width/height (and thus the pipeline surfaces) back to ~90px —
    // a race that intermittently produced 90x90 goldens. To make the resize
    // deterministic we PIN the canvas width/height setters to our target before
    // resizing, so any late layout-driven write is a no-op, then we poll until the
    // o0 surface texture is stably at the requested size before rendering.
    await page.evaluate((size) => {
      const r = window.__noisemakerCanvasRenderer
      const p = window.__noisemakerRenderingPipeline
      const canvas = r && r.canvas
      if (canvas) {
        // Pin width/height: the real backing store is set to `size`; any later
        // assignment (e.g. the demo's handleResize) is swallowed. configurable so
        // this is reversible and overrides the renderer's own interceptor.
        const pin = (prop) => {
          Object.defineProperty(canvas, prop, {
            configurable: true,
            enumerable: true,
            get () { return size },
            set () { /* locked to `size` for deterministic capture */ }
          })
        }
        // Set the true backing store first via the prototype setter, then lock.
        const proto = Object.getPrototypeOf(canvas)
        const wd = Object.getOwnPropertyDescriptor(HTMLCanvasElement.prototype, 'width')
        const hd = Object.getOwnPropertyDescriptor(HTMLCanvasElement.prototype, 'height')
        if (wd && wd.set) wd.set.call(canvas, size)
        if (hd && hd.set) hd.set.call(canvas, size)
        void proto
        pin('width'); pin('height')
        if (canvas.style) { canvas.style.width = size + 'px'; canvas.style.height = size + 'px' }
      }
      if (p && typeof p.resize === 'function') p.resize(size, size)
    }, opts.size)

    // Poll until the presented o0 surface texture is stably at the requested size.
    // This drains any pending layout `resize` event and re-asserts the pipeline
    // size, guaranteeing the readback below sees a `size`x`size` surface.
    await page.waitForFunction((size) => {
      const p = window.__noisemakerRenderingPipeline
      if (!p || typeof p.resize !== 'function') return false
      const surf = p.surfaces && p.surfaces.get(p.graph?.renderSurface || 'o0')
      const info = surf && p.backend?.textures?.get(surf.read)
      if (!info) return false
      if (info.width !== size || info.height !== size) {
        // Re-assert (cheap no-op when already correct) and keep waiting.
        p.resize(size, size)
        return false
      }
      return true
    }, { timeout: STATUS_TIMEOUT }, opts.size)

    // Pin the normalized frame time, then render deterministic frames by driving the
    // PIPELINE directly (the CanvasRenderer re-syncs canvas size per frame and can
    // revert the resize; pipeline.render does the GPU work the readback reads).
    await page.evaluate(({ time, frames, size }) => {
      if (window.__noisemakerSetPausedTime) window.__noisemakerSetPausedTime(time)
      const p = window.__noisemakerRenderingPipeline
      const r = window.__noisemakerCanvasRenderer
      for (let i = 0; i < frames; i++) {
        if (p && p.render) p.render(time)
        else if (r && r.render) r.render(time)
      }
    }, { time: opts.time, frames: 8, size: opts.size })

    // Read back the presented o0 surface as LINEAR FLOAT (the reference RTs are
    // ARGBHalf linear). This matches the WebGL2 float readback in the repo's
    // playwright spec. We capture the surface texture, NOT the composited canvas
    // (which would be blended over the page background and gamma-encoded).
    const result = await page.evaluate(({ g }) => {
      const pipeline = window[g.renderingPipeline]
      if (!pipeline) return { status: 'error', error: 'no pipeline' }
      const backend = pipeline.backend
      const gl = backend?.gl
      const surface = pipeline.surfaces?.get(pipeline.graph?.renderSurface || 'o0')
      if (!gl || !surface) return { status: 'error', error: 'no GL surface' }
      const info = backend.textures?.get(surface.read)
      if (!info?.handle) return { status: 'error', error: 'no texture handle' }
      const { handle, width, height, glFormat } = info
      const fbo = gl.createFramebuffer()
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo)
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, handle, 0)
      if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE) {
        gl.bindFramebuffer(gl.FRAMEBUFFER, null); gl.deleteFramebuffer(fbo)
        return { status: 'error', error: 'FBO incomplete' }
      }
      const canFloat = !!(gl.getExtension('EXT_color_buffer_float') || gl.getExtension('WEBGL_color_buffer_float'))
      const isFloat = glFormat?.type === gl.HALF_FLOAT || glFormat?.type === gl.FLOAT
      gl.finish()
      let rgba8
      if (isFloat && canFloat) {
        const buf = new Float32Array(width * height * 4)
        gl.readPixels(0, 0, width, height, gl.RGBA, gl.FLOAT, buf)
        // Quantise linear float -> 8-bit for a comparable PNG. The Unity runner
        // must write the SAME linear-encoded 8-bit (no sRGB) for compare.py.
        rgba8 = new Array(width * height * 4)
        for (let i = 0; i < buf.length; i++) {
          rgba8[i] = Math.max(0, Math.min(255, Math.round(buf[i] * 255)))
        }
      } else {
        const buf = new Uint8Array(width * height * 4)
        gl.readPixels(0, 0, width, height, gl.RGBA, gl.UNSIGNED_BYTE, buf)
        rgba8 = Array.from(buf)
      }
      gl.bindFramebuffer(gl.FRAMEBUFFER, null)
      gl.deleteFramebuffer(fbo)
      return { status: 'ok', width, height, pixels: rgba8 }
    }, { g: globals })

    if (result.status === 'error') throw new Error(`readback failed: ${result.error}`)

    // Encode PNG with bottom-left origin flipped to top-down (PNG row 0 = top).
    // GL textures are bottom-left origin; we flip vertically here so the golden
    // PNG is top-down. The Unity runner applies the SAME final orientation —
    // this is the single Y-flip reconciliation point (reference/04 §coords,
    // NMBlit.shader). // TODO(verify): confirm orientation against a known
    // gradient program once both PNGs exist.
    const { width, height, pixels } = result
    const topDown = Buffer.alloc(width * height * 4)
    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        const src = ((height - 1 - y) * width + x) * 4
        const dst = (y * width + x) * 4
        topDown[dst] = pixels[src]
        topDown[dst + 1] = pixels[src + 1]
        topDown[dst + 2] = pixels[src + 2]
        topDown[dst + 3] = pixels[src + 3]
      }
    }
    pngBuffer = encodePng(width, height, topDown)

    const consoleErrors = session.getConsoleMessages().map(m => m.text)
    if (consoleErrors.length) {
      process.stderr.write(`[parity] console messages during render:\n  ${consoleErrors.join('\n  ')}\n`)
    }
  } finally {
    await session.teardown()
  }

  const pngPath = join(opts.outDir, `${programName}.golden.png`)
  writeFileSync(pngPath, pngBuffer)
  process.stderr.write(`[parity] wrote ${pngPath} (${opts.size}x${opts.size}, time=${opts.time}, backend=${opts.backend})\n`)
}

main().catch(err => {
  process.stderr.write(`[parity] FAILED: ${err?.stack || err?.message || JSON.stringify(err)}\n`)
  process.exit(1)
})
