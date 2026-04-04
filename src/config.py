"""
    src/config.py – Load and validate all settings from config.toml.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Tuple
from dataclasses import dataclass

_CONFIG_FILE = Path("config.toml")


# Dataclasses

@dataclass
class DownloadSource:
    label: str
    download_url: str
    metric_url: str

@dataclass
class MonitorSource:
    """Metric-only source — no downloads, just monitoring."""
    label: str
    metric_url: str


@dataclass
class AgentConfig:
    label: str
    host: str
    port: int
    user: str
    password: str
    daily_limit_gb: float
    monthly_limit_gb: float      # total monthly quota for this server
    usage_quota_pct: float       # fraction of quota allowed for downloads (0.0–1.0)

    @property
    def monthly_allowed_gb(self) -> float:
        return self.monthly_limit_gb * self.usage_quota_pct


    @property
    def is_local(self) -> bool:
        return self.host == "localhost"


@dataclass
class Config:
    # Panel
    total_days: int
    panel_host: str
    panel_port: int
    secret_key: str
    panel_username: str
    panel_password: str

    # Sources & agents
    download_sources: List[DownloadSource]
    monitors: List[MonitorSource]
    agents: List[AgentConfig]

    # Scheduler
    daily_variance: float
    schedule_weights: List[float]

    # Download behaviour
    download_speed_cap: int
    download_pause_probability: float
    download_pause_range: Tuple[int, int]
    max_concurrent_downloads: int
    download_max_retries: int
    download_retry_delay_range: Tuple[int, int]

    # Logging
    log_level: str
    log_file: str

    # Network
    verify_ssl: bool


# Loader

def _load_toml(path: Path) -> dict:
    try:
        import tomllib  # type: ignore
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except ImportError:
        pass
    try:
        import tomli  # type: ignore
        with open(path, "rb") as fh:
            return tomli.load(fh)
    except ImportError:
        print("[ERROR] Python < 3.11 detected. Install tomli: pip install tomli")
        sys.exit(1)


def load_config(path: Path = _CONFIG_FILE) -> Config:
    if not path.exists():
        print(f"[ERROR] Config file not found: {path}")
        sys.exit(1)

    data = _load_toml(path)

    panel    = data.get("panel", {})
    sched    = data.get("scheduler", {})
    dl       = data.get("download", {})
    network  = data.get("network", {})
    logging_ = data.get("logging", {})

    sources = [
        DownloadSource(label=s["label"], download_url=s["download_url"], metric_url=s["metric_url"])
        for s in data.get("sources", [])
    ]

    monitors = [
        MonitorSource(label=m["label"], metric_url=m["metric_url"])
        for m in data.get("monitors", [])
    ]

    agents = [
        AgentConfig(
            label=a["label"],
            host=a["host"],
            port=int(a.get("port", 22)),
            user=a["user"],
            password=a["password"],
            daily_limit_gb=float(a["daily_limit_gb"]),
            monthly_limit_gb=float(a.get("monthly_limit_gb", 0.0)),
            usage_quota_pct=float(a.get("usage_quota_pct", 1.0)),
        )
        for a in data.get("agents", [])
    ]

    pause_range       = dl.get("pause_range", [10, 90])
    retry_delay_range = dl.get("retry_delay_range", [30, 120])
    weights           = sched.get("schedule_weights", [0.05, 0.30, 0.35, 0.30])

    return Config(
        total_days=int(sched.get("days", 0)),
        panel_host=panel.get("host", "127.0.0.1"),
        panel_port=int(panel.get("port", 7070)),
        secret_key=panel.get("secret_key", "change-me"),
        panel_username=panel.get("username", "admin"),
        panel_password=panel.get("password", "admin"),
        download_sources=sources,
        monitors=monitors,
        agents=agents,
        daily_variance=float(sched.get("daily_variance", 0.20)),
        schedule_weights=list(weights),
        download_speed_cap=int(dl.get("speed_cap", 5 * 1024 ** 2)),
        download_pause_probability=float(dl.get("pause_probability", 0.3)),
        download_pause_range=(int(pause_range[0]), int(pause_range[1])),
        max_concurrent_downloads=int(dl.get("max_concurrent", 2)),
        download_max_retries=int(dl.get("max_retries", 3)),
        download_retry_delay_range=(int(retry_delay_range[0]), int(retry_delay_range[1])),
        log_level=str(logging_.get("level", "INFO")),
        log_file=str(logging_.get("file", "logs/netpulse.log")),
        verify_ssl=bool(network.get("verify_ssl", False)),
    )