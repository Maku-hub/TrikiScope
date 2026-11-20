"""Session events that must end up in the recording's event log."""

from trikiscope.app import TrikiApp
from trikiscope.config import AppConfig


def test_led_changes_are_logged_while_recording(tmp_path):
    log = tmp_path / "events.log"
    app = TrikiApp(AppConfig(csv_path=str(tmp_path / "d.csv"), raw_path=str(tmp_path / "r.jsonl"),
                             log_path=str(log)))
    app._on_led(True)  # not recording yet -> not logged
    app.recorder.start()
    app._on_led(True)
    app._on_led(False)
    app.recorder.stop()

    messages = [line.split(" ", 1)[1] for line in log.read_text(encoding="utf-8").splitlines()]
    assert [m for m in messages if m.startswith("led ")] == ["led on", "led off"]
