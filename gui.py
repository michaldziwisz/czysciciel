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
import os, sys, threading, subprocess, queue, time, json
import wx
import wx.lib.scrolledpanel as scrolled

# Mowa czytnika ekranu (ogloszenia postepu). Opcjonalna: gdy biblioteki nie ma,
# aplikacja dziala normalnie, tylko bez ogloszen - dlatego import w try.
try:
    import accessible_output2.outputs.auto as _ao2
    _MOWA = _ao2.Auto()
except Exception:
    _MOWA = None


def ogloszenie(tekst, przerwij=False):
    """Oglasza komunikat czytnikowi ekranu (NVDA/JAWS/SAPI).

    ZMIERZONE: wxPython nie potrafi ustawic obszaru "live" (UIA LiveSetting jest
    zawsze 0), a SetLabel na etykiecie statusu NIE JEST oglaszany samoczynnie.
    Bez tej funkcji osoba niewidoma nie wie, na jakim etapie jest przetwarzanie,
    dopoki sama nie przejdzie fokusem do statusu (WCAG 4.1.3).

    Wolamy TYLKO na kamieniach milowych (start i koniec pliku, koniec calosci,
    blad) - ogloszanie kazdego procentu byloby tak samo uciazliwe jak cisza.
    """
    if _MOWA is None or not tekst:
        return
    try:
        _MOWA.speak(tekst, interrupt=przerwij)
    except Exception:
        pass          # mowa nigdy nie moze wywalic przetwarzania


