#!/usr/bin/env node
// export-graph.mjs — GOLDEN graph producer.
//
// Runs the UNCHANGED reference JS engine (shaders/src/index.js -> compileGraph)
// on a DSL string and serialises the result to the normalized Render Graph JSON
// described in docs/GRAPH-JSON-SCHEMA.md. This is the ground truth the C# live
// path (Compiler/Expander) is diffed against, and the loader format the Unity
// runtime consumes directly.
//
// It carries ZERO graph-construction parity risk because the graph is produced
// by literally the reference code (ARCHITECTURE.md "(a) Golden path").
//
// Usage:
//   node export-graph.mjs "<dsl>" out.json
//   node export-graph.mjs --file program.dsl out.json
//   node export-graph.mjs "<dsl>"            # prints JSON to stdout
//
// Env:
//   NM_REFERENCE_ROOT   override the reference repo root (default: ../.. of this file)
//
// Requires the sibling reference engine at <root>/shaders/src/index.js. No build
// step — the reference is plain ESM. See tools/package.json.

import { readFileSync, writeFileSync, readdirSync, statSync } from 'node:fs'
import { join, dirname, resolve, basename } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))

// Reference engine lives in the sibling `noisemaker` repo (this repo was split
// out of noisemaker/noisemaker-hlsl/). Override with NM_REFERENCE_ROOT.
const REFERENCE_ROOT = process.env.NM_REFERENCE_ROOT
  ? resolve(process.env.NM_REFERENCE_ROOT)
  : resolve(__dirname, '..', '..', 'noisemaker')

const SRC_INDEX = join(REFERENCE_ROOT, 'shaders', 'src', 'index.js')
const EFFECTS_DIR = join(REFERENCE_ROOT, 'shaders', 'effects')

