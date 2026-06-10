"""Interactive probe to discover button / non-IMU traffic on the Triki.

Run it in YOUR terminal and follow the on-screen countdown. It captures the
NUS TX stream (and any other notifying characteristic) across three phases:

    1) STILL    - baseline, don't touch the button
    2) PRESS    - press the button repeatedly
    3) STILL    - baseline again

then compares the phases: it reassembles the IMU byte stream, removes valid
14-byte `22 00` frames, and reports the leftover ("stray") byte patterns per
phase. A button message should show up as a distinctive pattern that appears
in the PRESS phase but not the STILL phases. It also reads the vendor
characteristic 6e400004 once per phase, in case the button latches a register.

Usage:  python tools\\probe_button.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bleak import BleakClient, BleakScanner

from trikiscope import gatt_names as g
from trikiscope.config import (
    DEFAULT_START_COMMAND,
    NUS_RX_CHAR_UUID,
    NUS_TX_CHAR_UUID,
)

VENDOR_CHAR = "6e400004-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX = NUS_TX_CHAR_UUID.lower()


async def find(name="Triki", timeout=25.0):
    print(f"Scanning for '{name}' (press the button to wake it if needed)...", flush=True)
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


async def countdown(label: str, seconds: int):
    for remaining in range(seconds, 0, -1):
        print(f"\r  {label}: {remaining:2d}s  ", end="", flush=True)
        await asyncio.sleep(1.0)
    print(f"\r  {label}: done.      ", flush=True)


def analyse_phase(name: str, chunks, vendor_value):
    raw = bytearray()
    for _, b in chunks:
        raw.extend(b)

    frames = 0
    stray = bytearray()
    stray_runs = []
    pos = 0
    while pos < len(raw):
        if raw[pos] == 0x22 and raw[pos + 1: pos + 2] == b"\x00" and pos + 14 <= len(raw):
            if stray:
                stray_runs.append(bytes(stray))
                stray.clear()
            frames += 1
            pos += 14
        else:
            stray.append(raw[pos])
            pos += 1
    if stray:
        stray_runs.append(bytes(stray))

    notif = len(chunks)
    sizes = Counter(len(b) for _, b in chunks)
    run_heads = Counter(r[:2].hex(" ") for r in stray_runs if len(r) >= 2)
    total_stray = sum(len(r) for r in stray_runs)

    print(f"\n--- Phase: {name} ---")
    print(f"  notifications: {notif}, payload sizes: {dict(sorted(sizes.items()))}")
    print(f"  IMU frames: {frames}, stray bytes: {total_stray}, stray runs: {len(stray_runs)}")
    print(f"  vendor char 6e400004: {g.hexdump(vendor_value) if vendor_value is not None else '?'}")
    if run_heads:
        print(f"  stray-run leading bytes (top): {dict(run_heads.most_common(8))}")
    longruns = [r for r in stray_runs if len(r) >= 3]
    if longruns:
        print(f"  notable stray runs (len>=3):")
        for r in longruns[:8]:
            print(f"      {g.hexdump(r)}")


async def main():
    device = await find()
    if device is None:
        print("Device not found.")
        return 1
    print(f"Connecting to {device.address}...", flush=True)

    chunks_by_phase = defaultdict(list)
    current_phase = "init"

    def handler(_sender, data: bytearray):
        chunks_by_phase[current_phase].append((time.perf_counter(), bytes(data)))

    async with BleakClient(device) as client:
        print("Connected. Subscribing + starting stream.\n", flush=True)
        await client.start_notify(NUS_TX_CHAR_UUID, handler)
        await client.write_gatt_char(NUS_RX_CHAR_UUID, DEFAULT_START_COMMAND, response=False)
        await asyncio.sleep(1.0)  # let the stream warm up

        async def read_vendor():
            try:
                return bytes(await client.read_gatt_char(VENDOR_CHAR))
            except Exception:
                return None

        phases = [
            ("STILL-1  (keep still, DON'T touch button)", 5),
            (">>> PRESS THE BUTTON repeatedly NOW <<<", 8),
            ("STILL-2  (keep still again)", 5),
        ]
        vendor_values = {}
        for label, secs in phases:
            current_phase = label
            vendor_values[label] = await read_vendor()
            print(f"\n{label}")
            await countdown("  capturing", secs)

        await client.stop_notify(NUS_TX_CHAR_UUID)

    print("\n================ ANALYSIS ================")
    for label, _ in phases:
        analyse_phase(label, chunks_by_phase[label], vendor_values[label])
    print("\nTip: compare the PRESS phase's stray-run leading bytes / notable runs")
    print("against the STILL phases. A button message is a pattern unique to PRESS.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
