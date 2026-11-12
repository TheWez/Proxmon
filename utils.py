import logging
import os
import re
import shutil
import subprocess
import threading
import time
from functools import lru_cache
from pathlib import Path

import paramiko
import requests
from dotenv import load_dotenv

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

HEADERS = {"Authorization": f"PVEAPIToken={API_TOKEN_ID}={API_TOKEN_SECRET}"}

# Seconds to wait for the Proxmox API before giving up (an unreachable host
# would otherwise hang a refresh indefinitely).
API_TIMEOUT = 10

# Global variables
TABLE_CURSOR = dict()
selected_vm = {
    "vmid": None,
    "type": None,
    "name": None,
    "status": None,
}


_ssh_lock = threading.Lock()
_ssh_client = None
_ssh_target = None


def _close_ssh():
    """Drop the cached SSH connection (caller holds _ssh_lock)."""
    global _ssh_client, _ssh_target
    if _ssh_client is not None:
        try:
            _ssh_client.close()
        except Exception:
            pass
    _ssh_client = _ssh_target = None


def ssh_execute_command(host, username, password, command, port=22):
    """SSH into a Proxmox node and execute a command.

    The connection is kept open and reused between calls (this runs every few
    seconds), and is re-established automatically if it has dropped.
    """
    global _ssh_client, _ssh_target
    if not host:
        return None

    target = (host, port, username)
    with _ssh_lock:
        try:
            for attempt in (1, 2):
                transport = _ssh_client.get_transport() if _ssh_client else None
                if _ssh_target != target or transport is None or not transport.is_active():
                    _close_ssh()
                    client = paramiko.SSHClient()
                    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    client.connect(
                        host, port=port, username=username, password=password, timeout=10
                    )
                    _ssh_client, _ssh_target = client, target
                try:
                    _, stdout, stderr = _ssh_client.exec_command(command, timeout=10)
                    output = stdout.read().decode().strip()
                    error = stderr.read().decode().strip()
                    return output if output else error
                except Exception:
                    # a cached connection can go stale without the transport
                    # noticing: reconnect once before giving up
                    _close_ssh()
                    if attempt == 2:
                        raise
        except Exception as e:
            _close_ssh()
            return f"SSH Connection failed: {str(e)}"


# Fetch data from Proxmox API
def get_data_from_proxapi(url):
    """Fetch data from Proxmox API"""
    response = requests.get(url, headers=HEADERS, verify=False, timeout=API_TIMEOUT)
    response.raise_for_status()
    return response.json()["data"]


# Seconds a guest's config is reused before being fetched again. Configs rarely
# change, and they are needed on every refresh (MAC lookup, config panel).
CONFIG_TTL = 30

_cache = {}
_cache_lock = threading.Lock()


def cached(key, ttl, loader):
    """Return loader()'s result, reusing it for `ttl` seconds. Errors aren't cached."""
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
    value = loader()
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)
    return value


def get_vm_config(vmid, vm_type):
    """Fetch vm/lxc config (cached for CONFIG_TTL seconds)"""
    if vm_type == "vm":
        guesttype = "qemu"
    else:
        guesttype = "lxc"
    url = f"{PROXMOX_API_URL}/api2/json/nodes/{NODE}/{guesttype}/{vmid}/config"
    return cached(("config", url), CONFIG_TTL, lambda: get_data_from_proxapi(url))


def vm_power_action(vmid, vm_type, action):
    """Start, gracefully shut down, or force-stop a VM or LXC.

    action is "start", "shutdown" (ACPI/clean shutdown) or "stop" (immediate,
    like pulling the power cord). Returns the Proxmox task id.
    """
    if action not in ("start", "shutdown", "stop"):
        raise ValueError(f"unknown power action: {action}")
    guesttype = "qemu" if vm_type == "vm" else "lxc"
    url = f"{PROXMOX_API_URL}/api2/json/nodes/{NODE}/{guesttype}/{vmid}/status/{action}"
    response = requests.post(url, headers=HEADERS, verify=False, timeout=API_TIMEOUT)
    response.raise_for_status()
    return response.json()["data"]


