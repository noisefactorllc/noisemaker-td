#!/usr/bin/env node
// convert-definitions.mjs — AUTOMATED effect-definition regenerator.
//
// Walks shaders/effects/<ns>/<name>/definition.js, imports each reference Effect
// instance, and emits a runtime-shape JSON to
//   td/noisemaker/effects/<ns>/<func>.json
//
// The emitted shape matches the hand-written Tier-1 files (Effects/synth/*.json):
//   { name, namespace, func, tags, description, paramAliases,
//     globals{ <key>: { type, default, uniform, define, min, max, choices } },
//     passes[ { name, program, inputs, outputs, uniforms } ],
//     textures{ <id>: { width, height, [depth], [is3D], format } } }
//
// This tool SUPERSEDES the hand-written Tier-1 JSON for all ~175 effects: running
// it regenerates them deterministically from the single source of truth (the JS
// definitions). The C# runtime's definition loader consumes these files; shader
// SOURCE still lives in Shaders/Effects/<ns>/<Func>.{hlsl,shader} (authored/ported
// separately — see PORTING-GUIDE.md). This tool only ports the DATA.
//
// Usage:
//   node convert-definitions.mjs                # convert all effects
//   node convert-definitions.mjs synth/noise    # convert one (ns/name)
//   node convert-definitions.mjs --dry-run      # print summary, write nothing
//
// Env:
//   NM_REFERENCE_ROOT  reference engine root (required; no default — no sibling assumed)
//   NM_OUT_DIR         override output dir (default: ../td/noisemaker/effects)

import { readdirSync, statSync, mkdirSync, writeFileSync, readFileSync, existsSync } from 'node:fs'
import { join, dirname, resolve, basename } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { referenceRoot } from './reference-root.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
// Reference engine root from NM_REFERENCE_ROOT (no default — no sibling assumed on clone).
const REFERENCE_ROOT = referenceRoot()
const EFFECTS_DIR = join(REFERENCE_ROOT, 'shaders', 'effects')
const OUT_DIR = process.env.NM_OUT_DIR
  ? resolve(process.env.NM_OUT_DIR)
  : resolve(__dirname, '..', 'td', 'noisemaker', 'effects')

// Namespaces, mirrored from shaders/scripts/generate-shader-manifest.mjs.
const NAMESPACES = [
  'classicNoisedeck', 'filter', 'filter3d',
  'mixer', 'points', 'render', 'synth', 'synth3d'
]

// Bootstrap metadata — the `starter` flag is computed by the reference manifest
// generator (shaders/scripts/generate-shader-manifest.mjs isStarterEffect) and is
// the SINGLE SOURCE OF TRUTH (reference/02 §1.3 STARTER_OPS <- registerStarterOps).
// We DO NOT re-derive it; we project it straight from shaders/effects/manifest.json,
// keyed "<namespace>/<dirname>". The C# loader keys on the explicit `starter` field.
const MANIFEST_PATH = join(EFFECTS_DIR, 'manifest.json')
let MANIFEST
try {
  MANIFEST = JSON.parse(readFileSync(MANIFEST_PATH, 'utf8'))
} catch (err) {
  process.stderr.write(`[convert] FATAL: cannot read manifest ${MANIFEST_PATH} — ${err?.message || err}\n`)
  process.exit(1)
}

// ---------------------------------------------------------------------------
// Field projection. We copy only the fields the C# definition loader reads, in a
// stable order, so the output is byte-stable across runs and minimally diffs.
// ---------------------------------------------------------------------------

// Project one global/param spec. Drops UI-only metadata (ui, category, label,
// enabledBy) which the renderer never reads; keeps everything that influences
// uniforms, defines, defaults, ranges and enum mappings.
function projectGlobal (spec) {
  const out = {}
  if (spec.type !== undefined) out.type = spec.type
  if (spec.default !== undefined) out.default = spec.default
  if (spec.uniform !== undefined) out.uniform = spec.uniform
  // enum/enumPath reference an EXTERNAL enum table (e.g. index -> "palette",
  // smoothing -> "smoothing"). Without these the validator cannot resolve a
  // named arg (palette(index: solaris)) and silently falls back to the default
  // (reference/02 §6.10 member-param resolution). Inline `choices` are separate.
  if (spec.enum !== undefined) out.enum = spec.enum
  if (spec.enumPath !== undefined) out.enumPath = spec.enumPath
  if (spec.define !== undefined) out.define = spec.define
  if (spec.min !== undefined) out.min = spec.min
  if (spec.max !== undefined) out.max = spec.max
  if (spec.zero !== undefined) out.zero = spec.zero
  if (spec.choices !== undefined) out.choices = spec.choices
  if (spec.colorModeUniform !== undefined) out.colorModeUniform = spec.colorModeUniform
  return out
}

function projectGlobals (globals) {
  if (!globals) return {}
  const out = {}
  // Object.entries preserves declaration order — parity-critical for palette
  // index = positional key order (reference/03), so DO NOT sort.
  for (const [key, spec] of Object.entries(globals)) {
    out[key] = projectGlobal(spec)
  }
  return out
}

function projectPass (pass) {
  const out = { name: pass.name, program: pass.program }
  out.inputs = pass.inputs || {}
  if (pass.uniforms !== undefined) out.uniforms = pass.uniforms
  out.outputs = pass.outputs || {}
  // Execution modifiers (mostly used by agent/compute effects).
  if (pass.drawMode !== undefined) out.drawMode = pass.drawMode
  if (pass.drawBuffers !== undefined) out.drawBuffers = pass.drawBuffers
  if (pass.count !== undefined) out.count = pass.count
  if (pass.countUniform !== undefined) out.countUniform = pass.countUniform
  if (pass.repeat !== undefined) out.repeat = pass.repeat
  if (pass.blend !== undefined) out.blend = pass.blend
  if (pass.clear !== undefined) out.clear = pass.clear
  if (pass.type !== undefined) out.type = pass.type
  if (pass.entryPoint !== undefined) out.entryPoint = pass.entryPoint
  return out
}

