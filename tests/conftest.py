import os
import sys
from pathlib import Path

# config.py reads its settings when first imported, so set them before any test
# module imports proxmon/utils. (.env is not loaded over existing variables.)
os.environ.update(
    PROXMOX_HOST="https://pve.test:8006",
    TOKEN_ID="tester@pve!proxmon",
    TOKEN_SECRET="secret",
    NODE="pve1",
    SSH_HOST="pve.test",
    SSH_USER="user",
    SSH_PASSWORD="password",
)
os.environ.pop("NODES", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
import requests  # noqa: E402

import fakepve  # noqa: E402
import utils  # noqa: E402


@pytest.fixture
def pve(monkeypatch):
    """A fake Proxmox API (and ARP table) behind requests; caches start empty."""
    fake = fakepve.FakePVE()
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "post", fake.post)
    monkeypatch.setattr(utils, "ssh_execute_command", lambda *a, **k: fakepve.ARP_OUTPUT)
    monkeypatch.setattr(utils, "NODES", "")
    utils._cache.clear()
    yield fake
    utils._cache.clear()
