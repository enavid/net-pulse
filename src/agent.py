"""
src/agent.py – SSH-based remote agent dispatcher.

Each agent:
  1. Checks for existing today's plan — reuses it if found, creates new if not.
  2. Persists the plan to SQLite.
  3. Executes each pending event with retry support, updating status in DB.
"""

from __future__ import annotations


import time
import random
import asyncio
import asyncssh
from src import storage
from src.state import State
from datetime import datetime
from src.logger import get_logger
from typing import List, Optional
from src.downloader import download_file, DownloadResult
from src.config import AgentConfig, Config, DownloadSource
from src.scheduler import generate_event_times, seconds_until


log = get_logger("agent")


# ── Connection test ───────────────────────────────────────────────────────────

async def test_agent_connection(agent: AgentConfig) -> tuple[bool, str]:
    if agent.is_local:
        return True, "localhost – no SSH needed"
    try:
        async with asyncssh.connect(
            host=agent.host,
            port=agent.port,
            username=agent.user,
            password=agent.password,
            known_hosts=None,
            connect_timeout=60,
        ):
            return True, "SSH connection successful"
    except Exception as exc:
        return False, str(exc)


# Remote download via SSH

async def _run_remote_download(
    agent: AgentConfig,
    url: str,
    speed_cap: int,
    pause_probability: float,
    pause_range: tuple[int, int],
    verify_ssl: bool,
    file_size_bytes: int = 1 * 1024 ** 3,
    max_retries: int = 3,
    retry_delay_range: tuple[int, int] = (30, 120),
) -> DownloadResult:
    result = DownloadResult(url=url, agent_label=agent.label)
    speed_arg = f"--limit-rate={speed_cap // 1024}k" if speed_cap > 0 else ""
    no_check  = "--no-check-certificate" if not verify_ssl else ""
    cmd = f"wget -q {speed_arg} {no_check} -O /dev/null '{url}' && echo OK"

    log.info("SSH download starting | agent=%s | host=%s | url=%s | max_retries=%d",
             agent.label, agent.host, url, max_retries)

    for attempt in range(1, max_retries + 1):
        if attempt > 1:
            delay = random.randint(*retry_delay_range)
            log.info("SSH retry %d/%d | agent=%s | waiting=%ds", attempt, max_retries, agent.label, delay)
            await asyncio.sleep(delay)

        start = time.monotonic()
        try:
            if random.random() < pause_probability:
                secs = random.randint(*pause_range)
                await asyncio.sleep(secs)

            async with asyncssh.connect(
                host=agent.host,
                port=agent.port,
                username=agent.user,
                password=agent.password,
                known_hosts=None,
                connect_timeout=15,
            ) as conn:
                proc = await conn.run(cmd, timeout=3600)
                if proc.returncode == 0:
                    result.success = True
                    result.bytes_downloaded = file_size_bytes
                    result.duration_seconds = time.monotonic() - start
                    result.error = None
                    log.info("SSH download complete | agent=%s | attempt=%d/%d | duration=%.1fs",
                             agent.label, attempt, max_retries, result.duration_seconds)
                    break
                else:
                    result.error = proc.stderr.strip() or f"exit code {proc.returncode}"
                    result.duration_seconds = time.monotonic() - start
                    log.warning("SSH download failed | agent=%s | attempt=%d/%d | error=%s",
                                agent.label, attempt, max_retries, result.error)
        except Exception as exc:
            result.error = repr(exc) if not str(exc).strip() else str(exc)
            result.duration_seconds = time.monotonic() - start
            log.warning("SSH connection error | agent=%s | attempt=%d/%d | error=%s",
                        agent.label, attempt, max_retries, result.error)

    result.attempts = attempt
    return result


# Agent runner

