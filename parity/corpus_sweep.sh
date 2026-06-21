#!/usr/bin/env bash
# corpus_sweep.sh — LIVE blaster-corpus end-to-end parity (the DSL -> graph -> execute target).
#
# Renders every parity/corpus comp through TD's LIVE Polymorphic compiler (NM_LIVE_DSL: TD compiles
# the DSL itself, builds the TOP network, runs it) and grades vs a reference golden. This is the real
# parity target — not the per-effect sweep, but whole real programs people wrote in the DSL:
#
#   stateless  single-frame byte/SSIM parity (deterministic multi-effect programs)
#   agent      points/flow chaotic agent flows — evolve 8 frames-from-zero; they render faithfully
#              end-to-end but, like the flagship target.dsl, diverge from the WebGL2 golden the longer
#              they run (cross-rasterizer chaos gate — see docs/CHAOS-GATE.md). Gated on no-NaN +
#              SSIM at f8 (the pre-deep-chaos checkpoint).
#   stateful   navierStokes / reactionDiffusion / mnca / feedback — evolve 8 frames; same chaos gate.
#   skip-*     external-input (media/text/scope/...) or third-party/unknown effects — the reference
#              rejects those too (blaster occasionally references community effects not in the catalog).
#
# Self-contained: DSLs are in-repo; goldens render from NM_REFERENCE_ROOT (no default — no sibling
# assumed on clone). Needs TD license-activated.
#
#   NM_REFERENCE_ROOT=/path/to/noisemaker parity/corpus_sweep.sh
#   parity/corpus_sweep.sh --stateless   # only the fast deterministic byte/SSIM tier
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(cd "$HERE/.." && pwd)"
PY="$REPO/parity/.venv/bin/python"; [ -x "$PY" ] || PY=python3
OUT="$REPO/parity/out"; mkdir -p "$OUT"
REF="${NM_REFERENCE_ROOT:-}"
[ -n "$REF" ] && [ -d "$REF/shaders" ] || { echo "set NM_REFERENCE_ROOT to the upstream Noisemaker engine (tree with shaders/)"; exit 2; }
only_stateless=0; for a in "$@"; do [ "$a" = "--stateless" ] && only_stateless=1; done

# 1. classify
CLS="$("$PY" "$REPO/parity/corpus_classify.py")"
stateless="$(echo "$CLS" | awk -F'\t' '$2=="stateless"{print $1}')"
agentful="$(echo "$CLS" | awk -F'\t' '$2=="agent"||$2=="stateful"{print $1}')"
skipped="$(echo "$CLS" | awk -F'\t' '$2 ~ /^skip/{printf "%s(%s) ",$1,$2}')"
echo "=== classify: $(echo "$stateless"|wc -w|tr -d ' ') stateless, $(echo "$agentful"|wc -w|tr -d ' ') agent/stateful, skipped: ${skipped:-none} ==="

pass=0; fail=0; failed=""

# 2. stateless tier — single-frame, byte/SSIM parity
if [ -n "$stateless" ]; then
  MAN="$(mktemp)"; for n in $stateless; do echo "$n parity/corpus/$n.dsl" >> "$MAN"; done
  ( cd "$REPO" && node parity/batch-golden.mjs "$MAN" "$OUT" --size 256 --frames 1 --timestep 0 --time 0.25 ) >/dev/null 2>&1
  rm -f "$MAN"
  # SSIM-gated like the main sweep's discontinuity effects: faithful render = ssim >= 0.999 (covers
  # byte-clean AND ULP-gated like bRUa1g's oklch/tetraCosine); the per-line max-diff shows byte-clean.
  echo "--- stateless (ssim>=0.999; max-diff<=1 = byte-clean) ---"
  while read -r line; do
    case "$line" in *"[PASS]"*) pass=$((pass+1));; *"[FAIL]"*) fail=$((fail+1)); failed="$failed ${line#*] }";; esac
    echo "  $line"
  done < <(NM_LIVE_DSL=1 NM_REFERENCE_ROOT="$REF" TOL=255 SSIM=0.999 bash "$REPO/parity/run.sh" "$stateless" 2>&1 | grep -E '\[PASS\]|\[FAIL\]')
fi

# 3. agent/stateful tier — evolve 8 frames, chaos-gated (no-NaN + SSIM at f8)
if [ "$only_stateless" = 0 ] && [ -n "$agentful" ]; then
  echo "--- agent/stateful (evolve 8f, chaos-gated: no-NaN + ssim>=0.95 at f8) ---"
  for n in $agentful; do
    printf '%s parity/corpus/%s.dsl\n' "$n" "$n" > "$OUT/_cm.txt"
    ( cd "$REPO" && node parity/batch-golden.mjs "$OUT/_cm.txt" "$OUT" --size 256 --frames 8 --timestep 0 --time 0.25 ) >/dev/null 2>&1
    cp -f "$OUT/$n.golden.png" "$OUT/$n.f0008.golden.png" 2>/dev/null
    log="$(NM_FRAMES=8 NM_SAMPLES=8 NM_TIMESTEP=0 NM_TIME=0.25 NM_SIZE=256 NM_WAIT="${NM_WAIT:-220}" \
           TOL=255 SSIM=0 bash "$REPO/parity/evolve.sh" "$n" 2>&1)"
    nan="$(echo "$log" | grep -oE 'nan=[0-9]+' | head -1 | cut -d= -f2)"
    cand="$OUT/$n.f0008.candidate.png"
    if [ ! -f "$cand" ]; then echo "  [FAIL] $n (no candidate — did not render)"; fail=$((fail+1)); failed="$failed $n"; continue; fi
    ssim="$("$PY" "$REPO/parity/compare.py" "$OUT/$n.f0008.golden.png" "$cand" --name "$n" --tolerance 255 --ssim-min 0 2>&1 | grep -oE 'ssim=[0-9.]+' | head -1 | cut -d= -f2)"
    if [ "${nan:-1}" = "0" ] && awk "BEGIN{exit !(${ssim:-0}>=0.95)}"; then
      echo "  [PASS] $n: renders end-to-end, ssim=$ssim f8 (chaos-gated over time)"; pass=$((pass+1))
    else
      echo "  [FAIL] $n: nan=${nan:-?} ssim=${ssim:-?} f8"; fail=$((fail+1)); failed="$failed $n"
    fi
  done
  rm -f "$OUT/_cm.txt"
fi

echo "=== LIVE CORPUS: $pass / $((pass+fail)) render at parity-or-chaos-gate${failed:+  — FAILED:$failed} ==="
[ -z "$failed" ]
