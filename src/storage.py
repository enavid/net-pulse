"""
    src/storage.py – SQLite-backed persistent storage.

    Tables:
      - agents           : agent servers (replaces config.toml [[agents]])
      - sources          : download sources (replaces config.toml [[sources]])
      - monitors         : metric-only sources (replaces config.toml [[monitors]])
      - system_settings  : key-value store for runtime settings
      - planned_events   : daily download plan per agent
      - manual_jobs      : on-demand download jobs triggered from UI
      - monthly_usage    : cumulative bytes downloaded per agent per month
      - daily_stats      : daily download stats per agent (survives restarts)
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import List, Optional
from datetime import datetime, date

_DB_FILE = Path("logs/netpulse.db")


# Connection

def get_connection() -> sqlite3.Connection:
    os.makedirs(_DB_FILE.parent, exist_ok=True)
    conn = sqlite3.connect(str(_DB_FILE), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# Schema

def init_db() -> None:
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS agents (
                label               TEXT PRIMARY KEY,
                host                TEXT NOT NULL,
                port                INTEGER NOT NULL DEFAULT 22,
                user                TEXT NOT NULL,
                password            TEXT NOT NULL,
                daily_limit_gb      REAL NOT NULL DEFAULT 1.0,
                monthly_limit_gb    REAL NOT NULL DEFAULT 0.0,
                usage_quota_pct     REAL NOT NULL DEFAULT 1.0,
                enabled             INTEGER NOT NULL DEFAULT 1,
                created_at          TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS sources (
                label               TEXT PRIMARY KEY,
                download_url        TEXT NOT NULL,
                metric_url          TEXT NOT NULL DEFAULT '',
                enabled             INTEGER NOT NULL DEFAULT 1,
                created_at          TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS monitors (
                label               TEXT PRIMARY KEY,
                metric_url          TEXT NOT NULL,
                enabled             INTEGER NOT NULL DEFAULT 1,
                created_at          TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS system_settings (
                key                 TEXT PRIMARY KEY,
                value               TEXT NOT NULL,
                updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS planned_events (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                date                TEXT NOT NULL,
                agent_label         TEXT NOT NULL,
                source_label        TEXT NOT NULL,
                scheduled_at        TEXT NOT NULL,
                status              TEXT NOT NULL DEFAULT 'pending',
                bytes_downloaded    INTEGER NOT NULL DEFAULT 0,
                error               TEXT
            );

            CREATE TABLE IF NOT EXISTS manual_jobs (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_label         TEXT NOT NULL,
                source_label        TEXT NOT NULL,
                download_count      INTEGER NOT NULL DEFAULT 1,
                mode                TEXT NOT NULL DEFAULT 'immediate',
                interval_type       TEXT NOT NULL DEFAULT 'fixed',
                interval_seconds    INTEGER NOT NULL DEFAULT 60,
                start_at            TEXT,
                status              TEXT NOT NULL DEFAULT 'pending',
                completed_count     INTEGER NOT NULL DEFAULT 0,
                bytes_downloaded    INTEGER NOT NULL DEFAULT 0,
                error               TEXT,
                created_at          TEXT NOT NULL DEFAULT (datetime('now')),
                started_at          TEXT,
                finished_at         TEXT
            );

            CREATE TABLE IF NOT EXISTS monthly_usage (
                agent_label         TEXT NOT NULL,
                year_month          TEXT NOT NULL,
                downloaded_bytes    INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (agent_label, year_month)
            );

            CREATE TABLE IF NOT EXISTS daily_stats (
                agent_label         TEXT NOT NULL,
                date                TEXT NOT NULL,
                downloaded_bytes    INTEGER NOT NULL DEFAULT 0,
                downloads_ok        INTEGER NOT NULL DEFAULT 0,
                downloads_fail      INTEGER NOT NULL DEFAULT 0,
                last_download_at    TEXT,
                PRIMARY KEY (agent_label, date)
            );

            CREATE INDEX IF NOT EXISTS idx_events_date    ON planned_events(date);
            CREATE INDEX IF NOT EXISTS idx_events_status  ON planned_events(status);
            CREATE INDEX IF NOT EXISTS idx_manual_status  ON manual_jobs(status);
        """)


# Default settings seed