async def run_agent(agent: AgentConfig, sources: List[DownloadSource], cfg: Config, state: State) -> None:
    if not sources:
        log.warning("No sources available | agent=%s", agent.label)
        return

    today = datetime.now().strftime("%Y-%m-%d")

    # Check for existing plan, delete stale pending events
    storage.delete_stale_pending(today, agent.label)

    existing = [r for r in storage.get_events_for_date(today) if r["agent_label"] == agent.label]
    if existing:
        log.info("Reusing existing plan | agent=%s | events=%d", agent.label, len(existing))
        # Only run events that are still pending
        pending = [r for r in existing if r["status"] == "pending"]
        log.info("Pending events to execute | agent=%s | count=%d", agent.label, len(pending))
        semaphore = asyncio.Semaphore(cfg.max_concurrent_downloads)
        tasks = [_execute_event(r["id"], r["source_label"], sources, agent, cfg, state, semaphore) for r in pending]
        await asyncio.gather(*tasks)
        log.info("Agent finished daily cycle | agent=%s", agent.label)
        return

    # Build new plan 
    variance     = 1.0 + random.uniform(-cfg.daily_variance, cfg.daily_variance)
    target_bytes = int(agent.daily_limit_gb * 1024 ** 3 * variance)
    approx_size  = 1 * 1024 ** 3
    n_events     = max(1, round(target_bytes / approx_size))

    log.info("Agent plan | agent=%s | target_gb=%.2f | events=%d", agent.label, target_bytes / 1024 ** 3, n_events)

    event_times  = generate_event_times(n_events, cfg.schedule_weights)
    source_cycle = [sources[i % len(sources)] for i in range(n_events)]

    plan_rows = [
        {
            "date": today,
            "agent_label": agent.label,
            "source_label": source_cycle[i].label,
            "scheduled_at": event_times[i].isoformat(),
        }
        for i in range(n_events)
    ]
    storage.insert_planned_events(plan_rows)

    # Map scheduled_at → event id
    db_events = storage.get_events_for_date(today)
    event_id_map = {
        (r["agent_label"], r["scheduled_at"]): r["id"]
        for r in db_events
        if r["agent_label"] == agent.label
    }

    semaphore = asyncio.Semaphore(cfg.max_concurrent_downloads)

    async def _job(idx: int):
        event_id  = event_id_map.get((agent.label, event_times[idx].isoformat()))
        source    = source_cycle[idx]
        wait      = seconds_until(event_times[idx])
        if wait > 0:
            log.info("Download scheduled | agent=%s | source=%s | in=%.0fs", agent.label, source.label, wait)
            await asyncio.sleep(wait)
        await _execute_event(event_id, source.label, sources, agent, cfg, state, semaphore)

    tasks = [_job(i) for i in range(n_events)]
    await asyncio.gather(*tasks)
    log.info("Agent finished daily cycle | agent=%s", agent.label)


async def _execute_event(
    event_id: Optional[int],
    source_label: str,
    sources: List[DownloadSource],
    agent: AgentConfig,
    cfg: Config,
    state: State,
    semaphore: asyncio.Semaphore,
) -> None:
    """Execute a single planned download event."""
    source = next((s for s in sources if s.label == source_label), sources[0])

    if event_id:
        storage.update_event_status(event_id, "running")
    state.load_plan_from_db()

    async with semaphore:
        if agent.is_local:
            result = await download_file(
                url=source.download_url,
                agent_label=agent.label,
                speed_cap=cfg.download_speed_cap,
                pause_probability=cfg.download_pause_probability,
                pause_range=cfg.download_pause_range,
                verify_ssl=cfg.verify_ssl,
                max_retries=getattr(cfg, "download_max_retries", 3),
                retry_delay_range=getattr(cfg, "download_retry_delay_range", (30, 120)),
            )
        else:
            result = await _run_remote_download(
                agent=agent,
                url=source.download_url,
                speed_cap=cfg.download_speed_cap,
                pause_probability=cfg.download_pause_probability,
                pause_range=cfg.download_pause_range,
                verify_ssl=cfg.verify_ssl,
                max_retries=getattr(cfg, "download_max_retries", 3),
                retry_delay_range=getattr(cfg, "download_retry_delay_range", (30, 120)),
            )

    final_status = "done" if result.success else "failed"
    if event_id:
        storage.update_event_status(event_id, final_status, result.bytes_downloaded, result.error)

    if result.success:
        storage.add_monthly_usage(agent.label, result.bytes_downloaded)

    state.record_download(agent.label, result.bytes_downloaded, result.success)
    state.load_plan_from_db()