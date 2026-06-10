"""Triki IMU frame decoding.

The device streams 14-byte frames over the Nordic UART Service TX characteristic::

    22 00 | gyroX | gyroY | gyroZ | accelX | accelY | accelZ

Each axis is a signed 16-bit little-endian integer.  ``FrameParser`` re-synchronises
on the ``22 00`` header so it can recover from partial / merged BLE notifications.

This is a faithful Python port of the C# ``FrameParser`` / ``ImuSampleProcessor`` /
``ImuStats`` types from the original WPF TrikiReader.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator, List, Optional

FRAME_LENGTH = 14
FRAME_HEADER_BYTE0 = 0x22
# The second header byte is a status flag: 0x00 = button released, 0x01 = pressed.
# (Discovered live: pressing the button flips 22 00 -> 22 01 with the same payload.)
FRAME_STATUS_BYTES = frozenset({0x00, 0x01})
FRAME_HEADER = bytes([FRAME_HEADER_BYTE0, 0x00])  # kept for the released-state frame


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class ImuSample:
    """A single decoded IMU reading (raw counts + scaled physical units)."""

    frame_index: int
    timestamp_utc: datetime
    gyro_x: float
    gyro_y: float
    gyro_z: float
    accel_x: float
    accel_y: float
    accel_z: float
    raw_gyro_x: int
    raw_gyro_y: int
    raw_gyro_z: int
    raw_accel_x: int
    raw_accel_y: int
    raw_accel_z: int
    status: int = 0
    """Second header byte; bit 0 is the button state (1 = pressed)."""

    @property
    def button_pressed(self) -> bool:
        return bool(self.status & 0x01)

    @staticmethod
    def from_frame(
        frame: bytes,
        frame_index: int,
        gyro_scale: float,
        accel_scale: float,
        timestamp_utc: Optional[datetime] = None,
    ) -> "ImuSample":
        if len(frame) < FRAME_LENGTH:
            raise ValueError(f"frame too short: {len(frame)} < {FRAME_LENGTH}")
        if timestamp_utc is None:
            timestamp_utc = _utcnow()

        status = frame[1]
        # Six signed little-endian int16s starting at offset 2 (after the header).
        gx, gy, gz, ax, ay, az = struct.unpack_from("<6h", frame, 2)
        return ImuSample(
            frame_index=frame_index,
            timestamp_utc=timestamp_utc,
            gyro_x=gx / gyro_scale,
            gyro_y=gy / gyro_scale,
            gyro_z=gz / gyro_scale,
            accel_x=ax / accel_scale,
            accel_y=ay / accel_scale,
            accel_z=az / accel_scale,
            raw_gyro_x=gx,
            raw_gyro_y=gy,
            raw_gyro_z=gz,
            raw_accel_x=ax,
            raw_accel_y=ay,
            raw_accel_z=az,
            status=status,
        )

    @property
    def gyro_magnitude(self) -> float:
        return (self.gyro_x ** 2 + self.gyro_y ** 2 + self.gyro_z ** 2) ** 0.5

    @property
    def accel_magnitude(self) -> float:
        return (self.accel_x ** 2 + self.accel_y ** 2 + self.accel_z ** 2) ** 0.5


class FrameParser:
    """Re-assembles 14-byte frames from a stream of BLE notification chunks."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.dropped_byte_count = 0

    def push(self, data: bytes) -> Iterator[bytes]:
        """Feed raw bytes; yield each complete frame discovered."""
        self._buffer.extend(data)

        while True:
            header_index = self._find_header()
            if header_index < 0:
                # No header in the buffer.  Drop everything except a trailing 0x22
                # that might be the first byte of a header split across notifications.
                if self._buffer:
                    keep_trailing = self._buffer[-1] == 0x22
                    drop_count = len(self._buffer) - 1 if keep_trailing else len(self._buffer)
                    self.dropped_byte_count += drop_count
                    if keep_trailing:
                        trailing = self._buffer[-1]
                        self._buffer.clear()
                        self._buffer.append(trailing)
                    else:
                        self._buffer.clear()
                return

            if header_index > 0:
                self.dropped_byte_count += header_index
                del self._buffer[:header_index]

            if len(self._buffer) < FRAME_LENGTH:
                return

            frame = bytes(self._buffer[:FRAME_LENGTH])
            del self._buffer[:FRAME_LENGTH]
            yield frame

    def _find_header(self) -> int:
        # A frame starts with 0x22 followed by a status byte (0x00 released,
        # 0x01 pressed). Matching both keeps button frames in sync instead of
        # dropping them as garbage.
        buffer = self._buffer
        start = 0
        while True:
            idx = buffer.find(FRAME_HEADER_BYTE0, start)
            if idx < 0 or idx + 1 >= len(buffer):
                return -1
            if buffer[idx + 1] in FRAME_STATUS_BYTES:
                return idx
            start = idx + 1


