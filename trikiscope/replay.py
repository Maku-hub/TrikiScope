"""Play a recorded session back through the normal pipeline, without a device.

Two recording formats are understood:

* **raw** (``.jsonl``, written by the recorder next to the CSV) - every BLE
  notification exactly as received, so parsing, the sample clock, orientation,
  gestures and games all run on the real byte stream;
* **samples** (``.csv``) - decoded samples; each row is re-encoded into a 14-byte
  frame (raw counts + button flag), so older CSV recordings can be replayed too.

:class:`ReplayClient` has the same surface as :class:`~trikiscope.ble.TrikiBleClient`
(``run`` / ``stop`` / ``set_led``) and reports through the same callbacks.
Timing follows the recording at 1x speed (orientation integrates real time).
"""

from __future__ import annotations

import asyncio
import csv
import json
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional

from .ble import AdvertisementInfo, BleCallbacks, DeviceInfo
from .recorder import RAW_FORMAT


@dataclass
class Recording:
    path: Path
    kind: str  # "raw" or "csv"
    records: List[tuple[float, bytes]]  # (seconds, notification payload)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.records[-1][0] - self.records[0][0] if self.records else 0.0


class RecordingError(ValueError):
    pass


def load_recording(path: str | Path) -> Recording:
    """Load a raw ``.jsonl`` or samples ``.csv`` recording."""
    path = Path(path)
    with path.open(encoding="utf-8") as fh:
        first = fh.readline()
    if first.lstrip().startswith("{"):
        recording = _load_raw(path)
    else:
        recording = _load_csv(path)
    if not recording.records:
        raise RecordingError(f"{path}: no data to replay")
    return recording


def _load_raw(path: Path) -> Recording:
    meta: dict[str, Any] = {}
    records: List[tuple[float, bytes]] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if lineno == 1 and obj.get("format") == RAW_FORMAT:
                    meta = obj
                    continue
                records.append((float(obj["t"]), bytes.fromhex(obj["data"])))
            except (ValueError, KeyError, TypeError) as exc:
                raise RecordingError(f"{path}:{lineno}: not a raw recording line ({exc})") from exc
    return Recording(path, "raw", records, meta)


def _load_csv(path: Path) -> Recording:
    records: List[tuple[float, bytes]] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        needed = {"timestamp_utc", "raw_gyro_x", "raw_gyro_y", "raw_gyro_z",
                  "raw_accel_x", "raw_accel_y", "raw_accel_z"}
        if not reader.fieldnames or not needed <= set(reader.fieldnames):
            raise RecordingError(f"{path}: not a TrikiScope samples CSV (missing columns)")
        for lineno, row in enumerate(reader, 2):
            try:
                t = datetime.fromisoformat(row["timestamp_utc"]).timestamp()
                status = 1 if row.get("button", "0").strip() == "1" else 0
                frame = bytes([0x22, status]) + struct.pack(
                    "<6h",
                    int(row["raw_gyro_x"]), int(row["raw_gyro_y"]), int(row["raw_gyro_z"]),
                    int(row["raw_accel_x"]), int(row["raw_accel_y"]), int(row["raw_accel_z"]),
                )
            except (ValueError, struct.error) as exc:
                raise RecordingError(f"{path}:{lineno}: bad sample row ({exc})") from exc
            records.append((t, frame))
    return Recording(path, "csv", records, {})


class ReplayClient:
    """Feeds a :class:`Recording` to the app's BLE callbacks in real time."""

    def __init__(self, recording: Recording, callbacks: BleCallbacks, loop: bool = False) -> None:
        self._rec = recording
        self._cb = callbacks
        self._loop = loop
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    async def set_led(self, on: bool) -> bool:
        self._cb.on_log("LED is not available during replay.")
        return False

    async def run(self) -> None:
        rec = self._rec
        meta = rec.meta
        name = meta.get("device_name") or f"replay: {rec.path.name}"
        self._cb.on_state("connecting")
        self._cb.on_advertisement(
            AdvertisementInfo(name=name, address=meta.get("device_address") or "replay",
                              rssi=None, tx_power=None)
        )
        self._cb.on_device_info(
            DeviceInfo(device_name=name, firmware_revision=meta.get("firmware_revision"))
        )
        self._cb.on_gatt([])
        self._cb.on_state("connected")
        self._cb.on_log(
            f"Replaying {rec.path} ({rec.kind}, {len(rec.records)} records, {rec.duration:.1f} s)"
            + (", looping" if self._loop else "")
        )
        try:
            while True:
                if not await self._play_once():
                    self._cb.on_log("Replay stopped.")
                    return
                if not self._loop:
                    self._cb.on_log("Replay finished.")
                    return
        finally:
            self._cb.on_state("disconnected")

    async def _play_once(self) -> bool:
        """Play all records once; False if stopped midway."""
        loop = asyncio.get_running_loop()
        t0 = self._rec.records[0][0]
        start_mono = loop.time()
        start_wall = datetime.now(timezone.utc)
        for t, data in self._rec.records:
            offset = t - t0
            delay = offset - (loop.time() - start_mono)
            if delay > 0 and await self._stopped_within(delay):
                return False
            if self._stop_event.is_set():
                return False
            self._cb.on_notification(data, start_wall + timedelta(seconds=offset))
        return True

    async def _stopped_within(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False


def recording_start_command(recording: Recording) -> Optional[bytes]:
    """The start command stored in a raw recording's header, if any."""
    value = recording.meta.get("start_command")
    if not value:
        return None
    try:
        return bytes.fromhex(value)
    except ValueError:
        return None