def format_used_total(used, total, divisor, decimals=0):
    """Format a resource as "used/total" in the unit given by `divisor`.

    A value that is 0 or unknown is shown as "-": a stopped guest uses no
    memory, and Proxmox can't report disk usage for VMs (only for containers).
    """

    def fmt(value):
        return f"{value / divisor:.{decimals}f}" if value else "-"

    return f"{fmt(used)}/{fmt(total)}"


def get_guests():
    """All VMs and LXCs of the node, with their current status, in two requests.

    The list endpoints already include status, CPU, memory, disk and network
    counters, so no per-guest status request is needed. Each entry gets a
    "type" of "VM" or "LXC".
    """
    guests = []
    for kind, label in (("qemu", "VM"), ("lxc", "LXC")):
        for guest in get_data_from_proxapi(
            f"{PROXMOX_API_URL}/api2/json/nodes/{NODE}/{kind}"
        ):
            guests.append({**guest, "type": label})
    return sorted(guests, key=lambda g: g["vmid"])


def get_vmids():
    """Retrieve VM and LXC IDs from Proxmox."""
    vmids = {"vm": [], "lxc": []}
    vm_data = get_data_from_proxapi(f"{PROXMOX_API_URL}/api2/json/nodes/{NODE}/qemu")
    vmids["vm"].extend([vm["vmid"] for vm in vm_data])
    lxc_data = get_data_from_proxapi(f"{PROXMOX_API_URL}/api2/json/nodes/{NODE}/lxc")
    vmids["lxc"].extend([lxc["vmid"] for lxc in lxc_data])
    return vmids


def get_vm_data(vmid, type="vm"):
    if type == "vm":
        url = f"{PROXMOX_API_URL}/api2/json/nodes/{NODE}/qemu/{vmid}/status/current"
    elif type == "lxc":
        url = f"{PROXMOX_API_URL}/api2/json/nodes/{NODE}/lxc/{vmid}/status/current"
    else:
        return None
    return get_data_from_proxapi(url)


def get_rrd_data(vmid, type="vm"):
    if type == "vm":
        guesttype = "qemu"
    else:
        guesttype = "lxc"
    url = f"{PROXMOX_API_URL}/api2/json/nodes/{NODE}/{guesttype}/{vmid}/rrddata?timeframe=hour"
    return get_data_from_proxapi(url)


def draw_vertical_bar_chart(
    data,
    height=10,
    value_width=5,
    decimal_places=1,
    chart_width=None,
    color="white",
    char="░",
    max_output_width=200,
    y_min=None,
    y_max=None,
):
    """
    Draws a vertical bar chart with consistent padding and adjustable width.

    Args:
        data: A list of numerical values to chart.
        height: The height of the chart.
        value_width: The total width of the formatted numerical value.
        decimal_places: The number of decimal places to display.
        chart_width: The width of the bars in the chart. If None, it defaults to the length of the data.
        max_output_width: Maximum width of the output string.
        y_min, y_max: Optional fixed scale (e.g. 0 and 100 for a percentage);
            by default the scale is taken from the data.
    Returns:
        A string representation of the chart.
    """
    if chart_width is None:
        chart_width = len(data)

    max_value = max(data) if y_max is None else y_max
    min_value = min(data) if y_min is None else y_min
    range_value = max_value - min_value

    chart = [[" " for _ in range(chart_width)] for _ in range(height)]

    for i, value in enumerate(data):
        normalized_value = (
            max(0, min(height - 1, int((value - min_value) / range_value * (height - 1))))
            if range_value > 0
            else 0
        )
        if i < chart_width:
            for h in range(normalized_value + 1):
                if h < height:
                    chart[height - 1 - h][i] = f"{char}"

    chart_str = ""
    format_string = f">{value_width}.{decimal_places}f"
    for h in range(height):
        value_at_height = (
            min_value + (range_value * (height - 1 - h) / (height - 1))
            if range_value > 0
            else min_value
        )
        formatted_value = f"{value_at_height:{format_string}}"
        chart_str += f"{formatted_value} ┤ " + "".join(chart[h]) + "\n"

    chart_str += f"{' ' * value_width} ╰" + "─" * chart_width + "\n"
    # chart_str += "    " + ' '.join(str(i) for i in range(len(data))) + "\n"

    # Limit the output width
    lines = chart_str.splitlines()
    truncated_lines = []
    for line in lines:
        if len(line) > max_output_width:
            truncated_lines.append(line[:max_output_width] + "...")
        else:
            truncated_lines.append(line)
    chart_str = "\n".join(truncated_lines)

    return f"[{color}]{chart_str}[/{color}]"


