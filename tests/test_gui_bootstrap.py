"""Test installer output handling without opening GUI windows."""
import importlib.util
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


@unittest.skipUnless(importlib.util.find_spec("wx"), "wxPython is required")
class BootstrapOutputTests(unittest.TestCase):
    def check_output(self, output, returncode):
        import gui

        proc = Mock(stdout=io.StringIO(output), returncode=returncode)
        frame = SimpleNamespace(
            _python_for_helper=Mock(return_value=["python", "bootstrap.py"]),
            _no_window=lambda: 0, _procs=set(),
            _boot_progress=Mock(), append_log=Mock(),
        )
        with patch.object(gui, "helper_script", return_value="bootstrap.py"), \
             patch.object(gui.subprocess, "Popen", return_value=proc), \
             patch.object(gui.wx, "CallAfter", side_effect=lambda fn, *args: fn(*args)):
            result = gui.MainFrame._ensure_runtime(frame)
        proc.wait.assert_called_once()
        self.assertFalse(frame._procs)
        return result, frame

    def test_raw_traceback_is_visible_when_bootstrap_crashes(self):
        result, frame = self.check_output(
            "BLOG|sprzatam stare: cache\n"
            "Traceback (most recent call last):\n"
            "UnicodeEncodeError: cannot encode character\n", 1)
        self.assertEqual(result, (None, None))
        frame.append_log.assert_any_call("  Traceback (most recent call last):")
        frame.append_log.assert_any_call("  UnicodeEncodeError: cannot encode character")

    def test_successful_protocol_preserves_unicode_paths(self):
        result, frame = self.check_output(
            "BOOT|88|Pobieranie modułu odszumiania\n"
            "BOOTOK|C:\\Zażółć\\python.exe|C:\\Zażółć\\ffmpeg.exe\n", 0)
        self.assertEqual(result, ("C:\\Zażółć\\python.exe", "C:\\Zażółć\\ffmpeg.exe"))
        frame._boot_progress.assert_called_once_with(88, "Pobieranie modułu odszumiania")


if __name__ == "__main__":
    unittest.main()
