# Czyściciel

Aplikacja dla Windows do czyszczenia nagrań mowy z wtrąceń „yyy / eee / mmm"
(fillerów) oraz nadmiernie długich pauz — tak, by brzmiało naturalnie.
W pełni obsługiwana z klawiatury i czytnika ekranu (NVDA/JAWS).

Używa modelu AI (`classla/wav2vecbert2-filledPause`), który rozpoznaje fillery
z samego sygnału dźwiękowego (nie z transkrypcji), działa po polsku i jest
niezależny od mówcy.

## Najważniejsze cechy

- Wykrywanie i wycinanie fillerów modelem AI.
- Skracanie zbyt długich pauz z zachowaniem naturalnego oddechu (3 presety).
- Tryb wsadowy — wiele plików naraz.
- Eksport projektu **Reapera (.RPP)** do dalszej, niedestrukcyjnej edycji.
- Automatyczny wybór **GPU (NVIDIA) lub procesora** — bez konfiguracji.
- Dostępny interfejs (etykiety po polsku, pełna obsługa czytnika ekranu).

## Instalacja i pierwsze uruchomienie

Nie ma osobnego instalatora. Pobierz folder `Czysciciel` (z sekcji Releases)
i uruchom `Czysciciel.exe`.

**Pierwsze uruchomienie** dociąga jednorazowo środowisko uruchomieniowe
(silnik AI + model + ffmpeg) do:

    %LOCALAPPDATA%\Czysciciel

- Na komputerze z kartą **NVIDIA** pobierze wersję GPU (szybsze przetwarzanie).
- Bez karty NVIDIA pobierze wersję na **procesor** (wolniej, ale działa wszędzie).
- Rozmiar pobrania: ok. 5 GB (GPU) lub ok. 2,5 GB (CPU). Wymaga internetu
  tylko przy pierwszym uruchomieniu; potem działa offline.
- Postęp pokazywany jest na pasku i w dzienniku (czytane przez czytnik ekranu).

Kolejne uruchomienia są natychmiastowe.

## Jak używać

1. Dodaj pliki (przycisk „Dodaj pliki..." lub „Dodaj folder...").
2. Zaznacz na liście te, które chcesz wyczyścić (spacja zaznacza/odznacza).
3. Wybierz opcje: preset skracania pauz, minimalną długość fillera,
   ewentualnie „tylko fillery" lub eksport projektu Reapera.
4. (Opcjonalnie) wskaż folder wyjściowy — domyślnie wynik ląduje obok źródła.
5. Naciśnij „Wyczyść" (lub F5).

Wynik: `nazwa_czysty.mp3` (192 kbps, 44,1 kHz stereo) oraz `ciecia_nazwa.json`
z listą wszystkich cięć. Przy włączonej opcji — także `nazwa.RPP`.

### Presety skracania pauz

| preset       | nie rusza pauz do | dłuższe skraca do | efekt            |
|--------------|-------------------|-------------------|------------------|
| zachowawczy  | 0,70 s            | 0,60 s            | ledwo zauważalne |
| umiarkowany  | 0,50 s            | 0,45 s            | dobry kompromis  |
| zwarty       | 0,35 s            | 0,30 s            | radiowe tempo    |

Fillery krótsze niż ustawiony próg (domyślnie 0,30 s) są ignorowane — chroni to
przed wycięciem lekko przeciągniętego „y" w środku słowa.

## Wymagania

- Windows 64-bit.
- Do wersji GPU: karta NVIDIA ze sterownikiem obsługującym CUDA 12.
- Połączenie z internetem przy pierwszym uruchomieniu.

## Licencja

Kod: MIT (plik `LICENSE`). Komponenty firm trzecich i ich licencje: plik
`TRZECIE_STRONY.txt`. Model na licencji Apache-2.0, ffmpeg w wariancie LGPL.

## Budowanie ze źródeł

Wymaga Windows z Pythonem 3.12, wxPython i PyInstaller:

    build.bat

Wynik: `dist\Czysciciel\Czysciciel.exe` (lekki launcher; ciężkie komponenty
dociągane przy pierwszym uruchomieniu).
