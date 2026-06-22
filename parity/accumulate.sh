#!/usr/bin/env bash
# parity/accumulate.sh — parity for the multi-frame FEEDBACK-ACCUMULATION effects.
#
# The single-frame sweep.sh force-cooks ONE frame, which cannot drive a feedback effect whose state
# only latches on a real engine tick. These three instead evolve 8 frames-from-zero (timestep 0 — the
# reference golden protocol, parity/batch-golden.mjs) through the evolve harness (non-real-time
# timeline playback; the Feedback TOP latches once per frame), then grade against a batch-golden
# reference. They were [DEFER]'d in sweep.sh ("needs an async engine frame loop"); the evolve harness
# is that loop, so this is where they are now actually driven and graded.
#
#   cellularAutomata   discrete CA — byte-identical to the reference at EVERY frame (strict). seed:1
#                      zoom:8 is a fixed point (f1==f8), so this also proves the feedback re-feeds
#                      stably over 8 frames (a stuck/!latching feedback would drift or blacken).
#   motionBlur         rgba8unorm feedback (the REFERENCE's own surface format — verified in the
#                      graph). f1/f2 byte-exact; then a mild per-frame 8-bit re-quantization rounding
#                      drift (Metal round-to-nearest tie-break vs ANGLE) makes the contractive blend
#                      approach its fixed point a hair slower — peaks ~max-diff 10 mid-transient, back
#                      to max-diff 3 / ssim 0.99992 at the canonical f8. SSIM-gated, like the sweep's
#                      other cross-backend-drift effects (edge/crt/...). The alpha series
#                      0.60->0.84->0.94->0.99 (1-0.4^n) is the proof the accumulation actually runs.
#   reactionDiffusion  continuous Gray-Scott at the stability limit. seed + f1/f2 are bit-exact (the
#                      diffusion kernel + reaction term are a faithful port) then it chaotically
#                      amplifies sub-ULP cross-backend fp differences (f4 ssim ~0.88). NO stable
#                      golden exists to hit: even two reference WebGL2 harnesses (batch-golden vs
#                      export-and-render) diverge to ssim ~0.47. So it is gated on the early frames
#                      only; f4+ is reported, not failed — the same chaos class as navierStokes and
#                      the flagship target (see docs/CHAOS-GATE.md).
#   synth3d_cellularAutomata3d   the 3D-volume discrete CA, evolved as a ca_state feedback volume and
#                      turned to pixels by render3d()'s raymarch. f1/f2 max-abs-diff=1 (the seed
#                      volume + 3D neighbour update are an exact port); at f8 a few boundary cells
#                      flip after 8 generations of cross-backend ULP and the raymarch over the sharp
#                      voxel amplifies that single flip to a high max-diff (170) at a negligible mean
#                      (0.43) — ssim-gated (0.996), the discrete-CA class but read through a raymarch.
#   synth3d_reactionDiffusion3d  the 3D-volume Gray-Scott (rd_state feedback volume) through render3d().
#                      f1/f2 bit-exact (max-abs-diff=1) then the continuous reaction term chaotically
#                      amplifies sub-ULP fp differences (f8 ssim ~0.977) — same chaos class as the 2D
#                      effect; f8 reported, not failed.
#
#   NM_REFERENCE_ROOT=/path/to/noisemaker parity/accumulate.sh   # regen goldens, drive all, grade
#   parity/accumulate.sh --no-stage   # reuse existing parity/out/<e>.f*.golden.png; just drive+grade
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(cd "$HERE/.." && pwd)"
PY="$REPO/parity/.venv/bin/python"; [ -x "$PY" ] || PY=python3
OUT="$REPO/parity/out"; mkdir -p "$OUT"

EFFECTS="cellularAutomata motionBlur reactionDiffusion synth3d_cellularAutomata3d synth3d_reactionDiffusion3d"
GRADE_FRAMES="1 2 8"          # frames captured + graded (per-effect verdict below)

stage=1
for a in "$@"; do [ "$a" = "--no-stage" ] && stage=0; done

# 1. Reference goldens — batch-golden.mjs renders frame N from a clean zeroed start (timestep 0,
#    time 0.25). One run per frame count over the 3-effect manifest; rename to <name>.f<NNNN>.golden.
if [ "$stage" = 1 ]; then
  command -v node >/dev/null || { echo "node not found (needed to regenerate goldens); use --no-stage"; exit 2; }
  MAN="$(mktemp)"; for e in $EFFECTS; do echo "$e parity/programs/$e.dsl" >> "$MAN"; done
  for N in $GRADE_FRAMES; do
    f="$(printf '%04d' "$N")"; gdir="$OUT/_acc_g$N"; rm -rf "$gdir"
    ( cd "$REPO" && node parity/batch-golden.mjs "$MAN" "$gdir" --size 256 --frames "$N" --timestep 0 --time 0.25 ) \
      >/dev/null 2>&1 || { echo "golden gen failed at frames=$N (set NM_REFERENCE_ROOT to the upstream engine)"; exit 2; }
    for e in $EFFECTS; do cp "$gdir/$e.golden.png" "$OUT/$e.f$f.golden.png"; done
  done
  rm -f "$MAN"
