"""Settings, read once from the environment (or a .env file).

See .env_example for the available settings.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# Proxmox API
PROXMOX_API_URL = os.getenv("PROXMOX_HOST")
API_TOKEN_ID = os.getenv("TOKEN_ID")
API_TOKEN_SECRET = os.getenv("TOKEN_SECRET")
NODE = os.getenv("NODE")

# Optional SSH access to the Proxmox host (used to look up guest IP addresses)
SSH_HOST = os.getenv("SSH_HOST")
SSH_PORT = int(os.getenv("SSH_PORT", 22))
SSH_USER = os.getenv("SSH_USER")
SSH_PASSWORD = os.getenv("SSH_PASSWORD")

HEADERS = {"Authorization": f"PVEAPIToken={API_TOKEN_ID}={API_TOKEN_SECRET}"}

# Seconds to wait for the Proxmox API before giving up (an unreachable host
# would otherwise hang a refresh indefinitely).
API_TIMEOUT = 10

REQUIRED_SETTINGS = ("PROXMOX_HOST", "TOKEN_ID", "TOKEN_SECRET", "NODE")


def missing_settings():
    """Names of required settings that are not set."""
    return [name for name in REQUIRED_SETTINGS if not os.getenv(name)]
