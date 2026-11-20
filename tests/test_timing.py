"""Sample timestamps must reflect the IMU rate, not BLE notification bursts."""

import struct
from datetime import datetime, timedelta, timezone

import pytest

from trikiscope.protocol import (
    BleNotificationProcessor,
    SampleClock,
    sample_rate_from_start_command,
)
from trikiscope.config import DEFAULT_START_COMMAND

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
FRAME = b"\x22\x00" + struct.pack("<6h", 0, 0, 0, 0, 0, 2048)


def simulate(odr: float, seconds: float, chunk: int, burst: int, interval_ms: float, nominal: float):
    """Frames produced at ``odr``, sent as ``chunk``-byte notifications, at most
    ``burst`` of them per connection event every ``interval_ms``."""
    stream = FRAME * int(odr * seconds)
    proc = BleNotificationProcessor(131.0, 2048.0, 0, sample_rate_hz=nominal)
    samples, pos, event = [], 0, 0
    while pos + chunk <= len(stream):
        event += 1
        now = event * interval_ms / 1000.0
        ready = int(now * odr) * len(FRAME)
        sent = 0
        while pos + chunk <= ready and sent < burst:
            samples += proc.process(stream[pos:pos + chunk], T0 + timedelta(seconds=now))
            pos += chunk
            sent += 1
    return samples


@pytest.mark.parametrize(
    "chunk,burst,interval_ms",
    [(20, 1, 7.5), (20, 4, 15.0), (16, 4, 30.0), (20, 6, 45.0)],
)
def test_sample_spacing_matches_imu_rate_regardless_of_ble_timing(chunk, burst, interval_ms):
    samples = simulate(104.0, 10.0, chunk, burst, interval_ms, nominal=104.0)
    dts = [(b.timestamp_utc - a.timestamp_utc).total_seconds() for a, b in zip(samples, samples[1:])]
    assert all(dt > 0 for dt in dts), "timestamps must be strictly increasing"
    integrated = sum(dts)
    true = (len(samples) - 1) / 104.0
    assert integrated == pytest.approx(true, rel=0.03)
    # Each step stays close to the sample period (no 20 ms fallback steps).
    assert max(dts) < 1.5 / 104.0


def test_clock_learns_real_rate_when_nominal_is_wrong():
    # Stream really runs at 52 Hz but we assumed 104 Hz.
    samples = simulate(52.0, 10.0, 20, 4, 30.0, nominal=104.0)
    late = samples[len(samples) // 2:]
    span = (late[-1].timestamp_utc - late[0].timestamp_utc).total_seconds()
    assert span / (len(late) - 1) == pytest.approx(1 / 52.0, rel=0.03)


def test_timeline_stays_locked_to_arrival_clock():
    samples = simulate(104.0, 10.0, 20, 4, 30.0, nominal=100.0)
    end = T0 + timedelta(seconds=10.0)
    assert abs((samples[-1].timestamp_utc - end).total_seconds()) < 0.1


def test_clock_reanchors_after_stall():
    clock = SampleClock(100.0)
    first = clock.stamp(T0, 2)
    later = clock.stamp(T0 + timedelta(seconds=2.0), 2)
    assert later[-1] == T0 + timedelta(seconds=2.0)
    assert (later[0] - first[-1]).total_seconds() > 1.0
    assert clock.period == pytest.approx(0.01)  # the stall did not inflate the period


def test_rate_from_start_command():
    assert sample_rate_from_start_command(DEFAULT_START_COMMAND) == 104.0
    assert sample_rate_from_start_command(bytes.fromhex("201000D007340003")) == 52.0
    assert sample_rate_from_start_command(b"\x01\x02") is None
