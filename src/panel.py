"""
    src/panel.py – FastAPI web panel (binds to 127.0.0.1 only). Access via SSH tunnel: ssh -L 7070:127.0.0.1:7070 user@server
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from src.state import State
from src.config import Config
from fastapi import FastAPI, Request
from src.logger import get_log_buffer
from src.metrics import fetch_all_metrics
from src.agent import test_agent_connection
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse


_HERE = Path(__file__).parent.parent


def create_app(cfg: Config, state: State) -> FastAPI:
    app = FastAPI(title="NetPulse", docs_url=None, redoc_url=None)

    templates = Jinja2Templates(directory=str(_HERE / "templates"))
    static_path = _HERE / "static"
    if static_path.exists():
        app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

    # Dashboard
    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        return templates.TemplateResponse("index.html", {"request": request})

    # API: state
    @app.get("/api/state")
    async def api_state():
        return JSONResponse(state.to_dict())

    # API: metrics
    @app.get("/api/metrics")
    async def api_metrics():
        results = await fetch_all_metrics(cfg.download_sources, cfg.verify_ssl)
        return JSONResponse([
            {
                "label": m.label,
                "rx_gb": m.rx_gb,
                "tx_gb": m.tx_gb,
                "reachable": m.reachable,
                "error": m.error,
            }
            for m in results
        ])

    # API: ping agents
    @app.get("/api/ping-agents")
    async def api_ping_agents():
        tasks = [test_agent_connection(a) for a in cfg.agents]
        results_raw = await asyncio.gather(*tasks)
        return JSONResponse([
            {"label": a.label, "host": a.host, "ok": ok, "message": msg}
            for a, (ok, msg) in zip(cfg.agents, results_raw)
        ])

    # API: logs
    @app.get("/api/logs")
    async def api_logs():
        return JSONResponse({"lines": get_log_buffer()[-200:]})

    # API: config summary
    @app.get("/api/config")
    async def api_config():
        return JSONResponse({
            "agents": [
                {"label": a.label, "host": a.host, "daily_limit_gb": a.daily_limit_gb}
                for a in cfg.agents
            ],
            "sources": [
                {"label": s.label, "download_url": s.download_url}
                for s in cfg.download_sources
            ],
        })

    return app
