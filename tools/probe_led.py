"""Confirm whether vendor characteristic 6e400004 controls the LED.

Safe: it only writes the boolean values the register already accepts (0/1) and
restores the original at the end. Watch the cap's LED and check it matches the
printed pattern:

    1) LED ON   for 3 s
    2) LED OFF  for 2 s
    3) five fast blinks
    4) restore original

Usage:  python tools\\probe_led.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bleak import BleakClient, BleakScanner

from trikiscope import gatt_names as g

VENDOR_CHAR = "6e400004-b5a3-f393-e0a9-e50e24dcca9e"


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


async def main():
    device = await find()
    if device is None:
        print("Device not found.")
        return 1

    async with BleakClient(device) as client:
        print(f"Connected to {device.address}.", flush=True)
        try:
            original = bytes(await client.read_gatt_char(VENDOR_CHAR))
        except Exception as exc:  # noqa: BLE001
            print(f"ABORT: cannot read original value: {exc}")
            return 1
        print(f"Original 6e400004 = {g.hexdump(original)}\n")

        async def write(byte):
            await client.write_gatt_char(VENDOR_CHAR, bytes([byte]), response=True)

        try:
            print(">>> LED should be ON now (3 s) <<<", flush=True)
            await write(0x01)
            await asyncio.sleep(3.0)

            print(">>> LED should be OFF now (2 s) <<<", flush=True)
            await write(0x00)
            await asyncio.sleep(2.0)

            print(">>> 5 fast blinks <<<", flush=True)
            for _ in range(5):
                await write(0x01)
                await asyncio.sleep(0.25)
                await write(0x00)
                await asyncio.sleep(0.25)
        finally:
            await client.write_gatt_char(VENDOR_CHAR, original, response=True)
            final = bytes(await client.read_gatt_char(VENDOR_CHAR))
            print(f"\nRestored 6e400004 = {g.hexdump(final)} (original {g.hexdump(original)})")

    print("\nDid the LED follow the pattern (3s on, 2s off, 5 blinks)?")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