def get_vmids_dict():
    """Retrieve VM and LXC IDs as a dictionary."""
    vmids = {}
    for vm in get_data_from_proxapi(f"{PROXMOX_API_URL}/api2/json/nodes/{NODE}/qemu"):
        vmids[vm["vmid"]] = {"type": "qemu"}
    for lxc in get_data_from_proxapi(f"{PROXMOX_API_URL}/api2/json/nodes/{NODE}/lxc"):
        vmids[lxc["vmid"]] = {"type": "lxc"}
    return vmids


def get_pve_subnets():
    """Retrieve Proxmox subnets."""
    url = f"{PROXMOX_API_URL}/api2/json/nodes/{NODE}/network/"
    data = get_data_from_proxapi(url)
    return [item["cidr"] for item in data if "cidr" in item]


def find_vm_ip_address(guests=None):
    """Find IP addresses of VMs based on their MAC addresses.

    `guests` is the list from get_guests(); it is fetched if not given.
    """
    if guests is None:
        guests = get_guests()
    vmid_data = {
        g["vmid"]: {"type": "qemu" if g["type"] == "VM" else "lxc"} for g in guests
    }
    ip_table = {}
    if SSH_HOST:
        neigh_data = ssh_execute_command(
            SSH_HOST, SSH_USER, SSH_PASSWORD, "arp -a", SSH_PORT
        )
        pattern = re.compile(r"\(\s*(\d+\.\d+\.\d+\.\d+)\s*\) at (\S+)")
        matches = pattern.findall(neigh_data)
        mac_to_ip = {mac.upper(): ip for ip, mac in matches}

        for vmid, info in vmid_data.items():
            try:
                config_data = get_vm_config(
                    vmid, "vm" if info["type"] == "qemu" else "lxc"
                )
            except requests.RequestException:
                logging.getLogger().warning("Could not read config of %s", vmid)
                continue
            net_config = config_data.get("net0", "")
            mac_match = re.search(
                r"(?:hwaddr=)?([0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5})", net_config
            )
            mac_address = mac_match.group(1) if mac_match else None
            if mac_address:
                ip_table[vmid] = {
                    "mac": mac_address,
                    "ip": mac_to_ip.get(mac_address, "N/A"),
                }
    else:
        for vmid, info in vmid_data.items():
            if info["type"] == "qemu":
                ip_table[vmid] = {
                    "mac": "N/A",
                    "ip": "N/A",
                }
                continue
            # url = f"{PROXMOX_API_URL}/api2/json/nodes/{NODE}/{info['type']}/{vmid}/config"
            url = f"{PROXMOX_API_URL}/api2/json/nodes/{NODE}/{info['type']}/{vmid}/interfaces"
            try:
                config_data = get_data_from_proxapi(url)
            except requests.RequestException:
                # e.g. a stopped container has no network interfaces to query
                continue
            for data in config_data:
                name = data.get("name", "")
                if name != "lo":
                    ip_table[vmid] = {
                        "mac": data.get("hwaddr"),
                        "ip": data.get("inet"),
                    }
    return ip_table


