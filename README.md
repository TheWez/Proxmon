# Proxmon
Proxmox monitoring cli tool
![Proxmon](assets/image.png)

## Features

- Monitor Proxmox nodes and VMs from the command line.
- Display resource usage such as CPU, memory, and storage.
- Fetch and display VM statuses in real-time.
- Live GPU usage, VRAM and GTT (AMD iGPU/dGPU) history charts, plus NVIDIA support via `nvidia-smi` (when run on the PVE host).
- Lightweight and easy to use.

## Installation

1. Clone the repository:
    ```bash
    git clone https://github.com/Sr-vZ/Proxmon.git
    ```
2. Navigate to the project directory:
    ```bash
    cd proxmon
    ```
3. Install dependencies:
    ```bash
    pip install -r requirements.txt
    # or
    uv sync
    ```

## Setup
### 1. Create a proxmox API Token
- Login to proxmox and add an api user
- Add an API token for the api user, write down the secret
- Grant appropriate permissions to the user and the API token
### 2. Create .env file
Note, all SSH_ parameters are optional. If provided, they will allow looking up VMs IP addresses (via the host's ARP table)
```bash
PROXMOX_HOST = https://proxmox.myorg.com:8006
TOKEN_ID = api@pve!api-token
TOKEN_SECRET = abc12345-6789-defg-1234-1234567890ab
NODE = proxmox
SSH_HOST = proxmox.myorg.com
SSH_PORT = 22
SSH_USER = api
SSH_PASSWORD = ssh-password
```
    
**Clusters:** by default only `NODE` is monitored. Set `NODES` to a comma-separated list (`NODES = pve1,pve2`) or `*` (every online node) to show the guests of several nodes in one table, with a `Node` column. Start/stop, stats and config use each guest's own node, the top bar shows the status of the highlighted guest's node, and a node that can't be reached is reported without hiding the others. `NODE` must be the node Proxmon runs on.

CPU temperature and GPU usage/VRAM are read locally (Proxmon must run on the PVE host, so they are only shown for `NODE`): AMD via amdgpu sysfs, NVIDIA via `nvidia-smi`. The top bar segment is hidden if no GPU is found.

## Usage

Run the tool with:
```bash
python proxmon.py
```
or, when using uv:
```bash
uv run python proxmon.py
```
## Demo

Here is a quick demo of Proxmon in action:
[https://github.com/Sr-vZ/Proxmon/blob/main/assets/Proxmon_Demo.mp4](https://github.com/Sr-vZ/Proxmon/raw/refs/heads/main/assets/Proxmon_Demo.mp4)

https://github.com/Sr-vZ/Proxmon/raw/refs/heads/main/assets/Proxmon_Demo.mp4

## Contributing

Contributions are welcome! Please fork the repository and submit a pull request.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
