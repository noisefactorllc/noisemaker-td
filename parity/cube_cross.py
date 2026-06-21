#!/usr/bin/env python3
"""cube_cross.py — assemble 6 baked cube faces into a horizontal-cross PNG.

Mirrors the reference `renderer/cubeExport.js` crossLayout: a 4-col x 3-row grid where each face
(GL order +X,-X,+Y,-Y,+Z,-Z) lands in the cell that makes adjacent faces share cube edges —

           [+Y]
      [+X] [+Z] [-X] [-Z]
           [-Y]

Faces are <prefix>.face{0..5}.<suffix>.png; writes <prefix>.cross.<suffix>.png.

    parity/.venv/bin/python parity/cube_cross.py parity/out/<prog> candidate
    parity/.venv/bin/python parity/cube_cross.py parity/out/<prog> golden
"""
import sys
import numpy as np
from PIL import Image

# cell (col, row) per face index — identical to cubeExport.CROSS_CELL.
CROSS_CELL = [(0, 1), (2, 1), (1, 0), (1, 2), (1, 1), (3, 1)]


def main():
    if len(sys.argv) != 3:
        sys.exit('usage: cube_cross.py <prefix> <suffix>   (reads <prefix>.face{0..5}.<suffix>.png)')
    prefix, suffix = sys.argv[1], sys.argv[2]
    faces = [np.asarray(Image.open('%s.face%d.%s.png' % (prefix, f, suffix)).convert('RGBA'))
             for f in range(6)]
    size = faces[0].shape[0]
    cross = np.zeros((size * 3, size * 4, 4), dtype=np.uint8)
    for f, (cx, cy) in enumerate(CROSS_CELL):
        cross[cy * size:(cy + 1) * size, cx * size:(cx + 1) * size] = faces[f]
    out = '%s.cross.%s.png' % (prefix, suffix)
    Image.fromarray(cross, 'RGBA').save(out)
    print('wrote %s (%dx%d)' % (out, size * 4, size * 3))


if __name__ == '__main__':
    main()
