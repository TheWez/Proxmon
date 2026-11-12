"""Unit tests for the pure helpers and the Proxmox API layer in utils.py."""

import pytest
import requests

import utils
from fakepve import GiB, MiB


# -- format_used_total ----------------------------------------------------------

@pytest.mark.parametrize(
    "used, total, divisor, decimals, expected",
    [
        (1 * GiB, 2 * GiB, GiB, 0, "1/2"),
        (256 * MiB, 512 * MiB, MiB, 0, "256/512"),
        (0, 4 * GiB, GiB, 0, "-/4"),  # stopped guest: no memory in use
        (0, 0, GiB, 0, "-/-"),  # unknown (e.g. VM disk usage)
        (1.5 * GiB, 4 * GiB, GiB, 1, "1.5/4.0"),
    ],
)
def test_format_used_total(used, total, divisor, decimals, expected):
    assert utils.format_used_total(used, total, divisor, decimals) == expected


# -- sorting and filtering ------------------------------------------------------

GUESTS = [
    {"vmid": 100, "name": "Web", "type": "VM", "status": "running", "node": "pve1", "cpu": 0.5, "mem": 2 * GiB},
    {"vmid": 9, "name": "db", "type": "LXC", "status": "stopped", "node": "pve2", "cpu": 0.0, "mem": 0},
    {"vmid": 20, "name": "cache", "type": "LXC", "status": "running", "node": "pve1", "cpu": 0.1, "mem": GiB},
]
IP_DATA = {
    100: {"mac": "AA:BB:CC:00:00:01", "ip": "10.0.0.10/24"},
    9: {"mac": "aa:bb:cc:00:00:02", "ip": "10.0.0.9/24"},
    # 20: no entry at all
}


def ids(guests):
    return [g["vmid"] for g in guests]


def test_sort_by_id_is_numeric():
    assert ids(utils.sort_guests(GUESTS, IP_DATA, "ID")) == [9, 20, 100]
    assert ids(utils.sort_guests(GUESTS, IP_DATA, "ID", reverse=True)) == [100, 20, 9]


def test_sort_by_name_ignores_case():
    assert ids(utils.sort_guests(GUESTS, IP_DATA, "Name")) == [20, 9, 100]  # cache, db, Web


def test_sort_by_ip_is_numeric_and_missing_ips_come_last():
    assert ids(utils.sort_guests(GUESTS, IP_DATA, "IP")) == [9, 100, 20]
    # "10.0.0.9" must sort before "10.0.0.10", which a string sort gets wrong
    assert ids(utils.sort_guests(GUESTS, IP_DATA, "IP", reverse=True)) == [20, 100, 9]


def test_sort_by_numeric_column_uses_the_value_not_the_text():
    assert ids(utils.sort_guests(GUESTS, IP_DATA, "CPU (%)")) == [9, 20, 100]
    assert ids(utils.sort_guests(GUESTS, IP_DATA, "RAM used/max (MB)")) == [9, 20, 100]


def test_sort_ties_are_broken_by_id():
    guests = [{"vmid": 5, "status": "running"}, {"vmid": 3, "status": "running"}]
    assert ids(utils.sort_guests(guests, {}, "Status")) == [3, 5]


def test_sort_unknown_column_falls_back_to_id():
    assert ids(utils.sort_guests(GUESTS, IP_DATA, "Bogus")) == [9, 20, 100]


def test_sort_does_not_modify_its_input():
    before = list(GUESTS)
    utils.sort_guests(GUESTS, IP_DATA, "Name", reverse=True)
    assert GUESTS == before


@pytest.mark.parametrize(
    "text, expected",
    [
        ("", [100, 9, 20]),
        ("   ", [100, 9, 20]),
        ("web", [100]),  # case-insensitive
        ("LXC", [9, 20]),  # type
        ("stopped", [9]),  # status
        ("pve1", [100, 20]),  # node
        ("10.0.0.9", [9]),  # IP
        ("aa:bb:cc:00:00:02", [9]),  # MAC, any case
        ("100", [100]),  # id
        ("lxc running", [20]),  # every term must match
        ("nothing-matches", []),
    ],
)
def test_filter_guests(text, expected):
    assert ids(utils.filter_guests(GUESTS, IP_DATA, text)) == expected


# -- chart ------------------------------------------------------------------------

def chart_rows(chart):
    """The bar rows of a chart, without the axis labels and the colour markup."""
    lines = chart.split("\n")[:-1]  # drop the "╰───" axis line
    return [line.split("┤ ", 1)[1] for line in lines if "┤" in line]


