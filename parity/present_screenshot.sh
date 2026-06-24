#!/usr/bin/env bash
# present_screenshot.sh — open the flagship in TouchDesigner, evolve it 30s, and screenshot the app.
#
#   parity/present_screenshot.sh                 # present_hero, 1024px, 1800 frames (30s)
#   NM_PROGRAM=present_hero NM_SIZE=1024 NM_FRAMES=1800 parity/present_screenshot.sh
#
# Builds td/nm_present.toe, launches TD (GUI), which builds the program as a live node network,
# evolves NM_FRAMES frames of non-real-time playback (Feedback TOPs latch — the real 30s state),
# then opens the output TOP in a viewer, frames the network, writes a READY marker and STAYS OPEN.
# We then screencapture the screen. Capturing the display needs macOS Screen Recording permission
# for the HOST terminal app (Terminal.app here) — if the grab is black/empty the script leaves TD
# open and tells you to grant it (System Settings -> Privacy & Security -> Screen Recording) or snap
# it yourself (Cmd+Shift+4, Space, click the TD window).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(cd "$HERE/.." && pwd)"
TD="${TD_BIN:-/Applications/TouchDesigner.app/Contents/MacOS/TouchDesigner}"
PY="$REPO/parity/.venv/bin/python"; [ -x "$PY" ] || PY=python3
OUT="$REPO/parity/out"; mkdir -p "$OUT"
PROG="${NM_PROGRAM:-present_hero}"
SHOT="$OUT/${PROG}.tdshot.png"
READY="$OUT/_present_ready.txt"
WAIT="${NM_WAIT:-480}"

python3 "$REPO/td/build_present_gui_toe.py" >/dev/null || { echo "FAIL: build_present_gui_toe.py"; exit 1; }

pkill -f 'MacOS/TouchDesigner' 2>/dev/null; sleep 1
rm -f "$READY" "$OUT/_present_log.txt" "$SHOT"

NM_PROGRAM="$PROG" NM_SIZE="${NM_SIZE:-1024}" NM_FRAMES="${NM_FRAMES:-1800}" \
  NM_SAMPLES="${NM_SAMPLES:-600,1200,1800}" NM_TIMESTEP="${NM_TIMESTEP:-0.0016667}" NM_TIME="${NM_TIME:-0.25}" \
  "$TD" "$REPO/td/nm_present.toe" >/dev/null 2>&1 &
TDPID=$!
echo "TD launched (pid $TDPID) — building + evolving ${NM_FRAMES:-1800} frames; waiting up to ${WAIT}s for READY…"

ok=0
for i in $(seq 1 "$WAIT"); do
  [ -f "$READY" ] && { ok=1; break; }
  kill -0 "$TDPID" 2>/dev/null || { echo "TD exited early — see log:"; sed 's/^/  /' "$OUT/_present_log.txt" 2>/dev/null; exit 1; }
  sleep 1
done
[ "$ok" = 1 ] || { echo "timed out waiting for READY ; log:"; sed 's/^/  /' "$OUT/_present_log.txt" 2>/dev/null; kill "$TDPID" 2>/dev/null; exit 1; }
echo "READY: $(cat "$READY")"

# bring TD to the front, give the viewer a beat to paint, then grab the screen.
osascript -e 'tell application "TouchDesigner" to activate' >/dev/null 2>&1
sleep 2
screencapture -x "$SHOT" 2>/dev/null

if [ -f "$SHOT" ] && "$PY" - "$SHOT" <<'PY'
import sys, numpy as np
from PIL import Image
a = np.asarray(Image.open(sys.argv[1]).convert('RGB')).astype('float32')
sys.exit(0 if a.std() > 3 else 1)
PY
then
  echo "CAPTURED: $SHOT"
  kill "$TDPID" 2>/dev/null; pkill -f 'MacOS/TouchDesigner' 2>/dev/null
  exit 0
else
  echo "screencapture failed or BLACK (Screen Recording permission for Terminal.app not active)."
  echo "TD is LEFT OPEN showing the 30s-evolved flagship — grab it with Cmd+Shift+4, Space, click the window,"
  echo "or grant Terminal Screen Recording (System Settings -> Privacy & Security -> Screen Recording) and re-run."
  exit 2
fi
