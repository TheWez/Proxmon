import functools
import json
import logging
import os
import threading
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from rich.columns import Columns
from rich.console import Group
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Button, DataTable, Footer, Header, Static

from utils import (
    draw_vertical_bar_chart,
    find_vm_ip_address,
    format_gpu_summary,
    format_used_total,
    get_cpu_temperature,
    get_data_from_proxapi,
    get_gpu_stats,
    get_guests,
    get_vm_config,
    get_vm_data,
    vm_power_action,
)

# Configure logging
logging.basicConfig(level=logging.ERROR)
log_file_path = Path.cwd() / "proxmon.log"
file_handler = logging.FileHandler(log_file_path)
file_handler.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
file_handler.setFormatter(formatter)
logging.getLogger().addHandler(file_handler)

# Load environment variables
load_dotenv()

# Proxmox API configuration
PROXMOX_API_URL = os.getenv("PROXMOX_HOST")
API_TOKEN_ID = os.getenv("TOKEN_ID")
API_TOKEN_SECRET = os.getenv("TOKEN_SECRET")
NODE = os.getenv("NODE")
SSH_HOST = os.getenv("SSH_HOST")
SSH_PORT = int(os.getenv("SSH_PORT", 22))
SSH_USER = os.getenv("SSH_USER")
SSH_PASSWORD = os.getenv("SSH_PASSWORD")

# Disable warnings for unverified HTTPS requests
requests.packages.urllib3.disable_warnings(
    requests.packages.urllib3.exceptions.InsecureRequestWarning
)

# Number of samples kept for the GPU history charts
GPU_HISTORY = 100

# Global variables
selected_vm = {
    "vmid": None,
    "type": None,
    "name": None,
    "status": None,
}


def coalesced(fn):
    """Run a refresh method on one thread at a time.

    A call made while a previous one is still running doesn't pile up (or get
    dropped): it is remembered and the method runs once more as soon as the
    current run finishes. Slow API responses therefore can't stack up requests
    or starve the refresh.
    """

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        state = self.__dict__.setdefault(
            f"_coalesce_{fn.__name__}",
            {"lock": threading.Lock(), "running": False, "pending": False},
        )
        with state["lock"]:
            if state["running"]:
                state["pending"] = True
                return
            state["running"] = True
        try:
            while True:
                fn(self, *args, **kwargs)
                with state["lock"]:
                    if not state["pending"]:
                        state["running"] = False
                        return
                    state["pending"] = False
        except BaseException:
            with state["lock"]:
                state["running"] = False
                state["pending"] = False
            raise

    return wrapper


def describe_error(exc):
    """Short, human-readable description of a refresh failure."""
    if isinstance(exc, requests.Timeout):
        text = "Proxmox API timed out"
    elif isinstance(exc, requests.ConnectionError):
        text = "Cannot reach Proxmox API"
    elif isinstance(exc, requests.HTTPError) and exc.response is not None:
        text = f"Proxmox API returned {exc.response.status_code} {exc.response.reason}"
    else:
        text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= 120 else text[:117] + "..."


def guarded(fn):
    """Report failures of a background refresh instead of letting them crash the app.

    The last good data stays on screen; the app shows a notice (once, when the
    problem starts and again when it clears) and keeps retrying on its timers.
    """

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            result = fn(self, *args, **kwargs)
        except Exception as exc:
            self._report_failure(fn.__name__, exc)
        else:
            self._report_success(fn.__name__)
            return result

    return wrapper


class ConfirmScreen(ModalScreen[bool]):
    """Small yes/no dialog. "No" has the focus, so Enter cancels."""

    BINDINGS = [
        ("y", "answer(True)", "Yes"),
        ("n", "answer(False)", "No"),
        ("escape", "answer(False)", "Cancel"),
    ]

    def __init__(self, message):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm_box"):
            yield Static(self.message, id="confirm_message")
            with Horizontal(id="confirm_buttons"):
                yield Button("Yes (y)", variant="error", id="confirm_yes")
                yield Button("No (n)", variant="primary", id="confirm_no")

    def on_mount(self):
        self.query_one("#confirm_no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm_yes")

    def action_answer(self, answer: bool) -> None:
        self.dismiss(answer)


