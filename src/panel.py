"""
src/panel.py – FastAPI web panel with session auth.
               Binds to 127.0.0.1. Access via SSH tunnel.
               Full CRUD for agents/sources/monitors/settings + manual jobs.

Security notes:
  - Session tokens are HMAC-SHA256 signed with secret_key, TTL 12h
  - All inputs validated server-side before DB write
  - Parameterised queries only — no string formatting into SQL
  - No sensitive data (passwords) returned in GET responses
  - Panel bound to 127.0.0.1 — never exposed to public internet
"""

from __future__ import annotations

import re
import hmac
import time
import asyncio
import hashlib
from src import storage
from pathlib import Path
from src.state import State
from typing import Any, Optional
from src.metrics import fetch_all_metrics
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from src.logger import get_log_buffer, get_logger
from fastapi import FastAPI, Request, Response, Form
from src.agent import test_agent_connection, run_manual_job
from src.config import RuntimeConfig, SystemConfig, load_runtime_config
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse


_HERE           = Path(__file__).parent.parent
_SESSION_COOKIE = "np_session"
_SESSION_TTL    = 12 * 3600

log = get_logger("panel")


# Auth helpers

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


# Input validation helpers

_LABEL_RE = re.compile(r'^[\w\-. ]{1,64}$')
_URL_RE   = re.compile(r'^https?://.{3,}')


def _validate_label(v: str) -> str:
    v = str(v).strip()
    if not _LABEL_RE.match(v):
        raise ValueError(f"Invalid label '{v}': use letters, numbers, hyphens, dots, spaces (max 64)")
    return v


def _validate_url(v: str, field: str = "URL") -> str:
    v = str(v).strip()
    if not _URL_RE.match(v):
        raise ValueError(f"Invalid {field}: must start with http:// or https://")
    return v


def _validate_positive_float(v: Any, field: str) -> float:
    try:
        f = float(v)
        if f < 0:
            raise ValueError()
        return f
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a non-negative number")


def _validate_positive_int(v: Any, field: str, min_val: int = 0) -> int:
    try:
        i = int(v)
        if i < min_val:
            raise ValueError()
        return i
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be an integer >= {min_val}")


def _validate_pct(v: Any, field: str = "quota_pct") -> float:
    f = _validate_positive_float(v, field)
    if f > 1.0:
        raise ValueError(f"{field} must be between 0.0 and 1.0")
    return f


def _err(msg: str, status: int = 422) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


# App factory

