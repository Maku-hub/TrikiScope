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
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, Iterator, List, Optional

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


RESYNC_CONFIRM_FRAMES = 3
"""Consecutive headers, 14 bytes apart, required to accept a boundary after sync loss."""


class FrameParser:
    """Re-assembles 14-byte frames from a stream of BLE notification chunks.

    The header (``22 00`` / ``22 01``) is not unique: a payload int16 equal to 34
    or 290 encodes as the same two bytes.  While the stream is in sync the parser
    simply expects the next header right after the previous frame.  After sync is
    lost it only accepts a boundary confirmed by :data:`RESYNC_CONFIRM_FRAMES`
    headers spaced exactly one frame apart; if several alignments within one frame
    pass that test (a constant payload field equal to 34), the one whose
    accelerometer magnitude is closest to 1 g wins.
    """

    def __init__(self, accel_lsb_per_g: float = 2048.0) -> None:
        self._buffer = bytearray()
        self._accel_lsb_per_g = accel_lsb_per_g
        # A fresh stream is assumed to start on a frame boundary; if it does not,
        # the first mismatch drops the parser into confirmed resync.
        self._synced = True
        self.dropped_byte_count = 0
        self.resync_count = 0

    def push(self, data: bytes) -> Iterator[bytes]:
        """Feed raw bytes; yield each complete frame discovered."""
        self._buffer.extend(data)

        while True:
            if self._synced:
                if len(self._buffer) < 2:
                    return
                if not self._is_header(0):
                    self._synced = False
                    self.resync_count += 1
                    continue
            elif not self._resync():
                return

            if len(self._buffer) < FRAME_LENGTH:
                return
            frame = bytes(self._buffer[:FRAME_LENGTH])
            del self._buffer[:FRAME_LENGTH]
            yield frame

    def _is_header(self, idx: int) -> bool:
        buffer = self._buffer
        return (
            idx + 1 < len(buffer)
            and buffer[idx] == FRAME_HEADER_BYTE0
            and buffer[idx + 1] in FRAME_STATUS_BYTES
        )

    def _drop(self, count: int) -> None:
        if count > 0:
            self.dropped_byte_count += count
            del self._buffer[:count]

    def _resync(self) -> bool:
        """Align the buffer on a confirmed frame boundary; False = need more data."""
        span = FRAME_LENGTH * (RESYNC_CONFIRM_FRAMES - 1) + 2
        while True:
            first = self._first_candidate()
            if first < 0:
                # No header start at all.  Keep only a trailing 0x22 that may be
                # the first byte of a header split across notifications.
                keep = 1 if self._buffer and self._buffer[-1] == FRAME_HEADER_BYTE0 else 0
                self._drop(len(self._buffer) - keep)
                return False
            self._drop(first)
            # Judge every alignment within one frame of the first candidate; wait
            # until each possible candidate has enough data behind it to be judged.
            best: Optional[int] = None
            best_score = 0.0
            for offset in range(FRAME_LENGTH):
                if offset + 1 >= len(self._buffer):
                    return False
                if not self._is_header(offset):
                    continue
                if offset + span > len(self._buffer):
                    return False
                if not all(self._is_header(offset + k * FRAME_LENGTH) for k in range(1, RESYNC_CONFIRM_FRAMES)):
                    continue
                score = self._implausibility(offset)
                if best is None or score < best_score:
                    best, best_score = offset, score
            if best is None:
                self._drop(1)  # the first candidate was a false header
                continue
            self._drop(best)
            self._synced = True
            return True

    def _first_candidate(self) -> int:
        buffer = self._buffer
        start = 0
        while True:
            idx = buffer.find(FRAME_HEADER_BYTE0, start)
            if idx < 0 or idx + 1 >= len(buffer):
                return -1
            if buffer[idx + 1] in FRAME_STATUS_BYTES:
                return idx
            start = idx + 1

    def _implausibility(self, offset: int) -> float:
        """How far the accel magnitude of the frame at ``offset`` is from 1 g."""
        ax, ay, az = struct.unpack_from("<3h", self._buffer, offset + 8)
        magnitude = (ax * ax + ay * ay + az * az) ** 0.5 / self._accel_lsb_per_g
        return abs(magnitude - 1.0)


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


DEFAULT_SAMPLE_RATE_HZ = 104.0


