// reference-root.mjs — resolve the upstream Noisemaker engine root from NM_REFERENCE_ROOT.
//
// This repo does NOT bundle or assume any sibling project is present on a fresh clone. The runtime
// port (td/noisemaker/) is fully self-contained; only the dev-time parity / codegen tooling needs
// the upstream engine — to (re)generate reference goldens or re-transpile the shaders/definitions.
// There is intentionally NO default path (no "../noisemaker"): point NM_REFERENCE_ROOT at the engine
// root (the tree that contains shaders/) when you run that tooling.
import { resolve } from 'node:path'
import { existsSync } from 'node:fs'

export function referenceRoot ({ need = 'shaders' } = {}) {
  const env = process.env.NM_REFERENCE_ROOT
  if (!env) {
    process.stderr.write(
      'NM_REFERENCE_ROOT is not set.\n' +
      'This repo does not assume any sibling project on clone. Set NM_REFERENCE_ROOT to the upstream\n' +
      'Noisemaker engine root (the tree containing shaders/) to run this parity/codegen tool.\n')
    process.exit(3)
  }
  const root = resolve(env)
  if (need && !existsSync(resolve(root, need))) {
    process.stderr.write(`NM_REFERENCE_ROOT=${root} has no ${need}/ — not a Noisemaker engine root.\n`)
    process.exit(3)
  }
  return root
}
