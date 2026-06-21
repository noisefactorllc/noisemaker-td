#!/usr/bin/env bash
# evolve.sh <program> — STATEFUL multi-frame parity render in TouchDesigner.
#
#   parity/evolve.sh navierStokes                 # 1800 frames, sample at frame 1800
#   NM_FRAMES=30 NM_SAMPLES=10,20,30 parity/evolve.sh navierStokes
#   NM_FRAMES=1800 NM_SAMPLES=300,600,900,1200,1500,1800 parity/evolve.sh target
#
# Builds td/nm_evolve.toe (Execute DAT with frame callbacks), launches TD, which evolves the
# network over N real frames of non-real-time playback (so Feedback TOPs latch) and saves
# <prog>.f<NNNN>.candidate.png samples, then quits. Grades vs parity/out/<prog>.f<NNNN>.golden.png
# when goldens exist.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
TD="${TD_BIN:-/Applications/TouchDesigner.app/Contents/MacOS/TouchDesigner}"
PY="$REPO/parity/.venv/bin/python"; [ -x "$PY" ] || PY=python3
PROG="${1:-navierStokes}"
FRAMES="${NM_FRAMES:-1800}"
SAMPLES="${NM_SAMPLES:-$FRAMES}"
OUT="$REPO/parity/out"; mkdir -p "$OUT"

python3 "$REPO/td/build_evolve_toe.py" >/dev/null || { echo "FAIL: build_evolve_toe.py"; exit 1; }

rm -f "$OUT/_evolve_log.txt"
for s in ${SAMPLES//,/ }; do rm -f "$OUT/$PROG.f$(printf '%04d' "$s").candidate.png"; done

NM_PROGRAM="$PROG" NM_FRAMES="$FRAMES" NM_SAMPLES="$SAMPLES" NM_SIZE="${NM_SIZE:-256}" \
  NM_TIMESTEP="${NM_TIMESTEP:-0.0016667}" NM_TIME="${NM_TIME:-0.25}" \
  "$TD" "$REPO/td/nm_evolve.toe" >/dev/null 2>&1 &
PID=$!
for i in $(seq 1 "${NM_WAIT:-1800}"); do
  grep -q '=== DONE' "$OUT/_evolve_log.txt" 2>/dev/null && break
  kill -0 "$PID" 2>/dev/null || break
  sleep 1
done
sleep 1; kill "$PID" 2>/dev/null; pkill -f 'MacOS/TouchDesigner' 2>/dev/null

echo "--- evolve log ---"; sed 's/^/  /' "$OUT/_evolve_log.txt" 2>/dev/null
if ! grep -q '=== DONE' "$OUT/_evolve_log.txt" 2>/dev/null; then
  echo "FAIL: evolve did not finish (TD blocked at license dialog, build error, or playback stall)."
  exit 1
fi

# Grade any samples that have goldens.
rc=0
for s in ${SAMPLES//,/ }; do
  f="$(printf '%04d' "$s")"
  cand="$OUT/$PROG.f$f.candidate.png"; gold="$OUT/$PROG.f$f.golden.png"
  [ -f "$cand" ] || { echo "$PROG f$f: FAIL (no candidate)"; rc=1; continue; }
  if [ -f "$gold" ]; then
    "$PY" "$REPO/parity/compare.py" "$gold" "$cand" --name "$PROG.f$f" \
      --tolerance "${TOL:-2}" --ssim-min "${SSIM:-0.98}" || rc=1
  else
    echo "$PROG f$f: candidate OK (no golden to compare)"
  fi
done
exit $rc
