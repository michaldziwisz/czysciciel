#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Czysciciel - worker (silnik czyszczenia audio z fillerow yyy/eee i nadmiarowych pauz).

Uruchamiany przez GUI jako podproces w srodowisku runtime (venv dociagniety przy
pierwszym starcie). Moze tez dzialac samodzielnie z linii polecen.

Protokol postepu na STDOUT (parsowany przez GUI; kazda linia osobno):
  PROGRESS|<0..100>|<krotki opis etapu>
  LOG|<linia dziennika dla czlowieka>
  DONE|<sciezka_wyjscia>
  ERR|<komunikat bledu>

Etapy:
  1. remux wejscia do czystego WAV (pelna jakosc, ffmpeg)
  2. detekcja fillerow modelem CLASSLA (GPU fp16 jesli jest karta, inaczej CPU)
  3. detekcja pauz do skrocenia
  4. strumieniowe wyciecie w pelnej jakosci + crossfade
  5. eksport MP3 (+ opcjonalnie projekt Reapera .RPP)

Uzycie:
  python worker.py <wejscie> [wyjscie.mp3] [-p preset] [--bez-pauz]
                   [--min-filler S] [--rpp] [--zostaw-wav]
"""
import os, re, sys, json, shutil, subprocess, time, bisect

# Windows: dzieci (ffmpeg) BEZ wlasnego okna konsoli. GUI odpala worker z
# CREATE_NO_WINDOW, ale flaga nie propaguje sie na wnuki - kazdy subprocess.run
# ffmpeg bez niej migalby czarna konsola przy remuxie/eksporcie.
if os.name == "nt":
    _NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    _SI = subprocess.STARTUPINFO()
    _SI.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _SI.wShowWindow = 0  # SW_HIDE
    def _win_kw():
        return {"creationflags": _NO_WINDOW, "startupinfo": _SI}
else:
    def _win_kw():
        return {}

# UTF-8 na stdout (Windows konsola/pipe) - inaczej polskie znaki w logu sie sypia
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# HF_HOME i ffmpeg ustawia GUI/bootstrap przez zmienne srodowiskowe zanim nas odpali.
os.environ.setdefault("HF_HOME", os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Czysciciel", "hf_cache"))
# TRYB OFFLINE: model jest juz pobrany raz przez bootstrap. Bez tego transformers
# przy KAZDYM starcie laczy sie z HuggingFace, by sprawdzic ETag/nowsza wersje -
# powoduje pauze "cos pobiera z HF" i pada bez internetu. Wymuszamy uzycie cache.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
from itertools import pairwise

MODEL = "classla/wav2vecbert2-filledPause"
# Model klasyfikacji audio (AudioSet) do wykrywania MUZYKI. Uzywany, by NIE wycinac
# fillerow/pauz z fragmentow, w ktorych gra muzyka (spiew, jingle, podklad) - model
# fillerow myli przeciagniete dzwieki muzyczne z "yyy". AST: 527 klas AudioSet,
# multilabel (sigmoid). Interesuje nas prawdopodobienstwo klasy "Music".
MUSIC_MODEL = "MIT/ast-finetuned-audioset-10-10-0.4593"
# Sciezka do PLASKIEGO katalogu modelu (bootstrap pobiera go tam przez local_dir).
# GUI ustawia CZYSCICIEL_MODEL_DIR; niezaleznie liczymy tez domyslna z LOCALAPPDATA
# (odpornosc na blad sciezki w GUI - kandydatow probujemy po kolei).
def _model_candidates(subdir="model", env_key="CZYSCICIEL_MODEL_DIR"):
    cands = []
    md = os.environ.get(env_key, "").strip()
    if md: cands.append(md)
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    cands.append(os.path.join(base, "Czysciciel", subdir))
    return cands
def _model_ref():
    """Zwraca (sciezka/repo, local_files_only). Preferuj lokalny plaski katalog;
    sprawdz WSZYSTKICH kandydatow (env + domyslny), inaczej spadnij na repo-id."""
    for d in _model_candidates():
        if os.path.exists(os.path.join(d, "preprocessor_config.json")):
            return d, True
    return MODEL, True
def _music_model_ref():
    """Jak _model_ref, ale dla modelu wykrywania muzyki (AST)."""
    for d in _model_candidates(subdir="music_model", env_key="CZYSCICIEL_MUSIC_MODEL_DIR"):
        if os.path.exists(os.path.join(d, "preprocessor_config.json")):
            return d, True
    return MUSIC_MODEL, True
SR = 16000; CHUNK = 30.0; FS = 0.020
OVERLAP = 3.0              # nakladka okien detekcji fillerow (s) - filler na granicy
                          # 30s jest w calosci w sasiednim oknie (najdluzszy ~1.6s)
CUT = 0.30                 # min dlugosc fillera
KEEP = 0.50; TARGET = 0.45 # pauzy: do KEEP zostaw, dluzsze skroc do TARGET
SAFE_MS = 20; XF_MS = 25
# --- wykrywanie muzyki (AST/AudioSet) ---
MUSIC_WIN = 4.0            # dlugosc okna analizy muzyki (s) - rozdzielczosc detekcji
MUSIC_THRESH = 0.50        # prog prawdopodobienstwa klasy "Music" (sigmoid)
MUSIC_PAD = 0.30           # margines rozszerzenia regionu muzyki (s) w kazda strone

# --- wykrywanie ODGLOSOW do wyciecia (chrzakniecia/kaszel, oddechy, mlasniecia) ---
# Uzywa TEGO SAMEGO modelu AST (AudioSet) co ochrona muzyki. Skanuje gestszym oknem
# (lepsza lokalizacja krotkich zdarzen) i wycina okna, w ktorych dany odglos jest
# obecny A JEDNOCZESNIE NIE MA w nich mowy (p(Speech) < guard). Dzieki temu usuwamy
# tylko odglosy wystepujace SAMODZIELNIE (w przerwach) - jak fillery - a nie tniemy
# glosu. Wykryte ciecia przechodza pozniej przez ten sam filtr muzyki (poza muzyka).
SOUND_WIN = 1.0            # dlugosc okna analizy odglosow (s)
SOUND_HOP = 0.5            # krok okna (nakladka dla lepszej lokalizacji)
SOUND_PAD = 0.05           # margines rozszerzenia regionu odglosu (s) - maly, by nie
                           # zjadac poczatku sasiedniego slowa
SOUND_THRESH = 0.15        # prog sigmoid dla klas odglosu (AudioSet dla tych klas bywa
                           # niski, wiec prog nizszy niz przy muzyce) - do kalibracji uchem
SOUND_SPEECH_GUARD = 0.15  # prog p(mowa) uznajacy okno za "mowa" (do sasiedztwa nizej)
SOUND_SPEECH_CTX = 0.3     # tnij odglos tylko gdy w promieniu tylu sekund NIE ma mowy
                           # (odglos IZOLOWANY w przerwie). Chroni koncowki slow (szum
                           # glosek s/sz/f przyklejony do samogloski) i poczatki wypowiedzi;
                           # maly promien, by lapac oddechy blisko zdan (oddech przed/po).
# nazwy klas AudioSet per kategoria (dopasowanie po nazwie w id2label modelu)
SOUND_CLASSES = {
    "chrzakniecia": ["Throat clearing", "Cough", "Sneeze", "Snort"],
    "oddechy":      ["Breathing", "Gasp", "Sigh", "Sniff", "Pant", "Wheeze"],
    "mlasniecia":   ["Chewing, mastication", "Biting", "Clicking"],
}
SPEECH_CLASSES = ["Speech", "Male speech, man speaking",
                  "Female speech, woman speaking", "Child speech, kid speaking"]

# --- komunikacja z GUI ---
def emit(kind, payload):
    print(f"{kind}|{payload}", flush=True)

def progress(pct, msg):
    emit("PROGRESS", f"{int(pct)}|{msg}")

def log(m):
    emit("LOG", f"[{time.strftime('%H:%M:%S')}] {m}")

def _ffmpeg_bin():
    """Sciezka do ffmpeg: zmienna FFMPEG_BIN (ustawia bootstrap) albo 'ffmpeg' z PATH."""
    return os.environ.get("FFMPEG_BIN", "ffmpeg")

# --- ciezkie importy dopiero gdy liczymy ---
def _load_light():
    """Lekkie zaleznosci (numpy + soundfile) BEZ torcha/transformers. GUI wola
    `wzorzec_glosu` przy dodawaniu probki glosu - nie ma po co ladowac 2 GB modeli
    i zajmowac GPU tylko po to, zeby policzyc jeden embedding na CPU.
    UWAGA: `import x` w bloku `if` przypisuje do LOKALNEJ nazwy nawet przy `global`
    zadeklarowanym wyzej - dlatego importujemy bezwarunkowo (idempotentne, tanie)."""
    global np, sf
    import numpy as np
    import soundfile as sf

def _load_heavy():
    global np, torch, sf, librosa, pd, AutoFeatureExtractor, Wav2Vec2BertForAudioFrameClassification
    global ASTForAudioClassification
    import numpy as np, torch, soundfile as sf, librosa
    import pandas as pd
    from transformers import AutoFeatureExtractor, Wav2Vec2BertForAudioFrameClassification
    from transformers import ASTForAudioClassification

# ---------- MIEDZYPROCESOWY ZAMEK NA GPU ----------
# Tryb wsadowy GUI uruchamia kilka workerow rownolegle, by czas CPU (enkode,
# ciecie) jednego pliku nakladal sie na czas GPU (detekcja) nastepnego. Ale karta
# jest jedna - dwa modele naraz = OutOfMemory. Ten zamek gwarantuje, ze tylko
# JEDEN proces liczy na GPU w danej chwili; reszta czeka. Trzymany przez CALY
# region detekcji (fillery+muzyka), wiec enkode poprzedniego pliku nachodzi na
# detekcje nastepnego. Blokada plikowa OS zwalnia sie AUTOMATYCZNIE gdy proces
# ginie (STOP/crash) - brak ryzyka zakleszczenia po ubiciu workera.
def _gpu_lock_path():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "Czysciciel")
    try: os.makedirs(d, exist_ok=True)
    except Exception: pass
    return os.path.join(d, "gpu.lock")

class GpuLock:
    """Kontekst: wejscie czeka az GPU bedzie wolne, wyjscie zwalnia. Na CPU no-op."""
    def __enter__(self):
        self.fd = None
        if not torch.cuda.is_available():
            return self                      # brak wspoldzielonej karty - zamek zbedny
        self.fd = open(_gpu_lock_path(), "a+")
        try:                                 # upewnij sie ze jest 1 bajt do zablokowania
            self.fd.seek(0, os.SEEK_END)
            if self.fd.tell() == 0: self.fd.write("L"); self.fd.flush()
        except Exception: pass
        waited = False
        while True:
            try:
                self.fd.seek(0)
                if os.name == "nt":
                    import msvcrt; msvcrt.locking(self.fd.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl; fcntl.flock(self.fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if not waited:
                    log("czekam na GPU (inny plik jest teraz na karcie)..."); waited = True
                time.sleep(0.5)
    def __exit__(self, *exc):
        if self.fd is None: return
        try:
            self.fd.seek(0)
            if os.name == "nt":
                import msvcrt; msvcrt.locking(self.fd.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl; fcntl.flock(self.fd.fileno(), fcntl.LOCK_UN)
        except Exception: pass
        try: self.fd.close()
        except Exception: pass
        self.fd = None

# ---------- FILLERY ----------
def f2i(frames, off):
    """Zamienia predykcje ramkowe (0/filler) na interwaly (a,b) w sekundach + offset.
    NIE odrzuca juz interwalow dotykajacych krawedzi OKNA - przy nakladce (overlap)
    filler przeciety granica jest widziany w calosci przez sasiednie okno, a duplikaty
    scalamy pozniej. Odrzucamy tylko za krotkie (< CUT)."""
    res = []; ndf = pd.DataFrame({"t": [FS*i for i in range(len(frames))], "f": frames}).dropna()
    idx = ndf.f.diff()[ndf.f.diff() != 0].index.values
    for si, ei in pairwise(idx):
        if ndf.loc[si:ei-1, "f"].mode()[0] != 0:
            res.append((round(ndf.loc[si, "t"], 3), round(ndf.loc[ei, "t"], 3)))
    res = [i for i in res if i[1]-i[0] >= CUT]
    return [(a+off, b+off) for a, b in res]

def _merge_intervals(iv, gap=0.05):
    """Scala nakladajace sie / stykajace interwaly (a,b). Konieczne przy nakladce
    okien - ten sam filler bywa wykryty w dwoch sasiednich oknach."""
    if not iv: return []
    iv = sorted(iv)
    out = [list(iv[0])]
    for a, b in iv[1:]:
        if a <= out[-1][1] + gap: out[-1][1] = max(out[-1][1], b)
        else: out.append([a, b])
    return [(a, b) for a, b in out]

_FILLER_MODEL = None
def _get_filler_model():
    """Laduje model fillerow RAZ (cache modulowy). Iteracja domykajaca wola detekcje
    wielokrotnie - bez cache przeladowywalaby model z dysku w kazdej rundzie."""
    global _FILLER_MODEL
    if _FILLER_MODEL is None:
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        half = (dev == "cuda")
        log(f"model na {dev}{' fp16' if half else ''}")
        ref, lfo = _model_ref()
        fe = AutoFeatureExtractor.from_pretrained(ref, local_files_only=lfo)
        model = Wav2Vec2BertForAudioFrameClassification.from_pretrained(
            ref, torch_dtype=torch.float16 if half else torch.float32,
            local_files_only=lfo).to(dev)
        model.eval()
        _FILLER_MODEL = (fe, model, dev, half)
    return _FILLER_MODEL

_AST_MODEL = None
def _get_ast_model():
    """Laduje model AST (AudioSet) RAZ i buduje mape nazwa_klasy -> indeks.
    Wspoldzielony przez ochrone MUZYKI i wycinanie ODGLOSOW (chrzakniecia/oddechy/
    mlasniecia) - inaczej ladowalibysmy ten sam ~350MB model dwa razy. Zwraca
    (fe, model, dev, half, label2idx) albo rzuca wyjatek (obslugiwany przez wolajacego)."""
    global _AST_MODEL
    if _AST_MODEL is None:
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        half = (dev == "cuda")
        ref, lfo = _music_model_ref()
        log(f"model odgłosów/muzyki (AST) na {dev}{' fp16' if half else ''}")
        fe = AutoFeatureExtractor.from_pretrained(ref, local_files_only=lfo)
        model = ASTForAudioClassification.from_pretrained(
            ref, torch_dtype=torch.float16 if half else torch.float32,
            local_files_only=lfo).to(dev)
        model.eval()
        id2label = getattr(model.config, "id2label", {}) or {}
        label2idx = {}
        for k, v in id2label.items():
            label2idx[str(v).strip().lower()] = int(k)
        _AST_MODEL = (fe, model, dev, half, label2idx)
    return _AST_MODEL

def detect_fillers(y_full, p_lo=20, p_hi=70, verbose=True):
    fe, model, dev, half = _get_filler_model()
    # NAKLADKA (overlap): okna nachodza na siebie o OVERLAP s, wiec filler przeciety
    # granica okna jest w calosci wewnatrz sasiedniego okna - inaczej gubimy fillery
    # na szwach co CHUNK s (objaw: drugi przebieg czyszczenia dokladal kolejne).
    iv = []; step = int(CHUNK*SR); hop = int((CHUNK-OVERLAP)*SR); n = len(y_full)
    nch = (n+hop-1)//hop
    for ci, cs in enumerate(range(0, n, hop)):
        ch = y_full[cs:cs+step]
        if len(ch) < int(0.5*SR): continue
        try:
            with torch.no_grad():
                inp = fe([ch], return_tensors="pt", sampling_rate=SR).to(dev)
                if half: inp = {k: (v.half() if v.dtype == torch.float32 else v) for k, v in inp.items()}
                pred = model(**inp).logits.float().argmax(-1)[0].cpu().numpy()
        except torch.cuda.OutOfMemoryError:
            log(f"OOM na kawalku {ci}, fallback CPU dla niego")
            torch.cuda.empty_cache()
            with torch.no_grad():
                inp = fe([ch], return_tensors="pt", sampling_rate=SR)
                m_cpu = model.float().cpu()
                pred = m_cpu(**inp).logits.argmax(-1)[0].numpy()
                model.to(dev)
                if half: model.half()
        iv += f2i(pred.tolist(), cs/SR)
        progress(p_lo + (p_hi-p_lo)*(ci+1)/max(nch, 1), f"Detekcja fillerow: {ci+1}/{nch}")
        if verbose and ci % 20 == 0: log(f"  fillery: kawalek {ci+1}/{nch}")
    # scal duplikaty z nakladki + odrzuc fillery na SKRAJNYCH krawedziach materialu
    # (poczatek 0.0 i sam koniec - tam nie ma kontekstu, model bywa niepewny)
    iv = _merge_intervals(iv)
    dur = n/SR
    iv = [(a, b) for a, b in iv if a > 0.0 and b < dur - 0.02]
    return iv

# ---------- PAUZY ----------
def detect_pauses(y):
    HOP = 160; FRAME = 400
    rms = librosa.feature.rms(y=y, frame_length=FRAME, hop_length=HOP)[0]
    db = 20*np.log10(np.maximum(rms, 1e-8)); speech = db > -38.0
    change = [0]; cur = speech[0]
    for k in range(1, len(speech)):
        if speech[k] != cur: change.append(k); cur = speech[k]
    change.append(len(speech))
    cuts = []
    for j in range(len(change)-1):
        a, b = change[j], change[j+1]; ta, tb = a*HOP/SR, b*HOP/SR
        if speech[a]: continue
        if tb-ta <= KEEP: continue
        ke = TARGET/2; ca = ta+ke; cb = tb-ke
        if cb-ca > 0.02: cuts.append((ca, cb))
    return cuts

# ---------- MUZYKA (AST/AudioSet) ----------
def detect_music(y_full):
    """Zwraca liste (start_s, end_s) regionow, w ktorych gra MUZYKA (na osi oryginalu).
    Analiza oknami MUSIC_WIN sekund modelem AST (AudioSet, multilabel). Dla kazdego
    okna liczymy sigmoid logitow i bierzemy prawdopodobienstwo klasy 'Music'; okno z
    p >= MUSIC_THRESH oznaczamy jako muzyke. Sasiednie muzyczne okna scalamy, region
    rozszerzamy o MUSIC_PAD z kazdej strony (zeby zlapac naboki/wybrzmienia). Blad
    ladowania modelu = brak muzyki (pusta lista) - lepiej wyczyscic niz pasc."""
    try:
        fe, model, dev, half, label2idx = _get_ast_model()
        progress(72, "Ładowanie modelu wykrywania muzyki...")
        # indeks klasy "Music" z mapy etykiet modelu. Fallback: 137 (AudioSet).
        music_idx = label2idx.get("music")
        if music_idx is None:
            music_idx = 137
            log("nie znaleziono etykiety 'Music' w modelu - uzywam indeksu 137")
    except Exception as e:
        log(f"model muzyki niedostepny ({e!r}) - pomijam wykrywanie muzyki")
        return []
    step = int(MUSIC_WIN * SR); n = len(y_full)
    nwin = (n + step - 1) // step
    flags = []  # (start_s, end_s, is_music)
    for wi, ws in enumerate(range(0, n, step)):
        ch = y_full[ws:ws+step]
        if len(ch) < int(0.5*SR):
            flags.append((ws/SR, (ws+len(ch))/SR, False)); continue
        try:
            with torch.no_grad():
                inp = fe([ch], return_tensors="pt", sampling_rate=SR).to(dev)
                if half: inp = {k: (v.half() if v.dtype == torch.float32 else v) for k, v in inp.items()}
                logits = model(**inp).logits.float()[0]
                p_music = torch.sigmoid(logits)[music_idx].item()
        except torch.cuda.OutOfMemoryError:
            torch.cuda.empty_cache()
            with torch.no_grad():
                inp = fe([ch], return_tensors="pt", sampling_rate=SR)
                m_cpu = model.float().cpu()
                logits = m_cpu(**inp).logits.float()[0]
                p_music = torch.sigmoid(logits)[music_idx].item()
                model.to(dev)
                if half: model.half()
        flags.append((ws/SR, (ws+len(ch))/SR, p_music >= MUSIC_THRESH))
        progress(72 + 4*(wi+1)/max(nwin, 1), f"Wykrywanie muzyki: {wi+1}/{nwin}")
    # scal sasiednie muzyczne okna w regiony + margines
    regions = []
    for (a, b, ismus) in flags:
        if not ismus: continue
        a = max(0.0, a - MUSIC_PAD); b = b + MUSIC_PAD
        if regions and a <= regions[-1][1]:
            regions[-1][1] = max(regions[-1][1], b)
        else:
            regions.append([a, b])
    total_music = sum(b-a for a, b in regions)
    log(f"muzyka: {len(regions)} region(ow), lacznie {total_music/60:.1f} min")
    return [(a, b) for a, b in regions]

# ---------- ODGLOSY: chrzakniecia/kaszel, oddechy, mlasniecia (AST/AudioSet) ----------
def detect_sounds(y_full, kategorie):
    """Zwraca liste ciec {a,b,dur,typ} dla wybranych KATEGORII odglosow (na osi
    oryginalu). Uzywa tego samego modelu AST co detect_music. Dla kazdego okna
    (SOUND_WIN, krok SOUND_HOP) liczy sigmoid logitow; okno kwalifikuje sie do
    wyciecia gdy MAX prawdopodobienstwo klas danej kategorii >= SOUND_THRESH ORAZ
    p(mowa) < SOUND_SPEECH_GUARD (chronimy glos - tniemy tylko odglosy wystepujace
    samodzielnie w przerwach, jak fillery). Sasiednie okna tej samej kategorii scala
    w regiony + margines SOUND_PAD. Blad modelu = pusta lista (worker nie pada).
    kategorie: lista kluczy z SOUND_CLASSES (np. ['chrzakniecia','oddechy']).
    """
    kategorie = [k for k in kategorie if k in SOUND_CLASSES]
    if not kategorie:
        return []
    try:
        fe, model, dev, half, label2idx = _get_ast_model()
        progress(76, "Ładowanie modelu wykrywania odgłosów...")
    except Exception as e:
        log(f"model odgłosów niedostepny ({e!r}) - pomijam wykrywanie odgłosów")
        return []
    # indeksy klas per kategoria + indeksy mowy (guard)
    cat_idx = {}
    for kat in kategorie:
        idxs = [label2idx[nm.lower()] for nm in SOUND_CLASSES[kat] if nm.lower() in label2idx]
        if idxs:
            cat_idx[kat] = idxs
    if not cat_idx:
        log("nie znaleziono etykiet odgłosów w modelu - pomijam")
        return []
    speech_idx = [label2idx[nm.lower()] for nm in SPEECH_CLASSES if nm.lower() in label2idx]
    win = int(SOUND_WIN * SR); hop = int(SOUND_HOP * SR); n = len(y_full)
    nwin = (n + hop - 1) // hop
    # 1. przelot: per okno zapisz p(mowa) i kandydatow odglosu (a,b,kat). Decyzje o
    # cieciu podejmujemy w 2. przelocie, bo potrzebujemy KONTEKSTU (mowa obok).
    speech_win = []              # (a_s, b_s, p_speech) dla kazdego okna
    cand = []                    # (a_s, b_s, kat) - okno przekroczylo prog odglosu
    for wi, ws in enumerate(range(0, n, hop)):
        ch = y_full[ws:ws+win]
        if len(ch) < int(0.3*SR):
            continue
        try:
            with torch.no_grad():
                inp = fe([ch], return_tensors="pt", sampling_rate=SR).to(dev)
                if half: inp = {k: (v.half() if v.dtype == torch.float32 else v) for k, v in inp.items()}
                probs = torch.sigmoid(model(**inp).logits.float()[0])
        except torch.cuda.OutOfMemoryError:
            torch.cuda.empty_cache()
            with torch.no_grad():
                inp = fe([ch], return_tensors="pt", sampling_rate=SR)
                m_cpu = model.float().cpu()
                probs = torch.sigmoid(m_cpu(**inp).logits.float()[0])
                model.to(dev)
                if half: model.half()
        a_s, b_s = ws/SR, (ws+len(ch))/SR
        p_speech = max((probs[i].item() for i in speech_idx), default=0.0)
        speech_win.append((a_s, b_s, p_speech))
        for kat, idxs in cat_idx.items():
            if max(probs[i].item() for i in idxs) >= SOUND_THRESH:
                cand.append((a_s, b_s, kat))
        if wi % 40 == 0:
            progress(76 + 2*(wi+1)/max(nwin, 1), f"Wykrywanie odgłosów: {wi+1}/{nwin}")
    # regiony MOWY (okna z p_speech >= guard) - do sprawdzania sasiedztwa kandydatow
    speech_reg = [(a, b) for (a, b, ps) in speech_win if ps >= SOUND_SPEECH_GUARD]
    def _mowa_obok(a, b):
        """Czy w promieniu SOUND_SPEECH_CTX od [a,b] jest jakies okno z mowa?"""
        lo, hi = a - SOUND_SPEECH_CTX, b + SOUND_SPEECH_CTX
        for (sa, sb) in speech_reg:
            if sa < hi and sb > lo:
                return True
        return False
    # 2. przelot: zostaw tylko odglosy IZOLOWANE (bez mowy w sasiedztwie) - chroni
    # koncowki slow (s/sz/f) i poczatki wypowiedzi, gdzie mowa jest tuz obok.
    hits = {kat: [] for kat in cat_idx}
    for (a_s, b_s, kat) in cand:
        if not _mowa_obok(a_s, b_s):
            hits[kat].append((a_s, b_s))
    # scal okna kazdej kategorii w regiony + margines, zbuduj ciecia
    out = []
    typ_nazwa = {"chrzakniecia": "chrząknięcie", "oddechy": "oddech", "mlasniecia": "mlaśnięcie"}
    for kat, iv in hits.items():
        if not iv:
            continue
        iv.sort()
        regions = []
        for a, b in iv:
            a = max(0.0, a - SOUND_PAD); b = b + SOUND_PAD
            if regions and a <= regions[-1][1]:
                regions[-1][1] = max(regions[-1][1], b)
            else:
                regions.append([a, b])
        for a, b in regions:
            out.append({"a": a, "b": b, "dur": b-a, "typ": typ_nazwa.get(kat, kat)})
        log(f"odgłosy [{typ_nazwa.get(kat, kat)}]: {len(regions)} fragment(ow)")
    return out

def _in_music(a, b, music_regions):
    """Czy odcinek [a,b] (s) NACHODZI na ktorykolwiek region muzyki."""
    for (ma, mb) in music_regions:
        if a < mb and b > ma:
            return True
    return False

def filter_cuts_by_music(allc, music_regions):
    """Usuwa z listy ciec te, ktore wpadaja w muzyke (zachowujemy muzyke nietknieta).
    Zwraca (kept, removed_count)."""
    if not music_regions:
        return allc, 0
    kept = [c for c in allc if not _in_music(c["a"], c["b"], music_regions)]
    return kept, len(allc) - len(kept)

# ---------- GUARD MOWY: Silero VAD (chroni glos przed wycinaniem ODGLOSOW) ----------
# PROBLEM (Michal, 07.08.2026): narzedzie wycinalo mowe SYNTETYCZNA (czytnik ekranu),
# a on robi o niej materialy - to TRESC, nie zaklocenie.
# PRZYCZYNA: model AST (AudioSet 2017) nie rozpoznaje dzisiejszego neuronowego TTS jako
# mowy. Zmierzone: mediana p(mowa) 0.259 na TTS vs 0.542-0.575 na ludzkim glosie, 43.5%
# okien ponizej SOUND_SPEECH_GUARD. Zamiast mowy AST widzi Gasp 0.65 / Snort 0.59 /
# Biting 0.61 - czyli DOKLADNIE klasy wycinane jako odglosy.
# CZEGO NIE PROBOWAC (wszystko ZMIERZONE i odrzucone):
#  1. klasa "Speech synthesizer" w SPEECH_CLASSES: p ma mediane 0.001, max 0.138 ->
#     niechronione okna 43.5% -> 43.5%, ZERO zmiany.
#  2. podniesienie progu: musialby byc >=0.76, a ludzki glos ma mediane 0.542 - chronilby
#     zwykle nagrania i apka przestalaby wycinac fillery.
#  3. heurystyka "okno ma dzwiek, ale AST nie widzi mowy" (poprzednie detect_tts): AST jest
#     wobec TTS NIESTABILNY (ta sama synteza: 0.020 -> 0.729 -> 0.507 w kolejnych sekundach),
#     wiec regiony byly dziurawe i ciecia przeciskaly sie przez luki. Michal slyszal to
#     przy 1:42, 2:55, 3:32, 3:57. Scalanie luk pomagalo tylko czesciowo.
# ROZWIAZANIE (spostrzezenie Michala: "przeciez nie jest ani muzyka, ani cisza, bardziej
# przypomina mowe"): uzyc DETEKTORA MOWY, nie klasyfikatora dzwiekow. Silero VAD (MIT,
# 2.3 MB ONNX) wykrywa AKTYWNOSC MOWY niezaleznie od tego, czy ludzka czy syntetyczna.
# Zmierzone na materiale Michala: 176 s p=0.991, 217 s p=0.992, 242 s p=0.996 - czyli
# dokladnie tam, gdzie AST zawodzil. Na ludzkim nagraniu 91% okien. RTF 0.005 (5h23m ~
# 1.6 min, wobec 80 min DeepFilterNet). onnxruntime NIE koliduje z torchem apki
# (sprawdzone: torch 2.7.0+cu128 + CUDA dziala; UWAGA - pip silero-vad dociaga
# torchaudio 2.11, ktore ZAWIESZA import torch. Dlatego czysty ONNX, bez pakietu pip).
# ZAKRES (decyzja Michala, wariant 1): guard dotyczy TYLKO ODGLOSOW (chrzakniecia/oddechy/
# mlasniecia). NIE fillerow - bo ZMIERZONE: Silero uznaje 76% fillerow (19/25) za mowe
# (srednie p=0.503, mediana max 0.840), wiec guard na fillerach zablokowalby 3/4 ciec
# i zabil podstawowa funkcje apki. Fillery zostaja przy swoim dedykowanym modelu
# (wav2vecbert2-filledPause), ktory dziala poprawnie takze na syntezie.
VAD_SR = 16000         # Silero v5 dziala WYLACZNIE w 16 kHz
VAD_WIN = 512          # ...i wymaga DOKLADNIE 512 probek na okno (32 ms)
VAD_CTX = 64           # ...ORAZ 64 probek kontekstu z poprzedniego okna, doklejanych
                       # z przodu (OnnxWrapper.__call__: `cat([self._context, x])`).
                       # BEZ TEGO MODEL ZWRACA ~0.000 DLA WSZYSTKIEGO, takze ludzkiej mowy -
                       # popelnilem ten blad i omal nie uznalem, ze Silero nie dziala.
VAD_THRESH = 0.5       # p(mowa) >= tyle => okno jest mowa (wartosc domyslna Silero)
VAD_PAD = 0.20         # margines wokol regionu mowy (s) - chroni koncowki wypowiedzi
VAD_MERGE_GAP = 0.35   # scal regiony mowy rozdzielone krotsza przerwa (oddech miedzy slowami)

_vad_sess = None

# ---------- CHRONIONE GLOSY (wzorce mowcy, CAMPPlus ONNX) ----------
# PROBLEM: model fillerow uznaje mowe SYNTETYCZNA (czytnik ekranu) za "yyy" i ja wycina,
# a dla Michala to TRESC audycji. Automatycznego wykrywania syntezy NIE DA SIE zrobic -
# 7 podejsc zmierzonych i obalonych (klasa AudioSet p=0.001; koniunkcja VAD+AST 0.556 vs
# 0.556; cechy sygnalowe AUC 0.79; model antispoof AUC 0.209 - uznawal WSZYSTKO za synteze;
# prog pewnosci fillerow 0.950 vs 0.947; podglosnienie - ekstraktor normalizuje wejscie;
# embeddingi mowcy bez nadzoru 0.710 vs 0.683). Szczegoly w skillu.
# ROZWIAZANIE (pomysl Michala): user DODAJE WZORCE glosow do ochrony ("wyciecie 1-2 glosow
# syntetycznych to i tak mniej roboty niz ciecie pliku"). Zadanie zmienia sie z
# "rozpoznaj synteze" (niewykonalne) na "znajdz TEN glos" - a do tego CAMPPlus jest
# trenowany. ZMIERZONE: wzorce chronia 100% swoich okien, 4.3% materialu, 0.0% obcego
# ludzkiego nagrania. Dwa wskazane syntezatory maja podobienstwo 0.158 = rozne glosy,
# wiec jeden wzorzec NIE wystarczy (dlatego LISTA wzorcow).
SPK_SR = 16000         # CAMPPlus dziala w 16 kHz
SPK_WIN = 2.0          # okno na jeden embedding (s)
SPK_HOP = 0.5          # KROK okna. ZMIERZONE: przy kroku = SPK_WIN (bez zachodzenia)
                       # przecieklo 20% syntezy - ciecia fillerow trwaja 0.3-1 s, wiec
                       # krotki fragment albo lezacy na GRANICY okna dawal mieszanke
                       # glos+synteza i podobienstwo spadalo pod prog. Krok 0.5 s dal
                       # 29 trafien vs 8 na tym samym materiale (3.6x).
SPK_MEL = 80           # fbank 80 pasm (wymog modelu)
SPK_THRESH = 0.30      # prog STARTU regionu chronionego (podobienstwo po normalizacji
                       # kanalu). NIE obnizac: przy 0.20 pokrycie syntezy 100%, ale
                       # falszywa ochrona ludzkiej mowy skacze do 10.6% (apka nie tnie).
SPK_THRESH_LO = 0.25   # prog KONTYNUACJI (histereza). Synteza to CIAGLY blok jednego
                       # glosu, wiec gdy region JUZ sie zaczal, sasiednie okna wystarcza
                       # ze sa "podobne", nie "pewne". ZMIERZONE: sam prog 0.30 lapal
                       # tylko 80% okien syntezy (co piate przeciekalo i tam ciecia
                       # lecialy normalnie - Michal to slyszal). Histereza domyka bloki
                       # NIE ruszajac progu startu. ZMIERZONE (prog startu 0.30):
                       #   lo 0.30 (brak histerezy): synteza 80%, ludzki 2.3%
                       #   lo 0.25 (TU):             synteza 91%, ludzki 3.9%
                       #   lo 0.20:                  synteza 95%, ludzki 6.5%
                       # Wybrane 0.25 - najlepsze pokrycie przy falszywej ochronie <=5%.
                       # Ponizej 0.20 pokrycie NIE rosnie, a ludzki dalej sie psuje.
SPK_PAD = 0.75         # margines regionu chronionego (s); 0.3 s bylo za waskie
SPK_MERGE_GAP = 1.5    # scal chronione regiony rozdzielone krotsza luka
SPK_MIN_RMS = 0.005    # ciszy nie ma sensu porownywac (nie ma barwy glosu)

_spk_sess = None

def _get_spk():
    """Sesja ONNX modelu embeddingow mowcy (CAMPPlus, 27 MB, Apache-2.0).
    Zwraca None gdy modelu/onnxruntime brak - wtedy ochrona glosow jest po prostu
    nieaktywna (log mowi GDZIE szukalem; cichy brak funkcji to blad, ktory juz
    popelnilem przy VAD)."""
    global _spk_sess
    if _spk_sess is not None:
        return _spk_sess or None
    try:
        import onnxruntime as ort
    except Exception as e:
        log(f"onnxruntime niedostepny ({e!r}) - ochrona glosow wylaczona")
        _spk_sess = False
        return None
    kand = [os.environ.get("SPK_MODEL")] + [os.path.join(d, "campplus.onnx")
                                            for d in _model_candidates()]
    for p in [k for k in kand if k]:
        if os.path.exists(p):
            try:
                _spk_sess = ort.InferenceSession(p, providers=["CPUExecutionProvider"])
                log(f"model glosow: {os.path.basename(p)}")
                return _spk_sess
            except Exception as e:
                log(f"blad ladowania modelu glosow ({e!r})")
    log("model glosow nie znaleziony (szukalem w: %s) - ochrona glosow wylaczona"
        % ", ".join(str(k) for k in kand if k))
    _spk_sess = False
    return None

def _spk_fbank(seg):
    """80-pasmowy fbank + CMN, zgodnie z 3D-Speaker. Bez ditheringu (powtarzalnosc)."""
    import kaldi_native_fbank as knf
    o = knf.FbankOptions()
    o.frame_opts.samp_freq = SPK_SR
    o.frame_opts.dither = 0.0
    o.frame_opts.snip_edges = True
    o.mel_opts.num_bins = SPK_MEL
    f = knf.OnlineFbank(o)
    f.accept_waveform(SPK_SR, (seg * 32768.0).tolist())
    f.input_finished()
    fr = [f.get_frame(i) for i in range(f.num_frames_ready)]
    if not fr:
        return None
    X = np.array(fr, dtype=np.float32)
    return X - X.mean(axis=0, keepdims=True)

def _spk_embed(y16, sess, co="glosy", hop=None):
    """Embeddingi (192D, znormalizowane) per okno SPK_WIN, krokiem `hop` (domyslnie
    SPK_HOP). Okna ZACHODZA na siebie - bez tego przeciekalo 20% syntezy (krotkie
    fragmenty i granice okien mieszaly sie z glosem sasiada)."""
    win = int(SPK_WIN * SPK_SR)
    krok = int((hop if hop else SPK_HOP) * SPK_SR)
    out, poz = [], []
    idx = list(range(0, max(0, len(y16) - win + 1), krok))
    for k, i in enumerate(idx):
        seg = y16[i:i + win]
        if float(np.sqrt(np.mean(seg ** 2))) < SPK_MIN_RMS:
            continue
        X = _spk_fbank(seg)
        if X is None:
            continue
        try:
            e = sess.run(None, {sess.get_inputs()[0].name: X[None, :, :]})[0][0]
        except Exception as e_:
            log(f"blad modelu glosow ({e_!r}) - ochrona przerwana")
            return np.zeros((0, 0)), []
        out.append(e / (np.linalg.norm(e) + 1e-9))
        poz.append(i / SPK_SR)
        if idx and k % 400 == 0:
            progress(78 + 2 * (k + 1) / len(idx), f"Wykrywanie {co}: {k+1}/{len(idx)}")
    return np.array(out, dtype=np.float64), poz

def wzorzec_glosu(src, start=None, dur=None):
    """Wzorzec z DOWOLNEGO formatu audio (mp3/flac/m4a/wav...) - dekodowanie ffmpegiem
    do PCM 16 kHz mono, jak reszta wejsc aplikacji. Opcjonalnie wycinek [start, start+dur].
    Zwraca (wektor 192D, spojnosc_probki) albo (None, 0.0).
    Spojnosc < 0.6 oznacza, ze probka zawiera WIECEJ NIZ JEDEN glos albo cisze -
    GUI pokazuje ja userowi, bo zla probka daje bezuzyteczny wzorzec."""
    sess = _get_spk()
    if sess is None:
        return None, 0.0
    # numpy/soundfile: nazwy modulowe powstaja dopiero w _load_light/_load_heavy, wiec
    # NIE testuj `np is None` - przed pierwszym ladowaniem nazwa nie istnieje (NameError).
    # _load_light jest idempotentne i tanie.
    _load_light()
    tmp = os.path.join(os.environ.get("TEMP") or "/tmp", "_czysc_wzorzec.wav")
    cmd = [_ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", str(float(start))]
    if dur:
        cmd += ["-t", str(float(dur))]
    cmd += ["-i", src, "-ar", str(SPK_SR), "-ac", "1", "-c:a", "pcm_s16le", tmp]
    try:
        subprocess.run(cmd, capture_output=True, check=True)
        y, _ = sf.read(tmp, dtype="float32")
    except Exception as e:
        log(f"nie moge zdekodowac probki glosu ({e!r})")
        return None, 0.0
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if y.ndim > 1:
        y = y.mean(axis=1)
    E, _ = _spk_embed(y.astype(np.float32), sess, co="wzorca glosu")
    if len(E) < 2:
        log("probka glosu za krotka/za cicha (min ~4 s mowy)")
        return None, 0.0
    c = E.mean(axis=0)
    c = c / (np.linalg.norm(c) + 1e-9)
    return c, float(np.mean(E @ c))

def wczytaj_wzorce(sciezka):
    """Wzorce glosow z JSON zapisanego przez GUI: {"wzorce":[{"nazwa":..,"wektor":[..]}]}.
    Bledny/niekompletny plik NIE moze wywalic przetwarzania - loguje i zwraca [] (wtedy
    ochrona jest nieaktywna, ale plik zostanie przetworzony)."""
    if not sciezka or not os.path.exists(sciezka):
        return []
    try:
        d = json.load(open(sciezka, encoding="utf-8"))
        out = []
        for w in d.get("wzorce", []):
            v = w.get("wektor") or []
            if len(v) >= 64:                       # 192D w CAMPPlus; sanity, nie sztywno
                out.append([float(x) for x in v])
        if out:
            log(f"chronione głosy: wczytano {len(out)} wzorc(ów)")
        return out
    except Exception as e:
        log(f"nie moge wczytac wzorcow glosow ({e!r}) - ochrona glosow nieaktywna")
        return []


def detect_protected_voices(y_full, wzorce):
    """Regiony (start, end) podobne do KTOREGOKOLWIEK wzorca - chronione przed cieciami.
    NORMALIZACJA KANALU (kluczowa): embedding koduje takze tor nagrania (mikrofon,
    kompresja, tlo), wspolny dla calego pliku. BEZ odjecia sredniej pliku wzorce
    "chronily" 98.1% CALEGO materialu (artefakt - apka przestalaby cokolwiek wycinac);
    po odjeciu 4.3%, czyli realny udzial syntezy. Ta sama srednia stosowana do wzorcow,
    zeby porownanie bylo w tej samej przestrzeni."""
    if not wzorce:
        return []
    sess = _get_spk()
    if sess is None:
        return []
    y16 = (librosa.resample(y_full, orig_sr=SR, target_sr=SPK_SR)
           if SR != SPK_SR else y_full).astype(np.float32)
    E, poz = _spk_embed(y16, sess)
    if len(E) < 3:
        return []
    mu = E.mean(axis=0)
    def _cn(X):
        Z = X - mu
        return Z / (np.linalg.norm(Z, axis=1, keepdims=True) + 1e-9)
    En = _cn(E)
    W = _cn(np.array([w for w in wzorce], dtype=np.float64))
    sim = np.max(En @ W.T, axis=1)
    # HISTEREZA: region startuje na oknie PEWNYM (>= SPK_THRESH) i ciagnie sie przez okna
    # tylko PODOBNE (>= SPK_THRESH_LO). Bez tego co piate okno syntezy przeciekalo, bo
    # spadalo pod prog (mieszanka z sasiednim glosem, koncowka wypowiedzi, cichszy
    # fragment) - a ciecia w tej dziurze byly slyszalne.
    okna = []
    aktywny = False
    for i, s in enumerate(sim):
        if s >= SPK_THRESH:
            aktywny = True
        elif s < SPK_THRESH_LO:
            aktywny = False
        if aktywny:
            okna.append((poz[i], poz[i] + SPK_WIN))
    if not okna:
        log(f"chronione glosy: brak dopasowan (prog {SPK_THRESH:.2f})")
        return []
    reg = _scal_regiony(okna, SPK_PAD, SPK_MERGE_GAP)
    tot = sum(b - a for a, b in reg)
    log(f"chronione głosy: {len(reg)} region(ów), łącznie {tot/60:.1f} min "
        f"({100*len(okna)/max(len(En),1):.0f}% materiału) - z {len(wzorce)} wzorc(ów)")
    return reg


def _get_vad():
    """Sesja ONNX Silero VAD. None gdy model/onnxruntime niedostepne (wtedy fallback
    na stary guard AST - lepiej czyscic slabiej niz pasc)."""
    global _vad_sess
    if _vad_sess is not None:
        return _vad_sess
    # Sciezka modelu: env VAD_MODEL (ustawia GUI), a jak nie - WSZYSCY kandydaci
    # z _model_candidates (env CZYSCICIEL_MODEL_DIR + domyslny LOCALAPPDATA). Ten sam
    # wzorzec co model fillerow/muzyki i fallback DFN - bez niego wystarczy, ze env
    # nie dotrze (uruchomienie CLI, inny profil) i guard po cichu sie NIE wlacza.
    kandydaci = [os.environ.get("VAD_MODEL", "").strip()] + [
        os.path.join(d, "silero_vad.onnx") for d in _model_candidates()]
    path = next((p for p in kandydaci if p and os.path.exists(p)), None)
    if path is None:
        log("model VAD nie znaleziony - guard mowy wylaczony "
            f"(szukalem w: {', '.join(p for p in kandydaci if p)})")
        return None
    try:
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.inter_op_num_threads = so.intra_op_num_threads = 1   # maly model: 1 watek szybszy
        _vad_sess = ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])
        return _vad_sess
    except Exception as e:
        log(f"onnxruntime niedostepny ({e!r}) - guard mowy wylaczony")
        return None

def detect_speech(y_full):
    """Zwraca liste (start_s, end_s) regionow MOWY (ludzkiej ORAZ syntetycznej).
    Uzywane jako WETO dla wycinania odglosow: co Silero uznaje za mowe, tego nie tniemy
    jako chrzakniecie/oddech/mlasniecie. Zwraca [] gdy VAD niedostepny."""
    sess = _get_vad()
    if sess is None:
        return []
    y16 = (librosa.resample(y_full, orig_sr=SR, target_sr=VAD_SR)
           if SR != VAD_SR else y_full).astype(np.float32)
    state = np.zeros((2, 1, 128), dtype=np.float32)
    ctx = np.zeros(VAD_CTX, dtype=np.float32)
    sr_arr = np.array(VAD_SR, dtype=np.int64)
    okna, n = [], len(y16)
    nwin = max(1, n // VAD_WIN)
    for wi, i in enumerate(range(0, n - VAD_WIN, VAD_WIN)):
        blok = y16[i:i + VAD_WIN]
        try:
            p, state = sess.run(None, {"input": np.concatenate([ctx, blok]).reshape(1, -1),
                                       "state": state, "sr": sr_arr})
        except Exception as e:
            log(f"blad VAD ({e!r}) - guard mowy przerwany")
            return []
        ctx = blok[-VAD_CTX:]
        if float(p[0][0]) >= VAD_THRESH:
            okna.append((i / VAD_SR, (i + VAD_WIN) / VAD_SR))
        if wi % 500 == 0:
            progress(76 + 2 * (wi + 1) / nwin, f"Wykrywanie mowy: {wi+1}/{nwin}")
    regions = _scal_regiony(okna, VAD_PAD, VAD_MERGE_GAP)
    tot = sum(b - a for a, b in regions)
    log(f"mowa (VAD): {len(regions)} region(ow), lacznie {tot/60:.1f} min")
    return regions

def _scal_regiony(flagi, pad, gap):
    """Scala okna w regiony: rozszerza o `pad` i zlepia te rozdzielone luka <= `gap`.
    Wydzielone z detect_tts, by dalo sie przetestowac bez modelu (patrz verify_app.py):
    to tu siedzial bug zgloszony przez Michala - bez `gap` okno, w ktorym AST chwilowo
    uznal synteze za mowe, tworzylo LUKE i przez nia przeciskalo sie ciecie."""
    out = []
    for a, b in flagi:
        a = max(0.0, a - pad); b = b + pad
        if out and a <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]

# ---------- ITERACJA DOMYKAJACA (idempotencja) ----------
# Detekcja per-okno + prog daja efekt: plik przemielony ponownie lapie kilka nowych,
# krotkich fillerow/pauz (graniczne przypadki, ktore po zmianie kontekstu przekraczaja
# prog). Zeby POJEDYNCZY przebieg dawal plik idempotentny (kolejne czyszczenia lapia
# zero), iterujemy TU: wykrywamy na sygnale bez dotychczasowych ciec, mapujemy nowe na
# os oryginalu, akumulujemy - az kolejna runda usuwalaby < progu. Pomiar (odc.15min):
# fillery 67->5->2->0, wiec zbiega; pauzy szumia ale usuwaja 0s (stop po realnym czasie).
ITER_MAX = 6                 # twardy limit rund (bezpiecznik na wypadek oscylacji)
ITER_MAX_DOKLADNY = 30       # tryb dokladny: iteruj "do oporu", ale i tak z bezpiecznikiem
ITER_MIN_GAIN_S = 0.30       # stop gdy runda dokladaby mniej wycietego czasu niz tyle

def _build_kept_signal(y, cuts_samples):
    """Z sygnalu y (16k) usuwa przedzialy cuts_samples (probki), zwraca (y2, keeps)."""
    n = len(y)
    ci = sorted([max(0,s), min(n,e)] for s, e in cuts_samples if e > s)
    merged = []
    for s, e in ci:
        if merged and s <= merged[-1][1]: merged[-1][1] = max(merged[-1][1], e)
        else: merged.append([s, e])
    keeps = []; prev = 0
    for s, e in merged:
        if s > prev: keeps.append((prev, s))
        prev = e
    if prev < n: keeps.append((prev, n))
    y2 = np.concatenate([y[s:e] for s, e in keeps]) if keeps else np.zeros(0, dtype=y.dtype)
    return y2, keeps

def _map_back(iv_new, keeps):
    """Mapuje interwaly (a,b) w sekundach z osi SYGNALU-BEZ-CIEC na os ORYGINALU
    (przez segmenty keeps, w probkach)."""
    starts_new = []; acc = 0
    for s, e in keeps:
        starts_new.append(acc); acc += (e - s)
    def m(t):
        p = int(round(t*SR))
        i = bisect.bisect_right(starts_new, p) - 1
        i = max(0, min(i, len(keeps)-1))
        return (keeps[i][0] + (p - starts_new[i])) / SR
    return [(m(a), m(b)) for a, b in iv_new]

def detect_all_cuts_iterative(y, tnij_fillery, tnij_cisze, dokladny=False):
    """Zwraca allc - liste ciec {a,b,dur,typ} na osi ORYGINALU, domknieta iteracyjnie
    tak, by ponowne czyszczenie wyniku lapalo ~zero. Model ladowany raz.
    dokladny=True: iteruje DO OPORU (az realny przyrost = 0), bez limitu ITER_MAX."""
    total_cuts = []               # (a,b,typ) na osi oryginalu, akumulowane
    n = len(y)
    limit = ITER_MAX_DOKLADNY if dokladny else ITER_MAX
    min_gain = 0.0 if dokladny else ITER_MIN_GAIN_S   # dokladny: stop dopiero gdy 0s
    rnd = 0
    while rnd < limit:
        rnd += 1
        if rnd == 1:
            log("weryfikacja: runda 1 (detekcja fillerów i pauz)...")
        else:
            log(f"weryfikacja: runda {rnd} (sprawdzam, czy zostało coś do wycięcia)...")
        # sygnal po dotychczasowych cieciach (w probkach) - na nim szukamy NOWYCH
        cur_samples = [(int(a*SR), int(b*SR)) for a, b, _ in total_cuts]
        y2, keeps = _build_kept_signal(y, cur_samples) if total_cuts else (y, [(0, n)])
        if len(y2) < int(0.5*SR): break
        # pasek: 1. runda zajmuje glowna czesc (20..66%), kolejne domykaja (66..70%)
        if rnd == 1: p_lo, p_hi = 20, 66
        else: p_lo, p_hi = 66, 70
        f_new = detect_fillers(y2, p_lo=p_lo, p_hi=p_hi, verbose=(rnd == 1)) if tnij_fillery else []
        p_new = detect_pauses(y2) if tnij_cisze else []
        # mapuj z osi sygnalu-bez-ciec na os oryginalu
        if total_cuts:
            f_new = _map_back(f_new, keeps); p_new = _map_back(p_new, keeps)
        new = [(a, b, "filler") for a, b in f_new] + [(a, b, "pauza") for a, b in p_new]
        if rnd == 1:
            log(f"fillery: {len(f_new)}"); log(f"pauzy do skrócenia: {len(p_new)}")
        # ile REALNEGO czasu wycietego (po SAFE/xf/merge) mamy PRZED i PO tej rundzie
        # - OBA liczone tak samo przez compute_keeps, inaczej miary sa niespojne
        def _cut_frames(cuts):
            _, merged = compute_keeps(n, SR, [(a, b) for a, b, _ in cuts])
            return sum(e-s for s, e in merged)
        prev_frames = _cut_frames(total_cuts)
        cand = total_cuts + new
        now_frames = _cut_frames(cand)
        gain_s = (now_frames - prev_frames)/SR
        if rnd > 1:
            if gain_s <= min_gain:
                log(f"  weryfikacja: runda {rnd} nic już nie wycina (+{gain_s:.2f}s) - koniec")
                break
            log(f"  weryfikacja: runda {rnd} domknęła jeszcze +{gain_s:.2f}s ({len(new)} cięć)")
            progress(66 + 4*min(rnd, ITER_MAX)/ITER_MAX, f"Weryfikacja ({rnd})...")
        total_cuts = cand
        if not new: break
    allc = [{"a": a, "b": b, "dur": b-a, "typ": t} for a, b, t in total_cuts]
    allc.sort(key=lambda z: z["a"])
    return allc

# ---------- KEEP SEGMENTS (wspolne dla ciecia i eksportu RPP) ----------
def compute_keeps(total_frames, sr, cuts):
    """Zwraca (keeps, merged) w PROBKACH. keeps=segmenty do zachowania."""
    safe = int(SAFE_MS/1000*sr); xf = int(XF_MS/1000*sr)
    ci = []
    for a, b in cuts:
        A = int(a*sr)+safe; B = int(b*sr)-safe
        if B-A > xf: ci.append([A, B])
    ci.sort()
    merged = []
    for c in ci:
        if merged and c[0] <= merged[-1][1]: merged[-1][1] = max(merged[-1][1], c[1])
        else: merged.append(c)
    keeps = []; prev = 0
    for a, b in merged:
        if a > prev: keeps.append((prev, a))
        prev = b
    if prev < total_frames: keeps.append((prev, total_frames))
    return keeps, merged

# ---------- ODSZUMIANIE (DeepFilterNet3, opcjonalne) ----------
# Osobny program deep-filter.exe (Rust, MIT/Apache-2.0) dociagany przez bootstrap jak
# ffmpeg - ZERO zaleznosci Pythona (pakiet pip nie ma wheela dla 3.12) i liczy na CPU,
# wiec nie konkuruje z detekcja o GPU. Model jest MASKUJACY (tlumi szum, nie dorabia
# tresci) - patrz DFN_ATTEN nizej: NIE uzywamy domyslnego 100 dB narzedzia, bo na
# nagraniach zdalnych wchodzi w glos; zmierzone: za 1 dB cichszego tla placi sie ~5-9 dB
# glebsza ingerencja w mowe. NIE jest dereverberem (p_reverb=0.1 w treningu).
DFN_SR = 48000             # deep-filter przyjmuje WYLACZNIE 48 kHz WAV
DFN_TAIL = 1440            # wyjscie jest KROTSZE o tyle probek @48k (30 ms) MIMO -D;
                           # stale, zmierzone na kazdym poziomie tlumienia -> kompensujemy
                           # jak encoder delay MP3, inaczej rozjedzie sie os czasu

def _dfn_bin():
    """Sciezka do deep-filter.exe. Jak przy modelach (pulapka v1.5.1): env DFN_BIN
    ustawia GUI, ale gdyby go brakowalo (CLI, zle env), probujemy domyslnej lokalizacji
    %LOCALAPPDATA%\\Czysciciel\\tools - inaczej opcja cicho nie dziala."""
    for p in (os.environ.get("DFN_BIN", ""),
              os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                           "Czysciciel", "tools", "deep-filter.exe")):
        if p and os.path.exists(p):
            return p
    return ""

def denoise_file(ain, outdir, stem, atten_db, sr, ch):
    """Odszumia CALY plik przed detekcja i cieciem. Zwraca sciezke nowego WAV
    (w oryginalnym sr/ch) albo None gdy sie nie udalo - wtedy wolajacy zostaje
    przy nieodszumionym materiale (lepiej wyczyscic bez denoise niz pasc)."""
    dfn = _dfn_bin()
    if not dfn or not os.path.exists(dfn):
        log("odszumianie: brak deep-filter.exe - pomijam")
        return None
    ff = _ffmpeg_bin()
    tmp48 = os.path.join(outdir, stem + "_tmp_dfn48.wav")
    dfn_out_dir = os.path.join(outdir, stem + "_tmp_dfn_out")
    try:
        # 1. do 48k (wymog modelu). Kanaly zachowujemy - DFN radzi sobie ze stereo.
        r = subprocess.run([ff, "-y", "-i", ain, "-ar", str(DFN_SR), "-c:a", "pcm_s16le", tmp48],
                           capture_output=True, text=True, **_win_kw())
        if not (os.path.exists(tmp48) and os.path.getsize(tmp48) > 1000):
            log(f"odszumianie: konwersja do 48k nie wyszla ({r.stderr[-200:]}) - pomijam")
            return None
        # 2. odszum (-D kompensuje opoznienie STFT/lookahead modelu)
        os.makedirs(dfn_out_dir, exist_ok=True)
        r = subprocess.run([dfn, "-a", str(atten_db), "-D", "-o", dfn_out_dir, tmp48],
                           capture_output=True, text=True, **_win_kw())
        den = os.path.join(dfn_out_dir, os.path.basename(tmp48))
        if not (os.path.exists(den) and os.path.getsize(den) > 1000):
            log(f"odszumianie: deep-filter nie dal wyniku ({(r.stderr or '')[-200:]}) - pomijam")
            return None
        # 3. KOMPENSACJA skrocenia + powrot do oryginalnego sr/ch, by dalszy pipeline
        # (detekcja, ciecia, RPP, rozdzialy) liczyl na tej samej osi czasu co wejscie
        out = os.path.join(outdir, stem + "_tmp_odszum.wav")
        pad = f"apad=pad_len={DFN_TAIL}" if DFN_TAIL else "anull"
        r = subprocess.run([ff, "-y", "-i", den, "-af", pad,
                            "-ar", str(sr), "-ac", str(ch), "-c:a", "pcm_s16le", out],
                           capture_output=True, text=True, **_win_kw())
        if not (os.path.exists(out) and os.path.getsize(out) > 1000):
            log(f"odszumianie: powrot do {sr} Hz nie wyszedl - pomijam")
            return None
        log(f"odszumiono (tlumienie {atten_db} dB, kompensacja {DFN_TAIL} próbek)")
        return out
    except Exception as e:
        log(f"odszumianie pominięte (błąd: {e})")
        return None
    finally:
        for p in (tmp48,):
            try:
                if os.path.exists(p): os.remove(p)
            except Exception: pass
        try:
            if os.path.isdir(dfn_out_dir): shutil.rmtree(dfn_out_dir, ignore_errors=True)
        except Exception: pass

# ---------- NORMALIZACJA GLOSNOSCI (EBU R128 / LUFS, opcjonalna) ----------
# CELE: -16 LUFS = poziom publikacji podcastow (Apple), -23 LUFS = NORMA EBU R128
# (Target Level, punkt h; true peak <= -1 dBTP, punkt m), -14 LUFS = Spotify.
# UWAGA: -16/-14 to poziomy odtwarzania PLATFORM, nie normy - nie nazywac ich norma.
#
# DLACZEGO NIE loudnorm w jednym przebiegu: gdy zadany cel wymaga wiekszego wzmocnienia
# niz pozwala true peak, loudnorm PO CICHU przechodzi w tryb "dynamic" i KOMPRESUJE
# dynamike (zmierzone: cel -16 na materiale -21.4 LUFS => gain skacze 2.76..7.66 dB,
# rozrzut 4.91 dB). Dlatego: mierzymy raz (ebur128), potem STALY gain + limiter tylko
# na szczyty. Limiter w OVERSAMPLINGU 4x, bo alimiter nie jest true-peak (limit -1.5
# dawal realne -1.0 dBTP).
LUFS_TARGETS = {"-16": -16.0, "-23": -23.0, "-14": -14.0}
TP_CEILING = -1.5          # dBTP; zapas wzgl. wymaganego przez R128 -1 dBTP
LIM_OS = 4                 # krotnosc oversamplingu limitera (176.4k dla 44.1k)

def measure_loudness(path):
    """Mierzy (integrated LUFS, true peak dBTP) filtrem ebur128. Zwraca (None, None)
    gdy pomiar sie nie uda - wtedy normalizacje pomijamy, nie zgadujemy."""
    ff = _ffmpeg_bin()
    r = subprocess.run([ff, "-hide_banner", "-i", path, "-af", "ebur128=peak=true",
                        "-f", "null", "-"], capture_output=True, text=True, **_win_kw())
    err = r.stderr or ""
    # bierzemy OSTATNIE wystapienie (podsumowanie na koncu), nie chwilowe odczyty
    mi = re.findall(r"I:\s+(-?\d+\.\d+)\s+LUFS", err)
    mp = re.findall(r"Peak:\s+(-?\d+\.\d+)\s+dBFS", err)
    if not mi or not mp:
        return None, None
    return float(mi[-1]), float(mp[-1])

def loudness_filter(cur_lufs, cur_tp, target_lufs, sr):
    """Buduje lancuch ffmpeg: STALY gain + limiter true-peak w oversamplingu.
    Zwraca (filtr, gain_db, czy_limiter_bedzie_pracowal)."""
    gain = target_lufs - cur_lufs
    peak_after = cur_tp + gain
    need_lim = peak_after > TP_CEILING
    vol = f"volume={gain:.2f}dB"
    if not need_lim:
        # zapas na szczyty wystarcza - czysty gain, ZERO ingerencji w dynamike
        return vol, gain, False
    os_sr = int(sr * LIM_OS)
    return (f"aresample={os_sr}:resampler=soxr:precision=28,{vol},"
            f"alimiter=limit={TP_CEILING}dB:level=disabled:attack=5:release=50,"
            f"aresample={sr}:resampler=soxr:precision=28"), gain, True

# ---------- CIECIE STRUMIENIOWE ----------
def cut_stream(ain, keeps, aout, sr, ch):
    xf = int(XF_MS/1000*sr)
    def xfade(x1, x2, n):
        if len(x1) < n or len(x2) < n or n <= 0: return np.concatenate([x1, x2])
        fo = np.linspace(1, 0, n)[:, None]; fi = np.linspace(0, 1, n)[:, None]
        return np.concatenate([x1[:-n], x1[-n:]*fo+x2[:n]*fi, x2[n:]])
    written = 0
    with sf.SoundFile(ain) as fin, sf.SoundFile(aout, 'w', samplerate=sr, channels=ch, subtype="PCM_16") as fout:
        tail = None
        for (s, e) in keeps:
            fin.seek(s); block = fin.read(e-s, dtype="float32", always_2d=True)
            if tail is None: tail = block
            else:
                j = xfade(tail, block, xf)
                fout.write(j[:-xf] if len(j) > xf else j); written += max(0, len(j)-xf)
                tail = j[-xf:] if len(j) > xf else np.zeros((0, ch), dtype="float32")
        if tail is not None and len(tail) > 0: fout.write(tail); written += len(tail)
    return written

# ---------- ZAPIS WYCIETYCH FRAGMENTOW (do odsluchu/kontroli) ----------
def write_removed(ain, merged, aout, sr, ch):
    """Sklada wszystkie WYCIETE fragmenty (merged, w probkach) w jeden plik,
    rozdzielone krotka cisza, zeby przy odsluchu bylo slychac granice."""
    gap = np.zeros((int(0.35*sr), ch), dtype="float32")  # 0.35s ciszy miedzy fragmentami
    written = 0
    with sf.SoundFile(ain) as fin, sf.SoundFile(aout, 'w', samplerate=sr, channels=ch, subtype="PCM_16") as fout:
        for i, (s, e) in enumerate(merged):
            if e <= s: continue
            fin.seek(s); block = fin.read(e-s, dtype="float32", always_2d=True)
            if i > 0: fout.write(gap); written += len(gap)
            fout.write(block); written += len(block)
    return written

# ---------- ROZDZIALY (chaptery ID3/CHAP) ----------
# Rozdzialy w pliku wejsciowym maja czasy na osi ORYGINALU. Po wycieciu fillerow/pauz
# os wynikowa jest krotsza - a KLUCZOWE: przesuniecie NIE jest jednorodne. Sklada sie
# z DWOCH skladnikow, ktore narastaja im dalej w material:
#   1. suma dlugosci wszystkich WYCIETYCH fragmentow PRZED danym punktem,
#   2. crossfade XF_MS (25ms) odejmowany na KAZDYM zlaczeniu keep-segmentow.
# Punkt k-tego zachowanego segmentu ladduje w wyniku na: (suma dlugosci keepow<k) - k*XF.
# To identyczna matematyka jak w export_rpp (item startuje xf przed koncem poprzedniego).
# Dlatego 00:00:00 zostaje 00:00:00, ale kolejne znaczniki trzeba remapowac coraz mocniej.

def _ffmeta_unescape(s):
    out = []; i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i+1 < len(s):
            out.append(s[i+1]); i += 2
        else:
            out.append(c); i += 1
    return "".join(out)

def _ffmeta_escape(s):
    # ffmetadata: escape = ; # \ i znak nowej linii
    res = []
    for c in s:
        if c in "=;#\\":
            res.append("\\" + c)
        elif c == "\n":
            res.append("\\\n")
        else:
            res.append(c)
    return "".join(res)

def read_chapters(path, ff):
    """Czyta rozdzialy z pliku audio przez 'ffmpeg -f ffmetadata' (bootstrap nie
    dostarcza ffprobe). Zwraca liste (start_s, end_s, title) na osi ORYGINALU
    lub [] gdy brak rozdzialow / blad."""
    tmp = path + ".ffmeta_in.txt"
    try:
        subprocess.run([ff, "-y", "-v", "error", "-i", path, "-f", "ffmetadata", tmp],
                       capture_output=True, text=True, **_win_kw())
        if not os.path.exists(tmp):
            return []
        with open(tmp, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except Exception as e:
        log(f"nie udalo sie odczytac rozdzialow: {e!r}")
        return []
    finally:
        try:
            if os.path.exists(tmp): os.remove(tmp)
        except Exception:
            pass
    chapters = []; cur = None; tb = (1, 1000)
    for ln in lines:
        if ln.strip().upper() == "[CHAPTER]":
            if cur is not None: chapters.append(cur)
            cur = {"tb": (1, 1000), "start": 0, "end": 0, "title": ""}
            continue
        if cur is None:
            continue
        if ln.startswith("[") and ln.strip().endswith("]"):
            chapters.append(cur); cur = None; continue
        if "=" not in ln:
            continue
        key, val = ln.split("=", 1)
        key = key.strip().upper()
        if key == "TIMEBASE":
            try:
                num, den = val.strip().split("/"); cur["tb"] = (int(num), int(den))
            except Exception:
                cur["tb"] = (1, 1000)
        elif key == "START":
            try: cur["start"] = int(val.strip())
            except Exception: cur["start"] = 0
        elif key == "END":
            try: cur["end"] = int(val.strip())
            except Exception: cur["end"] = 0
        elif key.lower() == "title":
            cur["title"] = _ffmeta_unescape(val)
    if cur is not None:
        chapters.append(cur)
    out = []
    for c in chapters:
        num, den = c["tb"]; den = den or 1000
        out.append((c["start"]*num/den, c["end"]*num/den, c["title"]))
    return out

def remap_sample(p, keeps, xf):
    """Mapuje probke p z osi ORYGINALU na probke osi WYNIKU (po wycieciu + crossfade).
    Punkt wpadajacy w wyciety fragment przyciaga sie do poczatku nastepnego
    zachowanego segmentu. Uwzglednia narastajacy dryf -k*xf."""
    cum = 0
    for k, (s, e) in enumerate(keeps):
        base = cum - k*xf
        if p < s:
            return max(0, base)
        if p < e:
            return max(0, base + (p - s))
        cum += (e - s)
    total = cum - max(0, len(keeps)-1)*xf
    return max(0, total)

def _fmt_ts(seconds):
    if seconds < 0: seconds = 0
    total_ms = int(round(seconds * 1000))
    h = total_ms // 3600000; total_ms %= 3600000
    m = total_ms // 60000; total_ms %= 60000
    s = total_ms // 1000
    return f"{h:02d}:{m:02d}:{s:02d}"

def remap_chapters(chapters, keeps, sr, xf):
    """Zwraca liste (new_start_s, new_end_s, title) na osi WYNIKU."""
    out = []
    for st, en, title in chapters:
        ns = remap_sample(int(round(st*sr)), keeps, xf) / sr
        ne = remap_sample(int(round(en*sr)), keeps, xf) / sr
        out.append((ns, ne, title))
    return out

def write_chapters_txt(txt_path, chapters):
    """Sidecar 'chapters.txt': jedna linia na rozdzial w postaci 'etykieta timestamp'."""
    with open(txt_path, "w", encoding="utf-8") as f:
        for st, en, title in chapters:
            label = (title or "").strip() or "Rozdział"
            f.write(f"{label} {_fmt_ts(st)}\n")
    return txt_path

def write_chapters_ffmeta(meta_path, chapters, total_out_s):
    """Buduje plik ffmetadata z SAMYMI rozdzialami (skorygowane czasy) do wstrzykniecia
    w plik wynikowy przez '-map_chapters'. END domykamy do dlugosci wyniku."""
    with open(meta_path, "w", encoding="utf-8") as f:
        f.write(";FFMETADATA1\n")
        n = len(chapters)
        for i, (st, en, title) in enumerate(chapters):
            start_ms = int(round(st * 1000))
            # END: nastepny start, a dla ostatniego - koniec materialu
            end_ms = int(round((chapters[i+1][0] if i+1 < n else total_out_s) * 1000))
            if end_ms <= start_ms: end_ms = start_ms + 1
            f.write("[CHAPTER]\n")
            f.write("TIMEBASE=1/1000\n")
            f.write(f"START={start_ms}\n")
            f.write(f"END={end_ms}\n")
            f.write(f"title={_ffmeta_escape((title or '').strip())}\n")
    return meta_path

# ---------- EKSPORT PROJEKTU REAPERA (.RPP) ----------
def mp3_encoder_delay(ff, path):
    """Zwraca encoder delay (probki) jaki dekoder USUWA z przodu MP3 (LAME/gapless).
    KLUCZOWE dla RPP: ffmpeg dekoduje MP3 gapless (nasze ciecia liczone bez tych
    probek), ale Reaper importujac MP3 tych probek NIE usuwa - cala tresc jest
    przesunieta o delay do tylu, wiec SOFFS trafialby wczesniej niz trzeba
    (objaw: 'kazde ciecie o ~0.1s za wczesnie'). Kompensujemy dodajac delay do SOFFS.
    Odczyt najpewniejszy z samego ffmpega (bootstrap nie dostarcza ffprobe):
    'ffmpeg -v debug' wypisuje na stderr 'demuxer injecting skip <N> / discard'.
    Zwraca 0 gdy nie MP3 / nie udalo sie odczytac (wtedy zero kompensacji)."""
    if os.path.splitext(path)[1].lower() != ".mp3":
        return 0
    try:
        r = subprocess.run([ff, "-v", "debug", "-i", path, "-t", "0.1", "-f", "null", "-"],
                           capture_output=True, text=True, **_win_kw())
        import re
        # preferuj 'demuxer injecting skip N', fallback 'skip N / discard'
        m = re.search(r"injecting skip\s+(\d+)", r.stderr or "")
        if not m:
            m = re.search(r"skip\s+(\d+)\s*/\s*discard", r.stderr or "")
        return int(m.group(1)) if m else 0
    except Exception as e:
        log(f"nie udalo sie odczytac encoder-delay MP3 ({e!r}) - bez kompensacji")
        return 0

def _rpp_guid():
    import uuid
    return "{" + str(uuid.uuid4()).upper() + "}"

def _rpp_item(pos, length, soffs, name, src, stype, fin=0.0, fout=0.0):
    # FADEIN/FADEOUT: ksztalt 1 (rowna moc) + dlugosc w s -> crossfade na nalozeniu
    return f"""    <ITEM
      POSITION {pos:.6f}
      LENGTH {length:.6f}
      SOFFS {soffs:.6f}
      FADEIN 1 {fin:.6f} 0 1 0 0 0
      FADEOUT 1 {fout:.6f} 0 1 0 0 0
      NAME "{name}"
      GUID {_rpp_guid()}
      IGUID {_rpp_guid()}
      <SOURCE {stype}
        FILE "{src}"
      >
    >"""

def _rpp_document(track_name, items, markers, sr, ripple):
    body = "\n".join(items)
    mk = "\n".join(markers)
    # UWAGA: itemy ida BEZPOSREDNIO do TRACK. Nie ma kontenera <ITEMS> - Reaper
    # zglaszalby go jako 'element not understood'.
    return f"""<REAPER_PROJECT 0.1 "7.0/win64" {int(time.time())}
  RIPPLE {ripple}
  GROUPOVERRIDE 0 0 0
  AUTOXFADE 1
  SAMPLERATE {sr} 0 0
  <RECORD_CFG
  >
  TEMPO 120 4 4
{mk}
  <TRACK {_rpp_guid()}
    NAME "{track_name}"
    TRACKHEIGHT 0 0 0 0 0 0
{body}
  >
