"""Headless tests of the Textual app against the fake Proxmox (see fakepve.py).

The app runs for real, in Textual's test driver, with a pilot that presses keys;
only the network, the GPU and the temperature sensor are faked.
"""

import asyncio

import pytest
import requests

import proxmon
import utils


# -- helpers that don't need an app ---------------------------------------------

def test_describe_error_is_short_and_readable():
    assert describe(requests.Timeout()) == "Proxmox API timed out"
    assert describe(requests.ConnectionError("boom")) == "Cannot reach Proxmox API"

    class Resp:
        status_code, reason = 401, "Unauthorized"

    assert describe(requests.HTTPError("x", response=Resp())) == "Proxmox API returned 401 Unauthorized"
    assert describe(ValueError("bad")) == "ValueError: bad"
    assert len(describe(ValueError("x" * 500))) == 120


def describe(exc):
    return proxmon.describe_error(exc)


class Worker:
    """Stand-in for the app: coalesced() keeps its state on the instance."""

    def __init__(self, work):
        self.work, self.runs = work, 0

    @proxmon.coalesced
    def refresh(self):
        self.runs += 1
        self.work(self)


def test_coalesced_runs_once_more_for_calls_made_while_running():
    def work(self):
        if self.runs == 1:  # three more requests arrive during the first run
            self.refresh()
            self.refresh()
            self.refresh()

    w = Worker(work)
    w.refresh()
    assert w.runs == 2  # not 4: the three were merged into one re-run


def test_coalesced_recovers_after_an_exception():
    def work(self):
        if self.runs == 1:
            raise RuntimeError("first run fails")

    w = Worker(work)
    with pytest.raises(RuntimeError):
        w.refresh()
    w.refresh()  # not stuck "running"
    assert w.runs == 2


# -- the running app -----------------------------------------------------------------

GPUS = [{"vendor": "amd", "name": "AMD GPU", "util": 42.0, "vram_used_gb": 0.2, "vram_total_gb": 0.5,
         "gtt_used_gb": 3.0, "gtt_total_gb": 16.0}]


def run_app(monkeypatch, scenario, size=(190, 55), settle=0.6):
    """Start the app (sensors faked), let it load, and await scenario(app, pilot)."""
    for module in (proxmon, utils):
        if hasattr(module, "get_gpu_stats"):
            monkeypatch.setattr(module, "get_gpu_stats", lambda: list(GPUS))
        if hasattr(module, "get_cpu_temperature"):
            monkeypatch.setattr(module, "get_cpu_temperature", lambda: {"CPU": "45.0"})

    async def main():
        app = proxmon.ProxmonApp()
        async with app.run_test(size=size) as pilot:
            await pilot.pause(settle)
            await scenario(app, pilot)

    asyncio.run(asyncio.wait_for(main(), 60))


def table(app):
    return app.query_one("#vm_table")


def row_ids(app):
    t = table(app)
    return [int(str(t.get_row_at(i)[0])) for i in range(t.row_count)]


def headers(app):
    return [str(c.label) for c in table(app).columns.values()]


def toasts(app):
    return [n.message for n in app._notifications]


def test_table_lists_every_guest_with_used_and_max_columns(monkeypatch, pve):
    async def scenario(app, pilot):
        assert row_ids(app) == [100, 101, 200, 201]
        assert "Node" not in headers(app)  # single node: no Node column
        cols = headers(app)
        by_id = {int(str(table(app).get_row_at(i)[0])): [str(c) for c in table(app).get_row_at(i)]
                 for i in range(4)}
        ram, disk = cols.index("RAM used/max (MB)"), cols.index("Disk used/max (GB)")
        assert by_id[100][ram].strip() == "1024/2048"
        assert by_id[101][ram].strip() == "-/8192"  # stopped: nothing in use
        assert by_id[200][disk].strip() == "3.0/8.0"

    run_app(monkeypatch, scenario)


def test_one_refresh_costs_two_requests_per_node(monkeypatch, pve):
    async def scenario(app, pilot):
        pve.counts.clear()
        app.update_table()
        await pilot.pause(0.8)
        # guest lists (qemu + lxc) only; the configs for the MAC lookup are cached
        assert pve.counts["/nodes/pve1/qemu"] == 1 and pve.counts["/nodes/pve1/lxc"] == 1
        assert pve.counts["__total__"] == 2

    run_app(monkeypatch, scenario)


def test_start_acts_at_once_on_a_stopped_guest(monkeypatch, pve):
    async def scenario(app, pilot):
        await pilot.press("down")  # 101, a stopped VM
        await pilot.press("s")
        await pilot.pause(0.5)
        assert pve.posts == ["/nodes/pve1/qemu/101/status/start"]
        assert any("Starting vm-b" in t for t in toasts(app))

    run_app(monkeypatch, scenario)


def test_shutdown_asks_first_and_no_is_the_default(monkeypatch, pve):
    async def scenario(app, pilot):
        await pilot.press("s")  # 100 is running
        await pilot.pause(0.3)
        assert isinstance(app.screen, proxmon.ConfirmScreen)
        await pilot.press("enter")  # "No" has the focus
        await pilot.pause(0.3)
        assert pve.posts == []
        await pilot.press("s", "y")
        await pilot.pause(0.5)
        assert pve.posts == ["/nodes/pve1/qemu/100/status/shutdown"]

    run_app(monkeypatch, scenario)


