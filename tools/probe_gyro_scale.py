"""Measure the gyro scale (LSB per deg/s) by turning the Triki exactly 360 degrees.

The frame format carries raw int16 gyro counts; TrikiScope converts them with
``gyro_scale``.  The old default 131.0 (an MPU-6050 value) was wrong: this probe
measured 14.26 on a real cap, i.e. the LSM6DSL +-2000 dps setting (datasheet
14.29) that the start command ``20 10 00 D0 07 68 00 03`` selects.  How it works:

    1) keep the cap still on a table      -> gyro bias is measured
    2) turn it flat on the table by exactly N full turns (default 1), then stop
    3) the integral of raw gyro-Z over the turn divided by N*360 deg = LSB/(deg/s)

Repeat for several trials; the script compares the average against the
LSM6DSL full-scale settings.  Run it in YOUR terminal:

    python tools\\probe_gyro_scale.py [--turns 1] [--trials 3]

Tip: align an edge of the cap with something on the table (a sheet of paper's
edge) so the start and end pose match well; 1-2 deg error = ~0.5% scale error.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trikiscope.ble import BleCallbacks, TrikiBleClient  # noqa: E402
from trikiscope.config import AppConfig  # noqa: E402
from trikiscope.protocol import BleNotificationProcessor, ImuSample  # noqa: E402

# LSB per deg/s for each candidate configuration.
CANDIDATES = {
    "MPU-6050 +-250 dps (old default)": 131.0,
    "LSM6DSL +-125 dps": 1000 / 4.375,
    "LSM6DSL +-250 dps": 1000 / 8.75,
    "LSM6DSL +-500 dps": 1000 / 17.5,
    "LSM6DSL +-1000 dps": 1000 / 35.0,
    "LSM6DSL +-2000 dps": 1000 / 70.0,
}

BIAS_SECONDS = 2.0
STILL_END_SECONDS = 1.0
INT16_LIMIT = 32760


@dataclass
class TurnAnalyzer:
    """State machine: bias -> wait for motion -> integrate -> still again = done."""

    turns: float = 1.0
    phase: str = "bias"  # bias -> armed -> turning -> done
    bias: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    threshold: float = 0.0
    integral: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    peak_raw: int = 0
    samples: int = 0
    _bias_buf: List[tuple] = field(default_factory=list)
    _bias_t0: Optional[datetime] = None
    _last_t: Optional[datetime] = None
    _still_since: Optional[datetime] = None
    _turn_t0: Optional[datetime] = None
    duration: float = 0.0

    def feed(self, s: ImuSample) -> None:
        raw = (s.raw_gyro_x, s.raw_gyro_y, s.raw_gyro_z)
        t = s.timestamp_utc
        if self.phase == "bias":
            if self._bias_t0 is None:
                self._bias_t0 = t
            self._bias_buf.append(raw)
            if (t - self._bias_t0).total_seconds() >= BIAS_SECONDS:
                n = len(self._bias_buf)
                self.bias = [sum(v[i] for v in self._bias_buf) / n for i in range(3)]
                var = sum((v[2] - self.bias[2]) ** 2 for v in self._bias_buf) / n
                # Motion = clearly above noise; floor keeps it sane on a dead-quiet sensor.
                self.threshold = max(8.0 * math.sqrt(var), 40.0)
                self.phase = "armed"
            return

        z = raw[2] - self.bias[2]
        if self.phase == "armed":
            if abs(z) > self.threshold:
                self.phase = "turning"
                self._turn_t0 = t
                self._last_t = t
            return

        if self.phase == "turning":
            dt = (t - self._last_t).total_seconds()
            self._last_t = t
            for i in range(3):
                self.integral[i] += (raw[i] - self.bias[i]) * dt
            self.peak_raw = max(self.peak_raw, max(abs(v) for v in raw))
            self.samples += 1
            if abs(z) <= self.threshold:
                if self._still_since is None:
                    self._still_since = t
                elif (t - self._still_since).total_seconds() >= STILL_END_SECONDS:
                    self.duration = (t - self._turn_t0).total_seconds()
                    self.phase = "done"
            else:
                self._still_since = None

    @property
    def lsb_per_dps(self) -> float:
        return abs(self.integral[2]) / (360.0 * self.turns)


def report(results: List[TurnAnalyzer]) -> None:
    print("\n=== Results ===")
    for i, r in enumerate(results, 1):
        off_axis = math.hypot(r.integral[0], r.integral[1]) / max(abs(r.integral[2]), 1e-9) * 100
        sat = "  SATURATED - turn slower!" if r.peak_raw >= INT16_LIMIT else ""
        print(f"trial {i}: {r.lsb_per_dps:7.2f} LSB/(deg/s)  turn {r.duration:4.1f}s, "
              f"{r.samples} samples, off-axis {off_axis:4.1f}%, peak raw {r.peak_raw}{sat}")
    valid = [r.lsb_per_dps for r in results if r.peak_raw < INT16_LIMIT]
    if not valid:
        print("No valid trial (all saturated).")
        return
    mean = sum(valid) / len(valid)
    print(f"\nmean: {mean:.2f} LSB/(deg/s)   (=> --gyro-scale {mean:.2f})")
    for name, value in sorted(CANDIDATES.items(), key=lambda kv: abs(kv[1] - mean)):
        print(f"  {name:36s} {value:7.2f}  error {100 * (mean - value) / value:+6.1f}%")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--turns", type=float, default=1.0, help="full turns per trial (default 1)")
    ap.add_argument("--trials", type=int, default=3, help="number of trials (default 3)")
    args = ap.parse_args()

    config = AppConfig(scan_timeout_seconds=30.0)
    processor = BleNotificationProcessor(config.gyro_scale, config.accel_scale, config.startup_discard_samples)
    results: List[TurnAnalyzer] = []
    current = TurnAnalyzer(turns=args.turns)
    state = {"current": current}

    def on_notification(data: bytes, ts: datetime) -> None:
        for s in processor.process(data, ts):
            state["current"].feed(s)

    callbacks = BleCallbacks(
        on_log=lambda m: print(f"[ble] {m}"),
        on_state=lambda s: None,
        on_advertisement=lambda a: print(f"[ble] found {a.name} {a.address}"),
        on_device_info=lambda i: None,
        on_gatt=lambda g: None,
        on_battery=lambda b: None,
        on_notification=on_notification,
    )
    client = TrikiBleClient(config, callbacks)
    print("Press the button on the Triki to wake it...")
    task = asyncio.create_task(client.run())

    try:
        for trial in range(1, args.trials + 1):
            current = TurnAnalyzer(turns=args.turns)
            state["current"] = current
            shown = None
            while current.phase != "done":
                if task.done():
                    print("BLE session ended.")
                    report(results)
                    return
                if current.phase != shown:
                    shown = current.phase
                    if shown == "bias":
                        print(f"\n--- trial {trial}/{args.trials}: lay the cap FLAT and keep it STILL ...")
                    elif shown == "armed":
                        print(f"    bias z={current.bias[2]:+.1f} raw, motion threshold {current.threshold:.0f} raw")
                        print(f"    now turn it {args.turns:g} full turn(s) on the table, then let go and keep still")
                    elif shown == "turning":
                        print("    turning... (stop at the start pose and hold still for 1 s)")
                await asyncio.sleep(0.05)
            print(f"    done: {current.lsb_per_dps:.2f} LSB/(deg/s)")
            results.append(current)
    finally:
        client.stop()
        await task
    report(results)


if __name__ == "__main__":
    asyncio.run(main())
