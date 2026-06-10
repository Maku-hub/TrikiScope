"""Entry point: ``python -m trikiscope``."""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import gatt_names as g
from .config import DEFAULT_START_COMMAND, AppConfig


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trikiscope",
        description="Terminalowy inspektor BLE oraz podgląd IMU/orientacji dla urządzenia Żabka Triki.",
    )
    parser.add_argument("--name", default="Triki", help="Fragment reklamowanej nazwy urządzenia (domyślnie: Triki).")
    parser.add_argument("--scan-timeout", type=float, default=30.0, help="Czas skanowania w sekundach.")
    parser.add_argument("--gyro-scale", type=float, default=131.0, help="Przelicznik żyroskopu (LSB na deg/s).")
    parser.add_argument("--accel-scale", type=float, default=2048.0, help="Przelicznik akcelerometru (LSB na g).")
    parser.add_argument("--settle-delay", type=float, default=0.0, help="Ile sekund odczekać przed wysłaniem komendy startowej.")
    parser.add_argument("--discard", type=int, default=20, help="Liczba początkowych próbek do odrzucenia.")
    parser.add_argument(
        "--start-command",
        default=DEFAULT_START_COMMAND.hex(),
        help="Bajty (hex) zapisywane do NUS RX, by uruchomić strumień.",
    )
    parser.add_argument("--no-start", action="store_true", help="Nie wysyłaj komendy startowej automatycznie.")
    parser.add_argument("--auto-connect", action="store_true", help="Rozpocznij skanowanie/łączenie zaraz po starcie.")
    parser.add_argument(
        "--mode",
        choices=["madgwick", "complementary"],
        default="madgwick",
        help="Filtr orientacji do użycia.",
    )
    parser.add_argument("--record", action="store_true", help="Zacznij nagrywać od razu.")
    parser.add_argument("--csv", default="triki_data.csv", help="Ścieżka pliku CSV.")
    parser.add_argument("--log", default="triki_events.log", help="Ścieżka pliku logu zdarzeń.")
    parser.add_argument(
        "--scan",
        action="store_true",
        help="Wypisz wszystkie reklamujące się urządzenia BLE i zakończ (bez TUI).",
    )
    return parser


def _config_from_args(args: argparse.Namespace) -> AppConfig:
    try:
        start_command = bytes.fromhex(args.start_command)
    except ValueError:
        print(f"Nieprawidłowy hex w --start-command: {args.start_command!r}", file=sys.stderr)
        raise SystemExit(2)
    return AppConfig(
        device_name=args.name,
        scan_timeout_seconds=args.scan_timeout,
        gyro_scale=args.gyro_scale,
        accel_scale=args.accel_scale,
        start_command=start_command,
        settle_delay_seconds=args.settle_delay,
        startup_discard_samples=args.discard,
        auto_start_stream=not args.no_start,
        auto_connect=args.auto_connect,
        orientation_mode=args.mode,
        record=args.record,
        csv_path=args.csv,
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
    args = _build_parser().parse_args(argv)

    if args.scan:
        asyncio.run(_run_scan(args.scan_timeout))
        return 0

    config = _config_from_args(args)
    from .app import TrikiApp

    app = TrikiApp(config)
    if config.record:
        app.recorder.start()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
