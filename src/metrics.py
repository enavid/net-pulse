"""
    src/metrics.py – Fetch and parse VPN server metrics from the JSON endpoint.
"""

from __future__ import annotations

import httpx
import asyncio
from typing import Optional
from src.logger import get_logger
from dataclasses import dataclass


log = get_logger("metrics")


@dataclass
class ServerMetric:
    label: str
    metric_url: str
    rx_gb: float = 0.0
    tx_gb: float = 0.0
    reachable: bool = False
    error: Optional[str] = None


async def fetch_metric(label: str, url: str, verify_ssl: bool = False) -> ServerMetric:
    metric = ServerMetric(label=label, metric_url=url)
    try:
        async with httpx.AsyncClient(verify=verify_ssl, timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            net = data.get("network", {})
            metric.rx_gb = float(net.get("rx_gb", 0.0))
            metric.tx_gb = float(net.get("tx_gb", 0.0))
            metric.reachable = True
            log.debug("Metric fetched | label=%s | rx_gb=%.2f | tx_gb=%.2f", label, metric.rx_gb, metric.tx_gb)
    except Exception as exc:
        metric.reachable = False
        metric.error = str(exc)
        log.warning("Metric fetch failed | label=%s | error=%s", label, exc)
    return metric


async def fetch_all_metrics(sources, verify_ssl=False):
    tasks = [fetch_metric(s.label, s.metric_url, verify_ssl) for s in sources]
    return await asyncio.gather(*tasks)
