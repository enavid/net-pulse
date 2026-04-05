"""
    src/state.py – Shared in-memory state. Loads from daily_stats DB on startup so data survives restarts.
"""

from __future__ import annotations

import os
import json
from datetime import datetime, date
from typing import Dict, List, Optional
from dataclasses import asdict, dataclass, field

_STATE_FILE = "logs/state.json"


@dataclass
class AgentStats:
    label: str
    daily_limit_gb: float
    downloaded_bytes: int = 0
    downloads_ok: int = 0
    downloads_fail: int = 0
    last_download_at: Optional[str] = None

    @property
    def downloaded_gb(self) -> float:
        return self.downloaded_bytes / 1024 ** 3


@dataclass
class PlannedEventView:
    id: int
    agent_label: str
    source_label: str
    scheduled_at: str
    status: str
    bytes_downloaded: int
    error: Optional[str]


@dataclass
class State:
    date: str = ""
    started_at: str = ""
    agents: Dict[str, AgentStats] = field(default_factory=dict)
    plan: List[PlannedEventView] = field(default_factory=list)

    def init(self, agent_configs) -> None:
        """
        Initialize state for a new cycle.
        Loads existing today's stats from DB so data survives restarts.
        """
        from src import storage
        today = date.today().isoformat()
        self.date = today
        self.started_at = datetime.now().isoformat()

        db_stats = {r["agent_label"]: r for r in storage.get_daily_stats(today)}

        for a in agent_configs:
            if a.label in db_stats:
                row = db_stats[a.label]
                self.agents[a.label] = AgentStats(
                    label=a.label,
                    daily_limit_gb=a.daily_limit_gb,
                    downloaded_bytes=row["downloaded_bytes"],
                    downloads_ok=row["downloads_ok"],
                    downloads_fail=row["downloads_fail"],
                    last_download_at=row["last_download_at"],
                )
            else:
                self.agents[a.label] = AgentStats(
                    label=a.label,
                    daily_limit_gb=a.daily_limit_gb,
                )
        self._save()

    def sync_agents_from_db(self) -> None:
        """
        Reload agent list from DB without resetting stats.
        Called after UI adds/removes an agent so overview reflects changes immediately.
        """
        from src import storage
        today    = date.today().isoformat()
        db_stats = {r["agent_label"]: r for r in storage.get_daily_stats(today)}
        db_agents = {r["label"]: r for r in storage.get_agents(enabled_only=True)}

        # Add new agents not yet in state
        for label, row in db_agents.items():
            if label not in self.agents:
                stats = db_stats.get(label)
                self.agents[label] = AgentStats(
                    label=label,
                    daily_limit_gb=row["daily_limit_gb"],
                    downloaded_bytes=stats["downloaded_bytes"] if stats else 0,
                    downloads_ok=stats["downloads_ok"] if stats else 0,
                    downloads_fail=stats["downloads_fail"] if stats else 0,
                    last_download_at=stats["last_download_at"] if stats else None,
                )
            else:
                # Update daily_limit_gb in case it changed
                self.agents[label].daily_limit_gb = row["daily_limit_gb"]

        # Remove agents deleted from DB
        for label in list(self.agents.keys()):
            if label not in db_agents:
                del self.agents[label]

        self._save()

    def load_plan_from_db(self) -> None:
        from src import storage
        rows = storage.get_today_events()
        self.plan = [
            PlannedEventView(
                id=r["id"],
                agent_label=r["agent_label"],
                source_label=r["source_label"],
                scheduled_at=r["scheduled_at"],
                status=r["status"],
                bytes_downloaded=r["bytes_downloaded"],
                error=r["error"],
            )
            for r in rows
        ]

    def record_download(self, agent_label: str, bytes_dl: int, success: bool) -> None:
        from src import storage
        if agent_label not in self.agents:
            return
        s = self.agents[agent_label]
        s.downloaded_bytes += bytes_dl
        if success:
            s.downloads_ok += 1
        else:
            s.downloads_fail += 1
        s.last_download_at = datetime.now().isoformat()
        # Persist to DB so restart doesn't lose data
        storage.upsert_daily_stats(agent_label, bytes_dl, success)
        self._save()

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "started_at": self.started_at,
            "agents": {k: asdict(v) for k, v in self.agents.items()},
            "plan": [asdict(e) for e in self.plan],
        }

    def _save(self) -> None:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        with open(_STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2)
