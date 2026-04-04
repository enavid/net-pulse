# NetPulse

> Distributed download coordinator with a live web panel, orchestrates multiple remote agents to generate realistic outbound traffic from VPN servers.

---

## Overview

NetPulse solves a specific infrastructure problem: when a set of VPN servers need consistent, realistic-looking download traffic to balance their upload-to-download ratio. It runs as a central coordinator that connects to a fleet of agent servers over short-lived SSH sessions, schedules file downloads across 24 hours following a human-like activity curve, tracks monthly quotas, and exposes everything through a secure web panel.

The coordinator itself can also act as a download agent in localhost mode, so you do not need a separate machine to get started. VPN servers are treated as pure upload sources, their upload bandwidth is free, so no quota tracking is applied to them. Quota tracking applies only to the agent machines that are doing the downloading.

---

## Project Structure

```
netpulse/
├── main.py              # Entry point starts panel thread and coordinator loop
├── config.toml          # All configuration (copy from config.toml.example)
├── config.toml.example  # Annotated configuration template
├── requirements.txt
├── src/
│   ├── config.py        # TOML loader and dataclasses
│   ├── coordinator.py   # Orchestrates one 24-hour cycle across all agents
│   ├── agent.py         # SSH dispatcher, plan management, retry logic
│   ├── downloader.py    # Local async downloader for localhost agent
│   ├── metrics.py       # VPN server metric fetcher
│   ├── scheduler.py     # Human-activity-curve event scheduler
│   ├── state.py         # In-memory state synced from SQLite
│   ├── storage.py       # SQLite persistence (plans, monthly usage)
│   ├── logger.py        # Rotating file logger with in-memory buffer for panel
│   └── panel.py         # FastAPI web panel with session authentication
├── templates/
│   ├── index.html       # Dashboard (dark/light, responsive, sidebar nav)
│   └── login.html       # Login page
└── logs/
    ├── netpulse.log     # Rotating log file
    ├── netpulse.db      # SQLite database
    └── state.json       # Latest cycle state snapshot
```

---

## Quick Start

Clone the repository, copy the example config, edit it with your servers, then run.

```bash
git clone https://github.com/enavid/net-pulse.git
cd netpulse
cp config.toml.example config.toml
```

Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate.bat
pip install -r requirements.txt
```

Start the coordinator:

```bash
python main.py
```

The program will ask how many days to run. Enter `0` to run indefinitely. Each 24-hour cycle completes and then automatically restarts.

To access the web panel, open an SSH tunnel from your local machine to the coordinator server and then navigate to `http://127.0.0.1:7070` in your browser:

```bash
ssh -L 7070:127.0.0.1:7070 user@your-server-ip
```

---

## Configuration

All settings live in `config.toml`. The file is divided into logical sections.

**`[panel]`** controls the web UI, host, port, the secret key used to sign session cookies, and the username/password for login. The panel always binds to `127.0.0.1` so it is never exposed to the public internet. Access it exclusively through an SSH tunnel.

**`[scheduler]`** sets how many days to run (`days = 0` for forever), the daily variance applied to each agent's download target (e.g. `0.20` = ±20%), and the activity weights for each 6-hour window of the day. The default weights `[0.05, 0.30, 0.35, 0.30]` produce light traffic at night and peaks in the morning and evening, matching typical human usage.

**`[download]`** controls download behaviour: speed cap per file in bytes per second, the probability and duration of random mid-download pauses, maximum concurrent downloads per agent, and retry settings. `max_retries = 3` means each failed download is retried up to three times with a random delay between attempts drawn from `retry_delay_range`.

**`[network]`** has a single `verify_ssl` flag. Set it to `false` if your VPN servers use self-signed certificates, which is common.

**`[[sources]]`** defines the VPN servers. Each source has a label, a direct download URL (pointing to a file on the VPN server, typically 1 GB), and a metric URL that returns a JSON object with `network.rx_gb` and `network.tx_gb` fields. Sources have no quota because VPN server upload is free. The coordinator fetches metrics from all sources before each cycle and logs them.

**`[[monitors]]`** is for servers you want to observe in the metrics panel but never download from. The same metric URL format applies.

