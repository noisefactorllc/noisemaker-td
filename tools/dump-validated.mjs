#!/usr/bin/env node
// dump-validated.mjs — dump the REFERENCE validator output `{plans, diagnostics, render}` for a
// DSL file as canonical JSON. The golden the Python validator port is diffed against
// (parity/compiler/check_validate.py). Reuses export-graph.mjs's bootstrapReference() so the
// shared op/enum/starter registries are populated exactly as the golden-graph path sees them.
//
// Env: NM_REFERENCE_ROOT  reference engine root (required; no default — no sibling assumed)
import { readFileSync } from 'node:fs'
import { resolve, dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { referenceRoot } from './reference-root.mjs'
import { bootstrapReference } from './export-graph.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const REF = referenceRoot()

await bootstrapReference()  // populate global op/enum/starter registries
const { compile } = await import(pathToFileURL(join(REF, 'shaders', 'src', 'lang', 'index.js')).href)

const file = process.argv[2]
if (!file) { process.stderr.write('usage: node dump-validated.mjs <file.dsl>\n'); process.exit(2) }
const src = readFileSync(file, 'utf8')
const result = compile(src)
process.stdout.write(JSON.stringify(result))
