"""Small terminal-graphics helpers: sparklines and a wireframe 3D cube."""

from __future__ import annotations

from typing import List, Sequence

from .orientation import Quaternion

_SPARK_CHARS = " ▁▂▃▄▅▆▇█"


def sparkline(values: Sequence[float], width: int, vmin: float | None = None, vmax: float | None = None) -> str:
    """Render the *last* ``width`` values as a unicode sparkline."""
    if width <= 0:
        return ""
    data = list(values)[-width:]
    if not data:
        return " " * width
    lo = vmin if vmin is not None else min(data)
    hi = vmax if vmax is not None else max(data)
    if hi <= lo:
        hi = lo + 1e-9
    out = []
    for v in data:
        norm = (v - lo) / (hi - lo)
        norm = max(0.0, min(1.0, norm))
        idx = int(round(norm * (len(_SPARK_CHARS) - 1)))
        out.append(_SPARK_CHARS[idx])
    return "".join(out).rjust(width)


# Cube geometry: 8 vertices, 12 edges.
_CUBE_VERTICES = [
    (-1, -1, -1),
    (1, -1, -1),
    (1, 1, -1),
    (-1, 1, -1),
    (-1, -1, 1),
    (1, -1, 1),
    (1, 1, 1),
    (-1, 1, 1),
]
_CUBE_EDGES = [
    (0, 1), (1, 2), (2, 3), (3, 0),  # back face
    (4, 5), (5, 6), (6, 7), (7, 4),  # front face
    (0, 4), (1, 5), (2, 6), (3, 7),  # connectors
]
# A short axis triad so the user can tell which way is "up"/"forward".
_AXES = [
    ((0, 0, 0), (1.6, 0, 0), "x"),
    ((0, 0, 0), (0, 1.6, 0), "y"),
    ((0, 0, 0), (0, 0, 1.6), "z"),
]


def render_cube(q: Quaternion, width: int = 44, height: int = 22) -> List[str]:
    """Return ``height`` strings drawing the cube rotated by ``q``."""
    width = max(20, width)
    height = max(10, height)
    grid = [[" "] * width for _ in range(height)]

    cx = width / 2.0
    cy = height / 2.0
    # Terminal cells are ~2x taller than wide, so squash the vertical scale.
    scale_x = (width / 2.0) * 0.62
    scale_y = (height / 2.0) * 0.62

    def project(p):
        rx, ry, rz = q.rotate_vector(*p)
        sx = int(round(cx + rx * scale_x))
        sy = int(round(cy - ry * scale_y))
        return sx, sy, rz

    projected = [project(v) for v in _CUBE_VERTICES]

    # Draw axes first (so the cube edges sit on top).
    for start, end, label in _AXES:
        sx, sy, _ = project(start)
        ex, ey, _ = project(end)
        _draw_line(grid, sx, sy, ex, ey, "·")
        if 0 <= ey < height and 0 <= ex < width:
            grid[ey][ex] = label

    for a, b in _CUBE_EDGES:
        ax, ay, az = projected[a]
        bx, by, bz = projected[b]
        depth = (az + bz) / 2.0
        char = "#" if depth >= 0 else "+"
        _draw_line(grid, ax, ay, bx, by, char)

    # Mark vertices.
    for sx, sy, sz in projected:
        if 0 <= sy < height and 0 <= sx < width:
            grid[sy][sx] = "●" if sz >= 0 else "○"

    return ["".join(row) for row in grid]


def _draw_line(grid: List[List[str]], x0: int, y0: int, x1: int, y1: int, char: str) -> None:
    """Bresenham line into the character grid (skips cells already drawn over)."""
    height = len(grid)
    width = len(grid[0]) if height else 0
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        if 0 <= y0 < height and 0 <= x0 < width:
            if grid[y0][x0] in (" ", "·"):
                grid[y0][x0] = char
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def bar(value: float, lo: float, hi: float, width: int = 20) -> str:
    """Centered horizontal bar for an angle/value in [lo, hi]."""
    if hi <= lo:
        return " " * width
    norm = (value - lo) / (hi - lo)
    norm = max(0.0, min(1.0, norm))
    filled = int(round(norm * width))
    return "█" * filled + "░" * (width - filled)
