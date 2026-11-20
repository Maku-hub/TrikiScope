"""Raw recording -> replay round trip, CSV replay and the CLI wiring."""

import asyncio
import json
import struct
from datetime import datetime, timedelta, timezone

import pytest

from trikiscope.__main__ import _build_parser, _config_from_args, _start_command_from_args
from trikiscope.ble import BleCallbacks
from trikiscope.config import (
    DEFAULT_START_COMMAND,
    build_start_command,
    gyro_lsb_per_dps,
    parse_start_command,
)
from trikiscope.protocol import BleNotificationProcessor
from trikiscope.recorder import Recorder
from trikiscope.replay import RecordingError, ReplayClient, load_recording, recording_start_command

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def frame(gz, status=0):
    return bytes([0x22, status]) + struct.pack("<6h", 0, 0, gz, 0, 0, 2048)


def notifications(n_frames=60, chunk=20):
    stream = b"".join(frame(i * 10, status=1 if 30 <= i < 35 else 0) for i in range(n_frames))
    return [stream[i:i + chunk] for i in range(0, len(stream) - chunk + 1, chunk)]


def record_raw(tmp_path, chunks, meta=None):
    rec = Recorder(str(tmp_path / "d.csv"), str(tmp_path / "e.log"), str(tmp_path / "raw.jsonl"))
    rec.start(meta or {"start_command": DEFAULT_START_COMMAND.hex()})
    for i, data in enumerate(chunks):
        rec.write_raw(data, T0 + timedelta(milliseconds=13 * i))
    rec.stop()
    return rec.raw_path


def callbacks(sink, states, logs):
    return BleCallbacks(
        on_log=logs.append,
        on_state=states.append,
        on_advertisement=lambda _a: None,
        on_device_info=lambda _i: None,
        on_gatt=lambda _g: None,
        on_battery=lambda _b: None,
        on_notification=lambda data, ts: sink.append((data, ts)),
    )


def test_raw_recording_round_trip(tmp_path):
    chunks = notifications()
    path = record_raw(tmp_path, chunks)
    rec = load_recording(path)
    assert rec.kind == "raw"
    assert [d for _t, d in rec.records] == chunks
    assert rec.records[1][0] - rec.records[0][0] == pytest.approx(0.013)
    assert recording_start_command(rec) == DEFAULT_START_COMMAND


def test_replay_feeds_identical_bytes_with_original_spacing(tmp_path):
    chunks = notifications(n_frames=30)
    rec = load_recording(record_raw(tmp_path, chunks))
    got, states, logs = [], [], []
    asyncio.run(ReplayClient(rec, callbacks(got, states, logs)).run())
    assert [d for d, _t in got] == chunks
    gaps = [(b - a).total_seconds() for (_, a), (_, b) in zip(got, got[1:])]
    assert all(g == pytest.approx(0.013, abs=1e-6) for g in gaps)
    assert states == ["connecting", "connected", "disconnected"]
    assert any("finished" in m for m in logs)


def test_replay_decodes_like_a_live_session(tmp_path):
    chunks = notifications()
    live = BleNotificationProcessor(14.29, 2048.0, 0)
    live_samples = [s for i, c in enumerate(chunks) for s in live.process(c, T0 + timedelta(milliseconds=13 * i))]

    got, states, logs = [], [], []
    asyncio.run(ReplayClient(load_recording(record_raw(tmp_path, chunks)), callbacks(got, states, logs)).run())
    replay = BleNotificationProcessor(14.29, 2048.0, 0)
    replay_samples = [s for data, ts in got for s in replay.process(data, ts)]

    key = lambda s: (s.raw_gyro_z, s.status)  # noqa: E731
    assert [key(s) for s in replay_samples] == [key(s) for s in live_samples]
    assert sum(s.button_pressed for s in replay_samples) == 5


def test_replay_stop_interrupts(tmp_path):
    rec = load_recording(record_raw(tmp_path, notifications()))
    rec.records = [(t * 100, d) for t, d in rec.records]  # stretch to minutes
    got, states, logs = [], [], []
    client = ReplayClient(rec, callbacks(got, states, logs), loop=True)

    async def scenario():
        task = asyncio.ensure_future(client.run())
        await asyncio.sleep(0.05)
        client.stop()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(scenario())
    assert len(got) == 1
    assert states[-1] == "disconnected"


