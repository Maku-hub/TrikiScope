"""Headless smoke test of the Textual app (no BLE device required)."""

import asyncio
import struct
from datetime import datetime, timezone

from trikiscope.app import TrikiApp
from trikiscope.ble import AdvertisementInfo, DeviceInfo, GattCharacteristicInfo, GattServiceInfo
from trikiscope.config import AppConfig
from trikiscope.protocol import BleNotificationProcessor


def make_frame(gx, gy, gz, ax, ay, az, status=0):
    return bytes([0x22, status]) + struct.pack("<6h", gx, gy, gz, ax, ay, az)


async def main():
    app = TrikiApp(AppConfig())
    async with app.run_test() as pilot:
        # Simulate BLE events arriving.
        app.processor = BleNotificationProcessor(131.0, 2048.0, 0)
        app._log("smoke: hello log")  # exercise the queued-log -> RichLog path
        app._on_state("connected")
        app._on_advertisement(
            AdvertisementInfo(
                name="Triki_ABCD",
                address="AA:BB:CC:DD:EE:FF",
                rssi=-54,
                tx_power=0,
                service_uuids=["6e400001-b5a3-f393-e0a9-e50e24dcca9e"],
                manufacturer_data={0x0059: b"\x01\x02\x03"},
                service_data={},
            )
        )
        app._on_device_info(
            DeviceInfo(
                device_name="Triki_ABCD",
                manufacturer="Nordic",
                model_number="TRIKI-1",
                firmware_revision="1.0.0",
                battery_percent=87,
                mtu=247,
            )
        )
        app._on_gatt(
            [
                GattServiceInfo(
                    uuid="6e400001-b5a3-f393-e0a9-e50e24dcca9e",
                    handle=0x0010,
                    description="Nordic UART Service",
                    characteristics=[
                        GattCharacteristicInfo(
                            uuid="6e400003-b5a3-f393-e0a9-e50e24dcca9e",
                            handle=0x0013,
                            description="NUS TX (notify)",
                            properties=["notify"],
                        )
                    ],
                )
            ]
        )
        app._on_battery(86)

        # Feed a burst of frames so IMU + orientation populate.
        now = datetime.now(timezone.utc)
        for i in range(60):
            data = make_frame(100 + i, -50, 25, 0, 0, 2048)
            app._on_notification(data, now)

        # Cycle through every tab using the keyboard shortcuts (1-6).
        for key in ("1", "2", "3", "4", "5", "6"):
            await pilot.press(key)
            app._tick()
            await pilot.pause()

        # Exercise the Games tab: switch games and render each one.
        await pilot.press("5")  # Games tab
        for _ in range(len(app.games)):
            app._tick()
            await pilot.press("right_square_bracket")  # next game
            await pilot.pause()
        await pilot.press("g")  # restart current game
        app._tick()
        assert app._current_game() is not None
        await pilot.press("6")  # back to the Log tab
        await pilot.pause()

        # Trigger a gesture (hard accel spike ~3.5 g) with a fresh timestamp,
        # then render immediately so it is still within the hold window.
        app._on_notification(make_frame(0, 0, 0, 0, 0, int(3.5 * 2048)), datetime.now(timezone.utc))
        app._tick()
        assert app._gesture_label is not None, "gesture was not detected/held"

        # Simulate a button press (header 22 01) then release (22 00).
        app._on_notification(make_frame(0, 0, 0, 0, 0, 2048, status=1), datetime.now(timezone.utc))
        assert app._button_pressed is True, "button-pressed frame not recognised"
        app._on_notification(make_frame(0, 0, 0, 0, 0, 2048, status=0), datetime.now(timezone.utc))
        assert app._button_pressed is False
        assert app._button_press_count == 1
        app._tick()  # drain the button log lines into the RichLog

        # LED toggle must be a no-op (not a crash) when not connected via real BLE.
        app._ble = None
        app.action_toggle_led()
        assert app._led_on is False, "LED should not toggle without a connection"
        app._tick()  # drain the "LED: connect first" log line

        assert app.state.last_sample is not None, "no IMU sample decoded"
        assert app.state.orientation is not None, "no orientation produced"
        assert app.state.device_info is not None
        assert app.state.gatt, "no GATT data"
        assert not app._pending_logs, "log queue was not drained into RichLog"
        assert app.query_one("TabbedContent").active == "log", "tab shortcut did not switch tabs"
        print("SMOKE OK: sample, orientation, gesture, tab shortcuts, device info, GATT and log all rendered.")


if __name__ == "__main__":
    asyncio.run(main())
