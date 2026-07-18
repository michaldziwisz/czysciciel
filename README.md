# Czyściciel

**Czyściciel** to program dla Windows, który automatycznie usuwa z nagrań mowy
wtrącenia typu „yyy", „eee", „mmm" (tzw. fillery) oraz skraca zbyt długie pauzy —
tak, żeby nagranie brzmiało naturalnie i zwięźle. Idealny do podcastów, audycji,
wywiadów i wykładów.

Program jest w pełni obsługiwany z klawiatury i przez czytniki ekranu
(NVDA, JAWS) — powstał z myślą o dostępności.

---

## Co potrafi

- **Usuwa fillery** („yyy", „eee", „mmm") rozpoznając je modelem sztucznej
  inteligencji — z samego dźwięku, nie z transkrypcji. Działa po polsku i
  niezależnie od tego, kto mówi.
- **Skraca za długie pauzy**, zostawiając naturalny oddech (trzy poziomy
  agresywności).
- **Przetwarza wiele plików naraz** (tryb wsadowy).
- **Zapisuje w dowolnym formacie**: MP3, AAC, Opus, Ogg Vorbis, WMA, AC3,
  a także bezstratnie: FLAC, ALAC, WAV. Do tego wybór jakości (bitrate) i
  liczby kanałów (mono / stereo / jak w źródle).
- **Eksportuje projekt Reapera (.RPP)** — jeśli chcesz dalej edytować materiał
  ręcznie, dostajesz gotowy, niedestrukcyjny projekt z zaznaczonymi cięciami.
- **Wybiera GPU albo procesor automatycznie** — na komputerze z kartą NVIDIA
  liczy szybciej, bez karty też zadziała.

---

## Instalacja

Program **nie wymaga instalatora**.

1. Wejdź na stronę [Releases](../../releases) i pobierz plik
   **`Czysciciel-windows.zip`**.
2. Rozpakuj go w dowolnym miejscu (np. na Pulpicie albo w `Dokumentach`).
3. Wejdź do rozpakowanego folderu `Czysciciel` i uruchom **`Czysciciel.exe`**.

### Pierwsze uruchomienie

Przy **pierwszym** starcie Czyściciel jednorazowo pobiera z internetu potrzebne
składniki (silnik AI, model rozpoznawania mowy i narzędzie do obróbki dźwięku).
Trafiają one do Twojego profilu użytkownika w folderze:

    %LOCALAPPDATA%\Czysciciel

- Na komputerze z kartą **NVIDIA** pobierze wersję GPU (ok. 5 GB) — szybsze
  przetwarzanie.
- Bez karty NVIDIA pobierze wersję na **procesor** (ok. 2,5 GB) — działa wszędzie.
- Postęp pobierania widać na pasku i w dzienniku (są odczytywane przez czytnik
  ekranu, więc wiesz, że program pracuje, a nie zawiesił się).

**Internet jest potrzebny tylko raz.** Każde kolejne uruchomienie jest
natychmiastowe i działa bez sieci.

---

## Jak używać — krok po kroku

1. **Dodaj nagrania.** Kliknij „Dodaj pliki..." (możesz zaznaczyć wiele naraz)
   albo „Dodaj folder..." (doda wszystkie nagrania z wybranego folderu).
2. **Zaznacz, co przetworzyć.** Na liście każdy plik ma pole wyboru —
   spacja zaznacza i odznacza.
3. **Wybierz, co wycinać** (sekcja „Co wycinać"):
   - tylko fillery,
   - tylko ciszę (za długie pauzy),
   - fillery i ciszę (domyślnie).
4. **Dostrój opcje** (opcjonalnie): poziom skracania pauz, minimalną długość
   fillera, format wyjściowy, jakość i kanały.
5. **Wybierz, co zapisać** (sekcja „Co zapisać"): sam plik audio, sam projekt
   Reapera, albo jedno i drugie.
6. **Wskaż folder wyjściowy** (opcjonalnie) — domyślnie wynik ląduje obok
   pliku źródłowego.
7. Naciśnij **„Uruchom czyszczenie"** (lub klawisz **F5**).

Gdy program skończy, pojawi się okno z podsumowaniem (ile plików przetworzono).
Przyciskiem „Otwórz folder wyniku" szybko przejdziesz do gotowych plików.

Wynik audio nazywa się `nazwa_czysty.<format>` (np. `wywiad_czysty.mp3`), a obok
powstaje plik `ciecia_nazwa.json` z listą wszystkich cięć (do wglądu).

---

## Poziomy skracania pauz

| Poziom       | Nie rusza pauz do | Dłuższe skraca do | Efekt              |
|--------------|-------------------|-------------------|--------------------|
| zachowawczy  | 0,70 s            | 0,60 s            | ledwo zauważalne   |
| umiarkowany  | 0,50 s            | 0,45 s            | dobry kompromis    |
| zwarty       | 0,35 s            | 0,30 s            | radiowe, szybkie tempo |

**Minimalna długość fillera** (domyślnie 0,30 s) chroni przed wycięciem lekko
przeciągniętego „y" w środku słowa — krótsze wtręty są pomijane.

---

## Formaty wyjściowe

| Format | Rozszerzenie | Rodzaj      | Kiedy wybrać                         |
|--------|--------------|-------------|--------------------------------------|
| MP3    | .mp3         | stratny     | uniwersalny, wszędzie działa         |
| AAC    | .m4a         | stratny     | dobra jakość przy mniejszym pliku    |
| Opus   | .opus        | stratny     | najlepsza jakość mowy przy niskim bitrate |
| Ogg Vorbis | .ogg     | stratny     | otwarty format, dobra jakość         |
| WMA    | .wma         | stratny     | zgodność ze starszym oprogramowaniem |
| AC3    | .ac3         | stratny     | dźwięk do wideo                      |
| FLAC   | .flac        | bezstratny  | pełna jakość, mniejszy niż WAV       |
| ALAC   | .m4a         | bezstratny  | pełna jakość w świecie Apple         |
| WAV    | .wav         | bezstratny  | surowy materiał do dalszej obróbki   |

Dla formatów stratnych ustawiasz **bitrate** (im wyższy, tym lepsza jakość i
większy plik). Dla bezstratnych bitrate nie ma znaczenia i jest wyłączony.

---

## Eksport do Reapera

Jeśli chcesz mieć kontrolę nad każdym cięciem, zaznacz eksport projektu Reapera.
Czyściciel utworzy plik `.RPP`, który:

- odwołuje się do **oryginalnego** nagrania (nic nie jest bezpowrotnie usuwane),
- ma materiał już poskładany po cięciach, ale **w pełni edytowalny** — każde
  cięcie możesz cofnąć lub przesunąć,
- zawiera znaczniki w miejscach cięć, żeby łatwo je odnaleźć.

Wystarczy otworzyć plik `.RPP` w [Reaperze](https://www.reaper.fm/).

---

## Jak to działa (w skrócie)

1. **Rozpoznanie fillerów.** Model `classla/wav2vecbert2-filledPause` analizuje
   nagranie w kawałkach po 20 milisekund i decyduje, gdzie jest filler. Robi to
   „ze słuchu" — z sygnału dźwiękowego — dlatego łapie „yyy", których zwykła
   transkrypcja w ogóle nie zapisuje.
2. **Wykrycie pauz.** Program mierzy poziom głośności i znajduje zbyt długie
   ciche fragmenty.
3. **Cięcie.** Wycinane fragmenty są usuwane płynnie (z delikatnym
   przenikaniem na łączeniach i marginesem bezpieczeństwa od granic słów), więc
   nie słychać „przeskoków". Cała operacja zachowuje pełną jakość dźwięku.
4. **Zapis** w wybranym formacie oraz — opcjonalnie — projekt Reapera.

---

## Wymagania

- Windows 64-bit.
- Wersja GPU: karta NVIDIA ze sterownikiem obsługującym CUDA 12.
- Połączenie z internetem **przy pierwszym uruchomieniu**.
- Około 3–6 GB miejsca na dysku (jednorazowo, na pobrane składniki).

---

## Najczęstsze pytania

**Czy moje nagrania są gdzieś wysyłane?**
Nie. Całe przetwarzanie odbywa się na Twoim komputerze. Internet jest używany
wyłącznie raz — do pobrania składników programu.

**Pierwsze uruchomienie długo pobiera — czy to normalne?**
Tak. To jednorazowe pobranie kilku gigabajtów. Kolejne starty są natychmiastowe.

**Nie mam karty NVIDIA — czy program zadziała?**
Tak, automatycznie użyje procesora. Będzie wolniej, ale wynik jest ten sam.

**Program wyciął za dużo / za mało.**
Zmień poziom skracania pauz i minimalną długość fillera, albo wybierz tryb
„tylko fillery" lub „tylko cisza". Zawsze możesz też wyeksportować projekt
Reapera i dopracować cięcia ręcznie.

---

## Licencja i podziękowania

Kod programu: licencja **MIT** (plik [`LICENSE`](LICENSE)).

Czyściciel korzysta z otwartych komponentów — pełna lista wraz z licencjami jest
w pliku [`TRZECIE_STRONY.txt`](TRZECIE_STRONY.txt). Najważniejsze:

- model `classla/wav2vecbert2-filledPause` (Apache-2.0),
- PyTorch, Transformers, librosa, soundfile,
- ffmpeg (wariant LGPL),
- wxPython (interfejs).

---

## Dla programistów — budowanie ze źródeł

Wymagany Windows z Pythonem 3.12:

```bat
pip install wxpython pyinstaller
build.bat
```

Wynik: `dist\Czysciciel\Czysciciel.exe` (lekki launcher; ciężkie składniki
dociągane są przy pierwszym uruchomieniu).

Gotowe paczki buduje też automatycznie GitHub Actions — każdy tag `v*` tworzy
Release z gotowym plikiem `Czysciciel-windows.zip`.