def test_csv_recording_is_replayed_as_frames(tmp_path):
    rec = Recorder(str(tmp_path / "s.csv"), str(tmp_path / "e.log"))
    rec.start()
    proc = BleNotificationProcessor(14.29, 2048.0, 0)
    original = proc.process(b"".join(frame(i, status=i % 2) for i in range(10)), T0)
    for s in original:
        rec.write_sample(s)
    rec.stop()

    loaded = load_recording(rec.csv_path)
    assert loaded.kind == "csv"
    replay = BleNotificationProcessor(14.29, 2048.0, 0)
    samples = [s for t, d in loaded.records for s in replay.process(d, datetime.fromtimestamp(t, timezone.utc))]
    assert [(s.raw_gyro_z, s.status) for s in samples] == [(s.raw_gyro_z, s.status) for s in original]


def test_garbage_file_is_rejected(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"format": "trikiscope-raw"}\n{"nope": 1}\n', encoding="utf-8")
    with pytest.raises(RecordingError):
        load_recording(bad)
    empty = tmp_path / "empty.csv"
    empty.write_text("a,b\n", encoding="utf-8")
    with pytest.raises(RecordingError):
        load_recording(empty)


# -- start command / CLI -----------------------------------------------------------


def test_start_command_builder_matches_known_commands():
    assert build_start_command() == bytes.fromhex("201000D007680003")
    assert build_start_command(16, 2000, 52) == bytes.fromhex("201000D007340003")  # Zappka
    assert parse_start_command(bytes.fromhex("201000D007340003")) == (16, 2000, 52)
    assert parse_start_command(b"\x20\x00") is None


def _cli(*argv):
    parser = _build_parser()
    args = parser.parse_args(list(argv))
    return _config_from_args(args, _start_command_from_args(parser, args))


def test_cli_defaults():
    cfg = _cli()
    assert cfg.start_command == DEFAULT_START_COMMAND
    assert cfg.gyro_scale == pytest.approx(1000 / 70)
    assert cfg.accel_scale == 2048.0
    assert cfg.auto_reconnect is True


def test_cli_ranges_set_command_and_scales(capsys):
    cfg = _cli("--odr", "52", "--gyro-range", "500", "--accel-range", "8")
    assert parse_start_command(cfg.start_command) == (8, 500, 52)
    assert cfg.gyro_scale == pytest.approx(gyro_lsb_per_dps(500))
    assert cfg.accel_scale == 4096.0
    assert "nie były sprawdzone" in capsys.readouterr().err


def test_cli_verified_settings_do_not_warn(capsys):
    _cli("--odr", "52")
    assert capsys.readouterr().err == ""


def test_cli_explicit_scale_wins():
    assert _cli("--gyro-range", "500", "--gyro-scale", "50").gyro_scale == 50.0


def test_cli_start_command_conflicts_with_ranges():
    with pytest.raises(SystemExit):
        _cli("--start-command", "201000D007680003", "--odr", "52")


def test_cli_address_and_no_reconnect():
    cfg = _cli("--address", "EC:07:A9:B8:C1:B7", "--no-reconnect")
    assert cfg.device_address == "EC:07:A9:B8:C1:B7"
    assert cfg.auto_reconnect is False


# -- full app on a replay -----------------------------------------------------------


def test_app_runs_a_replay_end_to_end(tmp_path):
    from trikiscope.app import TrikiApp
    from trikiscope.config import AppConfig

    path = record_raw(tmp_path, notifications(n_frames=80))
    config = AppConfig(auto_connect=True, replay_path=path, startup_discard_samples=5,
                       csv_path=str(tmp_path / "out.csv"), raw_path=str(tmp_path / "out.jsonl"),
                       log_path=str(tmp_path / "out.log"))

    async def scenario():
        app = TrikiApp(config, recording=load_recording(path))
        async with app.run_test() as pilot:
            for _ in range(200):
                await pilot.pause(0.02)
                if app.state.conn_state == "disconnected" and app.state.last_sample is not None:
                    break
            assert app.state.last_sample is not None, "replay produced no samples"
            assert app._button_press_count == 1
            assert app.title == "TrikiScope (replay)"
            await pilot.press("4")
            app._tick()

    asyncio.run(scenario())
