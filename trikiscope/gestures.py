"""Lightweight motion-gesture detection for the Triki controller.

Fed one :class:`ImuSample` at a time, it recognises a handful of robust,
game-controller-style gestures from gyro/accel magnitudes:

* FREE-FALL / THROW - accelerometer magnitude collapses toward 0 g,
* TAP / IMPACT      - a sharp accelerometer spike,
* SHAKE             - sustained high gyro activity,
* SPIN              - one gyro axis dominates at high rate.

A short refractory period stops a single event from firing repeatedly, and the
last event is held for a moment so the UI can show it.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

from .protocol import ImuSample

# Thresholds (gyro in deg/s, accel in g).
FREE_FALL_ACCEL_G = 0.35
FREE_FALL_MIN_SAMPLES = 2
IMPACT_ACCEL_G = 2.5
SHAKE_GYRO_DPS = 300.0
SHAKE_MIN_SAMPLES = 4
SPIN_AXIS_DPS = 250.0

REFRACTORY_SECONDS = 0.4
HOLD_SECONDS = 0.8


@dataclass(slots=True)
class GestureEvent:
    name: str
    timestamp: float  # seconds (sample timestamp)
    magnitude: float


class GestureDetector:
    def __init__(self) -> None:
        self._free_fall_run = 0
        self._shake_run = 0
        self._last_event_time: float = -1e9
        self.last_event: Optional[GestureEvent] = None
        self.recent: Deque[GestureEvent] = deque(maxlen=12)

    def reset(self) -> None:
        self._free_fall_run = 0
        self._shake_run = 0
        self._last_event_time = -1e9
        self.last_event = None
        self.recent.clear()

    def update(self, sample: ImuSample) -> Optional[GestureEvent]:
        t = sample.timestamp_utc.timestamp()
        accel = sample.accel_magnitude
        gyro = sample.gyro_magnitude

        # Track running conditions even during refractory so state is current.
        self._free_fall_run = self._free_fall_run + 1 if accel < FREE_FALL_ACCEL_G else 0
        self._shake_run = self._shake_run + 1 if gyro > SHAKE_GYRO_DPS else 0

        if t - self._last_event_time < REFRACTORY_SECONDS:
            return None

        event: Optional[GestureEvent] = None
        # Priority: a hard impact wins, then free-fall, then shake, then spin.
        if accel > IMPACT_ACCEL_G:
            event = GestureEvent("TAP / IMPACT", t, accel)
        elif self._free_fall_run >= FREE_FALL_MIN_SAMPLES:
            event = GestureEvent("FREE-FALL / THROW", t, accel)
        elif self._shake_run >= SHAKE_MIN_SAMPLES:
            event = GestureEvent("SHAKE", t, gyro)
        else:
            axis = max(abs(sample.gyro_x), abs(sample.gyro_y), abs(sample.gyro_z))
            if axis > SPIN_AXIS_DPS:
                event = GestureEvent("SPIN", t, axis)

        if event is not None:
            self._last_event_time = t
            self.last_event = event
            self.recent.appendleft(event)
        return event

    def active_label(self, now_seconds: float) -> Optional[str]:
        """The most recent event, if it is still within the hold window."""
        if self.last_event is None:
            return None
        if now_seconds - self.last_event.timestamp <= HOLD_SECONDS:
            return self.last_event.name
        return None
