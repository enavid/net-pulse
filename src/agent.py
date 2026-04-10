"""
src/agent.py – SSH-based remote agent dispatcher.

Handles:
  - Scheduled downloads (daily plan)
  - Manual on-demand jobs (with count, interval, speed_cap, delete_after)
  - Connection testing with 10 MB download check
  - SSH key auth (inline PEM or named key)
  - ProxyJump support
"""

from __future__ import annotations

import time
import random
import asyncio
import asyncssh
from src import storage
from src.state import State
from datetime import datetime
from typing import List, Optional
from src.logger import get_logger
from src.downloader import DownloadResult, download_file
from src.scheduler import generate_event_times, seconds_until
from src.config import AgentConfig, DownloadSource, RuntimeConfig

log = get_logger("agent")


# SSH connection factory

def _build_connect_kwargs(agent: AgentConfig, timeout: int = 90) -> dict:
    """Build asyncssh.connect kwargs, resolving key auth and ProxyJump."""
    kw = dict(
        host=agent.host, port=agent.port,
        username=agent.user, known_hosts=None,
        connect_timeout=timeout,
    )
    if agent.auth_type == "key" and agent.ssh_key:
        kw["client_keys"] = [asyncssh.import_private_key(agent.ssh_key)]
        kw["password"]    = None
    else:
        kw["password"] = agent.password

    if agent.has_jump:
        jump_kw = dict(
            host=agent.jump_host, port=agent.jump_port,
            username=agent.jump_user or agent.user,
            known_hosts=None, connect_timeout=timeout,
        )
        # Resolve jump key
        if agent.jump_key_name:
            jump_key_pem = storage.get_ssh_key_private(agent.jump_key_name)
            if jump_key_pem:
                jump_kw["client_keys"] = [asyncssh.import_private_key(jump_key_pem)]
                jump_kw["password"]    = None
            else:
                jump_kw["password"] = agent.password
        else:
            # Reuse same key/password as agent
            if agent.auth_type == "key" and agent.ssh_key:
                jump_kw["client_keys"] = [asyncssh.import_private_key(agent.ssh_key)]
                jump_kw["password"]    = None
            else:
                jump_kw["password"] = agent.password
        kw["tunnel"] = jump_kw  # asyncssh uses tunnel= for ProxyJump

    return kw


# Connection test

