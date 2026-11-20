"""Recording: decoded samples (CSV), raw BLE notifications (JSONL) and an event log.

The raw file keeps every NUS TX notification exactly as received, so a session
can be replayed through the full pipeline later (``--replay``)::

    {"format": "trikiscope-raw", "version": 1, "created": "...", "start_command": "2010..."}
    {"t": 1763631234.123456, "data": "2200fbff0300..."}
    ...

``t`` is the arrival time (Unix seconds), ``data`` the notification payload in hex.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, TextIO

RAW_FORMAT = "trikiscope-raw"
RAW_VERSION = 1

from .protocol import ImuSample

CSV_HEADER = [
    "frame_index",
    "timestamp_utc",
    "gyro_x_dps",
    "gyro_y_dps",
    "gyro_z_dps",
    "accel_x_g",
    "accel_y_g",
    "accel_z_g",
    "raw_gyro_x",
    "raw_gyro_y",
    "raw_gyro_z",
    "raw_accel_x",
    "raw_accel_y",
    "raw_accel_z",
    "pitch",
    "roll",
    "yaw",
    "button",
]


class Recorder:
    """Writes IMU samples to CSV, raw notifications to JSONL and events to a log.

    Files are opened only when recording starts; CSV and raw files never
    overwrite an earlier recording (a timestamp suffix is added instead).
    """

    def __init__(self, csv_path: str, log_path: str, raw_path: Optional[str] = None) -> None:
        self._base_csv_path = Path(csv_path)
        self._csv_path = self._base_csv_path
        self._base_raw_path = Path(raw_path) if raw_path else None
        self._raw_path: Optional[Path] = None
        self._log_path = Path(log_path)
        self._csv_file: Optional[TextIO] = None
        self._csv_writer = None
        self._raw_file: Optional[TextIO] = None
        self._log_file: Optional[TextIO] = None
        self.sample_count = 0
        self.notification_count = 0
        self.is_recording = False

    def start(self, meta: Optional[dict[str, Any]] = None) -> None:
        """Open the output files; ``meta`` goes into the raw file's header line."""
        if self.is_recording:
            return
        opened: list[TextIO] = []
        try:
            self._csv_path = _next_free_path(self._base_csv_path)
            # "x" = exclusive create: never truncate an earlier recording.
            self._csv_file = self._csv_path.open("x", newline="", encoding="utf-8")
            opened.append(self._csv_file)
            if self._base_raw_path is not None:
                self._raw_path = _next_free_path(self._base_raw_path)
                self._raw_file = self._raw_path.open("x", encoding="utf-8")
                opened.append(self._raw_file)
            self._log_file = self._log_path.open("a", encoding="utf-8")
        except OSError:
            for fh in opened:
                fh.close()
            self._csv_file = self._raw_file = None
            raise
        self._csv_writer = csv.writer(self._csv_file)
        self._csv_writer.writerow(CSV_HEADER)
        if self._raw_file is not None:
            header = {
                "format": RAW_FORMAT,
                "version": RAW_VERSION,
                "created": datetime.now(timezone.utc).isoformat(),
                **(meta or {}),
            }
            self._raw_file.write(json.dumps(header) + "\n")
        self.sample_count = 0
        self.notification_count = 0
        self.is_recording = True
        target = self._csv_path if self._raw_path is None else f"{self._csv_path} + {self._raw_path}"
        self.log_event(f"Recording started -> {target}")

    def write_raw(self, data: bytes, timestamp_utc: datetime) -> None:
        """Store one BLE notification exactly as received."""
        if not self.is_recording or self._raw_file is None:
            return
        record = {"t": round(timestamp_utc.timestamp(), 6), "data": data.hex()}
        self._raw_file.write(json.dumps(record) + "\n")
        self.notification_count += 1

    def write_sample(
        self,
        sample: ImuSample,
        pitch: float = 0.0,
        roll: float = 0.0,
        yaw: float = 0.0,
    ) -> None:
        if not self.is_recording or self._csv_writer is None:
            return
        self._csv_writer.writerow(
            [
                sample.frame_index,
                sample.timestamp_utc.isoformat(),
                f"{sample.gyro_x:.6f}",
                f"{sample.gyro_y:.6f}",
                f"{sample.gyro_z:.6f}",
                f"{sample.accel_x:.6f}",
                f"{sample.accel_y:.6f}",
                f"{sample.accel_z:.6f}",
                sample.raw_gyro_x,
                sample.raw_gyro_y,
                sample.raw_gyro_z,
                sample.raw_accel_x,
                sample.raw_accel_y,
                sample.raw_accel_z,
                f"{pitch:.3f}",
                f"{roll:.3f}",
                f"{yaw:.3f}",
                int(sample.button_pressed),
            ]
        )
        self.sample_count += 1

    def log_event(self, message: str) -> None:
        if self._log_file is None:
            return
        timestamp = datetime.now(timezone.utc).isoformat()
        self._log_file.write(f"{timestamp} {message}\n")
        self._log_file.flush()

    def stop(self) -> None:
        if not self.is_recording:
            return
        self.log_event(
            f"Recording stopped ({self.sample_count} samples, {self.notification_count} notifications)"
        )
        self.is_recording = False
        if self._csv_file is not None:
            self._csv_file.flush()
            self._csv_file.close()
            self._csv_file = None
            self._csv_writer = None
        if self._raw_file is not None:
            self._raw_file.flush()
            self._raw_file.close()
            self._raw_file = None
        if self._log_file is not None:
            self._log_file.flush()
            self._log_file.close()
            self._log_file = None

    @property
    def csv_path(self) -> str:
        return str(self._csv_path)

    @property
    def raw_path(self) -> Optional[str]:
        return str(self._raw_path) if self._raw_path else None


def _next_free_path(base: Path) -> Path:
    """``base`` if unused, else ``<stem>_<YYYYmmdd-HHMMSS>[_N]<suffix>``."""
    if not base.exists():
        return base
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = base.with_name(f"{base.stem}_{stamp}{base.suffix}")
    n = 2
    while candidate.exists():
        candidate = base.with_name(f"{base.stem}_{stamp}_{n}{base.suffix}")
        n += 1
    return candidate