**`[[agents]]`** defines the machines that will perform the actual downloads. Each agent has SSH credentials, a `daily_limit_gb` target (with variance applied), and optional monthly quota fields. If `monthly_limit_gb` is set, the coordinator tracks how many bytes that agent has downloaded this month against the allowed quota (`monthly_limit_gb × usage_quota_pct`). Agents with more remaining quota are given higher priority in the download schedule. Setting `host = "localhost"` makes the coordinator itself act as a download agent without any SSH connection.

A minimal example configuration looks like this:

```toml
[panel]
host = "127.0.0.1"
port = 7070
secret_key = "replace-with-random-string"
username = "admin"
password = "replace-with-strong-password"

[scheduler]
days = 0
daily_variance = 0.20
schedule_weights = [0.05, 0.30, 0.35, 0.30]

[download]
speed_cap = 5242880
pause_probability = 0.3
pause_range = [10, 90]
max_concurrent = 2
max_retries = 3
retry_delay_range = [30, 120]

[network]
verify_ssl = false

[logging]
level = "INFO"
file = "logs/netpulse.log"

[[sources]]
label = "vpn1"
download_url = "https://5.x.x.x/files/1gb.zip"
metric_url = "https://5.x.x.x/your-secret-token"

[[agents]]
label = "self"
host = "localhost"
port = 0
user = "local"
password = "local"
daily_limit_gb = 8.0
monthly_limit_gb = 200.0
usage_quota_pct = 0.60

[[agents]]
label = "agent1"
host = "192.168.1.10"
port = 22
user = "root"
password = "secret"
daily_limit_gb = 5.0
monthly_limit_gb = 100.0
usage_quota_pct = 0.80
```

---

## How Plans Work

At the start of each cycle, each agent builds a download plan for the day, a list of events spread across 24 hours according to the scheduler weights. The plan is written to the SQLite database immediately. If the program is restarted mid-day, the coordinator detects the existing plan and resumes only the events that are still pending, skipping anything already done or failed. Stale pending events from a previous incomplete run are cleaned up automatically. Each event's status transitions through `pending → running → done / failed` and is visible in the Today's Plan tab of the panel.

---

## Web Panel

The panel has five sections accessible from the sidebar. The Overview tab shows an agent card for each configured agent with its current download progress, connection status, and today's plan completion ratio. The Test Connections button opens a short SSH connection to every agent and reports whether it succeeded. The Today's Plan tab lists every scheduled event with its status, scheduled time, bytes downloaded, and any error message. The Quota / Metrics tab shows the monthly quota status for each agent, how much is allowed this month, how much has been used, and how much remains, alongside a table of live rx/tx metrics from all VPN sources and monitors. The Monthly History tab shows cumulative download totals per agent per calendar month, persisted in SQLite across restarts. The Logs tab shows a live-scrolling view of the last 200 log lines.

The panel supports dark and light themes. The chosen theme is remembered in the browser across sessions. On mobile the sidebar collapses into a hamburger drawer. All tables scroll horizontally on small screens.

---

## Autorun with systemd

Create a service file at `/etc/systemd/system/netpulse.service`:

```ini
[Unit]
Description=NetPulse coordinator
After=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/netpulse
ExecStart=/path/to/netpulse/.venv/bin/python main.py
StandardInput=null
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
```

Then enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now netpulse
sudo systemctl status netpulse
```

With `Restart=always` and `RestartSec=60`, systemd will restart NetPulse automatically if it exits after a completed cycle or crashes unexpectedly. The 60-second gap between cycles is enforced by the program itself before systemd would need to act.

---

## Data Persistence

NetPulse stores all persistent data in `logs/netpulse.db`, a SQLite file that survives restarts. Two tables are maintained: `planned_events` holds every download event with its date, agent, source, scheduled time, and final status. `monthly_usage` accumulates the total bytes downloaded per agent per calendar month. The `state.json` file is a snapshot of the current cycle written after every download, it is used by the panel for fast reads but is not the source of truth.

If you need to reset monthly counters, delete or clear the `monthly_usage` table in the database. If you need to force a fresh plan for today, delete today's rows from `planned_events` and restart.