_DEFAULT_SETTINGS = {
    "scheduler.days":              "0",
    "scheduler.daily_variance":    "0.20",
    "scheduler.schedule_weights":  "0.05,0.30,0.35,0.30",
    "scheduler.recreate_plan":     "true",
    "download.speed_cap":          "5242880",
    "download.pause_probability":  "0.3",
    "download.pause_range":        "10,90",
    "download.max_concurrent":     "2",
    "download.max_retries":        "3",
    "download.retry_delay_range":  "30,120",
    "network.verify_ssl":          "false",
    "network.connection_test_url": "https://speed.hetzner.de/10MB.bin",
    "ui.auto_refresh_seconds":     "10",
}


def seed_default_settings() -> None:
    """Insert default settings only if they don't exist yet."""
    with get_connection() as conn:
        for key, value in _DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO system_settings (key, value) VALUES (?, ?)",
                (key, value),
            )


# System settings

def get_setting(key: str, default: str = "") -> str:
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM system_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO system_settings (key, value, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value),
        )


def get_all_settings() -> dict:
    with get_connection() as conn:
        rows = conn.execute("SELECT key, value FROM system_settings").fetchall()
        return {r["key"]: r["value"] for r in rows}


# Agents

def get_agents(enabled_only: bool = True) -> List[sqlite3.Row]:
    with get_connection() as conn:
        q = "SELECT * FROM agents"
        if enabled_only:
            q += " WHERE enabled=1"
        q += " ORDER BY label"
        return conn.execute(q).fetchall()


def upsert_agent(data: dict) -> None:
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO agents (label, host, port, user, password, daily_limit_gb, monthly_limit_gb, usage_quota_pct, enabled, updated_at)
            VALUES (:label, :host, :port, :user, :password, :daily_limit_gb, :monthly_limit_gb, :usage_quota_pct, :enabled, datetime('now'))
            ON CONFLICT(label) DO UPDATE SET
                host=excluded.host, port=excluded.port, user=excluded.user,
                password=excluded.password, daily_limit_gb=excluded.daily_limit_gb,
                monthly_limit_gb=excluded.monthly_limit_gb, usage_quota_pct=excluded.usage_quota_pct,
                enabled=excluded.enabled, updated_at=excluded.updated_at
        """, data)


def delete_agent(label: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM agents WHERE label=?", (label,))


# Sources

def get_sources(enabled_only: bool = True) -> List[sqlite3.Row]:
    with get_connection() as conn:
        q = "SELECT * FROM sources"
        if enabled_only:
            q += " WHERE enabled=1"
        q += " ORDER BY label"
        return conn.execute(q).fetchall()


def upsert_source(data: dict) -> None:
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO sources (label, download_url, metric_url, enabled, updated_at)
            VALUES (:label, :download_url, :metric_url, :enabled, datetime('now'))
            ON CONFLICT(label) DO UPDATE SET
                download_url=excluded.download_url, metric_url=excluded.metric_url,
                enabled=excluded.enabled, updated_at=excluded.updated_at
        """, data)


