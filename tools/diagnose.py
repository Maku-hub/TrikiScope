"""Live diagnostic probe for the Triki device.

Scans, connects, dumps the full GATT database with values, subscribes to EVERY
notify/indicate characteristic (not just NUS TX), writes the start command, and
records raw notifications for a few seconds so we can analyse framing, packet
sizes, sample rate and any non-IMU traffic (e.g. button events).

Run:  python tools/diagnose.py [--capture 10] [--name Triki]
"""

from __future__ import annotations

import argparse
import asyncio
import struct
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bleak import BleakClient, BleakScanner

from trikiscope import gatt_names as g
from trikiscope.config import NUS_RX_CHAR_UUID, NUS_TX_CHAR_UUID, DEFAULT_START_COMMAND

NUS_TX = NUS_TX_CHAR_UUID.lower()


def now() -> float:
    return time.perf_counter()


async def scan(name: str, timeout: float):
    print(f"== Scanning up to {timeout:.0f}s for a name containing '{name}' ==")
    print("   (if nothing shows, press the button on Triki to wake it)\n")
    fut = asyncio.get_event_loop().create_future()
    needle = name.lower()
    seen = {}

    def cb(device, adv):
        nm = adv.local_name or device.name
        if nm:
            seen[device.address] = (nm, getattr(adv, "rssi", None))
        if nm and needle in nm.lower() and not fut.done():
            fut.set_result((device, adv))

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        print("Scan timed out. Devices seen this scan:")
        for addr, (nm, rssi) in sorted(seen.items(), key=lambda x: -(x[1][1] or -999)):
            print(f"  {addr}  {rssi} dBm  {nm}")
        return None, None
    finally:
        await scanner.stop()


def dump_advertisement(device, adv):
    print("\n== Advertisement ==")
    print(f"  name        : {adv.local_name or device.name}")
    print(f"  address     : {device.address}")
    print(f"  rssi        : {getattr(adv, 'rssi', None)} dBm")
    print(f"  tx_power    : {getattr(adv, 'tx_power', None)}")
    print(f"  service_uuids:")
    for u in adv.service_uuids or []:
        print(f"      {g.describe_service(u)}")
    print(f"  manufacturer_data:")
    for cid, data in (adv.manufacturer_data or {}).items():
        print(f"      0x{cid:04X} {g.company_name(cid)}: {g.hexdump(bytes(data))}")
    print(f"  service_data:")
    for u, data in (adv.service_data or {}).items():
        print(f"      {g.describe_service(u)}: {g.hexdump(bytes(data))}")
    pd = getattr(adv, "platform_data", None)
    if pd:
        print(f"  platform_data: {pd}")


async def dump_gatt(client: BleakClient):
    print("\n== GATT database ==")
    notify_chars = []
    for service in client.services:
        print(f"  [S] {g.describe_service(service.uuid)}  handle=0x{service.handle:04X}")
        for char in service.characteristics:
            props = ",".join(char.properties)
            print(f"      [C] {g.describe_characteristic(char.uuid)}  [{props}]  handle=0x{char.handle:04X}")
            if "read" in char.properties:
                try:
                    raw = bytes(await client.read_gatt_char(char))
                    text = g.decode_text(raw)
                    extra = f'  text="{text}"' if text else ""
                    print(f"          value: {g.hexdump(raw)}{extra}")
                except Exception as exc:  # noqa: BLE001
                    print(f"          read error: {exc}")
            for d in char.descriptors:
                try:
                    draw = bytes(await client.read_gatt_descriptor(d.handle))
                    print(f"          [D] {g.describe_descriptor(d.uuid)} (0x{d.handle:04X}): {g.hexdump(draw)}")
                except Exception:  # noqa: BLE001
                    print(f"          [D] {g.describe_descriptor(d.uuid)} (0x{d.handle:04X})")
            if "notify" in char.properties or "indicate" in char.properties:
                notify_chars.append(char)
    return notify_chars