def sample_rate_from_start_command(command: bytes) -> Optional[float]:
    """Output data rate encoded in a start command, if it looks like one.

    Observed commands are ``20 10 00 D0 07 <odr u16 LE> 03``: TrikiScope sends
    ``68 00`` (104 Hz, matching the measured ~104 frames/s) and the Żappka app
    ``34 00`` (52 Hz) - both LSM6DSL output data rates.
    """
    if len(command) != 8 or command[0] != 0x20:
        return None
    rate = int.from_bytes(command[5:7], "little")
    return float(rate) if 1 <= rate <= 6660 else None


class SampleClock:
    """Gives evenly spaced timestamps to samples that arrive in BLE bursts.

    Several frames share one notification and notifications are delivered in
    bursts once per connection interval, so arrival time says little about when
    a sample was taken.  Samples are instead spaced by the sample period: the
    nominal one at first, then the rate measured over the last few seconds (which
    also absorbs lost frames).  A slow correction keeps the timeline locked to the
    arrival clock, and a gap or jump beyond ``max_offset_s`` re-anchors it.
    """

    def __init__(
        self,
        nominal_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
        correction: float = 0.01,
        max_offset_s: float = 0.25,
        window_s: float = 3.0,
        min_window_s: float = 1.0,
    ) -> None:
        self._nominal_period = 1.0 / nominal_rate_hz
        self.period = self._nominal_period
        self._correction = correction
        self._max_offset = max_offset_s
        self._window_s = window_s
        self._min_window_s = min_window_s
        self._arrivals: Deque[tuple[float, int]] = deque()  # (arrival s, cumulative samples)
        self._count = 0
        self._last: Optional[float] = None

    def stamp(self, arrival: datetime, n: int) -> List[datetime]:
        if n <= 0:
            return []
        a = arrival.timestamp()
        if self._last is not None and abs(a - (self._last + self.period)) > self._max_offset:
            # Stream stalled (or the clock jumped): don't let the gap inflate the rate.
            self._arrivals.clear()
            self._last = None
        self._update_period(a, n)

        out: List[datetime] = []
        for i in range(n):
            if self._last is None:
                # (Re)anchor so the last sample of this burst lands on its arrival.
                t = a - (n - 1 - i) * self.period
            else:
                t = self._last + self.period
                t += self._correction * (a - t)
            self._last = t
            out.append(datetime.fromtimestamp(t, timezone.utc))
        return out

    def _update_period(self, a: float, n: int) -> None:
        self._count += n
        self._arrivals.append((a, self._count))
        while len(self._arrivals) > 2 and a - self._arrivals[1][0] >= self._window_s:
            self._arrivals.popleft()
        first_a, first_count = self._arrivals[0]
        span = a - first_a
        if span >= self._min_window_s and self._count > first_count:
            measured = span / (self._count - first_count)
            # Trust the measurement only within a sane band around the nominal rate.
            self.period = min(max(measured, self._nominal_period / 4), self._nominal_period * 4)


class BleNotificationProcessor:
    """Top-level pipeline: notification bytes -> list of :class:`ImuSample`."""

    def __init__(
        self,
        gyro_scale: float,
        accel_scale: float,
        startup_discard_samples: int,
        sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
    ) -> None:
        self._parser = FrameParser(accel_lsb_per_g=accel_scale)
        self._processor = ImuSampleProcessor(gyro_scale, accel_scale, startup_discard_samples)
        self.clock = SampleClock(sample_rate_hz)
        self.stats = self._processor.stats

    @property
    def resync_count(self) -> int:
        return self._parser.resync_count

    def process(self, data: bytes, timestamp_utc: Optional[datetime] = None) -> List[ImuSample]:
        """Decode one notification.

        ``timestamp_utc`` is the arrival time; each returned sample carries its
        own evenly spaced timestamp from :class:`SampleClock`.
        """
        if timestamp_utc is None:
            timestamp_utc = _utcnow()
        self.stats.notification_received(timestamp_utc)

        frames = list(self._parser.push(data))
        stamps = self.clock.stamp(timestamp_utc, len(frames))
        samples: List[ImuSample] = []
        for frame, stamp in zip(frames, stamps):
            sample = self._processor.process_frame(frame, stamp)
            if sample is not None:
                samples.append(sample)
        self.stats.dropped_byte_count = self._parser.dropped_byte_count
        return samples
