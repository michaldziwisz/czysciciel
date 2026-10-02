"""Bramka CI: właściwy interpreter, rzeczywisty wx, zero pominiętych testów."""
from pathlib import Path
import os
import sys
import unittest

if os.name != 'nt' or sys.version_info[:2] != (3, 12):
    raise SystemExit('Testy wydania wymagają Windows i Pythona 3.12.')
import wx

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
suite = unittest.defaultTestLoader.discover(str(root / 'tests'))
result = unittest.TextTestRunner(verbosity=2).run(suite)
print(f'Python: {sys.version}; wx: {wx.version()}; testy: {result.testsRun}; pominięte: {len(result.skipped)}')
sys.exit(0 if result.testsRun and result.wasSuccessful() and not result.skipped else 1)
