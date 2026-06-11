"""Tilt Maze - roll a ball through an arena by tilting the cap.

The ball is accelerated by the current pitch/roll of the device (the same
orientation the Orientation tab shows), bounces off walls, and you steer it onto
the glowing goal.  Each goal reached relocates the goal and bumps the score;
a running clock lets you chase a personal best (most goals in the least time).
"""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .base import Game, _clamp

# Arena dimensions in character cells (including the 1-cell wall border).
_W = 37
_H = 15

# Physics, in cells and seconds.
_GRAVITY = 30.0      # acceleration at full tilt (cells / s^2)
_MAX_SPEED = 22.0    # terminal speed (cells / s)
_FULL_TILT_DEG = 45.0  # tilt mapped to full acceleration
_DAMPING = 4.0       # velocity decay per second (rolling friction)


def _build_walls() -> set[tuple[int, int]]:
    """The arena: a solid border plus a few interior obstacles."""
    walls: set[tuple[int, int]] = set()
    for x in range(_W):
        walls.add((x, 0))
        walls.add((x, _H - 1))
    for y in range(_H):
        walls.add((0, y))
        walls.add((_W - 1, y))

    def vwall(x: int, y0: int, y1: int) -> None:
        for y in range(y0, y1 + 1):
            walls.add((x, y))

    def hwall(y: int, x0: int, x1: int) -> None:
        for x in range(x0, x1 + 1):
            walls.add((x, y))

    vwall(12, 1, 6)
    vwall(12, 9, 13)
    vwall(24, 4, 10)
    hwall(7, 13, 23)
    vwall(30, 2, 8)
    return walls


_WALLS = _build_walls()
_START = (3, 7)
# Goals cycle through these floor cells as each is collected.
_GOALS = [(34, 2), (34, 12), (2, 13), (20, 3), (2, 2), (28, 12)]


class TiltMaze(Game):
    name = "Tilt Maze"
    help = "Tilt the cap to roll the ball onto the ◆ goal.  r = re-zero tilt, g = restart."

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.x = float(_START[0])
        self.y = float(_START[1])
        self.vx = 0.0
        self.vy = 0.0
        self.score = 0
        self.elapsed = 0.0
        self._goal_index = 0
        self._last_t: float | None = None

    @property
    def gx(self) -> int:
        return _GOALS[self._goal_index % len(_GOALS)][0]

    @property
    def gy(self) -> int:
        return _GOALS[self._goal_index % len(_GOALS)][1]

    @staticmethod
    def _is_wall(cx: int, cy: int) -> bool:
        return (cx, cy) in _WALLS or not (0 <= cx < _W and 0 <= cy < _H)

    def on_sample(self, sample, orientation) -> None:
        t = sample.timestamp_utc.timestamp()
        if self._last_t is None:
            self._last_t = t
            return
        dt = t - self._last_t
        self._last_t = t
        if dt <= 0.0 or dt > 0.2:  # guard against gaps / clock jumps
            dt = 0.02
        self.elapsed += dt

        # Tilt -> acceleration.  Roll steers left/right, pitch steers up/down.
        ax = _clamp(orientation.roll / _FULL_TILT_DEG, -1.0, 1.0) * _GRAVITY
        ay = _clamp(orientation.pitch / _FULL_TILT_DEG, -1.0, 1.0) * _GRAVITY
        self.vx += ax * dt
        self.vy += ay * dt
        damp = max(0.0, 1.0 - _DAMPING * dt)
        self.vx *= damp
        self.vy *= damp
        self.vx = _clamp(self.vx, -_MAX_SPEED, _MAX_SPEED)
        self.vy = _clamp(self.vy, -_MAX_SPEED, _MAX_SPEED)

        # Move per-axis so a wall on one axis doesn't block the other.
        nx = self.x + self.vx * dt
        if self._is_wall(round(nx), round(self.y)):
            self.vx = 0.0
        else:
            self.x = _clamp(nx, 1.0, _W - 2.0)
        ny = self.y + self.vy * dt
        if self._is_wall(round(self.x), round(ny)):
            self.vy = 0.0
        else:
            self.y = _clamp(ny, 1.0, _H - 2.0)

        if round(self.x) == self.gx and round(self.y) == self.gy:
            self.score += 1
            self._goal_index += 1

    def render(self, width: int, height: int) -> RenderableType:
        bx, by = round(self.x), round(self.y)
        canvas = Text(no_wrap=True)
        for y in range(_H):
            for x in range(_W):
                if x == bx and y == by:
                    canvas.append("●", style="bold cyan")
                elif x == self.gx and y == self.gy:
                    canvas.append("◆", style="bold yellow")
                elif (x, y) in _WALLS:
                    canvas.append("█", style="grey37")
                else:
                    canvas.append(" ")
            if y < _H - 1:
                canvas.append("\n")

        stats = Table.grid(padding=(0, 2))
        stats.add_column(style="bold")
        stats.add_column()
        stats.add_row("Goals", str(self.score))
        stats.add_row("Time", f"{self.elapsed:5.1f} s")
        speed = (self.vx ** 2 + self.vy ** 2) ** 0.5
        stats.add_row("Speed", f"{speed:4.1f} cells/s")
        return Group(canvas, Panel(stats, border_style="grey50", expand=False))
