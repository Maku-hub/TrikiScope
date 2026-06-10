"""CSV + event-log recording."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TextIO

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
    """Writes IMU samples to CSV and free-text events to a log file.

    Both files are opened lazily on first write so nothing is created unless
    recording is actually started.
    """

    def __init__(self, csv_path: str, log_path: str) -> None:
        self._csv_path = Path(csv_path)
        self._log_path = Path(log_path)
        self._csv_file: Optional[TextIO] = None
        self._csv_writer = None
        self._log_file: Optional[TextIO] = None
        self.sample_count = 0
        self.is_recording = False

    def start(self) -> None:
        if self.is_recording:
            return
        self._csv_file = self._csv_path.open("w", newline="", encoding="utf-8")
        self._csv_writer = csv.writer(self._csv_file)
        self._csv_writer.writerow(CSV_HEADER)
        self._log_file = self._log_path.open("a", encoding="utf-8")
        self.sample_count = 0
        self.is_recording = True
        self.log_event(f"Recording started -> {self._csv_path}")

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
        self.log_event(f"Recording stopped ({self.sample_count} samples)")
        self.is_recording = False
        if self._csv_file is not None:
            self._csv_file.flush()
            self._csv_file.close()
            self._csv_file = None
            self._csv_writer = None
        if self._log_file is not None:
            self._log_file.flush()
            self._log_file.close()
            self._log_file = None

    @property
    def csv_path(self) -> str:
        return str(self._csv_path)
