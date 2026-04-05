"""
    src/coordinator.py – Orchestrates one 24-hour cycle across all agents.

    Each cycle reloads RuntimeConfig from DB so any UI changes take effect without restarting the process.
"""

from __future__ import annotations

import asyncio
from src import storage
from src.state import State
from src.logger import get_logger
from src.metrics import fetch_all_metrics
from src.agent import run_agent, run_manual_job
from src.config import SystemConfig, load_runtime_config

log = get_logger("coordinator")


async def run_cycle(sys_cfg: SystemConfig, state: State) -> None:
    # Reload runtime config from DB at the start of every cycle
    cfg = load_runtime_config(sys_cfg)

    log.info("Cycle start | agents=%d | sources=%d", len(cfg.agents), len(cfg.download_sources))

    storage.init_db()

    # Fetch VPN server metrics
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

    #  Sort agents by remaining monthly quota
    usage_rows    = storage.get_monthly_usage()
    monthly_usage = {r["agent_label"]: r["downloaded_bytes"] for r in usage_rows}

    def agent_remaining_gb(agent) -> float:
        if agent.monthly_limit_gb <= 0:
            return float("inf")
        used    = monthly_usage.get(agent.label, 0)
        allowed = agent.monthly_allowed_gb * 1024 ** 3
        return max(0.0, (allowed - used) / 1024 ** 3)

    sorted_agents = sorted(cfg.agents, key=agent_remaining_gb, reverse=True)
    for a in sorted_agents:
        log.info("Agent priority | label=%s | remaining_gb=%.2f", a.label, agent_remaining_gb(a))

    #  Initialise state (loads today's stats from DB)
    state.init(cfg.agents)
    state.load_plan_from_db()

    #  Run scheduled downloads + process any pending manual jobs
    agent_tasks = [run_agent(agent, cfg.download_sources, cfg, state) for agent in sorted_agents]
    manual_task = _process_manual_jobs(cfg, state)

    await asyncio.gather(*agent_tasks, manual_task)

    state.load_plan_from_db()
    log.info("Cycle complete | date=%s", state.date)


async def _process_manual_jobs(cfg, state: State) -> None:
    """Pick up pending manual jobs and dispatch them to the appropriate agent."""
    jobs = storage.get_pending_manual_jobs()
    if not jobs:
        return

    log.info("Manual jobs pending | count=%d", len(jobs))

    agent_map  = {a.label: a for a in cfg.agents}
    source_map = {s.label: s for s in cfg.download_sources}

    tasks = []
    for job in jobs:
        agent  = agent_map.get(job["agent_label"])
        source = source_map.get(job["source_label"])
        if not agent or not source:
            log.warning("Manual job skipped | id=%d | reason=agent or source not found", job["id"])
            storage.update_manual_job(job["id"], "failed", error="agent or source not found")
            continue
        # Pass full job row so runner can read count/interval/start_at
        tasks.append(run_manual_job(job["id"], agent, source, cfg, job))

    if tasks:
        await asyncio.gather(*tasks)
