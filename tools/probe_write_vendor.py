"""SAFE read-modify-restore probe of the vendor characteristic 6e400004.

Safety model:
* read and remember the ORIGINAL value first; abort entirely if it can't be read,
* only write small, already-observed values (0x00, 0x01, 0xFF),
* restore the original immediately after each test write,
* a final `finally` restores the original no matter what.

For each test value it observes the IMU stream (rate, 22 00 vs 22 01 frame counts,
payload sizes) so we can see whether the register changes streaming behaviour.

Usage:  python tools\\probe_write_vendor.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bleak import BleakClient, BleakScanner

from trikiscope import gatt_names as g
from trikiscope.config import DEFAULT_START_COMMAND, NUS_RX_CHAR_UUID, NUS_TX_CHAR_UUID

VENDOR_CHAR = "6e400004-b5a3-f393-e0a9-e50e24dcca9e"
TEST_VALUES = [b"\x00", b"\x01", b"\xff"]


async def find(name="Triki", timeout=25.0):
    print(f"Scanning for '{name}' (wake it with the button if needed)...", flush=True)
    fut = asyncio.get_event_loop().create_future()
    needle = name.lower()

    def cb(device, adv):
        nm = adv.local_name or device.name
        if nm and needle in nm.lower() and not fut.done():
            fut.set_result(device)

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        await scanner.stop()


class StreamObserver:
    def __init__(self):
        self.chunks = []

    def handler(self, _sender, data: bytearray):
        self.chunks.append((time.perf_counter(), bytes(data)))

    def snapshot(self):
        n = len(self.chunks)
        self.chunks = []
        return n


def summarise(chunks, seconds):
    raw = bytearray()
    sizes = Counter()
    for _, b in chunks:
        raw.extend(b)
        sizes[len(b)] += 1
    released = pressed = stray = 0
    pos = 0
    while pos < len(raw):
        if raw[pos] == 0x22 and pos + 14 <= len(raw) and raw[pos + 1] in (0x00, 0x01):
            if raw[pos + 1] == 0x01:
                pressed += 1
            else:
                released += 1
            pos += 14
        else:
            stray += 1
            pos += 1
    rate = (released + pressed) / seconds if seconds else 0
    return {
        "notif": len(chunks),
        "frames_22_00": released,
        "frames_22_01": pressed,
        "stray": stray,
        "rate_hz": round(rate, 1),
        "sizes": dict(sorted(sizes.items())),
    }


async def observe(observer, label, seconds):
    observer.chunks = []
    await asyncio.sleep(seconds)
    s = summarise(observer.chunks, seconds)
    print(f"  [{label}] {s}")
    return s


async def main():
    device = await find()
    if device is None:
        print("Device not found.")
        return 1

    observer = StreamObserver()
    async with BleakClient(device) as client:
        print(f"Connected to {device.address}.", flush=True)

        # 1) Read and remember the original value. Abort if we cannot.
        try:
            original = bytes(await client.read_gatt_char(VENDOR_CHAR))
        except Exception as exc:  # noqa: BLE001
            print(f"ABORT: cannot read original value of 6e400004: {exc}")
            return 1
        print(f"Original 6e400004 = {g.hexdump(original)}")

        await client.start_notify(NUS_TX_CHAR_UUID, observer.handler)
        await client.write_gatt_char(NUS_RX_CHAR_UUID, DEFAULT_START_COMMAND, response=False)
        await asyncio.sleep(1.0)

        try:
            await observe(observer, "baseline", 3.0)
            for value in TEST_VALUES:
                print(f"\n== Writing 6e400004 = {g.hexdump(value)} ==")
                try:
                    await client.write_gatt_char(VENDOR_CHAR, value, response=True)
                except Exception as exc:  # noqa: BLE001
                    print(f"  write failed: {exc}")
                    continue
                try:
                    readback = bytes(await client.read_gatt_char(VENDOR_CHAR))
                    print(f"  readback = {g.hexdump(readback)}")
                except Exception as exc:  # noqa: BLE001
                    print(f"  readback failed: {exc}")
                await observe(observer, f"after write {g.hexdump(value)}", 3.0)

                # Restore immediately and observe recovery.
                await client.write_gatt_char(VENDOR_CHAR, original, response=True)
                await observe(observer, "after restore", 1.5)
        finally:
            # Guarantee the original value is back.
            try:
                await client.write_gatt_char(VENDOR_CHAR, original, response=True)
                final = bytes(await client.read_gatt_char(VENDOR_CHAR))
                print(f"\nRestored 6e400004 = {g.hexdump(final)} (original was {g.hexdump(original)})")
            except Exception as exc:  # noqa: BLE001
                print(f"\nWARNING: could not confirm restore: {exc}")
            try:
                await client.stop_notify(NUS_TX_CHAR_UUID)
            except Exception:  # noqa: BLE001
                pass

    print("\nDone. Compare 'rate_hz', frame counts and 'sizes' across the writes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