def create_app(sys_cfg: SystemConfig, state: State) -> FastAPI:
    app = FastAPI(title="NetPulse", docs_url=None, redoc_url=None)

    templates   = Jinja2Templates(directory=str(_HERE / "templates"))
    static_path = _HERE / "static"
    if static_path.exists():
        app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

    def _guard(request: Request) -> Optional[JSONResponse]:
        if not _is_authenticated(request, sys_cfg.secret_key):
            return RedirectResponse("/login", status_code=303)
        return None

    def _cfg() -> RuntimeConfig:
        return load_runtime_config(sys_cfg)

    # Auth

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, error: str = ""):
        return templates.TemplateResponse(request=request, name="login.html", context={"error": error})

    @app.post("/login")
    async def login_submit(request: Request, response: Response,
                           username: str = Form(...), password: str = Form(...)):
        cfg = _cfg()
        if username == cfg.panel_username and password == cfg.panel_password:
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(_SESSION_COOKIE, _make_token(sys_cfg.secret_key),
                            httponly=True, samesite="strict", max_age=_SESSION_TTL)
            log.info("Login successful | user=%s", username)
            return resp
        log.warning("Login failed | user=%s", username)
        return RedirectResponse("/login?error=1", status_code=303)

    @app.get("/logout")
    async def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(_SESSION_COOKIE)
        return resp

    # Dashboard

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        if redir := _guard(request): return redir
        return templates.TemplateResponse(request=request, name="index.html")

    # API: state + sync

    @app.get("/api/state")
    async def api_state(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(state.to_dict())

    @app.post("/api/state/sync")
    async def api_state_sync(request: Request):
        """Force sync state.agents from DB — call after adding/removing agents."""
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        state.sync_agents_from_db()
        state.load_plan_from_db()
        log.info("State synced from DB via API")
        return JSONResponse({"ok": True})

    # API: settings

    @app.get("/api/settings")
    async def api_get_settings(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(storage.get_all_settings())

    @app.put("/api/settings")
    async def api_put_settings(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()

        # Validate known numeric/bool settings
        validators = {
            "scheduler.days":             lambda v: _validate_positive_int(v, "days"),
            "scheduler.daily_variance":   lambda v: _validate_pct(v, "daily_variance"),
            "download.speed_cap":         lambda v: _validate_positive_int(v, "speed_cap"),
            "download.pause_probability": lambda v: _validate_pct(v, "pause_probability"),
            "download.max_concurrent":    lambda v: _validate_positive_int(v, "max_concurrent", 1),
            "download.max_retries":       lambda v: _validate_positive_int(v, "max_retries", 1),
            "ui.auto_refresh_seconds":    lambda v: _validate_positive_int(v, "auto_refresh_seconds"),
            "network.connection_test_url":lambda v: _validate_url(v, "connection_test_url"),
        }

        for key, value in body.items():
            if key in validators:
                try:
                    validators[key](value)
                except ValueError as e:
                    return _err(str(e))
            storage.set_setting(str(key), str(value))

        log.info("Settings updated | keys=%s", list(body.keys()))
        return JSONResponse({"ok": True})

    # API: agents CRUD

    @app.get("/api/agents")
    async def api_get_agents(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        rows = storage.get_agents(enabled_only=False)
        # Never return password in list view
        return JSONResponse([
            {k: v for k, v in dict(r).items() if k != "password"}
            for r in rows
        ])

    @app.post("/api/agents")
    async def api_create_agent(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        try:
            label = _validate_label(body.get("label", ""))
            host  = str(body.get("host", "")).strip()
            if not host: raise ValueError("Host is required")
            port  = _validate_positive_int(body.get("port", 22), "port", 1)
            user  = str(body.get("user", "")).strip()
            if not user: raise ValueError("User is required")
            pw    = str(body.get("password", "")).strip()
            if not pw: raise ValueError("Password is required")
            daily = _validate_positive_float(body.get("daily_limit_gb", 1), "daily_limit_gb")
            monthly = _validate_positive_float(body.get("monthly_limit_gb", 0), "monthly_limit_gb")
            pct   = _validate_pct(body.get("usage_quota_pct", 1.0))
            enabled = int(bool(body.get("enabled", True)))
        except (ValueError, KeyError) as e:
            return _err(str(e))

        storage.upsert_agent({
            "label": label, "host": host, "port": port, "user": user,
            "password": pw, "daily_limit_gb": daily, "monthly_limit_gb": monthly,
            "usage_quota_pct": pct, "enabled": enabled,
        })
        state.sync_agents_from_db()
        log.info("Agent created | label=%s | host=%s", label, host)

        start_now = bool(body.get("start_now", False))
        return JSONResponse({"ok": True, "start_now": start_now})

    @app.put("/api/agents/{label}")
    async def api_update_agent(request: Request, label: str):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        try:
            _validate_label(label)
            host  = str(body.get("host", "")).strip()
            if not host: raise ValueError("Host is required")
            port  = _validate_positive_int(body.get("port", 22), "port", 1)
            user  = str(body.get("user", "")).strip()
            if not user: raise ValueError("User is required")
            pw    = str(body.get("password", "")).strip()
            if not pw: raise ValueError("Password is required")
            daily = _validate_positive_float(body.get("daily_limit_gb", 1), "daily_limit_gb")
            monthly = _validate_positive_float(body.get("monthly_limit_gb", 0), "monthly_limit_gb")
            pct   = _validate_pct(body.get("usage_quota_pct", 1.0))
            enabled = int(bool(body.get("enabled", True)))
        except (ValueError, KeyError) as e:
            return _err(str(e))

        storage.upsert_agent({
            "label": label, "host": host, "port": port, "user": user,
            "password": pw, "daily_limit_gb": daily, "monthly_limit_gb": monthly,
            "usage_quota_pct": pct, "enabled": enabled,
        })
        state.sync_agents_from_db()
        log.info("Agent updated | label=%s", label)
        return JSONResponse({"ok": True})

    @app.delete("/api/agents/{label}")
    async def api_delete_agent(request: Request, label: str):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        storage.delete_agent(label)
        state.sync_agents_from_db()
        log.info("Agent deleted | label=%s", label)
        return JSONResponse({"ok": True})

    @app.post("/api/agents/{label}/test")
    async def api_test_agent(request: Request, label: str):
        """Test SSH connection + download for a single agent."""
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        cfg   = _cfg()
        agent = next((a for a in cfg.agents if a.label == label), None)
        if not agent:
            return _err(f"Agent '{label}' not found", 404)
        ok, msg = await test_agent_connection(agent, cfg)
        return JSONResponse({"label": label, "ok": ok, "message": msg})

    # API: sources CRUD

    @app.get("/api/sources")
    async def api_get_sources(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse([dict(r) for r in storage.get_sources(enabled_only=False)])

    @app.post("/api/sources")
    async def api_create_source(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        try:
            label       = _validate_label(body.get("label", ""))
            download_url = _validate_url(body.get("download_url", ""), "download_url")
            metric_url  = body.get("metric_url", "").strip()
            if metric_url: _validate_url(metric_url, "metric_url")
            enabled = int(bool(body.get("enabled", True)))
        except ValueError as e:
            return _err(str(e))

        storage.upsert_source({"label": label, "download_url": download_url, "metric_url": metric_url, "enabled": enabled})
        log.info("Source created | label=%s", label)
        return JSONResponse({"ok": True})

    @app.put("/api/sources/{label}")
    async def api_update_source(request: Request, label: str):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        try:
            _validate_label(label)
            download_url = _validate_url(body.get("download_url", ""), "download_url")
            metric_url   = body.get("metric_url", "").strip()
            if metric_url: _validate_url(metric_url, "metric_url")
            enabled = int(bool(body.get("enabled", True)))
        except ValueError as e:
            return _err(str(e))

        storage.upsert_source({"label": label, "download_url": download_url, "metric_url": metric_url, "enabled": enabled})
        log.info("Source updated | label=%s", label)
        return JSONResponse({"ok": True})

    @app.delete("/api/sources/{label}")
    async def api_delete_source(request: Request, label: str):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        storage.delete_source(label)
        log.info("Source deleted | label=%s", label)
        return JSONResponse({"ok": True})

    # API: monitors CRUD

    @app.get("/api/monitors")
    async def api_get_monitors(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse([dict(r) for r in storage.get_monitors(enabled_only=False)])

    @app.post("/api/monitors")
    async def api_create_monitor(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        try:
            label      = _validate_label(body.get("label", ""))
            metric_url = _validate_url(body.get("metric_url", ""), "metric_url")
            enabled    = int(bool(body.get("enabled", True)))
        except ValueError as e:
            return _err(str(e))

        storage.upsert_monitor({"label": label, "metric_url": metric_url, "enabled": enabled})
        log.info("Monitor created | label=%s", label)
        return JSONResponse({"ok": True})

    @app.put("/api/monitors/{label}")
    async def api_update_monitor(request: Request, label: str):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        try:
            _validate_label(label)
            metric_url = _validate_url(body.get("metric_url", ""), "metric_url")
            enabled    = int(bool(body.get("enabled", True)))
        except ValueError as e:
            return _err(str(e))

        storage.upsert_monitor({"label": label, "metric_url": metric_url, "enabled": enabled})
        log.info("Monitor updated | label=%s", label)
        return JSONResponse({"ok": True})

    @app.delete("/api/monitors/{label}")
    async def api_delete_monitor(request: Request, label: str):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        storage.delete_monitor(label)
        log.info("Monitor deleted | label=%s", label)
        return JSONResponse({"ok": True})

    # API: metrics

    @app.get("/api/metrics")
    async def api_metrics(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        cfg         = _cfg()
        src_metrics = await fetch_all_metrics(cfg.download_sources, cfg.verify_ssl)
        mon_metrics = await fetch_all_metrics(cfg.monitors, cfg.verify_ssl)

        def entry(m, is_monitor=False):
            return {"label": m.label, "rx_gb": m.rx_gb, "tx_gb": m.tx_gb,
                    "reachable": m.reachable, "error": m.error, "is_monitor": is_monitor}

        return JSONResponse([entry(m) for m in src_metrics] + [entry(m, True) for m in mon_metrics])

    # API: agent quota

    @app.get("/api/agent-quota")
    async def api_agent_quota(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        cfg           = _cfg()
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

    # API: ping all agents

    @app.get("/api/ping-agents")
    async def api_ping_agents(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        cfg         = _cfg()
        tasks       = [test_agent_connection(a, cfg) for a in cfg.agents]
        results_raw = await asyncio.gather(*tasks)
        return JSONResponse([
            {"label": a.label, "host": a.host, "ok": ok, "message": msg}
            for a, (ok, msg) in zip(cfg.agents, results_raw)
        ])

    # API: plan

    @app.get("/api/plan")
    async def api_plan(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse([dict(r) for r in storage.get_today_events()])

    @app.delete("/api/plan/reset")
    async def api_plan_reset(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        from datetime import date
        storage.delete_all_pending_today(date.today().isoformat())
        state.load_plan_from_db()
        log.info("Today's plan reset")
        return JSONResponse({"ok": True})

    @app.delete("/api/plan/event/{event_id}")
    async def api_delete_event(request: Request, event_id: int):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        storage.delete_event(event_id)
        state.load_plan_from_db()
        return JSONResponse({"ok": True})

    # API: manual jobs

    @app.get("/api/manual-jobs")
    async def api_get_manual_jobs(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse([dict(r) for r in storage.get_manual_jobs()])

    @app.post("/api/manual-jobs")
    async def api_create_manual_job(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        try:
            agent_label     = _validate_label(body.get("agent_label", ""))
            source_label    = _validate_label(body.get("source_label", ""))
            download_count  = _validate_positive_int(body.get("download_count", 1), "download_count", 1)
            mode            = body.get("mode", "immediate")
            if mode not in ("immediate", "scheduled"):
                raise ValueError("mode must be 'immediate' or 'scheduled'")
            interval_type   = body.get("interval_type", "fixed")
            if interval_type not in ("fixed", "random"):
                raise ValueError("interval_type must be 'fixed' or 'random'")
            interval_seconds = _validate_positive_int(body.get("interval_seconds", 60), "interval_seconds", 1)
            start_at        = body.get("start_at", None) or None
        except ValueError as e:
            return _err(str(e))

        job_id = storage.create_manual_job(
            agent_label, source_label, download_count, mode,
            interval_type, interval_seconds, start_at,
        )
        log.info("Manual job created | id=%d | agent=%s | count=%d | mode=%s", job_id, agent_label, download_count, mode)

        # Dispatch immediately if mode is immediate
        if mode == "immediate":
            cfg    = _cfg()
            agent  = next((a for a in cfg.agents  if a.label == agent_label),  None)
            source = next((s for s in cfg.download_sources if s.label == source_label), None)
            if agent and source:
                job_row = storage.get_manual_jobs(limit=1)[0]  # freshly created row
                asyncio.create_task(run_manual_job(job_id, agent, source, cfg, job_row))
            else:
                log.warning("Manual job immediate dispatch failed | id=%d | agent or source not found", job_id)

        return JSONResponse({"ok": True, "job_id": job_id})

    # API: logs

    @app.get("/api/logs")
    async def api_logs(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"lines": get_log_buffer()[-500:]})

    # API: monthly history

    @app.get("/api/monthly-history")
    async def api_monthly_history(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse([dict(r) for r in storage.get_all_monthly_usage()])

    return app
