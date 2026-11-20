"""BLE layer behaviour with bleak replaced by fakes (no radio needed)."""

import asyncio
from types import SimpleNamespace

from trikiscope import ble as ble_mod
from trikiscope.ble import BleCallbacks, TrikiBleClient
from trikiscope.config import (
    DEFAULT_START_COMMAND,
    LED_CHAR_UUID,
    NUS_RX_CHAR_UUID,
    NUS_SERVICE_UUID,
    NUS_TX_CHAR_UUID,
    STOP_COMMAND,
    AppConfig,
)

TRIKI_A = ("Triki 1733538611", "EC:07:A9:B8:C1:B7", -50)
TRIKI_B = ("Triki 1700000001", "C0:FF:EE:00:00:01", -70)


def config(**kw) -> AppConfig:
    """Fast test config: no extra scan window unless a test asks for one."""
    kw.setdefault("pick_window_seconds", 0.0)
    return AppConfig(**kw)


class Recorder:
    def __init__(self, choice=None):
        self.logs, self.states, self.led, self.offered = [], [], [], []
        self._choice = choice

    def callbacks(self, with_picker=False) -> BleCallbacks:
        async def choose(devices):
            self.offered.append([d.address for d in devices])
            return self._choice

        return BleCallbacks(
            on_log=self.logs.append,
            on_state=self.states.append,
            on_advertisement=lambda _a: None,
            on_device_info=lambda _i: None,
            on_gatt=lambda _g: None,
            on_battery=lambda _b: None,
            on_notification=lambda _d, _t: None,
            on_led=self.led.append,
            choose_device=choose if with_picker else None,
        )


def _adv(name, address, rssi):
    device = SimpleNamespace(name=name, address=address)
    adv = SimpleNamespace(local_name=name, rssi=rssi, tx_power=None,
                          service_uuids=[], manufacturer_data={}, service_data={})
    return device, adv


class SilentScanner:
    """Never finds anything."""

    devices = ()

    def __init__(self, detection_callback):
        self._cb = detection_callback

    async def start(self):
        for d in self.devices:
            self._cb(*_adv(*d))

    async def stop(self):
        pass


class FindingScanner(SilentScanner):
    devices = (TRIKI_A,)


class TwoTrikiScanner(SilentScanner):
    devices = (("Other thing", "11:22:33:44:55:66", -40), TRIKI_B, TRIKI_A)


def _char(uuid, props):
    return SimpleNamespace(uuid=uuid, handle=0x10, properties=props, descriptors=[])


class FakeClient:
    """Connects; exposes NUS RX/TX and the LED characteristic (value 01)."""

    hang_on_connect = False
    instances: list = []

    def __init__(self, device, disconnected_callback=None):
        self.is_connected = False
        self.address = device.address
        self.mtu_size = 247
        self.disconnected_callback = disconnected_callback
        self.services = [
            SimpleNamespace(uuid=NUS_SERVICE_UUID, handle=1, characteristics=[
                _char(NUS_RX_CHAR_UUID, ["write-without-response"]),
                _char(NUS_TX_CHAR_UUID, ["notify"]),
                _char(LED_CHAR_UUID, ["read", "write"]),
            ])
        ]
        self.writes = []
        self.events = []
        FakeClient.instances.append(self)

    async def start_notify(self, char, handler):
        pass

    async def connect(self):
        if self.hang_on_connect:
            await asyncio.sleep(3600)
        self.is_connected = True

    async def disconnect(self):
        self.events.append("disconnect")
        self.is_connected = False

    async def read_gatt_char(self, char):
        return bytearray(b"\x01")

    async def write_gatt_char(self, char, data, response=False):
        uuid = char if isinstance(char, str) else char.uuid
        self.writes.append((uuid, bytes(data)))
        self.events.append(("write", uuid, bytes(data)))

    def drop(self):
        """Simulate the device going out of range."""
        self.is_connected = False
        self.disconnected_callback(self)


