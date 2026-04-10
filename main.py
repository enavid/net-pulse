"""
main.py – NetPulse entry point.

Access panel via SSH tunnel:
        ssh -L 10909:127.0.0.1:10909 user@your-server Then open http://127.0.0.1:10909
"""

from __future__ import annotations

import sys
import asyncio
import uvicorn
import threading
from src import storage
from src.state import State
from src.panel import create_app
from src.coordinator import run_cycle
from src.logger import get_logger, setup_logger
from src.config import load_system_config, load_total_days_from_toml, seed_db_from_toml


def _start_panel(sys_cfg, state) -> None:
    app = create_app(sys_cfg, state)
    uvicorn.run(app, host=sys_cfg.panel_host, port=sys_cfg.panel_port,
                log_level="warning", access_log=False)


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
        log.info("Waiting 60s before next cycle…")
        await asyncio.sleep(60)


def main() -> None:
    sys_cfg    = load_system_config()
    setup_logger(sys_cfg.log_file, sys_cfg.log_level)
    log        = get_logger("main")

    storage.init_db()
    storage.seed_default_settings()
    seed_db_from_toml()

    # Days: env var > config.toml [scheduler] days > 0 (forever)
    total_days = load_total_days_from_toml()

    state = State()

    log.info("NetPulse starting | panel=http://%s:%d | days=%s",
             sys_cfg.panel_host, sys_cfg.panel_port,
             total_days if total_days > 0 else "∞")
    log.info("SSH tunnel: ssh -L %d:127.0.0.1:%d user@<server>",
             sys_cfg.panel_port, sys_cfg.panel_port)

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