// ---------------------------------------------------------------------------
// Reference engine bootstrap.
//
// Effects do NOT self-register on import (definition.js only `export default new
// Effect(...)`). We mirror the registration the demo/tests perform: walk every
// effects/<ns>/<name>/definition.js, register the Effect under all lookup keys
// the validator/expander accept, and registerOp so the parser recognises the op.
// (See shaders/tests/test_canvas_apply_step_params.js loadEffect helper.)
// ---------------------------------------------------------------------------
async function bootstrapReference () {
  const mod = await import(pathToFileURL(SRC_INDEX).href)
  const {
    compileGraph, registerEffect, registerOp, registerStarterOps,
    mergeIntoEnums, stdEnums, sanitizeEnumName
  } = mod

  // Standard enums + starter ops (write/render/blend/etc.) — required before
  // any effect ops are parsed/validated. mergeIntoEnums is ASYNC — await it.
  if (mergeIntoEnums && stdEnums) await mergeIntoEnums(stdEnums)
  if (registerStarterOps) registerStarterOps()

  // Accumulated effect choices to register as resolvable enum members, so a bare
  // arg like `type: bSpline4x4` / `colorMode: hsv` / `stateSize: x2048` resolves
  // instead of failing S003. Mirrors canvas.js registerEffectWithRuntime:
  //   choicesToRegister[ns][func][key][name|sanitized] = { type:'Number', value }.
  const allChoices = {}

  // Map "<namespace>.<func>" -> { globalKey: DEFINE_NAME } for every global that
  // is a compile-time define (e.g. noise.type -> NOISE_TYPE). The reference keeps
  // these as plain uniforms in the compiled graph; the HLSL port binds them by
  // their DEFINE name, so normalizePass() promotes uniforms[globalKey] into
  // defines[DEFINE_NAME].
  const defineMap = {}

  const namespaces = readdirSync(EFFECTS_DIR, { withFileTypes: true })
    .filter(d => d.isDirectory())
    .map(d => d.name)

  for (const namespace of namespaces) {
    const nsDir = join(EFFECTS_DIR, namespace)
    let effectNames
    try {
      effectNames = readdirSync(nsDir, { withFileTypes: true })
        .filter(d => d.isDirectory())
        .map(d => d.name)
    } catch {
      continue
    }
    for (const name of effectNames) {
      const defPath = join(nsDir, name, 'definition.js')
      try { statSync(defPath) } catch { continue }
      let effectMod
      try {
        effectMod = await import(pathToFileURL(defPath).href)
      } catch (err) {
        // A single broken effect must not abort export of an unrelated program.
        process.stderr.write(`[export-graph] skip ${namespace}/${name}: ${err?.message || err}\n`)
        continue
      }
      const def = effectMod.default
      const instance = (typeof def === 'function') ? new def() : def
      if (!instance) continue

      // 17 effect definitions omit an explicit `namespace:` field (navierStokes,
      // reactionDiffusion, cellularAutomata, mnca, feedback, motionBlur, …). The
      // reference engine infers the namespace from the directory at registration;
      // expander.js sets pass.effectNamespace = effectDef.namespace || null, so an
      // omitted field makes the Unity port build "Noisemaker//<func>" (double slash)
      // -> shader not found -> the whole effect's passes are skipped. Populate it
      // from the directory here (the authoritative namespace) for parity.
      if (!instance.namespace) instance.namespace = namespace

      const func = instance.func || name
      // Register under every key form the engine may resolve `step.op` by.
      registerEffect(func, instance)
      registerEffect(`${namespace}.${func}`, instance)
      registerEffect(`${namespace}/${name}`, instance)
      registerEffect(`${namespace}.${name}`, instance)

      // Mirror canvas.js registerEffectWithRuntime: build op args AND register each
      // param's choices as enum members (with sanitized aliases) into allChoices.
      const args = Object.entries(instance.globals || {}).map(([key, spec]) => {
        let enumPath = spec.enum || spec.enumPath
        if (spec.choices && !enumPath) {
          enumPath = `${namespace}.${func}.${key}`
          allChoices[namespace] = allChoices[namespace] || {}
          allChoices[namespace][func] = allChoices[namespace][func] || {}
          allChoices[namespace][func][key] = allChoices[namespace][func][key] || {}
          for (const [nm, val] of Object.entries(spec.choices)) {
            if (typeof nm === 'string' && nm.endsWith(':')) continue // group header
            allChoices[namespace][func][key][nm] = { type: 'Number', value: val }
            const san = sanitizeEnumName ? sanitizeEnumName(nm) : nm
            if (san && san !== nm) allChoices[namespace][func][key][san] = { type: 'Number', value: val }
          }
        }
        return {
          name: key,
          type: spec.type === 'vec4' ? 'color' : spec.type,
          default: spec.default,
          enum: enumPath,
          enumPath,
          min: spec.min,
          max: spec.max,
          uniform: spec.uniform,
          choices: spec.choices
        }
      })
      if (registerOp) registerOp(`${namespace}.${func}`, { name: func, args })

      // Mark generators (no upstream-surface input) as STARTER ops (reference test
      // shaders/tests/test_canvas_apply_step_params.js); else `solid().write(o0)`
      // fails S005 (illegal chain).
      const isStarter = !((instance.passes || []).some(p =>
        p.inputs && Object.values(p.inputs).some(v =>
          ['inputTex', 'inputTex3d', 'src', 'o0', 'o1'].includes(v))))
      if (isStarter && registerStarterOps) registerStarterOps([`${namespace}.${func}`])
      if (instance.enums && mergeIntoEnums) await mergeIntoEnums(instance.enums)

      const defs = {}
      for (const [key, spec] of Object.entries(instance.globals || {})) {
        if (spec && spec.define) defs[key] = spec.define
      }
      if (Object.keys(defs).length) defineMap[`${namespace}.${func}`] = defs
    }
  }

  // Register all collected effect choices as enum members (one async merge).
  if (mergeIntoEnums && Object.keys(allChoices).length) await mergeIntoEnums(allChoices)

  return { compileGraph, defineMap }
}

// ---------------------------------------------------------------------------
// Normalisation: reference graph -> docs/GRAPH-JSON-SCHEMA.md shape.
//   * Map -> plain object (textures is a Map; allocations/programs are objects)
//   * each pass gets passType/namespace/func/progName/defines convenience fields
//   * drop volatile/runtime-only fields (compiledAt, workgroups, entryPoint...)
//   * preserve insertion order everywhere (parity-critical: phys_N / uniform order)
// ---------------------------------------------------------------------------
function mapToObject (m) {
  if (!m) return {}
  if (m instanceof Map) {
    const out = {}
    for (const [k, v] of m) out[k] = v
    return out
  }
  return m
}

// progName = the bare program basename. Reference program ids are
// `${nodeId}_${progName}${defineSuffix}`. We recover the basename from the pass's
// effectFunc-relative program name by stripping the `node_<n>_` prefix and any
// `__KEY_VAL` define suffix. Falls back to func.
function deriveProgName (pass) {
  const raw = pass.program || ''
  let s = raw
  const nodePrefix = pass.nodeId ? `${pass.nodeId}_` : null
  if (nodePrefix && s.startsWith(nodePrefix)) s = s.slice(nodePrefix.length)
  // Strip a trailing define-variant suffix: a run of `__name_value` groups.
  const suffixIdx = s.indexOf('__')
  if (suffixIdx > 0) s = s.slice(0, suffixIdx)
  return s || pass.effectFunc || 'main'
}

// Compile-time defines for a pass come from its resolved program entry, which
// carries `defines` (NOISE_TYPE, LOOP_OFFSET, ...). They are int-valued.
function definesForPass (pass, programs) {
  const prog = programs && programs[pass.program]
  const d = prog && prog.defines
  if (!d) return {}
  const out = {}
  for (const [k, v] of Object.entries(d)) out[k] = v
  return out
}

