"""Tests for the quaternion math and orientation filters."""

import math
from datetime import datetime, timedelta, timezone

from trikiscope.orientation import (
    ComplementaryTiltOrientationMapper,
    MadgwickAHRS,
    Quaternion,
    VisualOrientationMapper,
    create_orientation_mapper,
)
from trikiscope.protocol import ImuSample


def make_sample(gx, gy, gz, ax, ay, az, t):
    return ImuSample(
        frame_index=0,
        timestamp_utc=t,
        gyro_x=gx,
        gyro_y=gy,
        gyro_z=gz,
        accel_x=ax,
        accel_y=ay,
        accel_z=az,
        raw_gyro_x=0,
        raw_gyro_y=0,
        raw_gyro_z=0,
        raw_accel_x=0,
        raw_accel_y=0,
        raw_accel_z=0,
    )


def test_quaternion_identity_multiply():
    q = Quaternion(0.1, 0.2, 0.3, 0.9).normalized()
    ident = Quaternion.identity()
    r = ident * q
    assert math.isclose(r.x, q.x, abs_tol=1e-9)
    assert math.isclose(r.w, q.w, abs_tol=1e-9)


def test_quaternion_inverse():
    q = Quaternion(0.1, 0.2, 0.3, 0.9).normalized()
    r = q * q.inverse()
    assert math.isclose(r.x, 0.0, abs_tol=1e-9)
    assert math.isclose(r.y, 0.0, abs_tol=1e-9)
    assert math.isclose(r.z, 0.0, abs_tol=1e-9)
    assert math.isclose(abs(r.w), 1.0, abs_tol=1e-9)


def test_axis_angle_90_about_z_rotates_x_to_y():
    q = Quaternion.from_axis_angle(0, 0, 1, 90.0)
    x, y, z = q.rotate_vector(1, 0, 0)
    assert math.isclose(x, 0.0, abs_tol=1e-9)
    assert math.isclose(y, 1.0, abs_tol=1e-9)
    assert math.isclose(z, 0.0, abs_tol=1e-9)


def test_euler_from_identity_is_zero():
    pitch, roll, yaw = Quaternion.identity().to_euler_degrees()
    assert math.isclose(pitch, 0.0, abs_tol=1e-9)
    assert math.isclose(roll, 0.0, abs_tol=1e-9)
    assert math.isclose(yaw, 0.0, abs_tol=1e-9)


def test_slerp_endpoints():
    a = Quaternion.identity()
    b = Quaternion.from_axis_angle(0, 0, 1, 90.0)
    assert math.isclose(Quaternion.slerp(a, b, 0.0).w, a.w, abs_tol=1e-6)
    end = Quaternion.slerp(a, b, 1.0)
    assert math.isclose(abs(end.w), abs(b.w), abs_tol=1e-6)


def test_madgwick_converges_to_gravity_down():
    # With zero gyro and gravity on -Z, the filter should stay well-defined and unit norm.
    ahrs = MadgwickAHRS(beta=1.5)
    for _ in range(500):
        ahrs.update(0.0, 0.0, 0.0, 0.0, 0.0, -1.0, dt=0.01)
    norm = math.sqrt(sum(c * c for c in ahrs.q))
    assert math.isclose(norm, 1.0, abs_tol=1e-6)


def test_madgwick_ignores_zero_accel_and_nonpositive_dt():
    ahrs = MadgwickAHRS()
    before = list(ahrs.q)
    ahrs.update(1.0, 1.0, 1.0, 0.0, 0.0, 0.0, dt=0.01)  # zero accel -> ignored
    assert ahrs.q == before
    ahrs.update(1.0, 1.0, 1.0, 0.0, 0.0, 1.0, dt=0.0)  # dt<=0 -> ignored
    assert ahrs.q == before


