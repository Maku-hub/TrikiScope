"""Tests for frame parsing and IMU decoding."""

from datetime import datetime, timezone

from trikiscope.protocol import (
    BleNotificationProcessor,
    FrameParser,
    ImuSample,
    ImuSampleProcessor,
)


def make_frame(gx, gy, gz, ax, ay, az, status=0):
    import struct

    return bytes([0x22, status]) + struct.pack("<6h", gx, gy, gz, ax, ay, az)


def test_frame_is_14_bytes():
    assert len(make_frame(1, 2, 3, 4, 5, 6)) == 14


def test_parser_extracts_single_frame():
    parser = FrameParser()
    frame = make_frame(100, -200, 300, -400, 500, -600)
    frames = list(parser.push(frame))
    assert len(frames) == 1
    assert frames[0] == frame
    assert parser.dropped_byte_count == 0


def test_parser_resyncs_after_garbage():
    parser = FrameParser()
    frame = make_frame(1, 2, 3, 4, 5, 6)
    garbage = b"\xde\xad\xbe\xef"
    frames = list(parser.push(garbage + frame))
    assert len(frames) == 1
    assert parser.dropped_byte_count == len(garbage)


def test_parser_handles_split_across_chunks():
    parser = FrameParser()
    frame = make_frame(7, 8, 9, 10, 11, 12)
    first = list(parser.push(frame[:6]))
    assert first == []
    second = list(parser.push(frame[6:]))
    assert len(second) == 1
    assert second[0] == frame


def test_parser_keeps_trailing_header_byte():
    parser = FrameParser()
    # A lone 0x22 at the end may be the first byte of a header split across chunks.
    list(parser.push(b"\x01\x02\x22"))
    frame = make_frame(1, 1, 1, 1, 1, 1)
    # Supply the rest: 0x00 + remaining 12 bytes.
    frames = list(parser.push(b"\x00" + frame[2:]))
    assert len(frames) == 1


def test_parser_extracts_multiple_frames_in_burst():
    parser = FrameParser()
    f1 = make_frame(1, 2, 3, 4, 5, 6)
    f2 = make_frame(7, 8, 9, 10, 11, 12)
    frames = list(parser.push(f1 + f2))
    assert frames == [f1, f2]


def test_sample_scaling():
    frame = make_frame(262, -131, 0, 2048, -1024, 4096)
    sample = ImuSample.from_frame(frame, 0, gyro_scale=131.0, accel_scale=2048.0)
    assert sample.gyro_x == 2.0
    assert sample.gyro_y == -1.0
    assert sample.gyro_z == 0.0
    assert sample.accel_x == 1.0
    assert sample.accel_y == -0.5
    assert sample.accel_z == 2.0
    assert sample.raw_gyro_x == 262


def test_processor_discards_startup_samples():
    proc = ImuSampleProcessor(gyro_scale=131.0, accel_scale=2048.0, startup_discard_samples=3)
    frame = make_frame(1, 1, 1, 1, 1, 1)
    assert proc.process_frame(frame) is None
    assert proc.process_frame(frame) is None
    assert proc.process_frame(frame) is None
    fourth = proc.process_frame(frame)
    assert fourth is not None
    assert fourth.frame_index == 0
    assert proc.stats.written_sample_count == 1
    assert proc.stats.discarded_startup_sample_count == 3


def test_notification_processor_end_to_end():
    proc = BleNotificationProcessor(gyro_scale=131.0, accel_scale=2048.0, startup_discard_samples=0)
    ts = datetime.now(timezone.utc)
    f1 = make_frame(1, 2, 3, 4, 5, 6)
    f2 = make_frame(7, 8, 9, 10, 11, 12)
    samples = proc.process(f1 + f2, ts)
    assert len(samples) == 2
    assert proc.stats.notification_count == 1
    assert proc.stats.parsed_frame_count == 2


def test_button_frame_is_parsed_and_flagged():
    parser = FrameParser()
    released = make_frame(1, 2, 3, 4, 5, 6, status=0)
    pressed = make_frame(1, 2, 3, 4, 5, 6, status=1)
    frames = list(parser.push(released + pressed))
    assert len(frames) == 2, "button (22 01) frame must not be dropped"
    s_released = ImuSample.from_frame(frames[0], 0, 131.0, 2048.0)
    s_pressed = ImuSample.from_frame(frames[1], 1, 131.0, 2048.0)
    assert s_released.button_pressed is False
    assert s_pressed.button_pressed is True
    assert s_pressed.status == 1
    # Payload decodes identically regardless of the status byte.
    assert s_pressed.raw_gyro_x == s_released.raw_gyro_x


def test_parser_ignores_non_status_second_byte():
    # 0x22 followed by something other than 0x00/0x01 is not a frame header.
    parser = FrameParser()
    frame = make_frame(1, 1, 1, 1, 1, 1)
    frames = list(parser.push(b"\x22\x55\x99" + frame))
    assert len(frames) == 1
    assert frames[0] == frame


def test_notification_gap_tracking():
    proc = BleNotificationProcessor(131.0, 2048.0, 0)
    t0 = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2024, 1, 1, 0, 0, 0, 50000, tzinfo=timezone.utc)  # +50 ms
    proc.process(make_frame(0, 0, 0, 0, 0, 0), t0)
    proc.process(make_frame(0, 0, 0, 0, 0, 0), t1)
    assert abs(proc.stats.last_notification_gap_ms - 50.0) < 1e-6
    assert abs(proc.stats.max_notification_gap_ms - 50.0) < 1e-6
