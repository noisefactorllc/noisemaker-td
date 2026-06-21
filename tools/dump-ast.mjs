#!/usr/bin/env node
// dump-ast.mjs — dump the REFERENCE parser's AST for a DSL file as canonical JSON. The golden
// the Python parser port is diffed against (parity/compiler/check_parse.py). `parse(lex(src))`
// is self-contained for the builtin namespaces (shaders/src/runtime/tags.js seeds them), so no
// effect bootstrap is needed at the parse stage.
//
// Env: NM_REFERENCE_ROOT  reference engine root (required; no default — no sibling assumed)
import { readFileSync } from 'node:fs'
import { resolve, dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { referenceRoot } from './reference-root.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const REF = referenceRoot()

const lang = await import(pathToFileURL(join(REF, 'shaders', 'src', 'lang', 'index.js')).href)
const { lex, parse } = lang

const file = process.argv[2]
if (!file) { process.stderr.write('usage: node dump-ast.mjs <file.dsl>\n'); process.exit(2) }
const src = readFileSync(file, 'utf8')
const astNode = parse(lex(src))
process.stdout.write(JSON.stringify(astNode))
