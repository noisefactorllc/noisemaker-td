#!/usr/bin/env python3
"""build_points_probe_toe.py — author td/nm_points_probe.toe: the isolated point-scatter probe.

Same .toe-authoring mechanism as build_evolve_toe.py (toeexpand -> inject an Execute DAT ->
toecollapse), but the Execute DAT only enables onStart, which exec's td/points_probe.py and calls
probe_main() (build the tiny scatter network, render both branches, log the known-answer verdict,
quit). Build with stock python3; TD is needed only to RUN.

    python3 td/build_points_probe_toe.py
"""
import glob
import os
import shutil
import struct
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TD_DIR = os.path.join(REPO, 'td')
PROBE = os.path.join(TD_DIR, 'points_probe.py')
OUT_TOE = os.path.join(TD_DIR, 'nm_points_probe.toe')

TD_APP = os.environ.get('TD_APP') or next(iter(sorted(glob.glob('/Applications/TouchDesigner*.app'))), None)
if not TD_APP:
    sys.exit('TouchDesigner.app not found under /Applications (set TD_APP).')
TD_BIN_DIR = os.path.join(TD_APP, 'Contents', 'MacOS')
NEWPROJ = os.path.join(TD_APP, 'Contents', 'Resources', 'tfs', 'Samples', 'Setup', 'Base', 'NewProject.toe')
WORK = os.path.join(os.environ.get('TMPDIR', '/tmp'), 'nm_points_probe_build')

CALLBACK = '''# noisemaker points-probe bootstrap — build scatter network, render, verify, quit.
import traceback as _tb
_P = %r
_NS = {}
def onStart():
    if _NS.get('_done'):
        return
    _NS['_done'] = True
    g = {'__file__': _P, '__name__': '__nm_probe__'}
    for _n in ('op','ops','project','parent','me','root','var','mod','debug','tdu'):
        try:
            g[_n] = eval(_n)
        except Exception:
            pass
    try:
        exec(compile(open(_P).read(), _P, 'exec'), g)
        _NS.update(g)
        g['probe_main']()
    except Exception:
        print('[probe] onStart FAIL', _tb.format_exc())
        try:
            project.quit(force=True)
        except Exception:
            pass
''' % PROBE


def _toe_text(code):
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
    name = 'nm_probe_exec'
    with open(os.path.join(proj, name + '.n'), 'w') as f:
        f.write('DAT:execute\ntile 200 200 130 90\nflags =  viewer 1 parlanguage 0\n'
                'color 0.55 0.55 0.55 \nview 8 0 1 1 1 0 0 0 0 1 1\nend\n')
    with open(os.path.join(proj, name + '.parm'), 'w') as f:
        f.write('?\nstart 0 on\nlanguage 0 python\n?\n')
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
