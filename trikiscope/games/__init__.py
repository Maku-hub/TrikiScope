"""Mini-games for the Games tab, all driven by the live Triki stream.

Each game implements the :class:`Game` contract.  :func:`build_games` returns a
fresh instance of every game in selector order.
"""

from __future__ import annotations

from typing import List

from .base import Game
from .reflex import ReflexCatch
from .spin_meter import SpinMeter
from .tilt_maze import TiltMaze

__all__ = ["Game", "build_games", "TiltMaze", "SpinMeter", "ReflexCatch"]


def build_games() -> List[Game]:
    return [TiltMaze(), SpinMeter(), ReflexCatch()]
