"""Textual TUI dashboard for the Triki device."""

from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Deque, List, Optional

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Footer, Header, RichLog, Static, TabbedContent, TabPane

from . import gatt_names as g
from . import render
from .ble import (
    AdvertisementInfo,
    BleCallbacks,
    DeviceInfo,
    GattServiceInfo,
    TrikiBleClient,
)
from .config import AppConfig
from .games import Game, build_games
from .gestures import GestureDetector
from .orientation import Quaternion, VisualOrientation, create_orientation_mapper
from .protocol import BleNotificationProcessor, ImuSample
from .recorder import Recorder


@dataclass
class AppState:
    conn_state: str = "disconnected"
    advertisement: Optional[AdvertisementInfo] = None
    device_info: Optional[DeviceInfo] = None
    gatt: List[GattServiceInfo] = field(default_factory=list)
    battery: Optional[int] = None
    last_sample: Optional[ImuSample] = None
    orientation: Optional[VisualOrientation] = None
    orientation_mode: str = "madgwick"
    calibrating: bool = False


class TrikiApp(App):
    CSS = """
    Screen { background: $surface; }
    #overview-body, #gatt-body, #imu-body, #orient-body, #games-body { padding: 1 2; }
    RichLog { background: $panel; }
    """

    BINDINGS = [
        ("c", "connect", "Connect"),
        ("d", "disconnect", "Disconnect"),
        ("r", "reset_orientation", "Zero orient."),
        ("z", "recalibrate", "Recalibrate"),
        ("m", "toggle_mode", "Mode"),
        ("s", "toggle_record", "Record"),
        ("l", "toggle_led", "LED"),
        ("q", "quit", "Quit"),
        ("1", "show_tab('overview')", "Overview"),
        ("2", "show_tab('gatt')", "GATT"),
        ("3", "show_tab('imu')", "IMU"),
        ("4", "show_tab('orient')", "Orient"),
        ("5", "show_tab('games')", "Games"),
        ("6", "show_tab('log')", "Log"),
        ("left_square_bracket", "game_prev", "◀ Game"),
        ("right_square_bracket", "game_next", "Game ▶"),
        ("g", "game_restart", "Restart game"),
    ]

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self.state = AppState(orientation_mode=config.orientation_mode)
        self.processor: Optional[BleNotificationProcessor] = None
        self.mapper = create_orientation_mapper(config.orientation_mode)
        self.recorder = Recorder(config.csv_path, config.log_path)
        self.gestures = GestureDetector()
        self.games: List[Game] = build_games()
        self._active_game_index = 0

        maxlen = max(120, int(config.history_seconds * 110))
        self._gyro_x: Deque[float] = deque(maxlen=maxlen)
        self._gyro_y: Deque[float] = deque(maxlen=maxlen)
        self._gyro_z: Deque[float] = deque(maxlen=maxlen)
        self._accel_mag: Deque[float] = deque(maxlen=maxlen)
        self._sample_times: Deque[float] = deque(maxlen=200)
        self._notif_times: Deque[float] = deque(maxlen=200)
        self._notif_sizes: Counter = Counter()
        self._notif_bytes: int = 0
        self._pending_logs: Deque[str] = deque()
        self._ble: Optional[TrikiBleClient] = None
        self._sample_rate = 0.0
        self._connect_mono: Optional[float] = None
        self._peak_gyro = 0.0
        self._peak_accel = 0.0
        self._gesture_label: Optional[str] = None
        self._button_pressed = False
        self._button_press_count = 0
        self._led_on = False

    # -- layout ----------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="overview"):
            with TabPane("Overview", id="overview"):
                with VerticalScroll():
                    yield Static(id="overview-body")
            with TabPane("GATT", id="gatt"):
                with VerticalScroll():
                    yield Static(id="gatt-body")
            with TabPane("IMU", id="imu"):
                with VerticalScroll():
                    yield Static(id="imu-body")
            with TabPane("Orientation", id="orient"):
                with VerticalScroll():
                    yield Static(id="orient-body")
            with TabPane("Games", id="games"):
                with VerticalScroll():
                    yield Static(id="games-body")
            with TabPane("Log", id="log"):
                yield RichLog(id="event-log", wrap=False, markup=False, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        self.title = "TrikiScope"
        self.sub_title = "disconnected"
        self.set_interval(1 / 15, self._tick)
        if self.config.auto_connect:
            self.action_connect()

    # -- BLE lifecycle ---------------------------------------------------------

    def action_connect(self) -> None:
        if self.state.conn_state in ("scanning", "connecting", "connected"):
            self._log("Already connecting/connected.")
            return
        self.processor = BleNotificationProcessor(
            self.config.gyro_scale,
            self.config.accel_scale,
            self.config.startup_discard_samples,
        )
        self.mapper = create_orientation_mapper(self.state.orientation_mode)
        self.mapper.reset_for_new_stream()
        self._gyro_x.clear()
        self._gyro_y.clear()
        self._gyro_z.clear()
        self._accel_mag.clear()
        self._sample_times.clear()
        self._notif_times.clear()
        self._notif_sizes.clear()
        self._notif_bytes = 0
        self._peak_gyro = 0.0
        self._peak_accel = 0.0
        self._connect_mono = None
        self._button_pressed = False
        self._button_press_count = 0
        self._led_on = False
        self.gestures.reset()
        game = self._current_game()
        if game is not None:
            game.reset()
        self.state.last_sample = None
        self.state.orientation = None

        callbacks = BleCallbacks(
            on_log=self._log,
            on_state=self._on_state,
            on_advertisement=self._on_advertisement,
            on_device_info=self._on_device_info,
            on_gatt=self._on_gatt,
            on_battery=self._on_battery,
            on_notification=self._on_notification,
        )
        self._ble = TrikiBleClient(self.config, callbacks)
        self.run_worker(self._ble.run(), exclusive=True, name="ble")

    def action_show_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab

    # -- games -----------------------------------------------------------------

    def _current_game(self) -> Optional[Game]:
        if not self.games:
            return None
        return self.games[self._active_game_index % len(self.games)]

    def _switch_game(self, delta: int) -> None:
        if not self.games:
            return
        self._active_game_index = (self._active_game_index + delta) % len(self.games)
        game = self._current_game()
        if game is not None:
            game.reset()
            self._log(f"Game: {game.name}")
        self.query_one(TabbedContent).active = "games"

    def action_game_prev(self) -> None:
        self._switch_game(-1)

    def action_game_next(self) -> None:
        self._switch_game(1)

    def action_game_restart(self) -> None:
        game = self._current_game()
        if game is not None:
            game.reset()
            self._log(f"Game restarted: {game.name}")

    def action_toggle_led(self) -> None:
        if self._ble is None or self.state.conn_state != "connected":
            self._log("LED: connect first.")
            return
        self._led_on = not self._led_on
        self.run_worker(self._ble.set_led(self._led_on), exclusive=False, name="led")

    def action_disconnect(self) -> None:
        if self._ble is not None:
            self._log("Disconnecting...")
            self._ble.stop()

    def action_reset_orientation(self) -> None:
        self.mapper.reset()
        self._log("Orientation zeroed at current pose.")

    def action_recalibrate(self) -> None:
        self.mapper.reset_for_new_stream()
        self._log("Recalibrating orientation (hold device still)...")

    def action_toggle_mode(self) -> None:
        new_mode = "complementary" if self.state.orientation_mode == "madgwick" else "madgwick"
        self.state.orientation_mode = new_mode
        self.mapper = create_orientation_mapper(new_mode)
        self.mapper.reset_for_new_stream()
        self._log(f"Orientation mode: {new_mode}")

    def action_toggle_record(self) -> None:
        if self.recorder.is_recording:
            self.recorder.stop()
            self._log(f"Recording stopped: {self.recorder.csv_path}")
        else:
            self.recorder.start()
            self._log(f"Recording to {self.recorder.csv_path}")

    def on_unmount(self) -> None:
        if self.recorder.is_recording:
            self.recorder.stop()
        if self._ble is not None:
            self._ble.stop()

    # -- BLE callbacks (run on the event loop) ---------------------------------

    def _log(self, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._pending_logs.append(f"{ts}  {message}")

    def _on_state(self, state: str) -> None:
        self.state.conn_state = state
        if state == "connected":
            self._connect_mono = time.monotonic()
        elif state in ("disconnected", "error"):
            self._connect_mono = None
        if self.recorder.is_recording:
            self.recorder.log_event(f"connection state: {state}")

    def _on_advertisement(self, adv: AdvertisementInfo) -> None:
        self.state.advertisement = adv

    def _on_device_info(self, info: DeviceInfo) -> None:
        self.state.device_info = info
        if info.battery_percent is not None:
            self.state.battery = info.battery_percent

    def _on_gatt(self, services: List[GattServiceInfo]) -> None:
        self.state.gatt = services

    def _on_battery(self, level: int) -> None:
        self.state.battery = level

    def _on_notification(self, data: bytes, timestamp: datetime) -> None:
        if self.processor is None:
            return
        self._notif_times.append(timestamp.timestamp())
        self._notif_sizes[len(data)] += 1
        self._notif_bytes += len(data)
        samples = self.processor.process(data, timestamp)
        game = self._current_game()
        for sample in samples:
            orientation = self.mapper.update(sample)
            self.state.last_sample = sample
            self.state.orientation = orientation
            self.state.calibrating = getattr(self.mapper, "calibrating", False)
            self._peak_gyro = max(self._peak_gyro, sample.gyro_magnitude)
            self._peak_accel = max(self._peak_accel, sample.accel_magnitude)
            if game is not None:
                game.on_sample(sample, orientation)
            if sample.button_pressed != self._button_pressed:
                self._button_pressed = sample.button_pressed
                if sample.button_pressed:
                    self._button_press_count += 1
                self._log(f"Button {'PRESSED' if sample.button_pressed else 'released'}")
                if game is not None:
                    game.on_button(sample.button_pressed)
                if self.recorder.is_recording:
                    self.recorder.log_event(f"button {'pressed' if sample.button_pressed else 'released'}")
            event = self.gestures.update(sample)
            if event is not None:
                if game is not None:
                    game.on_gesture(event.name)
                if self.recorder.is_recording:
                    self.recorder.log_event(f"gesture: {event.name} (mag={event.magnitude:.2f})")
            self._gyro_x.append(sample.gyro_x)
            self._gyro_y.append(sample.gyro_y)
            self._gyro_z.append(sample.gyro_z)
            self._accel_mag.append(sample.accel_magnitude)
            self._sample_times.append(sample.timestamp_utc.timestamp())
            if self.recorder.is_recording:
                self.recorder.write_sample(
                    sample, orientation.pitch, orientation.roll, orientation.yaw
                )

    # -- rendering -------------------------------------------------------------

    def _tick(self) -> None:
        # The render timer can fire while the DOM is still mounting or already
        # tearing down (e.g. on quit); skip gracefully if widgets aren't there.
        try:
            # Drain queued log lines.
            if self._pending_logs:
                log_widget = self.query_one("#event-log", RichLog)
                while self._pending_logs:
                    log_widget.write(self._pending_logs.popleft())

            self._sample_rate = self._compute_rate()
            self._gesture_label = self.gestures.active_label(time.time())
            self.sub_title = self._status_line()

            active = self.query_one(TabbedContent).active
            if active == "overview":
                self.query_one("#overview-body", Static).update(self._render_overview())
            elif active == "gatt":
                self.query_one("#gatt-body", Static).update(self._render_gatt())
            elif active == "imu":
                self.query_one("#imu-body", Static).update(self._render_imu())
            elif active == "orient":
                self.query_one("#orient-body", Static).update(self._render_orientation())
            elif active == "games":
                self.query_one("#games-body", Static).update(self._render_games())
        except NoMatches:
            return

    def _compute_rate(self) -> float:
        return self._rate_from(self._sample_times)

    def _notif_rate(self) -> float:
        return self._rate_from(self._notif_times)

    @staticmethod
    def _rate_from(times: Deque[float]) -> float:
        if len(times) < 2:
            return 0.0
        span = times[-1] - times[0]
        if span <= 0:
            return 0.0
        return (len(times) - 1) / span

    def _byte_rate(self) -> float:
        # Approximate using the notification-rate window's average packet size.
        rate = self._notif_rate()
        if rate <= 0 or not self._notif_sizes:
            return 0.0
        total = sum(self._notif_sizes.values())
        avg_size = self._notif_bytes / total if total else 0.0
        return rate * avg_size

    def _uptime_text(self) -> str:
        if self._connect_mono is None:
            return "--"
        secs = int(time.monotonic() - self._connect_mono)
        return f"{secs // 60:d}m {secs % 60:02d}s"

    def _status_line(self) -> str:
        rec = " ● REC" if self.recorder.is_recording else ""
        batt = f" | batt {self.state.battery}%" if self.state.battery is not None else ""
        btn = " | BTN" if self._button_pressed else ""
        led = " | LED" if self._led_on else ""
        return f"{self.state.conn_state} | {self.state.orientation_mode} | {self._sample_rate:.0f} Hz{batt}{btn}{led}{rec}"

    def _render_overview(self) -> Group:
        return Group(
            self._panel_connection(),
            self._panel_advertisement(),
            self._panel_device_info(),
        )

    def _panel_connection(self) -> Panel:
        colors = {
            "connected": "green",
            "scanning": "yellow",
            "connecting": "yellow",
            "disconnected": "red",
            "error": "red bold",
        }
        color = colors.get(self.state.conn_state, "white")
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold")
        table.add_column()
        table.add_row("State", Text(self.state.conn_state, style=color))
        table.add_row("Uptime", self._uptime_text())
        table.add_row("Sample rate", f"{self._sample_rate:.1f} Hz")
        table.add_row("Battery", "--" if self.state.battery is None else f"{self.state.battery}%")
        table.add_row("Orientation mode", self.state.orientation_mode)
        table.add_row("Recording", "yes" if self.recorder.is_recording else "no")
        table.add_row("LED", Text("ON", style="bold red") if self._led_on else "off")
        return Panel(table, title="Connection", border_style=color)

    def _panel_advertisement(self) -> Panel:
        adv = self.state.advertisement
        if adv is None:
            return Panel(Text("No advertisement captured yet.", style="dim"), title="Advertisement")
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold")
        table.add_column()
        table.add_row("Name", adv.name or "--")
        table.add_row("Address", adv.address)
        table.add_row("RSSI", "--" if adv.rssi is None else f"{adv.rssi} dBm")
        table.add_row("Tx power", "--" if adv.tx_power is None else f"{adv.tx_power} dBm")
        if adv.service_uuids:
            table.add_row("Service UUIDs", "\n".join(g.describe_service(u) for u in adv.service_uuids))
        for cid, data in adv.manufacturer_data.items():
            table.add_row(
                f"Mfr 0x{cid:04X}",
                f"{g.company_name(cid)}: {g.hexdump(data)}",
            )
        for uuid, data in adv.service_data.items():
            table.add_row(f"SvcData {g.describe_service(uuid)}", g.hexdump(data))
        return Panel(table, title="Advertisement", border_style="cyan")

    def _panel_device_info(self) -> Panel:
        info = self.state.device_info
        if info is None:
            return Panel(Text("Connect to read device information.", style="dim"), title="Device Information")
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold")
        table.add_column()
        rows = [
            ("Device name", info.device_name),
            ("Device ID", g.device_id_from_name(info.device_name)),
            ("Appearance", info.appearance),
            ("Manufacturer", info.manufacturer),
            ("Model", info.model_number),
            ("Serial", info.serial_number),
            ("Firmware", info.firmware_revision),
            ("Hardware", info.hardware_revision),
            ("Software", info.software_revision),
            ("System ID", info.system_id),
            ("PnP ID", info.pnp_id),
            ("MTU", str(info.mtu) if info.mtu else None),
        ]
        for label, value in rows:
            table.add_row(label, value if value else "--")
        return Panel(table, title="Device Information", border_style="magenta")

    def _render_gatt(self) -> Group:
        if not self.state.gatt:
            return Group(Text("No GATT database yet - connect first.", style="dim"))
        tree = Tree("GATT database")
        for svc in self.state.gatt:
            svc_node = tree.add(
                Text.assemble((svc.description, "bold cyan"), (f"  handle 0x{svc.handle:04X}", "dim"))
            )
            for char in svc.characteristics:
                props = ",".join(char.properties)
                label = Text.assemble(
                    (char.description, "bold"),
                    (f"  [{props}]", "yellow"),
                    (f"  handle 0x{char.handle:04X}", "dim"),
                )
                char_node = svc_node.add(label)
                if char.value is not None:
                    hexs = g.hexdump(char.value)
                    meaning = g.interpret(char.uuid, char.value)
                    if meaning:
                        char_node.add(
                            Text.assemble(
                                ("value: ", "dim"), (f"{meaning}  ", "green"), (f"({hexs})", "dim")
                            )
                        )
                    else:
                        char_node.add(Text.assemble(("value: ", "dim"), (hexs, "green")))
                elif char.read_error:
                    char_node.add(Text(f"read error: {char.read_error}", style="red"))
                for desc in char.descriptors:
                    char_node.add(Text(f"descriptor: {desc.description} (0x{desc.handle:04X})", style="dim"))
        return Group(tree)

    def _render_imu(self) -> Group:
        sample = self.state.last_sample
        if sample is None:
            return Group(Text("No IMU samples yet. Connect and start the stream.", style="dim"))

        values = Table(title=None, expand=False)
        values.add_column("Axis", style="bold")
        values.add_column("Gyro (deg/s)", justify="right")
        values.add_column("raw", justify="right", style="dim")
        values.add_column("Accel (g)", justify="right")
        values.add_column("raw", justify="right", style="dim")
        values.add_row("X", f"{sample.gyro_x:+8.2f}", str(sample.raw_gyro_x), f"{sample.accel_x:+7.3f}", str(sample.raw_accel_x))
        values.add_row("Y", f"{sample.gyro_y:+8.2f}", str(sample.raw_gyro_y), f"{sample.accel_y:+7.3f}", str(sample.raw_accel_y))
        values.add_row("Z", f"{sample.gyro_z:+8.2f}", str(sample.raw_gyro_z), f"{sample.accel_z:+7.3f}", str(sample.raw_accel_z))
        values.add_row(
            "|mag|",
            f"{sample.gyro_magnitude:8.2f}",
            "",
            f"{sample.accel_magnitude:7.3f}",
            "",
        )

        spark_w = 48
        charts = Table.grid(padding=(0, 2))
        charts.add_column(style="bold")
        charts.add_column()
        charts.add_row("gyro X", Text(render.sparkline(self._gyro_x, spark_w), style="cyan"))
        charts.add_row("gyro Y", Text(render.sparkline(self._gyro_y, spark_w), style="green"))
        charts.add_row("gyro Z", Text(render.sparkline(self._gyro_z, spark_w), style="magenta"))
        charts.add_row("accel |mag|", Text(render.sparkline(self._accel_mag, spark_w), style="yellow"))

        stats = self.processor.stats if self.processor else None
        stats_table = Table.grid(padding=(0, 2))
        stats_table.add_column(style="bold")
        stats_table.add_column()
        if stats:
            stats_table.add_row("Notifications", str(stats.notification_count))
            stats_table.add_row("Frames parsed", str(stats.parsed_frame_count))
            stats_table.add_row("Samples written", str(stats.written_sample_count))
            stats_table.add_row("Startup discarded", str(stats.discarded_startup_sample_count))
            stats_table.add_row("Dropped bytes", str(stats.dropped_byte_count))
            stats_table.add_row("Notif gap last", f"{stats.last_notification_gap_ms:.1f} ms")
            stats_table.add_row("Notif gap max", f"{stats.max_notification_gap_ms:.1f} ms")
            stats_table.add_row("Sample rate", f"{self._sample_rate:.1f} Hz")
            stats_table.add_row("Notif rate", f"{self._notif_rate():.1f} /s")
            stats_table.add_row("Throughput", f"{self._byte_rate() / 1024:.2f} KiB/s")
            if self._notif_sizes:
                sizes = ", ".join(
                    f"{size}B×{count}" for size, count in sorted(self._notif_sizes.items())
                )
                stats_table.add_row("Packet sizes", sizes)
        frame_hex = (
            g.hexdump(
                bytes([0x22, sample.status])
                + sample.raw_gyro_x.to_bytes(2, "little", signed=True)
                + sample.raw_gyro_y.to_bytes(2, "little", signed=True)
                + sample.raw_gyro_z.to_bytes(2, "little", signed=True)
                + sample.raw_accel_x.to_bytes(2, "little", signed=True)
                + sample.raw_accel_y.to_bytes(2, "little", signed=True)
                + sample.raw_accel_z.to_bytes(2, "little", signed=True)
            )
        )
        motion = Table.grid(padding=(0, 2))
        motion.add_column(style="bold")
        motion.add_column()
        if self._button_pressed:
            motion.add_row("Button", Text("PRESSED", style="bold white on dark_green"))
        else:
            motion.add_row("Button", Text("released", style="dim"))
        motion.add_row("Button presses", str(self._button_press_count))
        if self._gesture_label:
            motion.add_row("Gesture", Text(self._gesture_label, style="bold yellow on dark_red"))
        else:
            motion.add_row("Gesture", Text("--", style="dim"))
        motion.add_row("Peak gyro", f"{self._peak_gyro:.1f} deg/s")
        motion.add_row("Peak accel", f"{self._peak_accel:.2f} g")

        return Group(
            Panel(values, title="Live IMU", border_style="cyan"),
            Panel(motion, title="Motion / gestures", border_style="red"),
            Panel(charts, title=f"Activity (last {spark_w} samples)", border_style="green"),
            Panel(stats_table, title="Stream statistics", border_style="blue"),
            Panel(Text(frame_hex, style="dim"), title="Last decoded frame", border_style="grey50"),
        )

    def _render_orientation(self) -> Group:
        orientation = self.state.orientation
        q = orientation.quaternion if orientation else Quaternion.identity()
        cube_lines = render.render_cube(q, width=50, height=22)
        cube_text = Text("\n".join(cube_lines), style="cyan")

        info = Table.grid(padding=(0, 2))
        info.add_column(style="bold")
        info.add_column()
        if orientation:
            info.add_row("Pitch", f"{orientation.pitch:+7.1f}°  " + render.bar(orientation.pitch, -90, 90, 18))
            info.add_row("Roll", f"{orientation.roll:+7.1f}°  " + render.bar(orientation.roll, -180, 180, 18))
            info.add_row("Yaw", f"{orientation.yaw:+7.1f}°  " + render.bar(orientation.yaw, -180, 180, 18))
            info.add_row("Quaternion", f"x={q.x:+.3f} y={q.y:+.3f} z={q.z:+.3f} w={q.w:+.3f}")
        else:
            info.add_row("Status", "waiting for samples")
        info.add_row("Mode", self.state.orientation_mode)
        info.add_row("Calibrating", "yes (hold still)" if self.state.calibrating else "no")

        return Group(
            Panel(cube_text, title="Orientation (wireframe)", border_style="cyan"),
            Panel(info, title="Angles", border_style="magenta"),
            Text("r = zero at current pose   z = recalibrate   m = switch filter", style="dim"),
        )

    def _render_games(self) -> Group:
        game = self._current_game()
        if game is None:
            return Group(Text("No games available.", style="dim"))

        selector = Text()
        for i, gm in enumerate(self.games):
            if i == self._active_game_index:
                selector.append(f" {gm.name} ", style="bold black on cyan")
            else:
                selector.append(f" {gm.name} ", style="dim")
            selector.append("  ")

        if self.state.conn_state != "connected" or self.state.last_sample is None:
            body = Text("Connect (press c) and hold the cap still to calibrate, then play.", style="yellow")
        elif self.state.calibrating:
            body = Text("Calibrating... hold the cap still for a moment.", style="yellow")
        else:
            body = game.render(60, 22)

        return Group(
            Panel(selector, title="Games", border_style="cyan"),
            Panel(body, title=game.name, border_style="green"),
            Text(f"[ ] switch game    g restart    {game.help}", style="dim"),
        )
