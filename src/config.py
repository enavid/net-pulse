"""
src/config.py – Two-layer configuration.

  SystemConfig  : loaded once from config.toml (panel auth, log, port).
                  Never changes at runtime.
  RuntimeConfig : loaded from DB before every cycle (agents, sources,
                  monitors, scheduler/download settings).
                  Can be updated from the UI without restarting.
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
    monthly_limit_gb: float
    usage_quota_pct: float

    @property
    def monthly_allowed_gb(self) -> float:
        return self.monthly_limit_gb * self.usage_quota_pct

    @property
    def is_local(self) -> bool:
        return self.host == "localhost"


@dataclass
class SystemConfig:
    """Static config loaded from config.toml — panel auth and basic infra only."""
    panel_host: str
    panel_port: int
    secret_key: str
    panel_username: str
    panel_password: str
    log_level: str
    log_file: str


@dataclass
class RuntimeConfig:
    """Dynamic config loaded from DB — can change between cycles."""
    # Entities
    agents: List[AgentConfig]
    download_sources: List[DownloadSource]
    monitors: List[MonitorSource]

    # Scheduler
    total_days: int
    daily_variance: float
    schedule_weights: List[float]
    recreate_plan: bool

    # Download behaviour
    download_speed_cap: int
    download_pause_probability: float
    download_pause_range: Tuple[int, int]
    max_concurrent_downloads: int
    download_max_retries: int
    download_retry_delay_range: Tuple[int, int]

    # Network
    verify_ssl: bool
    connection_test_url: str

    # UI
    auto_refresh_seconds: int

    # System (copied from SystemConfig for convenience)
    panel_host: str = "127.0.0.1"
    panel_port: int = 7070
    secret_key: str = "change-me"
    panel_username: str = "admin"
    panel_password: str = "admin"
    log_level: str = "INFO"
    log_file: str = "logs/netpulse.log"


# TOML loader

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


def load_system_config(path: Path = _CONFIG_FILE) -> SystemConfig:
    """Load static system config from config.toml."""
    if not path.exists():
        print(f"[ERROR] Config file not found: {path}")
        sys.exit(1)
    data    = _load_toml(path)
    panel   = data.get("panel", {})
    logging_ = data.get("logging", {})
    return SystemConfig(
        panel_host=panel.get("host", "127.0.0.1"),
        panel_port=int(panel.get("port", 7070)),
        secret_key=panel.get("secret_key", "change-me"),
        panel_username=panel.get("username", "admin"),
        panel_password=panel.get("password", "admin"),
        log_level=str(logging_.get("level", "INFO")),
        log_file=str(logging_.get("file", "logs/netpulse.log")),
    )


def seed_db_from_toml(path: Path = _CONFIG_FILE) -> None:
    """
    On first run: read agents/sources/monitors/settings from config.toml
    and insert them into DB (only if DB tables are empty).
    """
    from src import storage as st

    if not path.exists():
        return

    data = _load_toml(path)

    # Seed settings only if table is empty
    existing = st.get_all_settings()
    if not existing:
        st.seed_default_settings()
        sched   = data.get("scheduler", {})
        dl      = data.get("download", {})
        network = data.get("network", {})

        overrides = {
            "scheduler.days":              str(sched.get("days", 0)),
            "scheduler.daily_variance":    str(sched.get("daily_variance", 0.20)),
            "scheduler.schedule_weights":  ",".join(str(w) for w in sched.get("schedule_weights", [0.05, 0.30, 0.35, 0.30])),
            "scheduler.recreate_plan":     str(sched.get("recreate_plan", True)).lower(),
            "download.speed_cap":          str(dl.get("speed_cap", 5242880)),
            "download.pause_probability":  str(dl.get("pause_probability", 0.3)),
            "download.pause_range":        ",".join(str(v) for v in dl.get("pause_range", [10, 90])),
            "download.max_concurrent":     str(dl.get("max_concurrent", 2)),
            "download.max_retries":        str(dl.get("max_retries", 3)),
            "download.retry_delay_range":  ",".join(str(v) for v in dl.get("retry_delay_range", [30, 120])),
            "network.verify_ssl":          str(network.get("verify_ssl", False)).lower(),
            "network.connection_test_url": network.get("connection_test_url", "https://speed.hetzner.de/10MB.bin"),
        }
        for k, v in overrides.items():
            st.set_setting(k, v)

    # Seed agents only if table is empty
    if not st.get_agents(enabled_only=False):
        for a in data.get("agents", []):
            st.upsert_agent({
                "label":            a["label"],
                "host":             a["host"],
                "port":             int(a.get("port", 22)),
                "user":             a["user"],
                "password":         a["password"],
                "daily_limit_gb":   float(a["daily_limit_gb"]),
                "monthly_limit_gb": float(a.get("monthly_limit_gb", 0.0)),
                "usage_quota_pct":  float(a.get("usage_quota_pct", 1.0)),
                "enabled":          1,
            })

    # Seed sources only if table is empty
    if not st.get_sources(enabled_only=False):
        for s in data.get("sources", []):
            st.upsert_source({
                "label":        s["label"],
                "download_url": s["download_url"],
                "metric_url":   s.get("metric_url", ""),
                "enabled":      1,
            })

    # Seed monitors only if table is empty
    if not st.get_monitors(enabled_only=False):
        for m in data.get("monitors", []):
            st.upsert_monitor({
                "label":      m["label"],
                "metric_url": m["metric_url"],
                "enabled":    1,
            })


def load_runtime_config(sys_cfg: SystemConfig) -> RuntimeConfig:
    """Load dynamic config from DB."""
    from src import storage as st

    settings = st.get_all_settings()

    def s(key: str, default: str = "") -> str:
        return settings.get(key, default)

    def _range(key: str, default: str) -> Tuple[int, int]:
        parts = s(key, default).split(",")
        return int(parts[0]), int(parts[1])

    def _weights(key: str) -> List[float]:
        return [float(x) for x in s(key, "0.05,0.30,0.35,0.30").split(",")]

    agents = [
        AgentConfig(
            label=r["label"], host=r["host"], port=r["port"],
            user=r["user"], password=r["password"],
            daily_limit_gb=r["daily_limit_gb"],
            monthly_limit_gb=r["monthly_limit_gb"],
            usage_quota_pct=r["usage_quota_pct"],
        )
        for r in st.get_agents(enabled_only=True)
    ]

    sources = [
        DownloadSource(label=r["label"], download_url=r["download_url"], metric_url=r["metric_url"])
        for r in st.get_sources(enabled_only=True)
    ]

    monitors = [
        MonitorSource(label=r["label"], metric_url=r["metric_url"])
        for r in st.get_monitors(enabled_only=True)
    ]

    return RuntimeConfig(
        agents=agents,
        download_sources=sources,
        monitors=monitors,
        total_days=int(s("scheduler.days", "0")),
        daily_variance=float(s("scheduler.daily_variance", "0.20")),
        schedule_weights=_weights("scheduler.schedule_weights"),
        recreate_plan=s("scheduler.recreate_plan", "true").lower() == "true",
        download_speed_cap=int(s("download.speed_cap", "5242880")),
        download_pause_probability=float(s("download.pause_probability", "0.3")),
        download_pause_range=_range("download.pause_range", "10,90"),
        max_concurrent_downloads=int(s("download.max_concurrent", "2")),
        download_max_retries=int(s("download.max_retries", "3")),
        download_retry_delay_range=_range("download.retry_delay_range", "30,120"),
        verify_ssl=s("network.verify_ssl", "false").lower() == "true",
        connection_test_url=s("network.connection_test_url", "https://speed.hetzner.de/10MB.bin"),
        auto_refresh_seconds=int(s("ui.auto_refresh_seconds", "10")),
        panel_host=sys_cfg.panel_host,
        panel_port=sys_cfg.panel_port,
        secret_key=sys_cfg.secret_key,
        panel_username=sys_cfg.panel_username,
        panel_password=sys_cfg.panel_password,
        log_level=sys_cfg.log_level,
        log_file=sys_cfg.log_file,
    )
