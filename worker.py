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
import os, sys, json, subprocess, time

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
# Sciezka do PLASKIEGO katalogu modelu (bootstrap pobiera go tam przez local_dir).
# GUI ustawia CZYSCICIEL_MODEL_DIR; niezaleznie liczymy tez domyslna z LOCALAPPDATA
# (odpornosc na blad sciezki w GUI - kandydatow probujemy po kolei).
def _model_candidates():
    cands = []
    md = os.environ.get("CZYSCICIEL_MODEL_DIR", "").strip()
    if md: cands.append(md)
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    cands.append(os.path.join(base, "Czysciciel", "model"))
    return cands
def _model_ref():
    """Zwraca (sciezka/repo, local_files_only). Preferuj lokalny plaski katalog;
    sprawdz WSZYSTKICH kandydatow (env + domyslny), inaczej spadnij na repo-id."""
    for d in _model_candidates():
        if os.path.exists(os.path.join(d, "preprocessor_config.json")):
            return d, True
    return MODEL, True
SR = 16000; CHUNK = 30.0; FS = 0.020
CUT = 0.30                 # min dlugosc fillera
KEEP = 0.50; TARGET = 0.45 # pauzy: do KEEP zostaw, dluzsze skroc do TARGET
SAFE_MS = 20; XF_MS = 25

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
def _load_heavy():
    global np, torch, sf, librosa, pd, AutoFeatureExtractor, Wav2Vec2BertForAudioFrameClassification
    import numpy as np, torch, soundfile as sf, librosa
    import pandas as pd
    from transformers import AutoFeatureExtractor, Wav2Vec2BertForAudioFrameClassification

# ---------- FILLERY ----------
def f2i(frames, off, n_total):
    res = []; ndf = pd.DataFrame({"t": [FS*i for i in range(len(frames))], "f": frames}).dropna()
    idx = ndf.f.diff()[ndf.f.diff() != 0].index.values
    for si, ei in pairwise(idx):
        if ndf.loc[si:ei-1, "f"].mode()[0] != 0:
            res.append((round(ndf.loc[si, "t"], 3), round(ndf.loc[ei, "t"], 3)))
    res = [i for i in res if i[1]-i[0] >= CUT and i[0] != 0.0 and i[1] != FS*len(frames)]
    return [(a+off, b+off) for a, b in res]

def detect_fillers(y_full):
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    half = (dev == "cuda")
    log(f"model na {dev}{' fp16' if half else ''}")
    progress(15, f"Ladowanie modelu ({dev})...")
    ref, lfo = _model_ref()
    fe = AutoFeatureExtractor.from_pretrained(ref, local_files_only=lfo)
    model = Wav2Vec2BertForAudioFrameClassification.from_pretrained(
        ref, torch_dtype=torch.float16 if half else torch.float32,
        local_files_only=lfo).to(dev)
    model.eval()
    iv = []; step = int(CHUNK*SR); n = len(y_full)
    nch = (n+step-1)//step
    for ci, cs in enumerate(range(0, n, step)):
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
        iv += f2i(pred.tolist(), cs/SR, n)
        # detekcja fillerow to 20..70% paska
        progress(20 + 50*(ci+1)/max(nch, 1), f"Detekcja fillerow: {ci+1}/{nch}")
        if ci % 20 == 0: log(f"  fillery: kawalek {ci+1}/{nch}")
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
    global CUT, KEEP, TARGET
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

        # 2. wczytanie 16k mono + detekcja
        progress(12, "Wczytywanie audio...")
        log(f"wczytuję {ain_proc} (16k mono do detekcji)")
        y, _ = librosa.load(ain_proc, sr=SR, mono=True)
        log(f"długość {len(y)/SR:.0f}s")

        fillers = detect_fillers(y) if tnij_fillery else []
        log(f"fillery: {len(fillers)}")
        progress(72, "Detekcja ciszy (pauz)..." if tnij_cisze else "Pomijam ciszę...")
        pauses = detect_pauses(y) if tnij_cisze else []
        log(f"pauzy do skrócenia: {len(pauses)}")

        allc = [{"a": aa, "b": bb, "dur": bb-aa, "typ": "filler"} for aa, bb in fillers] + \
               [{"a": aa, "b": bb, "dur": bb-aa, "typ": "pauza"} for aa, bb in pauses]
        allc.sort(key=lambda z: z["a"])
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
