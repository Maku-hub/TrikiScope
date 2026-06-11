"""Spin Meter - how fast can you spin the cap?

Start spinning and a 5-second round begins; the meter tracks your peak angular
rate (from the gyro magnitude) and remembers your best run.  A pure test of how
hard you can flick the Triki.
"""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from .. import render as r
from .base import Game

_START_DPS = 150.0     # spin faster than this to start a round
_ROUND_SECONDS = 5.0
_GAUGE_MAX_DPS = 2000.0  # full-scale of the live gauge


class SpinMeter(Game):
    name = "Spin Meter"
    help = "Spin the cap to start a 5 s round.  Beat your best peak rate.  g = reset best."

    def __init__(self) -> None:
        self.best = 0.0
        self.reset()

    def reset(self) -> None:
        self.state = "ready"  # ready -> running -> done
        self.cur = 0.0
        self.peak = 0.0
        self.remaining = _ROUND_SECONDS
        self.best = 0.0
        self._start_t: float | None = None

    def on_sample(self, sample, orientation) -> None:
        t = sample.timestamp_utc.timestamp()
        self.cur = sample.gyro_magnitude

        if self.state == "ready":
            if self.cur >= _START_DPS:
                self.state = "running"
                self._start_t = t
                self.peak = self.cur
        elif self.state == "running":
            self.peak = max(self.peak, self.cur)
            elapsed = t - (self._start_t or t)
            self.remaining = max(0.0, _ROUND_SECONDS - elapsed)
            if self.remaining <= 0.0:
                self.state = "done"
                self.best = max(self.best, self.peak)

    def _play_again(self) -> None:
        self.state = "ready"
        self.cur = 0.0
        self.peak = 0.0
        self.remaining = _ROUND_SECONDS
        self._start_t = None

    def on_button(self, pressed: bool) -> None:
        if pressed and self.state == "done":
            self._play_again()

    def render(self, width: int, height: int) -> RenderableType:
        gauge = r.bar(self.cur, 0.0, _GAUGE_MAX_DPS, 40)
        rps = self.cur / 360.0
        peak_rps = self.peak / 360.0
        best_rps = self.best / 360.0

        if self.state == "ready":
            headline = Text("Spin the cap to start!", style="bold cyan")
        elif self.state == "running":
            headline = Text(f"GO!  {self.remaining:4.1f}s left", style="bold green")
        else:
            headline = Text("Round over - press the cap button to play again", style="bold yellow")

        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold")
        table.add_column()
        table.add_row("Now", f"{self.cur:7.0f} deg/s   ({rps:4.2f} rev/s)")
        table.add_row("Peak", Text(f"{self.peak:7.0f} deg/s   ({peak_rps:4.2f} rev/s)", style="cyan"))
        table.add_row("Best", Text(f"{self.best:7.0f} deg/s   ({best_rps:4.2f} rev/s)", style="bold magenta"))

        return Group(
            headline,
            Text(""),
            Text(gauge, style="green"),
            Text(""),
            table,
        )
