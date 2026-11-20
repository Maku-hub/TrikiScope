"""Runtime configuration for TrikiScope."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# Nordic UART Service (NUS) UUIDs used by the Triki device.
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # write (phone -> device)
NUS_TX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # notify (device -> phone)
# Vendor characteristic; bit 0 controls the LED (0 = off, 1 = on). Confirmed live.
LED_CHAR_UUID = "6e400004-b5a3-f393-e0a9-e50e24dcca9e"

# The command written to RX to start the IMU stream.  Layout (inferred, matches
# measurements): 20 | accel FS u16 LE (16 g) | gyro FS u16 LE (2000 dps) |
# ODR u16 LE (0x68 = 104 Hz; the Zappka app sends 0x34 = 52 Hz) | 03.
DEFAULT_START_COMMAND = bytes.fromhex("201000D007680003")

# LSM6DSL sensitivities for the full scales the start command selects.
GYRO_LSB_PER_DPS = 1000.0 / 70.0  # +-2000 dps: 70 mdps/LSB -> 14.29 LSB/(deg/s)
ACCEL_LSB_PER_G = 2048.0  # +-16 g: 0.488 mg/LSB


@dataclass(slots=True)
class AppConfig:
    """Tunable options for scanning, decoding and recording."""

    device_name: str = "Triki"
    """Substring matched (case-insensitive) against advertised local names."""

    scan_timeout_seconds: float = 30.0
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

    # Orientation filter defaults (ported from the WPF VisualOrientationMapper).
    orientation_mode: str = "madgwick"  # or "complementary"
    madgwick_beta: float = 1.5
    gyro_gain: float = 1.0
    smoothing_factor: float = 0.35
    visual_deadband_degrees: float = 8.0

    # Recording.
    record: bool = False
    csv_path: str = "triki_data.csv"
    log_path: str = "triki_events.log"

    history_seconds: float = 8.0
    """How much IMU history to keep for sparkline charts."""
