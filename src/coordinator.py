"""
    src/coordinator.py – Orchestrates all agents for one 24-hour cycle.

    New responsibilities:
      1. Fetch metrics from [[sources]] and rank them by remaining quota.
      2. Build today's download plan and persist it to SQLite.
      3. Run all agents concurrently, feeding them the prioritised source list.
"""

from __future__ import annotations

import asyncio
from typing import List
from src import storage
from src.state import State
from src.agent import run_agent
from src.logger import get_logger
from src.config import Config, DownloadSource
from src.metrics import fetch_all_metrics, ServerMetric

log = get_logger("coordinator")


def _prioritise_sources(
    sources: List[DownloadSource],
    metrics: List[ServerMetric],
    monthly_usage: dict,   # source_label → bytes used this month
) -> List[DownloadSource]:
    """
    Return sources sorted by most remaining monthly quota (descending).
    Monitors are excluded — only [[sources]] are prioritised.
    Sources with no monthly_limit_gb set are treated as unlimited (sorted last).
    """
    def remaining_gb(src: DownloadSource) -> float:
        if src.monthly_limit_gb <= 0:
            return float("inf")
        used_bytes = monthly_usage.get(src.label, 0)
        allowed_bytes = src.monthly_allowed_gb * 1024 ** 3
        return max(0.0, (allowed_bytes - used_bytes) / 1024 ** 3)

    ranked = sorted(sources, key=remaining_gb, reverse=True)

    for s in ranked:
        rem = remaining_gb(s)
        log.info(
            "Source priority | label=%s | monthly_limit_gb=%.1f | allowed_pct=%.0f%% | remaining_gb=%.2f",
            s.label, s.monthly_limit_gb, s.usage_quota_pct * 100, rem,
        )

    return ranked


async def run_cycle(cfg: Config, state: State) -> None:
    log.info("Cycle start | agents=%d | sources=%d", len(cfg.agents), len(cfg.download_sources))

    storage.init_db()

    # Fetch metrics for all sources (for prioritisation)
    metrics: List[ServerMetric] = []
    if cfg.download_sources:
        metrics = await fetch_all_metrics(cfg.download_sources, cfg.verify_ssl)
        for m in metrics:
            if m.reachable:
                log.info("VPN metric | label=%s | rx_gb=%.2f | tx_gb=%.2f", m.label, m.rx_gb, m.tx_gb)
            else:
                log.warning("VPN metric unreachable | label=%s | error=%s", m.label, m.error)

    # Fetch metrics for monitors (informational only)
    if cfg.monitors:
        monitor_metrics = await fetch_all_metrics(cfg.monitors, cfg.verify_ssl)
        for m in monitor_metrics:
            if m.reachable:
                log.info("Monitor metric | label=%s | rx_gb=%.2f | tx_gb=%.2f", m.label, m.rx_gb, m.tx_gb)
            else:
                log.warning("Monitor unreachable | label=%s | error=%s", m.label, m.error)

    # Load monthly usage from DB and prioritise sources
    usage_rows = storage.get_monthly_usage()
    monthly_usage = {r["source_label"]: r["downloaded_bytes"] for r in usage_rows}
    prioritised_sources = _prioritise_sources(cfg.download_sources, metrics, monthly_usage)

    # Initialise in-memory state
    state.init(cfg.agents)

    # Run all agents concurrently
    tasks = [
        run_agent(agent, prioritised_sources, cfg, state)
        for agent in cfg.agents
    ]
    await asyncio.gather(*tasks)

    # Reload plan into state for UI
    state.load_plan_from_db()

    log.info("Cycle complete | date=%s", state.date)
