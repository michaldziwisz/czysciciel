#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Czysciciel - graficzny interfejs (wxPython, dostepny dla NVDA/JAWS).

Czysci nagrania z fillerow (yyy/eee/mmm) i skraca nadmiarowe pauzy modelem AI.
Architektura 1: przy pierwszym uruchomieniu dociaga srodowisko (torch + model
+ ffmpeg) do %LOCALAPPDATA%\\Czysciciel; kolejne starty sa natychmiastowe.

Funkcje:
  - lista plikow do przetworzenia (checkboxy, tryb wsadowy),
  - preset skracania pauz, prog min-filler, tryb "tylko fillery",
  - folder wyjsciowy,
  - eksport projektu Reapera (.RPP),
  - pasek postepu + dziennik czytany przez czytnik ekranu,
  - otwarcie folderu z wynikiem.
"""
import os, sys, threading, subprocess, queue, time
import wx

APP_NAME = "Czysciciel"          # klucz techniczny: nazwa exe i folderu %LOCALAPPDATA% (bez ogonka)
APP_TITLE = "Czyściciel"         # nazwa wyswietlana czlowiekowi
PRESETY = ["zachowawczy", "umiarkowany", "zwarty"]
PRESET_OPISY = {
    "zachowawczy": "zachowawczy (ledwo zauważalne skracanie pauz)",
    "umiarkowany": "umiarkowany (domyślny, dobry kompromis)",
    "zwarty": "zwarty (radiowe, zwięzłe tempo)",
}
AUDIO_EXT = [".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".wma"]
# formaty wyjsciowe: klucz workera -> (etykieta, czy stratny -> bitrate aktywny)
FORMATY_OUT = [
    ("mp3",  "MP3 (.mp3)", True),
    ("aac",  "AAC (.m4a)", True),
    ("opus", "Opus (.opus)", True),
    ("ogg",  "Ogg Vorbis (.ogg)", True),
    ("wma",  "WMA (.wma)", True),
    ("ac3",  "AC3 (.ac3)", True),
    ("flac", "FLAC bezstratny (.flac)", False),
    ("alac", "ALAC bezstratny (.m4a)", False),
    ("wav",  "WAV nieskompresowany (.wav)", False),
]
TRYBY = [
    ("fillery", "Wycinaj tylko fillery (yyy, eee, mmm)"),
    ("cisza",   "Wycinaj tylko ciszę (za długie pauzy)"),
    ("oba",     "Wycinaj fillery i ciszę"),
]
EKSPORTY = [
    ("audio",  "Tylko plik audio"),
    ("reaper", "Tylko projekt Reapera (.RPP)"),
    ("oba",    "Audio i projekt Reapera"),
]
BITRATE_LISTA = [64, 96, 128, 160, 192, 224, 256, 320]

def app_dir():
    """Katalog, w ktorym lezy exe/skrypt (tam sa bootstrap.py, worker.py)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def resource(name):
    """Zasob spakowany PyInstallerem (bootstrap.py, worker.py) - _MEIPASS gdy frozen."""
    base = getattr(sys, "_MEIPASS", app_dir())
    return os.path.join(base, name)

def runtime_root():
    base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    return os.path.join(base, APP_NAME)

def helper_script(name):
    """Kopiuje skrypt pomocniczy (bootstrap.py/worker.py) z _MEIPASS do CZYSTEGO
    folderu app w runtime i zwraca sciezke tam. KRYTYCZNE: nie wolno uruchamiac
    tych skryptow z katalogu _internal PyInstallera - runtime venv python dodalby
    _internal na sys.path[0] i zaladowal frozen _cffi_backend.pyd zamiast swojego
    (Version mismatch cffi). Neutralny folder eliminuje ten konflikt."""
    src = resource(name)
    appdir = os.path.join(runtime_root(), "app")
    os.makedirs(appdir, exist_ok=True)
    dst = os.path.join(appdir, name)
    try:
        if (not os.path.exists(dst)) or os.path.getmtime(src) > os.path.getmtime(dst) \
           or os.path.getsize(src) != os.path.getsize(dst):
            import shutil
            shutil.copy2(src, dst)
    except Exception:
        pass
    return dst


