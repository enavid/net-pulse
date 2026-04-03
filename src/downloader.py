"""
    src/downloader.py – Local async file downloader with throttling and random pauses. Used when the coordinator itself acts as an agent (localhost).
"""

from __future__ import annotations


import os
import time
import uuid
import httpx
import random
import asyncio
from typing import Optional
from src.logger import get_logger
from dataclasses import dataclass


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


async def download_file(
    url: str,
    agent_label: str,
    speed_cap: int,
    pause_probability: float,
    pause_range: tuple[int, int],
    verify_ssl: bool = False,
) -> DownloadResult:
    result = DownloadResult(url=url, agent_label=agent_label)
    tmp = f"/tmp/np_{uuid.uuid4().hex}.tmp"
    start = time.monotonic()

    headers = {
        "User-Agent": _random_ua(),
        "Accept": "*/*",
        "Connection": "keep-alive",
    }

    log.info("Local download started | agent=%s | url=%s", agent_label, url)
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
        avg = result.bytes_downloaded / max(result.duration_seconds, 0.001) / 1024
        log.info(
            "Local download complete | agent=%s | bytes=%d | duration=%.1fs | avg=%.1f KB/s",
            agent_label, result.bytes_downloaded, result.duration_seconds, avg,
        )
    except Exception as exc:
        result.error = str(exc)
        result.duration_seconds = time.monotonic() - start
        log.warning("Local download failed | agent=%s | error=%s", agent_label, exc)
    finally:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass

    return result


def _random_ua() -> str:
    return random.choice([
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    ])
