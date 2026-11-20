# Changelog

Wszystkie istotne zmiany w projekcie są dokumentowane w tym pliku.

Format oparty na [Keep a Changelog](https://keepachangelog.com/pl/1.1.0/),
a projekt stosuje [wersjonowanie semantyczne](https://semver.org/lang/pl/).

## [Unreleased]

### Naprawione
- **Czas próbek** — próbki z jednej notyfikacji BLE dostawały ten sam znacznik czasu,
  przez co filtry orientacji i fizyka gier całkowały 1,6–2,6× za dużo czasu (zależnie
  od interwału połączenia). Nowy `SampleClock` rozkłada próbki równomiernie wg
  częstotliwości IMU (nominalnej z komendy startowej, potem mierzonej), a mappery
  orientacji ignorują kroki `dt` > 100 ms.
- **Resynchronizacja ramek** — po utracie synchronizacji parser mógł zaczepić się
  o fałszywy nagłówek `22 00` w danych (wartość surowa 34) i emitować przesunięte
  ramki. Granica ramki jest teraz przyjmowana dopiero po potwierdzeniu kolejnymi
  nagłówkami; licznik resynchronizacji widoczny w zakładce IMU.
- **Nagrywanie** — ponowne rozpoczęcie nagrywania nie nadpisuje już poprzedniego
  pliku CSV (kolejne pliki dostają znacznik czasu w nazwie); błąd otwarcia pliku
  nie wywraca aplikacji.
- **Rozłączanie** — klawisz `d` przerywa trwające skanowanie, łączenie i odczyt GATT
  zamiast czekać na ich zakończenie.
- **Dioda LED** — stan odczytywany z urządzenia po połączeniu i zmieniany w UI dopiero
  po udanym zapisie.

## [1.0.0] - 2026-06-18

Pierwsze stabilne wydanie. TrikiScope jest funkcjonalnie kompletny: łączy się z
kapslem Żabka Triki przez BLE, czyta z niego maksimum informacji, dekoduje strumień
IMU i wizualizuje orientację 3D w terminalu.

### Dodane
- **Overview** — stan połączenia, dane reklamowe BLE (RSSI, moc nadawania,
  manufacturer data z dekodowaniem), pełne informacje o urządzeniu, bateria, LED.
- **GATT** — pełna inwentaryzacja bazy GATT z odczytanymi wartościami i dekodowaniem
  „po ludzku".
- **IMU** — strumień na żywo (surowe int16 + przeliczone gyro/accel), statystyki
  strumienia, sparkline'y, panel Motion (przycisk, gesty, wartości szczytowe).
- **Orientation** — orientacja 3D filtrem Madgwick AHRS lub komplementarnym,
  wizualizacja obracającym się szkieletem sześcianu ASCII, auto-kalibracja.
- **Games** — mini-gry sterowane kapslem na żywo: Tilt Maze, Spin Meter, Reflex Catch
  (przełączanie `[`/`]`, restart `g`).
- **Log** — przewijalny dziennik zdarzeń.
- Nagrywanie do CSV (strumień + kąty + stan przycisku) i logu zdarzeń.
- Sterowanie diodą LED kapsla (klawisz `l`) przez charakterystykę vendor `6e400004`.
- Tryb skanowania bez TUI (`--scan`) oraz launchery `run.ps1` / `run.bat`.
- Narzędzia diagnostyczne w `tools/` użyte przy reverse-engineeringu urządzenia.

### Reverse-engineering urządzenia
- Protokół Nordic UART Service, komenda startowa `20 10 00 D0 07 68 00 03`.
- Format ramki IMU (14 B, nagłówek `22 00`, 6× int16 LE), skale gyro 131 / accel 2048.
- Odkrycie flagi przycisku w drugim bajcie nagłówka (`22 01`).
- Odkrycie sterowania diodą LED bitem 0 charakterystyki `6e400004`.

[1.0.0]: https://github.com/Maku-hub/TrikiScope/releases/tag/v1.0.0
