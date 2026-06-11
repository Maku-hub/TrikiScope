"""Tests for the Games-tab mini-games (pure stream consumers, no BLE)."""

from datetime import datetime, timedelta, timezone

from trikiscope.games import ReflexCatch, SpinMeter, TiltMaze, build_games
from trikiscope.games.tilt_maze import _START, _WALLS
from trikiscope.orientation import Quaternion, VisualOrientation
from trikiscope.protocol import ImuSample


def sample(t, gx=0.0, gy=0.0, gz=0.0, ax=0.0, ay=0.0, az=1.0):
    return ImuSample(0, t, gx, gy, gz, ax, ay, az, 0, 0, 0, 0, 0, 0)


def orient(pitch=0.0, roll=0.0, yaw=0.0):
    return VisualOrientation(pitch, roll, yaw, Quaternion.identity())


def test_build_games_returns_fresh_instances():
    a = build_games()
    b = build_games()
    assert [g.name for g in a] == ["Tilt Maze", "Spin Meter", "Reflex Catch"]
    assert a[0] is not b[0]


def test_tilt_maze_rolls_in_tilt_direction():
    game = TiltMaze()
    start_x = game.x
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    # Tilt fully to the right (positive roll) for a while.
    for _ in range(60):
        t += timedelta(milliseconds=10)
        game.on_sample(sample(t), orient(roll=45.0))
    assert game.x > start_x  # the ball accelerated to the right


def test_tilt_maze_keeps_ball_inside_and_off_walls():
    game = TiltMaze()
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    # Slam toward the top-left corner; the border must stop the ball.
    for _ in range(200):
        t += timedelta(milliseconds=10)
        game.on_sample(sample(t), orient(pitch=-45.0, roll=-45.0))
    cell = (round(game.x), round(game.y))
    assert 1 <= cell[0] <= 35 and 1 <= cell[1] <= 13
    assert cell not in _WALLS


def test_tilt_maze_first_sample_seeds_clock_without_moving():
    game = TiltMaze()
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    game.on_sample(sample(t), orient(roll=45.0))
    assert (game.x, game.y) == (float(_START[0]), float(_START[1]))


def test_spin_meter_starts_tracks_peak_and_finishes():
    game = SpinMeter()
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)

    # Quiet motion: stays in 'ready'.
    for _ in range(5):
        t += timedelta(milliseconds=10)
        game.on_sample(sample(t, gx=10.0), orient())
    assert game.state == "ready"

    # Spin hard: round starts and tracks the peak.
    t += timedelta(milliseconds=10)
    game.on_sample(sample(t, gx=400.0, gy=300.0), orient())  # mag = 500
    assert game.state == "running"
    t += timedelta(milliseconds=10)
    game.on_sample(sample(t, gx=800.0, gy=600.0), orient())  # mag = 1000
    assert game.peak >= 999.0

    # After the 5 s round the result is frozen as the best.
    t += timedelta(seconds=6)
    game.on_sample(sample(t, gx=100.0), orient())
    assert game.state == "done"
    assert game.best >= 999.0


def test_reflex_catch_full_round_measures_reaction():
    game = ReflexCatch()
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)

    def step(ms):
        nonlocal t
        t += timedelta(milliseconds=ms)
        game.on_sample(sample(t), orient())

    step(10)
    game.on_button(True)  # arm -> waiting (first wait is 1.4 s)
    assert game.state == "waiting"

    step(1500)  # past the deadline -> the 'go' signal fires
    assert game.state == "go"

    step(180)  # 180 ms reaction
    game.on_button(True)
    assert game.state == "result"
    assert 170.0 <= game.last_ms <= 190.0
    assert game.best == game.last_ms


def test_reflex_catch_false_start():
    game = ReflexCatch()
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    game.on_sample(sample(t), orient())
    game.on_button(True)  # arm
    t += timedelta(milliseconds=200)
    game.on_sample(sample(t), orient())  # still waiting
    game.on_button(True)  # acted too soon
    assert game.state == "early"
    assert game.best is None


def test_reflex_catch_tap_gesture_counts_in_go_but_not_arm():
    game = ReflexCatch()
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    game.on_sample(sample(t), orient())
    # A stray tap in 'ready' must not arm a round.
    game.on_gesture("TAP / IMPACT")
    assert game.state == "ready"
    # Arm, reach 'go', then a tap reacts.
    game.on_button(True)
    t += timedelta(milliseconds=1500)
    game.on_sample(sample(t), orient())
    assert game.state == "go"
    t += timedelta(milliseconds=150)
    game.on_sample(sample(t), orient())
    game.on_gesture("TAP / IMPACT")
    assert game.state == "result"
    assert game.last_ms is not None
