"""
    src/downloader.py – Local async file downloader with throttling, random pauses and automatic retry on failure.
"""

from __future__ import annotations


import os
import time
import uuid
import httpx
import random
import asyncio
from typing import Optional
from dataclasses import dataclass
from src.logger import get_logger

log = get_logger("downloader")
_CHUNK = 32 * 1024  # 32 KB


@dataclass
class DownloadResult:
    url: str
    agent_label: str
    bytes_downloaded: int = 0
    success: bool = False
    error: Optional[str] = None
    duration_seconds: float = 0.0
    attempts: int = 0


async def _attempt_download(
    url: str,
    agent_label: str,
    speed_cap: int,
    pause_probability: float,
    pause_range: tuple[int, int],
    verify_ssl: bool,
) -> DownloadResult:
    """Single download attempt — no retry logic here."""
    result = DownloadResult(url=url, agent_label=agent_label, attempts=1)
    tmp = f"/tmp/np_{uuid.uuid4().hex}.tmp"
    start = time.monotonic()

    headers = {
        "User-Agent": _random_ua(),
        "Accept": "*/*",
        "Connection": "keep-alive",
    }

    try:
        async with httpx.AsyncClient(verify=verify_ssl, timeout=None, headers=headers) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                paused = False
                with open(tmp, "wb") as fh:
                    async for chunk in resp.aiter_bytes(_CHUNK):
                        fh.write(chunk)
                        result.bytes_downloaded += len(chunk)

                        if speed_cap > 0:
                            await asyncio.sleep(len(chunk) / speed_cap)

                        if (
                            not paused
                            and result.bytes_downloaded > 1 * 1024 ** 2
                            and random.random() < pause_probability
                        ):
                            secs = random.randint(*pause_range)
                            log.debug("Download paused | agent=%s | secs=%d", agent_label, secs)
                            paused = True
                            await asyncio.sleep(secs)

        result.success = True
        result.duration_seconds = time.monotonic() - start
    except Exception as exc:
        result.error = str(exc)
        result.duration_seconds = time.monotonic() - start
    finally:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass

    return result


async def download_file(
    url: str,
    agent_label: str,
    speed_cap: int,
    pause_probability: float,
    pause_range: tuple[int, int],
    verify_ssl: bool = False,
    max_retries: int = 3,
    retry_delay_range: tuple[int, int] = (30, 120),
) -> DownloadResult:
    """
    Download with automatic retry.
    On each failure waits a random delay before retrying.
    Returns the last result regardless of success.
    """
    log.info("Local download started | agent=%s | url=%s | max_retries=%d", agent_label, url, max_retries)

    last: Optional[DownloadResult] = None
    total_attempts = 0

    for attempt in range(1, max_retries + 1):
        if attempt > 1:
            delay = random.randint(*retry_delay_range)
            log.info("Retry %d/%d | agent=%s | waiting=%ds", attempt, max_retries, agent_label, delay)
            await asyncio.sleep(delay)

        result = await _attempt_download(url, agent_label, speed_cap, pause_probability, pause_range, verify_ssl)
        total_attempts += 1
        last = result

        if result.success:
            avg = result.bytes_downloaded / max(result.duration_seconds, 0.001) / 1024
            log.info(
                "Local download complete | agent=%s | bytes=%d | duration=%.1fs | avg=%.1f KB/s | attempt=%d/%d",
                agent_label, result.bytes_downloaded, result.duration_seconds, avg, attempt, max_retries,
            )
            break
        else:
            log.warning(
                "Local download failed | agent=%s | attempt=%d/%d | error=%s",
                agent_label, attempt, max_retries, result.error,
            )

    last.attempts = total_attempts
    return last


def _random_ua() -> str:
    return random.choice([
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    ])
