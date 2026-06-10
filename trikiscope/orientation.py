"""Orientation estimation ported from the WPF TrikiReader.

Contains:

* :class:`Quaternion` - a small (x, y, z, w) quaternion matching WPF's
  ``System.Windows.Media.Media3D.Quaternion`` semantics (Hamilton product,
  axis/angle-in-degrees constructor) so the ported filters behave identically.
* :class:`MadgwickAHRS` - Madgwick's IMU gradient-descent AHRS filter.
* :class:`VisualOrientationMapper` - Madgwick + auto-zero calibration,
  slerp smoothing and a visual dead-band.
* :class:`ComplementaryTiltOrientationMapper` - a gyro/accel complementary
  filter (the "Zappka-like" pitch/roll mode).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from .protocol import ImuSample

_RAD2DEG = 180.0 / math.pi
_DEG2RAD = math.pi / 180.0


@dataclass(slots=True)
class Quaternion:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    w: float = 1.0

    @staticmethod
    def identity() -> "Quaternion":
        return Quaternion(0.0, 0.0, 0.0, 1.0)

    @staticmethod
    def from_axis_angle(ax: float, ay: float, az: float, angle_degrees: float) -> "Quaternion":
        length = math.sqrt(ax * ax + ay * ay + az * az)
        if length == 0.0:
            return Quaternion.identity()
        angle_rad = angle_degrees * _DEG2RAD
        s = math.sin(0.5 * angle_rad) / length
        return Quaternion(ax * s, ay * s, az * s, math.cos(0.5 * angle_rad))

    def __mul__(self, other: "Quaternion") -> "Quaternion":
        # WPF Hamilton product: self (left) * other (right).
        return Quaternion(
            self.w * other.x + self.x * other.w + self.y * other.z - self.z * other.y,
            self.w * other.y + self.y * other.w + self.z * other.x - self.x * other.z,
            self.w * other.z + self.z * other.w + self.x * other.y - self.y * other.x,
            self.w * other.w - self.x * other.x - self.y * other.y - self.z * other.z,
        )

    def norm(self) -> float:
        return math.sqrt(self.x * self.x + self.y * self.y + self.z * self.z + self.w * self.w)

    def normalized(self) -> "Quaternion":
        n = self.norm()
        if n == 0.0:
            return Quaternion.identity()
        return Quaternion(self.x / n, self.y / n, self.z / n, self.w / n)

    def inverse(self) -> "Quaternion":
        n2 = self.x * self.x + self.y * self.y + self.z * self.z + self.w * self.w
        if n2 == 0.0:
            return Quaternion.identity()
        return Quaternion(-self.x / n2, -self.y / n2, -self.z / n2, self.w / n2)

    def rotate_vector(self, vx: float, vy: float, vz: float) -> tuple[float, float, float]:
        """Rotate a 3-vector by this (assumed unit) quaternion."""
        qv = Quaternion(vx, vy, vz, 0.0)
        r = self * qv * self.inverse()
        return r.x, r.y, r.z

    def to_euler_degrees(self) -> tuple[float, float, float]:
        """Return (pitch, roll, yaw) in degrees (matches the WPF ExtractEulerAngles)."""
        x, y, z, w = self.x, self.y, self.z, self.w
        # Roll (x-axis)
        sinr_cosp = 2 * (w * x + y * z)
        cosr_cosp = 1 - 2 * (x * x + y * y)
        roll = math.atan2(sinr_cosp, cosr_cosp) * _RAD2DEG
        # Pitch (y-axis)
        sinp = 2 * (w * y - z * x)
        if abs(sinp) >= 1:
            pitch = math.copysign(math.pi / 2, sinp) * _RAD2DEG
        else:
            pitch = math.asin(sinp) * _RAD2DEG
        # Yaw (z-axis)
        siny_cosp = 2 * (w * z + x * y)
        cosy_cosp = 1 - 2 * (y * y + z * z)
        yaw = math.atan2(siny_cosp, cosy_cosp) * _RAD2DEG
        return pitch, roll, yaw

    @staticmethod
    def slerp(a: "Quaternion", b: "Quaternion", t: float) -> "Quaternion":
        a = a.normalized()
        b = b.normalized()
        dot = a.x * b.x + a.y * b.y + a.z * b.z + a.w * b.w
        if dot < 0.0:
            b = Quaternion(-b.x, -b.y, -b.z, -b.w)
            dot = -dot
        if dot > 0.9995:
            res = Quaternion(
                a.x + t * (b.x - a.x),
                a.y + t * (b.y - a.y),
                a.z + t * (b.z - a.z),
                a.w + t * (b.w - a.w),
            )
            return res.normalized()
        theta_0 = math.acos(max(-1.0, min(1.0, dot)))
        theta = theta_0 * t
        sin_theta = math.sin(theta)
        sin_theta_0 = math.sin(theta_0)
        s0 = math.cos(theta) - dot * sin_theta / sin_theta_0
        s1 = sin_theta / sin_theta_0
        return Quaternion(
            s0 * a.x + s1 * b.x,
            s0 * a.y + s1 * b.y,
            s0 * a.z + s1 * b.z,
            s0 * a.w + s1 * b.w,
        )


class MadgwickAHRS:
    """Madgwick's IMU AHRS (gyro in rad/s, accel in any unit; it is normalised)."""

    def __init__(self, beta: float = 0.1) -> None:
        self.beta = beta
        # [w, x, y, z]
        self.q: List[float] = [1.0, 0.0, 0.0, 0.0]

    def reset(self) -> None:
        self.q = [1.0, 0.0, 0.0, 0.0]

    def update(self, gx: float, gy: float, gz: float, ax: float, ay: float, az: float, dt: float) -> None:
        if dt <= 0.0:
            return
        q1, q2, q3, q4 = self.q

        _2q1 = 2.0 * q1
        _2q2 = 2.0 * q2
        _2q3 = 2.0 * q3
        _2q4 = 2.0 * q4
        _4q1 = 4.0 * q1
        _4q2 = 4.0 * q2
        _4q3 = 4.0 * q3
        _8q2 = 8.0 * q2
        _8q3 = 8.0 * q3
        q1q1 = q1 * q1
        q2q2 = q2 * q2
        q3q3 = q3 * q3
        q4q4 = q4 * q4

        if ax == 0.0 and ay == 0.0 and az == 0.0:
            return
        norm = math.sqrt(ax * ax + ay * ay + az * az)
        ax /= norm
        ay /= norm
        az /= norm

        s1 = _4q1 * q3q3 + _2q3 * ax + _4q1 * q2q2 - _2q2 * ay
        s2 = _4q2 * q4q4 - _2q4 * ax + 4.0 * q1q1 * q2 - _2q1 * ay - _4q2 + _8q2 * q2q2 + _8q2 * q3q3 + _4q2 * az
        s3 = 4.0 * q1q1 * q3 + _2q1 * ax + _4q3 * q4q4 - _2q4 * ay - _4q3 + _8q3 * q2q2 + _8q3 * q3q3 + _4q3 * az
        s4 = 4.0 * q2q2 * q4 - _2q2 * ax + 4.0 * q3q3 * q4 - _2q3 * ay

        norm = math.sqrt(s1 * s1 + s2 * s2 + s3 * s3 + s4 * s4)
        if norm > 0:
            s1 /= norm
            s2 /= norm
            s3 /= norm
            s4 /= norm

        q_dot1 = 0.5 * (-q2 * gx - q3 * gy - q4 * gz) - self.beta * s1
        q_dot2 = 0.5 * (q1 * gx + q3 * gz - q4 * gy) - self.beta * s2
        q_dot3 = 0.5 * (q1 * gy - q2 * gz + q4 * gx) - self.beta * s3
        q_dot4 = 0.5 * (q1 * gz + q2 * gy - q3 * gx) - self.beta * s4

        q1 += q_dot1 * dt
        q2 += q_dot2 * dt
        q3 += q_dot3 * dt
        q4 += q_dot4 * dt

        norm = math.sqrt(q1 * q1 + q2 * q2 + q3 * q3 + q4 * q4)
        if norm == 0.0:
            return
        self.q = [q1 / norm, q2 / norm, q3 / norm, q4 / norm]


