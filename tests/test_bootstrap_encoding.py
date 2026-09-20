"""Exercise real pipes with the Windows encoding from the reported failure.

Run: python -m unittest discover -s tests -v
No downloads or changes to the installed runtime are needed.
"""
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BootstrapEncodingTests(unittest.TestCase):
    def run_helper(self, source):
        env = dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONUTF8="0")
        return subprocess.run(
            [sys.executable, "-c", textwrap.dedent(source)], cwd=ROOT,
            env=env, capture_output=True, encoding="utf-8", errors="strict",
            timeout=20,
        )

    def test_denoiser_progress_and_optional_download_error(self):
        result = self.run_helper(r'''
            from unittest.mock import patch
            import bootstrap
            with patch.object(bootstrap.os.path, "exists", return_value=False), \
                 patch.object(bootstrap, "_download", side_effect=OSError("Błąd połączenia")) as download:
                bootstrap.ensure_denoiser({"dfn": "missing-denoiser.exe"})
                download.assert_called_once()
            print("BOOTOK|C:\\Zażółć\\python.exe|C:\\Zażółć\\ffmpeg.exe", flush=True)
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BOOT|88|Pobieranie modułu odszumiania", result.stdout)
        self.assertIn("Błąd połączenia", result.stdout)
        self.assertIn("BOOTOK|C:\\Zażółć\\python.exe", result.stdout)

    def test_errors_are_reported_through_direct_and_launcher_entry_points(self):
        for launcher in (False, True):
            with self.subTest(launcher=launcher):
                entry = "czysciciel.py" if launcher else "bootstrap.py"
                result = self.run_helper(f'''
                    import runpy, sys
                    from unittest.mock import patch
                    sys.argv = ["czysciciel.py", "--run-helper", "bootstrap.py"]
                    with patch("os.makedirs", side_effect=OSError("Błąd ścieżki")):
                        runpy.run_path({entry!r}, run_name="__main__")
                ''')
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("BLOG|BLAD: OSError('Błąd ścieżki')", result.stdout)
                self.assertIn("BLOG|  Traceback", result.stdout)
                self.assertIn("BOOTERR|Błąd ścieżki", result.stdout)
                self.assertNotIn("UnicodeEncodeError", result.stdout + result.stderr)

    def test_nested_python_output_and_errors_are_utf8(self):
        result = self.run_helper(r'''
            import sys
            import bootstrap
            result = bootstrap._run(
                [sys.executable, "-c", "print('Zażółć gęślą jaźń')"], "helper")
            assert result.stdout.strip() == "Zażółć gęślą jaźń", repr(result.stdout)
            try:
                bootstrap._run([sys.executable, "-c",
                    "import sys; sys.exit('Błąd pobierania')"], "helper")
            except RuntimeError as error:
                assert "Błąd pobierania" in str(error), repr(error)
                bootstrap.blog(str(error))
            else:
                raise AssertionError("expected helper failure")
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Błąd pobierania", result.stdout)


if __name__ == "__main__":
    unittest.main()
