"""BLE layer for the Triki device, built on ``bleak``.

Responsibilities:

* scan for a device whose advertised name contains the configured substring,
* capture the advertisement (RSSI, manufacturer data, service UUIDs, tx power),
* connect and enumerate the full GATT database (services / characteristics /
  descriptors, reading every readable value),
* decode the standard Device Information + Battery + Generic Access values,
* subscribe to battery-level notifications when available,
* subscribe to the Nordic UART TX characteristic and write the start command,
* forward every raw notification (bytes + capture timestamp) to a callback.

The layer is UI-agnostic: it talks to the rest of the app through the plain
callables in :class:`BleCallbacks`.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from . import gatt_names as g
from .config import (
    AppConfig,
    LED_CHAR_UUID,
    NUS_RX_CHAR_UUID,
    NUS_SERVICE_UUID,
    NUS_TX_CHAR_UUID,
    STOP_COMMAND,
)


@dataclass
class AdvertisementInfo:
    name: Optional[str]
    address: str
    rssi: Optional[int]
    tx_power: Optional[int]
    service_uuids: List[str] = field(default_factory=list)
    manufacturer_data: dict[int, bytes] = field(default_factory=dict)
    service_data: dict[str, bytes] = field(default_factory=dict)


@dataclass
class DeviceInfo:
    device_name: Optional[str] = None
    appearance: Optional[str] = None
    battery_percent: Optional[int] = None
    manufacturer: Optional[str] = None
    model_number: Optional[str] = None
    serial_number: Optional[str] = None
    firmware_revision: Optional[str] = None
    hardware_revision: Optional[str] = None
    software_revision: Optional[str] = None
    system_id: Optional[str] = None
    pnp_id: Optional[str] = None
    mtu: Optional[int] = None


@dataclass
class GattDescriptorInfo:
    uuid: str
    handle: int
    description: str


@dataclass
class GattCharacteristicInfo:
    uuid: str
    handle: int
    description: str
    properties: List[str]
    value: Optional[bytes] = None
    read_error: Optional[str] = None
    descriptors: List[GattDescriptorInfo] = field(default_factory=list)


@dataclass
class GattServiceInfo:
    uuid: str
    handle: int
    description: str
    characteristics: List[GattCharacteristicInfo] = field(default_factory=list)


@dataclass
class BleCallbacks:
    on_log: Callable[[str], None]
    on_state: Callable[[str], None]
    on_advertisement: Callable[[AdvertisementInfo], None]
    on_device_info: Callable[[DeviceInfo], None]
    on_gatt: Callable[[List[GattServiceInfo]], None]
    on_battery: Callable[[int], None]
    on_notification: Callable[[bytes, datetime], None]
    on_led: Callable[[bool], None] = lambda _on: None
    """LED state as read from the device on connect, and after each successful write."""


# Standard SIG UUIDs we read during device-info collection.
_GENERIC_ACCESS = "00001800-0000-1000-8000-00805f9b34fb"
_DEVICE_NAME = "00002a00-0000-1000-8000-00805f9b34fb"
_APPEARANCE = "00002a01-0000-1000-8000-00805f9b34fb"
_BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"
_MANUFACTURER = "00002a29-0000-1000-8000-00805f9b34fb"
_MODEL = "00002a24-0000-1000-8000-00805f9b34fb"
_SERIAL = "00002a25-0000-1000-8000-00805f9b34fb"
_FW = "00002a26-0000-1000-8000-00805f9b34fb"
_HW = "00002a27-0000-1000-8000-00805f9b34fb"
_SW = "00002a28-0000-1000-8000-00805f9b34fb"
_SYSTEM_ID = "00002a23-0000-1000-8000-00805f9b34fb"
_PNP_ID = "00002a50-0000-1000-8000-00805f9b34fb"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _StopRequested(Exception):
    """Raised inside :meth:`TrikiBleClient.run` when :meth:`stop` interrupts a step."""


class TrikiBleClient:
    def __init__(self, config: AppConfig, callbacks: BleCallbacks) -> None:
        self._config = config
        self._cb = callbacks
        self._client: Optional[BleakClient] = None
        self._stop_event = asyncio.Event()
        self._char_values: dict[str, bytes] = {}
        self._stream_started = False

    # -- public API ------------------------------------------------------------

    async def run(self) -> None:
        """Find, connect and stream until :meth:`stop` (or disconnect)."""
        self._stop_event.clear()
        self._stream_started = False
        try:
            device, adv = await self._scan()
            if device is None:
                self._cb.on_log(
                    "No matching device found. Press the button on Triki to wake it, then retry."
                )
                return  # _cleanup() reports "disconnected"
            if adv is not None:
                self._emit_advertisement(device, adv)

            self._cb.on_state("connecting")
            self._cb.on_log(f"Connecting to {device.name or '?'} ({device.address})...")
            await self._connect(device)
        except _StopRequested:
            self._cb.on_log("Stopped.")
        except asyncio.CancelledError:
            self._cb.on_log("Cancelled.")
            raise
        except Exception as exc:  # noqa: BLE001 - surface any backend error to the UI
            self._cb.on_log(f"Error: {exc!r}")
            self._cb.on_state("error")
        finally:
            await self._cleanup()

    def stop(self) -> None:
        """Request shutdown; also interrupts an ongoing scan / connect / setup."""
        self._stop_event.set()

    async def set_led(self, on: bool) -> bool:
        """Turn the device LED on/off via the vendor characteristic (bit 0).

        Returns True on success; the confirmed state is reported via ``on_led``.
        """
        if self._client is None or not self._client.is_connected:
            self._cb.on_log("Cannot set LED: not connected.")
            return False
        try:
            await self._client.write_gatt_char(LED_CHAR_UUID, bytes([1 if on else 0]), response=True)
        except Exception as exc:  # noqa: BLE001
            self._cb.on_log(f"LED write failed: {exc}")
            return False
        self._cb.on_log(f"LED {'on' if on else 'off'}.")
        self._cb.on_led(on)
        return True

    async def _unless_stopped(self, awaitable):
        """Await ``awaitable`` but abandon it (raising _StopRequested) once stop() is called."""
        if self._stop_event.is_set():
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            raise _StopRequested
        task = asyncio.ensure_future(awaitable)
        stopper = asyncio.ensure_future(self._stop_event.wait())
        try:
            await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopper.cancel()
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        if task.cancelled():
            raise _StopRequested
        return task.result()

    # -- scanning --------------------------------------------------------------

    async def _scan(self) -> tuple[Optional[BLEDevice], Optional[AdvertisementData]]:
        self._cb.on_state("scanning")
        self._cb.on_log(
            f"Scanning up to {self._config.scan_timeout_seconds:.0f}s for a name containing "
            f"'{self._config.device_name}'..."
        )
        found: asyncio.Future = asyncio.get_running_loop().create_future()
        name_needle = self._config.device_name.lower()

        def detection(device: BLEDevice, adv: AdvertisementData) -> None:
            name = adv.local_name or device.name
            if name and name_needle in name.lower() and not found.done():
                found.set_result((device, adv))

        scanner = BleakScanner(detection_callback=detection)
        await scanner.start()
        try:
            result = await self._unless_stopped(
                asyncio.wait_for(found, timeout=self._config.scan_timeout_seconds)
            )
        except asyncio.TimeoutError:
            self._cb.on_log("Scan timed out.")
            result = (None, None)
        finally:
            await scanner.stop()
        return result

    def _emit_advertisement(self, device: BLEDevice, adv: AdvertisementData) -> None:
        info = AdvertisementInfo(
            name=adv.local_name or device.name,
            address=device.address,
            rssi=getattr(adv, "rssi", None),
            tx_power=getattr(adv, "tx_power", None),
            service_uuids=list(adv.service_uuids or []),
            manufacturer_data={k: bytes(v) for k, v in (adv.manufacturer_data or {}).items()},
            service_data={k: bytes(v) for k, v in (adv.service_data or {}).items()},
        )
        self._cb.on_advertisement(info)

    # -- connection ------------------------------------------------------------

    async def _connect(self, device: BLEDevice) -> None:
        def on_disconnect(_client: BleakClient) -> None:
            self._cb.on_log("Device disconnected.")
            self._stop_event.set()

        self._client = BleakClient(device, disconnected_callback=on_disconnect)
        await self._unless_stopped(self._client.connect())
        self._cb.on_state("connected")
        self._cb.on_log("Connected.")

        await self._unless_stopped(self._enumerate_gatt(None))
        self._emit_led_state()
        # Read MTU after some GATT traffic: WinRT often still reports the default
        # ATT MTU (23) immediately after connect and only updates once negotiated.
        mtu = getattr(self._client, "mtu_size", None)
        if mtu:
            self._cb.on_log(f"Negotiated MTU: {mtu}")
        await self._read_device_info(mtu)
        await self._subscribe_battery()
        await self._subscribe_stream()

        self._cb.on_log("Streaming. Press 'd' to disconnect.")
        await self._stop_event.wait()

    async def _enumerate_gatt(self, mtu: Optional[int]) -> None:
        assert self._client is not None
        services: List[GattServiceInfo] = []
        for service in self._client.services:
            svc = GattServiceInfo(
                uuid=str(service.uuid),
                handle=service.handle,
                description=g.describe_service(service.uuid),
            )
            for char in service.characteristics:
                props = list(char.properties)
                value: Optional[bytes] = None
                read_error: Optional[str] = None
                if "read" in props:
                    try:
                        raw = await self._client.read_gatt_char(char)
                        value = bytes(raw)
                        self._char_values[str(char.uuid).lower()] = value
                    except Exception as exc:  # noqa: BLE001
                        read_error = str(exc)
                descriptors = [
                    GattDescriptorInfo(
                        uuid=str(d.uuid),
                        handle=d.handle,
                        description=g.describe_descriptor(d.uuid),
                    )
                    for d in char.descriptors
                ]
                svc.characteristics.append(
                    GattCharacteristicInfo(
                        uuid=str(char.uuid),
                        handle=char.handle,
                        description=g.describe_characteristic(char.uuid),
                        properties=props,
                        value=value,
                        read_error=read_error,
                        descriptors=descriptors,
                    )
                )
            services.append(svc)
        char_count = sum(len(s.characteristics) for s in services)
        self._cb.on_log(f"GATT: {len(services)} services, {char_count} characteristics.")
        self._cb.on_gatt(services)

    async def _read_device_info(self, mtu: Optional[int]) -> None:
        def text(uuid: str) -> Optional[str]:
            data = self._char_values.get(uuid.lower())
            return g.decode_text(data) if data else None

        battery = self._char_values.get(_BATTERY_LEVEL.lower())
        system_id = self._char_values.get(_SYSTEM_ID.lower())
        pnp = self._char_values.get(_PNP_ID.lower())
        appearance = self._char_values.get(_APPEARANCE.lower())

        info = DeviceInfo(
            device_name=text(_DEVICE_NAME) or (self._client.address if self._client else None),
            appearance=g.decode_appearance(appearance) if appearance else None,
            battery_percent=g.decode_battery_level(battery) if battery else None,
            manufacturer=text(_MANUFACTURER),
            model_number=text(_MODEL),
            serial_number=text(_SERIAL),
            firmware_revision=text(_FW),
            hardware_revision=text(_HW),
            software_revision=text(_SW),
            system_id=g.decode_system_id(system_id) if system_id else None,
            pnp_id=g.decode_pnp_id(pnp) if pnp else None,
            mtu=mtu,
        )
        self._cb.on_device_info(info)

    async def _subscribe_battery(self) -> None:
        assert self._client is not None
        char = self._find_char(_BATTERY_LEVEL)
        if char is None or "notify" not in char.properties:
            return

        def handler(_sender, data: bytearray) -> None:
            level = g.decode_battery_level(bytes(data))
            if level is not None:
                self._cb.on_battery(level)

        try:
            await self._client.start_notify(char, handler)
            self._cb.on_log("Subscribed to battery-level notifications.")
        except Exception as exc:  # noqa: BLE001
            self._cb.on_log(f"Battery notify failed: {exc}")

    async def _subscribe_stream(self) -> None:
        assert self._client is not None
        tx = self._find_char(NUS_TX_CHAR_UUID)
        if tx is None:
            self._cb.on_log("NUS TX characteristic not found - no IMU stream available.")
            return

        def handler(_sender, data: bytearray) -> None:
            self._cb.on_notification(bytes(data), _utcnow())

        await self._client.start_notify(tx, handler)
        self._cb.on_log(f"Subscribed to NUS TX (handle 0x{tx.handle:04X}).")

        if self._config.auto_start_stream and self._config.start_command:
            if self._config.settle_delay_seconds > 0:
                self._cb.on_log(
                    f"Keep Triki still. Starting stream in {self._config.settle_delay_seconds:.0f}s..."
                )
                await self._unless_stopped(asyncio.sleep(self._config.settle_delay_seconds))
            rx = self._find_char(NUS_RX_CHAR_UUID)
            if rx is None:
                self._cb.on_log("NUS RX characteristic not found - cannot send start command.")
                return
            await self._client.write_gatt_char(rx, self._config.start_command, response=False)
            self._stream_started = True
            self._cb.on_log(f"Wrote start command: {g.hexdump(self._config.start_command)}")

    def _emit_led_state(self) -> None:
        value = self._char_values.get(LED_CHAR_UUID.lower())
        if value:
            self._cb.on_led(bool(value[0] & 0x01))

    def _find_char(self, uuid: str):
        if self._client is None:
            return None
        target = uuid.lower()
        for service in self._client.services:
            for char in service.characteristics:
                if str(char.uuid).lower() == target:
                    return char
        return None

    async def _send_stop(self) -> None:
        """Ask the device to stop streaming (as the Zappka app does) before disconnecting."""
        rx = self._find_char(NUS_RX_CHAR_UUID)
        if rx is None:
            return
        try:
            await asyncio.wait_for(
                self._client.write_gatt_char(rx, STOP_COMMAND, response=False), timeout=1.0
            )
            self._cb.on_log(f"Wrote stop command: {g.hexdump(STOP_COMMAND)}")
        except Exception as exc:  # noqa: BLE001 - best effort, we disconnect anyway
            self._cb.on_log(f"Stop command failed: {exc!r}")

    async def _cleanup(self) -> None:
        if self._client is not None:
            try:
                if self._client.is_connected:
                    if self._stream_started:
                        await self._send_stop()
                    await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
        self._cb.on_state("disconnected")


async def quick_scan(timeout: float = 8.0) -> List[AdvertisementInfo]:
    """Standalone helper: list every advertising device (used by --scan)."""
    results: dict[str, AdvertisementInfo] = {}
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for address, (device, adv) in devices.items():
        results[address] = AdvertisementInfo(
            name=adv.local_name or device.name,
            address=address,
            rssi=getattr(adv, "rssi", None),
            tx_power=getattr(adv, "tx_power", None),
            service_uuids=list(adv.service_uuids or []),
            manufacturer_data={k: bytes(v) for k, v in (adv.manufacturer_data or {}).items()},
            service_data={k: bytes(v) for k, v in (adv.service_data or {}).items()},
        )
    return list(results.values())
