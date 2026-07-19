#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Czysciciel - bootstrap srodowiska uruchomieniowego (Architektura 1).

Przy PIERWSZYM uruchomieniu dociaga "duze klocki" do:
    %LOCALAPPDATA%\\Czysciciel\\runtime
i przygotowuje wszystko do dzialania offline przy kolejnych startach:

  1. uv (maly, samodzielny instalator Pythona/pakietow od Astral) -> tools/uv.exe
  2. standalone Python + venv (uv sam pobiera Pythona 3.12)
  3. torch: CUDA (cu121) jesli wykryto karte NVIDIA, inaczej CPU-only (~200 MB)
  4. transformers / librosa / soundfile / pandas / numpy / huggingface_hub
  5. ffmpeg (statyczny build LGPL) -> tools/ffmpeg.exe
  6. model classla/wav2vecbert2-filledPause (~2.2 GB) -> hf_cache

Idempotentny: gotowosc znaczy plik READY z pasujaca wersja RUNTIME_VER.
Protokol postepu (STDOUT, parsowany przez GUI):
    BOOT|<0..100>|<opis>
    BLOG|<linia dziennika>
    BOOTOK|<sciezka_python>|<sciezka_ffmpeg>
    BOOTERR|<komunikat>
"""
import os, sys, json, subprocess, shutil, urllib.request, zipfile, tempfile, ssl, time

RUNTIME_VER = "2"

# --- przypiete wersje (zgodne z dzialajacym cleaner/.venv) ---
PKGS_COMMON = [
    "transformers==5.14.1",
    "librosa==0.11.0",
    "soundfile==0.14.0",
    "pandas==3.0.3",
    "numpy==2.4.6",
    "huggingface_hub==1.24.0",
]
# torch: cu128 obejmuje karty od sm_75 (RTX 20xx) po sm_120 (RTX 50xx Blackwell).
# cu121 (do 2.5.1) NIE mial sm_120 - RTX 50xx padal "no kernel image". 2.7.0 to
# pierwszy cu128 z Blackwell. CPU-only osobno (male ~200MB).
TORCH_VER = "torch==2.7.0"
TORCH_CUDA_INDEX = "https://download.pytorch.org/whl/cu128"
TORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"

UV_URL = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
# ffmpeg statyczny (LGPL) - build BtbN. release/latest niezmiennie dostepny.
FFMPEG_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-lgpl-shared.zip"
MODEL = "classla/wav2vecbert2-filledPause"

def blog(m): print(f"BLOG|{m}", flush=True)
def boot(pct, msg): print(f"BOOT|{int(pct)}|{msg}", flush=True)

def runtime_root():
    base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    return os.path.join(base, "Czysciciel")

def _paths():
    root = runtime_root()
    return {
        "root": root,
        "runtime": os.path.join(root, "runtime"),
        "tools": os.path.join(root, "tools"),
        "venv": os.path.join(root, "runtime", "venv"),
        "hf": os.path.join(root, "hf_cache"),
        "model": os.path.join(root, "model"),  # PLASKI katalog modelu (bez symlinkow cache)
        "ready": os.path.join(root, "runtime", "READY"),
        "uv": os.path.join(root, "tools", "uv.exe"),
        "ffmpeg": os.path.join(root, "tools", "ffmpeg.exe"),
        "vpy": os.path.join(root, "runtime", "venv", "Scripts", "python.exe"),
    }

def _has_nvidia():
    """Czy jest karta NVIDIA (probujemy nvidia-smi.exe z System32)."""
    smi = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe")
    for cmd in ([smi] if os.path.exists(smi) else []) + [["nvidia-smi"]]:
        try:
            r = subprocess.run(cmd if isinstance(cmd, list) else [cmd],
                               capture_output=True, text=True, timeout=15)
            if r.returncode == 0 and "NVIDIA" in (r.stdout or ""):
                return True
        except Exception:
            pass
    return False

def _download(url, dest, desc, base_pct, span_pct):
    blog(f"pobieranie: {desc}")
    ctx = ssl.create_default_context()
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "Czysciciel/1.0"})
    with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        got = 0; chunk = 1024*256; last = 0
        with open(tmp, "wb") as f:
            while True:
                b = resp.read(chunk)
                if not b: break
                f.write(b); got += len(b)
                if total:
                    p = base_pct + span_pct*got/total
                    if time.time()-last > 0.5:
                        boot(p, f"{desc}: {got//(1024*1024)}/{total//(1024*1024)} MB")
                        last = time.time()
    os.replace(tmp, dest)
    blog(f"pobrano: {desc} ({os.path.getsize(dest)//(1024*1024)} MB)")

def _unzip_find(zip_path, member_suffix, dest_file):
    """Wypakuj z zip pierwszy plik konczacy sie member_suffix do dest_file."""
    with zipfile.ZipFile(zip_path) as z:
        cand = [n for n in z.namelist() if n.lower().endswith(member_suffix.lower())]
        if not cand:
            raise RuntimeError(f"nie znaleziono {member_suffix} w {os.path.basename(zip_path)}")
        name = cand[0]
        with z.open(name) as src, open(dest_file, "wb") as out:
            shutil.copyfileobj(src, out)
    return dest_file

def _unzip_dll_neighbours(zip_path, dest_dir):
    """ffmpeg-shared potrzebuje swoich .dll obok exe - wypakuj wszystkie .dll z bin/."""
    with zipfile.ZipFile(zip_path) as z:
        for n in z.namelist():
            if n.lower().endswith(".dll") and "/bin/" in n.lower():
                base = os.path.basename(n)
                with z.open(n) as src, open(os.path.join(dest_dir, base), "wb") as out:
                    shutil.copyfileobj(src, out)

def _run(cmd, desc, env=None):
    blog(f"$ {desc}")
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "")[-800:]
        raise RuntimeError(f"{desc} nie powiodlo sie (kod {r.returncode}):\n{tail}")
    return r

UPDATE_EVERY_DAYS = 7  # jak czesto sprawdzac aktualizacje modelu

def maybe_update_model(P, env):
    """Sprawdza aktualizacje modelu NAJWYZEJ raz na UPDATE_EVERY_DAYS dni.
    snapshot_download sciaga TYLKO zmienione/nowe pliki (ETag) - gdy nic sie nie
    zmienilo, nie pobiera nic. Brak sieci = cicha rezygnacja (dzialamy z cache).
    Znacznik ostatniej proby w pliku last_update_check."""
    chk = os.path.join(P["runtime"], "last_update_check")
    try:
        last = float(open(chk).read().strip()) if os.path.exists(chk) else 0
    except Exception:
        last = 0
    if time.time() - last < UPDATE_EVERY_DAYS*86400:
        return  # za wczesnie - nie zawracamy glowy siecia
    blog("sprawdzam aktualizacje modelu (raz na tydzien)...")
    dl = os.path.join(P["runtime"], "_upd.py")
    with open(dl, "w", encoding="utf-8") as f:
        f.write(
            "import os, sys\n"
            "try:\n"
            "    from huggingface_hub import snapshot_download\n"
            f"    snapshot_download('{MODEL}', local_dir=r'{P['model']}', etag_timeout=10)\n"
            "    print('UPD_OK')\n"
            "except Exception as e:\n"
            "    print('UPD_SKIP', repr(e)[:120]); sys.exit(0)\n"
        )
    try:
        r = subprocess.run([P["vpy"], dl], capture_output=True, text=True, env=env, timeout=180)
        if "UPD_OK" in (r.stdout or ""):
            blog("model aktualny (lub pobrano nowsza wersje)")
        else:
            blog("brak sieci/aktualizacji - dzialam z lokalnego modelu")
    except Exception:
        blog("sprawdzenie aktualizacji pominiete - dzialam z lokalnego modelu")
    finally:
        try: os.remove(dl)
        except Exception: pass
    # zapisz znacznik NIEZALEZNIE od wyniku (nie ponawiaj codziennie przy braku sieci)
    try:
        open(chk, "w").write(str(int(time.time())))
    except Exception:
        pass

def ensure(force_device=None):
    """Glowna funkcja. force_device: None(auto)/'cuda'/'cpu'. Zwraca (vpy, ffmpeg)."""
    P = _paths()
    os.makedirs(P["tools"], exist_ok=True)
    os.makedirs(P["runtime"], exist_ok=True)
    os.makedirs(P["hf"], exist_ok=True)

    # juz gotowe?
    if os.path.exists(P["ready"]):
        try:
            meta = json.load(open(P["ready"], encoding="utf-8"))
            if meta.get("ver") == RUNTIME_VER and os.path.exists(P["vpy"]) and os.path.exists(P["ffmpeg"]):
                boot(100, "Srodowisko gotowe")
                blog(f"srodowisko juz zainstalowane ({meta.get('device')})")
                # okresowe, nieblokujace sprawdzenie aktualizacji modelu
                env = os.environ.copy()
                env["VIRTUAL_ENV"] = P["venv"]
                maybe_update_model(P, env)
                return P["vpy"], P["ffmpeg"]
        except Exception:
            pass

    # wybor GPU/CPU
    if force_device in ("cuda", "cpu"):
        device = force_device
    else:
        device = "cuda" if _has_nvidia() else "cpu"
    blog(f"tryb obliczen: {device.upper()}"
         + (" (wykryto karte NVIDIA)" if device == "cuda" else " (brak karty NVIDIA - CPU)"))

    boot(1, "Przygotowanie instalacji...")

    # 1. uv
    if not os.path.exists(P["uv"]):
        z = os.path.join(P["tools"], "uv.zip")
        _download(UV_URL, z, "uv (instalator)", 1, 4)
        _unzip_find(z, "uv.exe", P["uv"])
        os.remove(z)
    boot(6, "uv gotowe")

    # 2. venv (uv sam pobiera Pythona 3.12)
    if not os.path.exists(P["vpy"]):
        _run([P["uv"], "venv", "--python", "3.12", P["venv"]], "tworzenie venv (Python 3.12)")
    boot(12, "Srodowisko Pythona gotowe")

    env = os.environ.copy()
    env["VIRTUAL_ENV"] = P["venv"]
    env["UV_CACHE_DIR"] = os.path.join(P["root"], "uv_cache")

    # 3. torch (CUDA lub CPU)
    if device == "cuda":
        boot(15, "Instalacja torch (CUDA, ~2.5 GB)...")
        _run([P["uv"], "pip", "install", "--python", P["vpy"],
              TORCH_VER, "--index-url", TORCH_CUDA_INDEX], "instalacja torch CUDA", env=env)
    else:
        boot(15, "Instalacja torch (CPU)...")
        _run([P["uv"], "pip", "install", "--python", P["vpy"],
              TORCH_VER, "--index-url", TORCH_CPU_INDEX], "instalacja torch CPU", env=env)
    boot(55, "torch zainstalowany")

    # 4. reszta pakietow
    boot(58, "Instalacja bibliotek (transformers, librosa...)...")
    _run([P["uv"], "pip", "install", "--python", P["vpy"], *PKGS_COMMON],
         "instalacja bibliotek", env=env)
    boot(70, "Biblioteki gotowe")

    # 5. ffmpeg
    if not os.path.exists(P["ffmpeg"]):
        z = os.path.join(P["tools"], "ffmpeg.zip")
        _download(FFMPEG_URL, z, "ffmpeg", 70, 8)
        _unzip_find(z, "bin/ffmpeg.exe", P["ffmpeg"])
        _unzip_dll_neighbours(z, P["tools"])  # .dll obok (build shared)
        os.remove(z)
    boot(80, "ffmpeg gotowy")

    # 6. model - do PLASKIEGO katalogu (local_dir), bez struktury cache HF/symlinkow.
    # Na Windows bez Developer Mode symlinki cache nie dzialaly i preprocessor_config.json
    # bywal nieczytelny -> "Can't load feature extractor". Plaski katalog to eliminuje.
    boot(82, "Pobieranie modelu AI (~2.2 GB)...")
    dl = os.path.join(P["runtime"], "_dlmodel.py")
    with open(dl, "w", encoding="utf-8") as f:
        f.write(
            "from huggingface_hub import snapshot_download\n"
            f"p=snapshot_download('{MODEL}', local_dir=r'{P['model']}')\n"
            "print('MODEL_OK', p)\n"
        )
    _run([P["vpy"], dl], "pobieranie modelu", env=env)
    os.remove(dl)
    boot(98, "Model pobrany")

    # marker gotowosci
    json.dump({"ver": RUNTIME_VER, "device": device, "ts": int(time.time())},
              open(P["ready"], "w", encoding="utf-8"))
    boot(100, "Instalacja zakonczona")
    blog("srodowisko gotowe - kolejne uruchomienia beda natychmiastowe")
    return P["vpy"], P["ffmpeg"]

if __name__ == "__main__":
    fd = None
    if "--cpu" in sys.argv: fd = "cpu"
    if "--cuda" in sys.argv: fd = "cuda"
    try:
        vpy, ff = ensure(force_device=fd)
        print(f"BOOTOK|{vpy}|{ff}", flush=True)
    except Exception as e:
        import traceback
        blog("BLAD: " + repr(e))
        for ln in traceback.format_exc().splitlines():
            blog("  " + ln)
        print(f"BOOTERR|{e}", flush=True)
        sys.exit(1)
