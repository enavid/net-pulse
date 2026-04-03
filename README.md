# NetPulse

> Distributed download coordinator with a live web panel, orchestrates multiple remote agents to generate realistic traffic from VPN servers.

---

## What it does

NetPulse runs on a central coordinator server and:

- Connects to **agent servers** via short-lived SSH sessions and triggers downloads.
- Distributes downloads randomly across 24 hours following a human-like activity curve.
- Reads **VPN server metrics** (rx/tx GB) to show network load in the panel.
- Exposes a **web panel on 127.0.0.1,** access it securely via SSH tunnel.
- The coordinator itself can also act as an agent (localhost mode).

---

## Project Structure

```
net-pulse/
├── main.py                  # Entry point
├── requirements.txt
├── .env.example
├── src/
│   ├── __init__.py
│   ├── config.py            # .env loader
│   ├── coordinator.py       # Orchestrates all agents for one cycle
│   ├── agent.py             # SSH dispatcher + local download
│   ├── downloader.py        # Local async downloader (localhost agent)
│   ├── metrics.py           # VPN metric fetcher
│   ├── scheduler.py         # Human-activity-curve event scheduler
│   ├── state.py             # Shared in-memory + JSON state
│   ├── logger.py            # Structured rotating logger + in-memory buffer
│   └── panel.py             # FastAPI web panel
├── templates/
│   └── index.html           # Dashboard UI
└── logs/
    ├── netpulse.log
    └── state.json
```

---

## Quick Start

### 1 · Clone & configure

```bash
git clone https://github.com/enavid/net-pulse.git
cd net-pulse
cp .env.example .env
# Edit .env — add your agents, download sources, and metric URLs
```

### 2 · Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate.bat
pip install -r requirements.txt
```

### 3 · Run

```bash
python main.py
```

### 4 · Access the panel

Open an SSH tunnel to your coordinator server:

```bash
ssh -L 7070:127.0.0.1:7070 user@your-server-ip
```

Then open your browser at:

```
http://127.0.0.1:7070
```

---

## Configuration (`.env`)

### Download Sources (VPN servers)

Format: `LABEL|DOWNLOAD_URL|METRIC_URL` — comma-separated.

```env
DOWNLOAD_SOURCES=server1|https://5.x.x.x/files/1gb.zip|https://5.x.x.x/your-metric-token,server2|https://5.x.x.y/files/1gb.zip|https://5.x.x.y/your-metric-token
```

> Set `VERIFY_SSL=False` if your servers use self-signed certificates.

### Agent Servers

Format: `LABEL|HOST|SSH_PORT|USER|PASSWORD|DAILY_LIMIT_GB` — comma-separated.

Use `localhost` as HOST to make the coordinator itself act as a download agent.

```env
AGENTS=agent1|192.168.1.10|22|root|secret|5,agent2|192.168.1.11|22|root|secret|10,self|localhost|0|local|local|8
```

### Full variable reference

| Variable                       | Default                 | Description                                |
| ------------------------------ | ----------------------- | ------------------------------------------ |
| `PANEL_HOST`                 | `127.0.0.1`           | Panel bind address (keep as 127.0.0.1)     |
| `PANEL_PORT`                 | `7070`                | Panel port                                 |
| `DOWNLOAD_SOURCES`           | —                      | VPN server entries                         |
| `AGENTS`                     | —                      | Agent server entries                       |
| `DAILY_VARIANCE`             | `0.20`                | ±% randomness on each agent's daily limit |
| `SCHEDULE_WEIGHTS`           | `0.05,0.30,0.35,0.30` | Activity weights per 6-hour window         |
| `DOWNLOAD_SPEED_CAP`         | `5242880` (5 MB/s)    | Per-download speed cap in bytes/sec        |
| `DOWNLOAD_PAUSE_PROBABILITY` | `0.3`                 | Chance of mid-download pause               |
| `DOWNLOAD_PAUSE_RANGE`       | `10,90`               | Pause range in seconds                     |
| `MAX_CONCURRENT_DOWNLOADS`   | `2`                   | Max parallel downloads per agent           |
| `VERIFY_SSL`                 | `False`               | Verify SSL certs (False for self-signed)   |
| `LOG_LEVEL`                  | `INFO`                | `DEBUG` / `INFO` / `WARNING`         |
| `LOG_FILE`                   | `logs/netpulse.log`   | Log file path                              |

---

## Panel Features

- **Agent cards** — live download progress, ok/fail counts, daily progress bar
- **⚡ Test Agent Connections** — ping all SSH agents and show connection status
- **VPN Server Metrics** — rx/tx GB from each VPN server's metric endpoint
- **Live Logs** — scrolling log viewer with auto-scroll, updated every 5 seconds

---

## Autorun (Linux – systemd)

```ini
# /etc/systemd/system/netpulse.service
[Unit]
Description=NetPulse coordinator
After=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/netpulse
ExecStart=/path/to/netpulse/.venv/bin/python main.py
StandardInput=null
Environment=NP_DAYS=0
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now netpulse
```