class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title=APP_TITLE + " - czyszczenie audio z fillerów i pauz",
                         size=(760, 640))
        self.worker_thread = None
        self.stop_flag = threading.Event()
        self.runtime_python = None
        self.runtime_ffmpeg = None
        self._set_icon()
        self._build_ui()
        self.Centre()
        self.Show()

    # ---------------- UI ----------------
    def _set_icon(self):
        """Ikona okna (pasek zadan, Alt+Tab). Cicho pomija gdy pliku brak."""
        try:
            p = resource(os.path.join("assets", "czysciciel.ico"))
            if os.path.exists(p):
                self.SetIcon(wx.Icon(p, wx.BITMAP_TYPE_ICO))
        except Exception:
            pass

    def _build_ui(self):
        panel = wx.Panel(self)
        # akcelerator: SetName wszedzie dla NVDA + StaticText PRZED kontrolka
        root = wx.BoxSizer(wx.VERTICAL)

        # --- lista plikow ---
        lbl_list = wx.StaticText(panel, label="&Pliki do wyczyszczenia:")
        root.Add(lbl_list, 0, wx.LEFT | wx.TOP, 8)
        self.lst = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.lst.EnableCheckBoxes(True)
        self.lst.InsertColumn(0, "Plik", width=430)
        self.lst.InsertColumn(1, "Ścieżka", width=280)
        self.lst.SetName("Lista plików do wyczyszczenia. Spacja zaznacza lub odznacza.")
        root.Add(self.lst, 1, wx.EXPAND | wx.ALL, 8)

        # przyciski listy
        row_btn = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_add = wx.Button(panel, label="&Dodaj pliki...")
        self.btn_addfolder = wx.Button(panel, label="Dodaj &folder...")
        self.btn_del = wx.Button(panel, label="&Usuń z listy")
        self.btn_clear = wx.Button(panel, label="Wy&czyść listę")
        for b in (self.btn_add, self.btn_addfolder, self.btn_del, self.btn_clear):
            b.SetName(b.GetLabel().replace("&", ""))
            row_btn.Add(b, 0, wx.RIGHT, 6)
        root.Add(row_btn, 0, wx.LEFT | wx.BOTTOM, 8)

        # --- opcje ---
        opt = wx.StaticBoxSizer(wx.VERTICAL, panel, "Opcje czyszczenia")

        # tryb pracy (przyciski opcji)
        self.rb_tryb = wx.RadioBox(panel, label="Co wycinać",
                                   choices=[t[1] for t in TRYBY],
                                   majorDimension=1, style=wx.RA_SPECIFY_COLS)
        self.rb_tryb.SetSelection(2)  # oba
        self.rb_tryb.SetName("Co wycinać")
        opt.Add(self.rb_tryb, 0, wx.EXPAND | wx.ALL, 5)

        # preset skracania pauz
        r1 = wx.BoxSizer(wx.HORIZONTAL)
        lbl_p = wx.StaticText(panel, label="&Skracanie pauz:")
        r1.Add(lbl_p, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.ch_preset = wx.Choice(panel, choices=[PRESET_OPISY[p] for p in PRESETY])
        self.ch_preset.SetSelection(1)  # umiarkowany
        self.ch_preset.SetName("Skracanie pauz - preset")
        r1.Add(self.ch_preset, 1, wx.ALIGN_CENTER_VERTICAL)
        opt.Add(r1, 0, wx.EXPAND | wx.ALL, 5)

        # min filler
        r2 = wx.BoxSizer(wx.HORIZONTAL)
        lbl_mf = wx.StaticText(panel, label="&Minimalna długość fillera (s):")
        r2.Add(lbl_mf, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.sc_minfiller = wx.SpinCtrlDouble(panel, min=0.10, max=1.00, inc=0.05, initial=0.30)
        self.sc_minfiller.SetDigits(2)
        self.sc_minfiller.SetName("Minimalna długość fillera w sekundach")
        r2.Add(self.sc_minfiller, 0, wx.ALIGN_CENTER_VERTICAL)
        opt.Add(r2, 0, wx.EXPAND | wx.ALL, 5)
        root.Add(opt, 0, wx.EXPAND | wx.ALL, 8)

        # --- format wyjscia ---
        fmt = wx.StaticBoxSizer(wx.VERTICAL, panel, "Format wyjściowy")
        rf = wx.BoxSizer(wx.HORIZONTAL)
        lbl_fmt = wx.StaticText(panel, label="&Format:")
        rf.Add(lbl_fmt, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.ch_format = wx.Choice(panel, choices=[f[1] for f in FORMATY_OUT])
        self.ch_format.SetSelection(0)  # mp3
        self.ch_format.SetName("Format wyjściowy")
        rf.Add(self.ch_format, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        lbl_ch = wx.StaticText(panel, label="&Kanały:")
        rf.Add(lbl_ch, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.ch_kanaly = wx.Choice(panel, choices=["jak w źródle", "mono", "stereo"])
        self.ch_kanaly.SetSelection(2)  # stereo
        self.ch_kanaly.SetName("Liczba kanałów")
        rf.Add(self.ch_kanaly, 0, wx.ALIGN_CENTER_VERTICAL)
        fmt.Add(rf, 0, wx.EXPAND | wx.ALL, 5)

        # bitrate (lista wyboru typowych wartosci)
        rb = wx.BoxSizer(wx.HORIZONTAL)
        self.lbl_bitrate = wx.StaticText(panel, label="&Bitrate (kbps):")
        rb.Add(self.lbl_bitrate, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.ch_bitrate = wx.Choice(panel, choices=[str(b) for b in BITRATE_LISTA])
        self.ch_bitrate.SetSelection(BITRATE_LISTA.index(192))
        self.ch_bitrate.SetName("Bitrate w kbps")
        rb.Add(self.ch_bitrate, 0, wx.ALIGN_CENTER_VERTICAL)
        fmt.Add(rb, 0, wx.EXPAND | wx.ALL, 5)
        root.Add(fmt, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.box_fmt = fmt  # do ukrywania przy trybie "tylko reaper"

        # --- co zapisac (eksport) ---
        self.rb_eksport = wx.RadioBox(panel, label="Co zapisać",
                                      choices=[e[1] for e in EKSPORTY],
                                      majorDimension=1, style=wx.RA_SPECIFY_COLS)
        self.rb_eksport.SetSelection(0)  # audio
        self.rb_eksport.SetName("Co zapisać")
        root.Add(self.rb_eksport, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # --- folder wyjsciowy ---
        r3 = wx.BoxSizer(wx.HORIZONTAL)
        lbl_out = wx.StaticText(panel, label="Folder &wyjściowy:")
        r3.Add(lbl_out, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.txt_out = wx.TextCtrl(panel)
        self.txt_out.SetName("Folder wyjściowy")
        self.txt_out.SetHint("domyślnie obok pliku wejściowego")
        r3.Add(self.txt_out, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.btn_out = wx.Button(panel, label="&Wybierz...")
        self.btn_out.SetName("Wybierz folder wyjściowy")
        r3.Add(self.btn_out, 0)
        root.Add(r3, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # --- start/stop ---
        r4 = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_start = wx.Button(panel, label="Ur&uchom czyszczenie  (F5)")
        self.btn_start.SetName("Uruchom czyszczenie")
        self.btn_stop = wx.Button(panel, label="Za&trzymaj")
        self.btn_stop.SetName("Zatrzymaj")
        self.btn_stop.Disable()
        self.btn_openout = wx.Button(panel, label="&Otwórz folder wyniku")
        self.btn_openout.SetName("Otwórz folder wyniku")
        for b in (self.btn_start, self.btn_stop, self.btn_openout):
            r4.Add(b, 0, wx.RIGHT, 6)
        root.Add(r4, 0, wx.LEFT | wx.BOTTOM, 8)

        # --- pasek postepu ---
        self.gauge = wx.Gauge(panel, range=100)
        self.gauge.SetName("Postęp")
        root.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        self.lbl_status = wx.StaticText(panel, label="Gotowy.")
        self.lbl_status.SetName("Status")
        root.Add(self.lbl_status, 0, wx.ALL, 8)

        # --- dziennik ---
        lbl_log = wx.StaticText(panel, label="Dzienni&k:")
        root.Add(lbl_log, 0, wx.LEFT, 8)
        self.log = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        self.log.SetName("Dziennik")
        root.Add(self.log, 1, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(root)

        # zdarzenia
        self.btn_add.Bind(wx.EVT_BUTTON, self.on_add)
        self.btn_addfolder.Bind(wx.EVT_BUTTON, self.on_add_folder)
        self.btn_del.Bind(wx.EVT_BUTTON, self.on_del)
        self.btn_clear.Bind(wx.EVT_BUTTON, self.on_clear)
        self.btn_out.Bind(wx.EVT_BUTTON, self.on_pick_out)
        self.btn_start.Bind(wx.EVT_BUTTON, self.on_start)
        self.btn_stop.Bind(wx.EVT_BUTTON, self.on_stop)
        self.btn_openout.Bind(wx.EVT_BUTTON, self.on_open_out)
        self.ch_format.Bind(wx.EVT_CHOICE, self.on_format_change)
        self.rb_eksport.Bind(wx.EVT_RADIOBOX, self.on_eksport_change)
        self.on_format_change(None)   # ustaw stan bitrate wg domyslnego formatu
        self.on_eksport_change(None)  # ustaw widocznosc ustawien audio wg eksportu

        # menu + akceleratory
        mb = wx.MenuBar()
        m = wx.Menu()
        mi_add = m.Append(wx.ID_ANY, "&Dodaj pliki...\tCtrl+O")
        mi_start = m.Append(wx.ID_ANY, "Ur&uchom czyszczenie\tF5")
        m.AppendSeparator()
        mi_exit = m.Append(wx.ID_EXIT, "Za&mknij\tAlt+F4")
        mb.Append(m, "&Program")
        mh = wx.Menu()
        mi_about = mh.Append(wx.ID_ABOUT, "&O programie")
        mb.Append(mh, "Pomo&c")
        self.SetMenuBar(mb)
        self.Bind(wx.EVT_MENU, self.on_add, mi_add)
        self.Bind(wx.EVT_MENU, self.on_start, mi_start)
        self.Bind(wx.EVT_MENU, lambda e: self.Close(), mi_exit)
        self.Bind(wx.EVT_MENU, self.on_about, mi_about)

        self.Bind(wx.EVT_CLOSE, self.on_close)

    # ---------------- helpery listy ----------------
    def _add_paths(self, paths):
        existing = {self.lst.GetItem(i, 1).GetText() for i in range(self.lst.GetItemCount())}
        added = 0
        for p in paths:
            p = os.path.abspath(p)
            if p in existing: continue
            if os.path.splitext(p)[1].lower() not in AUDIO_EXT: continue
            i = self.lst.InsertItem(self.lst.GetItemCount(), os.path.basename(p))
            self.lst.SetItem(i, 1, p)
            self.lst.CheckItem(i, True)
            added += 1
        if added:
            self.append_log(f"Dodano plików: {added}")

    def _checked_files(self):
        out = []
        for i in range(self.lst.GetItemCount()):
            if self.lst.IsItemChecked(i):
                out.append(self.lst.GetItem(i, 1).GetText())
        return out

    # ---------------- zdarzenia UI ----------------
    def on_add(self, evt):
        wc = "Pliki audio (" + ";".join("*"+e for e in AUDIO_EXT) + ")|" + \
             ";".join("*"+e for e in AUDIO_EXT) + "|Wszystkie pliki|*.*"
        with wx.FileDialog(self, "Wybierz pliki audio", wildcard=wc,
                           style=wx.FD_OPEN | wx.FD_MULTIPLE | wx.FD_FILE_MUST_EXIST) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self._add_paths(dlg.GetPaths())

    def on_add_folder(self, evt):
        with wx.DirDialog(self, "Wybierz folder z nagraniami") as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                d = dlg.GetPath()
                found = [os.path.join(d, f) for f in sorted(os.listdir(d))
                         if os.path.splitext(f)[1].lower() in AUDIO_EXT]
                self._add_paths(found)

    def on_del(self, evt):
        i = self.lst.GetFirstSelected()
        if i >= 0: self.lst.DeleteItem(i)

    def on_clear(self, evt):
        self.lst.DeleteAllItems()

    def on_pick_out(self, evt):
        with wx.DirDialog(self, "Wybierz folder wyjściowy") as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self.txt_out.SetValue(dlg.GetPath())

    def on_open_out(self, evt):
        d = self.txt_out.GetValue().strip()
        if not d:
            files = self._checked_files()
            d = os.path.dirname(files[0]) if files else app_dir()
        if os.path.isdir(d):
            try:
                os.startfile(d)  # Windows
            except Exception:
                self.append_log("Nie mogę otworzyć folderu: " + d)

    def on_format_change(self, evt):
        """Bitrate ma sens tylko dla formatow stratnych - wylacz liste dla bezstratnych."""
        stratny = FORMATY_OUT[self.ch_format.GetSelection()][2]
        self.ch_bitrate.Enable(stratny)
        self.lbl_bitrate.Enable(stratny)

    def on_eksport_change(self, evt):
        """Gdy wybrany TYLKO projekt Reapera - ustawienia audio sa nieistotne, ukryj je."""
        eksport = EKSPORTY[self.rb_eksport.GetSelection()][0]
        audio_potrzebne = eksport in ("audio", "oba")
        self.box_fmt.GetStaticBox().Show(audio_potrzebne)
        for c in (self.ch_format, self.ch_kanaly, self.ch_bitrate, self.lbl_bitrate):
            c.Show(audio_potrzebne)
        if audio_potrzebne:
            self.on_format_change(None)
        self.Layout()

    def on_about(self, evt):
        wx.MessageBox(
            APP_TITLE + " - czyszczenie audio z fillerów (yyy/eee) i nadmiarowych pauz.\n\n"
            "Model: classla/wav2vecbert2-filledPause (Apache-2.0).\n"
            "Silnik AI: PyTorch + Transformers. Audio: ffmpeg (LGPL), librosa, soundfile.\n\n"
            "Działa na karcie NVIDIA (szybciej) lub na procesorze.\n"
            "Środowisko instaluje się raz przy pierwszym uruchomieniu.",
            "O programie", wx.OK | wx.ICON_INFORMATION)

    # ---------------- uruchomienie ----------------
    def _set_running(self, running):
        for b in (self.btn_start, self.btn_add, self.btn_addfolder, self.btn_del,
                  self.btn_clear, self.btn_out, self.ch_preset, self.sc_minfiller,
                  self.rb_tryb, self.rb_eksport, self.ch_format, self.ch_kanaly,
                  self.ch_bitrate):
            b.Enable(not running)
        if not running:
            self.on_format_change(None)  # przywroc poprawny stan bitrate
        self.btn_stop.Enable(running)

    def on_start(self, evt):
        if self.worker_thread and self.worker_thread.is_alive():
            return
        files = self._checked_files()
        if not files:
            wx.MessageBox("Zaznacz przynajmniej jeden plik na liście.", APP_TITLE,
                          wx.OK | wx.ICON_WARNING)
            return
        preset = PRESETY[self.ch_preset.GetSelection()]
        minf = self.sc_minfiller.GetValue()
        tryb = TRYBY[self.rb_tryb.GetSelection()][0]
        eksport = EKSPORTY[self.rb_eksport.GetSelection()][0]
        fmt = FORMATY_OUT[self.ch_format.GetSelection()][0]
        bitrate = BITRATE_LISTA[self.ch_bitrate.GetSelection()]
        kanaly = ["zrodlo", "mono", "stereo"][self.ch_kanaly.GetSelection()]
        outdir = self.txt_out.GetValue().strip() or None
        opts = dict(preset=preset, minf=minf, tryb=tryb, eksport=eksport,
                    fmt=fmt, bitrate=bitrate, kanaly=kanaly, outdir=outdir)
        self.stop_flag.clear()
        self._set_running(True)
        self.gauge.SetValue(0)
        self.append_log("=" * 50)
        opis_tryb = TRYBY[self.rb_tryb.GetSelection()][1]
        self.append_log(f"Start. Plików: {len(files)} | {opis_tryb} | preset: {preset} | "
                        f"format: {fmt} {bitrate}kbps {kanaly} | eksport: {eksport}")
        self.worker_thread = threading.Thread(
            target=self._run_all, args=(files, opts), daemon=True)
        self.worker_thread.start()

    def on_stop(self, evt):
        self.stop_flag.set()
        self.append_log("Zatrzymywanie po bieżącym pliku...")
        self._proc_kill()

    _cur_proc = None
    def _proc_kill(self):
        p = self._cur_proc
        if p and p.poll() is None:
            try: p.terminate()
            except Exception: pass

    # ---- watek roboczy ----
    def _run_all(self, files, opts):
        try:
            # 1. srodowisko (bootstrap) - raz
            wx.CallAfter(self.set_status, "Sprawdzam środowisko...")
            vpy, ff = self._ensure_runtime()
            if vpy is None:
                wx.CallAfter(self._done, False, 0, 0)
                return
            self.runtime_python, self.runtime_ffmpeg = vpy, ff

            fmt_ext = {"mp3": "mp3", "aac": "m4a", "opus": "opus", "ogg": "ogg",
                       "wma": "wma", "ac3": "ac3", "flac": "flac", "alac": "m4a", "wav": "wav"}
            ext = fmt_ext.get(opts["fmt"], "mp3")
            audio_out = opts["eksport"] in ("audio", "oba")
            n = len(files); ok = 0
            for idx, f in enumerate(files):
                if self.stop_flag.is_set(): break
                base = os.path.basename(f); stem = os.path.splitext(base)[0]
                od = opts["outdir"] or os.path.dirname(f)
                os.makedirs(od, exist_ok=True)
                out = os.path.join(od, stem + "_czysty." + ext)
                wx.CallAfter(self.append_log, f"--- [{idx+1}/{n}] {base} ---")
                wx.CallAfter(self.set_status, f"[{idx+1}/{n}] {base}")
                rc = self._run_worker(vpy, ff, f, out, opts)
                # weryfikacja wyniku: audio -> plik audio; sam reaper -> plik .RPP
                if audio_out:
                    good = rc == 0 and os.path.exists(out) and os.path.getsize(out) > 50000
                else:
                    rpp = os.path.join(od, stem + ".RPP")
                    good = rc == 0 and os.path.exists(rpp) and os.path.getsize(rpp) > 100
                if good:
                    ok += 1
                    wx.CallAfter(self.append_log, f"OK: {base}")
                else:
                    wx.CallAfter(self.append_log, f"BŁĄD przy: {base}")
            wx.CallAfter(self.append_log, f"Zakończono. Sukces: {ok}/{n}")
            wx.CallAfter(self._done, True, ok, n)
        except Exception as e:
            import traceback
            wx.CallAfter(self.append_log, "BŁĄD krytyczny: " + repr(e))
            for ln in traceback.format_exc().splitlines():
                wx.CallAfter(self.append_log, "  " + ln)
            wx.CallAfter(self._done, False, 0, 0)

    def _ensure_runtime(self):
        """Odpala bootstrap.py minimalnym Pythonem (frozen: sys.executable z flaga)."""
        cmd = self._python_for_helper(helper_script("bootstrap.py"))
        vpy = ff = None
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                creationflags=self._no_window())
        self._cur_proc = proc
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line.startswith("BOOT|"):
                _, pct, msg = line.split("|", 2)
                wx.CallAfter(self._boot_progress, int(pct), msg)
            elif line.startswith("BLOG|"):
                wx.CallAfter(self.append_log, "  " + line[5:])
            elif line.startswith("BOOTOK|"):
                _, vpy, ff = line.split("|", 2)
            elif line.startswith("BOOTERR|"):
                wx.CallAfter(self.append_log, "BŁĄD instalacji: " + line[8:])
        proc.wait()
        if proc.returncode != 0 or not vpy:
            wx.CallAfter(self.append_log, "Nie udało się przygotować środowiska.")
            return None, None
        return vpy, ff

    def _run_worker(self, vpy, ff, fin, fout, opts):
        env = os.environ.copy()
        env["FFMPEG_BIN"] = ff
        root = os.path.dirname(os.path.dirname(os.path.dirname(vpy)))  # ...\Czysciciel
        env["HF_HOME"] = os.path.join(root, "hf_cache")
        args = [vpy, helper_script("worker.py"), fin, fout,
                "-p", opts["preset"],
                "--min-filler", f"{opts['minf']}",
                "--tryb", opts["tryb"],
                "--format", opts["fmt"],
                "--bitrate", f"{opts['bitrate']}",
                "--kanaly", opts["kanaly"],
                "--eksport", opts["eksport"]]
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", env=env,
                                creationflags=self._no_window())
        self._cur_proc = proc
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line.startswith("PROGRESS|"):
                try:
                    _, pct, msg = line.split("|", 2)
                    wx.CallAfter(self._progress, int(pct), msg)
                except Exception: pass
            elif line.startswith("LOG|"):
                wx.CallAfter(self.append_log, "  " + line[4:])
            elif line.startswith("DONE|"):
                pass
            elif line.startswith("ERR|"):
                wx.CallAfter(self.append_log, "  BŁĄD: " + line[4:])
            elif line.strip():
                wx.CallAfter(self.append_log, "  " + line)
        proc.wait()
        return proc.returncode

    def _python_for_helper(self, script):
        """Jaki Python odpala bootstrap. Frozen exe: uruchamiamy sam skrypt przez
        wbudowany interpreter (PyInstaller pakuje CPython). W dev: biezacy python."""
        if getattr(sys, "frozen", False):
            # frozen exe zawiera CPython - wywolanie 'exe skrypt.py' NIE zadziala,
            # dlatego bootstrap/worker uruchamiamy przez sys.executable z argumentem
            # trybu "runpy" ustawianym zmienna srodowiskowa (patrz launcher entry nizej).
            return [sys.executable, "--run-helper", script]
        return [sys.executable, script]

    def _no_window(self):
        return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

    # ---------------- callbacki UI (glowny watek) ----------------
    def _boot_progress(self, pct, msg):
        self.gauge.SetValue(max(0, min(100, pct)))
        self.set_status("Instalacja środowiska: " + msg)

    def _progress(self, pct, msg):
        self.gauge.SetValue(max(0, min(100, pct)))
        self.set_status(msg)

    def set_status(self, s):
        self.lbl_status.SetLabel(s)

    def append_log(self, s):
        self.log.AppendText(s + "\n")

    def _done(self, ok, done=0, total=0):
        self._set_running(False)
        self._cur_proc = None
        stopped = self.stop_flag.is_set()
        if stopped:
            self.set_status("Zatrzymano.")
            self.gauge.SetValue(0)
            wx.MessageBox(f"Przetwarzanie zatrzymane.\nUkończono {done} z {total} plików.",
                          APP_TITLE, wx.OK | wx.ICON_WARNING)
        elif ok:
            self.set_status("Gotowe.")
            self.gauge.SetValue(100)
            # pkt 5: alert w oknie dialogowym po przemieleniu calego wsadu
            if total and done < total:
                wx.MessageBox(
                    f"Zakończono z ostrzeżeniami.\nUdało się: {done} z {total} plików.\n"
                    "Szczegóły w dzienniku.",
                    APP_TITLE, wx.OK | wx.ICON_WARNING)
            else:
                msg = ("Gotowe! Przetworzono plik." if total == 1
                       else f"Gotowe! Przetworzono wszystkie pliki ({done} z {total}).")
                wx.MessageBox(msg, APP_TITLE, wx.OK | wx.ICON_INFORMATION)
        else:
            self.set_status("Zakończono z błędami.")
            self.gauge.SetValue(0)
            wx.MessageBox("Przetwarzanie zakończone błędem.\nSzczegóły w dzienniku.",
                          APP_TITLE, wx.OK | wx.ICON_ERROR)

    def on_close(self, evt):
        self.stop_flag.set()
        self._proc_kill()
        evt.Skip()


def main():
    app = wx.App(False)
    MainFrame()
    app.MainLoop()

if __name__ == "__main__":
    main()
