"""
    src/storage.py – SQLite-backed persistent storage.

    monthly_usage is now keyed by agent_label (not source_label)
    because agents are the ones downloading (and have quota limits).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import List, Optional
from dataclasses import dataclass
from datetime import datetime, date

_DB_FILE = Path("logs/netpulse.db")


@dataclass
class PlannedEvent:
    id: int
    date: str
    agent_label: str
    source_label: str
    scheduled_at: str
    status: str
    bytes_downloaded: int
    error: Optional[str]


def get_connection() -> sqlite3.Connection:
    os.makedirs(_DB_FILE.parent, exist_ok=True)
    conn = sqlite3.connect(str(_DB_FILE), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS planned_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                date            TEXT NOT NULL,
                agent_label     TEXT NOT NULL,
                source_label    TEXT NOT NULL,
                scheduled_at    TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending',
                bytes_downloaded INTEGER NOT NULL DEFAULT 0,
                error           TEXT
            );

            CREATE TABLE IF NOT EXISTS monthly_usage (
                agent_label     TEXT NOT NULL,
                year_month      TEXT NOT NULL,
                downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (agent_label, year_month)
            );

            CREATE INDEX IF NOT EXISTS idx_events_date   ON planned_events(date);
            CREATE INDEX IF NOT EXISTS idx_events_status ON planned_events(status);
        """)


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


def update_event_status(event_id: int, status: str, bytes_downloaded: int = 0, error: str = None) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE planned_events SET status=?, bytes_downloaded=?, error=? WHERE id=?",
            (status, bytes_downloaded, error, event_id),
        )


def get_events_for_date(date_str: str) -> List[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM planned_events WHERE date=? ORDER BY scheduled_at",
            (date_str,),
        ).fetchall()


def get_today_events() -> List[sqlite3.Row]:
    return get_events_for_date(date.today().isoformat())


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