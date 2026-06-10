"""Tests for the gesture detector."""

from datetime import datetime, timedelta, timezone

from trikiscope.gestures import GestureDetector
from trikiscope.protocol import ImuSample


def sample(gx, gy, gz, ax, ay, az, t):
    return ImuSample(0, t, gx, gy, gz, ax, ay, az, 0, 0, 0, 0, 0, 0)


def test_free_fall_detected():
    det = GestureDetector()
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    events = []
    for _ in range(5):
        t += timedelta(milliseconds=10)
        e = det.update(sample(0, 0, 0, 0.0, 0.0, 0.05, t))  # ~0 g -> free fall
        if e:
            events.append(e.name)
    assert "FREE-FALL / THROW" in events


def test_impact_detected():
    det = GestureDetector()
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    t += timedelta(milliseconds=10)
    e = det.update(sample(0, 0, 0, 0.0, 0.0, 3.5, t))  # 3.5 g spike
    assert e is not None and e.name == "TAP / IMPACT"


def test_shake_detected():
    det = GestureDetector()
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    names = []
    for _ in range(6):
        t += timedelta(milliseconds=10)
        # Magnitude > 300 dps but no single axis over the SPIN threshold (250).
        e = det.update(sample(200, 200, 150, 0.0, 0.0, 1.0, t))
        if e:
            names.append(e.name)
    assert "SHAKE" in names


def test_refractory_prevents_spam():
    det = GestureDetector()
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    count = 0
    for _ in range(10):
        t += timedelta(milliseconds=10)  # within 0.4 s refractory window
        if det.update(sample(0, 0, 0, 0.0, 0.0, 3.5, t)):
            count += 1
    assert count == 1  # only the first impact fires


def test_active_label_hold_and_decay():
    det = GestureDetector()
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    e = det.update(sample(0, 0, 0, 0.0, 0.0, 3.5, t0 + timedelta(milliseconds=10)))
    base = e.timestamp
    assert det.active_label(base + 0.1) == "TAP / IMPACT"
    assert det.active_label(base + 5.0) is None  # decayed away


def test_quiet_motion_no_event():
    det = GestureDetector()
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for _ in range(20):
        t += timedelta(milliseconds=10)
        assert det.update(sample(1, -1, 0.5, 0.0, 0.0, 1.0, t)) is None
