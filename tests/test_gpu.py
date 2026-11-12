"""Tests for the local GPU / temperature readers (no real hardware needed)."""

import subprocess
from types import SimpleNamespace

import utils

GiB = 2**30


def make_amd_card(root, name="card0", vendor="0x1002", busy="42", vram=(GiB // 4, GiB), gtt=(3 * GiB, 16 * GiB)):
    dev = root / name / "device"
    dev.mkdir(parents=True)
    (dev / "vendor").write_text(vendor + "\n")
    if busy is not None:
        (dev / "gpu_busy_percent").write_text(busy + "\n")
    if vram:
        (dev / "mem_info_vram_used").write_text(f"{vram[0]}\n")
        (dev / "mem_info_vram_total").write_text(f"{vram[1]}\n")
    if gtt:
        (dev / "mem_info_gtt_used").write_text(f"{gtt[0]}\n")
        (dev / "mem_info_gtt_total").write_text(f"{gtt[1]}\n")
    return dev


def test_amd_gpu_is_read_from_sysfs(tmp_path):
    make_amd_card(tmp_path)
    (gpu,) = utils.get_amd_gpus(tmp_path)
    assert gpu["vendor"] == "amd"
    assert gpu["util"] == 42.0
    assert gpu["vram_used_gb"] == 0.25 and gpu["vram_total_gb"] == 1.0
    assert gpu["gtt_used_gb"] == 3.0 and gpu["gtt_total_gb"] == 16.0


def test_amd_gpu_without_gtt_files(tmp_path):
    make_amd_card(tmp_path, gtt=None)
    (gpu,) = utils.get_amd_gpus(tmp_path)
    assert gpu["gtt_used_gb"] is None and gpu["gtt_total_gb"] is None


def test_non_amd_and_incomplete_cards_are_skipped(tmp_path):
    make_amd_card(tmp_path, "card0", vendor="0x10de")  # NVIDIA
    make_amd_card(tmp_path, "card1", busy=None)  # not amdgpu: no utilisation file
    make_amd_card(tmp_path, "card2", vram=None)  # no VRAM info
    make_amd_card(tmp_path, "card3", busy="n/a")  # garbage
    assert utils.get_amd_gpus(tmp_path) == []


def test_several_amd_gpus_are_listed_in_card_order(tmp_path):
    make_amd_card(tmp_path, "card1", busy="10")
    make_amd_card(tmp_path, "card0", busy="20")
    assert [g["util"] for g in utils.get_amd_gpus(tmp_path)] == [20.0, 10.0]


def test_no_gpu_directory_means_no_gpus(tmp_path):
    assert utils.get_amd_gpus(tmp_path / "missing") == []


def _nvidia_smi(monkeypatch, stdout, available=True):
    monkeypatch.setattr(utils.shutil, "which", lambda name: "/usr/bin/nvidia-smi" if available else None)
    monkeypatch.setattr(utils.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=stdout))


def test_nvidia_gpus_are_parsed_from_nvidia_smi(monkeypatch):
    _nvidia_smi(monkeypatch, "NVIDIA GeForce RTX 3060, 17, 1024, 12288\nTesla T4, 90, 2048, 16384\n")
    first, second = utils.get_nvidia_gpus()
    assert first["name"] == "NVIDIA GeForce RTX 3060"
    assert first["util"] == 17.0
    assert first["vram_used_gb"] == 1.0 and first["vram_total_gb"] == 12.0  # MiB -> GiB
    assert second["util"] == 90.0


def test_nvidia_ignores_malformed_lines(monkeypatch):
    _nvidia_smi(monkeypatch, "garbage\nGPU, [N/A], 1, 2\n\nGood GPU, 5, 512, 1024\n")
    assert [g["name"] for g in utils.get_nvidia_gpus()] == ["Good GPU"]


def test_nvidia_without_nvidia_smi(monkeypatch):
    _nvidia_smi(monkeypatch, "", available=False)
    assert utils.get_nvidia_gpus() == []


def test_nvidia_smi_failure_is_not_fatal(monkeypatch):
    monkeypatch.setattr(utils.shutil, "which", lambda name: "/usr/bin/nvidia-smi")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("nvidia-smi", 5)

    monkeypatch.setattr(utils.subprocess, "run", boom)
    assert utils.get_nvidia_gpus() == []


def test_get_gpu_stats_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("driver exploded")

    monkeypatch.setattr(utils, "get_nvidia_gpus", boom)
    assert utils.get_gpu_stats() == []


def test_get_gpu_stats_combines_vendors(monkeypatch):
    monkeypatch.setattr(utils, "get_nvidia_gpus", lambda: [{"vendor": "nvidia"}])
    monkeypatch.setattr(utils, "get_amd_gpus", lambda: [{"vendor": "amd"}])
    assert [g["vendor"] for g in utils.get_gpu_stats()] == ["nvidia", "amd"]


def gpu(util=12.0, vram=(0.5, 2.0), gtt=(None, None)):
    return {"util": util, "vram_used_gb": vram[0], "vram_total_gb": vram[1],
            "gtt_used_gb": gtt[0], "gtt_total_gb": gtt[1]}


def test_summary_of_one_gpu():
    assert utils.format_gpu_summary([gpu()]) == "GPU: 12% ｜ VRAM: 0.5/2.0 GB"


def test_summary_includes_gtt_when_present():
    text = utils.format_gpu_summary([gpu(gtt=(3.0, 16.0))])
    assert text.endswith("｜ GTT: 3.0/16.0 GB")


def test_summary_numbers_gpus_when_there_are_several():
    text = utils.format_gpu_summary([gpu(), gpu(util=99)])
    assert "GPU0: 12%" in text and "GPU1: 99%" in text


def test_summary_of_no_gpus_is_empty():
    assert utils.format_gpu_summary([]) == ""


def test_cpu_temperature(monkeypatch, tmp_path):
    zone = tmp_path / "temp"
    zone.write_text("45678\n")
    real_path = utils.Path
    monkeypatch.setattr(utils, "Path", lambda p: zone if "thermal" in str(p) else real_path(p))
    assert utils.get_cpu_temperature() == {"CPU": "45.7"}
    zone.unlink()
    assert utils.get_cpu_temperature() == {"CPU": "N/A"}
