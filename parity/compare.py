#!/usr/bin/env python3
"""Compare two PNGs for the noisemaker-hlsl parity harness.

Loads a GOLDEN reference PNG (from parity/export-and-render.mjs) and a CANDIDATE
PNG (from the Unity NMParityRunner), computes max-abs-diff and a simple global
SSIM, and fails if either exceeds a per-program tolerance. Mirrors the style and
tolerance conventions of ../../scripts/image_regression.py (argparse CLI, numpy,
PIL, max-abs-diff gate) and emits a small JSON report.

Both PNGs are expected to be the SAME size and the SAME orientation (top-down)
and SAME encoding (linear 8-bit, NOT sRGB) — the renderers are responsible for
producing matching orientation/encoding (the Y-flip reconciliation point).

CLI:
  python compare.py golden.png candidate.png [--tolerance 2] [--ssim-min 0.98] \
         [--report out.json] [--name synth/noise]

Exit code 0 = within tolerance, 1 = divergence, 2 = bad input.
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image


def load_rgba(path: Path) -> np.ndarray:
    """Load a PNG as a float32 HxWx4 array in [0,1]."""
    img = Image.open(path).convert("RGBA")
    return np.asarray(img, dtype=np.float32) / 255.0


def max_abs_diff(a: np.ndarray, b: np.ndarray) -> float:
    """Max absolute per-channel difference in 8-bit units (0..255)."""
    return float(np.max(np.abs(a - b)) * 255.0)


def mean_abs_diff(a: np.ndarray, b: np.ndarray) -> float:
    """Mean absolute per-channel difference in 8-bit units (0..255)."""
    return float(np.mean(np.abs(a - b)) * 255.0)


def global_ssim(a: np.ndarray, b: np.ndarray) -> float:
    """Simple global SSIM over luminance (single-window, no Gaussian).

    Adequate as a structural gate for parity (we already have a strict per-pixel
    max-abs gate); kept dependency-free (no skimage) to match the repo's minimal
    Python toolchain.
    """
    # Rec. 601 luma, matching the harness's luma weighting.
    def luma(x):
        return 0.299 * x[..., 0] + 0.587 * x[..., 1] + 0.114 * x[..., 2]

    la, lb = luma(a).ravel(), luma(b).ravel()
    mu_a, mu_b = la.mean(), lb.mean()
    var_a, var_b = la.var(), lb.var()
    cov = ((la - mu_a) * (lb - mu_b)).mean()
    c1 = (0.01) ** 2
    c2 = (0.03) ** 2
    num = (2 * mu_a * mu_b + c1) * (2 * cov + c2)
    den = (mu_a ** 2 + mu_b ** 2 + c1) * (var_a + var_b + c2)
    return float(num / den) if den != 0 else 1.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("golden", type=Path, help="Reference golden PNG")
    parser.add_argument("candidate", type=Path, help="Unity-rendered candidate PNG")
    parser.add_argument("--name", default=None, help="Program name for the report")
    # Default 2/255 mirrors a tight cross-backend tolerance; loosen per-program
    # for stochastic/feedback effects via --tolerance.
    parser.add_argument("--tolerance", type=float, default=2.0,
                        help="Max allowed max-abs-diff in 8-bit units (default 2)")
    parser.add_argument("--ssim-min", type=float, default=0.98,
                        help="Minimum acceptable global SSIM (default 0.98)")
    parser.add_argument("--report", type=Path, default=None, help="Write JSON report here")
    args = parser.parse_args()

    if not args.golden.exists():
        print(f"error: golden not found: {args.golden}", file=sys.stderr)
        return 2
    if not args.candidate.exists():
        print(f"error: candidate not found: {args.candidate}", file=sys.stderr)
        return 2

    a = load_rgba(args.golden)
    b = load_rgba(args.candidate)

    if a.shape != b.shape:
        print(f"error: size mismatch golden={a.shape} candidate={b.shape}", file=sys.stderr)
        if args.report:
            args.report.write_text(json.dumps({
                "name": args.name or args.golden.stem,
                "passed": False,
                "error": "size_mismatch",
                "golden_shape": list(a.shape),
                "candidate_shape": list(b.shape),
            }, indent=2) + "\n")
        return 1

    max_diff = max_abs_diff(a, b)
    mean_diff = mean_abs_diff(a, b)
    ssim = global_ssim(a, b)

    passed = (max_diff <= args.tolerance) and (ssim >= args.ssim_min)

    report = {
        "name": args.name or args.golden.stem,
        "golden": str(args.golden),
        "candidate": str(args.candidate),
        "shape": list(a.shape),
        "max_abs_diff": max_diff,
        "mean_abs_diff": mean_diff,
        "ssim": ssim,
        "tolerance": args.tolerance,
        "ssim_min": args.ssim_min,
        "passed": passed,
    }

    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {report['name']}: max-abs-diff={max_diff:.3f} "
          f"mean-abs-diff={mean_diff:.4f} ssim={ssim:.5f} "
          f"(tol={args.tolerance}, ssim_min={args.ssim_min})")

    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(f"wrote report {args.report}", file=sys.stderr)

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
