#!/usr/bin/env python3
"""build_parity_toe.py — author the bootstrap `td/nm_parity.toe` used by parity/run.sh.

TouchDesigner has no headless startup-script hook and its `.toe` is a binary, so we build a
minimal project containing one Execute DAT (callbacks `onStart`/`onCreate`) by transplanting
into the stock NewProject.toe skeleton via `toeexpand`/`toecollapse` (TD's own tools). On
load the Execute DAT execs `td/parity_render_all.py` (which renders the Tier-1 candidates),
then quits.

Run with stock python3 (no TD needed to BUILD the .toe; TD is needed to RUN it):
    python3 td/build_parity_toe.py

Paths are derived from this file's location; the TouchDesigner app is found under /Applications
(override with the TD_APP env var). Nothing here is machine-specific in committed form — the
absolute repo path is baked only into the generated `.toe`, which is gitignored.
"""
import glob
import os
import shutil
import struct
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TD_DIR = os.path.join(REPO, 'td')
RENDER = os.path.join(TD_DIR, 'parity_render_all.py')
OUT_TOE = os.path.join(TD_DIR, 'nm_parity.toe')

TD_APP = os.environ.get('TD_APP') or next(iter(sorted(glob.glob('/Applications/TouchDesigner*.app'))), None)
if not TD_APP:
    sys.exit('TouchDesigner.app not found under /Applications (set TD_APP).')
TD_BIN_DIR = os.path.join(TD_APP, 'Contents', 'MacOS')
NEWPROJ = os.path.join(TD_APP, 'Contents', 'Resources', 'tfs', 'Samples', 'Setup', 'Base', 'NewProject.toe')
WORK = os.path.join(os.environ.get('TMPDIR', '/tmp'), 'nm_parity_build')

# The Execute DAT body: define onStart/onCreate, exec the render script with a namespace that
# carries the TD operator globals + __file__ (so the imported runtime resolves them and the
# script's own __file__-based repo lookup works), then quit.
CALLBACK = '''# noisemaker parity bootstrap — renders Tier-1 candidates on load, then quits.
def _run():
    p = %r
    g = {'__file__': p, '__name__': '__nm_parity__'}
    for _n in ('op','ops','project','parent','me','root','var','mod','debug','tdu',
               'baseCOMP','glslTOP','glslmultiTOP','nullTOP','textDAT','feedbackTOP'):
        try:
            g[_n] = eval(_n)
        except Exception:
            pass
    try:
        exec(compile(open(p).read(), p, 'exec'), g)
    finally:
        try:
            project.quit(force=True)
        except Exception:
            pass

def onStart():
    _run()

def onCreate():
    _run()
''' % RENDER


def _toe_text(code):
    """Frame DAT text as TD stores it: '2\\n*' + 6 BE int32 [1,1,1,1,2,len] + utf8 body."""
    b = code.encode('utf-8')
    return b'2\n*' + struct.pack('>6i', 1, 1, 1, 1, 2, len(b)) + b


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    shutil.copy(NEWPROJ, os.path.join(WORK, 'boot.toe'))
    # NOTE: toeexpand/toecollapse exit non-zero even on success — verify by output files.
    subprocess.run([os.path.join(TD_BIN_DIR, 'toeexpand'), 'boot.toe'], cwd=WORK,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    dd = os.path.join(WORK, 'boot.toe.dir')
    if not os.path.isdir(dd):
        sys.exit('toeexpand failed')

    proj = os.path.join(dd, 'project1')
    os.makedirs(proj, exist_ok=True)
    name = 'nm_parity_exec'
    with open(os.path.join(proj, name + '.n'), 'w') as f:
        f.write('DAT:execute\ntile 200 200 130 90\nflags =  viewer 1 parlanguage 0\n'
                'color 0.55 0.55 0.55 \nview 8 0 1 1 1 0 0 0 0 1 1\nend\n')
    with open(os.path.join(proj, name + '.parm'), 'w') as f:
        f.write('?\nstart 0 on\ncreate 0 on\nlanguage 0 python\n?\n')   # enable onStart + onCreate
    with open(os.path.join(proj, name + '.text'), 'wb') as f:
        f.write(_toe_text(CALLBACK))

    # register the three child files in the .toc (authoritative recursive file list)
    toc = os.path.join(WORK, 'boot.toe.toc')
    lines = open(toc).read().splitlines()
    entries = ['project1/%s.%s' % (name, ext) for ext in ('n', 'parm', 'text')]
    out, done = [], False
    for ln in lines:
        out.append(ln)
        if ln.strip() in ('project1.panel', 'project1.parm') and not done:
            out.extend(entries); done = True
    open(toc, 'w').write('\n'.join(out) + '\n')

    if os.path.exists(OUT_TOE):
        os.remove(OUT_TOE)
    subprocess.run([os.path.join(TD_BIN_DIR, 'toecollapse'), 'boot.toe'], cwd=WORK,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    built = os.path.join(WORK, 'boot.toe')
    if not os.path.exists(built):
        sys.exit('toecollapse failed')
    shutil.copy(built, OUT_TOE)
    print('wrote %s (%d bytes)' % (OUT_TOE, os.path.getsize(OUT_TOE)))


if __name__ == '__main__':
    main()