@dataclass
class ImuStats:
    """Running counters for diagnostics."""

    notification_count: int = 0
    parsed_frame_count: int = 0
    discarded_startup_sample_count: int = 0
    written_sample_count: int = 0
    dropped_byte_count: int = 0
    last_notification_gap_ms: float = 0.0
    max_notification_gap_ms: float = 0.0
    _last_notification_ts: Optional[datetime] = field(default=None, repr=False)

    def notification_received(self, timestamp_utc: Optional[datetime] = None) -> None:
        if timestamp_utc is None:
            timestamp_utc = _utcnow()
        if self._last_notification_ts is not None:
            gap = (timestamp_utc - self._last_notification_ts).total_seconds() * 1000.0
            self.last_notification_gap_ms = max(0.0, gap)
            self.max_notification_gap_ms = max(self.max_notification_gap_ms, self.last_notification_gap_ms)
        self._last_notification_ts = timestamp_utc
        self.notification_count += 1


class ImuSampleProcessor:
    """Turns raw frames into :class:`ImuSample`, dropping the startup noise window."""

    def __init__(self, gyro_scale: float, accel_scale: float, startup_discard_samples: int) -> None:
        self._gyro_scale = gyro_scale
        self._accel_scale = accel_scale
        self._startup_discard_samples = startup_discard_samples
        self.stats = ImuStats()
        self._next_frame_index = 0

    def process_frame(self, frame: bytes, timestamp_utc: Optional[datetime] = None) -> Optional[ImuSample]:
        self.stats.parsed_frame_count += 1

        if self.stats.discarded_startup_sample_count < self._startup_discard_samples:
            self.stats.discarded_startup_sample_count += 1
            return None

        if timestamp_utc is None:
            timestamp_utc = _utcnow()

        sample = ImuSample.from_frame(
            frame,
            self._next_frame_index,
            self._gyro_scale,
            self._accel_scale,
            timestamp_utc,
        )
        self._next_frame_index += 1
        self.stats.written_sample_count += 1
        return sample


class BleNotificationProcessor:
    """Top-level pipeline: notification bytes -> list of :class:`ImuSample`."""

    def __init__(self, gyro_scale: float, accel_scale: float, startup_discard_samples: int) -> None:
        self._parser = FrameParser()
        self._processor = ImuSampleProcessor(gyro_scale, accel_scale, startup_discard_samples)
        self.stats = self._processor.stats

    def process(self, data: bytes, timestamp_utc: Optional[datetime] = None) -> List[ImuSample]:
        if timestamp_utc is None:
            timestamp_utc = _utcnow()
        self.stats.notification_received(timestamp_utc)

        samples: List[ImuSample] = []
        for frame in self._parser.push(data):
            sample = self._processor.process_frame(frame, timestamp_utc)
            self.stats.dropped_byte_count = self._parser.dropped_byte_count
            if sample is not None:
                samples.append(sample)
        self.stats.dropped_byte_count = self._parser.dropped_byte_count
        return samples
