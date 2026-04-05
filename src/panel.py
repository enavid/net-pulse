"""
src/panel.py – FastAPI web panel with session-based authentication.
               Binds to 127.0.0.1 only. Access via SSH tunnel.
"""

from __future__ import annotations

import hmac
import time
import asyncio
import hashlib
from src import storage
from pathlib import Path
from src.state import State
from src.config import Config
from src.logger import get_log_buffer
from src.metrics import fetch_all_metrics
from fastapi.staticfiles import StaticFiles
from src.agent import test_agent_connection
from fastapi.templating import Jinja2Templates
from fastapi import FastAPI, Request, Response, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse


_HERE = Path(__file__).parent.parent
_SESSION_COOKIE = "np_session"
_SESSION_TTL = 12 * 3600


def _sign(value: str, secret: str) -> str:
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def _make_token(secret: str) -> str:
    ts = str(int(time.time()))
    return f"{ts}.{_sign(ts, secret)}"


def _verify_token(token: str, secret: str) -> bool:
    try:
        ts_str, sig = token.split(".", 1)
        if not hmac.compare_digest(_sign(ts_str, secret), sig):
            return False
        return (time.time() - int(ts_str)) < _SESSION_TTL
    except Exception:
        return False


def _is_authenticated(request: Request, secret: str) -> bool:
    return _verify_token(request.cookies.get(_SESSION_COOKIE, ""), secret)


def create_app(cfg: Config, state: State) -> FastAPI:
    app = FastAPI(title="NetPulse", docs_url=None, redoc_url=None)

    templates = Jinja2Templates(directory=str(_HERE / "templates"))
    static_path = _HERE / "static"
    if static_path.exists():
        app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

    def _guard(request: Request):
        if not _is_authenticated(request, cfg.secret_key):
            return RedirectResponse("/login", status_code=303)
        return None

    # Auth
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, error: str = ""):
        return templates.TemplateResponse(request=request, name="login.html", context={"error": error})

    @app.post("/login")
    async def login_submit(request: Request, response: Response, username: str = Form(...), password: str = Form(...)):
        if username == cfg.panel_username and password == cfg.panel_password:
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(_SESSION_COOKIE, _make_token(cfg.secret_key),
                            httponly=True, samesite="strict", max_age=_SESSION_TTL)
            return resp
        return RedirectResponse("/login?error=1", status_code=303)

    @app.get("/logout")
    async def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(_SESSION_COOKIE)
        return resp

    # Dashboard
    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        if redir := _guard(request):
            return redir
        return templates.TemplateResponse(request=request, name="index.html")

    # API: state
    @app.get("/api/state")
    async def api_state(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(state.to_dict())

    # API: VPN server metrics (no quota — upload is free)
    @app.get("/api/metrics")
    async def api_metrics(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        src_metrics = await fetch_all_metrics(cfg.download_sources, cfg.verify_ssl)
        mon_metrics = await fetch_all_metrics(cfg.monitors, cfg.verify_ssl)

        def entry(m, is_monitor=False):
            return {
                "label": m.label,
                "rx_gb": m.rx_gb,
                "tx_gb": m.tx_gb,
                "reachable": m.reachable,
                "error": m.error,
                "is_monitor": is_monitor,
            }

        result  = [entry(m, False) for m in src_metrics]
        result += [entry(m, True)  for m in mon_metrics]
        return JSONResponse(result)

    # API: agent quota (agents have download limits)
    @app.get("/api/agent-quota")
    async def api_agent_quota(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        usage_rows    = storage.get_monthly_usage()
        monthly_usage = {r["agent_label"]: r["downloaded_bytes"] for r in usage_rows}
        return JSONResponse([
            {
                "label":                a.label,
                "host":                 a.host,
                "daily_limit_gb":       a.daily_limit_gb,
                "monthly_limit_gb":     a.monthly_limit_gb,
                "monthly_allowed_gb":   round(a.monthly_allowed_gb, 2),
                "monthly_used_gb":      round(monthly_usage.get(a.label, 0) / 1024 ** 3, 3),
                "monthly_remaining_gb": round(
                    max(0, (a.monthly_allowed_gb * 1024 ** 3 - monthly_usage.get(a.label, 0)) / 1024 ** 3), 3
                ),
                "usage_quota_pct":      a.usage_quota_pct,
                "has_quota":            a.monthly_limit_gb > 0,
            }
            for a in cfg.agents
        ])

    # API: ping agents
    @app.get("/api/ping-agents")
    async def api_ping_agents(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        tasks = [test_agent_connection(a, cfg) for a in cfg.agents]
        results_raw = await asyncio.gather(*tasks)
        return JSONResponse([
            {"label": a.label, "host": a.host, "ok": ok, "message": msg}
            for a, (ok, msg) in zip(cfg.agents, results_raw)
        ])

    # API: logs
    @app.get("/api/logs")
    async def api_logs(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"lines": get_log_buffer()[-200:]})

    # API: config summary
    @app.get("/api/config")
    async def api_config(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({
            "agents":   [{"label": a.label, "host": a.host, "daily_limit_gb": a.daily_limit_gb,
                          "monthly_limit_gb": a.monthly_limit_gb} for a in cfg.agents],
            "sources":  [{"label": s.label, "download_url": s.download_url} for s in cfg.download_sources],
            "monitors": [{"label": m.label} for m in cfg.monitors],
        })

    # API: plan
    @app.get("/api/plan")
    async def api_plan(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse([dict(r) for r in storage.get_today_events()])

    # API: monthly history
    @app.get("/api/monthly-history")
    async def api_monthly_history(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse([dict(r) for r in storage.get_all_monthly_usage()])

    return app