function normalizePass (pass, programs, defineMap) {
  const isBlit = pass.type === 'blit' || pass.program === 'blit' ||
    (pass.effectFunc === 'blit')
  const out = {
    id: pass.id,
    passType: isBlit ? 'blit' : 'effect',
    namespace: isBlit ? null : (pass.effectNamespace ?? null),
    func: isBlit ? 'blit' : (pass.effectFunc ?? null),
    progName: isBlit ? 'blit' : deriveProgName(pass),
    program: pass.program ?? null,
    defines: isBlit ? {} : definesForPass(pass, programs),
    inputs: pass.inputs || {},
    outputs: pass.outputs || {},
    uniforms: { ...(pass.uniforms || {}) },
    uniformSpecs: pass.uniformSpecs || {}
  }

  // Promote compile-time define globals (e.g. noise.type=10 -> NOISE_TYPE:10)
  // from uniforms into defines by their DEFINE name. The HLSL port binds these by
  // DEFINE name; the reference graph leaves them as plain uniforms.
  if (!isBlit && defineMap) {
    const dm = defineMap[`${pass.effectNamespace}.${pass.effectFunc}`] || {}
    for (const [globalKey, defineName] of Object.entries(dm)) {
      if (globalKey in out.uniforms) {
        const v = out.uniforms[globalKey]
        out.defines[defineName] = (typeof v === 'boolean') ? (v ? 1 : 0) : Math.trunc(Number(v))
        delete out.uniforms[globalKey]
      }
    }
  }

  // Optional execution modifiers — only emit when meaningfully present so the
  // JSON stays diffable and the C# loader's "absent vs null vs 0" model holds.
  if (pass.drawMode !== undefined) out.drawMode = pass.drawMode
  if (pass.count !== undefined) out.count = pass.count
  if (pass.countUniform !== undefined) out.countUniform = pass.countUniform
  if (pass.drawBuffers !== undefined) out.drawBuffers = pass.drawBuffers
  if (pass.blend !== undefined) out.blend = pass.blend
  if (pass.repeat !== undefined) out.repeat = pass.repeat
  if (pass.clear !== undefined) out.clear = pass.clear

  // Metadata.
  out.effectKey = pass.effectKey ?? null
  out.nodeId = pass.nodeId ?? null
  if (pass.stepIndex !== undefined) out.stepIndex = pass.stepIndex
  if (pass.inheritsVolumeSize !== undefined) out.inheritsVolumeSize = pass.inheritsVolumeSize
  out.scopedParams = pass.scopedParams || null

  return out
}

function normalizePrograms (programs) {
  const out = {}
  for (const [id, prog] of Object.entries(programs || {})) {
    // The HLSL loader does not need shader source (it resolves a Unity Shader by
    // namespace/func). Keep only uniformLayout + defines for traceability/diff.
    out[id] = {
      uniformLayout: prog.uniformLayout || {},
      defines: prog.defines || {}
    }
  }
  return out
}

export function normalizeGraph (graph, defineMap) {
  const programs = graph.programs || {}
  return {
    id: graph.id,
    source: graph.source,
    renderSurface: graph.renderSurface ?? null,
    passes: (graph.passes || []).map(p => normalizePass(p, programs, defineMap)),
    allocations: mapToObject(graph.allocations),
    textures: mapToObject(graph.textures),
    programs: normalizePrograms(programs)
  }
}

export async function exportGraph (dsl) {
  const { compileGraph, defineMap } = await bootstrapReference()
  const graph = compileGraph(dsl)
  return normalizeGraph(graph, defineMap)
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------
async function main () {
  const argv = process.argv.slice(2)
  if (argv.length === 0) {
    process.stderr.write('usage: node export-graph.mjs "<dsl>" [out.json]\n' +
      '       node export-graph.mjs --file program.dsl [out.json]\n')
    process.exit(2)
  }

  let dsl
  let outPath
  if (argv[0] === '--file') {
    dsl = readFileSync(argv[1], 'utf8')
    outPath = argv[2]
  } else {
    dsl = argv[0]
    outPath = argv[1]
  }

  const normalized = await exportGraph(dsl)
  const json = JSON.stringify(normalized, null, 2)

  if (outPath) {
    writeFileSync(outPath, json + '\n')
    process.stderr.write(`[export-graph] wrote ${outPath} (${normalized.passes.length} passes)\n`)
  } else {
    process.stdout.write(json + '\n')
  }
}

if (basename(process.argv[1] || '') === 'export-graph.mjs') {
  main().catch(err => {
    process.stderr.write(`[export-graph] FAILED: ${err?.stack || err?.message || JSON.stringify(err)}\n`)
    process.exit(1)
  })
}
