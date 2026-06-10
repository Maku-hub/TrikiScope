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


def test_factory():
    assert isinstance(create_orientation_mapper("madgwick"), VisualOrientationMapper)
    assert isinstance(create_orientation_mapper("complementary"), ComplementaryTiltOrientationMapper)
