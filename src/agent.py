"""
    src/agent.py – SSH-based remote agent dispatcher.

    For each remote agent, opens a short-lived SSH connection, runs a
    wget/curl command to download the file, then closes the connection.
    The coordinator itself (localhost) is handled by the local downloader.
"""

from __future__ import annotations


import time
import random
import asyncssh
import asyncio
from typing import List
from src.state import State
from src.logger import get_logger
from src.downloader import download_file, DownloadResult
from src.config import AgentConfig, Config, DownloadSource
from src.scheduler import generate_event_times, seconds_until


log = get_logger("agent")


# Connection test

async def test_agent_connection(agent: AgentConfig) -> tuple[bool, str]:
    """
        Try to open and immediately close an SSH connection.
        Returns (success, message).
    """
    if agent.is_local:
        return True, "localhost – no SSH needed"
    try:
        async with asyncssh.connect(
            host=agent.host,
            port=agent.port,
            username=agent.user,
            password=agent.password,
            known_hosts=None,
            connect_timeout=10,
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
) -> DownloadResult:
    result = DownloadResult(url=url, agent_label=agent.label)
    start = time.monotonic()

    # Build wget command with speed limit
    speed_arg = f"--limit-rate={speed_cap // 1024}k" if speed_cap > 0 else ""
    no_check = "--no-check-certificate" if not verify_ssl else ""
    cmd = f"wget -q {speed_arg} {no_check} -O /dev/null '{url}' && echo OK"

    log.info("SSH download starting | agent=%s | host=%s | url=%s", agent.label, agent.host, url)

    try:
        # Optional: simulate pause before connecting
        if random.random() < pause_probability:
            secs = random.randint(*pause_range)
            log.debug("Pre-download pause | agent=%s | secs=%d", agent.label, secs)
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
                # Estimate bytes from the URL size hint (best effort)
                result.bytes_downloaded = 1 * 1024 ** 3  # 1 GB default assumption
                result.duration_seconds = time.monotonic() - start
                log.info(
                    "SSH download complete | agent=%s | duration=%.1fs",
                    agent.label, result.duration_seconds,
                )
            else:
                result.error = proc.stderr.strip() or f"exit code {proc.returncode}"
                result.duration_seconds = time.monotonic() - start
                log.warning("SSH download failed | agent=%s | error=%s", agent.label, result.error)
    except Exception as exc:
        result.error = str(exc)
        result.duration_seconds = time.monotonic() - start
        log.warning("SSH connection error | agent=%s | error=%s", agent.label, exc)

    return result


# Agent runner

async def run_agent(agent: AgentConfig, sources: List[DownloadSource], cfg: Config, state: State) -> None:
    """
        Plan and execute all downloads for one agent over 24 hours.
    """
    variance = 1.0 + random.uniform(-cfg.daily_variance, cfg.daily_variance)
    target_bytes = int(agent.daily_limit_gb * 1024 ** 3 * variance)
    approx_file_size = 1 * 1024 ** 3  # 1 GB per download
    n_events = max(1, round(target_bytes / approx_file_size))

    log.info(
        "Agent plan | agent=%s | target_gb=%.2f | events=%d",
        agent.label, target_bytes / 1024 ** 3, n_events,
    )

    event_times = generate_event_times(n_events, cfg.schedule_weights)
    semaphore = asyncio.Semaphore(cfg.max_concurrent_downloads)

    async def _job(event_time, source: DownloadSource):
        wait = seconds_until(event_time)
        if wait > 0:
            log.info(
                "Download scheduled | agent=%s | source=%s | in=%.0fs",
                agent.label, source.label, wait,
            )
            await asyncio.sleep(wait)

        async with semaphore:
            if agent.is_local:
                result = await download_file(
                    url=source.download_url,
                    agent_label=agent.label,
                    speed_cap=cfg.download_speed_cap,
                    pause_probability=cfg.download_pause_probability,
                    pause_range=cfg.download_pause_range,
                    verify_ssl=cfg.verify_ssl,
                )
            else:
                result = await _run_remote_download(
                    agent=agent,
                    url=source.download_url,
                    speed_cap=cfg.download_speed_cap,
                    pause_probability=cfg.download_pause_probability,
                    pause_range=cfg.download_pause_range,
                    verify_ssl=cfg.verify_ssl,
                )

            state.record_download(agent.label, result.bytes_downloaded, result.success)

    # Round-robin sources across events
    source_cycle = [sources[i % len(sources)] for i in range(n_events)]
    tasks = [_job(t, s) for t, s in zip(event_times, source_cycle)]
    await asyncio.gather(*tasks)
    log.info("Agent finished daily cycle | agent=%s", agent.label)
