"""
src/panel.py – FastAPI web panel with session auth.
Security: HMAC-signed sessions, server-side input validation,
          parameterised queries, 127.0.0.1 binding only.
"""

from __future__ import annotations


import re
import time
import hmac
import asyncio
import hashlib
from src import storage
from pathlib import Path
from src.state import State
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from src.logger import get_log_buffer, get_logger
from fastapi import FastAPI, Form, Request, Response
from src.metrics import fetch_all_metrics, metric_to_dict
from src.agent import run_manual_job, test_agent_connection
from src.config import RuntimeConfig, SystemConfig, load_runtime_config
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse


_HERE           = Path(__file__).parent.parent
_SESSION_COOKIE = "np_session"
_SESSION_TTL    = 12 * 3600

log = get_logger("panel")


# Auth

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


# Validation

_LABEL_RE = re.compile(r'^[\w\-. ]{1,64}$')
_URL_RE   = re.compile(r'^https?://.{3,}')

def _validate_label(v: str) -> str:
    v = str(v).strip()
    if not _LABEL_RE.match(v):
        raise ValueError(f"Invalid label: use letters, numbers, hyphens, dots (max 64)")
    return v

def _validate_url(v: str, field: str = "URL") -> str:
    v = str(v).strip()
    if not _URL_RE.match(v):
        raise ValueError(f"{field} must start with http:// or https://")
    return v

def _validate_int(v, field: str, min_val: int = 0) -> int:
    try:
        i = int(v)
        if i < min_val:
            raise ValueError()
        return i
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be integer >= {min_val}")

def _validate_float(v, field: str, min_val: float = 0.0) -> float:
    try:
        f = float(v)
        if f < min_val:
            raise ValueError()
        return f
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be number >= {min_val}")

def _validate_pct(v, field: str) -> float:
    f = _validate_float(v, field)
    if f > 1.0:
        raise ValueError(f"{field} must be between 0.0 and 1.0")
    return f

def _err(msg: str, status: int = 422) -> JSONResponse:
    log.warning("API validation error | %s", msg)
    return JSONResponse({"error": msg}, status_code=status)


# App factory

