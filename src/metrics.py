"""
src/metrics.py – Fetch and parse VPN/monitor server metrics.
Returns full structured data for rich UI display.
"""

from __future__ import annotations

import httpx
import asyncio
from src.logger import get_logger
from dataclasses import dataclass, field
from typing import List, Optional, Protocol


log = get_logger("metrics")


class HasMetricUrl(Protocol):
    label: str
    metric_url: str


@dataclass
class NetworkStats:
    rx_gb: float = 0.0
    tx_gb: float = 0.0
    rx_mb: float = 0.0
    tx_mb: float = 0.0
    rx_packets: int = 0
    tx_packets: int = 0
    rx_errors: int = 0
    tx_errors: int = 0
    rx_drop: int = 0
    tx_drop: int = 0
    interface: str = ""


@dataclass
class MemoryStats:
    total_mb: float = 0.0
    used_mb: float = 0.0
    free_mb: float = 0.0
    used_pct: float = 0.0


@dataclass
class SystemStats:
    uptime_seconds: int = 0
    uptime_human: str = ""
    load_avg_1m: float = 0.0
    load_avg_5m: float = 0.0
    load_avg_15m: float = 0.0
    memory: MemoryStats = field(default_factory=MemoryStats)


@dataclass
class ServerMetric:
    label: str
    metric_url: str
    reachable: bool = False
    error: Optional[str] = None
    updated_at: str = ""
    interface: str = ""
    network: NetworkStats = field(default_factory=NetworkStats)
    system: SystemStats = field(default_factory=SystemStats)

    # Legacy flat fields for backward compat
    @property
    def rx_gb(self) -> float:
        return self.network.rx_gb

    @property
    def tx_gb(self) -> float:
        return self.network.tx_gb


async def fetch_metric(label: str, url: str, verify_ssl: bool = False) -> ServerMetric:
    metric = ServerMetric(label=label, metric_url=url)
    if not url:
        metric.error = "no metric URL configured"
        return metric
    try:
        async with httpx.AsyncClient(verify=verify_ssl, timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        net = data.get("network", {})
        sys_ = data.get("system", {})
        mem  = sys_.get("memory", {})

        metric.reachable  = True
        metric.updated_at = data.get("updated_at", "")
        metric.interface  = data.get("interface", "")

        metric.network = NetworkStats(
            rx_gb=float(net.get("rx_gb", 0.0)),
            tx_gb=float(net.get("tx_gb", 0.0)),
            rx_mb=float(net.get("rx_mb", 0.0)),
            tx_mb=float(net.get("tx_mb", 0.0)),
            rx_packets=int(net.get("rx_packets", 0)),
            tx_packets=int(net.get("tx_packets", 0)),
            rx_errors=int(net.get("rx_errors", 0)),
            tx_errors=int(net.get("tx_errors", 0)),
            rx_drop=int(net.get("rx_drop", 0)),
            tx_drop=int(net.get("tx_drop", 0)),
            interface=data.get("interface", ""),
        )
        metric.system = SystemStats(
            uptime_seconds=int(sys_.get("uptime_seconds", 0)),
            uptime_human=sys_.get("uptime_human", ""),
            load_avg_1m=float(sys_.get("load_avg_1m", 0.0)),
            load_avg_5m=float(sys_.get("load_avg_5m", 0.0)),
            load_avg_15m=float(sys_.get("load_avg_15m", 0.0)),
            memory=MemoryStats(
                total_mb=float(mem.get("total_mb", 0.0)),
                used_mb=float(mem.get("used_mb", 0.0)),
                free_mb=float(mem.get("free_mb", 0.0)),
                used_pct=float(mem.get("used_pct", 0.0)),
            ),
        )
        log.debug("Metric fetched | label=%s | rx=%.2f GB | tx=%.2f GB",
                  label, metric.network.rx_gb, metric.network.tx_gb)
    except Exception as exc:
        metric.reachable = False
        metric.error     = repr(exc) if not str(exc).strip() else str(exc)
        log.warning("Metric fetch failed | label=%s | error=%s", label, metric.error)
    return metric


async def fetch_all_metrics(sources: List[HasMetricUrl], verify_ssl: bool = False) -> List[ServerMetric]:
    tasks = [fetch_metric(s.label, s.metric_url, verify_ssl) for s in sources]
    return await asyncio.gather(*tasks)


def metric_to_dict(m: ServerMetric, is_monitor: bool = False) -> dict:
    return {
        "label":       m.label,
        "reachable":   m.reachable,
        "error":       m.error,
        "is_monitor":  is_monitor,
        "updated_at":  m.updated_at,
        "interface":   m.interface,
        "rx_gb":       m.network.rx_gb,
        "tx_gb":       m.network.tx_gb,
        "rx_mb":       m.network.rx_mb,
        "tx_mb":       m.network.tx_mb,
        "rx_packets":  m.network.rx_packets,
        "tx_packets":  m.network.tx_packets,
        "rx_errors":   m.network.rx_errors,
        "tx_errors":   m.network.tx_errors,
        "rx_drop":     m.network.rx_drop,
        "tx_drop":     m.network.tx_drop,
        "uptime_human":  m.system.uptime_human,
        "load_1m":       m.system.load_avg_1m,
        "load_5m":       m.system.load_avg_5m,
        "load_15m":      m.system.load_avg_15m,
        "mem_total_mb":  m.system.memory.total_mb,
        "mem_used_mb":   m.system.memory.used_mb,
        "mem_used_pct":  m.system.memory.used_pct,
    }