def test_chart_shows_the_most_recent_samples_when_there_are_too_many():
    chart = utils.draw_vertical_bar_chart([0] * 10 + [1, 2, 3], height=4, chart_width=3, decimal_places=0)
    # the scale comes from the visible samples (1..3), not from the old zeros
    assert chart.split("\n")[0].startswith("[white]    3 ┤")
    assert chart_rows(chart)[-1] == "░░░"
    assert chart_rows(chart)[0] == "  ░"  # only the newest sample reaches the top


def test_chart_has_one_row_per_height_and_an_axis():
    chart = utils.draw_vertical_bar_chart([1, 2, 3, 4], height=5, chart_width=4)
    assert len(chart_rows(chart)) == 5
    assert "╰" + "─" * 4 in chart


def test_chart_fixed_scale_labels_and_clamping():
    chart = utils.draw_vertical_bar_chart(
        [50, 500], height=3, chart_width=2, y_min=0, y_max=100, decimal_places=0
    )
    top, middle, bottom = [line.split(" ┤")[0].strip("[white] ") for line in chart.split("\n")[:3]]
    assert (top, middle, bottom) == ("100", "50", "0")
    # 500 is off the scale: it is clamped to a full column, not an error
    assert chart_rows(chart)[0].endswith("░")


def test_chart_flat_data_does_not_divide_by_zero():
    chart = utils.draw_vertical_bar_chart([7, 7, 7], height=4)
    assert len(chart_rows(chart)) == 4


def test_chart_truncates_wide_output():
    chart = utils.draw_vertical_bar_chart([1] * 300, max_output_width=50)
    plain = chart.replace("[white]", "").replace("[/white]", "")
    assert all(len(line) <= 50 + len("...") for line in plain.split("\n"))


# -- cache --------------------------------------------------------------------------

