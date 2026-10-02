"""Sprawdzenie potoków rzeczywistego --windowed EXE; uruchamiane po buildzie.

python tests/frozen_smoke.py <EXE> <prywatny katalog dowodów>
Nie zastępuje testu okna ani instalacji modeli.
"""
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    exe = Path(sys.argv[1]).resolve()
    root = Path(sys.argv[2]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONIOENCODING='cp1252', PYTHONUTF8='0')
    for name in ('TEMP', 'TMP', 'LOCALAPPDATA', 'APPDATA'):
        env[name] = str(root)
    helper = root / 'helper_fixture.py'
    helper.write_text('''import json, os, pathlib, runpy, subprocess, sys, traceback
root = pathlib.Path(__file__).parent
mode = sys.argv[1]
(root / ('streams-' + mode + '.json')).write_text(json.dumps({
    'stdout_none': sys.stdout is None, 'stderr_none': sys.stderr is None,
    'frozen': bool(getattr(sys, 'frozen', False)), 'executable': sys.executable}), encoding='utf-8')
if mode == 'earlyerror':
    raise RuntimeError('Błąd przed importem bootstrapu Zażółć')
# Prawdziwy nagłówek bootstrapu: reconfigure + środowisko dla dzieci.
runpy.run_path(str(pathlib.Path(sys._MEIPASS) / 'bootstrap.py'), run_name='fixture_import')
if mode == 'error':
    raise RuntimeError('Błąd ścieżki Zażółć')
print('BLOG|Zażółć gęślą jaźń ' + mode, flush=True)
print('Diagnostyka: moduł ' + mode, file=sys.stderr, flush=True)
if mode == 'parent':
    code = subprocess.call([sys.executable, '--run-helper', __file__, 'child'],
                           creationflags=subprocess.CREATE_NO_WINDOW,
                           stdout=sys.stdout, stderr=sys.stderr)
    assert code == 0, code
''', encoding='utf-8')
    rows = []
    for mode in ('parent', 'error', 'earlyerror'):
        command = [str(exe), '--run-helper', str(helper), mode]
        p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        timed_out = False
        try:
            out, err = p.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            timed_out = True
            p.kill()
            out, err = p.communicate(timeout=5)
        (root / (mode + '.stdout')).write_bytes(out)
        (root / (mode + '.stderr')).write_bytes(err)
        # Nawet błędne kodowanie RED musi pozostać w raporcie. Surowe bajty są obok.
        try:
            stdout, stderr = out.decode('utf-8', 'strict'), err.decode('utf-8', 'strict')
            utf8 = True
        except UnicodeDecodeError:
            stdout, stderr = out.decode('utf-8', 'replace'), err.decode('utf-8', 'replace')
            utf8 = False
        if mode == 'parent':
            ok = p.returncode == 0 and all('BLOG|Zażółć gęślą jaźń ' + m in stdout and 'Diagnostyka: moduł ' + m in stderr for m in ('parent', 'child'))
        else:
            message = ('Błąd ścieżki Zażółć' if mode == 'error' else 'Błąd przed importem bootstrapu Zażółć')
            ok = p.returncode == 1 and 'Traceback (most recent call last)' in stderr and 'RuntimeError: ' + message in stderr
        rows.append({'mode': mode, 'command': command, 'pid': p.pid, 'returncode': p.returncode,
                     'timeout': timed_out, 'stdout': stdout, 'stderr': stderr, 'utf8': utf8, 'ok': ok and utf8 and not timed_out})
    (root / 'result.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    # Konsola runnera może używać CP1252; JSON ASCII zachowuje Unicode przez escape.
    # Pełny raport plikowy powyżej pozostaje w UTF-8.
    print(json.dumps(rows, ensure_ascii=True))
    return 0 if all(r['ok'] for r in rows) else 1


if __name__ == '__main__':
    sys.exit(main())