class ProxmonApp(App):
    CSS_PATH = "styles.tcss"
    BINDINGS = [
        ("", "prev_entry", "Up"),
        ("", "next_entry", "Down"),
        ("󰌑", "", "Select"),
        ("s", "toggle_vm", "Start/Stop"),
        ("x", "force_stop", "Force Stop"),
        ("Ctrl+Q", "quit", "Quit"),
    ]

    timer: Timer
    timer_rrd: Timer
    layout = Layout()
    vm_stats = {}
    gpu_history = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._failing = {}  # refresh name -> error text, while that refresh is failing
        self._failing_lock = threading.Lock()
        self._guests = []  # guests currently in the table, in row order

    def _report_failure(self, name, exc):
        """Called from a worker thread when a refresh fails."""
        text = describe_error(exc)
        with self._failing_lock:
            first_for_source = name not in self._failing
            was_healthy = not self._failing
            self._failing[name] = text
        if first_for_source:
            logging.getLogger().warning("%s failed: %s", name, text, exc_info=exc)
        if was_healthy:
            self._ui(self.notify, f"{text}. Showing last data, retrying...", severity="error", timeout=8)
        self._ui(self._show_status)

    def _report_success(self, name):
        """Called from a worker thread when a refresh succeeds."""
        with self._failing_lock:
            if name not in self._failing:
                return
            del self._failing[name]
            recovered = not self._failing
        if recovered:
            self._ui(self.notify, "Recovered: data is live again", timeout=4)
        self._ui(self._show_status)

    def _show_status(self):
        """Show the current problem (if any) under the title (UI thread)."""
        with self._failing_lock:
            problems = list(self._failing.values())
        self.sub_title = f"⚠ {problems[0]}" if problems else ""

    def _ui(self, func, *args, **kwargs):
        """Run func on the UI thread from a worker thread (no-op if app is closing)."""
        try:
            self.call_from_thread(func, *args, **kwargs)
        except RuntimeError:
            pass

    def compose(self) -> ComposeResult:
        yield Header("Proxmox Monitor")
        yield Container(
            Static("Proxmox Node Monitor", id="topbar"),
            id="topbar_container",
            classes="topbar_container",
        )
        yield VerticalScroll(DataTable(id="vm_table"), id="vm_table_vs")
        yield VerticalScroll(Static(id="gpu_charts"), id="gpu_charts_vs")
        yield VerticalScroll(Static(id="stats", expand=True))
        yield Footer()

    @work(thread=True)
    @coalesced
    @guarded
    def update_node_stats(self):
        """Fetch node statistics (worker thread), then display them."""
        temp = get_cpu_temperature()
        url = f"{PROXMOX_API_URL}/api2/json/nodes/{NODE}/status"
        node_data = get_data_from_proxapi(url)
        gpus = get_gpu_stats()
        self._ui(self.render_node_stats, temp, node_data, gpus)

    def render_node_stats(self, temp, node_data, gpus):
        """Display node statistics (UI thread)."""
        free_memory = node_data.get("memory").get("free", 0) / (1024 * 1024 * 1024)
        total_memory = node_data.get("memory").get("total", 0) / (1024 * 1024 * 1024)
        used_memory = round(total_memory - free_memory, 0)
        cpu_load = (
            float(node_data.get("loadavg")[0])
            / int(node_data.get("cpuinfo").get("cores"))
            * 100
        )
        uptime = int(node_data.get("uptime", 0))
        formatted_uptime = f"{uptime // 86400:02}:{(uptime % 86400) // 3600:02}:{(uptime % 3600) // 60:02}:{uptime % 60:02}"
        # disk_free = node_data.get("rootfs").get("free") / (1024 * 1024 * 1024)
        disk_total = node_data.get("rootfs").get("total") / (1024 * 1024 * 1024)
        disk_used = node_data.get("rootfs").get("used") / (1024 * 1024 * 1024)

        gpu_summary = format_gpu_summary(gpus)
        gpu_text = f"｜ {gpu_summary} " if gpu_summary else ""

        text = (
            f"   {node_data.get('pveversion')} "
            f"｜   CPU: {node_data.get('cpuinfo').get('model')} "
            f"｜ 󰽘 Cores: {node_data.get('cpuinfo').get('cores')} "
            f"｜ 󰄪 Load: {cpu_load:.0f}% "
            f"｜ 🌡️Temp: {str(temp.get('CPU', 'N/A'))} °C "
            f"{gpu_text}"
            f"｜   RAM: {used_memory:.0f}/{total_memory:.0f} GB "
            f"｜  Disk: {disk_used:.0f}/{disk_total:.0f} GB "
            f"｜ 󰔚 Uptime: {formatted_uptime} "
        )
        self.query_one("#topbar", Static).update(Text(text, justify="full", style=""))

    def on_mount(self):
        """Initialize the app and set up timers."""
        global selected_vm
        table = self.query_one("#vm_table", DataTable)
        table.add_columns(
            "ID",
            "Status",
            "Type",
            "Name",
            "Core",
            "CPU (%)",
            "RAM used/max (MB)",
            "Disk used/max (GB)",
            "NetIN (MB)",
            "NetOUT (MB)",
            "MAC",
            "IP",
        )
        table.cursor_type = "row"
        table.zebra_stripes = False
        table.border = True
        self.update_table()
        self.update_node_stats()

        self.timer = self.set_interval(10, self.update_table)
        self.timer_node_stats = self.set_interval(10, self.update_node_stats)
        self.timer_rrd = self.set_interval(2, self.update_rrd_data)
        self.update_gpu_charts()
        self.timer_gpu = self.set_interval(2, self.update_gpu_charts)

    @work(thread=True)
    @coalesced
    @guarded
    def update_gpu_charts(self):
        """Read the GPUs (worker thread), then update the history charts."""
        self._ui(self.render_gpu_charts, get_gpu_stats())

    def render_gpu_charts(self, gpus):
        """Update the node-level GPU, VRAM and GTT history charts (UI thread).

        The panel is hidden when no supported GPU is found.
        """
        container = self.query_one("#gpu_charts_vs")
        container.display = bool(gpus)
        if not gpus:
            return

        rows = []
        for i, gpu in enumerate(gpus):
            label = f"GPU{i}" if len(gpus) > 1 else "GPU"
            hist = self.gpu_history.setdefault(i, {"util": [], "vram": [], "gtt": []})
            samples = (
                ("util", gpu["util"]),
                ("vram", gpu["vram_used_gb"]),
                ("gtt", gpu.get("gtt_used_gb")),
            )
            for key, value in samples:
                if value is not None:
                    hist[key] = (hist[key] + [value])[-GPU_HISTORY:]

            has_gtt = bool(gpu.get("gtt_total_gb"))
            n_panels = 3 if has_gtt else 2
            # panel border/padding + axis labels + column gap ~= 15 chars
            chart_width = max(10, self.size.width // n_panels - 15)

            panels = [
                Panel(
                    draw_vertical_bar_chart(
                        hist["util"],
                        height=6,
                        chart_width=chart_width,
                        color="green",
                        decimal_places=0,
                        y_min=0,
                        y_max=100,
                    ),
                    title=f"{label} Usage: {gpu['util']:.0f}%",
                    border_style="blue",
                ),
                Panel(
                    draw_vertical_bar_chart(
                        hist["vram"],
                        height=6,
                        chart_width=chart_width,
                        color="cyan",
                        decimal_places=1,
                        y_min=0,
                        y_max=gpu["vram_total_gb"],
                    ),
                    title=(
                        f"{label} VRAM: {gpu['vram_used_gb']:.1f}"
                        f"/{gpu['vram_total_gb']:.1f} GB"
                    ),
                    border_style="blue",
                ),
            ]
            if has_gtt:
                panels.append(
                    Panel(
                        draw_vertical_bar_chart(
                            hist["gtt"],
                            height=6,
                            chart_width=chart_width,
                            color="yellow",
                            decimal_places=1,
                            y_min=0,
                            y_max=gpu["gtt_total_gb"],
                        ),
                        title=(
                            f"{label} GTT: {gpu['gtt_used_gb']:.1f}"
                            f"/{gpu['gtt_total_gb']:.1f} GB"
                        ),
                        border_style="blue",
                    )
                )
            rows.append(Columns(panels, equal=True, expand=True))

        self.query_one("#gpu_charts", Static).update(Group(*rows))

    def _highlighted_guest(self):
        """The guest on the highlighted table row, or None."""
        table = self.query_one("#vm_table", DataTable)
        if table.row_count and 0 <= table.cursor_row < len(self._guests):
            return self._guests[table.cursor_row]
        return None

    @staticmethod
    def _guest_label(guest):
        return f"{guest['name']} ({guest['type']} {guest['vmid']})"

    def _confirm_then(self, message, action, guest):
        def answered(yes):
            if yes:
                self._power(guest, action)

        self.push_screen(ConfirmScreen(message), answered)

    def action_toggle_vm(self) -> None:
        """Start the highlighted guest if it is stopped, otherwise shut it down."""
        guest = self._highlighted_guest()
        if guest is None:
            self.notify("Select a VM/LXC first!", severity="warning")
        elif guest["status"] == "running":
            self._confirm_then(
                f"Shut down {self._guest_label(guest)}?\n(clean shutdown)",
                "shutdown",
                guest,
            )
        else:
            self._power(guest, "start")

    def action_force_stop(self) -> None:
        """Immediately stop the highlighted guest (like pulling the power cord)."""
        guest = self._highlighted_guest()
        if guest is None:
            self.notify("Select a VM/LXC first!", severity="warning")
        elif guest["status"] != "running":
            self.notify(f"{self._guest_label(guest)} is not running", severity="warning")
        else:
            self._confirm_then(
                f"Force stop {self._guest_label(guest)}?\n"
                "This cuts power without a clean shutdown.",
                "stop",
                guest,
            )

    @work(thread=True)
    def _power(self, guest, action):
        """Send a power action to Proxmox (worker thread)."""
        label = self._guest_label(guest)
        expected = "stopped" if action == "start" else "running"
        try:
            # the table can be up to 10s old: make sure we are acting on what the user saw
            current = get_vm_data(guest["vmid"], guest["type"].lower())["status"]
            if current != expected:
                self._ui(self.notify, f"{label} is now {current}; nothing done", severity="warning")
                self._ui(self.update_table)
                return
            vm_power_action(guest["vmid"], guest["type"].lower(), action)
        except Exception as exc:
            self._ui(
                self.notify,
                f"Could not {action} {label}: {describe_error(exc)}",
                severity="error",
            )
            return
        done = {"start": "Starting", "shutdown": "Shutting down", "stop": "Stopping"}[action]
        self._ui(self.notify, f"{done} {label}")
        # the task runs asynchronously on the host: refresh shortly after
        self._ui(self.set_timer, 2.0, self.update_table)

    @work(thread=True)
    @coalesced
    @guarded
    def update_table(self):
        """Fetch the VM and LXC data (worker thread), then show it in the table."""
        vm_data = get_guests()
        ip_data = find_vm_ip_address(vm_data)
        self._ui(self.render_table, vm_data, ip_data)

    def render_table(self, vm_data, ip_data):
        """Show the VM and LXC data in the table (UI thread)."""
        table = self.query_one("#vm_table", DataTable)

        # Remember which guest the cursor is on, so it stays on that guest even
        # if guests are added or removed above it
        previous_row = table.cursor_row
        previous_vmid = (
            self._guests[previous_row]["vmid"]
            if 0 <= previous_row < len(self._guests)
            else None
        )
        self._guests = vm_data

        table.clear()

        for idx, data in enumerate(vm_data):
            table.add_row(
                str(data["vmid"]),
                "🟢 Running" if data["status"] == "running" else "🔴 Stopped",
                data["type"],
                data["name"],
                "  " + str(data.get("cpus", 0)),
                str(round(data.get("cpu", 0) * 100, 2)),
                format_used_total(data.get("mem", 0), data.get("maxmem", 0), 1024**2),
                format_used_total(
                    data.get("disk", 0), data.get("maxdisk", 0), 1024**3, decimals=1
                ),
                str(round(data.get("netin", 0) / (1024 * 1024), 2)),
                str(round(data.get("netout", 0) / (1024 * 1024), 2)),
                ip_data.get(data["vmid"], {}).get("mac") or "N/A",
                ip_data.get(data["vmid"], {}).get("ip") or "N/A",
                key=str(data["vmid"]),
            )

        # Restore the cursor position
        vmids = [data["vmid"] for data in vm_data]
        if previous_vmid in vmids:
            table.move_cursor(row=vmids.index(previous_vmid))
        elif vm_data:
            table.move_cursor(row=min(max(previous_row, 0), len(vm_data) - 1))

        table.focus()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """The highlighted row is the selected VM/LXC: show its stats."""
        global selected_vm
        if not 0 <= event.cursor_row < len(self._guests):
            return
        guest = self._guests[event.cursor_row]
        changed = selected_vm["vmid"] != guest["vmid"]
        selected_vm.update(
            vmid=guest["vmid"],
            type=guest["type"].lower(),
            name=guest["name"],
            status=guest["status"],
        )
        if changed:
            self.update_rrd_data()

    @work(thread=True)
    @coalesced
    @guarded
    def update_rrd_data(self):
        """Fetch stats for the selected VM or LXC (worker thread), then show them."""
        selected = dict(selected_vm)  # snapshot: the selection can change under us
        if selected["vmid"] is None:
            return
        data = get_vm_data(selected["vmid"], selected["type"])
        config = get_vm_config(selected["vmid"], selected["type"])
        self._ui(self.render_rrd_data, selected, data, config)

    def render_rrd_data(self, selected, data, config):
        """Show stats for the selected VM or LXC (UI thread)."""
        global selected_vm
        if selected["vmid"] != selected_vm["vmid"]:
            return  # selection changed while fetching; the next refresh will catch up

        vmid = selected["vmid"]
        vm_type = selected["type"]
        vm_name = selected["name"]
        ts = time.time()
        cpu = data.get("cpu", data.get("maxcpu", 0)) * 100
        mem = data.get("mem", data.get("maxmem", 0)) / (1024 * 1024)
        netin = data.get("netin", 0) / (1024 * 1024)
        netout = data.get("netout", 0) / (1024 * 1024)

        if vmid not in self.vm_stats:
            self.vm_stats[vmid] = {
                "ts": [],
                "cpu": [],
                "mem": [],
                "netin": [],
                "netout": [],
            }

        # Update stats and keep only the last 100 values
        self.vm_stats[vmid]["ts"].append(ts)
        self.vm_stats[vmid]["ts"] = self.vm_stats[vmid]["ts"][-100:]

        self.vm_stats[vmid]["cpu"].append(cpu)
        self.vm_stats[vmid]["cpu"] = self.vm_stats[vmid]["cpu"][-100:]

        self.vm_stats[vmid]["mem"].append(mem)
        self.vm_stats[vmid]["mem"] = self.vm_stats[vmid]["mem"][-100:]

        self.vm_stats[vmid]["netin"].append(netin)
        self.vm_stats[vmid]["netin"] = self.vm_stats[vmid]["netin"][-100:]

        self.vm_stats[vmid]["netout"].append(netout)
        self.vm_stats[vmid]["netout"] = self.vm_stats[vmid]["netout"][-100:]

        layout = self.stats_layout()
        vm_type_icon = "" if vm_type == "lxc" else ""
        layout["stat_header"].update(
            Panel(
                f"VMID: {vmid} Type: {vm_type_icon}  {vm_type.upper()}"
                + f" Name: {vm_name} CPU: 󰓅 {cpu:.1f} % Mem:   {mem:.1f} MB  NetIn: 󰅢  MB {netin:.1f}  Netout: 󰅧  {netout:.1f} MB",
                title="Stats",
                border_style="blue",
            )
        )

        if data.get("status") == "running":
            layout["cpu"].update(
                Panel(
                    draw_vertical_bar_chart(
                        self.vm_stats[vmid]["cpu"],
                        height=8,
                        chart_width=90,
                        color="green",
                    ),
                    title="CPU Usage",
                    border_style="blue",
                )
            )
            layout["mem"].update(
                Panel(
                    draw_vertical_bar_chart(
                        self.vm_stats[vmid]["mem"],
                        height=8,
                        chart_width=90,
                        color="cyan",
                        decimal_places=0,
                    ),
                    title="Memory Usage",
                    border_style="blue",
                )
            )
            layout["netin"].update(
                Panel(
                    draw_vertical_bar_chart(
                        self.vm_stats[vmid]["netin"],
                        height=8,
                        chart_width=90,
                        color="yellow",
                        decimal_places=1,
                        char=".",
                    ),
                    title="Network In",
                    border_style="blue",
                )
            )
            layout["netout"].update(
                Panel(
                    draw_vertical_bar_chart(
                        self.vm_stats[vmid]["netout"],
                        height=8,
                        chart_width=90,
                        color="dodger_blue2",
                        decimal_places=1,
                        char=".",
                    ),
                    title="Network Out",
                    border_style="blue",
                )
            )
        else:
            layout["cpu"].update(
                Panel("VM/LXC Not Running!", title="CPU Usage", border_style="blue")
            )
            layout["mem"].update(
                Panel("VM/LXC Not Running!", title="Memory Usage", border_style="blue")
            )
            layout["netin"].update(
                Panel("VM/LXC Not Running!", title="Network In", border_style="blue")
            )
            layout["netout"].update(
                Panel("VM/LXC Not Running!", title="Network Out", border_style="blue")
            )

        layout["misc"].update(
            Panel(
                json.dumps(config, indent=4, sort_keys=True),
                border_style="magenta",
            )
        )
        self.query_one("#stats", Static).update(layout)

    def stats_layout(self):
        """Define the layout for statistics display."""
        layout = self.layout
        layout.split(
            Layout(name="stat_header", size=3),
            Layout(name="main"),
            Layout(name="misc"),
        )
        layout["main"].split_column(
            Layout(name="process"),
            Layout(name="network"),
        )
        layout["process"].split_row(
            Layout(name="cpu"),
            Layout(name="mem"),
        )
        layout["network"].split_row(
            Layout(name="netin"),
            Layout(name="netout"),
        )
        return layout


if __name__ == "__main__":
    app = ProxmonApp()
    app.run()
