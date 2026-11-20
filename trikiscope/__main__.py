"""Entry point: ``python -m trikiscope``."""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import gatt_names as g
from .config import (
    ACCEL_LSB_PER_G,
    ACCEL_RANGES_G,
    DEFAULT_ACCEL_G,
    DEFAULT_GYRO_DPS,
    DEFAULT_RATE_HZ,
    GYRO_LSB_PER_DPS,
    GYRO_MDPS_PER_LSB,
    SAMPLE_RATES_HZ,
    AppConfig,
    accel_lsb_per_g,
    build_start_command,
    gyro_lsb_per_dps,
    is_verified,
    parse_start_command,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trikiscope",
        description="Terminalowy inspektor BLE oraz podgląd IMU/orientacji dla urządzenia Żabka Triki.",
    )
    conn = parser.add_argument_group("połączenie")
    conn.add_argument("--name", default="Triki", help="Fragment reklamowanej nazwy urządzenia (domyślnie: Triki).")
    conn.add_argument(
        "--address",
        help="Łącz tylko z urządzeniem o tym adresie BLE (np. EC:07:A9:B8:C1:B7); pomija wybór urządzenia.",
    )
    conn.add_argument("--scan-timeout", type=float, default=30.0, help="Czas skanowania w sekundach.")
    conn.add_argument(
        "--no-reconnect",
        action="store_true",
        help="Nie łącz ponownie automatycznie po zerwaniu połączenia.",
    )
    conn.add_argument("--auto-connect", action="store_true", help="Rozpocznij skanowanie/łączenie zaraz po starcie.")
    conn.add_argument(
        "--scan",
        action="store_true",
        help="Wypisz wszystkie reklamujące się urządzenia BLE i zakończ (bez TUI).",
    )

    stream = parser.add_argument_group("strumień IMU")
    stream.add_argument(
        "--odr",
        type=int,
        choices=SAMPLE_RATES_HZ,
        help=f"Częstotliwość próbkowania w Hz (domyślnie {DEFAULT_RATE_HZ}; zweryfikowane: 52, 104).",
    )
    stream.add_argument(
        "--accel-range",
        type=int,
        choices=ACCEL_RANGES_G,
        help=f"Zakres akcelerometru ±g (domyślnie {DEFAULT_ACCEL_G}; zweryfikowany: 16).",
    )
    stream.add_argument(
        "--gyro-range",
        type=int,
        choices=sorted(GYRO_MDPS_PER_LSB),
        help=f"Zakres żyroskopu ±deg/s (domyślnie {DEFAULT_GYRO_DPS}; zweryfikowany: 2000).",
    )
    stream.add_argument(
        "--start-command",
        help="Surowa komenda startowa (hex) zamiast --odr/--accel-range/--gyro-range.",
    )
    stream.add_argument(
        "--gyro-scale",
        type=float,
        help=f"Wymuś przelicznik żyroskopu, LSB na deg/s (domyślnie z zakresu: {GYRO_LSB_PER_DPS:.2f}).",
    )
    stream.add_argument(
        "--accel-scale",
        type=float,
        help=f"Wymuś przelicznik akcelerometru, LSB na g (domyślnie z zakresu: {ACCEL_LSB_PER_G:.0f}).",
    )
    stream.add_argument("--settle-delay", type=float, default=0.0, help="Ile sekund odczekać przed wysłaniem komendy startowej.")
    stream.add_argument("--discard", type=int, default=20, help="Liczba początkowych próbek do odrzucenia.")
    stream.add_argument("--no-start", action="store_true", help="Nie wysyłaj komendy startowej automatycznie.")
    stream.add_argument(
        "--mode",
        choices=["madgwick", "complementary"],
        default="madgwick",
        help="Filtr orientacji do użycia.",
    )

    rec = parser.add_argument_group("nagrywanie i odtwarzanie")
    rec.add_argument("--record", action="store_true", help="Zacznij nagrywać od razu.")
    rec.add_argument("--csv", default="triki_data.csv", help="Ścieżka pliku CSV z próbkami.")
    rec.add_argument("--raw", default="triki_raw.jsonl", help="Ścieżka pliku z surowymi powiadomieniami BLE.")
    rec.add_argument("--log", default="triki_events.log", help="Ścieżka pliku logu zdarzeń.")
    rec.add_argument(
        "--replay",
        metavar="PLIK",
        help="Odtwórz nagranie (surowe .jsonl albo próbki .csv) zamiast łączyć się z kapslem.",
    )
    rec.add_argument("--loop", action="store_true", help="Odtwarzaj nagranie w pętli.")
    return parser


