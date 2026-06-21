#!/usr/bin/env node
// dump-tokens.mjs — dump the REFERENCE lexer's token stream for a DSL file as canonical
// JSON (one array of {type,lexeme,line,col}). The golden the Python lexer port is diffed
// against (parity/compiler/check_lex.py). lex() is pure tokenization — no registry needed,
// so we import lexer.js directly to avoid pulling in the validator/runtime.
//
// Env: NM_REFERENCE_ROOT  reference engine root (required; no default — no sibling assumed)
import { readFileSync } from 'node:fs'
import { resolve, dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { referenceRoot } from './reference-root.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const REF = referenceRoot()

const { lex } = await import(pathToFileURL(join(REF, 'shaders', 'src', 'lang', 'lexer.js')).href)

const file = process.argv[2]
if (!file) { process.stderr.write('usage: node dump-tokens.mjs <file.dsl>\n'); process.exit(2) }
const src = readFileSync(file, 'utf8')
const toks = lex(src).map(t => ({ type: t.type, lexeme: t.lexeme, line: t.line, col: t.col }))
process.stdout.write(JSON.stringify(toks))
