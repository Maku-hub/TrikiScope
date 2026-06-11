"""Reflex Catch - a reaction-time test driven by the cap.

Press the button to arm a round.  After a short, varying wait the screen flashes
``TAP NOW!`` - react as fast as you can with a sharp tap (IMPACT gesture) or a
button press.  Reacting before the signal counts as a false start.  Tracks your
best (lowest) reaction time.
"""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from .base import Game

# Deterministic wait sequence (seconds) cycled per round - no randomness, so the
# game stays unit-testable and resume-safe.
_WAITS = [1.4, 2.2, 0.9, 1.8, 3.0, 1.2]


class ReflexCatch(Game):
    name = "Reflex Catch"
    help = "Press the cap button to arm; on 'TAP NOW!' tap or press as fast as you can.  g = reset."

    def __init__(self) -> None:
        self.best: float | None = None
        self.reset()

    def reset(self) -> None:
        self.state = "ready"  # ready -> waiting -> go -> result / early
        self.last_ms: float | None = None
        self.best = None
        self._round = 0
        self._t = 0.0
        self._deadline = 0.0
        self._go_t = 0.0

    def on_sample(self, sample, orientation) -> None:
        self._t = sample.timestamp_utc.timestamp()
        if self.state == "waiting" and self._t >= self._deadline:
            self.state = "go"
            self._go_t = self._t

    def on_button(self, pressed: bool) -> None:
        if pressed:
            self._trigger(from_gesture=False)

    def on_gesture(self, name: str) -> None:
        # Only a sharp tap counts as a reaction; ignore spins/shakes.
        if name == "TAP / IMPACT":
            self._trigger(from_gesture=True)

    def _trigger(self, from_gesture: bool) -> None:
        if self.state == "ready":
            if not from_gesture:  # the button arms a round; a stray tap does not
                self._arm()
        elif self.state == "waiting":
            self.state = "early"  # acted before the signal
        elif self.state == "go":
            self.last_ms = max(0.0, (self._t - self._go_t) * 1000.0)
            if self.best is None or self.last_ms < self.best:
                self.best = self.last_ms
            self.state = "result"
        elif self.state in ("result", "early"):
            if not from_gesture:
                self._arm()

    def _arm(self) -> None:
        wait = _WAITS[self._round % len(_WAITS)]
        self._round += 1
        self._deadline = self._t + wait
        self.state = "waiting"

    def render(self, width: int, height: int) -> RenderableType:
        if self.state == "ready":
            banner = Text("Press the cap button to start", style="bold cyan")
        elif self.state == "waiting":
            banner = Text("wait for it...", style="bold yellow")
        elif self.state == "go":
            banner = Text("⚡ TAP NOW! ⚡", style="bold white on dark_green")
        elif self.state == "early":
            banner = Text("Too early!  Press to try again", style="bold white on dark_red")
        else:  # result
            banner = Text(f"{self.last_ms:.0f} ms  -  press to try again", style="bold green")

        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold")
        table.add_column()
        table.add_row("Last", "--" if self.last_ms is None else f"{self.last_ms:.0f} ms")
        table.add_row("Best", "--" if self.best is None else Text(f"{self.best:.0f} ms", style="bold magenta"))

        return Group(banner, Text(""), table)