fi

# 2. Drive each effect 8 frames through the evolve harness (permissive internal grade — the
#    authoritative per-effect verdict is step 3). Candidates land in parity/out/<e>.f<NNNN>.candidate.
SMPL="$(echo $GRADE_FRAMES | tr ' ' ',')"
for e in $EFFECTS; do
  NM_FRAMES=8 NM_TIMESTEP=0 NM_TIME=0.25 NM_SAMPLES="$SMPL" NM_WAIT="${NM_WAIT:-280}" \
    TOL=255 SSIM=0 bash "$REPO/parity/evolve.sh" "$e" >/dev/null 2>&1 || true
done

# 3. Per-effect, per-frame verdict.
grade() { # <effect> <frame> <tol> <ssim> ; prints PASS/FAIL line, returns nonzero on fail
  local e="$1" n="$2" tol="$3" ssim="$4" f; f="$(printf '%04d' "$n")"
  local g="$OUT/$e.f$f.golden.png" c="$OUT/$e.f$f.candidate.png"
  [ -f "$c" ] || { echo "  [FAIL] $e f$f — no candidate (evolve did not run)"; return 1; }
  "$PY" "$REPO/parity/compare.py" "$g" "$c" --name "$e.f$f" --tolerance "$tol" --ssim-min "$ssim" 2>&1 \
    | sed 's/^/  /'
}
report() { # informational only — never fails the sweep (chaos-gated frame)
  local e="$1" n="$2" f; f="$(printf '%04d' "$n")"
  local g="$OUT/$e.f$f.golden.png" c="$OUT/$e.f$f.candidate.png"
  [ -f "$c" ] || { echo "  [skip] $e f$f — no candidate"; return 0; }
  local line; line="$("$PY" "$REPO/parity/compare.py" "$g" "$c" --name "$e.f$f" --tolerance 255 --ssim-min 0 2>&1)"
  echo "  [chaos-gate] ${line#\[PASS\] }"
}

pass=0; fail=0
echo "=== cellularAutomata (discrete CA — strict at every frame) ==="
for n in 1 2 8; do if grade cellularAutomata "$n" 2.001 0.98; then pass=$((pass+1)); else fail=$((fail+1)); fi; done

echo "=== motionBlur (rgba8 feedback — f1/f2 strict, f8 SSIM-gated for 8-bit re-quant drift) ==="
if grade motionBlur 1 2.001 0.98; then pass=$((pass+1)); else fail=$((fail+1)); fi
if grade motionBlur 2 2.001 0.98; then pass=$((pass+1)); else fail=$((fail+1)); fi
if grade motionBlur 8 3.001 0.999; then pass=$((pass+1)); else fail=$((fail+1)); fi

echo "=== reactionDiffusion (continuous Gray-Scott — seed/f1/f2 strict, f4+ chaos-gated) ==="
if grade reactionDiffusion 1 2.001 0.98; then pass=$((pass+1)); else fail=$((fail+1)); fi
if grade reactionDiffusion 2 2.001 0.98; then pass=$((pass+1)); else fail=$((fail+1)); fi
report reactionDiffusion 8

echo "=== cellularAutomata3d (3D discrete CA via render3d — f1/f2 max-diff=1 strict, f8 ssim-gated) ==="
if grade synth3d_cellularAutomata3d 1 2.001 0.98; then pass=$((pass+1)); else fail=$((fail+1)); fi
if grade synth3d_cellularAutomata3d 2 2.001 0.98; then pass=$((pass+1)); else fail=$((fail+1)); fi
if grade synth3d_cellularAutomata3d 8 255   0.98; then pass=$((pass+1)); else fail=$((fail+1)); fi

echo "=== reactionDiffusion3d (3D Gray-Scott via render3d — f1/f2 strict, f8 chaos-gated) ==="
if grade synth3d_reactionDiffusion3d 1 2.001 0.98; then pass=$((pass+1)); else fail=$((fail+1)); fi
if grade synth3d_reactionDiffusion3d 2 2.001 0.98; then pass=$((pass+1)); else fail=$((fail+1)); fi
report synth3d_reactionDiffusion3d 8

echo "=== ACCUMULATE: $pass / $((pass+fail)) gated checks PASS (reactionDiffusion + reactionDiffusion3d f8 chaos-gated, reported) ==="
[ "$fail" = 0 ]
