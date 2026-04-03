"""
    src/coordinator.py – Orchestrates all agents for one 24-hour cycle.
"""

from __future__ import annotations

import asyncio
from src.state import State
from src.config import Config
from src.agent import run_agent
from src.logger import get_logger
from src.metrics import fetch_all_metrics


log = get_logger("coordinator")


async def run_cycle(cfg: Config, state: State) -> None:
    log.info("Cycle start | agents=%d | sources=%d", len(cfg.agents), len(cfg.download_sources))

    # Fetch metrics to show VPN server load (informational)
    if cfg.download_sources:
        all_sources = cfg.download_sources + cfg.monitors
        metrics = await fetch_all_metrics(all_sources, cfg.verify_ssl)
        for m in metrics:
            if m.reachable:
                log.info("VPN metric | label=%s | rx_gb=%.2f | tx_gb=%.2f", m.label, m.rx_gb, m.tx_gb)
            else:
                log.warning("VPN metric unreachable | label=%s | error=%s", m.label, m.error)

    state.init(cfg.agents)

    # Run all agents concurrently
    tasks = [run_agent(agent, cfg.download_sources, cfg, state) for agent in cfg.agents]
    await asyncio.gather(*tasks)

    log.info("Cycle complete | date=%s", state.date)
