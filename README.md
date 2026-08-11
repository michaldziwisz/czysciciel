# Czyściciel

**Czyściciel** to program dla Windows, który automatycznie porządkuje nagrania
mowy: usuwa wtrącenia typu „yyy", „eee", „mmm" (tzw. fillery), skraca zbyt długie
pauzy, a na życzenie wycina też chrząknięcia, oddechy i mlaśnięcia, odszumia
materiał i wyrównuje jego głośność. Idealny do podcastów, audycji, wywiadów
i wykładów.

Program jest w pełni obsługiwany z klawiatury i przez czytniki ekranu
(NVDA, JAWS) — powstał z myślą o dostępności.

---

## Co potrafi

**Cięcie**

- **Usuwa fillery** („yyy", „eee", „mmm") rozpoznając je modelem sztucznej
  inteligencji — z samego dźwięku, nie z transkrypcji. Działa po polsku i
  niezależnie od tego, kto mówi.
- **Skraca za długie pauzy**, zostawiając naturalny oddech (trzy poziomy
  agresywności).
- **Wycina odgłosy** — osobno włączane: chrząknięcia/kaszel/kichnięcia,
  oddechy/wdechy/pociągnięcia nosem, mlaśnięcia/cmoknięcia. Domyślnie wyłączone.
- **Tryb dokładny** — powtarza detekcję tyle razy, aż nie zostanie już nic do
  wycięcia (wolniej, ale wynik jest „domknięty").

**Czego nie tknie**

- **Fragmenty z muzyką** — drugi model AI wykrywa muzykę i blokuje w tych
  miejscach cięcia (model fillerów myli przeciągnięte dźwięki muzyczne z „yyy").
  Domyślnie włączone, z suwakiem czułości.
- **Wskazane głosy** — jeśli w nagraniu pojawia się mowa syntetyczna (np. czytnik
  ekranu) albo inny głos, którego program ruszać nie powinien, dodajesz jego
  krótką próbkę do listy „Chronione głosy". Program rozpoznaje ten głos i blokuje
  tam wszystkie cięcia. Wzorców może być wiele i zostają zapamiętane między
  sesjami.

**Obróbka dźwięku** (opcjonalna, domyślnie wyłączona)

- **Wyrównanie głośności (LUFS)** do wybranego celu: -16 (podcast/Apple),
  -23 (norma EBU R128 dla radia i TV) albo -14 (Spotify). Stałe wzmocnienie plus
  zabezpieczenie szczytów — bez kompresji dynamiki.
- **Odszumianie** modelem DeepFilterNet (trzy siły) — przydatne przy nagraniach
  zdalnych; na czystym materiale studyjnym nie jest potrzebne.

**Zapis i praca**

- **Przetwarza wiele plików naraz** (tryb wsadowy, do czterech równolegle,
  z zamkiem na kartę graficzną, żeby zadania nie biły się o GPU).
- **Zapisuje w dowolnym formacie**: MP3, AAC, Opus, Ogg Vorbis, WMA, AC3,
  a także bezstratnie: FLAC, ALAC, WAV. Do tego wybór jakości (bitrate) i
  liczby kanałów (mono / stereo / jak w źródle).
- **Eksportuje projekt Reapera (.RPP)** w dwóch wariantach: gotowy (cięcia już
  wykonane) albo do przejrzenia (fragmenty oznaczone „WYTNIJ").
- **Zachowuje rozdziały** — jeśli plik wejściowy ma rozdziały (chaptery), ich
  pozycje są przeliczane po cięciach, zapisywane w pliku wynikowym i dodatkowo
  w czytelnym pliku obok.
- **Może zapisać osobno to, co wycięte** — do odsłuchu i kontroli.
- **Pamięta ustawienia** między uruchomieniami.
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
składniki (silnik AI, modele i narzędzie do obróbki dźwięku). Trafiają one do
Twojego profilu użytkownika w folderze:

    %LOCALAPPDATA%\Czysciciel

- Na komputerze z kartą **NVIDIA** pobierze wersję GPU (ok. 5 GB) — szybsze
  przetwarzanie.
- Bez karty NVIDIA pobierze wersję na **procesor** (ok. 2,5 GB) — działa wszędzie.
- Postęp pobierania widać na pasku i w dzienniku (są odczytywane przez czytnik
  ekranu, więc wiesz, że program pracuje, a nie zawiesił się).

Mniejsze modele (wykrywanie muzyki i odgłosów, rozpoznawanie mowy, rozpoznawanie
głosów, odszumianie) dociągają się osobno i **bez przebudowy całego środowiska** —
jeśli masz już program zainstalowany, nowe funkcje pobiorą tylko swoje kilkadziesiąt
megabajtów.

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
   Reapera, albo jedno i drugie; przy projekcie Reapera — jego wariant.
6. **Zdecyduj o ochronie i dodatkach**: pomijanie muzyki (z suwakiem czułości),
   wycinanie odgłosów, chronione głosy, wyrównanie głośności, odszumianie.
7. **Wskaż folder wyjściowy** (opcjonalnie) — domyślnie wynik ląduje obok
   pliku źródłowego. Ustaw też, ile plików przetwarzać równolegle.
8. Naciśnij **„Uruchom czyszczenie"** (lub klawisz **F5**). Przycisk
   „Zatrzymaj" przerywa pracę.

Gdy program skończy, pojawi się okno z podsumowaniem (ile plików przetworzono).
Przyciskiem „Otwórz folder wyniku" szybko przejdziesz do gotowych plików.

### Co powstaje

| Plik | Kiedy | Co zawiera |
|------|-------|------------|
| `nazwa_czysty.<format>` | zawsze przy eksporcie audio | wyczyszczone nagranie |
| `ciecia_nazwa.json` | zawsze | lista wszystkich cięć (do wglądu) |
| `nazwa_wyciete.<format>` | gdy zaznaczysz opcję | sam materiał usunięty — do odsłuchu |
| `nazwa_czysty chapters.txt` | gdy źródło ma rozdziały | rozdziały z pozycjami po cięciach |
| `nazwa.RPP` | przy eksporcie projektu | projekt Reapera |

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
Czyściciel utworzy plik `.RPP`, który odwołuje się do oryginalnego nagrania (nic
nie jest bezpowrotnie usuwane) i jest w pełni edytowalny — każde cięcie możesz
cofnąć lub przesunąć. Do wyboru są dwa warianty:

- **Gotowy** — fragmenty już wycięte i dosunięte; otwierasz i słuchasz efektu.
- **Do przejrzenia** — nic nie jest usunięte, a fragmenty do wycięcia są
  oznaczone jako „WYTNIJ"; sam decydujesz, które faktycznie skasować.
- Możesz też poprosić o **oba projekty naraz**.

Wystarczy otworzyć plik `.RPP` w [Reaperze](https://www.reaper.fm/).

---

## Jak to działa (w skrócie)

1. **Odszumianie** (jeśli włączone) — model DeepFilterNet czyści tło jeszcze
   przed analizą, żeby detekcja pracowała na czystszym sygnale.
2. **Rozpoznanie fillerów.** Model `classla/wav2vecbert2-filledPause` analizuje
   nagranie w kawałkach po 20 milisekund i decyduje, gdzie jest filler. Robi to
   „ze słuchu" — z sygnału dźwiękowego — dlatego łapie „yyy", których zwykła
   transkrypcja w ogóle nie zapisuje.
3. **Wykrycie pauz.** Program mierzy poziom głośności i znajduje zbyt długie
   ciche fragmenty. W trybie dokładnym detekcja powtarza się aż do momentu, gdy
   kolejna runda nie znajduje już nic nowego.
4. **Wykrycie muzyki i odgłosów.** Model `MIT/ast-finetuned-audioset` rozpoznaje
   muzykę (te fragmenty są chronione przed jakimkolwiek cięciem) oraz — jeśli
   włączysz — chrząknięcia, oddechy i mlaśnięcia. Odgłosy liczone są na materiale
   już pozbawionym fillerów i pauz, dzięki czemu model „słyszy" prawdziwą mowę
   i nie myli końcówek słów z oddechem. Dodatkowy strażnik mowy (Silero VAD)
   pilnuje, żeby nie ucierpiała żadna głoska.
5. **Ochrona wskazanych głosów.** Jeśli dodałeś wzorce, program liczy „odcisk
   głosu" (model CAMPPlus) w oknach po 2 sekundy, przesuwanych co pół sekundy,
   i porównuje je z wzorcami. Miejsca podobne do któregokolwiek wzorca są
   wyłączane z cięcia. Porównanie odejmuje wcześniej charakterystykę toru
   nagrania, dzięki czemu mierzy sam głos, a nie mikrofon czy pogłos.
6. **Cięcie.** Wycinane fragmenty są usuwane płynnie (z delikatnym
   przenikaniem na łączeniach i marginesem bezpieczeństwa od granic słów), więc
   nie słychać „przeskoków". Cała operacja zachowuje pełną jakość dźwięku.
7. **Wyrównanie głośności** (jeśli włączone) — pomiar EBU R128 i stałe
   wzmocnienie z limiterem szczytów.
8. **Zapis** w wybranym formacie (z okładką i przeliczonymi rozdziałami)
   oraz — opcjonalnie — projekt Reapera.

---

## Wymagania

- Windows 64-bit.
- Wersja GPU: karta NVIDIA ze sterownikiem obsługującym CUDA 12
  (obsługiwane od RTX 20xx po RTX 50xx).
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
„tylko fillery" lub „tylko cisza". Włącz tryb dokładny, jeśli zostaje za dużo.
Zawsze możesz też wyeksportować projekt Reapera (wariant „do przejrzenia")
i zdecydować o każdym cięciu sam.

**Program przyciął muzykę albo wypowiedź z tła.**
Podnieś czułość wykrywania muzyki. Jeśli chodzi o konkretny głos (np. czytnik
ekranu), dodaj jego próbkę do „Chronionych głosów" — wtedy nie zostanie ruszony
w ogóle.

**Po co osobny plik z tym, co wycięte?**
Do kontroli. Odsłuchujesz kilkanaście sekund i od razu wiesz, czy program nie
zabrał czegoś potrzebnego, zamiast porównywać całe nagrania.

---

## Licencja i podziękowania

Kod programu: licencja **MIT** (plik [`LICENSE`](LICENSE)).

Czyściciel korzysta z otwartych komponentów — pełna lista wraz z licencjami jest
w pliku [`TRZECIE_STRONY.txt`](TRZECIE_STRONY.txt). Najważniejsze:

- model fillerów `classla/wav2vecbert2-filledPause` (Apache-2.0),
- model muzyki i odgłosów `MIT/ast-finetuned-audioset` (BSD-3-Clause),
- model głosów CAMPPlus / 3D-Speaker, ONNX (Apache-2.0),
- Silero VAD, wykrywanie mowy (MIT),
- DeepFilterNet, odszumianie (MIT / Apache-2.0),
- PyTorch, Transformers, librosa, soundfile, onnxruntime,
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

Silnik (`worker.py`) działa też samodzielnie z wiersza poleceń — `--help`
pokazuje wszystkie przełączniki odpowiadające opcjom z GUI.
