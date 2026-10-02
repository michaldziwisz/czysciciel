"""Regresje instalatora: prawdziwe procesy i potoki, bez pobierania zasobów."""
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import gui


class RuntimeProcessTests(unittest.TestCase):
    def exercise(self, code, *, read_error=None, callback_error=None, stop=False, run_all=False):
        children = []
        real_popen = subprocess.Popen
        started = threading.Event()
        received = threading.Event()
        outcome = {}
        logs = []
        progress = []
        frame = SimpleNamespace(
            _python_for_helper=lambda _: [sys.executable, '-B', '-c', code],
            _no_window=lambda: getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            _procs=set(), stop_flag=threading.Event(),
            append_log=logs.append, _done=Mock(), set_status=Mock(),
        )
        def on_progress(*args):
            received.set()
            if callback_error:
                raise callback_error
            progress.append(args)
        frame._boot_progress = on_progress
        frame._proc_kill = lambda: gui.MainFrame._proc_kill(frame)
        frame._ensure_runtime = lambda: gui.MainFrame._ensure_runtime(frame)

        class Reader:
            def __init__(self, stream):
                self.stream = stream
            def __iter__(self):
                return self
            def __next__(self):
                self.stream.readline()  # dziecko naprawdę rozpoczęło zapis
                raise read_error
            def close(self):
                self.stream.close()

        class Tracked(real_popen):
            def __init__(self, *args, **kwargs):
                kwargs['env'] = dict(os.environ, PYTHONIOENCODING='utf-8')
                super().__init__(*args, **kwargs)
                children.append(self)
                if read_error:
                    self.stdout = Reader(self.stdout)
                started.set()

        def invoke():
            try:
                outcome['value'] = (gui.MainFrame._run_all(frame, [], {}) if run_all
                                    else frame._ensure_runtime())
            except BaseException as error:
                outcome['error'] = error

        with patch.object(gui, 'helper_script', return_value='unused'), \
             patch.object(gui.subprocess, 'Popen', Tracked), \
             patch.object(gui.wx, 'CallAfter', side_effect=lambda fn, *args: fn(*args)):
            thread = threading.Thread(target=invoke, daemon=True)
            begin = time.monotonic()
            thread.start()
            try:
                self.assertTrue(started.wait(10), 'Nie uruchomiono procesu kontrolnego')
                if stop:
                    self.assertTrue(received.wait(10))
                    gui.MainFrame.on_stop(frame, None)
                thread.join(10)
                finished = not thread.is_alive()
                reaped = all(p.poll() is not None for p in children)
                registry = len(frame._procs)
            finally:
                # Także RED nie może osierocić dziecka ani zawiesić całego zestawu.
                for p in children:
                    if p.poll() is None:
                        p.kill()
                    p.wait(timeout=5)
                thread.join(5)
                for p in children:
                    p.stdout.close()
            self.assertTrue(finished, 'Zakleszczenie: rodzic czeka w wait, dziecko zapisuje pełny potok')
            self.assertTrue(reaped, 'Powrót przed zakończeniem helpera')
            self.assertEqual(registry, 0, 'Martwa referencja w _procs')
        outcome.update(logs=logs, progress=progress, frame=frame, elapsed=time.monotonic() - begin)
        return outcome

    def test_malformed_boot_with_full_pipe_raises_without_deadlock(self):
        result = self.exercise("import sys; print('BOOT|oops|błąd', flush=True); sys.stdout.write('X'*2000000); sys.stdout.flush()")
        self.assertIsInstance(result.get('error'), ValueError)
        self.assertIn('oops', str(result['error']))

    def test_read_error_with_writing_child_preserves_original_exception(self):
        error = OSError('Błąd odczytu kontrolnego')
        result = self.exercise("import sys; print('BLOG|początek', flush=True); sys.stdout.write('X'*2000000); sys.stdout.flush()", read_error=error)
        self.assertIs(result.get('error'), error)

    def test_callback_error_with_writing_child_preserves_original_exception(self):
        error = RuntimeError('Błąd odbiorcy kontrolnego')
        result = self.exercise("import sys; print('BOOT|1|start', flush=True); sys.stdout.write('X'*2000000); sys.stdout.flush()", callback_error=error)
        self.assertIs(result.get('error'), error)

    def test_success_with_large_output_and_unicode_paths(self):
        result = self.exercise("import sys; print('BOOT|88|moduł'); print(('BLOG|'+'X'*2000+'\\n')*1000, end=''); print('BOOTOK|C:\\\\Zażółć\\\\python.exe|C:\\\\Zażółć\\\\ffmpeg.exe')")
        self.assertNotIn('error', result)
        self.assertEqual(result['value'], (r'C:\Zażółć\python.exe', r'C:\Zażółć\ffmpeg.exe'))
        self.assertEqual(result['progress'], [(88, 'moduł')])
        self.assertEqual(len(result['logs']), 1000)

    def test_nonzero_exit_rejects_even_bootok_and_shows_stderr(self):
        result = self.exercise("import sys; print('BOOTOK|python|ffmpeg'); print('Traceback: błąd ścieżki', file=sys.stderr); sys.exit(7)")
        self.assertEqual(result.get('value'), (None, None))
        self.assertIn('  Traceback: błąd ścieżki', result['logs'])
        self.assertIn('Nie udało się przygotować środowiska.', result['logs'])

    def test_booterr_is_visible(self):
        result = self.exercise("import sys; print('BOOTERR|Błąd instalacji kontrolnej'); sys.exit(1)")
        self.assertEqual(result.get('value'), (None, None))
        self.assertIn('BŁĄD instalacji: Błąd instalacji kontrolnej', result['logs'])

    def test_stop_terminates_live_helper(self):
        result = self.exercise("import time; print('BOOT|1|start', flush=True); time.sleep(60)", stop=True)
        self.assertEqual(result.get('value'), (None, None))
        self.assertTrue(result['frame'].stop_flag.is_set())

    def test_normal_eof_waits_for_helper_without_killing_it(self):
        result = self.exercise("import os,time; print('BOOTOK|python|ffmpeg', flush=True); os.close(1); os.close(2); time.sleep(1)")
        self.assertEqual(result.get('value'), ('python', 'ffmpeg'))
        self.assertGreaterEqual(result['elapsed'], 0.9)

    def test_parser_error_is_reported_by_batch_handler(self):
        result = self.exercise("import sys; print('BOOT|oops|błąd', flush=True); sys.stdout.write('X'*2000000); sys.stdout.flush()", run_all=True)
        self.assertNotIn('error', result)
        self.assertTrue(any('BŁĄD krytyczny: ValueError' in line for line in result['logs']))
        result['frame']._done.assert_called_once_with(False, 0, 0)


if __name__ == '__main__':
    unittest.main()
