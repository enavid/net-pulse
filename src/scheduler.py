"""
    src/scheduler.py – Distribute download events across 24 hours following a human-like activity curve.
"""

from __future__ import annotations

import random
from typing import List
from datetime import datetime, timedelta


def generate_event_times(count: int, weights: List[float], base: datetime | None = None) -> List[datetime]:
    """
        Return `count` datetime objects spread across the next 24 hours.
        Events are denser during high-weight periods (morning/evening).
    """
    if base is None:
        base = datetime.now()

    minutes_in_day = 24 * 60
    minute_weights: List[float] = []
    for m in range(minutes_in_day):
        bucket = (m // 60) // 6
        minute_weights.append(weights[bucket])

    chosen = random.choices(range(minutes_in_day), weights=minute_weights, k=count)
    events: List[datetime] = []
    for m in sorted(chosen):
        jitter = random.randint(-120, 120)
        events.append(base + timedelta(minutes=m, seconds=jitter))
    return events


def seconds_until(target: datetime) -> float:
    return max(0.0, (target - datetime.now()).total_seconds())
