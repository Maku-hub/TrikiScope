"""Runtime configuration for TrikiScope."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple, Optional


# Nordic UART Service (NUS) UUIDs used by the Triki device.
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # write (phone -> device)
NUS_TX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # notify (device -> phone)
# Vendor characteristic; bit 0 controls the LED (0 = off, 1 = on). Confirmed live.
LED_CHAR_UUID = "6e400004-b5a3-f393-e0a9-e50e24dcca9e"

# The command written to RX to start the IMU stream configures the LSM6DSL:
#   20 | accel full scale g (u16 LE) | gyro full scale dps (u16 LE) | ODR Hz (u16 LE) | 03
START_COMMAND_OPCODE = 0x20
START_COMMAND_FLAGS = 0x03
# What the Zappka app writes to RX to stop the stream (seen in its BLE traffic).
STOP_COMMAND = bytes.fromhex("20000000000000")

# LSM6DSL settings.  Gyro sensitivity per full scale (mdps/LSB, datasheet); accel
# counts are full-scale-relative (32768 LSB = full scale).
GYRO_MDPS_PER_LSB = {125: 4.375, 250: 8.75, 500: 17.5, 1000: 35.0, 2000: 70.0}
ACCEL_RANGES_G = (2, 4, 8, 16)
SAMPLE_RATES_HZ = (26, 52, 104, 208)
# Combinations confirmed on a real cap (2026-11): gyro scale measured with
# tools/probe_gyro_scale.py, rates seen in TrikiScope (104 Hz) and Zappka (52 Hz) traffic.
VERIFIED_ACCEL_G = (16,)
VERIFIED_GYRO_DPS = (2000,)
VERIFIED_RATES_HZ = (52, 104)

DEFAULT_ACCEL_G = 16
DEFAULT_GYRO_DPS = 2000
DEFAULT_RATE_HZ = 104


class StreamSettings(NamedTuple):
    accel_g: int
    gyro_dps: int
    rate_hz: int


def build_start_command(
    accel_g: int = DEFAULT_ACCEL_G, gyro_dps: int = DEFAULT_GYRO_DPS, rate_hz: int = DEFAULT_RATE_HZ
) -> bytes:
    return (
        bytes([START_COMMAND_OPCODE])
        + accel_g.to_bytes(2, "little")
        + gyro_dps.to_bytes(2, "little")
        + rate_hz.to_bytes(2, "little")
        + bytes([START_COMMAND_FLAGS])
    )


def parse_start_command(command: bytes) -> Optional[StreamSettings]:
    """Settings encoded in a start command, or None if it doesn't look like one."""
    if len(command) != 8 or command[0] != START_COMMAND_OPCODE:
        return None
    return StreamSettings(
        int.from_bytes(command[1:3], "little"),
        int.from_bytes(command[3:5], "little"),
        int.from_bytes(command[5:7], "little"),
    )


def gyro_lsb_per_dps(gyro_dps: int) -> Optional[float]:
    mdps = GYRO_MDPS_PER_LSB.get(gyro_dps)
    return 1000.0 / mdps if mdps else None


def accel_lsb_per_g(accel_g: int) -> Optional[float]:
    return 32768.0 / accel_g if accel_g in ACCEL_RANGES_G else None


def is_verified(settings: StreamSettings) -> bool:
    return (
        settings.accel_g in VERIFIED_ACCEL_G
        and settings.gyro_dps in VERIFIED_GYRO_DPS
        and settings.rate_hz in VERIFIED_RATES_HZ
    )


DEFAULT_START_COMMAND = build_start_command()  # 20 10 00 D0 07 68 00 03
GYRO_LSB_PER_DPS = gyro_lsb_per_dps(DEFAULT_GYRO_DPS)  # 14.29 LSB/(deg/s)
ACCEL_LSB_PER_G = accel_lsb_per_g(DEFAULT_ACCEL_G)  # 2048 LSB/g


@dataclass(slots=True)
class AppConfig:
    """Tunable options for scanning, decoding and recording."""

    device_name: str = "Triki"
    """Substring matched (case-insensitive) against advertised local names."""
    device_address: Optional[str] = None
    """Connect only to this BLE address (skips the name match and the device picker)."""

    scan_timeout_seconds: float = 30.0
    pick_window_seconds: float = 1.5
    """After the first match keep scanning this long to spot other Triki devices."""
    auto_reconnect: bool = True
    """Reconnect (with back-off) after an established connection drops."""

    gyro_scale: float = GYRO_LSB_PER_DPS
    """LSB per deg/s (LSM6DSL @ +-2000 dps; confirmed by tools/probe_gyro_scale.py)."""
    accel_scale: float = ACCEL_LSB_PER_G
    """LSB per g (LSM6DSL @ +-16 g)."""

    start_command: bytes = DEFAULT_START_COMMAND
    settle_delay_seconds: float = 0.0
    startup_discard_samples: int = 20
    """Frames dropped at the start of a stream (device emits noise on wake)."""

    sample_rate_hz: Optional[float] = None
    """Nominal IMU output rate; None = taken from the start command (fallback 104 Hz)."""

    auto_start_stream: bool = True
    auto_connect: bool = False

    replay_path: Optional[str] = None
    """Play back a recording (raw .jsonl or samples .csv) instead of using BLE."""
    replay_loop: bool = False

    # Orientation filter defaults (ported from the WPF VisualOrientationMapper).
    orientation_mode: str = "madgwick"  # or "complementary"
    madgwick_beta: float = 1.5
    gyro_gain: float = 1.0
    smoothing_factor: float = 0.35
    visual_deadband_degrees: float = 8.0

    # Recording.
    record: bool = False
    csv_path: str = "triki_data.csv"
    raw_path: str = "triki_raw.jsonl"
    log_path: str = "triki_events.log"

    history_seconds: float = 8.0
    """How much IMU history to keep for sparkline charts."""
