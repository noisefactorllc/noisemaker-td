#!/usr/bin/env bash
# points_probe.sh — run the isolated GPU point-scatter probe in TouchDesigner.
#
# Builds td/nm_points_probe.toe (Execute DAT onStart -> probe_main), launches TD, which builds a
# tiny known-answer scatter network (4 agents -> 8x8 target) on BOTH the direct-gl_Position and
# the TDWorldToProj+camera branches, logs the per-pixel verdict, and quits. Prints the log.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
TD="${TD_BIN:-/Applications/TouchDesigner.app/Contents/MacOS/TouchDesigner}"
OUT="$REPO/parity/out"; mkdir -p "$OUT"

python3 "$REPO/td/build_points_probe_toe.py" >/dev/null || { echo "FAIL: build_points_probe_toe.py"; exit 1; }

rm -f "$OUT/_probe_log.txt" "$OUT/probe_direct.png" "$OUT/probe_camera.png"

NM_TD_REPO="$REPO" "$TD" "$REPO/td/nm_points_probe.toe" >/dev/null 2>&1 &
PID=$!
for i in $(seq 1 "${NM_WAIT:-180}"); do
  grep -q '=== PROBE DONE' "$OUT/_probe_log.txt" 2>/dev/null && break
  kill -0 "$PID" 2>/dev/null || break
  sleep 1
done
sleep 1; kill "$PID" 2>/dev/null; pkill -f 'MacOS/TouchDesigner' 2>/dev/null

echo "--- probe log ---"; sed 's/^/  /' "$OUT/_probe_log.txt" 2>/dev/null
grep -q '=== PROBE DONE' "$OUT/_probe_log.txt" 2>/dev/null || { echo "FAIL: probe did not finish"; exit 1; }
