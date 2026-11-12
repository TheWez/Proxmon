"""A fake Proxmox VE, faked at the HTTP layer (requests.get / requests.post).

Two nodes: pve1 (guests 100, 101, 200, 201) and pve2 (guests 300, 400). Tests
can make it slow (`latency`), make every call fail (`fail`, `fail_post`) or only
those for one node (`fail_node`), and inspect `counts` and `posts`.
"""

import collections
import re
import threading
import time

import requests

GiB = 2**30
MiB = 2**20

ARP_OUTPUT = (
    "? (10.0.0.1) at AA:BB:CC:00:00:01 [ether] on vmbr0\n"
    "? (10.0.0.2) at aa:bb:cc:00:00:03 [ether] on vmbr0\n"
)


def _guest(**kw):
    return {**dict(cpu=0, mem=0, disk=0, netin=0, netout=0), **kw}


def initial_state():
    return {
        ("pve1", "qemu", 100): _guest(name="vm-a", status="running", cpus=2, cpu=0.25, mem=1 * GiB,
                                      maxmem=2 * GiB, maxdisk=32 * GiB, netin=5e6, netout=2e6,
                                      mac="AA:BB:CC:00:00:01"),
        ("pve1", "qemu", 101): _guest(name="vm-b", status="stopped", cpus=4, maxmem=8 * GiB,
                                      maxdisk=64 * GiB, mac="AA:BB:CC:00:00:02"),
        ("pve1", "lxc", 200): _guest(name="ct-a", status="running", cpus=1, cpu=0.05, mem=256 * MiB,
                                     maxmem=512 * MiB, disk=3 * GiB, maxdisk=8 * GiB, netin=1e6,
                                     netout=1e6, mac="AA:BB:CC:00:00:03"),
        ("pve1", "lxc", 201): _guest(name="ct-stopped", status="stopped", cpus=1, maxmem=512 * MiB,
                                     maxdisk=4 * GiB, mac="AA:BB:CC:00:00:05"),
        ("pve2", "qemu", 300): _guest(name="vm-remote", status="running", cpus=2, cpu=0.5, mem=2 * GiB,
                                      maxmem=4 * GiB, maxdisk=16 * GiB, netin=9e6, netout=9e6,
                                      mac="AA:BB:CC:00:00:04"),
        ("pve2", "lxc", 400): _guest(name="ct-remote", status="running", cpus=2, cpu=0.1, mem=128 * MiB,
                                     maxmem=256 * MiB, disk=1 * GiB, maxdisk=2 * GiB, netin=3e6,
                                     netout=3e6, mac="AA:BB:CC:00:00:06"),
    }


class _Response:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.reason = data, status, "Error" if status >= 400 else "OK"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)

    def json(self):
        return {"data": self._data}


class FakePVE:
    def __init__(self):
        self.state = initial_state()
        self.counts = collections.Counter()  # requests per endpoint, "__total__" for all
        self.posts = []  # paths POSTed to
        self.latency = 0.0
        self.fail = False
        self.fail_post = False
        self.fail_node = None
        self.last_timeout = None
        self._lock = threading.Lock()

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _guest_dict(vmid, guest):
        keys = ("name", "status", "cpus", "cpu", "mem", "maxmem", "disk", "maxdisk", "netin", "netout")
        return {"vmid": vmid, "uptime": 100, **{k: guest[k] for k in keys}}

    def _maybe_fail(self, path, error):
        if self.latency:
            time.sleep(self.latency)
        if error or (self.fail_node and path.startswith(f"/nodes/{self.fail_node}/")):
            raise requests.ConnectionError("fake: host unreachable")

    # -- requests.get / requests.post replacements ----------------------------

    def get(self, url, headers=None, verify=None, timeout=None, **kw):
        path = url.split("/api2/json", 1)[1].split("?")[0]
        self.last_timeout = timeout
        with self._lock:
            self.counts[re.sub(r"/\d+/", "/{id}/", path)] += 1
            self.counts["__total__"] += 1
        self._maybe_fail(path, self.fail)

        if path == "/nodes":
            return _Response([{"node": "pve1", "status": "online"},
                              {"node": "pve2", "status": "online"},
                              {"node": "pve3", "status": "offline"}])
        m = re.fullmatch(r"/nodes/(\w+)/status", path)
        if m:
            return _Response({
                "pveversion": "pve-manager/9.0", "cpu": 0.37 if m.group(1) == "pve1" else 0.80,
                "loadavg": ["1.5", "1", "1"], "cpuinfo": {"cores": 8, "model": "FakeCPU"},
                "uptime": 90061, "memory": {"free": 8 * GiB, "total": 32 * GiB},
                "rootfs": {"total": 100 * GiB, "used": 20 * GiB},
            })
        m = re.fullmatch(r"/nodes/(\w+)/(qemu|lxc)", path)
        if m:
            node, kind = m.groups()
            return _Response([self._guest_dict(v, g) for (n, k, v), g in sorted(self.state.items())
                              if (n, k) == (node, kind)])
        m = re.fullmatch(r"/nodes/(\w+)/(qemu|lxc)/(\d+)/(status/current|config|interfaces|rrddata)", path)
        if m:
            node, kind, vmid, what = m.groups()
            vmid = int(vmid)
            guest = self.state.get((node, kind, vmid))
            if guest is None:
                return _Response(None, 500)
            if what == "status/current":
                return _Response(self._guest_dict(vmid, guest))
            if what == "rrddata":
                return _Response([{"time": 1000 + i, "cpu": 0.1 * i, "maxcpu": guest["cpus"],
                                   "mem": guest["mem"], "maxmem": guest["maxmem"],
                                   "netin": 100.0 * i, "netout": 50.0 * i} for i in range(5)])
            if what == "config":
                mac = guest["mac"]
                net0 = (f"virtio={mac},bridge=vmbr0" if kind == "qemu"
                        else f"name=eth0,hwaddr={mac},bridge=vmbr0")
                return _Response({"name": guest["name"], "cores": guest["cpus"], "net0": net0})
            if guest["status"] != "running":  # interfaces
                return _Response(None, 500)
            return _Response([{"name": "lo"},
                              {"name": "eth0", "hwaddr": guest["mac"], "inet": f"10.0.0.{vmid % 200}/24"}])
        return _Response(None, 404)

    def post(self, url, headers=None, verify=None, timeout=None, **kw):
        path = url.split("/api2/json", 1)[1]
        with self._lock:
            self.counts["POST " + re.sub(r"/\d+/", "/{id}/", path)] += 1
            self.posts.append(path)
        self._maybe_fail(path, self.fail_post)

        m = re.fullmatch(r"/nodes/(\w+)/(qemu|lxc)/(\d+)/status/(start|shutdown|stop)", path)
        if not m:
            return _Response(None, 404)
        node, kind, vmid, action = m.groups()
        guest = self.state[(node, kind, int(vmid))]
        guest["status"] = "running" if action == "start" else "stopped"
        if action != "start":
            guest["mem"] = guest["cpu"] = 0
        return _Response("UPID:fake")