def delete_source(label: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM sources WHERE label=?", (label,))


# Monitors

def get_monitors(enabled_only: bool = True) -> List[sqlite3.Row]:
    with get_connection() as conn:
        q = "SELECT * FROM monitors"
        if enabled_only:
            q += " WHERE enabled=1"
        q += " ORDER BY label"
        return conn.execute(q).fetchall()


def upsert_monitor(data: dict) -> None:
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO monitors (label, metric_url, enabled, updated_at)
            VALUES (:label, :metric_url, :enabled, datetime('now'))
            ON CONFLICT(label) DO UPDATE SET
                metric_url=excluded.metric_url, enabled=excluded.enabled,
                updated_at=excluded.updated_at
        """, data)


def delete_monitor(label: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM monitors WHERE label=?", (label,))


# Planned events

def insert_planned_events(events: list[dict]) -> None:
    with get_connection() as conn:
        conn.executemany(
            """INSERT INTO planned_events (date, agent_label, source_label, scheduled_at, status)
               VALUES (:date, :agent_label, :source_label, :scheduled_at, 'pending')""",
            events,
        )


def delete_stale_pending(date_str: str, agent_label: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM planned_events WHERE date=? AND agent_label=? AND status='pending'",
            (date_str, agent_label),
        )


def delete_all_pending_today(date_str: str) -> None:
    """Delete all pending events for today (used for full plan reset from UI)."""
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM planned_events WHERE date=? AND status='pending'",
            (date_str,),
        )


def update_event_status(event_id: int, status: str, bytes_downloaded: int = 0, error: str = None) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE planned_events SET status=?, bytes_downloaded=?, error=? WHERE id=?",
            (status, bytes_downloaded, error, event_id),
        )


def delete_event(event_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM planned_events WHERE id=?", (event_id,))


def get_events_for_date(date_str: str) -> List[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM planned_events WHERE date=? ORDER BY scheduled_at",
            (date_str,),
        ).fetchall()


def get_today_events() -> List[sqlite3.Row]:
    return get_events_for_date(date.today().isoformat())


# Manual jobs

def create_manual_job(
    agent_label: str,
    source_label: str,
    download_count: int,
    mode: str,
    interval_type: str,
    interval_seconds: int,
    start_at: Optional[str],
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO manual_jobs
               (agent_label, source_label, download_count, mode, interval_type, interval_seconds, start_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (agent_label, source_label, download_count, mode, interval_type, interval_seconds, start_at),
        )
        return cur.lastrowid


def get_pending_manual_jobs() -> List[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM manual_jobs WHERE status='pending' ORDER BY created_at"
        ).fetchall()


def get_manual_jobs(limit: int = 50) -> List[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM manual_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()


def update_manual_job(
    job_id: int,
    status: str,
    bytes_downloaded: int = 0,
    completed_count: int = 0,
    error: str = None,
) -> None:
    now = datetime.now().isoformat()
    with get_connection() as conn:
        if status == "running":
            conn.execute(
                "UPDATE manual_jobs SET status=?, started_at=? WHERE id=?",
                (status, now, job_id),
            )
        elif status in ("done", "failed"):
            conn.execute(
                """UPDATE manual_jobs
                   SET status=?, bytes_downloaded=bytes_downloaded+?, completed_count=?, error=?, finished_at=?
                   WHERE id=?""",
                (status, bytes_downloaded, completed_count, error, now, job_id),
            )
        else:
            conn.execute(
                "UPDATE manual_jobs SET status=?, bytes_downloaded=bytes_downloaded+?, completed_count=? WHERE id=?",
                (status, bytes_downloaded, completed_count, job_id),
            )


# Monthly usage

def add_monthly_usage(agent_label: str, bytes_downloaded: int) -> None:
    ym = datetime.now().strftime("%Y-%m")
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO monthly_usage (agent_label, year_month, downloaded_bytes)
               VALUES (?, ?, ?)
               ON CONFLICT(agent_label, year_month)
               DO UPDATE SET downloaded_bytes = downloaded_bytes + excluded.downloaded_bytes""",
            (agent_label, ym, bytes_downloaded),
        )


def get_monthly_usage(year_month: str = None) -> List[sqlite3.Row]:
    ym = year_month or datetime.now().strftime("%Y-%m")
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM monthly_usage WHERE year_month=?", (ym,)
        ).fetchall()


def get_all_monthly_usage() -> List[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM monthly_usage ORDER BY year_month DESC, agent_label"
        ).fetchall()


# Daily stats (persists across restarts)

def upsert_daily_stats(agent_label: str, bytes_downloaded: int, success: bool) -> None:
    today = date.today().isoformat()
    now   = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO daily_stats (agent_label, date, downloaded_bytes, downloads_ok, downloads_fail, last_download_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_label, date) DO UPDATE SET
                downloaded_bytes  = downloaded_bytes  + excluded.downloaded_bytes,
                downloads_ok      = downloads_ok      + excluded.downloads_ok,
                downloads_fail    = downloads_fail    + excluded.downloads_fail,
                last_download_at  = excluded.last_download_at
        """, (
            agent_label, today,
            bytes_downloaded,
            1 if success else 0,
            0 if success else 1,
            now,
        ))


def get_daily_stats(date_str: str = None) -> List[sqlite3.Row]:
    d = date_str or date.today().isoformat()
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM daily_stats WHERE date=?", (d,)
        ).fetchall()
