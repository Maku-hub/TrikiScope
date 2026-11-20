"""BLE layer behaviour with bleak replaced by fakes (no radio needed)."""

import asyncio
from types import SimpleNamespace

import pytest

from trikiscope import ble as ble_mod
from trikiscope.ble import BleCallbacks, TrikiBleClient
from trikiscope.config import LED_CHAR_UUID, AppConfig


class Recorder:
    def __init__(self):
        self.logs, self.states, self.led = [], [], []

    def callbacks(self) -> BleCallbacks:
        return BleCallbacks(
            on_log=self.logs.append,
            on_state=self.states.append,
            on_advertisement=lambda _a: None,
            on_device_info=lambda _i: None,
            on_gatt=lambda _g: None,
            on_battery=lambda _b: None,
            on_notification=lambda _d, _t: None,
            on_led=self.led.append,
        )


class SilentScanner:
    """Never finds anything."""

    def __init__(self, detection_callback):
        self.stopped = False

    async def start(self):
        pass

    async def stop(self):
        self.stopped = True


class FindingScanner(SilentScanner):
    """Reports a Triki immediately."""

    def __init__(self, detection_callback):
        super().__init__(detection_callback)
        self._cb = detection_callback

    async def start(self):
        device = SimpleNamespace(name="Triki 1", address="EC:07:A9:B8:C1:B7")
        adv = SimpleNamespace(
            local_name="Triki 1", rssi=-50, tx_power=None,
            service_uuids=[], manufacturer_data={}, service_data={},
        )
        self._cb(device, adv)


def _char(uuid, props):
    return SimpleNamespace(uuid=uuid, handle=0x10, properties=props, descriptors=[])


class FakeClient:
    """Connects; exposes only the LED characteristic (value 01)."""

    hang_on_connect = False

    def __init__(self, device, disconnected_callback=None):
        self.is_connected = False
        self.address = device.address
        self.mtu_size = 247
        self.services = [
            SimpleNamespace(uuid="6e400001-b5a3-f393-e0a9-e50e24dcca9e", handle=1,
                            characteristics=[_char(LED_CHAR_UUID, ["read", "write"])])
        ]
        self.writes = []

    async def connect(self):
        if self.hang_on_connect:
            await asyncio.sleep(3600)
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def read_gatt_char(self, char):
        return bytearray(b"\x01")

    async def write_gatt_char(self, char, data, response=False):
        self.writes.append((char, bytes(data)))


async def _run_then_stop(client: TrikiBleClient, after: float = 0.05, limit: float = 2.0):
    task = asyncio.ensure_future(client.run())
    await asyncio.sleep(after)
    client.stop()
    await asyncio.wait_for(task, timeout=limit)


def test_stop_interrupts_scan(monkeypatch):
    monkeypatch.setattr(ble_mod, "BleakScanner", SilentScanner)
    rec = Recorder()
    client = TrikiBleClient(AppConfig(scan_timeout_seconds=30.0), rec.callbacks())
    asyncio.run(_run_then_stop(client))  # would take 30 s without the fix
    assert rec.states[-1] == "disconnected"
    assert "connecting" not in rec.states
    assert any("Stopped" in m for m in rec.logs)


def test_stop_interrupts_connect(monkeypatch):
    monkeypatch.setattr(ble_mod, "BleakScanner", FindingScanner)

    class Hanging(FakeClient):
        hang_on_connect = True

    monkeypatch.setattr(ble_mod, "BleakClient", Hanging)
    rec = Recorder()
    client = TrikiBleClient(AppConfig(), rec.callbacks())
    asyncio.run(_run_then_stop(client))
    assert "connecting" in rec.states
    assert "connected" not in rec.states
    assert rec.states[-1] == "disconnected"


def test_led_state_is_read_on_connect_and_confirmed_on_write(monkeypatch):
    monkeypatch.setattr(ble_mod, "BleakScanner", FindingScanner)
    monkeypatch.setattr(ble_mod, "BleakClient", FakeClient)
    rec = Recorder()
    client = TrikiBleClient(AppConfig(), rec.callbacks())

    async def scenario():
        task = asyncio.ensure_future(client.run())
        for _ in range(100):
            await asyncio.sleep(0.01)
            if "connected" in rec.states and rec.led:
                break
        assert rec.led == [True], "LED state read from the device was not reported"
        assert await client.set_led(False) is True
        assert rec.led == [True, False]
        client.stop()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(scenario())


def test_failed_led_write_does_not_report_a_state(monkeypatch):
    rec = Recorder()
    client = TrikiBleClient(AppConfig(), rec.callbacks())

    class Failing(FakeClient):
        async def write_gatt_char(self, char, data, response=False):
            raise RuntimeError("GATT error")

    fake = Failing(SimpleNamespace(address="x"))
    fake.is_connected = True
    client._client = fake
    assert asyncio.run(client.set_led(True)) is False
    assert rec.led == []
    assert any("failed" in m for m in rec.logs)