>
"""

def _src_info(source_file):
    src = os.path.abspath(source_file).replace("\\", "/")
    ext = os.path.splitext(source_file)[1].lower()
    stype = {".mp3": "MP3", ".flac": "FLAC", ".ogg": "VORBIS", ".opus": "VORBIS"}.get(ext, "WAVE")
    return src, stype

def export_rpp(rpp_path, source_file, keeps, merged, sr, src_delay=0):
    """WARIANT GOTOWY: fillery/pauzy JUZ wyciete, segmenty dosuniete z CROSSFADEM
    na zlaczeniach (te same 25ms co plik audio z ffmpega - brzmi tak samo plynnie).
    Kolejne itemy NAKLADAJA sie o XF_MS i maja fade in/out => crossfade. Odwolanie
    do ORYGINALU przez SOFFS, wiec kazde ciecie mozna cofnac/rozciagnac.
    src_delay: encoder-delay zrodla (probki) dodawany do SOFFS - Reaper NIE usuwa
    delay MP3 (ffmpeg usuwa), wiec bez tego cala tresc bylaby o delay za wczesnie."""
    src, stype = _src_info(source_file)
    def secs(fr): return fr/sr
    def soffs(fr): return (fr + src_delay)/sr   # kompensacja encoder-delay zrodla
    xf = XF_MS / 1000.0
    items = []; pos = 0.0; n = len(keeps)
    for idx, (s, e) in enumerate(keeps):
        length = secs(e - s)
        fin = xf if idx > 0 else 0.0            # crossfade z poprzednim
        fout = xf if idx < n-1 else 0.0         # crossfade z nastepnym
        items.append(_rpp_item(pos, length, soffs(s), "segment", src, stype, fin, fout))
        # nastepny item startuje xf PRZED koncem tego => nalozenie = crossfade
        pos += length - (xf if idx < n-1 else 0.0)
    markers = []; mp = 0.0; idx = 1
    for (s, e) in keeps[:-1]:
        mp += secs(e-s) - xf
        markers.append(f'  MARKER {idx} {mp:.6f} "ciecie" 0 0 1 R {_rpp_guid()}')
        idx += 1
    content = _rpp_document("Czysciciel - material oczyszczony (dosuniety)",
                            items, markers, sr, ripple=0)
    with open(rpp_path, "w", encoding="utf-8") as f:
        f.write(content)
    cut_total = sum(secs(b-a) for a, b in merged)
    log(f"projekt Reapera (gotowy): {rpp_path} (wycięte {cut_total/60:.1f} min, {len(keeps)} segmentów)")
    return rpp_path

def export_rpp_marked(rpp_path, source_file, keeps, merged, sr, src_delay=0):
    """WARIANT DO PRZEJRZENIA: caly material na osi w ORYGINALNYM ukladzie
    (nic nie dosuniete), rozbity na itemy. Fragmenty do wyciecia to osobne
    itemy nazwane 'WYTNIJ N' (czytnik ekranu je odczyta), zachowane to 'zostaw'.
    Projekt ma wlaczony RIPPLE ALL - skasowanie itemu 'WYTNIJ' automatycznie
    dosuwa reszte. Jesli uznasz, ze czegos wyciac nie warto - po prostu nie
    kasujesz tego itemu.
    src_delay: encoder-delay MP3 dodawany do SOFFS (Reaper nie usuwa delay ktory
    ffmpeg usuwal) - inaczej tresc kazdego itemu bylaby o delay za wczesnie."""
    src, stype = _src_info(source_file)
    def secs(fr): return fr/sr
    def soffs(fr): return (fr + src_delay)/sr
    # zbuduj pelna sekwencje segmentow (keep + cut) posortowana po czasie
    segs = [("keep", s, e) for (s, e) in keeps] + [("cut", s, e) for (s, e) in merged]
    segs.sort(key=lambda z: z[1])
    items = []; markers = []; cut_no = 0
    for typ, s, e in segs:
        if e <= s: continue
        if typ == "cut":
            cut_no += 1
            name = f"WYTNIJ {cut_no}"
            # marker na poczatku fragmentu do wyciecia - latwa nawigacja
            markers.append(f'  MARKER {cut_no} {secs(s):.6f} "WYTNIJ {cut_no}" 0 0 1 R {_rpp_guid()}')
        else:
            name = "zostaw"
        # POSITION = oryginalny czas (bez dosuwania), SOFFS = ten sam + kompensacja delay
        items.append(_rpp_item(secs(s), secs(e-s), soffs(s), name, src, stype))
    content = _rpp_document("Czysciciel - do przejrzenia (skasuj itemy WYTNIJ)",
                            items, markers, sr, ripple=2)  # 2 = ripple all tracks
    with open(rpp_path, "w", encoding="utf-8") as f:
        f.write(content)
    log(f"projekt Reapera (do przejrzenia): {rpp_path} ({cut_no} fragmentów oznaczonych WYTNIJ)")
    return rpp_path

def main():
    import argparse
    # TRYB WZORCA GLOSU: liczy jeden embedding z probki i wypisuje go jako WZORZEC|{json}.
    # Osobna, wczesna sciezka - GUI wola to przy dodawaniu glosu do ochrony i NIE chce
    # ladowac torcha ani przechodzic przez caly parser wejscia/wyjscia.
    if "--wzorzec" in sys.argv:
        i = sys.argv.index("--wzorzec")
        src = sys.argv[i + 1] if len(sys.argv) > i + 1 else ""
        st = float(sys.argv[i + 2]) if len(sys.argv) > i + 2 else None
        du = float(sys.argv[i + 3]) if len(sys.argv) > i + 3 else None
        c, spoj = wzorzec_glosu(src, st, du)
        if c is None:
            return 2
        print("WZORZEC|" + json.dumps({"wektor": [float(x) for x in c],
                                       "spojnosc": round(spoj, 4)}), flush=True)
        return 0
    global CUT, KEEP, TARGET, MUSIC_THRESH
    PRESETY = {
        "zachowawczy": (0.30, 0.70, 0.60),
        "umiarkowany": (0.30, 0.50, 0.45),
        "zwarty":      (0.30, 0.35, 0.30),
    }
    # format -> (kodek ffmpeg, rozszerzenie, czy stratny [bitrate ma znaczenie])
    FORMATY = {
        "mp3":  ("libmp3lame", "mp3",  True),
        "aac":  ("aac",        "m4a",  True),
        "opus": ("libopus",    "opus", True),
        "ogg":  ("libvorbis",  "ogg",  True),
        "wma":  ("wmav2",      "wma",  True),
        "ac3":  ("ac3",        "ac3",  True),
        "flac": ("flac",       "flac", False),
        "alac": ("alac",       "m4a",  False),
        "wav":  ("pcm_s16le",  "wav",  False),
    }
    ap = argparse.ArgumentParser(description="Czysciciel - czyszczenie audio z fillerow (yyy/eee) i nadmiarowych pauz.")
    ap.add_argument("wejscie", help="plik audio wejsciowy (mp3/wav/...)")
    ap.add_argument("wyjscie", nargs="?", help="plik wyjsciowy (rozszerzenie wg formatu)")
    ap.add_argument("-p", "--preset", choices=list(PRESETY), default="umiarkowany",
                    help="agresywnosc skracania pauz (domyslnie: umiarkowany)")
    ap.add_argument("--tryb", choices=["fillery", "cisza", "oba"], default="oba",
                    help="co wycinac: fillery / cisza(pauzy) / oba (domyslnie: oba)")
    ap.add_argument("--min-filler", type=float, default=CUT,
                    help=f"min. dlugosc fillera w s (domyslnie {CUT})")
    ap.add_argument("--format", choices=list(FORMATY), default="mp3",
                    help="format wyjsciowy audio (domyslnie: mp3)")
    ap.add_argument("--bitrate", type=int, default=192,
                    help="bitrate w kbps dla formatow stratnych (domyslnie 192)")
    ap.add_argument("--kanaly", choices=["zrodlo", "mono", "stereo"], default="stereo",
                    help="liczba kanalow wyjscia (domyslnie stereo)")
    ap.add_argument("--eksport", choices=["audio", "reaper", "oba"], default="audio",
                    help="co zapisac: audio / projekt reaper / oba (domyslnie: audio)")
    ap.add_argument("--wariant-rpp", choices=["gotowy", "przejrzenie", "oba"], default="gotowy",
                    help="wariant projektu Reapera: gotowy (dosuniety) / przejrzenie "
                         "(itemy WYTNIJ, ripple) / oba (domyslnie: gotowy)")
    ap.add_argument("--zapisz-wyciete", action="store_true",
                    help="zapisz tez osobny plik z tym, co zostalo wyciete (do odsluchu)")
    # muzyka: domyslnie POMIJAMY fragmenty z muzyka (nie tniemy fillerow/pauz w muzyce).
    # Flaga wylaczajaca dla zaawansowanych; GUI trzyma to jako domyslnie wlaczony checkbox.
    ap.add_argument("--bez-omijania-muzyki", action="store_true",
                    help="NIE omijaj muzyki - tnij fillery/pauzy w calym materiale (domyslnie muzyka jest chroniona)")
    ap.add_argument("--prog-muzyki", type=float, default=MUSIC_THRESH,
                    help=f"prog czulosci wykrywania muzyki 0..1 (domyslnie {MUSIC_THRESH}; "
                         "wyzszy = chroni tylko wyrazna muzyke, tlo tnie; nizszy = chroni juz przy sladzie muzyki)")
    ap.add_argument("--dokladny", action="store_true",
                    help="tryb dokladny: weryfikuj DO OPORU (wiele rund az nic nie zostanie do wyciecia); wydluza przetwarzanie")
    # CHRONIONE GLOSY: plik JSON z wzorcami (lista wektorow 192D) zapisany przez GUI.
    # Nie przez argv, bo wektory to setki liczb - i user moze miec kilka wzorcow.
    ap.add_argument("--chronione-glosy", default="",
                    help="sciezka do JSON z wzorcami glosow do OCHRONY (nie tnij tam nic); "
                         "GUI zapisuje ten plik z listy 'Chronione glosy'")
    # ODGLOSY: dodatkowe kategorie do wyciecia (jak fillery: tylko poza muzyka i mowa).
    # Domyslnie WYLACZONE - wlaczane osobnymi flagami (GUI: checkboxy).
    ap.add_argument("--tnij-chrzakniecia", action="store_true",
                    help="wycinaj chrzakniecia, kaszel, kichniecia (poza muzyka i mowa)")
    ap.add_argument("--tnij-oddechy", action="store_true",
                    help="wycinaj oddechy, wdechy, pociagniecia nosem (poza muzyka i mowa)")
    ap.add_argument("--tnij-mlasniecia", action="store_true",
                    help="wycinaj mlasniecia, cmokniecia, kliki ustne (poza muzyka i mowa)")
    # ODSZUMIANIE (DeepFilterNet) - domyslnie WYLACZONE. Na czystym nagraniu studyjnym
    # szkodzi (zmierzone: tlo cichnie o 0.11 dB przy 6 dB ingerencji w mowe).
    ap.add_argument("--odszum", action="store_true",
                    help="odszum nagranie modelem DeepFilterNet PRZED detekcja (dla nagran zdalnych)")
    ap.add_argument("--odszum-sila", type=int, default=6, choices=[6, 12, 100],
                    help="tlumienie szumu w dB: 6=delikatnie (domyslnie), 12=srednio, 100=mocno")
    # NORMALIZACJA GLOSNOSCI - domyslnie WYLACZONA
    ap.add_argument("--normalizuj", action="store_true",
                    help="normalizuj glosnosc do zadanego poziomu LUFS (staly gain + limiter true-peak)")
    ap.add_argument("--lufs", default="-16", choices=sorted(LUFS_TARGETS),
                    help="cel: -16=podcast/Apple (domyslnie), -23=norma EBU R128, -14=Spotify")
    # zgodnosc wstecz:
    ap.add_argument("--bez-pauz", action="store_true", help="alias --tryb fillery")
    ap.add_argument("--rpp", action="store_true", help="alias --eksport oba")
    ap.add_argument("--zostaw-wav", action="store_true", help="nie kasuj posredniego pliku .wav")
    a = ap.parse_args()

    # rozwiazanie aliasow zgodnosci
    tryb = a.tryb
    if a.bez_pauz: tryb = "fillery"
    eksport = a.eksport
    if a.rpp and eksport == "audio": eksport = "oba"
    tnij_fillery = tryb in ("fillery", "oba")
    tnij_cisze = tryb in ("cisza", "oba")
    kodek, ext, stratny = FORMATY[a.format]

    try:
        progress(2, "Ładowanie bibliotek...")
        _load_heavy()

        CUT, keep_p, target_p = a.min_filler, *PRESETY[a.preset][1:]
        KEEP, TARGET = keep_p, target_p
        MUSIC_THRESH = min(1.0, max(0.0, a.prog_muzyki))
        ain = a.wejscie
        # wyjscie: uzyj podanego, ale wymus poprawne rozszerzenie wg formatu
        if a.wyjscie:
            aout = os.path.splitext(a.wyjscie)[0] + "." + ext
        else:
            aout = os.path.splitext(ain)[0] + "_czysty." + ext
        outdir = os.path.dirname(os.path.abspath(aout))
        os.makedirs(outdir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(ain))[0]
        ff = _ffmpeg_bin()

        opis_tryb = {"fillery": "tylko fillery", "cisza": "tylko cisza (pauzy)",
                     "oba": "fillery + cisza"}[tryb]
        log(f"tryb: {opis_tryb} | preset={a.preset} | min-filler={CUT}s"
            + (f" | pauzy: keep<={KEEP}s->target {TARGET}s" if tnij_cisze else ""))
        log(f"format: {a.format} ({kodek})"
            + (f" | bitrate {a.bitrate} kbps" if stratny else " | bezstratny")
            + f" | kanaly: {a.kanaly} | eksport: {eksport}")

        # 1. REMUX do czystego WAV (pelna jakosc, eliminuje wadliwe ramki zrodla)
        progress(5, "Przygotowanie audio (remux)...")
        src_wav = os.path.join(outdir, stem + "_src.wav")
        log(f"remux wejścia do czystego WAV: {ain}")
        r = subprocess.run([ff, "-y", "-err_detect", "ignore_err", "-i", ain,
                            "-c:a", "pcm_s16le", src_wav], capture_output=True, text=True, **_win_kw())
        if not (os.path.exists(src_wav) and os.path.getsize(src_wav) > 1000000):
            log(f"BŁĄD remuxu, używam oryginału bezpośrednio. ffmpeg: {r.stderr[-300:]}")
            src_wav = ain
        ain_proc = src_wav

        # 1b. ODSZUMIANIE (opcjonalne, DOMYSLNIE OFF) - PRZED detekcja i cieciem, by
        # detektory (fillery/muzyka/odglosy) i eksport pracowaly na tym samym sygnale.
        # Zachowuje dlugosc (kompensacja DFN_TAIL), wiec ciecia, rozdzialy i RPP zostaja
        # na tej samej osi czasu. Porazka = log + praca na materiale nieodszumionym.
        if a.odszum:
            progress(8, "Odszumianie (DeepFilterNet)...")
            log(f"odszumiam modelem DeepFilterNet (tłumienie {a.odszum_sila} dB)...")
            _i = sf.info(ain_proc)
            _den = denoise_file(ain_proc, outdir, stem, a.odszum_sila, _i.samplerate, _i.channels)
            if _den:
                ain_proc = _den

        # 2. wczytanie 16k mono + detekcja (region GPU pod miedzyprocesowym zamkiem:
        # w trybie wsadowym enkode/ciecie poprzedniego pliku (CPU) nachodzi na
        # detekcje nastepnego (GPU), ale na karcie jest zawsze tylko JEDEN worker)
        progress(12, "Wczytywanie audio...")
        log(f"wczytuję {ain_proc} (16k mono do detekcji)")
        y, _ = librosa.load(ain_proc, sr=SR, mono=True)
        log(f"długość {len(y)/SR:.0f}s")

        with GpuLock():
            # detekcja iteracyjna: domyka wynik tak, by ponowne czyszczenie lapalo ~zero
            allc = detect_all_cuts_iterative(y, tnij_fillery, tnij_cisze, dokladny=a.dokladny)

            # 2a. ODGLOSY (chrzakniecia/kaszel, oddechy, mlasniecia) - dodatkowe ciecia
            # wykryte modelem AST, tylko poza mowa (guard w detect_sounds). Dodajemy do
            # allc PRZED filtrem muzyki, zeby tez podlegaly ochronie muzyki.
            kategorie = []
            if a.tnij_chrzakniecia: kategorie.append("chrzakniecia")
            if a.tnij_oddechy: kategorie.append("oddechy")
            if a.tnij_mlasniecia: kategorie.append("mlasniecia")
            if kategorie:
                opisy = {"chrzakniecia": "chrząknięcia/kaszel", "oddechy": "oddechy",
                         "mlasniecia": "mlaśnięcia"}
                log("wykrywanie odgłosów: " + ", ".join(opisy[k] for k in kategorie))
                # WYKRYWAJ ODGLOSY NA SYGNALE PO WYCIECIU FILLEROW/PAUZ, nie na oryginale.
                # Model AST slyszy przeciagniety filler "yyy/eee" jako mowe (Speech), wiec
                # guard mowy w detect_sounds chronilby oddechy sasiadujace z fillerami - a te
                # fillery i tak wycinamy. Na sygnale-bez-fillerow guard ocenia PRAWDZIWA mowe,
                # wiec oddech przy (bylym) fillerze staje sie izolowany i jest ciety; oddech
                # przy realnym slowie dalej chroniony.
                cur_samples = [(int(c["a"]*SR), int(c["b"]*SR)) for c in allc]
                if cur_samples:
                    y_snd, keeps_snd = _build_kept_signal(y, cur_samples)
                else:
                    y_snd, keeps_snd = y, [(0, len(y))]
                snd = detect_sounds(y_snd, kategorie)
                if snd and cur_samples:
                    # mapuj regiony z osi sygnalu-bez-fillerow na os ORYGINALU. Region
                    # przekraczajacy szew (granice keep-segmentu = miejsce po wycietym
                    # fillerze) ROZBIJAMY per segment, by NIGDY nie ciac przez szew w
                    # zachowane audio (inaczej ryzyko wciecia w realne slowo obok ciecia).
                    bounds = []; acc = 0
                    for s, e in keeps_snd:
                        bounds.append((acc/SR, (acc+(e-s))/SR, s/SR)); acc += (e - s)
                    mapped = []
                    for c in snd:
                        for (ns, ne, so) in bounds:
                            lo = max(c["a"], ns); hi = min(c["b"], ne)
                            if hi > lo:
                                mapped.append({"a": so+(lo-ns), "b": so+(hi-ns),
                                               "dur": hi-lo, "typ": c["typ"]})
                    snd = mapped
                if snd:
                    allc = allc + snd
                    allc.sort(key=lambda z: z["a"])
                    log(f"odgłosy: dodano {len(snd)} fragmentów do wycięcia")

            # 2b. MUZYKA: domyslnie chronimy fragmenty z muzyka - odrzucamy ciecia w muzyce
            # (model fillerow myli spiew/instrumenty z "yyy"). Wylaczane --bez-omijania-muzyki.
            glosy = wczytaj_wzorce(a.chronione_glosy)
            omijaj_muzyke = not a.bez_omijania_muzyki
            if omijaj_muzyke and allc:
                log(f"ochrona muzyki: włączona (próg {MUSIC_THRESH:.2f})")
                music = detect_music(y)
                before = len(allc)
                allc, removed = filter_cuts_by_music(allc, music)
                if music:
                    log(f"chronione (muzyka): odrzucono {removed}/{before} cięć")
                    # gdy prawie caly material to muzyka - ostrzez (bramka calego pliku)
                    total_music = sum(b-a for a, b in music)
                    if total_music >= 0.9 * (len(y)/SR):
                        log("UWAGA: materiał to niemal w całości muzyka - nic nie wycinam")
            elif not omijaj_muzyke:
                log("omijanie muzyki WYŁĄCZONE - tnę w całym materiale")
            # CHRONIONE GLOSY: NIEZALEZNE od ochrony muzyki - dziala takze gdy user
            # muzyki nie chroni. Wzorce podaje UZYTKOWNIK (automat wybieral zly glos:
            # lapal dominujacego mowce, nie synteze - patrz komentarz przy SPK_*).
            if glosy and allc:
                chronione = detect_protected_voices(y, glosy)
                if chronione:
                    before = len(allc)
                    allc, removed = filter_cuts_by_music(allc, chronione)
                    log(f"chronione głosy: odrzucono {removed}/{before} cięć")
        # <- tu zamek GPU zwolniony: dalej same operacje CPU/dysk (ciecie, enkode)

        json.dump({"fillers": allc}, open(os.path.join(outdir, f"ciecia_{stem}.json"), "w"), indent=1)

        # 3. keep-segments (wspolne dla ciecia i RPP)
        info = sf.info(ain_proc); sr = info.samplerate; ch = info.channels
        keeps, merged = compute_keeps(info.frames, sr, [(c["a"], c["b"]) for c in allc])

        # 3b. ROZDZIALY: przeczytaj z oryginalu, skoryguj czasy wzgledem ciec.
        # Dlugosc wyniku (w probkach): suma keepow minus crossfade na kazdym zlaczeniu.
        xf = int(XF_MS/1000*sr)
        out_frames = sum(e-s for s, e in keeps) - max(0, len(keeps)-1)*xf
        out_total_s = max(0, out_frames)/sr
        chapters_meta = None
        try:
            src_chapters = read_chapters(ain, ff)
        except Exception as e:
            log(f"rozdzialy: pominieto ({e!r})"); src_chapters = []
        if src_chapters:
            new_chapters = remap_chapters(src_chapters, keeps, sr, xf)
            # sidecar '<nazwa pliku wyjsciowego> chapters.txt' - etykieta timestamp / linia
            txt_path = os.path.splitext(aout)[0] + " chapters.txt"
            write_chapters_txt(txt_path, new_chapters)
            log(f"rozdzialy: {len(new_chapters)} -> {txt_path}")
            # ffmetadata do wstrzykniecia skorygowanych rozdzialow w plik wynikowy
            chapters_meta = os.path.join(outdir, stem + "_chapters.ffmeta.txt")
            write_chapters_ffmeta(chapters_meta, new_chapters, out_total_s)
        else:
            log("rozdzialy: brak w pliku wejsciowym")

        # 4. eksport AUDIO (jesli wybrany)
        if eksport in ("audio", "oba"):
            # formaty (klucze), ktore sensownie przenosza okladke (attached_pic)
            cover_ok = a.format in ("mp3", "aac", "alac", "flac")
            # wspolny enkoder WAV -> docelowy format (DRY: czysty i wyciete)
            # tagi (tytul/wykonawca/album...) i okladke kopiujemy z ORYGINALU wejscia
            def encode(wav_in, out_path, etap, chap_meta=None):
                progress(90, etap)
                log(etap)
                # NORMALIZACJA (opcjonalna): mierzymy gotowy, POCIETY material i liczymy
                # STALY gain. Pomiar musi byc na tym co realnie wychodzi - ciecia zmieniaja
                # glosnosc zintegrowana (usuwamy pauzy = material gestszy).
                af_norm = None
                if a.normalizuj:
                    tgt = LUFS_TARGETS[a.lufs]
                    progress(88, "Pomiar głośności (EBU R128)...")
                    cur_i, cur_tp = measure_loudness(wav_in)
                    if cur_i is None:
                        log("normalizacja: pomiar głośności nie powiódł się - pomijam")
                    else:
                        af_norm, gain, lim = loudness_filter(cur_i, cur_tp, tgt, sr)
                        log(f"głośność: {cur_i:.1f} LUFS / szczyt {cur_tp:.1f} dBTP "
                            f"-> cel {tgt:.0f} LUFS (wzmocnienie {gain:+.2f} dB, "
                            + (f"limiter true-peak {TP_CEILING} dBTP" if lim
                               else "bez limitera - zapas na szczyty wystarcza") + ")")
                # 2 wejscia: [0]=czysty WAV (audio), [1]=oryginal (zrodlo tagow/okladki)
                # opcjonalnie [2]=ffmetadata ze SKORYGOWANYMI rozdzialami
                enc = [ff, "-y", "-i", wav_in, "-i", ain]
                if chap_meta:
                    enc += ["-i", chap_meta]
                enc += ["-map", "0:a"]
                if cover_ok:
                    enc += ["-map", "1:v?"]          # okladka jesli istnieje (opcjonalnie)
                enc += ["-map_metadata", "1"]        # tagi tekstowe z oryginalu
                # rozdzialy: wstrzyknij skorygowane z wejscia 2, inaczej NIE kopiuj
                # starych (bledne czasy z oryginalu) - domyslnie ffmpeg by je przeniosl
                if chap_meta:
                    enc += ["-map_chapters", "2"]
                else:
                    enc += ["-map_chapters", "-1"]
                if af_norm:
                    enc += ["-af", af_norm]
                enc += ["-c:a", kodek]
                if stratny:
                    enc += ["-b:a", f"{a.bitrate}k"]
                if a.kanaly == "mono":
                    enc += ["-ac", "1"]
                elif a.kanaly == "stereo":
                    enc += ["-ac", "2"]
                if cover_ok:
                    enc += ["-c:v", "copy", "-disposition:v", "attached_pic"]
                enc.append(out_path)
                r2 = subprocess.run(enc, capture_output=True, text=True, **_win_kw())
                # fallback: gdyby przenoszenie tagow/okladki/rozdzialow zawiodlo, sprobuj bez nich
                if not (os.path.exists(out_path) and os.path.getsize(out_path) > 1000):
                    log("kopiowanie tagów nie powiodło się - eksport bez tagów")
                    enc2 = [ff, "-y", "-i", wav_in]
                    if chap_meta:
                        enc2 += ["-i", chap_meta, "-map", "0:a", "-map_chapters", "1"]
                    enc2 += ["-c:a", kodek]
                    if af_norm: enc2 += ["-af", af_norm]
                    if stratny: enc2 += ["-b:a", f"{a.bitrate}k"]
                    if a.kanaly == "mono": enc2 += ["-ac", "1"]
                    elif a.kanaly == "stereo": enc2 += ["-ac", "2"]
                    enc2.append(out_path)
                    r2 = subprocess.run(enc2, capture_output=True, text=True, **_win_kw())
                    if not (os.path.exists(out_path) and os.path.getsize(out_path) > 1000):
                        raise RuntimeError("eksport audio nie powiódł się: " + r2.stderr[-300:])

            progress(78, "Cięcie w pełnej jakości...")
            log("tnę strumieniowo w pełnej jakości...")
            wav_out = os.path.join(outdir, stem + "_tmp_czysty.wav")
            written = cut_stream(ain_proc, keeps, wav_out, sr, ch)
            di = info.frames/sr; do = written/sr
            log(f"wycięte: {di:.0f}s -> {do:.0f}s (usunięto {di-do:.0f}s = {(di-do)/60:.1f}min, {len(merged)} cięć)")
            encode(wav_out, aout, f"Eksport {a.format.upper()}...", chap_meta=chapters_meta)
            if not a.zostaw_wav and os.path.exists(wav_out):
                os.remove(wav_out)
            if chapters_meta and os.path.exists(chapters_meta):
                try: os.remove(chapters_meta)
                except Exception: pass

            # 4b. opcjonalnie: osobny plik z tym, co WYCIETE (do odsluchu/kontroli)
            if a.zapisz_wyciete and merged:
                progress(93, "Zapis wyciętych fragmentów...")
                log(f"zapisuję wycięte fragmenty ({len(merged)} kawałków)...")
                wav_rm = os.path.join(outdir, stem + "_tmp_wyciete.wav")
                write_removed(ain_proc, merged, wav_rm, sr, ch)
                # nazwa wycietych: bazuje na nazwie WEJSCIA (obok <nazwa>_czysty powstaje
                # <nazwa>_wyciete), niezaleznie od nazwy pliku wyjsciowego
                out_rm = os.path.join(outdir, stem + "_wyciete." + ext)
                encode(wav_rm, out_rm, "Eksport wyciętych fragmentów...")
                if not a.zostaw_wav and os.path.exists(wav_rm):
                    os.remove(wav_rm)
                log(f"wycięte zapisane: {out_rm}")
            elif a.zapisz_wyciete:
                log("nic nie wycięto - plik z wyciętymi fragmentami pominięty")

        # 5. eksport REAPER (jesli wybrany)
        rpp_path = None
        if eksport in ("reaper", "oba"):
            progress(95, "Eksport projektu Reapera...")
            # ZRODLO RPP = bezstratny FLAC zdekodowany z DOKLADNIE tego gapless-PCM,
            # na ktorym liczylismy ciecia (ain_proc). Dzieki temu SOFFS trafia 1:1 w te
            # sama tresc co czysty plik. MP3 jako zrodlo dawal rozjazd: Reaper seekuje/
            # dekoduje MP3 inaczej niz nasz ffmpeg-gapless (zmienny offset ~0.1-0.2s,
            # nie do skompensowania staly delay). FLAC nie ma encoder-delay -> SOFFS=probka/sr.
            rpp_src = os.path.join(outdir, stem + "_zrodlo.flac")
            fr = subprocess.run([ff, "-y", "-i", ain_proc, "-c:a", "flac", rpp_src],
                                capture_output=True, text=True, **_win_kw())
            if os.path.exists(rpp_src) and os.path.getsize(rpp_src) > 1000:
                src_delay = 0  # FLAC bez delay - zadnej korekty
                log(f"zrodlo projektu Reapera: bezstratny FLAC {os.path.basename(rpp_src)} "
                    f"({os.path.getsize(rpp_src)//(1024*1024)} MB, ciecia trafiaja 1:1)")
            else:
                # awaryjnie: wskaz oryginal (MP3) z kompensacja encoder-delay
                rpp_src = ain
                src_delay = mp3_encoder_delay(ff, ain)
                log(f"FLAC zrodlowy nie powstal ({fr.stderr[-200:]}) - RPP wskazuje oryginal "
                    f"z kompensacja delay +{src_delay} probek (moze nie byc 1:1)")
            if a.wariant_rpp in ("gotowy", "oba"):
                rpp_path = os.path.join(outdir, stem + ".RPP")
                export_rpp(rpp_path, rpp_src, keeps, merged, sr, src_delay=src_delay)
            if a.wariant_rpp in ("przejrzenie", "oba"):
                rpp_m = os.path.join(outdir, stem + "_do_przejrzenia.RPP")
                export_rpp_marked(rpp_m, rpp_src, keeps, merged, sr, src_delay=src_delay)
                if rpp_path is None:
                    rpp_path = rpp_m

        if src_wav != ain and os.path.exists(src_wav):
            os.remove(src_wav)
        # posredni WAV po odszumianiu (gdy uzyte) - sprzataj jak src_wav
        if ain_proc not in (ain, src_wav) and os.path.exists(ain_proc):
            try: os.remove(ain_proc)
            except Exception: pass
        if chapters_meta and os.path.exists(chapters_meta):
            try: os.remove(chapters_meta)
            except Exception: pass

        progress(100, "Gotowe")
        wynik = aout if eksport in ("audio", "oba") else rpp_path
        log(f"GOTOWE: {wynik}")
        emit("DONE", wynik)
    except Exception as e:
        import traceback
        log("BŁĄD: " + repr(e))
        for ln in traceback.format_exc().splitlines():
            log("  " + ln)
        emit("ERR", repr(e))
        sys.exit(1)

if __name__ == "__main__":
    main()
