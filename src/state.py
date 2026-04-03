"""
    src/state.py – Shared in-memory state for the coordinator. Persisted to JSON so the panel can read it without locks.
"""

from __future__ import annotations

import os
import json
from datetime import datetime
from typing import Dict, Optional
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
class State:
    date: str = ""
    started_at: str = ""
    agents: Dict[str, AgentStats] = field(default_factory=dict)

    def init(self, agent_configs) -> None:
        self.date = datetime.now().strftime("%Y-%m-%d")
        self.started_at = datetime.now().isoformat()
        for a in agent_configs:
            self.agents[a.label] = AgentStats(label=a.label, daily_limit_gb=a.daily_limit_gb)
        self._save()

    def record_download(self, agent_label: str, bytes_dl: int, success: bool) -> None:
        if agent_label not in self.agents:
            return
        s = self.agents[agent_label]
        s.downloaded_bytes += bytes_dl
        if success:
            s.downloads_ok += 1
        else:
            s.downloads_fail += 1
        s.last_download_at = datetime.now().isoformat()
        self._save()

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "started_at": self.started_at,
            "agents": {k: asdict(v) for k, v in self.agents.items()},
        }

    def _save(self) -> None:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        with open(_STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2)
