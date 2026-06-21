#!/usr/bin/env bash
# parity/cubemap.sh <prog> — 6-FACE CUBEMAP BAKE parity.
#
#   parity/cubemap.sh                              # synth3d_renderCubemapSurface
#   parity/cubemap.sh synth3d_renderCubemap3d      # the lit-blob renderer
#   SIZE=256 parity/cubemap.sh <prog>
#
# A full cubemap is HOST-DRIVEN: render the same graph 6x, setting cubeBasis to each GL face
# basis (+X,-X,+Y,-Y,+Z,-Z) between renders. This drives BOTH sides of that loop and grades:
#   1. reference 6 faces  — export-and-render.mjs --cubemap (pipeline.renderCubemap)
#   2. TD 6 faces         — td/cubemap_bake.py rebinds cubeBasis per face (uniform_binder Matrices)
#   3. assemble both into the canonical horizontal cross (cube_cross.py) and compare
# Each face is a deterministic single render (like the single-face test), so the bar is max-diff<=1.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(cd "$HERE/.." && pwd)"
REF="${NM_REFERENCE_ROOT:-}"
[ -n "$REF" ] && [ -d "$REF/shaders" ] || { echo "set NM_REFERENCE_ROOT to the upstream Noisemaker engine (tree with shaders/)"; exit 2; }
TD="${TD_BIN:-/Applications/TouchDesigner.app/Contents/MacOS/TouchDesigner}"
PY="$REPO/parity/.venv/bin/python"; [ -x "$PY" ] || PY=python3
PROG="${1:-synth3d_renderCubemapSurface}"
SIZE="${SIZE:-256}"
OUT="$REPO/parity/out"; mkdir -p "$OUT"
DSL="$REPO/parity/programs/$PROG.dsl"; [ -f "$DSL" ] || DSL="$REPO/parity/corpus/$PROG.dsl"
[ -f "$DSL" ] || { echo "no DSL for $PROG"; exit 2; }

# 1. reference 6 faces
rm -f "$OUT/$PROG".face*.golden.png "$OUT/$PROG".face*.candidate.png
NM_REFERENCE_ROOT="$REF" node "$REPO/parity/export-and-render.mjs" "$DSL" "$OUT" \
  --size "$SIZE" --cubemap >/dev/null 2>&1 || { echo "FAIL: reference cubemap render"; exit 1; }

# 2. TD bake (env-parametrized probe builder authors the .toe; it execs td/cubemap_bake.py)
NM_PROBE_FILE="$REPO/td/cubemap_bake.py" NM_PROBE_TOE="$REPO/td/nm_cubemap_bake.toe" \
  python3 "$REPO/td/build_points_probe_toe.py" >/dev/null || { echo "FAIL: build bake .toe"; exit 1; }
rm -f "$OUT/_cubemap_log.txt"
NM_PROGRAM="$PROG" NM_SIZE="$SIZE" "$TD" "$REPO/td/nm_cubemap_bake.toe" >/dev/null 2>&1 &
PID=$!
for i in $(seq 1 "${NM_WAIT:-200}"); do
  grep -qE 'BAKE DONE|abort|FAIL' "$OUT/_cubemap_log.txt" 2>/dev/null && break
  kill -0 "$PID" 2>/dev/null || break
  sleep 1
done
sleep 1; kill "$PID" 2>/dev/null; pkill -f 'MacOS/TouchDesigner' 2>/dev/null
if ! grep -q 'BAKE DONE' "$OUT/_cubemap_log.txt" 2>/dev/null; then
  echo "FAIL: TD bake did not finish"; sed 's/^/  /' "$OUT/_cubemap_log.txt" 2>/dev/null; exit 1
fi

# 3. assemble crosses
"$PY" "$REPO/parity/cube_cross.py" "$OUT/$PROG" candidate >/dev/null
"$PY" "$REPO/parity/cube_cross.py" "$OUT/$PROG" golden >/dev/null

# 4. grade 6 faces + the cross (deterministic single renders -> max-diff<=1)
rc=0
for k in 0 1 2 3 4 5; do
  "$PY" "$REPO/parity/compare.py" "$OUT/$PROG.face$k.golden.png" "$OUT/$PROG.face$k.candidate.png" \
    --name "$PROG.face$k" --tolerance "${TOL:-2}" --ssim-min "${SSIM:-0.98}" || rc=1
done
"$PY" "$REPO/parity/compare.py" "$OUT/$PROG.cross.golden.png" "$OUT/$PROG.cross.candidate.png" \
  --name "$PROG.cross" --tolerance "${TOL:-2}" --ssim-min "${SSIM:-0.98}" || rc=1
exit $rc
