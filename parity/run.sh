#!/usr/bin/env bash
# parity/run.sh — golden (reference) -> candidate (TouchDesigner) -> compare.
#
# Usage:
#   parity/run.sh                       # all Tier-1 programs
#   parity/run.sh all                   # all Tier-1 programs
#   parity/run.sh solid                 # one program
#   SIZE=256 TIME=0.25 parity/run.sh
#
# How it works: TouchDesigner has no headless startup hook, so we build a bootstrap .toe
# (td/build_parity_toe.py) whose Execute DAT renders the candidates on load via
# td/parity_render_all.py, then quits. We launch it display-bound and auto-quitting.
#
# PREREQUISITE: TouchDesigner must be LICENSE-ACTIVATED once (Derivative account + key, via the
# GUI) before this can render. A fresh install blocks at the activation modal and the render
# will TIME OUT — this script detects that and says so. See README "Prerequisites".
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
# Reference engine via NM_REFERENCE_ROOT (no default — this repo assumes no sibling project on clone).
REF="${NM_REFERENCE_ROOT:-}"
[ -n "$REF" ] && [ -d "$REF/shaders" ] || { echo "set NM_REFERENCE_ROOT to the upstream Noisemaker engine (the tree containing shaders/) — no sibling is assumed on clone"; exit 2; }
TD="${TD_BIN:-/Applications/TouchDesigner.app/Contents/MacOS/TouchDesigner}"
PY="$REPO/parity/.venv/bin/python"; [ -x "$PY" ] || PY=python3   # needs numpy + pillow
SIZE="${SIZE:-256}"; TIME="${TIME:-0.25}"; TOL="${TOL:-2}"; SSIM="${SSIM:-0.98}"
OUT="$REPO/parity/out"; mkdir -p "$OUT"
ALL="solid noise cell gradient shape osc2d blur blendMode"
case "${1:-all}" in all) PROGS="$ALL";; *) PROGS="$1";; esac

# 1. goldens — graph JSON (unchanged reference compileGraph) + golden PNG (reference WebGL2 render)
for p in $PROGS; do
  DSL="$REPO/parity/programs/$p.dsl"; [ -f "$DSL" ] || DSL="$REPO/parity/corpus/$p.dsl"   # comps live in corpus/
  NM_REFERENCE_ROOT="$REF" node "$REPO/tools/export-graph.mjs" --file "$DSL" "$OUT/$p.graph.json" >/dev/null 2>&1 \
    || { echo "FAIL: export-graph $p"; exit 1; }
  [ -f "$OUT/$p.golden.png" ] || NM_REFERENCE_ROOT="$REF" node "$REPO/parity/export-and-render.mjs" \
    "$DSL" "$OUT" --size "$SIZE" --time "$TIME" --backend webgl2 >/dev/null 2>&1 || true
done

# 2. bootstrap .toe
python3 "$REPO/td/build_parity_toe.py" >/dev/null || { echo "FAIL: build_parity_toe.py"; exit 1; }

# 3. render candidates in TouchDesigner (batch; the .toe renders NM_PROGRAMS then quits)
rm -f "$OUT/_render_log.txt"; for p in $PROGS; do rm -f "$OUT/$p.candidate.png"; done
NM_PROGRAMS="$(echo $PROGS | tr ' ' ',')" NM_SIZE="$SIZE" NM_TIME="$TIME" NM_LIVE_DSL="${NM_LIVE_DSL:-}" \
  "$TD" "$REPO/td/nm_parity.toe" >/dev/null 2>&1 &
TDPID=$!
for i in $(seq 1 150); do
  [ -f "$OUT/_render_log.txt" ] && grep -q '=== DONE' "$OUT/_render_log.txt" 2>/dev/null && break
  kill -0 "$TDPID" 2>/dev/null || break
  sleep 1
done
sleep 1; kill "$TDPID" 2>/dev/null; pkill -f 'MacOS/TouchDesigner' 2>/dev/null

if ! grep -q '=== DONE' "$OUT/_render_log.txt" 2>/dev/null; then
  echo "FAIL: TouchDesigner did not finish rendering."
  if [ -s "$OUT/_render_log.txt" ]; then echo "--- render log ---"; sed 's/^/  /' "$OUT/_render_log.txt"
  else echo "  No render log — TD is likely blocked at the one-time LICENSE ACTIVATION dialog."
       echo "  Activate TouchDesigner once (GUI), then re-run. See README Prerequisites."; fi
  exit 1
fi

# 4. compare candidate vs golden
rc=0
for p in $PROGS; do
  if [ ! -f "$OUT/$p.candidate.png" ]; then echo "$p: FAIL (no candidate — see render log)"; rc=1; continue; fi
  if [ ! -f "$OUT/$p.golden.png" ]; then echo "$p: candidate OK, no golden PNG to compare"; continue; fi
  "$PY" "$REPO/parity/compare.py" "$OUT/$p.golden.png" "$OUT/$p.candidate.png" \
    --name "$p" --tolerance "$TOL" --ssim-min "$SSIM" || rc=1
done
exit $rc
