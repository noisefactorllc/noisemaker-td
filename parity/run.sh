#!/usr/bin/env bash
# parity/run.sh — golden (reference) -> candidate (TouchDesigner) -> compare.
#
# Usage:
#   parity/run.sh solid                 # one Tier-1 program
#   parity/run.sh all                   # all Tier-1 programs
#   SIZE=256 TIME=0.25 parity/run.sh solid
#
# PREREQUISITE: TouchDesigner must be LICENSE-ACTIVATED once (Derivative account + key, via the
# GUI) before this can render. A fresh install blocks at the activation modal and the candidate
# render will TIME OUT — this script detects that and tells you. See README "Prerequisites".
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
REF="${NM_REFERENCE_ROOT:-$REPO/../noisemaker}"
TD="${TD_BIN:-/Applications/TouchDesigner.app/Contents/MacOS/TouchDesigner}"
SIZE="${SIZE:-256}"
TIME="${TIME:-0.25}"
TOL="${TOL:-2}"
SSIM="${SSIM:-0.98}"
OUT="$REPO/parity/out"
TIER1="solid noise cell gradient shape osc2d blur blendMode"
mkdir -p "$OUT"

run_one() {
  local prog="$1"
  local dsl="$REPO/parity/programs/$prog.dsl"
  local graph="$OUT/$prog.graph.json"
  local golden="$OUT/$prog.golden.png"
  local cand="$OUT/$prog.candidate.png"
  local donef="/tmp/nm_done_$prog.txt"
  local logf="/tmp/nm_log_$prog.txt"
  echo "=== $prog ==="

  # 1. golden graph JSON (reference compileGraph — the unchanged producer)
  NM_REFERENCE_ROOT="$REF" node "$REPO/tools/export-graph.mjs" --file "$dsl" "$graph" >/dev/null 2>&1 \
    || { echo "  FAIL: export-graph"; return 1; }

  # 2. golden PNG (reference WebGL2 render). If absent and the harness can't produce it, skip compare.
  if [ ! -f "$golden" ]; then
    NM_REFERENCE_ROOT="$REF" node "$REPO/parity/export-and-render.mjs" "$prog" "$OUT" \
      --size "$SIZE" --time "$TIME" --backend webgl2 >/dev/null 2>&1 || true
  fi

  # 3. candidate render in TouchDesigner (display-bound, scripted, auto-quit)
  rm -f "$cand" "$donef" "$logf"
  NM_RUNTIME="$REPO/td" NM_GRAPH="$graph" NM_OUT="$cand" NM_SIZE="$SIZE" NM_TIME="$TIME" \
  NM_LOG="$logf" NM_DONE="$donef" \
    TOUCH_START_COMMAND="exec(open('$REPO/parity/render-candidate.py').read())" \
    "$TD" >/dev/null 2>&1 &
  local td_pid=$!

  # wait up to 90s for the done sentinel
  local i=0
  while [ $i -lt 90 ]; do
    [ -f "$donef" ] && break
    kill -0 "$td_pid" 2>/dev/null || break
    sleep 1; i=$((i+1))
  done
  kill "$td_pid" 2>/dev/null; pkill -f 'MacOS/TouchDesigner' 2>/dev/null; sleep 1

  if [ ! -f "$cand" ]; then
    echo "  FAIL: no candidate after ${i}s."
    if [ ! -s "$logf" ]; then
      echo "  HINT: TD wrote no log — it is almost certainly blocked at the one-time LICENSE"
      echo "        ACTIVATION dialog. Activate TouchDesigner once (GUI), then re-run."
    else
      echo "  --- candidate log ---"; sed 's/^/  /' "$logf"
    fi
    return 1
  fi

  # 4. compare
  if [ ! -f "$golden" ]; then
    echo "  candidate OK ($cand) but no golden PNG — generate goldens (reference render) to compare."
    return 0
  fi
  python3 "$REPO/parity/compare.py" "$golden" "$cand" --name "$prog" --tolerance "$TOL" --ssim-min "$SSIM"
}

main() {
  local target="${1:-all}"
  local rc=0
  if [ "$target" = "all" ]; then
    for p in $TIER1; do run_one "$p" || rc=1; done
  else
    run_one "$target" || rc=1
  fi
  exit $rc
}
main "$@"