def get_cpu_temperature():
    """Get the CPU temperature (°C) from the local thermal zone (Proxmon runs on the PVE host)."""
    try:
        temp = float(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except (OSError, ValueError):
        logging.getLogger().exception("Could not read CPU temperature")
        temp = None
    return {"CPU": ("%.1f" % temp) if temp else "N/A"}


GPU_SYSFS_ROOT = Path("/sys/class/drm")
AMD_VENDOR_ID = "0x1002"


def _read_text(path):
    """Read and strip a small sysfs file; None if it can't be read."""
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


@lru_cache(maxsize=None)
def _pci_device_name(slot, fallback):
    """Look up a PCI device name with lspci (cached; falls back if unavailable)."""
    try:
        out = subprocess.run(
            ["lspci", "-s", slot], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        # "c5:00.0 VGA compatible controller: Advanced Micro Devices ..."
        return out.split(": ", 1)[1] if ": " in out else fallback
    except (OSError, subprocess.SubprocessError):
        return fallback


def get_amd_gpus(root=GPU_SYSFS_ROOT):
    """Read AMD GPU utilisation and VRAM from the amdgpu sysfs interface."""
    gpus = []
    for dev in sorted(Path(root).glob("card*/device")):
        if _read_text(dev / "vendor") != AMD_VENDOR_ID:
            continue
        try:
            util = float(_read_text(dev / "gpu_busy_percent"))
            used = float(_read_text(dev / "mem_info_vram_used"))
            total = float(_read_text(dev / "mem_info_vram_total"))
        except (TypeError, ValueError):
            continue  # not amdgpu, or files missing
        gtt_used = _read_text(dev / "mem_info_gtt_used")
        gtt_total = _read_text(dev / "mem_info_gtt_total")
        gpus.append(
            {
                "vendor": "amd",
                "name": _pci_device_name(dev.resolve().name, "AMD GPU"),
                "util": util,
                "vram_used_gb": used / 1024**3,
                "vram_total_gb": total / 1024**3,
                # GTT = system RAM the GPU can use; the real pool on iGPUs
                "gtt_used_gb": int(gtt_used) / 1024**3 if gtt_used else None,
                "gtt_total_gb": int(gtt_total) / 1024**3 if gtt_total else None,
            }
        )
    return gpus


def get_nvidia_gpus():
    """Read NVIDIA GPU utilisation and VRAM via nvidia-smi."""
    if not shutil.which("nvidia-smi"):
        return []
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 4:
            continue
        try:
            util, used, total = (float(x) for x in parts[1:])
        except ValueError:
            continue
        gpus.append(
            {
                "vendor": "nvidia",
                "name": parts[0],
                "util": util,
                "vram_used_gb": used / 1024,  # nvidia-smi reports MiB
                "vram_total_gb": total / 1024,
                "gtt_used_gb": None,
                "gtt_total_gb": None,
            }
        )
    return gpus


def get_gpu_stats():
    """Get GPU utilisation and VRAM usage from the local machine (the PVE host).

    Returns a list of dicts (vendor, name, util, vram_used_gb, vram_total_gb),
    empty if no supported GPU is found.
    """
    try:
        return get_nvidia_gpus() + get_amd_gpus()
    except Exception:
        logging.getLogger().exception("Could not read GPU stats")
        return []


def format_gpu_summary(gpus):
    """Format GPU stats for the top bar, e.g. 'GPU: 12% ｜ VRAM: 0.5/2.0 GB'."""
    parts = []
    for i, gpu in enumerate(gpus):
        label = f"GPU{i}" if len(gpus) > 1 else "GPU"
        text = (
            f"{label}: {gpu['util']:.0f}% "
            f"｜ VRAM: {gpu['vram_used_gb']:.1f}/{gpu['vram_total_gb']:.1f} GB"
        )
        if gpu.get("gtt_total_gb"):
            text += f" ｜ GTT: {gpu['gtt_used_gb']:.1f}/{gpu['gtt_total_gb']:.1f} GB"
        parts.append(text)
    return " ｜ ".join(parts)
