#!/usr/bin/env node
// convert-shaders.mjs — AUTOMATED reference-GLSL → TouchDesigner-GLSL-TOP transpiler.
//
// Walks shaders/effects/<ns>/<name>/glsl/<program>.glsl (the reference's shipping
// WebGL2 / GLSL-ES-300 shaders) and emits a TouchDesigner GLSL TOP pixel shader to
//   td/noisemaker/shaders/effects/<ns>/<name>/<program>.frag
//
// WHY GLSL, not WGSL: TouchDesigner's GLSL TOP is OpenGL GLSL with the SAME bottom-left
// raster origin as the reference's WebGL2 backend, so the reference GLSL is the closest
// possible source — the transform is purely structural (no math edits, no Y-flip by
// default), which is why this can be automated. See ARCHITECTURE.md / PORTING-GUIDE.md.
//
// The transform (and ONLY this):
//   1. strip `#version …`, `precision …`, and `#ifdef GL_ES … #endif` precision guards
//      (TD auto-prepends its own `#version` + preamble incl. sTD2DInputs / TDOutputSwizzle).
//   2. drop `uniform sampler2D <name>;` declarations; emit `#define <name> sTD2DInputs[i]`
//      in declaration order, and a machine-readable `// NM_INPUTS: <name>=i …` header that
//      the Python network builder reads to wire TOP inputs in the same order.
//   3. detect the fragment output. Single `out vec4 <name>;` → rename `main`→`nm_main` and
//      append `void main(){ nm_main(); <name> = TDOutputSwizzle(<name>); }` so the swizzle
//      is applied exactly once regardless of how the body writes it. Multiple outputs (MRT,
//      e.g. agent passes) are emitted verbatim and FLAGGED for manual review (no auto-wrap).
//   4. everything else — uniforms, helpers (PCG/prng/map/…), `gl_FragCoord`, `textureSize`,
//      `#ifndef X #define X default #endif` define-fallbacks, the entire body — is preserved
//      VERBATIM. Per-pass compile-time define overrides (NOISE_TYPE, LOOP_OFFSET) are injected
//      by the builder at network-build time, not baked here.
//
// Y-flip: default OFF (TD == reference == OpenGL bottom-left, verified at bring-up Task 2.3).
// `--flip-y` routes `gl_FragCoord` through an `nm_FragCoord` helper that flips Y against
// `uTDOutputInfo.res.w`, the single control point if bring-up ever shows a mismatch.
//
// Usage:
//   node convert-shaders.mjs                 # transpile all effects
//   node convert-shaders.mjs synth/noise     # one effect (ns/name)
//   node convert-shaders.mjs --flip-y        # emit with Y-flip indirection
//   node convert-shaders.mjs --dry-run       # report only, write nothing
//
// Env:
//   NM_REFERENCE_ROOT  reference engine root (required; no default — no sibling assumed)
//   NM_OUT_DIR         output root (default: ../td/noisemaker/shaders/effects)

import { readdirSync, statSync, mkdirSync, writeFileSync, existsSync } from 'node:fs'
import { readFileSync } from 'node:fs'
import { join, dirname, resolve, basename } from 'node:path'
import { fileURLToPath } from 'node:url'
import { referenceRoot } from './reference-root.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const REFERENCE_ROOT = referenceRoot()
const EFFECTS_DIR = join(REFERENCE_ROOT, 'shaders', 'effects')
const OUT_DIR = process.env.NM_OUT_DIR
  ? resolve(process.env.NM_OUT_DIR)
  : resolve(__dirname, '..', 'td', 'noisemaker', 'shaders', 'effects')

const NAMESPACES = [
  'classicNoisedeck', 'filter', 'filter3d',
  'mixer', 'points', 'render', 'synth', 'synth3d'
]