def _start_command_from_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> bytes:
    stream_flags = (args.odr, args.accel_range, args.gyro_range)
    if args.start_command:
        if any(v is not None for v in stream_flags):
            parser.error("--start-command wyklucza --odr / --accel-range / --gyro-range")
        try:
            return bytes.fromhex(args.start_command)
        except ValueError:
            parser.error(f"nieprawidłowy hex w --start-command: {args.start_command!r}")
    return build_start_command(
        args.accel_range or DEFAULT_ACCEL_G,
        args.gyro_range or DEFAULT_GYRO_DPS,
        args.odr or DEFAULT_RATE_HZ,
    )


def _config_from_args(args: argparse.Namespace, start_command: bytes) -> AppConfig:
    # Scales follow the ranges the start command selects, unless forced.
    settings = parse_start_command(start_command)
    gyro_scale = args.gyro_scale
    accel_scale = args.accel_scale
    if settings is not None:
        gyro_scale = gyro_scale or gyro_lsb_per_dps(settings.gyro_dps)
        accel_scale = accel_scale or accel_lsb_per_g(settings.accel_g)
        if not is_verified(settings) and not args.replay:
            print(
                f"Uwaga: ustawienia ±{settings.accel_g} g / ±{settings.gyro_dps} deg/s / {settings.rate_hz} Hz "
                "nie były sprawdzone na kapslu. Skalę żyroskopu zweryfikujesz przez "
                "tools/probe_gyro_scale.py z tymi samymi opcjami.",
                file=sys.stderr,
            )
    return AppConfig(
        device_name=args.name,
        device_address=args.address,
        scan_timeout_seconds=args.scan_timeout,
        auto_reconnect=not args.no_reconnect,
        gyro_scale=gyro_scale or GYRO_LSB_PER_DPS,
        accel_scale=accel_scale or ACCEL_LSB_PER_G,
        start_command=start_command,
        settle_delay_seconds=args.settle_delay,
        startup_discard_samples=args.discard,
        auto_start_stream=not args.no_start,
        auto_connect=args.auto_connect or bool(args.replay),
        replay_path=args.replay,
        replay_loop=args.loop,
        orientation_mode=args.mode,
        record=args.record,
        csv_path=args.csv,
        raw_path=args.raw,
        log_path=args.log,
    )


async def _run_scan(timeout: float) -> None:
    from .ble import quick_scan

    print(f"Skanowanie przez {timeout:.0f}s...\n")
    devices = await quick_scan(timeout)
    if not devices:
        print("Nie znaleziono reklamujących się urządzeń.")
        return
    devices.sort(key=lambda d: (d.rssi if d.rssi is not None else -999), reverse=True)
    for d in devices:
        rssi = f"{d.rssi} dBm" if d.rssi is not None else "?"
        print(f"{d.address}  {rssi:>9}  {d.name or '(no name)'}")
        for cid, data in d.manufacturer_data.items():
            print(f"    mfr 0x{cid:04X} {g.company_name(cid)}: {g.hexdump(data)}")
        for uuid in d.service_uuids:
            print(f"    svc {g.describe_service(uuid)}")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.scan:
        asyncio.run(_run_scan(args.scan_timeout))
        return 0

    recording = None
    if args.replay:
        from .replay import RecordingError, load_recording, recording_start_command

        try:
            recording = load_recording(args.replay)
        except (OSError, RecordingError) as exc:
            print(f"Nie można odtworzyć nagrania: {exc}", file=sys.stderr)
            return 2
        stored = recording_start_command(recording)
        explicit = args.start_command or any(v is not None for v in (args.odr, args.accel_range, args.gyro_range))
        if stored and not explicit:
            # Decode with the settings the recording was made with.
            args.start_command = stored.hex()

    start_command = _start_command_from_args(parser, args)
    config = _config_from_args(args, start_command)
    from .app import TrikiApp

    app = TrikiApp(config, recording=recording)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
