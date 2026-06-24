#!/usr/bin/env python3
"""build_present_gui_toe.py — author td/nm_present.toe: the in-app presentation harness.

Identical mechanics to build_evolve_toe.py (Execute DAT with onStart + per-frame callbacks), but it
execs td/present_gui.py, whose evolve_frame_end sets up a clean view and writes a READY marker
instead of quitting — so TD stays open displaying the 30s-evolved flagship for a screencapture.

Run with stock python3 (no TD needed to BUILD; TD is needed to RUN):
    python3 td/build_present_gui_toe.py
"""
import glob
import os
import shutil
import struct
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TD_DIR = os.path.join(REPO, 'td')
DRIVER = os.path.join(TD_DIR, 'present_gui.py')
OUT_TOE = os.path.join(TD_DIR, 'nm_present.toe')

TD_APP = os.environ.get('TD_APP') or next(iter(sorted(glob.glob('/Applications/TouchDesigner*.app'))), None)
if not TD_APP:
    sys.exit('TouchDesigner.app not found under /Applications (set TD_APP).')
TD_BIN_DIR = os.path.join(TD_APP, 'Contents', 'MacOS')
NEWPROJ = os.path.join(TD_APP, 'Contents', 'Resources', 'tfs', 'Samples', 'Setup', 'Base', 'NewProject.toe')
WORK = os.path.join(os.environ.get('TMPDIR', '/tmp'), 'nm_present_build')

CALLBACK = '''# noisemaker present bootstrap — non-real-time playback, then set up view + READY (no quit).
import traceback as _tb
_P = %r
_NS = {}
def _load():
    g = {'__file__': _P, '__name__': '__nm_present__'}
    for _n in ('op','ops','project','parent','me','root','var','mod','debug','tdu','ui',
               'baseCOMP','glslTOP','glslmultiTOP','nullTOP','textDAT','feedbackTOP',
               'constantTOP','selectTOP','renderTOP','geometryCOMP','glslmaterialMAT',
               'cameraCOMP','lightCOMP','sopto','rectangleSOP','gridSOP','tableDAT'):
        try:
            g[_n] = eval(_n)
        except Exception:
            pass
    exec(compile(open(_P).read(), _P, 'exec'), g)
    _NS.clear(); _NS.update(g)
def onStart():
    if _NS.get('_setup_done'):
        return
    _NS['_setup_done'] = True
    try:
        _load(); _NS['evolve_setup']()
    except Exception:
        print('[present] onStart FAIL', _tb.format_exc())
def onFrameStart(frame):
    fn = _NS.get('evolve_frame_start')
    if fn:
        try:
            fn(frame)
        except Exception:
            print('[present] frameStart', _tb.format_exc())
def onFrameEnd(frame):
    fn = _NS.get('evolve_frame_end')
    if fn:
        try:
            fn(frame)
        except Exception:
            print('[present] frameEnd', _tb.format_exc())
''' % DRIVER


def _toe_text(code):
    """Frame DAT text as TD stores it: '2\\n*' + 6 BE int32 [1,1,1,1,2,len] + utf8 body."""
    b = code.encode('utf-8')
    return b'2\n*' + struct.pack('>6i', 1, 1, 1, 1, 2, len(b)) + b


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    shutil.copy(NEWPROJ, os.path.join(WORK, 'boot.toe'))
    subprocess.run([os.path.join(TD_BIN_DIR, 'toeexpand'), 'boot.toe'], cwd=WORK,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    dd = os.path.join(WORK, 'boot.toe.dir')
    if not os.path.isdir(dd):
        sys.exit('toeexpand failed')

    proj = os.path.join(dd, 'project1')
    os.makedirs(proj, exist_ok=True)
    name = 'nm_present_exec'
    with open(os.path.join(proj, name + '.n'), 'w') as f:
        f.write('DAT:execute\ntile 200 200 130 90\nflags =  viewer 1 parlanguage 0\n'
                'color 0.55 0.55 0.55 \nview 8 0 1 1 1 0 0 0 0 1 1\nend\n')
    with open(os.path.join(proj, name + '.parm'), 'w') as f:
        f.write('?\nstart 0 on\nframestart 0 on\nframeend 0 on\nlanguage 0 python\n?\n')
    with open(os.path.join(proj, name + '.text'), 'wb') as f:
        f.write(_toe_text(CALLBACK))

    toc = os.path.join(WORK, 'boot.toe.toc')
    lines = open(toc).read().splitlines()
    entries = ['project1/%s.%s' % (name, ext) for ext in ('n', 'parm', 'text')]
    out, done = [], False
    for ln in lines:
        out.append(ln)
        if ln.strip() in ('project1.panel', 'project1.parm') and not done:
            out.extend(entries)
            done = True
    open(toc, 'w').write('\n'.join(out) + '\n')

    if os.path.exists(OUT_TOE):
        os.remove(OUT_TOE)
    subprocess.run([os.path.join(TD_BIN_DIR, 'toecollapse'), 'boot.toe'], cwd=WORK,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not os.path.exists(os.path.join(WORK, 'boot.toe')):
        sys.exit('toecollapse failed')
    shutil.copy(os.path.join(WORK, 'boot.toe'), OUT_TOE)
    print('wrote %s (%d bytes)' % (OUT_TOE, os.path.getsize(OUT_TOE)))


if __name__ == '__main__':
    main()