@dataclass(slots=True)
class VisualOrientation:
    pitch: float
    roll: float
    yaw: float
    quaternion: Quaternion


def _normalize_angle(angle: float) -> float:
    while angle > 180.0:
        angle -= 360.0
    while angle < -180.0:
        angle += 360.0
    return angle


class VisualOrientationMapper:
    """Madgwick-based orientation with auto-zero, slerp smoothing and dead-band."""

    _AUTO_ZERO_GYRO_STILL_THRESHOLD = 2.0
    _AUTO_ZERO_MIN_ACCEL = 0.85
    _AUTO_ZERO_MAX_ACCEL = 1.15

    def __init__(
        self,
        gyro_gain: float = 2.5,
        fallback_dt: float = 0.02,
        minimum_dt: float = 0.001,
        beta: float = 1.5,
        smoothing_factor: float = 0.35,
        visual_deadband_degrees: float = 8.0,
    ) -> None:
        self._ahrs = MadgwickAHRS(beta)
        self._gyro_gain = gyro_gain
        self._fallback_dt = fallback_dt
        self._minimum_dt = minimum_dt
        self._smoothing_factor = smoothing_factor
        self._visual_deadband_degrees = visual_deadband_degrees

        self._last_ts: Optional[datetime] = None
        self._offset = Quaternion.identity()
        self._smoothed = Quaternion.identity()
        self._is_first_sample = True
        self._auto_zero_pending = False
        self._auto_zero_count = 0
        self._auto_zero_stable_count = 0
        self._auto_zero_min = 0
        self._auto_zero_required_stable = 0
        self._auto_zero_max = 0
        self.yaw = 0.0
        self.calibrating = False

    def update(self, sample: ImuSample) -> VisualOrientation:
        dt = self._fallback_dt
        if self._last_ts is not None:
            dt = (sample.timestamp_utc - self._last_ts).total_seconds()
            if dt <= self._minimum_dt:
                dt = self._fallback_dt
        self._last_ts = sample.timestamp_utc

        gx = sample.gyro_x * self._gyro_gain * _DEG2RAD
        gy = sample.gyro_y * self._gyro_gain * _DEG2RAD
        # Negate gyro-Z so a left (CCW) spin while flat rotates the visual left too.
        # Yaw is gyro-only (accel does not constrain heading), so this affects only
        # yaw direction and leaves pitch/roll unchanged.
        gz = -sample.gyro_z * self._gyro_gain * _DEG2RAD

        self._ahrs.update(gx, gy, gz, sample.accel_x, sample.accel_y, sample.accel_z, dt)
        raw_quat = self._to_visual_quaternion(self._ahrs.q)

        if self._auto_zero_pending:
            return self._update_auto_zero(sample, raw_quat)

        target = self._apply_visual_deadband(self._offset * raw_quat)

        if self._is_first_sample:
            self._smoothed = target
            self._is_first_sample = False
        else:
            self._smoothed = Quaternion.slerp(self._smoothed, target, self._smoothing_factor)

        pitch, roll, yaw = self._smoothed.to_euler_degrees()
        self.yaw = yaw
        return VisualOrientation(pitch, roll, yaw, self._smoothed)

    def reset_for_new_stream(self, minimum=50, stable_window=10, maximum=200) -> None:
        self._ahrs.reset()
        self._last_ts = None
        self._offset = Quaternion.identity()
        self._smoothed = Quaternion.identity()
        self._is_first_sample = True
        self._auto_zero_pending = True
        self._auto_zero_count = 0
        self._auto_zero_stable_count = 0
        self._auto_zero_min = minimum
        self._auto_zero_required_stable = stable_window
        self._auto_zero_max = maximum
        self.yaw = 0.0
        self.calibrating = True

    def reset(self) -> None:
        current = self._to_visual_quaternion(self._ahrs.q)
        self._offset = current.inverse()
        self._smoothed = Quaternion.identity()
        self._is_first_sample = True
        self._auto_zero_pending = False
        self._auto_zero_count = 0
        self._auto_zero_stable_count = 0
        self.yaw = 0.0
        self.calibrating = False

    def _update_auto_zero(self, sample: ImuSample, raw_quat: Quaternion) -> VisualOrientation:
        self.calibrating = True
        self._auto_zero_count += 1
        if self._auto_zero_count > self._auto_zero_min:
            if self._is_still(sample):
                self._auto_zero_stable_count += 1
            else:
                self._auto_zero_stable_count = 0
        if (
            self._auto_zero_stable_count >= self._auto_zero_required_stable
            or self._auto_zero_count >= self._auto_zero_max
        ):
            self._offset = raw_quat.inverse()
            self._smoothed = Quaternion.identity()
            self._is_first_sample = True
            self._auto_zero_pending = False
            self.calibrating = False
        self.yaw = 0.0
        return VisualOrientation(0.0, 0.0, 0.0, Quaternion.identity())

    def _is_still(self, sample: ImuSample) -> bool:
        if sample.gyro_magnitude > self._AUTO_ZERO_GYRO_STILL_THRESHOLD:
            return False
        mag = sample.accel_magnitude
        return self._AUTO_ZERO_MIN_ACCEL <= mag <= self._AUTO_ZERO_MAX_ACCEL

    @staticmethod
    def _to_visual_quaternion(q: List[float]) -> Quaternion:
        # Madgwick is [w, x, y, z]; WPF quaternion is (x, y, z, w) with axis remap.
        return Quaternion(-q[1], q[2], -q[3], q[0])

    def _apply_visual_deadband(self, q: Quaternion) -> Quaternion:
        if self._visual_deadband_degrees <= 0.0:
            return q
        q = q.normalized()
        if q.w < 0.0:
            q = Quaternion(-q.x, -q.y, -q.z, -q.w)
        clamped_w = max(-1.0, min(1.0, q.w))
        angle_deg = 2.0 * math.acos(clamped_w) * _RAD2DEG
        if angle_deg <= self._visual_deadband_degrees:
            return Quaternion.identity()
        axis_len = math.sqrt(q.x * q.x + q.y * q.y + q.z * q.z)
        if axis_len <= 1e-12:
            return Quaternion.identity()
        return Quaternion.from_axis_angle(
            q.x / axis_len, q.y / axis_len, q.z / axis_len, angle_deg - self._visual_deadband_degrees
        )


