"""Runtime configuration for TrikiScope."""

from __future__ import annotations

from dataclasses import dataclass, field


# Nordic UART Service (NUS) UUIDs used by the Triki device.
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # write (phone -> device)
NUS_TX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # notify (device -> phone)
# Vendor characteristic; bit 0 controls the LED (0 = off, 1 = on). Confirmed live.
LED_CHAR_UUID = "6e400004-b5a3-f393-e0a9-e50e24dcca9e"

# The command the Zappka app writes to RX to start the IMU stream.
DEFAULT_START_COMMAND = bytes.fromhex("201000D007680003")


@dataclass(slots=True)
class AppConfig:
    """Tunable options for scanning, decoding and recording."""

    device_name: str = "Triki"
    """Substring matched (case-insensitive) against advertised local names."""

    scan_timeout_seconds: float = 30.0
    gyro_scale: float = 131.0
    """LSB per deg/s (LSM6DSL @ +-250 dps)."""
    accel_scale: float = 2048.0
    """LSB per g (LSM6DSL @ +-16 g)."""

    start_command: bytes = DEFAULT_START_COMMAND
    settle_delay_seconds: float = 0.0
    startup_discard_samples: int = 20
    """Frames dropped at the start of a stream (device emits noise on wake)."""

    auto_start_stream: bool = True
    auto_connect: bool = False

    # Orientation filter defaults (ported from the WPF VisualOrientationMapper).
    orientation_mode: str = "madgwick"  # or "complementary"
    madgwick_beta: float = 1.5
    gyro_gain: float = 2.5
    smoothing_factor: float = 0.35
    visual_deadband_degrees: float = 8.0

    # Recording.
    record: bool = False
    csv_path: str = "triki_data.csv"
    log_path: str = "triki_events.log"

    history_seconds: float = 8.0
    """How much IMU history to keep for sparkline charts."""
