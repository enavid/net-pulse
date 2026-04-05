"""
    src/coordinator.py – Orchestrates all agents for one 24-hour cycle.

    New responsibilities:
      1. Fetch metrics from [[sources]] and rank them by remaining quota.
      2. Build today's download plan and persist it to SQLite.
      3. Run all agents concurrently, feeding them the prioritised source list.
"""

from __future__ import annotations

import asyncio
from src import storage
from src.state import State
from src.config import Config
from src.agent import run_agent
from src.logger import get_logger
from src.metrics import fetch_all_metrics

log = get_logger("coordinator")


async def run_cycle(cfg: Config, state: State) -> None:
    log.info("Cycle start | agents=%d | sources=%d", len(cfg.agents), len(cfg.download_sources))

    storage.init_db()

    # Fetch metrics for sources (informational only — no quota)
    if cfg.download_sources:
        metrics = await fetch_all_metrics(cfg.download_sources, cfg.verify_ssl)
        for m in metrics:
            if m.reachable:
                log.info("VPN metric | label=%s | rx_gb=%.2f | tx_gb=%.2f", m.label, m.rx_gb, m.tx_gb)
            else:
                log.warning("VPN metric unreachable | label=%s | error=%s", m.label, m.error)

    if cfg.monitors:
        monitor_metrics = await fetch_all_metrics(cfg.monitors, cfg.verify_ssl)
        for m in monitor_metrics:
            if m.reachable:
                log.info("Monitor metric | label=%s | rx_gb=%.2f | tx_gb=%.2f", m.label, m.rx_gb, m.tx_gb)
            else:
                log.warning("Monitor unreachable | label=%s | error=%s", m.label, m.error)

    # Load monthly usage and sort agents by remaining quota (most remaining → runs more)
    usage_rows = storage.get_monthly_usage()
    monthly_usage = {r["agent_label"]: r["downloaded_bytes"] for r in usage_rows}

    def agent_remaining_gb(agent) -> float:
        if agent.monthly_limit_gb <= 0:
            return float("inf")
        used = monthly_usage.get(agent.label, 0)
        allowed = agent.monthly_allowed_gb * 1024 ** 3
        return max(0.0, (allowed - used) / 1024 ** 3)

    sorted_agents = sorted(cfg.agents, key=agent_remaining_gb, reverse=True)
    for a in sorted_agents:
        log.info("Agent priority | label=%s | remaining_gb=%.2f", a.label, agent_remaining_gb(a))

    state.init(cfg.agents)
    state.load_plan_from_db()

    tasks = [run_agent(agent, cfg.download_sources, cfg, state) for agent in sorted_agents]
    await asyncio.gather(*tasks)

    state.load_plan_from_db()
    log.info("Cycle complete | date=%s", state.date)