def test_visual_mapper_autozero_then_outputs():
    mapper = VisualOrientationMapper()
    mapper.reset_for_new_stream(minimum=5, stable_window=3, maximum=50)
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    last = None
    for i in range(40):
        t = t + timedelta(milliseconds=10)
        # Device held still, flat: gravity on -Z, ~1g, no rotation.
        last = mapper.update(make_sample(0.0, 0.0, 0.0, 0.0, 0.0, -1.0, t))
    assert last is not None
    # After calibration it should no longer be flagged as calibrating.
    assert mapper.calibrating is False


def test_complementary_mapper_runs():
    mapper = ComplementaryTiltOrientationMapper()
    mapper.reset_for_new_stream(minimum=2, stable_window=2, maximum=10)
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    result = None
    for _ in range(20):
        t = t + timedelta(milliseconds=10)
        result = mapper.update(make_sample(0.0, 0.0, 0.0, 0.0, 0.0, -1.0, t))
    assert result is not None
    assert -180.0 <= result.yaw <= 180.0


def test_complementary_angles_stay_in_range():
    # Continuous fast rotation must not let the displayed angle drift past +-180.
    mapper = ComplementaryTiltOrientationMapper()
    mapper.reset_for_new_stream(minimum=0, stable_window=1, maximum=1)
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    out = None
    for _ in range(400):
        t = t + timedelta(milliseconds=10)
        # Spin hard about every axis; accel off-magnitude so tilt fusion is skipped.
        out = mapper.update(make_sample(300.0, 300.0, 300.0, 0.0, 0.0, -2.0, t))
    assert out is not None
    assert -180.0 <= out.pitch <= 180.0
    assert -180.0 <= out.roll <= 180.0
    assert -180.0 <= out.yaw <= 180.0


BIAS = (0.8, -0.5, 1.2)  # deg/s zero-rate offset of a "real" sensor


def _still_with_bias(t):
    # +1 g on Z is Madgwick's resting pose, so the filter has nothing to converge
    # and any yaw change can only come from integrating the gyro bias.
    return make_sample(BIAS[0], BIAS[1], BIAS[2], 0.0, 0.0, 1.0, t)


def _yaw_after(mapper, seconds: float) -> float:
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    out = None
    for _ in range(int(seconds * 100)):
        t += timedelta(milliseconds=10)
        out = mapper.update(_still_with_bias(t))
    return out.yaw


def test_auto_zero_estimates_gyro_bias_and_stops_yaw_drift():
    for mapper in (VisualOrientationMapper(visual_deadband_degrees=0.0),
                   ComplementaryTiltOrientationMapper(visual_deadband_degrees=0.0)):
        mapper.reset_for_new_stream()
        yaw = _yaw_after(mapper, 10.0)
        assert mapper.calibrating is False
        assert mapper.bias_measured is True
        assert all(math.isclose(b, e, abs_tol=1e-9) for b, e in zip(mapper.gyro_bias, BIAS))
        # Uncorrected, 1.2 deg/s for ~8 s would have drifted ~10 deg.
        assert abs(yaw) < 0.5, type(mapper).__name__


def test_bias_kept_when_calibration_times_out():
    mapper = ComplementaryTiltOrientationMapper()
    mapper.reset_for_new_stream()
    _yaw_after(mapper, 5.0)
    learned = mapper.gyro_bias
    # Recalibrate while the device keeps moving: no still window -> keep the old bias.
    mapper.reset_for_new_stream(minimum=0, stable_window=10, maximum=20)
    t = datetime(2024, 2, 1, tzinfo=timezone.utc)
    for _ in range(30):
        t += timedelta(milliseconds=10)
        mapper.update(make_sample(90.0, 0.0, 0.0, 0.0, 0.0, -1.0, t))
    assert mapper.calibrating is False
    assert mapper.bias_measured is False
    assert mapper.gyro_bias == learned


def test_factory():
    assert isinstance(create_orientation_mapper("madgwick"), VisualOrientationMapper)
    assert isinstance(create_orientation_mapper("complementary"), ComplementaryTiltOrientationMapper)