async def capture(client: BleakClient, notify_chars, capture_seconds: float):
    print(f"\n== Subscribing to {len(notify_chars)} notify/indicate characteristic(s) ==")
    records = defaultdict(list)  # uuid -> list of (t, bytes)
    t0 = now()

    def make_handler(uuid):
        def handler(_sender, data: bytearray):
            records[uuid].append((now() - t0, bytes(data)))
        return handler

    for char in notify_chars:
        try:
            await client.start_notify(char, make_handler(str(char.uuid).lower()))
            print(f"  subscribed {g.describe_characteristic(char.uuid)}")
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED to subscribe {char.uuid}: {exc}")

    # Send the start command on NUS RX.
    try:
        await client.write_gatt_char(NUS_RX_CHAR_UUID, DEFAULT_START_COMMAND, response=False)
        print(f"  wrote start command to NUS RX: {g.hexdump(DEFAULT_START_COMMAND)}")
    except Exception as exc:  # noqa: BLE001
        print(f"  start command write failed: {exc}")

    print(f"  capturing for {capture_seconds:.0f}s... move/rotate/press the Triki now!")
    await asyncio.sleep(capture_seconds)

    for char in notify_chars:
        try:
            await client.stop_notify(char)
        except Exception:  # noqa: BLE001
            pass
    return records, now() - t0


def analyse(records, elapsed):
    print("\n== Notification analysis ==")
    if not records:
        print("  No notifications received.")
        return
    for uuid, items in records.items():
        n = len(items)
        total_bytes = sum(len(b) for _, b in items)
        sizes = Counter(len(b) for _, b in items)
        first_bytes = Counter(b[:2].hex(" ") for _, b in items if b)
        rate = n / elapsed if elapsed > 0 else 0
        print(f"\n  characteristic {g.describe_characteristic(uuid)}")
        print(f"    notifications: {n}  ({rate:.1f}/s), total {total_bytes} bytes")
        print(f"    payload sizes: {dict(sorted(sizes.items()))}")
        print(f"    leading 2 bytes histogram: {dict(first_bytes)}")

        if uuid == NUS_TX:
            _analyse_imu_stream(items, elapsed)
        else:
            # Show a few raw samples for unknown notifying characteristics.
            print("    sample payloads:")
            for t, b in items[:6]:
                print(f"      t={t:6.2f}s  {g.hexdump(b)}")


def _analyse_imu_stream(items, elapsed):
    raw = bytearray()
    for _, b in items:
        raw.extend(b)
    # Walk the concatenated stream, splitting on the 0x22 0x00 header.
    headers = Counter()
    frames = 0
    i = 0
    dropped = 0
    distinct_headers_in_stream = Counter()
    # Scan every byte position for a header to characterise framing.
    pos = 0
    last_frame_starts = []
    while pos + 14 <= len(raw):
        if raw[pos] == 0x22 and raw[pos + 1] == 0x00:
            frames += 1
            last_frame_starts.append(pos)
            pos += 14
        else:
            dropped += 1
            pos += 1
    print(f"    reassembled stream: {len(raw)} bytes -> {frames} frames of 14B, "
          f"{dropped} stray bytes between frames")
    if frames:
        print(f"    effective IMU sample rate: {frames / elapsed:.1f} Hz")
    # Decode a few frames spread across the capture.
    sample_idx = [0, frames // 2, frames - 1] if frames >= 3 else list(range(frames))
    print("    decoded samples (gyro deg/s @131, accel g @2048):")
    for idx in sample_idx:
        if 0 <= idx < len(last_frame_starts):
            s = last_frame_starts[idx]
            gx, gy, gz, ax, ay, az = struct.unpack_from("<6h", raw, s + 2)
            print(f"      frame {idx:4d}: gyro=({gx/131:+7.2f},{gy/131:+7.2f},{gz/131:+7.2f})  "
                  f"accel=({ax/2048:+6.3f},{ay/2048:+6.3f},{az/2048:+6.3f})  raw={g.hexdump(bytes(raw[s:s+14]))}")
    # Per-notification framing (are frames aligned to notification boundaries?).
    misaligned = sum(1 for _, b in items if len(b) % 14 != 0 or (b[:2].hex() != "2200"))
    print(f"    notifications not starting on a 14B-aligned 22 00 frame: {misaligned}/{len(items)}")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="Triki")
    parser.add_argument("--scan-timeout", type=float, default=25.0)
    parser.add_argument("--capture", type=float, default=10.0)
    args = parser.parse_args()

    device, adv = await scan(args.name, args.scan_timeout)
    if device is None:
        return 1
    dump_advertisement(device, adv)

    print(f"\n== Connecting to {device.address} ==")
    disconnected = asyncio.Event()
    client = BleakClient(device, disconnected_callback=lambda _c: disconnected.set())
    await client.connect()
    print("  connected.")
    mtu = getattr(client, "mtu_size", None)
    print(f"  MTU: {mtu}")
    try:
        notify_chars = await dump_gatt(client)
        records, elapsed = await capture(client, notify_chars, args.capture)
        analyse(records, elapsed)
    finally:
        if client.is_connected:
            await client.disconnect()
        print("\n== Disconnected ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