class NazwaDostepna(wx.Accessible):
    """Nadaje nazwe i opis dostepny kontrolce, ktora inaczej ich nie ma.

    POWOD (zmierzone na wxWidgets 3.3.3): dla wx.SpinCtrlDouble ANI SetName, ANI
    SetHelpText, ANI SetToolTip, ANI sasiadujaca etykieta NIE nadaja nazwy
    elementowi, ktory realnie dostaje fokus - czytnik odczytuje samo "0.30".
    SpinCtrlDouble jest kontenerem; fokus idzie na wewnetrzne wx.TextCtrl,
    wiec obiekt tej klasy trzeba ustawic WLASNIE na tym dziecku (patrz
    nazwij_pole_liczbowe). Referencje trzymamy w atrybucie okna, bo po zebraniu
    przez odsmiecacz nazwa przestaje dzialac.
    """

    def __init__(self, nazwa, opis=""):
        super().__init__()
        self._nazwa = nazwa
        self._opis = opis

    def GetName(self, childId):
        return (wx.ACC_OK, self._nazwa)

    def GetDescription(self, childId):
        if self._opis:
            return (wx.ACC_OK, self._opis)
        return (wx.ACC_NOT_IMPLEMENTED, "")

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
# Poziomy glosnosci docelowej. WAZNE: -23 to NORMA (EBU R128), -16/-14 to poziomy
# odtwarzania platform - opisy musza to rozrozniac, by nie wprowadzac usera w blad.
LUFS_WARTOSCI = ["-16", "-23", "-14"]
LUFS_OPISY = [
    "-16 LUFS - podcast (Apple)",
    "-23 LUFS - norma EBU R128 (radio, TV)",
    "-14 LUFS - Spotify",
]
# Sila odszumiania. Domyslnie DELIKATNIE: zmierzone, ze mocniejsze ustawienia kupuja
# marne 2.5 dB cichszego tla za 13.7 dB glebszej ingerencji w glos.
ODSZUM_WARTOSCI = [6, 12, 100]
ODSZUM_OPISY = [
    "delikatnie (zalecane)",
    "średnio",
    "mocno",
]
WARIANTY_RPP = [
    ("gotowy",      "Gotowy: fragmenty już wycięte i dosunięte"),
    ("przejrzenie", "Do przejrzenia: fragmenty oznaczone „WYTNIJ” (ripple)"),
    ("oba",         "Oba projekty naraz"),
]

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
        self._procs = set()             # aktywne procesy worker.py (kilka w trybie rownoleglym)
        self.runtime_python = None
        self.runtime_ffmpeg = None
        self._set_icon()
        self._build_ui()
        self._load_settings()                    # przywroc ustawienia z poprzedniej sesji
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

    def _nazwij_kontrolke(self, ctrl, nazwa, opis=""):
        """Nadaje nazwe dostepna kontrolce, ktorej SetName nie wystarcza.

        Dotyczy kontrolek opartych na natywnych klasach Windows (wx.ListCtrl ->
        SysListView32, wx.TextCtrl wielolinijkowy). ZMIERZONE: dla listy plikow
        SetName z tresc instrukcji byl w kodzie, a UIA i NVDA raportowaly nazwe
        PUSTA - czyli najwazniejsza kontrolka programu byla dla czytnika
        bezimienna. wx.Accessible ustawiony wprost na kontrolce to naprawia.
        """
        if not hasattr(self, "_akcesoria"):
            self._akcesoria = []
        ctrl.SetName(nazwa)
        a = NazwaDostepna(nazwa, opis)
        self._akcesoria.append(a)
        try:
            ctrl.SetAccessible(a)
        except Exception:
            pass
        if opis:
            try:
                ctrl.SetToolTip(opis)
            except Exception:
                pass

    def _nazwij_pole_liczbowe(self, ctrl, nazwa, opis=""):
        """Nadaje nazwe dostepna polu liczbowemu (wx.SpinCtrlDouble / wx.SpinCtrl).

        Ustawia wx.Accessible na WEWNETRZNYM wx.TextCtrl, bo to ono dostaje fokus
        i jego nazwe czyta czytnik ekranu. Sam SetName na kontrolce nie wystarcza
        (zmierzone: nazwa pusta, czytnik mowi tylko wartosc). Dla pewnosci
        ustawiamy takze na kontrolce nadrzednej - gdy przyszla wersja wxWidgets
        zmieni budowe kontrolki, nazwa nadal bedzie skad wziac.
        """
        if not hasattr(self, "_akcesoria"):
            self._akcesoria = []       # referencje: bez nich odsmiecacz zabiera obiekty
        # ZMIERZONE zachowanie dwoch typow pol liczbowych w wxWidgets 3.3.3:
        #  - wx.SpinCtrlDouble ma dzieci [TextCtrl, SpinButton]; fokus idzie na
        #    TextCtrl, wiec wx.Accessible ustawiony na TYM dziecku dziala i daje
        #    zarowno nazwe, jak i opis (NVDA czyta oba);
        #  - wx.SpinCtrl (calkowity) ma ZERO dzieci - jest jedna kontrolka
        #    natywna, ktora nazwe bierze z sasiedniej etykiety, a naszego
        #    wx.Accessible ignoruje. Tam opisu nie da sie podac osobno, wiec
        #    doklejamy go do NAZWY: lepiej dluzsza nazwa niz zgubiona informacja.
        dzieci_txt = [c for c in ctrl.GetChildren() if isinstance(c, wx.TextCtrl)]
        if dzieci_txt:
            cele = dzieci_txt + [ctrl]
            pelna_nazwa = nazwa
        else:
            cele = [ctrl]
            pelna_nazwa = f"{nazwa}. {opis}" if opis else nazwa
        ctrl.SetName(pelna_nazwa)
        for cel in cele:
            a = NazwaDostepna(pelna_nazwa, opis)
            self._akcesoria.append(a)
            try:
                cel.SetAccessible(a)
            except Exception:
                pass
        if opis:
            try:
                ctrl.SetToolTip(opis)     # widzacym tez sie przyda
            except Exception:
                pass

    def _build_ui(self):
        # Panel PRZEWIJANY, nie zwykly wx.Panel. Bez tego przy domyslnym rozmiarze
        # okna dolne kontrolki (przycisk uruchomienia, pasek postepu, status,
        # dziennik) byly przyciete albo w ogole nie powstawaly w drzewie
        # dostepnosci, a okno nie mialo paska przewijania - czyli nie bylo jak do
        # nich dotrzec wzrokiem ani powiekszeniem (WCAG 1.4.10, 1.4.4).
        panel = scrolled.ScrolledPanel(self, style=wx.TAB_TRAVERSAL)
        self.panel = panel
        # akcelerator: SetName wszedzie dla NVDA + StaticText PRZED kontrolka
        root = wx.BoxSizer(wx.VERTICAL)

        # --- lista plikow ---
        lbl_list = wx.StaticText(panel, label="Pli&ki do wyczyszczenia:")
        root.Add(lbl_list, 0, wx.LEFT | wx.TOP, 8)
        self.lst = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.lst.EnableCheckBoxes(True)
        self.lst.InsertColumn(0, "Plik", width=430)
        self.lst.InsertColumn(1, "Ścieżka", width=280)
        self._nazwij_kontrolke(
            self.lst,
            "Lista plików do wyczyszczenia",
            "Spacja zaznacza lub odznacza plik. Delete usuwa go z listy.")

        root.Add(self.lst, 1, wx.EXPAND | wx.ALL, 8)

        # przyciski listy
        row_btn = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_add = wx.Button(panel, label="&Dodaj pliki...")
        self.btn_addfolder = wx.Button(panel, label="Doda&j folder...")
        self.btn_del = wx.Button(panel, label="Usuń &z listy")
        self.btn_clear = wx.Button(panel, label="Wyczyść listę (Alt+&1)")
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
        lbl_mf = wx.StaticText(panel, label="M&inimalna długość fillera (s):")
        r2.Add(lbl_mf, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.sc_minfiller = wx.SpinCtrlDouble(panel, min=0.10, max=1.00, inc=0.05, initial=0.30)
        self.sc_minfiller.SetDigits(2)
        self._nazwij_pole_liczbowe(
            self.sc_minfiller,
            "Minimalna długość fillera w sekundach",
            "Krótsze wtrącenia są pomijane. Domyślnie 0,30 sekundy.")
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
        lbl_ch = wx.StaticText(panel, label="K&anały:")
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

        # wariant projektu Reapera (widoczny gdy eksport obejmuje reaper)
        self.rb_wariant = wx.RadioBox(panel, label="Wariant projektu Reapera",
                                      choices=[w[1] for w in WARIANTY_RPP],
                                      majorDimension=1, style=wx.RA_SPECIFY_COLS)
        self.rb_wariant.SetSelection(0)  # gotowy
        self.rb_wariant.SetName("Wariant projektu Reapera")
        root.Add(self.rb_wariant, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # opcja dodatkowa: osobny plik z wycietymi fragmentami (do odsluchu)
        self.cb_wyciete = wx.CheckBox(panel,
            label="Zapisz też osobny plik z tym, co wycięte, do odsłuchu (Alt+&2)")
        self._nazwij_kontrolke(
            self.cb_wyciete,
            "Zapisz osobny plik z wyciętymi fragmentami",
            "Powstaje dodatkowy plik z materiałem, który został usunięty. Do odsłuchu.")
        root.Add(self.cb_wyciete, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # ochrona muzyki: NIE wycinaj fillerow/pauz z fragmentow, w ktorych gra muzyka
        # (model fillerow myli spiew/instrumenty z "yyy"). Domyslnie WLACZONE.
        self.cb_muzyka = wx.CheckBox(panel,
            label="Pomijaj fragmenty z &muzyką (nie tnij, gdy gra muzyka)")
        self.cb_muzyka.SetValue(True)
        self.cb_muzyka.SetName("Pomijaj fragmenty z muzyką")
        root.Add(self.cb_muzyka, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # suwak czulosci wykrywania muzyki (0..100%). 100 = maks czulosc, chroni kazdy
        # slad muzyki; 0 = wylaczone, tnie wszedzie. Prog modelu = 1 - czulosc/100.
        rm = wx.BoxSizer(wx.HORIZONTAL)
        self.lbl_prog = wx.StaticText(panel, label="Czuł&ość wykrywania muzyki:")
        rm.Add(self.lbl_prog, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.sl_muzyka = wx.Slider(panel, value=50, minValue=0, maxValue=100,
                                   style=wx.SL_HORIZONTAL)
        self._nazwij_kontrolke(
            self.sl_muzyka,
            "Czułość wykrywania muzyki",
            "Wartość w procentach. 100 chroni najwięcej fragmentów, 0 wyłącza ochronę muzyki.")
        rm.Add(self.sl_muzyka, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.lbl_prog_val = wx.StaticText(panel, label="50%")
        self.lbl_prog_val.SetName("Wartość czułości")
        rm.Add(self.lbl_prog_val, 0, wx.ALIGN_CENTER_VERTICAL)
        root.Add(rm, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.sl_muzyka.Bind(wx.EVT_SLIDER, self.on_prog_change)
        self.cb_muzyka.Bind(wx.EVT_CHECKBOX, self.on_muzyka_toggle)

        # --- dodatkowe odglosy do wyciecia (chrzakniecia/kaszel, oddechy, mlasniecia) ---
        # Wykrywane tym samym modelem AudioSet co muzyka, wycinane jak fillery: tylko
        # tam gdzie NIE ma muzyki (podlegaja ochronie muzyki) ani mowy (guard). Bez
        # suwakow - Michal: "regulowac tu nie ma sensu". Domyslnie WYLACZONE.
        lbl_odg = wx.StaticText(panel, label="Dodatkowo wycinaj odgłosy (gdy nie ma muzyki):")
        root.Add(lbl_odg, 0, wx.LEFT | wx.TOP, 8)
        self.cb_chrzak = wx.CheckBox(panel,
            label="C&hrząknięcia, kaszel, kichnięcia")
        self._nazwij_kontrolke(self.cb_chrzak, "Wycinaj chrząknięcia, kaszel i kichnięcia")
        root.Add(self.cb_chrzak, 0, wx.LEFT | wx.RIGHT, 8)
        self.cb_oddech = wx.CheckBox(panel,
            label="Oddechy, wdechy, pociągnięcia nosem (Alt+&3)")
        self._nazwij_kontrolke(self.cb_oddech, "Wycinaj oddechy, wdechy i pociągnięcia nosem")
        root.Add(self.cb_oddech, 0, wx.LEFT | wx.RIGHT, 8)
        self.cb_mlask = wx.CheckBox(panel,
            label="Mlaśnięcia, cmoknięcia, kliknięcia ustne (Alt+&4)")
        self._nazwij_kontrolke(self.cb_mlask, "Wycinaj mlaśnięcia, cmoknięcia i kliknięcia ustne")
        root.Add(self.cb_mlask, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # --- CHRONIONE GLOSY (wzorce mowcy) ---
        # Automatyczne wykrywanie mowy syntetycznej jest NIEWYKONALNE (7 podejsc
        # zmierzonych i obalonych - patrz komentarz SPK_* w worker.py). Dziala tylko
        # nadzorowane "znajdz TEN glos": user dodaje probki glosow, ktore maja byc
        # nietkniete (np. czytnik ekranu w audycji o czytnikach). Michal: "wyciecie
        # 1-2 glosow syntetycznych to i tak mniej roboty niz ciecie pliku".
        box_gl = wx.StaticBox(panel, label="Chronione głosy (nie wycinaj z nich niczego)")
        sb_gl = wx.StaticBoxSizer(box_gl, wx.VERTICAL)
        lbl_gl = wx.StaticText(panel, label=
            "Dodaj próbkę głosu (np. czytnika ekranu), który ma zostać nietknięty. "
            "Wzorce działają też w kolejnych sesjach.")
        sb_gl.Add(lbl_gl, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
        # ListBox, nie ListCtrl: czytniki ekranu czytaja go bez dodatkowych zabiegow,
        # a lista jest jednokolumnowa (nazwa + spojnosc w jednym wierszu).
        self.lst_glosy = wx.ListBox(panel, size=(-1, 90), style=wx.LB_SINGLE)
        self._nazwij_kontrolke(
            self.lst_glosy,
            "Lista chronionych głosów",
            "Głosy z tej listy nie są nigdy wycinane. Wzorce działają też w kolejnych sesjach.")
        sb_gl.Add(self.lst_glosy, 1, wx.EXPAND | wx.ALL, 6)
        rg = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_gl_add = wx.Button(panel, label="Dodaj &głos z pliku...")
        self._nazwij_kontrolke(self.btn_gl_add, "Dodaj chroniony głos z pliku audio")
        rg.Add(self.btn_gl_add, 0, wx.RIGHT, 6)
        self.btn_gl_del = wx.Button(panel, label="Usuń głos (Alt+&5)")
        self._nazwij_kontrolke(self.btn_gl_del, "Usuń zaznaczony chroniony głos")
        rg.Add(self.btn_gl_del, 0)
        sb_gl.Add(rg, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        root.Add(sb_gl, 0, wx.EXPAND | wx.ALL, 8)
        self.btn_gl_add.Bind(wx.EVT_BUTTON, self.on_glos_add)
        self.btn_gl_del.Bind(wx.EVT_BUTTON, self.on_glos_del)
        self.glosy = []          # [{"nazwa":str,"wektor":[float],"spojnosc":float}]
        # stan poczatkowy: pusta lista => "Usun" nieaktywny (inaczej czytnik oglasza
        # aktywny przycisk, ktory nic nie robi). _load_settings wola to ponownie.
        self._glosy_odswiez()

        # --- OBROBKA DZWIEKU (opcjonalna, domyslnie WYLACZONA) ---
        # Obie funkcje ingeruja w brzmienie, wiec swiadomie startuja jako OFF.
        box_ob = wx.StaticBox(panel, label="Obróbka dźwięku (opcjonalna)")
        sb_ob = wx.StaticBoxSizer(box_ob, wx.VERTICAL)

        self.cb_norm = wx.CheckBox(panel, label="&Wyrównaj głośność (LUFS)")
        self._nazwij_kontrolke(
            self.cb_norm,
            "Wyrównaj głośność",
            "Wyrównuje głośność nagrania do wybranego poziomu. Nie zmienia dynamiki wypowiedzi.")
        sb_ob.Add(self.cb_norm, 0, wx.ALL, 4)
        r_lufs = wx.BoxSizer(wx.HORIZONTAL)
        self.lbl_lufs = wx.StaticText(panel, label="Poziom docelowy (Alt+&6):")
        r_lufs.Add(self.lbl_lufs, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.ch_lufs = wx.Choice(panel, choices=LUFS_OPISY)
        self.ch_lufs.SetSelection(0)
        self.ch_lufs.SetName("Poziom docelowy głośności")
        r_lufs.Add(self.ch_lufs, 0, wx.ALIGN_CENTER_VERTICAL)
        sb_ob.Add(r_lufs, 0, wx.LEFT | wx.BOTTOM, 20)

        self.cb_odszum = wx.CheckBox(panel, label="Odszum &nagranie")
        self._nazwij_kontrolke(
            self.cb_odszum,
            "Odszum nagranie",
            "Usuwa szum modelem DeepFilterNet. Przydatne przy nagraniach z szumem tła.")
        sb_ob.Add(self.cb_odszum, 0, wx.ALL, 4)
        r_ods = wx.BoxSizer(wx.HORIZONTAL)
        self.lbl_odszum = wx.StaticText(panel, label="Siła odszumiania (Alt+&7):")
        r_ods.Add(self.lbl_odszum, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.ch_odszum = wx.Choice(panel, choices=ODSZUM_OPISY)
        self.ch_odszum.SetSelection(0)
        self._nazwij_kontrolke(
            self.ch_odszum,
            "Siła odszumiania",
            "Delikatnie jest zalecane. Mocniejsze ustawienia bardziej ingerują w głos.")
        r_ods.Add(self.ch_odszum, 0, wx.ALIGN_CENTER_VERTICAL)
        sb_ob.Add(r_ods, 0, wx.LEFT | wx.BOTTOM, 20)
        root.Add(sb_ob, 0, wx.EXPAND | wx.ALL, 8)

        self.cb_norm.Bind(wx.EVT_CHECKBOX, self.on_norm_toggle)
        self.cb_odszum.Bind(wx.EVT_CHECKBOX, self.on_odszum_toggle)
        self.on_norm_toggle(None)
        self.on_odszum_toggle(None)

        # --- folder wyjsciowy ---
        r3 = wx.BoxSizer(wx.HORIZONTAL)
        lbl_out = wx.StaticText(panel, label="Fo&lder wyjściowy:")
        r3.Add(lbl_out, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.txt_out = wx.TextCtrl(panel)
        self.txt_out.SetName("Folder wyjściowy")
        self.txt_out.SetHint("domyślnie obok pliku wejściowego")
        r3.Add(self.txt_out, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.btn_out = wx.Button(panel, label="W&ybierz...")
        self.btn_out.SetName("Wybierz folder wyjściowy")
        r3.Add(self.btn_out, 0)
        root.Add(r3, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # --- liczba plikow przetwarzanych rownolegle (tryb wsadowy) ---
        # W tle: gdy jeden plik jest na GPU (detekcja), inny moze rownolegle konczyc
        # na CPU (enkode/ciecie) - karta obsluguje 1 plik naraz (zamek GPU w worker).
        # Wiecej = szybszy wsad, ale wiecej RAM/dysku (kazdy plik trzyma ~2GB WAV).
        rpar = wx.BoxSizer(wx.HORIZONTAL)
        lbl_par = wx.StaticText(panel, label="Przetwarzaj równolegle plików (Alt+&8):")
        rpar.Add(lbl_par, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        # SpinCtrlDouble z zerem cyfr po przecinku, NIE wx.SpinCtrl. Powod jest
        # wylacznie dostepnosciowy: wx.SpinCtrl to jedna kontrolka natywna bez
        # dzieci, ktora nazwe bierze z sasiedniej etykiety i ignoruje wlasny
        # wx.Accessible - nie da sie wiec dolozyc do niej OPISU dla czytnika
        # ekranu. SpinCtrlDouble ma wewnetrzne pole edycji, na ktorym opis dziala.
        # Zachowanie dla uzytkownika jest identyczne: wartosci calkowite 1-4.
        self.sc_workers = wx.SpinCtrlDouble(panel, min=1, max=4, inc=1, initial=2)
        self.sc_workers.SetDigits(0)
        self._nazwij_pole_liczbowe(
            self.sc_workers,
            "Liczba plików przetwarzanych równolegle",
            "Domyślnie 2. Karta graficzna obsługuje jeden plik naraz, "
            "reszta czeka na nią.")
        rpar.Add(self.sc_workers, 0, wx.ALIGN_CENTER_VERTICAL)
        root.Add(rpar, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # --- tryb dokladny (weryfikacja do oporu) ---
        # Domyslnie worker robi kilka rund domykajacych (do ~0.3s przyrostu). Tryb
        # dokladny iteruje az NIC nie zostanie do wyciecia - kosztem czasu.
        self.cb_dokladny = wx.CheckBox(panel,
            label="Tryb dokładny, weryfikuj do oporu — może wydłużyć przetwarzanie (Alt+&9)")
        self._nazwij_kontrolke(
            self.cb_dokladny,
            "Tryb dokładny",
            "Powtarza weryfikację, aż nic nie zostanie do wycięcia. Może wydłużyć przetwarzanie.")
        root.Add(self.cb_dokladny, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # --- start/stop ---
        r4 = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_start = wx.Button(panel, label="&Uruchom czyszczenie  (F5)")
        self.btn_start.SetName("Uruchom czyszczenie")
        self.btn_stop = wx.Button(panel, label="Za&trzymaj")
        self.btn_stop.SetName("Zatrzymaj")
        self.btn_stop.Disable()
        # ZMIERZONE: mnemonik dziala poprawnie w obu wariantach napisu. Wczesniej
        # wygladalo, ze Alt+R nie dziala, ale przyczyna byla inna - handler
        # cicho konczyl prace, gdy folder wyniku nie istnial (patrz on_open_out).
        self.btn_openout = wx.Button(panel, label="Otwó&rz folder wyniku")
        self.btn_openout.SetName("Otwórz folder wyniku")
        for b in (self.btn_start, self.btn_stop, self.btn_openout):
            r4.Add(b, 0, wx.RIGHT, 6)
        root.Add(r4, 0, wx.LEFT | wx.BOTTOM, 8)

        # --- pasek postepu ---
        self.gauge = wx.Gauge(panel, range=100)
        self._nazwij_kontrolke(self.gauge, "Postęp")
        root.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        self.lbl_status = wx.StaticText(panel, label="Gotowy.")
        self._nazwij_kontrolke(self.lbl_status, "Status")
        root.Add(self.lbl_status, 0, wx.ALL, 8)

        # --- dziennik ---
        lbl_log = wx.StaticText(panel, label="Dzi&ennik:")
        root.Add(lbl_log, 0, wx.LEFT, 8)
        self.log = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        self._nazwij_kontrolke(
            self.log,
            "Dziennik",
            "Pełny przebieg pracy programu. Tekst tylko do czytania.")
        root.Add(self.log, 1, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(root)
        # Wlacza przewijanie pionowe i liczy realny rozmiar zawartosci. Bez tego
        # ScrolledPanel zachowuje sie jak zwykly panel i tresc nadal jest ucieta.
        panel.SetupScrolling(scroll_x=False, scroll_y=True, scrollToTop=True)
        # Przewijanie MYSZKA i kolkiem jest dodatkiem - obsluga z klawiatury
        # dziala przez tabulacje (fokus sam przewija panel do kontrolki).

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
            except Exception as e:
                self.append_log("Nie mogę otworzyć folderu: " + d)
                self.set_status("Nie udało się otworzyć folderu")
                ogloszenie(f"Nie udało się otworzyć folderu. {e}", przerwij=True)
        else:
            # Bez tego przycisk po prostu NIC nie robil, gdy folder jeszcze nie
            # istnieje (np. przed pierwszym przetworzeniem albo gdy sciezka
            # zrodlowa zniknela). Cisza jest dla osoby niewidomej nieodroznialna
            # od zawieszenia programu, wiec zawsze musi pasc komunikat.
            self.append_log("Folder nie istnieje: " + d)
            self.set_status("Folder wyniku jeszcze nie istnieje")
            ogloszenie("Folder wyniku jeszcze nie istnieje. Pojawi się po "
                       "przetworzeniu pierwszego pliku.", przerwij=True)

    def on_format_change(self, evt):
        """Bitrate ma sens tylko dla formatow stratnych - wylacz liste dla bezstratnych."""
        stratny = FORMATY_OUT[self.ch_format.GetSelection()][2]
        self.ch_bitrate.Enable(stratny)
        self.lbl_bitrate.Enable(stratny)

    def on_prog_change(self, evt):
        """Etykieta czulosci w procentach obok suwaka."""
        self.lbl_prog_val.SetLabel(f"{self.sl_muzyka.GetValue()}%")

    def on_muzyka_toggle(self, evt):
        """Suwak progu aktywny tylko gdy ochrona muzyki wlaczona."""
        on = self.cb_muzyka.GetValue()
        for c in (self.sl_muzyka, self.lbl_prog, self.lbl_prog_val):
            c.Enable(on)

    def on_norm_toggle(self, evt):
        """Wybor poziomu LUFS aktywny tylko gdy normalizacja wlaczona."""
        on = self.cb_norm.GetValue()
        for c in (self.ch_lufs, self.lbl_lufs):
            c.Enable(on)

    def on_odszum_toggle(self, evt):
        """Wybor sily odszumiania aktywny tylko gdy odszumianie wlaczone."""
        on = self.cb_odszum.GetValue()
        for c in (self.ch_odszum, self.lbl_odszum):
            c.Enable(on)

    def on_eksport_change(self, evt):
        """Ustawienia audio widoczne gdy powstaje audio; wariant RPP - gdy powstaje reaper."""
        eksport = EKSPORTY[self.rb_eksport.GetSelection()][0]
        audio_potrzebne = eksport in ("audio", "oba")
        reaper_potrzebny = eksport in ("reaper", "oba")
        self.box_fmt.GetStaticBox().Show(audio_potrzebne)
        for c in (self.ch_format, self.ch_kanaly, self.ch_bitrate, self.lbl_bitrate):
            c.Show(audio_potrzebne)
        # plik z wycietymi ma sens tylko gdy powstaje audio
        self.cb_wyciete.Show(audio_potrzebne)
        self.cb_wyciete.Enable(audio_potrzebne)
        # wariant projektu Reapera - tylko gdy powstaje reaper
        self.rb_wariant.Show(reaper_potrzebny)
        self.rb_wariant.Enable(reaper_potrzebny)
        if audio_potrzebne:
            self.on_format_change(None)
        self.Layout()

    def on_about(self, evt):
        wx.MessageBox(
            APP_TITLE + " - czyszczenie audio z fillerów (yyy/eee) i nadmiarowych pauz.\n\n"
            "Model: classla/wav2vecbert2-filledPause (Apache-2.0).\n"
            "Wykrywanie muzyki: MIT/ast-finetuned-audioset (BSD-3-Clause).\n"
            "Silnik AI: PyTorch + Transformers. Audio: ffmpeg (LGPL), librosa, soundfile.\n\n"
            "Działa na karcie NVIDIA (szybciej) lub na procesorze.\n"
            "Środowisko instaluje się raz przy pierwszym uruchomieniu.",
            "O programie", wx.OK | wx.ICON_INFORMATION)

    # ---------------- chronione glosy ----------------
    def _glosy_odswiez(self):
        """Przerysuj liste. Spojnosc pokazujemy JAWNIE - zla probka (kilka glosow albo
        cisza) daje bezuzyteczny wzorzec, a user musi to widziec, nie zgadywac.
        Zmierzone: probka czystej syntezy 0.653, probka z domieszka innego glosu 0.440."""
        self.lst_glosy.Clear()
        for g in self.glosy:
            sp = g.get("spojnosc", 0.0)
            ost = "" if sp >= 0.6 else "  (słaba próbka - może zawierać inny głos)"
            self.lst_glosy.Append(f"{g['nazwa']} - jakość {sp:.2f}{ost}")
        self.btn_gl_del.Enable(bool(self.glosy))

    def on_glos_add(self, _evt):
        """Dodaj wzorzec z DOWOLNEGO formatu audio - dekodowanie ffmpegiem do PCM
        (jak reszta wejsc apki), wiec mp3/flac/m4a/ogg/wav dzialaja tak samo."""
        dlg = wx.FileDialog(self, "Wybierz próbkę głosu do ochrony",
                            wildcard=("Pliki audio|*.mp3;*.wav;*.flac;*.m4a;*.aac;*.ogg;"
                                      "*.opus;*.wma;*.mp4;*.mkv|Wszystkie pliki|*.*"),
                            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy(); return
        src = dlg.GetPath()
        dlg.Destroy()
        # liczenie embeddingu w WATKU - inaczej GUI zamarza (i czytnik milczy)
        self.btn_gl_add.Enable(False)
        self.set_status("Analizuję próbkę głosu...")
        ogloszenie("Analizuję próbkę głosu. To potrwa kilkanaście sekund.")
        threading.Thread(target=self._glos_worker, args=(src,), daemon=True).start()

    def _glos_worker(self, src):
        """Liczy wzorzec w osobnym PROCESIE (worker.py --wzorzec) - model glosow zyje
        w venv runtime, ktorego GUI (PyInstaller) nie ma we wlasnym interpreterze.
        Srodowisko ustawiane jak w _run_worker, inaczej worker nie znajdzie modelu."""
        try:
            vpy, ff = self._ensure_runtime()
            if not vpy:
                raise RuntimeError("środowisko nie jest gotowe")
            root = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                                APP_NAME)
            env = os.environ.copy()
            env["FFMPEG_BIN"] = ff
            env["SPK_MODEL"] = os.path.join(root, "model", "campplus.onnx")
            env["CZYSCICIEL_MODEL_DIR"] = os.path.join(root, "model")
            r = subprocess.run([vpy, helper_script("worker.py"), "--wzorzec", src],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=300, env=env,
                               creationflags=self._no_window())
            wek, spoj, err = None, 0.0, ""
            for ln in ((r.stdout or "") + (r.stderr or "")).splitlines():
                if ln.startswith("WZORZEC|"):
                    d = json.loads(ln[8:])
                    wek, spoj = d.get("wektor"), float(d.get("spojnosc", 0.0))
                elif ln.startswith("LOG|"):
                    err = ln[4:]
            if not wek:
                raise RuntimeError(err or "nie udało się policzyć wzorca głosu")
            wx.CallAfter(self._glos_gotowy, os.path.basename(src), wek, spoj)
        except Exception as e:
            wx.CallAfter(self._glos_blad, str(e))

    def _glos_gotowy(self, nazwa, wektor, spojnosc):
        self.glosy.append({"nazwa": nazwa, "wektor": wektor, "spojnosc": spojnosc})
        self._glosy_odswiez()
        self._save_settings()
        self.btn_gl_add.Enable(True)
        self.set_status(f"Dodano chroniony głos: {nazwa} (jakość {spojnosc:.2f})")
        # KAMIEN MILOWY: analiza probki trwa kilkanascie sekund, wiec jej wynik
        # musi byc oglaszany - inaczej uzytkownik nie wie, czy sie udalo.
        ogloszenie(f"Dodano chroniony głos: {nazwa}.")
        # focus na liste - user od razu slyszy, co dodal
        self.lst_glosy.SetSelection(len(self.glosy) - 1)
        self.lst_glosy.SetFocus()

    def _glos_blad(self, msg):
        self.btn_gl_add.Enable(True)
        self.set_status("Nie udało się dodać głosu")
        wx.MessageBox(f"Nie udało się przygotować wzorca głosu.\n\n{msg}\n\n"
                      "Wskazówka: próbka powinna zawierać co najmniej 4-5 sekund "
                      "samej mowy tego głosu, bez muzyki i bez innych osób.",
                      "Chronione głosy", wx.OK | wx.ICON_WARNING)

    def on_glos_del(self, _evt):
        i = self.lst_glosy.GetSelection()
        if i == wx.NOT_FOUND:
            return
        nazwa = self.glosy[i]["nazwa"]
        del self.glosy[i]
        self._glosy_odswiez()
        self._save_settings()
        self.set_status(f"Usunięto chroniony głos: {nazwa}")
        if self.glosy:
            self.lst_glosy.SetSelection(min(i, len(self.glosy) - 1))
        self.lst_glosy.SetFocus()

    def _zapisz_glosy_tmp(self):
        """Wzorce do pliku JSON dla workera (wektory sa za dlugie na argv)."""
        if not self.glosy:
            return ""
        p = os.path.join(runtime_root(), "chronione_glosy.json")
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"wzorce": self.glosy}, f)
            return p
        except Exception:
            return ""

    # ---------------- persystencja ustawien ----------------
    def _settings_path(self):
        return os.path.join(runtime_root(), "settings.json")

    def _collect_settings(self):
        """Ustawienia uzytkownika do zapamietania miedzy sesjami. NIE zapisujemy listy
        plikow (celowo - za kazdym razem inna)."""
        return {
            "preset": self.ch_preset.GetSelection(),
            "minfiller": self.sc_minfiller.GetValue(),
            "tryb": self.rb_tryb.GetSelection(),
            "eksport": self.rb_eksport.GetSelection(),
            "format": self.ch_format.GetSelection(),
            "kanaly": self.ch_kanaly.GetSelection(),
            "bitrate": self.ch_bitrate.GetSelection(),
            "wariant": self.rb_wariant.GetSelection(),
            "zapisz_wyciete": self.cb_wyciete.GetValue(),
            "omijaj_muzyke": self.cb_muzyka.GetValue(),
            "prog_muzyki": self.sl_muzyka.GetValue(),
            # int(): SpinCtrlDouble zwraca float (2.0), a dalej liczba jest uzywana
            # jako liczba procesow i trafia do zapisu ustawien
            "workers": int(self.sc_workers.GetValue()),
            "dokladny": self.cb_dokladny.GetValue(),
            "tnij_chrzak": self.cb_chrzak.GetValue(),
            "tnij_oddech": self.cb_oddech.GetValue(),
            "tnij_mlask": self.cb_mlask.GetValue(),
            "normalizuj": self.cb_norm.GetValue(),
            "lufs": self.ch_lufs.GetSelection(),
            "odszum": self.cb_odszum.GetValue(),
            "odszum_sila": self.ch_odszum.GetSelection(),
            "outdir": self.txt_out.GetValue().strip(),
            # CHRONIONE GLOSY: wzorce zapamietane miedzy sesjami - glos czytnika jest
            # zawsze ten sam, wiec raz dodany dziala na kolejne odcinki.
            "glosy": self.glosy,
        }

    def _save_settings(self):
        try:
            import json
            os.makedirs(runtime_root(), exist_ok=True)
            with open(self._settings_path(), "w", encoding="utf-8") as f:
                json.dump(self._collect_settings(), f, ensure_ascii=False, indent=1)
        except Exception:
            pass   # zapis ustawien nie moze nigdy wywalic aplikacji

    def _load_settings(self):
        """Przywraca ustawienia z poprzedniej sesji. Uszkodzony/niepelny plik jest
        ignorowany po cichu - kazdy klucz osobno, wiec brak jednego nie psuje reszty."""
        try:
            import json
            with open(self._settings_path(), encoding="utf-8") as f:
                s = json.load(f)
        except Exception:
            return
        def _sel(ctrl, key, n):
            v = s.get(key)
            if isinstance(v, int) and 0 <= v < n: ctrl.SetSelection(v)
        def _val(ctrl, key, typ):
            v = s.get(key)
            if isinstance(v, typ): ctrl.SetValue(v)
        _sel(self.ch_preset, "preset", len(PRESETY))
        _sel(self.rb_tryb, "tryb", len(TRYBY))
        _sel(self.rb_eksport, "eksport", len(EKSPORTY))
        _sel(self.ch_format, "format", len(FORMATY_OUT))
        _sel(self.ch_kanaly, "kanaly", 3)
        _sel(self.ch_bitrate, "bitrate", len(BITRATE_LISTA))
        _sel(self.rb_wariant, "wariant", len(WARIANTY_RPP))
        _val(self.sc_minfiller, "minfiller", (int, float))
        _val(self.cb_wyciete, "zapisz_wyciete", bool)
        _val(self.cb_muzyka, "omijaj_muzyke", bool)
        _val(self.sl_muzyka, "prog_muzyki", int)
        # (int, float): kontrolka jest teraz SpinCtrlDouble (patrz komentarz przy
        # jej tworzeniu), wiec zapisana wartosc moze byc int albo float
        _val(self.sc_workers, "workers", (int, float))

        _val(self.cb_dokladny, "dokladny", bool)
        _val(self.cb_chrzak, "tnij_chrzak", bool)
        _val(self.cb_oddech, "tnij_oddech", bool)
        _val(self.cb_mlask, "tnij_mlask", bool)
        # CHRONIONE GLOSY: kazdy wzorzec sprawdzany osobno - uszkodzony wpis nie moze
        # wywalic wczytywania calej listy ani ustawien.
        self.glosy = []
        for g in (s.get("glosy") or []):
            try:
                v = [float(x) for x in (g.get("wektor") or [])]
                if len(v) >= 64 and g.get("nazwa"):
                    self.glosy.append({"nazwa": str(g["nazwa"]), "wektor": v,
                                       "spojnosc": float(g.get("spojnosc", 0.0))})
            except Exception:
                continue
        self._glosy_odswiez()
        _val(self.cb_norm, "normalizuj", bool)
        _sel(self.ch_lufs, "lufs", len(LUFS_WARTOSCI))
        _val(self.cb_odszum, "odszum", bool)
        _sel(self.ch_odszum, "odszum_sila", len(ODSZUM_WARTOSCI))
        outdir = s.get("outdir")
        if isinstance(outdir, str) and outdir:
            self.txt_out.SetValue(outdir)
        # odswiez stany zalezne (bitrate wg formatu, widocznosc RPP, suwak wg muzyki)
        self.on_format_change(None)
        self.on_eksport_change(None)
        self.on_muzyka_toggle(None)
        self.on_norm_toggle(None)
        self.on_odszum_toggle(None)

    # ---------------- uruchomienie ----------------
    def _set_running(self, running):
        for b in (self.btn_start, self.btn_add, self.btn_addfolder, self.btn_del,
                  self.btn_clear, self.btn_out, self.ch_preset, self.sc_minfiller,
                  self.rb_tryb, self.rb_eksport, self.ch_format, self.ch_kanaly,
                  self.ch_bitrate, self.cb_wyciete, self.cb_muzyka, self.sl_muzyka,
                  self.rb_wariant, self.sc_workers, self.cb_dokladny,
                  self.cb_chrzak, self.cb_oddech, self.cb_mlask,
                  self.cb_norm, self.ch_lufs, self.cb_odszum, self.ch_odszum):
            b.Enable(not running)
        if not running:
            self.on_format_change(None)   # przywroc poprawny stan bitrate
            self.on_eksport_change(None)  # przywroc widocznosc/aktywnosc opcji audio
            self.on_muzyka_toggle(None)   # suwak progu wg stanu checkboxa muzyki
            self.on_norm_toggle(None)     # lista LUFS wg stanu checkboxa normalizacji
            self.on_odszum_toggle(None)   # lista sily wg stanu checkboxa odszumiania
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
        zapisz_wyciete = self.cb_wyciete.GetValue() and eksport in ("audio", "oba")
        omijaj_muzyke = self.cb_muzyka.GetValue()
        # suwak = czulosc 0..100%; prog modelu odwrotnie: 100% czulosci -> prog 0.0
        prog_muzyki = 1.0 - self.sl_muzyka.GetValue() / 100.0
        wariant_rpp = WARIANTY_RPP[self.rb_wariant.GetSelection()][0]
        outdir = self.txt_out.GetValue().strip() or None
        workers = int(self.sc_workers.GetValue())   # SpinCtrlDouble zwraca float
        dokladny = self.cb_dokladny.GetValue()
        tnij_chrzak = self.cb_chrzak.GetValue()
        tnij_oddech = self.cb_oddech.GetValue()
        tnij_mlask = self.cb_mlask.GetValue()
        normalizuj = self.cb_norm.GetValue()
        lufs = LUFS_WARTOSCI[self.ch_lufs.GetSelection()]
        odszum = self.cb_odszum.GetValue()
        odszum_sila = ODSZUM_WARTOSCI[self.ch_odszum.GetSelection()]
        opts = dict(preset=preset, minf=minf, tryb=tryb, eksport=eksport,
                    fmt=fmt, bitrate=bitrate, kanaly=kanaly, outdir=outdir,
                    zapisz_wyciete=zapisz_wyciete, wariant_rpp=wariant_rpp,
                    omijaj_muzyke=omijaj_muzyke, prog_muzyki=prog_muzyki, workers=workers,
                    dokladny=dokladny, tnij_chrzak=tnij_chrzak, tnij_oddech=tnij_oddech,
                    tnij_mlask=tnij_mlask, normalizuj=normalizuj, lufs=lufs,
                    odszum=odszum, odszum_sila=odszum_sila,
                    glosy_json=self._zapisz_glosy_tmp())
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
        # KAMIEN MILOWY: start pracy. Przerywamy biezaca wypowiedz, bo to reakcja
        # na swiadome dzialanie uzytkownika i ma dojsc od razu.
        ogloszenie(f"Rozpoczynam czyszczenie. Plików: {len(files)}.", przerwij=True)

    def on_stop(self, evt):
        self.stop_flag.set()
        self.append_log("Zatrzymywanie — przerywam bieżące pliki...")
        self._proc_kill()

    def _proc_kill(self):
        """Ubija WSZYSTKIE aktywne procesy worker.py (tryb rownolegly). Zamek GPU
        zwalnia sie sam gdy proces ginie (blokada plikowa OS)."""
        for p in list(self._procs):
            try:
                if p.poll() is None: p.terminate()
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
            n = len(files); workers = max(1, min(int(opts.get("workers", 2)), n))

            # stan wspoldzielony przez rownolegle workery (aktualizacja statusu dla NVDA)
            self._n_total = n
            self._slots = {}          # etykieta pliku -> ostatni komunikat etapu
            self._done_cnt = 0
            def process_one(idx, f):
                if self.stop_flag.is_set():
                    return None
                base = os.path.basename(f); stem = os.path.splitext(base)[0]
                od = opts["outdir"] or os.path.dirname(f)
                os.makedirs(od, exist_ok=True)
                out = os.path.join(od, stem + "_czysty." + ext)
                label = f"[{idx+1}/{n}] {base}"
                tag = f"[{idx+1}/{n}]"
                wx.CallAfter(self.append_log, f"{tag} start: {base}")
                rc = self._run_worker(vpy, ff, f, out, opts, label)
                if audio_out:
                    good = rc == 0 and os.path.exists(out) and os.path.getsize(out) > 50000
                else:
                    rpp = os.path.join(od, stem + ".RPP")
                    good = rc == 0 and os.path.exists(rpp) and os.path.getsize(rpp) > 100
                wx.CallAfter(self._file_done, tag, base, good, label)
                return good

            ok = 0
            if workers == 1:
                for idx, f in enumerate(files):
                    if self.stop_flag.is_set(): break
                    if process_one(idx, f): ok += 1
            else:
                from concurrent.futures import ThreadPoolExecutor, as_completed
                wx.CallAfter(self.append_log,
                             f"Tryb równoległy: {workers} pliki naraz "
                             f"(karta obsługuje jeden na raz, reszta czeka).")
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futs = {ex.submit(process_one, idx, f): idx for idx, f in enumerate(files)}
                    for fut in as_completed(futs):
                        if fut.result(): ok += 1
            wx.CallAfter(self.append_log, f"Zakończono. Sukces: {ok}/{n}")
            wx.CallAfter(self._done, True, ok, n)
        except Exception as e:
            import traceback
            wx.CallAfter(self.append_log, "BŁĄD krytyczny: " + repr(e))
            for ln in traceback.format_exc().splitlines():
                wx.CallAfter(self.append_log, "  " + ln)
            wx.CallAfter(self._done, False, 0, 0)

    def _file_done(self, tag, base, good, label):
        """Wpis koncowy pliku + aktualizacja licznika/statusu (glowny watek)."""
        self.append_log(f"{tag} {'OK' if good else 'BŁĄD'}: {base}")
        self._done_cnt = getattr(self, "_done_cnt", 0) + 1
        self._slots.pop(label, None)
        self._render_status()
        # KAMIEN MILOWY: koniec pliku - oglaszamy czytnikowi ekranu, bo sama
        # zmiana etykiety statusu nie jest oglaszana samoczynnie (WCAG 4.1.3).
        n = getattr(self, "_n_total", 0)
        stan = "gotowy" if good else "błąd"
        if n > 1:
            ogloszenie(f"Plik {self._done_cnt} z {n} {stan}: {base}")
        else:
            ogloszenie(f"Plik {stan}: {base}")

    def _render_status(self):
        """Status zbiorczy dla czytnika: ile gotowe + co aktualnie w toku."""
        n = getattr(self, "_n_total", 0)
        done = getattr(self, "_done_cnt", 0)
        wtoku = "; ".join(f"{lab.split(']')[0]}]: {msg}" for lab, msg in self._slots.items())
        s = f"Gotowe {done}/{n}." + (f" W toku — {wtoku}" if wtoku else "")
        self.set_status(s)
        if n:
            self.gauge.SetValue(max(0, min(100, int(done * 100 / n))))

    def _ensure_runtime(self):
        """Odpala bootstrap.py minimalnym Pythonem (frozen: sys.executable z flaga)."""
        cmd = self._python_for_helper(helper_script("bootstrap.py"))
        vpy = ff = None
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                creationflags=self._no_window())
        self._procs.add(proc)           # rejestr do ubijania (bootstrap tez reaguje na STOP)
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
        self._procs.discard(proc)
        if proc.returncode != 0 or not vpy:
            wx.CallAfter(self.append_log, "Nie udało się przygotować środowiska.")
            return None, None
        return vpy, ff

    def _run_worker(self, vpy, ff, fin, fout, opts, label):
        tag = label.split("]")[0] + "]" if "]" in label else label   # np. "[7/53]"
        env = os.environ.copy()
        env["FFMPEG_BIN"] = ff
        # root = %LOCALAPPDATA%\Czysciciel (NIE licz dirname od vpy - vpy jest 4 poziomy
        # gleboko: ...\Czysciciel\runtime\venv\Scripts\python.exe; wczesniej 3x dirname
        # dawalo ...\runtime\model = zla sciezka -> model nieznaleziony -> pad offline).
        root = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP_NAME)
        env["HF_HOME"] = os.path.join(root, "hf_cache")
        env["CZYSCICIEL_MODEL_DIR"] = os.path.join(root, "model")  # plaski katalog modelu
        env["CZYSCICIEL_MUSIC_MODEL_DIR"] = os.path.join(root, "music_model")  # model muzyki (AST)
        env["DFN_BIN"] = os.path.join(root, "tools", "deep-filter.exe")  # odszumianie (opcjonalne)
        env["VAD_MODEL"] = os.path.join(root, "model", "silero_vad.onnx")  # guard mowy (Silero)
        env["SPK_MODEL"] = os.path.join(root, "model", "campplus.onnx")  # chronione glosy (CAMPPlus)
        args = [vpy, helper_script("worker.py"), fin, fout,
                "-p", opts["preset"],
                "--min-filler", f"{opts['minf']}",
                "--tryb", opts["tryb"],
                "--format", opts["fmt"],
                "--bitrate", f"{opts['bitrate']}",
                "--kanaly", opts["kanaly"],
                "--eksport", opts["eksport"],
                "--wariant-rpp", opts.get("wariant_rpp", "gotowy")]
        if opts.get("zapisz_wyciete"):
            args.append("--zapisz-wyciete")
        # domyslnie chronimy muzyke; flaga workera WYLACZA ochrone
        if not opts.get("omijaj_muzyke", True):
            args.append("--bez-omijania-muzyki")
        else:
            args += ["--prog-muzyki", f"{opts.get('prog_muzyki', 0.50)}"]
        if opts.get("dokladny"):
            args.append("--dokladny")
        if opts.get("tnij_chrzak"):
            args.append("--tnij-chrzakniecia")
        if opts.get("tnij_oddech"):
            args.append("--tnij-oddechy")
        if opts.get("tnij_mlask"):
            args.append("--tnij-mlasniecia")
        # obrobka dzwieku (obie opcje domyslnie wylaczone)
        if opts.get("odszum"):
            args += ["--odszum", "--odszum-sila", f"{opts.get('odszum_sila', 6)}"]
        if opts.get("normalizuj"):
            args += ["--normalizuj", "--lufs", opts.get("lufs", "-16")]
        # CHRONIONE GLOSY: sciezka do JSON z wzorcami (wektory za dlugie na argv)
        if opts.get("glosy_json"):
            args += ["--chronione-glosy", opts["glosy_json"]]
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", env=env,
                                creationflags=self._no_window())
        self._procs.add(proc)                 # rejestr do ubijania przy STOP (kilka naraz)
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                if line.startswith("PROGRESS|"):
                    try:
                        _, pct, msg = line.split("|", 2)
                        # PROGRESS tyka co kawalek - za czesto na dziennik. Ląduje w
                        # slocie statusu (etap biezacego pliku), pasek liczy ukonczone.
                        wx.CallAfter(self._slot_update, label, msg)
                    except Exception: pass
                elif line.startswith("LOG|"):
                    # LOG = kamienie milowe (remux, detekcja, ile fillerow). Na ZYWO
                    # do dziennika z tagiem pliku - inaczej przy dlugich plikach cisza.
                    wx.CallAfter(self.append_log, f"{tag} {line[4:]}")
                elif line.startswith("DONE|"):
                    pass
                elif line.startswith("ERR|"):
                    wx.CallAfter(self.append_log, f"{tag} BŁĄD: {line[4:]}")
                elif line.strip():
                    wx.CallAfter(self.append_log, f"{tag} {line}")
        finally:
            proc.wait()
            self._procs.discard(proc)
        return proc.returncode

    def _slot_update(self, label, msg):
        """Zapamietaj etap biezacego pliku i odswiez status zbiorczy (glowny watek)."""
        if hasattr(self, "_slots"):
            self._slots[label] = msg
            self._render_status()


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
        # Pierwsze uruchomienie pobiera kilka gigabajtow. Oglaszamy PROGI co 25 %,
        # nie kazda aktualizacje - inaczej czytnik mowilby bez przerwy.
        prog = (pct // 25) * 25
        if prog > getattr(self, "_ostatni_prog_boot", -1) and prog > 0:
            self._ostatni_prog_boot = prog
            ogloszenie(f"Instalacja środowiska: {prog} procent.")

    def set_status(self, s):
        self.lbl_status.SetLabel(s)

    def append_log(self, s):
        self.log.AppendText(s + "\n")

    def _done(self, ok, done=0, total=0):
        self._set_running(False)
        self._procs.clear()
        stopped = self.stop_flag.is_set()
        if stopped:
            self.set_status("Zatrzymano.")
            self.gauge.SetValue(0)
            # Ogloszenie PRZED okienkiem: komunikat modalny i tak zabierze fokus,
            # ale dzieki temu wynik jest slyszalny takze wtedy, gdy uzytkownik
            # pracuje w innym oknie i wroci pozniej.
            ogloszenie(f"Zatrzymano. Ukończono {done} z {total} plików.", przerwij=True)
            wx.MessageBox(f"Przetwarzanie zatrzymane.\nUkończono {done} z {total} plików.",
                          APP_TITLE, wx.OK | wx.ICON_WARNING)
        elif ok:
            self.set_status("Gotowe.")
            self.gauge.SetValue(100)
            # pkt 5: alert w oknie dialogowym po przemieleniu calego wsadu
            if total and done < total:
                ogloszenie(f"Zakończono z ostrzeżeniami. Udało się {done} z {total} plików.",
                           przerwij=True)
                wx.MessageBox(
                    f"Zakończono z ostrzeżeniami.\nUdało się: {done} z {total} plików.\n"
                    "Szczegóły w dzienniku.",
                    APP_TITLE, wx.OK | wx.ICON_WARNING)
            else:
                msg = ("Gotowe! Przetworzono plik." if total == 1
                       else f"Gotowe! Przetworzono wszystkie pliki ({done} z {total}).")
                ogloszenie(msg, przerwij=True)
                wx.MessageBox(msg, APP_TITLE, wx.OK | wx.ICON_INFORMATION)
        else:
            self.set_status("Zakończono z błędami.")
            self.gauge.SetValue(0)
            ogloszenie("Przetwarzanie zakończone błędem. Szczegóły w dzienniku.",
                       przerwij=True)
            wx.MessageBox("Przetwarzanie zakończone błędem.\nSzczegóły w dzienniku.",
                          APP_TITLE, wx.OK | wx.ICON_ERROR)

    def on_close(self, evt):
        self._save_settings()
        self.stop_flag.set()
        self._proc_kill()
        evt.Skip()


def main():
    app = wx.App(False)
    MainFrame()
    app.MainLoop()

if __name__ == "__main__":
    main()
