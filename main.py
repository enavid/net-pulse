"""
    main.py – NetPulse entry point.

    Starts the web panel on 127.0.0.1 and runs the coordinator loop.
    Access the panel via SSH tunnel: ssh -L 7070:127.0.0.1:7070 user@your-server Then open http://127.0.0.1:7070 in your browser.
"""

from __future__ import annotations

import asyncio
import sys
import time
import threading

import uvicorn

from src.config import load_config
from src.coordinator import run_cycle
from src.logger import setup_logger, get_logger
from src.panel import create_app
from src.state import State


def _start_panel(cfg, state) -> None:
    """Run the FastAPI panel in a background thread."""
    app = create_app(cfg, state)
    uvicorn.run(
        app,
        host=cfg.panel_host,
        port=cfg.panel_port,
        log_level="warning",   # uvicorn noise suppressed; we have our own logger
        access_log=False,
    )


async def _run_loop(cfg, state, total_days: int) -> None:
    log = get_logger("main")
    day = 1
    while True:
        log.info("Starting day %d | total_days=%s", day, total_days if total_days > 0 else "∞")
        await run_cycle(cfg, state)
        log.info("Day %d complete", day)
        if total_days > 0 and day >= total_days:
            log.info("All %d day(s) complete. Exiting.", total_days)
            break
        day += 1
        log.info("Waiting 60 s before next cycle...")
        await asyncio.sleep(60)


def main() -> None:
    cfg = load_config()
    setup_logger(cfg.log_file, cfg.log_level)
    log = get_logger("main")

    raw = input("How many days to run? (0 = run forever): ").strip()
    total_days = int(raw) if raw.isdigit() else 0

    state = State()

    log.info(
        "NetPulse starting | panel=http://%s:%d | agents=%d | sources=%d | monitors_only=%d",
        cfg.panel_host, cfg.panel_port, len(cfg.agents), len(cfg.download_sources),len(cfg.monitors),
    )
    log.info(
        "SSH tunnel command: ssh -L %d:127.0.0.1:%d user@<server>",
        cfg.panel_port, cfg.panel_port,
    )

    # Start panel in daemon thread so it doesn't block
    panel_thread = threading.Thread(target=_start_panel, args=(cfg, state), daemon=True)
    panel_thread.start()
    log.info("Panel started | url=http://%s:%d", cfg.panel_host, cfg.panel_port)

    try:
        asyncio.run(_run_loop(cfg, state, total_days))
    except KeyboardInterrupt:
        log.info("Interrupted by user. Shutting down.")
        sys.exit(0)


if __name__ == "__main__":
    main()
