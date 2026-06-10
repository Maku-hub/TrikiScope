"""End-to-end test of the production BLE layer (trikiscope.ble.TrikiBleClient).

Runs the real client with printing callbacks for a few seconds, then stops.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trikiscope.ble import BleCallbacks, TrikiBleClient
from trikiscope.config import AppConfig
from trikiscope.protocol import BleNotificationProcessor


async def main():
    config = AppConfig(scan_timeout_seconds=25.0)
    processor = BleNotificationProcessor(
        config.gyro_scale, config.accel_scale, config.startup_discard_samples
    )
    counters = {"notif": 0, "samples": 0}

    def on_notification(data: bytes, ts: datetime):
        counters["notif"] += 1
        counters["samples"] += len(processor.process(data, ts))

    callbacks = BleCallbacks(
        on_log=lambda m: print(f"[log]   {m}"),
        on_state=lambda s: print(f"[state] {s}"),
        on_advertisement=lambda a: print(f"[adv]   {a.name} {a.address} rssi={a.rssi} mfr={a.manufacturer_data}"),
        on_device_info=lambda i: print(f"[info]  fw={i.firmware_revision} mtu={i.mtu} batt={i.battery_percent} name={i.device_name}"),
        on_gatt=lambda services: print(f"[gatt]  {len(services)} services, {sum(len(s.characteristics) for s in services)} chars"),
        on_battery=lambda b: print(f"[batt]  {b}%"),
        on_notification=on_notification,
    )

    client = TrikiBleClient(config, callbacks)
    task = asyncio.create_task(client.run())

    # Let it stream for a while, then stop cleanly.
    await asyncio.sleep(34.0)
    print("[probe] stopping...")
    client.stop()
    await task

    print(f"\n[probe] notifications={counters['notif']} decoded_samples={counters['samples']} "
          f"frames_parsed={processor.stats.parsed_frame_count} dropped_bytes={processor.stats.dropped_byte_count}")


if __name__ == "__main__":
    asyncio.run(main())