def _install(monkeypatch, scanner=FindingScanner, client=FakeClient):
    FakeClient.instances = []
    monkeypatch.setattr(ble_mod, "BleakScanner", scanner)
    monkeypatch.setattr(ble_mod, "BleakClient", client)


async def _wait_for(predicate, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


async def _run_then_stop(client: TrikiBleClient, after: float = 0.05, limit: float = 2.0):
    task = asyncio.ensure_future(client.run())
    await asyncio.sleep(after)
    client.stop()
    await asyncio.wait_for(task, timeout=limit)


def test_stop_interrupts_scan(monkeypatch):
    _install(monkeypatch, scanner=SilentScanner)
    rec = Recorder()
    client = TrikiBleClient(config(scan_timeout_seconds=30.0), rec.callbacks())
    asyncio.run(_run_then_stop(client))  # would take 30 s without the fix
    assert rec.states[-1] == "disconnected"
    assert "connecting" not in rec.states
    assert any("Stopped" in m for m in rec.logs)


def test_stop_interrupts_connect(monkeypatch):
    class Hanging(FakeClient):
        hang_on_connect = True

    _install(monkeypatch, client=Hanging)
    rec = Recorder()
    client = TrikiBleClient(config(), rec.callbacks())
    asyncio.run(_run_then_stop(client))
    assert "connecting" in rec.states
    assert "connected" not in rec.states
    assert rec.states[-1] == "disconnected"


def test_led_state_is_read_on_connect_and_confirmed_on_write(monkeypatch):
    _install(monkeypatch)
    rec = Recorder()
    client = TrikiBleClient(config(), rec.callbacks())

    async def scenario():
        task = asyncio.ensure_future(client.run())
        await _wait_for(lambda: "connected" in rec.states and rec.led)
        assert rec.led == [True], "LED state read from the device was not reported"
        assert await client.set_led(False) is True
        assert rec.led == [True, False]
        client.stop()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(scenario())


def _connect_and_stop(monkeypatch, cfg: AppConfig) -> FakeClient:
    _install(monkeypatch)
    rec = Recorder()
    client = TrikiBleClient(cfg, rec.callbacks())

    async def scenario():
        task = asyncio.ensure_future(client.run())
        await _wait_for(lambda: any("Streaming" in m for m in rec.logs))
        client.stop()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(scenario())
    return FakeClient.instances[-1]


def test_stop_command_sent_before_disconnect(monkeypatch):
    fake = _connect_and_stop(monkeypatch, config())
    rx = NUS_RX_CHAR_UUID
    assert fake.events[-2:] == [("write", rx, STOP_COMMAND), "disconnect"]
    assert ("write", rx, DEFAULT_START_COMMAND) in fake.events


def test_no_stop_command_when_stream_was_never_started(monkeypatch):
    fake = _connect_and_stop(monkeypatch, config(auto_start_stream=False))
    assert all(e == "disconnect" or e[2] != STOP_COMMAND for e in fake.events)
    assert fake.events[-1] == "disconnect"


def test_failed_led_write_does_not_report_a_state():
    rec = Recorder()
    client = TrikiBleClient(config(), rec.callbacks())

    class Failing(FakeClient):
        async def write_gatt_char(self, char, data, response=False):
            raise RuntimeError("GATT error")

    fake = Failing(SimpleNamespace(address="x"))
    fake.is_connected = True
    client._client = fake
    assert asyncio.run(client.set_led(True)) is False
    assert rec.led == []
    assert any("failed" in m for m in rec.logs)


# -- auto-reconnect ------------------------------------------------------------


def _fast_backoff(monkeypatch):
    monkeypatch.setattr(TrikiBleClient, "RECONNECT_DELAYS", (0.01,))


def test_reconnects_to_the_same_device_after_a_drop(monkeypatch):
    _install(monkeypatch, scanner=TwoTrikiScanner)
    _fast_backoff(monkeypatch)
    rec = Recorder()
    client = TrikiBleClient(config(), rec.callbacks())  # no picker -> strongest (TRIKI_A)

    async def scenario():
        task = asyncio.ensure_future(client.run())
        await _wait_for(lambda: rec.states.count("connected") == 1 and any("Streaming" in m for m in rec.logs))
        FakeClient.instances[-1].drop()
        await _wait_for(lambda: rec.states.count("connected") == 2)
        client.stop()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(scenario())
    assert "reconnecting" in rec.states
    assert [c.address for c in FakeClient.instances] == [TRIKI_A[1], TRIKI_A[1]]
    assert rec.states[-1] == "disconnected"


def test_no_reconnect_when_disabled(monkeypatch):
    _install(monkeypatch)
    rec = Recorder()
    client = TrikiBleClient(config(auto_reconnect=False), rec.callbacks())

    async def scenario():
        task = asyncio.ensure_future(client.run())
        await _wait_for(lambda: any("Streaming" in m for m in rec.logs))
        FakeClient.instances[-1].drop()
        await asyncio.wait_for(task, timeout=2.0)  # ends on its own

    asyncio.run(scenario())
    assert "reconnecting" not in rec.states
    assert len(FakeClient.instances) == 1


def test_keeps_retrying_while_device_is_away_and_stop_cancels_backoff(monkeypatch):
    _install(monkeypatch)
    monkeypatch.setattr(TrikiBleClient, "RECONNECT_DELAYS", (0.01, 30.0))
    rec = Recorder()
    client = TrikiBleClient(config(scan_timeout_seconds=0.05), rec.callbacks())

    async def scenario():
        task = asyncio.ensure_future(client.run())
        await _wait_for(lambda: any("Streaming" in m for m in rec.logs))
        monkeypatch.setattr(ble_mod, "BleakScanner", SilentScanner)  # device gone
        FakeClient.instances[-1].drop()
        await _wait_for(lambda: any("attempt 2" in m for m in rec.logs))  # now in a 30 s back-off
        client.stop()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(scenario())
    assert any("Scan timed out" in m for m in rec.logs)
    assert rec.states[-1] == "disconnected"


# -- device choice ---------------------------------------------------------------


def test_picker_is_offered_all_matching_devices_by_signal(monkeypatch):
    _install(monkeypatch, scanner=TwoTrikiScanner)
    rec = Recorder(choice=TRIKI_B[1])
    client = TrikiBleClient(config(pick_window_seconds=0.01), rec.callbacks(with_picker=True))

    async def scenario():
        task = asyncio.ensure_future(client.run())
        await _wait_for(lambda: "connected" in rec.states)
        client.stop()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(scenario())
    assert rec.offered == [[TRIKI_A[1], TRIKI_B[1]]]  # strongest first, non-Triki filtered out
    assert FakeClient.instances[0].address == TRIKI_B[1]


def test_cancelled_pick_connects_to_nothing(monkeypatch):
    _install(monkeypatch, scanner=TwoTrikiScanner)
    rec = Recorder(choice=None)
    client = TrikiBleClient(config(pick_window_seconds=0.01), rec.callbacks(with_picker=True))
    asyncio.run(asyncio.wait_for(client.run(), timeout=2.0))
    assert FakeClient.instances == []
    assert any("No device selected" in m for m in rec.logs)


def test_address_filter_skips_name_match_and_picker(monkeypatch):
    _install(monkeypatch, scanner=TwoTrikiScanner)
    rec = Recorder(choice=TRIKI_A[1])
    cfg = config(device_address=TRIKI_B[1].lower(), pick_window_seconds=5.0)
    client = TrikiBleClient(cfg, rec.callbacks(with_picker=True))

    async def scenario():
        task = asyncio.ensure_future(client.run())
        await _wait_for(lambda: "connected" in rec.states, timeout=1.0)  # no 5 s pick window
        client.stop()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(scenario())
    assert rec.offered == []
    assert FakeClient.instances[0].address == TRIKI_B[1]