def create_app(sys_cfg: SystemConfig, state: State) -> FastAPI:
    app = FastAPI(title="NetPulse", docs_url=None, redoc_url=None)

    templates   = Jinja2Templates(directory=str(_HERE / "templates"))
    static_path = _HERE / "static"
    if static_path.exists():
        app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

    def _guard(request: Request):
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
            log.info("Login OK | user=%s", username)
            return resp
        log.warning("Login failed | user=%s", username)
        return RedirectResponse("/login?error=1", status_code=303)

    @app.get("/logout")
    async def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(_SESSION_COOKIE)
        return resp

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        if redir := _guard(request): return redir
        return templates.TemplateResponse(request=request, name="index.html")

    # State

    @app.get("/api/state")
    async def api_state(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(state.to_dict())

    @app.post("/api/state/sync")
    async def api_state_sync(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        state.sync_agents_from_db()
        state.load_plan_from_db()
        return JSONResponse({"ok": True})

    # Settings

    @app.get("/api/settings")
    async def api_get_settings(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(storage.get_all_settings())

    @app.put("/api/settings")
    async def api_put_settings(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        validators = {
            "scheduler.days":             lambda v: _validate_int(v, "days"),
            "scheduler.daily_variance":   lambda v: _validate_pct(v, "daily_variance"),
            "download.speed_cap":         lambda v: _validate_int(v, "speed_cap"),
            "download.pause_probability": lambda v: _validate_pct(v, "pause_probability"),
            "download.max_concurrent":    lambda v: _validate_int(v, "max_concurrent", 1),
            "download.max_retries":       lambda v: _validate_int(v, "max_retries", 1),
            "ui.auto_refresh_seconds":    lambda v: _validate_int(v, "auto_refresh_seconds"),
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

    # SSH Keys

    @app.get("/api/ssh-keys")
    async def api_get_ssh_keys(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse([dict(r) for r in storage.get_ssh_keys()])

    @app.post("/api/ssh-keys")
    async def api_create_ssh_key(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        try:
            name        = _validate_label(body.get("name", ""))
            private_key = str(body.get("private_key", "")).strip()
            if not private_key:
                raise ValueError("Private key content is required")
            if "PRIVATE KEY" not in private_key:
                raise ValueError("Does not look like a valid PEM private key")
            comment = str(body.get("comment", "")).strip()[:200]
        except ValueError as e:
            return _err(str(e))
        storage.upsert_ssh_key(name, private_key, comment)
        log.info("SSH key saved | name=%s", name)
        return JSONResponse({"ok": True})

    @app.delete("/api/ssh-keys/{name}")
    async def api_delete_ssh_key(request: Request, name: str):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        storage.delete_ssh_key(name)
        log.info("SSH key deleted | name=%s", name)
        return JSONResponse({"ok": True})

    # Agents

    @app.get("/api/agents")
    async def api_get_agents(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        rows = storage.get_agents(enabled_only=False)
        # Never return private key content in list
        return JSONResponse([
            {k: v for k, v in dict(r).items() if k not in ("password", "ssh_key")}
            for r in rows
        ])

    def _parse_agent_body(body: dict, is_new: bool, label: str) -> dict:
        host  = str(body.get("host", "")).strip()
        if not host: raise ValueError("Host is required")
        port  = _validate_int(body.get("port", 22), "port", 1)
        user  = str(body.get("user", "")).strip()
        if not user: raise ValueError("User is required")

        auth_type = body.get("auth_type", "password")
        if auth_type not in ("password", "key"):
            raise ValueError("auth_type must be 'password' or 'key'")

        pw          = str(body.get("password", "")).strip()
        ssh_key     = str(body.get("ssh_key", "")).strip()
        ssh_key_name = str(body.get("ssh_key_name", "")).strip()

        if auth_type == "password" and (is_new or pw):
            if not pw: raise ValueError("Password is required for password auth")
        if auth_type == "key":
            if not ssh_key and not ssh_key_name:
                raise ValueError("Provide an SSH key (inline PEM or select a named key)")

        daily   = _validate_float(body.get("daily_limit_gb", 1), "daily_limit_gb")
        monthly = _validate_float(body.get("monthly_limit_gb", 0), "monthly_limit_gb")
        pct     = _validate_pct(body.get("usage_quota_pct", 1.0), "usage_quota_pct")
        enabled = int(bool(body.get("enabled", True)))

        # ProxyJump
        jump_host     = str(body.get("jump_host", "")).strip()
        jump_port     = _validate_int(body.get("jump_port", 22), "jump_port", 1)
        jump_user     = str(body.get("jump_user", "")).strip()
        jump_key_name = str(body.get("jump_key_name", "")).strip()

        return {
            "label": label, "host": host, "port": port, "user": user,
            "password": pw, "ssh_key": ssh_key, "ssh_key_name": ssh_key_name,
            "auth_type": auth_type,
            "jump_host": jump_host, "jump_port": jump_port,
            "jump_user": jump_user, "jump_key_name": jump_key_name,
            "daily_limit_gb": daily, "monthly_limit_gb": monthly,
            "usage_quota_pct": pct, "enabled": enabled,
        }

    @app.post("/api/agents")
    async def api_create_agent(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        try:
            label = _validate_label(body.get("label", ""))
            data  = _parse_agent_body(body, is_new=True, label=label)
        except ValueError as e:
            return _err(str(e))
        storage.upsert_agent(data)
        state.sync_agents_from_db()
        log.info("Agent created | label=%s | auth=%s", label, data["auth_type"])
        return JSONResponse({"ok": True, "start_now": bool(body.get("start_now"))})

    @app.put("/api/agents/{label}")
    async def api_update_agent(request: Request, label: str):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        try:
            _validate_label(label)
            data = _parse_agent_body(body, is_new=False, label=label)
        except ValueError as e:
            return _err(str(e))
        storage.upsert_agent(data)
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
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        cfg   = _cfg()
        agent = next((a for a in cfg.agents if a.label == label), None)
        if not agent:
            return _err(f"Agent '{label}' not found", 404)
        ok, msg = await test_agent_connection(agent, cfg)
        return JSONResponse({"label": label, "ok": ok, "message": msg})

    # Sources

    @app.get("/api/sources")
    async def api_get_sources(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse([dict(r) for r in storage.get_sources(enabled_only=False)])

    @app.post("/api/sources")
    async def api_create_source(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        try:
            label        = _validate_label(body.get("label", ""))
            download_url = _validate_url(body.get("download_url", ""), "download_url")
            metric_url   = body.get("metric_url", "").strip()
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

    # Monitors

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

    # Metrics

    @app.get("/api/metrics")
    async def api_metrics(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        cfg         = _cfg()
        src_metrics = await fetch_all_metrics(cfg.download_sources, cfg.verify_ssl)
        mon_metrics = await fetch_all_metrics(cfg.monitors, cfg.verify_ssl)
        return JSONResponse(
            [metric_to_dict(m, False) for m in src_metrics] +
            [metric_to_dict(m, True)  for m in mon_metrics]
        )

    # Agent Quota

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
                "monthly_remaining_gb": round(max(0,
                    (a.monthly_allowed_gb * 1024 ** 3 - monthly_usage.get(a.label, 0)) / 1024 ** 3), 3),
                "usage_quota_pct":      a.usage_quota_pct,
                "has_quota":            a.monthly_limit_gb > 0,
            }
            for a in cfg.agents
        ])

    # Ping

    @app.get("/api/ping-agents")
    async def api_ping_agents(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        cfg         = _cfg()
        results_raw = await asyncio.gather(*[test_agent_connection(a, cfg) for a in cfg.agents])
        return JSONResponse([
            {"label": a.label, "host": a.host, "ok": ok, "message": msg}
            for a, (ok, msg) in zip(cfg.agents, results_raw)
        ])

    # Plan

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

    # Manual Jobs

    @app.get("/api/manual-jobs")
    async def api_get_manual_jobs(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse([dict(r) for r in storage.get_manual_jobs()])

    @app.post("/api/manual-jobs")
    async def api_create_manual_job(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        try:
            agent_label      = _validate_label(body.get("agent_label", ""))
            source_label     = _validate_label(body.get("source_label", ""))
            download_count   = _validate_int(body.get("download_count", 1), "download_count", 1)
            mode             = body.get("mode", "immediate")
            if mode not in ("immediate", "scheduled"):
                raise ValueError("mode must be 'immediate' or 'scheduled'")
            interval_type    = body.get("interval_type", "fixed")
            if interval_type not in ("fixed", "random"):
                raise ValueError("interval_type must be 'fixed' or 'random'")
            interval_seconds = _validate_int(body.get("interval_seconds", 60), "interval_seconds", 1)
            speed_cap        = _validate_int(body.get("speed_cap", 0), "speed_cap")
            delete_after     = bool(body.get("delete_after", False))
            start_at         = body.get("start_at") or None
        except ValueError as e:
            return _err(str(e))

        job_id = storage.create_manual_job(
            agent_label, source_label, download_count, mode,
            interval_type, interval_seconds, speed_cap, delete_after, start_at,
        )
        log.info("Manual job created | id=%d | agent=%s | count=%d | delete=%s",
                 job_id, agent_label, download_count, delete_after)

        if mode == "immediate":
            cfg    = _cfg()
            agent  = next((a for a in cfg.agents if a.label == agent_label), None)
            source = next((s for s in cfg.download_sources if s.label == source_label), None)
            if agent and source:
                job_row = storage.get_manual_job(job_id)
                if job_row:
                    asyncio.create_task(run_manual_job(job_id, agent, source, cfg, job_row))
            else:
                log.warning("Manual job dispatch failed | id=%d | agent/source not found", job_id)

        return JSONResponse({"ok": True, "job_id": job_id})

    @app.delete("/api/manual-jobs/{job_id}")
    async def api_cancel_manual_job(request: Request, job_id: int):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        ok = storage.cancel_manual_job(job_id)
        if ok:
            log.info("Manual job cancelled | id=%d", job_id)
            return JSONResponse({"ok": True})
        return _err("Cannot cancel — job not found or already finished", 400)

    # Logs

    @app.get("/api/logs")
    async def api_logs(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"lines": get_log_buffer()[-500:]})

    # Monthly history

    @app.get("/api/monthly-history")
    async def api_monthly_history(request: Request):
        if _guard(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse([dict(r) for r in storage.get_all_monthly_usage()])

    return app