async def test_agent_connection(agent: AgentConfig, cfg: RuntimeConfig) -> tuple[bool, str]:
    log.info("Connection test started | agent=%s | host=%s | auth=%s | jump=%s",
             agent.label, agent.host, agent.auth_type, agent.jump_host or "none")

    if agent.is_local:
        try:
            import httpx
            log.info("Testing local download | agent=%s | url=%s", agent.label, cfg.connection_test_url)
            async with httpx.AsyncClient(verify=cfg.verify_ssl, timeout=15.0) as client:
                async with client.stream("GET", cfg.connection_test_url) as resp:
                    resp.raise_for_status()
                    downloaded = 0
                    async for chunk in resp.aiter_bytes(32 * 1024):
                        downloaded += len(chunk)
                        if downloaded >= 10 * 1024 * 1024:
                            break
            msg = f"localhost – download OK ({downloaded // 1024 // 1024} MB received)"
            log.info("Connection test passed | agent=%s | result=%s", agent.label, msg)
            return True, msg
        except Exception as exc:
            error = repr(exc) if not str(exc).strip() else str(exc)
            log.warning("Connection test failed | agent=%s | error=%s", agent.label, error)
            return False, f"localhost – download failed: {error}"

    try:
        kw       = _build_connect_kwargs(agent, timeout=90)
        no_check = "--insecure" if not cfg.verify_ssl else ""
        log.info("Testing SSH | agent=%s | host=%s:%d", agent.label, agent.host, agent.port)
        async with asyncssh.connect(**kw) as conn:
            log.info("SSH connected | agent=%s | running download test", agent.label)
            proc = await conn.run(
                f"curl -s {no_check} --max-time 30 -o /dev/null -r 0-10485760 '{cfg.connection_test_url}' && echo OK",
                timeout=60,
            )
            if proc.returncode == 0:
                msg = "SSH OK – download test passed"
                log.info("Connection test passed | agent=%s", agent.label)
                return True, msg
            else:
                error = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
                log.warning("Connection test failed | agent=%s | error=%s", agent.label, error)
                return False, f"SSH OK – download failed: {error}"
    except Exception as exc:
        error = repr(exc) if not str(exc).strip() else str(exc)
        log.warning("Connection test failed | agent=%s | error=%s", agent.label, error)
        return False, f"SSH failed: {error}"


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
    delete_after: bool = False,
) -> DownloadResult:
    result    = DownloadResult(url=url, agent_label=agent.label)
    speed_arg = f"--limit-rate {speed_cap}" if speed_cap > 0 else ""
    no_check  = "--insecure" if not verify_ssl else ""

    if delete_after:
        # Save to temp file, then delete — simulates actual disk usage
        cmd = f"tmp=$(mktemp); curl -s {no_check} {speed_arg} -o \"$tmp\" '{url}' && rm -f \"$tmp\" && echo OK"
    else:
        cmd = f"curl -s {no_check} {speed_arg} -o /dev/null '{url}' && echo OK"

    log.info("SSH download starting | agent=%s | host=%s | url=%s | retries=%d | delete=%s",
             agent.label, agent.host, url, max_retries, delete_after)
    log.debug("SSH cmd | agent=%s | %s", agent.label, cmd)

    attempt = 0
    for attempt in range(1, max_retries + 1):
        if attempt > 1:
            delay = random.randint(*retry_delay_range)
            log.info("SSH retry %d/%d | agent=%s | wait=%ds", attempt, max_retries, agent.label, delay)
            await asyncio.sleep(delay)

        start = time.monotonic()
        try:
            if random.random() < pause_probability:
                secs = random.randint(*pause_range)
                log.debug("Pre-download pause | agent=%s | secs=%d", agent.label, secs)
                await asyncio.sleep(secs)

            log.info("SSH connecting | agent=%s | attempt=%d/%d", agent.label, attempt, max_retries)
            kw = _build_connect_kwargs(agent, timeout=90)
            async with asyncssh.connect(**kw) as conn:
                log.info("SSH connected | agent=%s | executing download", agent.label)
                proc = await conn.run(cmd, timeout=3600)
                if proc.returncode == 0:
                    result.success          = True
                    result.bytes_downloaded = file_size_bytes
                    result.duration_seconds = time.monotonic() - start
                    result.error            = None
                    log.info("SSH download complete | agent=%s | attempt=%d/%d | duration=%.1fs",
                             agent.label, attempt, max_retries, result.duration_seconds)
                    break
                else:
                    stderr = proc.stderr.decode() if isinstance(proc.stderr, bytes) else (proc.stderr or "")
                    stdout = proc.stdout.decode() if isinstance(proc.stdout, bytes) else (proc.stdout or "")
                    result.error            = stderr.strip() or stdout.strip() or f"exit {proc.returncode}"
                    result.duration_seconds = time.monotonic() - start
                    log.warning("SSH download failed | agent=%s | attempt=%d/%d | error=%s",
                                agent.label, attempt, max_retries, result.error)
        except Exception as exc:
            result.error            = repr(exc) if not str(exc).strip() else str(exc)
            result.duration_seconds = time.monotonic() - start
            log.warning("SSH connection error | agent=%s | attempt=%d/%d | error=%s",
                        agent.label, attempt, max_retries, result.error)

    result.attempts = attempt
    return result


# Single download dispatch

