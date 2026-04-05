"""
    main.py – NetPulse entry point.
    Starts the web panel on 127.0.0.1 and runs the coordinator loop.
    Access the panel via SSH tunnel: ssh -L 7070:127.0.0.1:7070 user@your-server Then open http://127.0.0.1:7070 in your browser.
"""

from __future__ import annotations

import os
import sys
import uvicorn
import asyncio
import threading
from src import storage
from src.state import State
from src.panel import create_app
from src.coordinator import run_cycle
from src.logger import setup_logger, get_logger
from src.config import load_system_config, seed_db_from_toml



def _start_panel(sys_cfg, state) -> None:
    app = create_app(sys_cfg, state)
    uvicorn.run(
        app,
        host=sys_cfg.panel_host,
        port=sys_cfg.panel_port,
        log_level="warning",
        access_log=False,
    )


async def _run_loop(sys_cfg, state, total_days: int) -> None:
    log = get_logger("main")
    day = 1
    while True:
        log.info("Starting day %d | total_days=%s", day, total_days if total_days > 0 else "∞")
        await run_cycle(sys_cfg, state)
        log.info("Day %d complete", day)
        if total_days > 0 and day >= total_days:
            log.info("All %d day(s) complete. Exiting.", total_days)
            break
        day += 1
        log.info("Waiting 60s before next cycle...")
        await asyncio.sleep(60)


def main() -> None:
    sys_cfg = load_system_config()
    setup_logger(sys_cfg.log_file, sys_cfg.log_level)
    log = get_logger("main")

    # Init DB and seed from config.toml on first run
    storage.init_db()
    storage.seed_default_settings()
    seed_db_from_toml()

    # Determine how many days to run
    env_days = 0
    if env_days == 0:
        total_days = int(env_days)
    else:
        raw = input("How many days to run? (0 = run forever): ").strip()
        total_days = int(raw) if raw.isdigit() else 0

    state = State()

    log.info(
        "NetPulse starting | panel=http://%s:%d",
        sys_cfg.panel_host, sys_cfg.panel_port,
    )
    log.info(
        "SSH tunnel command: ssh -L %d:127.0.0.1:%d user@<server>",
        sys_cfg.panel_port, sys_cfg.panel_port,
    )

    panel_thread = threading.Thread(target=_start_panel, args=(sys_cfg, state), daemon=True)
    panel_thread.start()
    log.info("Panel started | url=http://%s:%d", sys_cfg.panel_host, sys_cfg.panel_port)

    try:
        asyncio.run(_run_loop(sys_cfg, state, total_days))
    except KeyboardInterrupt:
        log.info("Interrupted by user. Shutting down.")
        sys.exit(0)


if __name__ == "__main__":
    main()
