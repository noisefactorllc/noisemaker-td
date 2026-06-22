#!/usr/bin/env python3
"""present_compose.py — compose a DSL program + its rendered canvas into one presentation PNG.

    parity/.venv/bin/python parity/present_compose.py <prog> [frameSamplePNG] [out.png]

Reads parity/programs/<prog>.dsl + the canvas PNG (default parity/out/<prog>.f1800.candidate.png),
draws the DSL source in a monospace panel on the left and the canvas on the right, and writes a
single side-by-side image (default parity/out/<prog>.present.png).
"""
import os
import sys
from PIL import Image, ImageDraw, ImageFont

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _font(size):
    for p in ('/System/Library/Fonts/Menlo.ttc', '/System/Library/Fonts/Monaco.ttf',
              '/System/Library/Fonts/Courier.dfont', '/Library/Fonts/Courier New.ttf'):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def main():
    prog = sys.argv[1] if len(sys.argv) > 1 else 'present_hero'
    canvas_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(REPO, 'parity', 'out', '%s.f1800.candidate.png' % prog)
    out_path = sys.argv[3] if len(sys.argv) > 3 else os.path.join(REPO, 'parity', 'out', '%s.present.png' % prog)

    dsl = open(os.path.join(REPO, 'parity', 'programs', '%s.dsl' % prog)).read().rstrip('\n').split('\n')
    canvas = Image.open(canvas_path).convert('RGB')

    BG = (18, 18, 22)
    FG = (220, 222, 228)
    DIM = (120, 124, 135)
    ACCENT = (255, 150, 90)
    fsz, lh = 14, 19
    font = _font(fsz)
    title_font = _font(20)
    label_font = _font(15)

    # left panel: DSL text
    pad = 28
    text_w = max((font.getlength(ln) for ln in dsl), default=400)
    panel_w = int(text_w) + pad * 2
    panel_h = len(dsl) * lh + pad * 2 + 40        # +40 for the title row

    # right panel: canvas scaled square to ~ the DSL height (capped), min 512
    disp = max(512, min(panel_h - pad * 2 - 40, 900))
    canvas_disp = canvas.resize((disp, disp), Image.LANCZOS)

    H = max(panel_h, disp + pad * 2 + 40)
    W = panel_w + disp + pad * 3
    img = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(img)

    # titles
    d.text((pad, pad - 6), 'DSL program', font=title_font, fill=ACCENT)
    d.text((panel_w + pad * 2, pad - 6), 'TouchDesigner canvas · o1 @ 30s', font=title_font, fill=ACCENT)

    # DSL body
    y = pad + 40
    for ln in dsl:
        col = DIM if (ln.strip().startswith('search') or ln.strip().startswith('//')) else FG
        d.text((pad, y), ln, font=font, fill=col)
        y += lh

    # canvas + frame
    cx, cy = panel_w + pad * 2, pad + 40
    img.paste(canvas_disp, (cx, cy))
    d.rectangle([cx - 1, cy - 1, cx + disp, cy + disp], outline=(60, 62, 70), width=1)
    d.text((cx, cy + disp + 8), 'noisemaker-td · live compiler · 1800 frames (30s) · stateSize x1024 + navierStokes',
           font=label_font, fill=DIM)

    img.save(out_path)
    print('wrote %s (%dx%d)' % (out_path, W, H))


if __name__ == '__main__':
    main()
