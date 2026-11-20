"""Recording must never overwrite an earlier recording."""

import csv
from datetime import datetime, timezone

from trikiscope.protocol import ImuSample
from trikiscope.recorder import CSV_HEADER, Recorder


def _sample(i: int) -> ImuSample:
    frame = bytes([0x22, 0x00]) + bytes(12)
    return ImuSample.from_frame(frame, i, 131.0, 2048.0, datetime.now(timezone.utc))


def _rows(path) -> list:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


def test_second_recording_goes_to_a_new_file(tmp_path):
    rec = Recorder(str(tmp_path / "data.csv"), str(tmp_path / "events.log"))

    rec.start()
    first_path = rec.csv_path
    rec.write_sample(_sample(0))
    rec.write_sample(_sample(1))
    rec.stop()

    rec.start()
    second_path = rec.csv_path
    rec.write_sample(_sample(0))
    rec.stop()

    assert first_path == str(tmp_path / "data.csv")
    assert second_path != first_path
    assert len(_rows(first_path)) == 3, "first recording was overwritten"
    assert _rows(second_path)[0] == CSV_HEADER
    assert len(_rows(second_path)) == 2


def test_preexisting_file_is_kept(tmp_path):
    existing = tmp_path / "data.csv"
    existing.write_text("keep me\n", encoding="utf-8")
    rec = Recorder(str(existing), str(tmp_path / "events.log"))
    rec.start()
    rec.stop()
    assert existing.read_text(encoding="utf-8") == "keep me\n"
    assert rec.csv_path != str(existing)
    assert rec.csv_path.endswith(".csv")