class ComplementaryTiltOrientationMapper:
    """Gyro/accel complementary filter producing pitch/roll/yaw directly."""

    _AUTO_ZERO_GYRO_STILL_THRESHOLD = 2.0
    _AUTO_ZERO_MIN_ACCEL = 0.85
    _AUTO_ZERO_MAX_ACCEL = 1.15

    def __init__(
        self,
        gyro_gain: float = 1.0,
        fallback_dt: float = 0.02,
        minimum_dt: float = 0.001,
        complementary_alpha: float = 0.96,
        smoothing_factor: float = 0.35,
        visual_deadband_degrees: float = 4.0,
    ) -> None:
        self._gyro_gain = gyro_gain
        self._fallback_dt = fallback_dt
        self._minimum_dt = minimum_dt
        self._alpha = complementary_alpha
        self._smoothing = smoothing_factor
        self._deadband = visual_deadband_degrees

        self._last_ts: Optional[datetime] = None
        self._pitch = self._roll = self._yaw = 0.0
        self._pitch_off = self._roll_off = self._yaw_off = 0.0
        self._disp_pitch = self._disp_roll = self._disp_yaw = 0.0
        self._is_first_display = True
        self._auto_zero_pending = False
        self._auto_zero_count = 0
        self._auto_zero_stable_count = 0
        self._auto_zero_min = 0
        self._auto_zero_required_stable = 0
        self._auto_zero_max = 0
        self.yaw = 0.0
        self.calibrating = False

    def update(self, sample: ImuSample) -> VisualOrientation:
        dt = self._dt(sample.timestamp_utc)
        self._update_angles(sample, dt)

        if self._auto_zero_pending:
            return self._update_auto_zero(sample)

        target_pitch = self._deadband_angle(_normalize_angle(self._pitch - self._pitch_off))
        target_roll = self._deadband_angle(_normalize_angle(self._roll - self._roll_off))
        target_yaw = self._deadband_angle(_normalize_angle(self._yaw - self._yaw_off))

        if self._is_first_display:
            self._disp_pitch, self._disp_roll, self._disp_yaw = target_pitch, target_roll, target_yaw
            self._is_first_display = False
        else:
            # NormalizeAngle on the delta keeps the smoothing on the shortest path;
            # re-normalising the accumulator keeps the displayed angle within
            # [-180, 180] instead of drifting (e.g. roll showing -258 deg).
            self._disp_pitch = _normalize_angle(
                self._disp_pitch + _normalize_angle(target_pitch - self._disp_pitch) * self._smoothing
            )
            self._disp_roll = _normalize_angle(
                self._disp_roll + _normalize_angle(target_roll - self._disp_roll) * self._smoothing
            )
            self._disp_yaw = _normalize_angle(
                self._disp_yaw + _normalize_angle(target_yaw - self._disp_yaw) * self._smoothing
            )

        return self._to_orientation(self._disp_pitch, self._disp_roll, self._disp_yaw)

    def reset_for_new_stream(self, minimum=50, stable_window=10, maximum=200) -> None:
        self._last_ts = None
        self._pitch = self._roll = self._yaw = 0.0
        self._pitch_off = self._roll_off = self._yaw_off = 0.0
        self._disp_pitch = self._disp_roll = self._disp_yaw = 0.0
        self._is_first_display = True
        self._auto_zero_pending = True
        self._auto_zero_count = 0
        self._auto_zero_stable_count = 0
        self._auto_zero_min = minimum
        self._auto_zero_required_stable = stable_window
        self._auto_zero_max = maximum
        self.calibrating = True

    def reset(self) -> None:
        self._pitch_off, self._roll_off, self._yaw_off = self._pitch, self._roll, self._yaw
        self._disp_pitch = self._disp_roll = self._disp_yaw = 0.0
        self._is_first_display = True
        self._auto_zero_pending = False
        self._auto_zero_count = 0
        self._auto_zero_stable_count = 0
        self.calibrating = False

    def _dt(self, ts: datetime) -> float:
        dt = self._fallback_dt
        if self._last_ts is not None:
            dt = (ts - self._last_ts).total_seconds()
            if dt <= self._minimum_dt:
                dt = self._fallback_dt
        self._last_ts = ts
        return dt

    def _update_angles(self, sample: ImuSample, dt: float) -> None:
        gyro_pitch = self._pitch + sample.gyro_y * self._gyro_gain * dt
        gyro_roll = self._roll - sample.gyro_x * self._gyro_gain * dt
        self._yaw = _normalize_angle(self._yaw - sample.gyro_z * self._gyro_gain * dt)

        tilt = self._accel_tilt(sample)
        if tilt is not None:
            accel_pitch, accel_roll = tilt
            self._pitch = self._alpha * gyro_pitch + (1.0 - self._alpha) * accel_pitch
            self._roll = self._alpha * gyro_roll + (1.0 - self._alpha) * accel_roll
        else:
            self._pitch = gyro_pitch
            self._roll = gyro_roll
        self._pitch = _normalize_angle(self._pitch)
        self._roll = _normalize_angle(self._roll)

    @staticmethod
    def _accel_tilt(sample: ImuSample) -> Optional[tuple[float, float]]:
        mag = sample.accel_magnitude
        if mag < 0.65 or mag > 1.35:
            return None
        horizontal = math.sqrt(sample.accel_y ** 2 + sample.accel_z ** 2)
        pitch = math.atan2(sample.accel_x, horizontal) * _RAD2DEG
        roll = math.atan2(sample.accel_y, -sample.accel_z) * _RAD2DEG
        return pitch, roll

    def _update_auto_zero(self, sample: ImuSample) -> VisualOrientation:
        self.calibrating = True
        self._auto_zero_count += 1
        if self._auto_zero_count > self._auto_zero_min:
            if self._is_still(sample):
                self._auto_zero_stable_count += 1
            else:
                self._auto_zero_stable_count = 0
        if (
            self._auto_zero_stable_count >= self._auto_zero_required_stable
            or self._auto_zero_count >= self._auto_zero_max
        ):
            self._pitch_off, self._roll_off, self._yaw_off = self._pitch, self._roll, self._yaw
            self._disp_pitch = self._disp_roll = self._disp_yaw = 0.0
            self._is_first_display = True
            self._auto_zero_pending = False
            self.calibrating = False
        return self._to_orientation(0.0, 0.0, 0.0)

    def _is_still(self, sample: ImuSample) -> bool:
        if sample.gyro_magnitude > self._AUTO_ZERO_GYRO_STILL_THRESHOLD:
            return False
        mag = sample.accel_magnitude
        return self._AUTO_ZERO_MIN_ACCEL <= mag <= self._AUTO_ZERO_MAX_ACCEL

    def _deadband_angle(self, angle: float) -> float:
        if self._deadband <= 0.0:
            return angle
        if abs(angle) <= self._deadband:
            return 0.0
        return math.copysign(abs(angle) - self._deadband, angle)

    def _to_orientation(self, pitch: float, roll: float, yaw: float) -> VisualOrientation:
        self.yaw = yaw
        qz = Quaternion.from_axis_angle(0, 0, 1, yaw)
        qy = Quaternion.from_axis_angle(0, 1, 0, pitch)
        qx = Quaternion.from_axis_angle(1, 0, 0, roll)
        quat = qz * qy * qx
        return VisualOrientation(pitch, roll, yaw, quat)


def create_orientation_mapper(mode: str):
    mode = mode.lower()
    if mode in ("madgwick", "visual"):
        return VisualOrientationMapper()
    if mode in ("complementary", "zappka", "zappkalike", "tilt"):
        return ComplementaryTiltOrientationMapper()
    raise ValueError(f"unknown orientation mode: {mode!r}")
