"""Launcher helpera: traceback w potoku zamiast dialogu bootloadera."""
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import czysciciel


class LauncherTests(unittest.TestCase):
    def test_exception_is_reported_and_becomes_exit_one(self):
        err = io.StringIO()
        with patch.object(sys, 'argv', ['Czysciciel.exe', '--run-helper', 'fixture.py']), \
             patch.object(sys, 'stderr', err), \
             patch.object(czysciciel.runpy, 'run_path', side_effect=RuntimeError('Błąd ścieżki')):
            with self.assertRaises(SystemExit) as result:
                czysciciel._run_helper()
        self.assertEqual(result.exception.code, 1)
        self.assertIn('Traceback (most recent call last)', err.getvalue())
        self.assertIn('RuntimeError: Błąd ścieżki', err.getvalue())

    def test_missing_streams_do_not_crash_successful_helper(self):
        with patch.object(sys, 'argv', ['Czysciciel.exe', '--run-helper', 'fixture.py', 'arg']), \
             patch.object(sys, 'stdout', None), patch.object(sys, 'stderr', None), \
             patch.object(czysciciel.runpy, 'run_path') as run:
            czysciciel._run_helper()
            run.assert_called_once_with('fixture.py', run_name='__main__')
            self.assertEqual(sys.argv, ['fixture.py', 'arg'])

    def test_explicit_exit_code_is_preserved(self):
        with patch.object(sys, 'argv', ['exe', '--run-helper', 'fixture.py']), \
             patch.object(czysciciel.runpy, 'run_path', side_effect=SystemExit(7)):
            with self.assertRaises(SystemExit) as result:
                czysciciel._run_helper()
        self.assertEqual(result.exception.code, 7)

    def test_early_error_uses_utf8_without_importing_bootstrap(self):
        code = """import sys
from unittest.mock import patch
import czysciciel
sys.argv = ['exe', '--run-helper', 'fixture.py']
with patch.object(czysciciel.runpy, 'run_path', side_effect=RuntimeError('Błąd ścieżki Zażółć')):
    czysciciel.main()
"""
        p = subprocess.run([sys.executable, '-B', '-c', code],
                           cwd=Path(__file__).resolve().parents[1], capture_output=True,
                           env=dict(os.environ, PYTHONIOENCODING='cp1252', PYTHONUTF8='0'),
                           timeout=20)
        self.assertEqual(p.returncode, 1)
        self.assertIn('RuntimeError: Błąd ścieżki Zażółć', p.stderr.decode('utf-8', 'replace'))


if __name__ == '__main__':
    unittest.main()