def test_force_stop_is_a_separate_confirmed_action(monkeypatch, pve):
    async def scenario(app, pilot):
        await pilot.press("x", "y")
        await pilot.pause(0.5)
        assert pve.posts == ["/nodes/pve1/qemu/100/status/stop"]
        await pilot.press("down")  # 101 is stopped: nothing to force-stop
        await pilot.press("x")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, proxmon.ConfirmScreen)
        assert any("not running" in t for t in toasts(app))

    run_app(monkeypatch, scenario)


def test_power_action_is_skipped_if_the_guest_changed_state_meanwhile(monkeypatch, pve):
    async def scenario(app, pilot):
        await pilot.press("down")  # the table shows 101 as stopped...
        pve.state[("pve1", "qemu", 101)]["status"] = "running"  # ...but someone started it
        await pilot.press("s")
        await pilot.pause(0.5)
        assert pve.posts == []
        assert any("is now running" in t for t in toasts(app))

    run_app(monkeypatch, scenario)


def test_failed_power_action_is_reported(monkeypatch, pve):
    async def scenario(app, pilot):
        await pilot.press("down")
        pve.fail_post = True
        await pilot.press("s")
        await pilot.pause(0.5)
        assert any("Could not start" in t and "Cannot reach" in t for t in toasts(app))

    run_app(monkeypatch, scenario)


def refresh_everything(app):
    """Run each periodic refresh now, rather than waiting for its timer."""
    app.update_table()
    app.update_node_stats()
    app.update_rrd_data()


def test_outage_keeps_the_last_data_and_recovers(monkeypatch, pve):
    async def scenario(app, pilot):
        pve.fail = True
        refresh_everything(app)
        await pilot.pause(0.8)
        assert row_ids(app) == [100, 101, 200, 201]  # last good data stays
        assert app.sub_title.startswith("⚠")
        assert sum("Cannot reach" in t for t in toasts(app)) == 1  # one toast, not one per failure

        app.update_table()
        await pilot.pause(0.5)
        assert sum("Cannot reach" in t for t in toasts(app)) == 1  # still just one

        pve.fail = False
        refresh_everything(app)  # every source must succeed once for the warning to clear
        await pilot.pause(1.0)
        assert app.sub_title == ""
        assert any("Recovered" in t for t in toasts(app))

    run_app(monkeypatch, scenario)


def test_ui_stays_responsive_while_the_api_is_slow(monkeypatch, pve):
    async def scenario(app, pilot):
        pve.latency = 0.4
        loop = asyncio.get_running_loop()
        worst, last = 0.0, loop.time()
        app.update_table()
        for _ in range(40):  # ~2 s in which every refresh is waiting on the API
            await asyncio.sleep(0.05)
            now = loop.time()
            worst, last = max(worst, now - last), now
        assert worst < 0.3, f"event loop was blocked for {worst:.2f}s"

    run_app(monkeypatch, scenario)


def test_sorting_cycles_columns_and_follows_the_highlighted_guest(monkeypatch, pve):
    async def scenario(app, pilot):
        assert headers(app)[0] == "ID ▲"
        await pilot.press("down")
        guest = lambda: app._guests[table(app).cursor_row]["vmid"]  # noqa: E731
        assert guest() == 101
        await pilot.press("o")  # next column: Status
        await pilot.pause(0.3)
        assert [h for h in headers(app) if "▲" in h] == ["Status ▲"]
        assert row_ids(app) == [100, 200, 101, 201]  # running first, ties by ID
        assert guest() == 101  # the highlight moved with the guest
        await pilot.press("r")
        await pilot.pause(0.3)
        assert row_ids(app) == [201, 101, 200, 100]
        assert "Status ▼" in headers(app)

    run_app(monkeypatch, scenario)


def test_filter_box_narrows_the_table_and_does_not_trigger_shortcuts(monkeypatch, pve):
    async def scenario(app, pilot):
        await pilot.press("slash")
        box = app.query_one("#filter")
        assert box.display and box.has_focus
        for ch in "stopped":  # contains "s", which starts/stops a guest outside the box
            await pilot.press(ch)
        await pilot.pause(0.3)
        assert pve.posts == []
        assert row_ids(app) == [101, 201]
        await pilot.press("enter")
        assert table(app).has_focus and row_ids(app) == [101, 201]
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert not box.display and row_ids(app) == [100, 101, 200, 201]

    run_app(monkeypatch, scenario)


@pytest.mark.parametrize("nodes", ["pve1,pve2", "*"])
def test_multi_node_mode_shows_every_node(monkeypatch, pve, nodes):
    monkeypatch.setattr(utils, "NODES", nodes)

    async def scenario(app, pilot):
        assert row_ids(app) == [100, 101, 200, 201, 300, 400]
        assert "Node" in headers(app)
        await pilot.press("slash")
        for ch in "pve2":
            await pilot.press(ch)
        await pilot.pause(0.3)
        assert row_ids(app) == [300, 400]
        # a guest on the other node is controlled through that node's endpoint
        await pilot.press("enter")
        await pilot.press("s", "y")
        await pilot.pause(0.5)
        assert pve.posts[-1].startswith("/nodes/pve2/")

    run_app(monkeypatch, scenario)


def test_one_unreachable_node_does_not_hide_the_others(monkeypatch, pve):
    monkeypatch.setattr(utils, "NODES", "pve1,pve2")

    async def scenario(app, pilot):
        pve.fail_node = "pve2"
        app.update_table()
        await pilot.pause(0.8)
        assert app.sub_title.startswith("⚠") and "pve2" in app.sub_title
        assert {100, 101, 200, 201} <= set(row_ids(app))

        pve.fail_node = None
        app.update_table()
        await pilot.pause(0.8)
        assert app.sub_title == ""
        assert row_ids(app) == [100, 101, 200, 201, 300, 400]

    run_app(monkeypatch, scenario)
