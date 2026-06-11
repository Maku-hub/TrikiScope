"""The :class:`Game` contract shared by every mini-game in the Games tab.

A game is fed the same live data the rest of the dashboard already produces:

* :meth:`on_sample` - one decoded :class:`ImuSample` plus the current
  :class:`VisualOrientation`, at the device sample rate (~100 Hz),
* :meth:`on_button` - the cap's physical button, on each state change,
* :meth:`on_gesture` - a recognised gesture name (see :mod:`trikiscope.gestures`).

It draws itself through :meth:`render`, which returns any Rich renderable; the
app wraps that in a titled panel.  Games hold no BLE state of their own - they
are pure consumers of the stream, so they are trivial to unit-test offline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import RenderableType

if TYPE_CHECKING:  # avoid import cycles at runtime; these are type-only hints
    from ..orientation import VisualOrientation
    from ..protocol import ImuSample


class Game:
    """Base class for a Triki-controlled mini-game."""

    #: Short label shown in the game selector strip.
    name: str = "Game"
    #: One-line "how to play" hint shown under the game.
    help: str = ""

    def reset(self) -> None:
        """Restart the game from a clean state (called when (re)selected)."""

    def on_sample(self, sample: "ImuSample", orientation: "VisualOrientation") -> None:
        """Advance the game with one IMU sample and the current orientation."""

    def on_button(self, pressed: bool) -> None:
        """The cap's physical button changed state (``True`` = pressed)."""

    def on_gesture(self, name: str) -> None:
        """A gesture fired (e.g. ``"TAP / IMPACT"``, ``"SPIN"``)."""

    def render(self, width: int, height: int) -> RenderableType:  # pragma: no cover - trivial
        """Return a Rich renderable drawing the current game state."""
        raise NotImplementedError


def _clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else hi if value > hi else value