async def _single_download(
    agent: AgentConfig,
    source: DownloadSource,
    cfg: RuntimeConfig,
    speed_cap_override: int = 0,
    delete_after: bool = False,
) -> DownloadResult:
    speed_cap = speed_cap_override if speed_cap_override > 0 else cfg.download_speed_cap
    if agent.is_local:
        return await download_file(
            url=source.download_url, agent_label=agent.label,
            speed_cap=speed_cap,
            pause_probability=cfg.download_pause_probability,
            pause_range=cfg.download_pause_range,
            verify_ssl=cfg.verify_ssl,
            max_retries=cfg.download_max_retries,
            retry_delay_range=cfg.download_retry_delay_range,
            delete_after=delete_after,
        )
    return await _run_remote_download(
        agent=agent, url=source.download_url,
        speed_cap=speed_cap,
        pause_probability=cfg.download_pause_probability,
        pause_range=cfg.download_pause_range,
        verify_ssl=cfg.verify_ssl,
        max_retries=cfg.download_max_retries,
        retry_delay_range=cfg.download_retry_delay_range,
        delete_after=delete_after,
    )


# Execute one planned event

async def _execute_event(
    event_id: Optional[int],
    source_label: str,
    sources: List[DownloadSource],
    agent: AgentConfig,
    cfg: RuntimeConfig,
    state: State,
    semaphore: asyncio.Semaphore,
) -> None:
    source = next((s for s in sources if s.label == source_label), sources[0])
    if event_id:
        storage.update_event_status(event_id, "running")
    state.load_plan_from_db()

    async with semaphore:
        result = await _single_download(agent, source, cfg)

    final_status = "done" if result.success else "failed"
    if event_id:
        storage.update_event_status(event_id, final_status, result.bytes_downloaded, result.error)
    if result.success:
        storage.add_monthly_usage(agent.label, result.bytes_downloaded)
    state.record_download(agent.label, result.bytes_downloaded, result.success)
    state.load_plan_from_db()


# Scheduled agent runner

async def run_agent(agent: AgentConfig, sources: List[DownloadSource], cfg: RuntimeConfig, state: State) -> None:
    if not sources:
        log.warning("No sources available | agent=%s", agent.label)
        return

    today = datetime.now().strftime("%Y-%m-%d")

    if not cfg.recreate_plan:
        existing = [r for r in storage.get_events_for_date(today) if r["agent_label"] == agent.label]
        if existing:
            log.info("Reusing existing plan | agent=%s | events=%d", agent.label, len(existing))
            pending   = [r for r in existing if r["status"] == "pending"]
            semaphore = asyncio.Semaphore(cfg.max_concurrent_downloads)
            await asyncio.gather(*[
                _execute_event(r["id"], r["source_label"], sources, agent, cfg, state, semaphore)
                for r in pending
            ])
            log.info("Agent finished | agent=%s", agent.label)
            return

    storage.delete_stale_pending(today, agent.label)
    variance     = 1.0 + random.uniform(-cfg.daily_variance, cfg.daily_variance)
    target_bytes = int(agent.daily_limit_gb * 1024 ** 3 * variance)
    n_events     = max(1, round(target_bytes / (1 * 1024 ** 3)))

    log.info("Agent plan | agent=%s | target_gb=%.2f | events=%d",
             agent.label, target_bytes / 1024 ** 3, n_events)

    event_times  = generate_event_times(n_events, cfg.schedule_weights)
    source_cycle = [sources[i % len(sources)] for i in range(n_events)]

    storage.insert_planned_events([
        {"date": today, "agent_label": agent.label,
         "source_label": source_cycle[i].label,
         "scheduled_at": event_times[i].isoformat()}
        for i in range(n_events)
    ])

    db_events    = storage.get_events_for_date(today)
    event_id_map = {
        (r["agent_label"], r["scheduled_at"]): r["id"]
        for r in db_events if r["agent_label"] == agent.label
    }
    semaphore = asyncio.Semaphore(cfg.max_concurrent_downloads)

    async def _job(idx: int):
        event_id = event_id_map.get((agent.label, event_times[idx].isoformat()))
        wait     = seconds_until(event_times[idx])
        if wait > 0:
            log.info("Download scheduled | agent=%s | source=%s | in=%.0fs",
                     agent.label, source_cycle[idx].label, wait)
            await asyncio.sleep(wait)
        await _execute_event(event_id, source_cycle[idx].label, sources, agent, cfg, state, semaphore)

    await asyncio.gather(*[_job(i) for i in range(n_events)])
    log.info("Agent finished daily cycle | agent=%s", agent.label)