def test_cached_reuses_the_value_until_the_ttl_expires(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(utils.time, "monotonic", lambda: now[0])
    utils._cache.clear()
    calls = []

    def loader():
        calls.append(1)
        return len(calls)

    assert utils.cached("k", 30, loader) == 1
    now[0] += 29
    assert utils.cached("k", 30, loader) == 1  # still fresh
    now[0] += 2
    assert utils.cached("k", 30, loader) == 2  # expired: loaded again
    assert utils.cached("other", 30, loader) == 3  # keys are independent


def test_cached_does_not_store_errors():
    utils._cache.clear()
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise requests.ConnectionError("down")
        return "ok"

    with pytest.raises(requests.ConnectionError):
        utils.cached("flaky", 30, flaky)
    assert utils.cached("flaky", 30, flaky) == "ok"


# -- nodes and guests ---------------------------------------------------------------

def test_get_nodes_defaults_to_the_local_node(pve):
    assert utils.get_nodes() == ["pve1"]


def test_get_nodes_uses_the_configured_list_home_node_first(pve, monkeypatch):
    monkeypatch.setattr(utils, "NODES", " pve2 , pve1,pve2 ")
    assert utils.get_nodes() == ["pve1", "pve2"]  # de-duplicated, NODE first
    assert pve.counts["__total__"] == 0  # no discovery needed


def test_get_nodes_star_discovers_online_nodes_and_caches_them(pve, monkeypatch):
    monkeypatch.setattr(utils, "NODES", "*")
    assert utils.get_nodes() == ["pve1", "pve2"]  # pve3 is offline
    assert utils.get_nodes() == ["pve1", "pve2"]
    assert pve.counts["/nodes"] == 1


def test_get_guests_single_node(pve):
    guests = utils.get_guests()
    assert [g["vmid"] for g in guests] == [100, 101, 200, 201]
    assert {g["vmid"]: g["type"] for g in guests} == {100: "VM", 101: "VM", 200: "LXC", 201: "LXC"}
    assert {g["node"] for g in guests} == {"pve1"}
    # one list request per guest type; no per-guest status requests
    assert pve.counts["__total__"] == 2
    assert pve.last_timeout == utils.API_TIMEOUT


def test_get_guests_multi_node_merges_and_sorts(pve, monkeypatch):
    monkeypatch.setattr(utils, "NODES", "pve1,pve2")
    guests = utils.get_guests()
    assert [g["vmid"] for g in guests] == [100, 101, 200, 201, 300, 400]
    assert {g["vmid"]: g["node"] for g in guests}[300] == "pve2"


def test_get_guests_reports_a_failed_node_but_keeps_the_others(pve, monkeypatch):
    monkeypatch.setattr(utils, "NODES", "pve1,pve2")
    pve.fail_node = "pve2"
    errors = {}
    guests = utils.get_guests(errors)
    assert {g["node"] for g in guests} == {"pve1"}
    assert list(errors) == ["pve2"]
    assert isinstance(errors["pve2"], requests.ConnectionError)


def test_get_guests_raises_when_a_node_fails_and_errors_are_not_collected(pve, monkeypatch):
    monkeypatch.setattr(utils, "NODES", "pve1,pve2")
    pve.fail_node = "pve2"
    with pytest.raises(requests.ConnectionError):
        utils.get_guests()


def test_get_guests_raises_when_every_node_fails(pve, monkeypatch):
    monkeypatch.setattr(utils, "NODES", "pve1,pve2")
    pve.fail = True
    with pytest.raises(requests.ConnectionError):
        utils.get_guests({})


def test_get_guests_single_node_failure_raises(pve):
    pve.fail = True
    with pytest.raises(requests.ConnectionError):
        utils.get_guests({})


# -- power actions --------------------------------------------------------------------

@pytest.mark.parametrize(
    "vm_type, action, node, path",
    [
        ("vm", "start", None, "/nodes/pve1/qemu/101/status/start"),
        ("lxc", "shutdown", None, "/nodes/pve1/lxc/101/status/shutdown"),
        ("vm", "stop", "pve2", "/nodes/pve2/qemu/101/status/stop"),
    ],
)
def test_vm_power_action_posts_to_the_right_endpoint(pve, vm_type, action, node, path):
    # 101 only exists on pve1, so this checks the URL; the fake ignores unknown guests
    pve.state[("pve2", "qemu", 101)] = dict(pve.state[("pve1", "qemu", 101)])
    pve.state[("pve1", "lxc", 101)] = dict(pve.state[("pve1", "qemu", 101)])
    assert utils.vm_power_action(101, vm_type, action, node) == "UPID:fake"
    assert pve.posts == [path]


def test_vm_power_action_rejects_unknown_actions(pve):
    with pytest.raises(ValueError):
        utils.vm_power_action(100, "vm", "reboot")
    assert pve.posts == []


def test_vm_power_action_propagates_api_failures(pve):
    pve.fail_post = True
    with pytest.raises(requests.ConnectionError):
        utils.vm_power_action(100, "vm", "stop")


# -- config cache and IP lookup ---------------------------------------------------------

def test_vm_config_is_cached(pve):
    utils.get_vm_config(100, "vm")
    utils.get_vm_config(100, "vm")
    assert pve.counts["/nodes/pve1/qemu/{id}/config"] == 1
    utils.get_vm_config(100, "vm", "pve1")  # same node spelled out: still the same entry
    assert pve.counts["/nodes/pve1/qemu/{id}/config"] == 1


def test_find_vm_ip_address_via_arp_matches_macs_case_insensitively(pve):
    table = utils.find_vm_ip_address()
    assert table[100] == {"mac": "AA:BB:CC:00:00:01", "ip": "10.0.0.1"}
    assert table[200] == {"mac": "AA:BB:CC:00:00:03", "ip": "10.0.0.2"}  # ARP has it lower-case
    assert table[101]["ip"] == "N/A"  # known MAC, not in the ARP table


def test_find_vm_ip_address_reads_configs_from_each_guests_node(pve, monkeypatch):
    monkeypatch.setattr(utils, "NODES", "pve1,pve2")
    table = utils.find_vm_ip_address()
    assert table[300]["mac"] == "AA:BB:CC:00:00:04"
    assert pve.counts["/nodes/pve2/qemu/{id}/config"] == 1


def test_find_vm_ip_address_skips_guests_whose_config_cannot_be_read(pve, monkeypatch):
    monkeypatch.setattr(utils, "NODES", "pve1,pve2")
    guests = utils.get_guests()
    pve.fail_node = "pve2"
    table = utils.find_vm_ip_address(guests)
    assert 300 not in table and 400 not in table
    assert 100 in table


def test_find_vm_ip_address_without_ssh_uses_container_interfaces(pve, monkeypatch):
    monkeypatch.setattr(utils, "SSH_HOST", None)
    table = utils.find_vm_ip_address()
    assert table[100] == {"mac": "N/A", "ip": "N/A"}  # VMs have no interface API
    assert table[200] == {"mac": "AA:BB:CC:00:00:03", "ip": "10.0.0.0/24"}
    assert 201 not in table  # stopped container: nothing to report
