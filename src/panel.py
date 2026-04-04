"""
src/panel.py – FastAPI web panel (127.0.0.1 only).
"""

from __future__ import annotations

import asyncio
from src import storage
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

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        return templates.TemplateResponse(request=request, name="index.html")

    @app.get("/api/state")
    async def api_state():
        return JSONResponse(state.to_dict())

    @app.get("/api/metrics")
    async def api_metrics():
        # Fetch metrics for download sources + monitors separately
        src_metrics = await fetch_all_metrics(cfg.download_sources, cfg.verify_ssl)
        mon_metrics = await fetch_all_metrics(cfg.monitors, cfg.verify_ssl)

        # Load monthly usage for quota calculation
        usage_rows = storage.get_monthly_usage()
        monthly_usage = {r["source_label"]: r["downloaded_bytes"] for r in usage_rows}

        def source_entry(m, src=None):
            entry = {
                "label": m.label,
                "rx_gb": m.rx_gb,
                "tx_gb": m.tx_gb,
                "reachable": m.reachable,
                "error": m.error,
                "is_monitor": src is None,
            }
            if src is not None:
                used_bytes = monthly_usage.get(src.label, 0)
                allowed_bytes = src.monthly_allowed_gb * 1024 ** 3
                entry["monthly_limit_gb"]   = src.monthly_limit_gb
                entry["monthly_allowed_gb"] = src.monthly_allowed_gb
                entry["monthly_used_gb"]    = round(used_bytes / 1024 ** 3, 3)
                entry["monthly_remaining_gb"] = round(max(0, (allowed_bytes - used_bytes) / 1024 ** 3), 3)
                entry["usage_quota_pct"]    = src.usage_quota_pct
            return entry

        src_map = {s.label: s for s in cfg.download_sources}
        result = [source_entry(m, src_map.get(m.label)) for m in src_metrics]
        result += [source_entry(m) for m in mon_metrics]
        return JSONResponse(result)

    @app.get("/api/ping-agents")
    async def api_ping_agents():
        tasks = [test_agent_connection(a) for a in cfg.agents]
        results_raw = await asyncio.gather(*tasks)
        return JSONResponse([
            {"label": a.label, "host": a.host, "ok": ok, "message": msg}
            for a, (ok, msg) in zip(cfg.agents, results_raw)
        ])

    @app.get("/api/logs")
    async def api_logs():
        return JSONResponse({"lines": get_log_buffer()[-200:]})

    @app.get("/api/config")
    async def api_config():
        return JSONResponse({
            "agents": [
                {"label": a.label, "host": a.host, "daily_limit_gb": a.daily_limit_gb}
                for a in cfg.agents
            ],
            "sources": [
                {
                    "label": s.label,
                    "download_url": s.download_url,
                    "monthly_limit_gb": s.monthly_limit_gb,
                    "usage_quota_pct": s.usage_quota_pct,
                    "monthly_allowed_gb": s.monthly_allowed_gb,
                }
                for s in cfg.download_sources
            ],
            "monitors": [
                {"label": m.label} for m in cfg.monitors
            ],
        })

    @app.get("/api/plan")
    async def api_plan():
        rows = storage.get_today_events()
        return JSONResponse([dict(r) for r in rows])

    @app.get("/api/monthly-history")
    async def api_monthly_history():
        rows = storage.get_all_monthly_usage()
        return JSONResponse([dict(r) for r in rows])

    return app
