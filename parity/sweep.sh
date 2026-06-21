#!/usr/bin/env bash
# parity/sweep.sh — full parity sweep: render every staged program in TouchDesigner, then compare
# each against its reference golden with a PER-EFFECT tolerance. Effects with hard discontinuities
# (fractal root basins, step() thresholds, df64 ULP, NEAREST coord tie-breaks) cannot be bit-exact
# cross-device (MoltenVK/Metal vs ANGLE/WebGL2), so they are gated on structural SSIM — mirrors the
# sibling noisemaker-godot sweep, with TD-measured pixel counts in the comments.
#
#   parity/sweep.sh                 # stage (from ../noisemaker-godot) + render all + compare
#   parity/sweep.sh --no-stage      # skip re-staging; use the DSLs/goldens already in out/
#   parity/sweep.sh --compare-only  # don't render; re-grade the existing candidates
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(cd "$HERE/.." && pwd)"
PY="$REPO/parity/.venv/bin/python"; [ -x "$PY" ] || PY=python3
OUT="$REPO/parity/out"; CHUNK="${CHUNK:-15}"

# Per-effect tolerance: "<max-abs-diff/255> <ssim-min>". Default 2.001 is the epsilon-tolerant
# form of "<= 2" (compare.py's float round-trip reads an exact 2.0 as 2.0000001). SSIM stays 0.98.
tol_for() { case "$1" in
  newton)     echo "255 0.98" ;;  # Newton-fractal root basins = Julia set; df64 ULP across Metal/ANGLE (60 px)
  shadow)     echo "255 0.99" ;;  # step(threshold) flips fg<->shadow where mask ~= threshold (95 px); SSIM-gated
  uvRemap)    echo "22 0.98" ;;   # NEAREST coord-resampling tie-breaks on exact texel boundaries (30 px, 0.05%)
  distortion) echo "12 0.98" ;;   # Sobel-over-noise + NEAREST coord boundary amplifies +/-1 drift (7 px, 0.01%)
  edge)       echo "8 0.98" ;;    # x2 contrast convolution amplifies upstream 1-LSB noise (11 px, <0.1%)
  crt)        echo "3 0.98" ;;    # transcendental cos/pow seam flips one texel index (max diff exactly 2)
  *)          echo "2.001 0.98" ;;
esac; }

stage=1; render=1
for a in "$@"; do case "$a" in
  --no-stage)     stage=0 ;;
  --compare-only) stage=0; render=0 ;;
esac; done

[ "$stage" = 1 ] && "$PY" "$REPO/parity/stage_coverage.py" >/dev/null
SET="$(cat "$OUT/_render_set.txt" 2>/dev/null || true)"
[ -n "$SET" ] || { echo "no render set — run parity/stage_coverage.py first"; exit 1; }

# 1. render all candidates (chunked TD sessions). Permissive tol so the render step always emits a
#    candidate; the authoritative verdict is the per-effect compare in step 2.
if [ "$render" = 1 ]; then
  read -ra ALL <<< "$SET"; n=${#ALL[@]}; i=0
  while [ $i -lt $n ]; do
    TOL=255 SSIM=0 bash "$REPO/parity/run.sh" "${ALL[*]:i:CHUNK}" >/dev/null 2>&1 || true
    i=$((i + CHUNK))
  done
fi

# 2. per-effect compare with the tolerance table.
pass=0; fail=0; failed=""
for name in $SET; do
  [ -f "$OUT/$name.golden.png" ] || continue
  if [ ! -f "$OUT/$name.candidate.png" ]; then
    echo "[FAIL] $name (no candidate)"; fail=$((fail + 1)); failed="$failed $name"; continue
  fi
  read -r TOL SSIM <<< "$(tol_for "$name")"
  if "$PY" "$REPO/parity/compare.py" "$OUT/$name.golden.png" "$OUT/$name.candidate.png" \
       --name "$name" --tolerance "$TOL" --ssim-min "$SSIM"; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1)); failed="$failed $name"
  fi
done
echo "=== SWEEP: $pass / $((pass + fail)) PASS${failed:+  — FAILED:$failed} ==="
[ -z "$failed" ]