// ---------------------------------------------------------------------------
// The transpile. Pure string surgery; never touches the math.
// ---------------------------------------------------------------------------
function transpile (src, { flipY }) {
  const notes = []
  let lines = src.split('\n')

  // 1. strip headers: #version, standalone precision, and GL_ES precision guards.
  const out = []
  for (let i = 0; i < lines.length; i++) {
    const ln = lines[i]
    const t = ln.trim()
    if (/^#version\b/.test(t)) continue
    if (/^precision\s+(highp|mediump|lowp)\b/.test(t)) continue
    // `#ifdef GL_ES … #endif` wrapping only precision lines → drop the whole guard.
    if (/^#ifdef\s+GL_ES\b/.test(t)) {
      let j = i + 1
      const inner = []
      while (j < lines.length && !/^\s*#endif\b/.test(lines[j])) { inner.push(lines[j]); j++ }
      const onlyPrecision = inner.every(l => l.trim() === '' || /^precision\b/.test(l.trim()))
      if (onlyPrecision && j < lines.length) { i = j; continue } // skip guard + #endif
      // otherwise keep the guard (rare) — falls through
    }
    out.push(ln)
  }
  let body = out.join('\n')

  // 2. input samplers: declaration order → sTD2DInputs[i].
  // Tolerate a trailing line comment after the `;` (the reference declares e.g.
  // `uniform sampler2D selfTex;   // Feedback buffer` — without `(?://.*)?` these were missed,
  // left as unbound custom samplers reading black: the all-black `feedback` bug).
  const samplerRe = /^[ \t]*uniform[ \t]+sampler2D[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*;[ \t]*(?:\/\/.*)?$/gm
  const inputs = []
  let m
  while ((m = samplerRe.exec(body)) !== null) inputs.push(m[1])
  body = body.replace(samplerRe, '') // remove declarations
  const inputDefines = inputs.map((n, i) => `#define ${n} sTD2DInputs[${i}]`).join('\n')

  // 2b. v_texCoord: the reference's vertex-shader [0,1] quad varying. A TD GLSL TOP doesn't output
  // it — it provides the built-in `vUV` instead — so a frag declaring `in vec2 v_texCoord;` fails to
  // LINK ("Input 'v_texCoord' … has no corresponding output in vertex shader"). Map it to `vUV.st`
  // (the same bottom-left [0,1] texcoord — no Y-flip, matching the default convention). Only
  // filter/{texture,grime,wobble,spookyTicker} declare it.
  body = body.replace(/^[ \t]*in[ \t]+vec2[ \t]+v_texCoord[ \t]*;[ \t]*(?:\/\/.*)?$/gm,
    '#define v_texCoord vUV.st')

  // 2c. std140 uniform BLOCK -> plain uniform decls. A TD GLSL TOP has no UBO parameter, but it binds
  // a large `uniform vec4 data[N]` as a **Uniform Array** (Arrays page, CHOP-sourced) — the std140 UBO
  // equivalent (proven: td/array_probe.py). Strip `layout(std140) uniform Name { <decls>; };` to its
  // inner `uniform <decl>;` lines; the backend packs the flat uniforms into the array via the effect's
  // uniformLayout. Only synth/remap declares one (`vec4 data[267]`). Flagged so the builder can see it.
  body = body.replace(/layout\s*\(\s*std140\s*\)\s*uniform\s+(\w+)\s*\{([\s\S]*?)\}\s*;/g,
    (_m, blockName, inner) => {
      const decls = inner.split(';').map(s => s.trim()).filter(Boolean)
      notes.push(`UNIFORM_ARRAY (std140 block ${blockName} -> ${decls.join('; ')})`)
      return decls.map(d => `uniform ${d};`).join('\n')
    })

  // 3. fragment outputs.
  const outRe = /^[ \t]*(?:layout\s*\([^)]*\)\s*)?out[ \t]+vec4[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*;[ \t]*(?:\/\/.*)?$/gm
  const outs = []
  let om
  while ((om = outRe.exec(body)) !== null) outs.push(om[1])
  const isMRT = outs.length > 1
  const outName = outs[0] || 'fragColor'
  if (outs.length === 0) notes.push('NO_OUT_VAR (gl_FragColor-style? needs manual port)')
  if (isMRT) notes.push(`MRT (${outs.length} outputs: ${outs.join(',')}) — emitted verbatim, manual swizzle/wiring`)

  // 4. optional Y-flip indirection.
  let flipPreamble = ''
  if (flipY) {
    // route gl_FragCoord through nm_FragCoord (flip Y about the output height).
    body = body.replace(/\bgl_FragCoord\b/g, 'nm_FragCoord')
    flipPreamble =
      '// --flip-y: gl_FragCoord routed through nm_FragCoord (Y flipped about output height)\n' +
      'vec4 nm_FragCoord;\n'
    // define nm_FragCoord at top of nm_main (injected in the rename step below).
  }

  // header the builder reads.
  const inputsHeader = `// NM_INPUTS: ${inputs.length ? inputs.map((n, i) => `${n}=${i}`).join(' ') : '(none)'}`
  const outHeader = `// NM_OUTPUT: ${isMRT ? `MRT ${outs.join(',')}` : outName}`

  // assemble: machine header + input #defines + (flip decl) + transformed body.
  let result = [inputsHeader, outHeader, inputDefines, flipPreamble, body.trim(), ''].filter(s => s !== '').join('\n')

  // 5. single-output: rename main→nm_main and wrap with TDOutputSwizzle.
  if (!isMRT && outs.length === 1) {
    let renamed = false
    result = result.replace(/\bvoid[ \t]+main[ \t]*\(/, () => { renamed = true; return 'void nm_main(' })
    if (renamed) {
      // inject nm_FragCoord assignment at the start of nm_main if flipping.
      if (flipY) {
        result = result.replace(/\bvoid nm_main\s*\(\s*\)\s*\{/,
          'void nm_main() {\n    nm_FragCoord = vec4(gl_FragCoord.x, uTDOutputInfo.res.w - gl_FragCoord.y, gl_FragCoord.z, gl_FragCoord.w);')
      }
      result += `\nvoid main() {\n    nm_main();\n    ${outName} = TDOutputSwizzle(${outName});\n}\n`
    } else {
      notes.push('NO_MAIN (could not find void main) — needs manual port')
    }
  }

  return { result, inputs, outs, isMRT, notes }
}

// ---------------------------------------------------------------------------
function* enumeratePrograms (filter) {
  for (const namespace of NAMESPACES) {
    const nsDir = join(EFFECTS_DIR, namespace)
    if (!existsSync(nsDir)) continue
    for (const entry of readdirSync(nsDir).sort()) {
      const effectDir = join(nsDir, entry)
      if (!statSync(effectDir).isDirectory()) continue
      if (filter && `${namespace}/${entry}` !== filter) continue
      const glslDir = join(effectDir, 'glsl')
      if (!existsSync(glslDir)) continue
      for (const g of readdirSync(glslDir).sort()) {
        if (!g.endsWith('.glsl')) continue
        yield { namespace, name: entry, program: basename(g, '.glsl'), path: join(glslDir, g) }
      }
    }
  }
}

function main () {
  const argv = process.argv.slice(2)
  const dryRun = argv.includes('--dry-run')
  const flipY = argv.includes('--flip-y')
  const filter = argv.find(a => !a.startsWith('--')) || null

  let written = 0, flagged = 0, programs = 0
  const flags = []

  for (const { namespace, name, program, path } of enumeratePrograms(filter)) {
    programs++
    const src = readFileSync(path, 'utf8')
    let res
    try {
      res = transpile(src, { flipY })
    } catch (err) {
      flagged++; flags.push(`${namespace}/${name}/${program}: THREW ${err?.message || err}`); continue
    }
    if (res.notes.length) { flagged++; flags.push(`${namespace}/${name}/${program}: ${res.notes.join('; ')}`) }
    const outDir = join(OUT_DIR, namespace, name)
    const outPath = join(outDir, `${program}.frag`)
    if (!dryRun) {
      mkdirSync(outDir, { recursive: true })
      writeFileSync(outPath, res.result)
    }
    written++
    process.stderr.write(`[shaders] ${namespace}/${name}/${program} -> ${program}.frag` +
      `${res.inputs.length ? ` [in:${res.inputs.join(',')}]` : ''}${res.isMRT ? ' [MRT]' : ''}${dryRun ? ' (dry)' : ''}\n`)
  }

  process.stderr.write(`\n[shaders] ${dryRun ? 'would write' : 'wrote'} ${written}/${programs} program(s); ${flagged} flagged for review.\n`)
  for (const f of flags) process.stderr.write(`  ! ${f}\n`)
}

if (basename(process.argv[1] || '') === 'convert-shaders.mjs') {
  try { main() } catch (err) {
    process.stderr.write(`[shaders] FAILED: ${err?.stack || err?.message || err}\n`); process.exit(1)
  }
}
