"""The device picker modal inside the real (headless) app."""

import asyncio

from trikiscope.app import DevicePicker, TrikiApp
from trikiscope.ble import AdvertisementInfo
from trikiscope.config import AppConfig

DEVICES = [
    AdvertisementInfo(name="Triki 1", address="EC:07:A9:B8:C1:B7", rssi=-50, tx_power=None),
    AdvertisementInfo(name="Triki 2", address="C0:FF:EE:00:00:01", rssi=-70, tx_power=None),
]


def _pick(*keys):
    async def scenario():
        app = TrikiApp(AppConfig())
        async with app.run_test() as pilot:
            task = asyncio.ensure_future(app._choose_device(DEVICES))
            await pilot.pause()
            assert isinstance(app.screen, DevicePicker)
            for key in keys:
                await pilot.press(key)
            await pilot.pause()
            result = await asyncio.wait_for(task, timeout=2.0)
            assert not isinstance(app.screen, DevicePicker)
            return result

    return asyncio.run(scenario())


def test_picker_returns_highlighted_device():
    assert _pick("down", "enter") == "C0:FF:EE:00:00:01"


def test_picker_escape_cancels():
    assert _pick("escape") is None
