#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Czysciciel - punkt wejscia (frozen exe: PyInstaller onedir).

Ten SAM plik jest wywolywany na trzy sposoby:
  Czysciciel.exe                      -> uruchamia GUI
  Czysciciel.exe --run-helper X.py    -> uruchamia skrypt pomocniczy X (bootstrap/worker)
                                         WBUDOWANYM interpreterem (subproces nie ma
                                         osobnego pythona - to ten sam exe).

Powod: spakowany exe zawiera CPython, ale nie da sie go wywolac jak 'python plik.py'.
Multiplexujemy przez argument --run-helper, ktory wykonuje docelowy skrypt przez runpy
w kontekscie tego exe (dostep do sys._MEIPASS, spakowanych zaleznosci itd.).
"""
import sys, os, runpy

def _run_helper():
    # Czysciciel.exe --run-helper <sciezka_skryptu> [reszta argv...]
    script = sys.argv[2]
    # przekaz reszte argumentow tak, jakby skrypt byl wywolany bezposrednio
    sys.argv = [script] + sys.argv[3:]
    # Kodowanie obowiązuje też dla błędów przed importem bootstrapu.
    # Bez przekierowania windowed EXE może nie mieć strumieni; reconfigure
    # ich nie tworzy. GUI przekazuje helperowi rzeczywisty potok przez Popen.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    os.environ["PYTHONIOENCODING"] = "utf-8:backslashreplace"
    try:
        runpy.run_path(script, run_name="__main__")
    except Exception:
        # Dialog błędu bootloadera zatrzymuje helper i oczekujące na niego GUI.
        import traceback
        traceback.print_exc()
        sys.exit(1)

def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--run-helper":
        _run_helper()
        return
    # domyslnie: GUI
    import gui
    gui.main()

if __name__ == "__main__":
    main()