# Manual job runner

async def run_manual_job(
    job_id: int, agent: AgentConfig, source: DownloadSource,
    cfg: RuntimeConfig, job_row,
) -> None:
    download_count   = int(job_row["download_count"])
    interval_type    = job_row["interval_type"]
    interval_seconds = int(job_row["interval_seconds"])
    speed_cap        = int(job_row["speed_cap"]) if job_row["speed_cap"] else 0
    delete_after     = bool(job_row["delete_after"])
    start_at         = job_row["start_at"]

    log.info("Manual job starting | id=%d | agent=%s | source=%s | count=%d | speed=%d | delete=%s",
             job_id, agent.label, source.label, download_count, speed_cap, delete_after)

    if start_at:
        try:
            target = datetime.fromisoformat(start_at)
            wait   = (target - datetime.now()).total_seconds()
            if wait > 0:
                log.info("Manual job waiting | id=%d | wait=%.0fs", job_id, wait)
                await asyncio.sleep(wait)
        except ValueError:
            log.warning("Manual job invalid start_at | id=%d | value=%s", job_id, start_at)

    if storage.is_job_cancelled(job_id):
        log.info("Manual job cancelled before start | id=%d", job_id)
        return

    storage.update_manual_job(job_id, "running")

    total_bytes = 0
    completed   = 0
    last_error  = None

    for i in range(download_count):
        if storage.is_job_cancelled(job_id):
            log.info("Manual job cancelled | id=%d | done=%d/%d", job_id, completed, download_count)
            break

        if i > 0:
            wait = (random.randint(max(1, interval_seconds // 2), interval_seconds * 2)
                    if interval_type == "random" else interval_seconds)
            log.info("Manual job interval | id=%d | wait=%ds | dl=%d/%d",
                     job_id, wait, i + 1, download_count)
            slept = 0
            while slept < wait:
                if storage.is_job_cancelled(job_id):
                    return
                chunk = min(5, wait - slept)
                await asyncio.sleep(chunk)
                slept += chunk

        try:
            result = await _single_download(agent, source, cfg, speed_cap, delete_after)
            if result.success:
                total_bytes += result.bytes_downloaded
                completed   += 1
                storage.add_monthly_usage(agent.label, result.bytes_downloaded)
                log.info("Manual job dl done | id=%d | dl=%d/%d | bytes=%d",
                         job_id, i + 1, download_count, result.bytes_downloaded)
            else:
                last_error = result.error
                log.warning("Manual job dl failed | id=%d | dl=%d/%d | error=%s",
                            job_id, i + 1, download_count, result.error)
            # Live progress update
            storage.update_manual_job(job_id, "progress", result.bytes_downloaded, completed)
        except Exception as exc:
            last_error = repr(exc) if not str(exc).strip() else str(exc)
            log.warning("Manual job dl error | id=%d | dl=%d/%d | error=%s",
                        job_id, i + 1, download_count, last_error)

    if storage.is_job_cancelled(job_id):
        return

    final_status = "done" if completed > 0 else "failed"
    storage.update_manual_job(job_id, final_status, 0, completed, last_error)
    log.info("Manual job finished | id=%d | agent=%s | done=%d/%d | total=%d bytes",
             job_id, agent.label, completed, download_count, total_bytes)
