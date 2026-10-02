"""Kodowanie raportu sondy, nie test EXE: wyniki procesów są jawną atrapą."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(os.name == 'nt', 'Sonda wydania działa na Windows')
class SmokeReportTests(unittest.TestCase):
    def test_report_survives_cp1252_console_without_losing_unicode(self):
        script = Path(__file__).with_name('frozen_smoke.py')
        probe = r'''import runpy, sys
from unittest.mock import patch
script, folder = sys.argv[1:]
class FixtureProcess:
    pid = 123
    def __init__(self, command, **kwargs):
        self.mode = command[-1]
        self.returncode = 0 if self.mode == 'parent' else 1
    def communicate(self, timeout=None):
        if self.mode == 'parent':
            out = ''.join('BLOG|Zażółć gęślą jaźń '+m+'\n' for m in ('parent','child'))
            err = ''.join('Diagnostyka: moduł '+m+'\n' for m in ('parent','child'))
        else:
            out = ''
            message = 'Błąd ścieżki Zażółć' if self.mode == 'error' else 'Błąd przed importem bootstrapu Zażółć'
            err = 'Traceback (most recent call last):\nRuntimeError: '+message+'\n'
        return out.encode('utf-8'), err.encode('utf-8')
sys.argv = [script, 'fixture-not-an-exe', folder]
with patch('subprocess.Popen', FixtureProcess):
    runpy.run_path(script, run_name='__main__')
'''
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run(
                [sys.executable, '-B', '-c', probe, str(script), folder],
                capture_output=True, timeout=20,
                env=dict(os.environ, PYTHONIOENCODING='cp1252:strict', PYTHONUTF8='0'),
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode('ascii', 'replace'))
            console = json.loads(result.stdout.decode('ascii'))
            stored = json.loads((Path(folder) / 'result.json').read_text(encoding='utf-8'))
            self.assertEqual(console, stored)
            self.assertEqual(len(stored), 3)
            self.assertTrue(all(row['ok'] for row in stored))
            self.assertIn('Zażółć', stored[0]['stdout'])


if __name__ == '__main__':
    unittest.main()