function projectTextures (textures, is3D) {
  if (!textures) return undefined
  const out = {}
  for (const [id, spec] of Object.entries(textures)) {
    const t = {}
    if (spec.width !== undefined) t.width = spec.width
    if (spec.height !== undefined) t.height = spec.height
    if (spec.depth !== undefined) t.depth = spec.depth
    if (is3D || spec.is3D) t.is3D = true
    t.format = spec.format || 'rgba16f'
    out[id] = t
  }
  return out
}

function convertEffect (instance, namespace, name) {
  const func = instance.func || name
  const def = {
    name: instance.name || func,
    namespace: instance.namespace || namespace,
    func
  }
  // Authoritative starter flag from the manifest (NOT re-derived). The manifest is
  // keyed "<namespace>/<dirname>"; default false (an effect absent from the manifest
  // is not a registered starter, matching canvas.js loadManifest).
  const mkey = `${namespace}/${name}`
  const mentry = MANIFEST[mkey]
  if (mentry === undefined) {
    process.stderr.write(`[convert] WARN: ${mkey} not in manifest — starter defaults to false\n`)
  }
  def.starter = !!(mentry && mentry.starter)
  if (instance.tags) def.tags = instance.tags
  if (instance.description) def.description = instance.description
  def.paramAliases = instance.paramAliases || {}
  def.globals = projectGlobals(instance.globals)
  def.passes = (instance.passes || []).map(projectPass)
  def.textures = projectTextures(instance.textures, false) || {}

  // 3D volume textures, when present, carry is3D.
  if (instance.textures3d) {
    const t3d = projectTextures(instance.textures3d, true)
    Object.assign(def.textures, t3d)
  }

  // Carry forward optional declarative flags the runtime may key on.
  if (instance.defaultProgram !== undefined) def.defaultProgram = instance.defaultProgram
  // Output-surface passthrough declarations (reference/03 §4.10). The expander uses
  // these to update the 2D/agent-state cursors so downstream effects read the right
  // surface. The particle pipeline (pointsEmit/flow/physical/lenia/pointsRender) relies
  // on outputXyz/Vel/Rgba — dropping them broke agent-state propagation.
  if (instance.outputTex !== undefined) def.outputTex = instance.outputTex
  if (instance.outputTex3d !== undefined) def.outputTex3d = instance.outputTex3d
  if (instance.outputGeo !== undefined) def.outputGeo = instance.outputGeo
  if (instance.outputXyz !== undefined) def.outputXyz = instance.outputXyz
  if (instance.outputVel !== undefined) def.outputVel = instance.outputVel
  if (instance.outputRgba !== undefined) def.outputRgba = instance.outputRgba
  if (instance.hidden) def.hidden = true
  if (instance.deprecatedBy) def.deprecatedBy = instance.deprecatedBy

  return def
}

async function loadInstance (defPath) {
  const mod = await import(pathToFileURL(defPath).href)
  const d = mod.default
  return (typeof d === 'function') ? new d() : d
}

function* enumerateEffects (filter) {
  for (const namespace of NAMESPACES) {
    const nsDir = join(EFFECTS_DIR, namespace)
    if (!existsSync(nsDir)) continue
    for (const entry of readdirSync(nsDir).sort()) {
      const effectDir = join(nsDir, entry)
      if (!statSync(effectDir).isDirectory()) continue
      const defPath = join(effectDir, 'definition.js')
      if (!existsSync(defPath)) continue
      if (filter && `${namespace}/${entry}` !== filter) continue
      yield { namespace, name: entry, defPath }
    }
  }
}

async function main () {
  const argv = process.argv.slice(2)
  const dryRun = argv.includes('--dry-run')
  const filter = argv.find(a => !a.startsWith('--')) || null

  let written = 0
  let failed = 0
  const errors = []

  for (const { namespace, name, defPath } of enumerateEffects(filter)) {
    let instance
    try {
      instance = await loadInstance(defPath)
    } catch (err) {
      failed++
      errors.push(`${namespace}/${name}: import failed — ${err?.message || err}`)
      continue
    }
    if (!instance) {
      failed++
      errors.push(`${namespace}/${name}: no default export`)
      continue
    }
    const func = instance.func || name
    const def = convertEffect(instance, namespace, name)
    const outNsDir = join(OUT_DIR, namespace)
    const outPath = join(outNsDir, `${func}.json`)
    if (!dryRun) {
      mkdirSync(outNsDir, { recursive: true })
      writeFileSync(outPath, JSON.stringify(def, null, 2) + '\n')
    }
    written++
    process.stderr.write(`[convert] ${namespace}/${name} -> Effects/${namespace}/${func}.json${dryRun ? ' (dry-run)' : ''}\n`)
  }

  process.stderr.write(`\n[convert] ${dryRun ? 'would write' : 'wrote'} ${written} effect(s), ${failed} failed.\n`)
  for (const e of errors) process.stderr.write(`  ! ${e}\n`)
  if (failed > 0 && written === 0) process.exit(1)
}

if (basename(process.argv[1] || '') === 'convert-definitions.mjs') {
  main().catch(err => {
    process.stderr.write(`[convert] FAILED: ${err?.stack || err?.message || JSON.stringify(err)}\n`)
    process.exit(1)
  })
}
