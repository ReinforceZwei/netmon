"""FastAPI app: dashboard, settings editor, data management, JSON API.

Single process/worker (uvicorn --workers 1): the probe engine runs as threads
inside the app, and settings saved from the dashboard are applied live.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__
from .db import DB, day_str, iso, now_local
from .probe import ProbeEngine
from .settings import DEFAULTS, Settings

log = logging.getLogger("netmon.web")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("NETMON_DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "netmon.db"
REPORTS_DIR = DATA_DIR / "reports"

RANGES = {
    "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800,
    "30d": 2592000, "90d": 7776000, "1y": 31536000,
}
BUCKETS = [30, 60, 120, 300, 600, 900, 1800, 3600, 10800, 21600, 43200, 86400]

_engines = {"db": None, "settings": None, "probe": None}


def get_db() -> DB:
    return _engines["db"]


def get_settings() -> Settings:
    return _engines["settings"]


def get_probe() -> ProbeEngine:
    return _engines["probe"]


# ---------------------------------------------------------------- security

def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return f"sha256${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt, digest = stored.split("$", 2)
    except ValueError:
        return False
    if algo != "sha256":
        return False
    candidate = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return hmac.compare_digest(candidate, digest)


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Optional HTTP Basic auth; enabled from the dashboard once a password is set."""

    async def dispatch(self, request: Request, call_next):
        auth = get_settings().data.get("auth", {})
        if not auth.get("enabled"):
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            import base64

            try:
                raw = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
                user, _, password = raw.partition(":")
            except Exception:  # noqa: BLE001
                user = password = ""
            stored_hash = auth.get("password_hash", "")
            if (
                user == auth.get("username", "admin")
                and stored_hash
                and verify_password(password, stored_hash)
            ):
                return await call_next(request)
        return Response(
            status_code=401,
            content="Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="netmon"'},
        )


# ---------------------------------------------------------------- helpers

def _pick_bucket(span_sec: int) -> int:
    target = max(30, span_sec // 300)
    for bucket in BUCKETS:
        if bucket >= target:
            return bucket
    return BUCKETS[-1]


def _range_window(range_key: str | None, frm: str | None, to: str | None) -> tuple[int, int, str, int]:
    now = now_local()
    if frm or to:
        def parse(value: str | None, default_epoch: float) -> int:
            if not value:
                return int(default_epoch)
            value = value.strip()
            if value.isdigit():
                return int(value)
            return int(datetime.fromisoformat(value).astimezone().timestamp())

        start = parse(frm, now.timestamp() - 86400)
        end = parse(to, now.timestamp() + 1)
        key = "custom"
    else:
        key = range_key if range_key in RANGES else "24h"
        end = int(now.timestamp()) + 1
        start = end - RANGES[key]
    if end <= start:
        raise HTTPException(status_code=400, detail="end must be after start")
    return start, end, key, _pick_bucket(end - start)


# ---------------------------------------------------------------- lifespan

@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    db = DB(DB_PATH)
    settings = Settings(db, DATA_DIR)
    probe = ProbeEngine(db, settings, DATA_DIR)
    _engines.update({"db": db, "settings": settings, "probe": probe})
    probe.start()
    try:
        yield
    finally:
        probe.stop()


app = FastAPI(title="netmon", version=__version__, lifespan=lifespan)
app.add_middleware(BasicAuthMiddleware)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ---------------------------------------------------------------- pages

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "version": __version__,
            "settings": settings.data,
            "default_range": settings.data.get("ui", {}).get("default_range", "24h"),
            "ranges": list(RANGES.keys()),
        },
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse(
        request,
        "settings.html",
        {"version": __version__, "settings": get_settings().data},
    )


@app.get("/data", response_class=HTMLResponse)
async def data_page(request: Request):
    return templates.TemplateResponse(
        request,
        "data.html",
        {
            "version": __version__,
            "settings": get_settings().data,
            "counts": get_db().counts(),
            "bounds": get_db().range_bounds(),
        },
    )


# ---------------------------------------------------------------- api: state

@app.get("/api/health")
async def health():
    return {"ok": True, "version": __version__}


@app.get("/api/status")
async def api_status():
    probe = get_probe()
    db = get_db()
    now = int(now_local().timestamp())
    status = probe.status_dict()
    day_ago = now - 86400
    status["summary_24h"] = {
        "targets": db.ping_summary(day_ago, now),
        "speedtest": db.speedtest_stats(day_ago),
        "coverage": db.coverage(day_ago, now),
    }
    status["counts"] = db.counts()
    status["recent_events"] = db.recent_events(20)
    status["digest_due"] = probe.digest_due_days()
    return status


@app.get("/api/series")
async def api_series(
    range: str = Query("24h"),
    frm: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
):
    db = get_db()
    start, end, key, bucket = _range_window(range, frm, to)
    series = db.latency_series(start, end, bucket)
    return {
        "range": {"key": key, "from": start, "to": end, "bucket": bucket},
        "targets": sorted(series.keys()),
        "latency": series,
        "loss_buckets": db.loss_series(start, end, bucket),
        "speedtests": [r for r in db.speedtests(since_epoch=start) if r["epoch"] < end],
        "summary": {
            "targets": db.ping_summary(start, end),
            "speedtest": db.speedtest_stats(start),
            "coverage": db.coverage(start, end),
        },
        "hourly_profile": db.hourly_profile(end - 30 * 86400),
        "events": db.events_between(start, end)[:100],
    }


@app.get("/api/speedtests")
async def api_speedtests(limit: int = Query(50, le=500)):
    return {"items": get_db().speedtests(limit=limit)}


@app.get("/api/events")
async def api_events(limit: int = Query(100, le=1000)):
    return {"items": get_db().recent_events(limit)}


@app.get("/api/reports")
async def api_reports():
    items = []
    for path in sorted(REPORTS_DIR.glob("report_*.md"), reverse=True):
        png = path.with_suffix(".png")
        items.append({
            "name": path.name,
            "day": path.stem.replace("report_", ""),
            "md_bytes": path.stat().st_size,
            "png": png.name if png.exists() else None,
            "mtime": iso(datetime.fromtimestamp(path.stat().st_mtime)),
        })
    return {"items": items}


@app.get("/reports/{name}")
async def get_report(name: str):
    if "/" in name or ".." in name:
        raise HTTPException(status_code=400, detail="bad name")
    path = REPORTS_DIR / name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    if name.endswith(".png"):
        return Response(content=path.read_bytes(), media_type="image/png")
    if name.endswith(".md"):
        return PlainTextResponse(path.read_text(), media_type="text/markdown")
    raise HTTPException(status_code=400, detail="unsupported file type")


# ---------------------------------------------------------------- api: actions

@app.post("/api/speedtest/run")
async def api_speedtest_run():
    probe = get_probe()
    if probe.speedtest_running:
        return JSONResponse({"ok": False, "error": "a speedtest is already running"}, status_code=409)
    probe.request_speedtest("manual")
    get_db().add_event("info", "speedtest queued from dashboard")
    return {"ok": True, "queued": True}


@app.post("/api/report/send")
async def api_report_send(payload: dict = Body(default={})):
    day = (payload or {}).get("day")
    result = get_probe().send_digest_now(day)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/api/webhook/test")
async def api_webhook_test():
    result = get_probe().test_webhook()
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/api/prune")
async def api_prune(payload: dict = Body(default={})):
    everything = bool((payload or {}).get("everything"))
    days = (payload or {}).get("days")
    if everything and (payload or {}).get("confirm") != "DELETE":
        raise HTTPException(status_code=400, detail="confirmation required")
    if not everything:
        try:
            days = int(days)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="days must be an integer")
    deleted = get_db().prune(older_than_days=None if everything else days, everything=everything)
    get_db().add_event(
        "warn",
        f"data pruned: {'everything' if everything else f'older than {days}d'} -> {deleted}",
    )
    return {"ok": True, "deleted": deleted, "counts": get_db().counts()}


@app.get("/api/export.csv")
async def api_export(table: str = Query("ping"), days: int = Query(0, ge=0)):
    since = int((now_local() - timedelta(days=days)).timestamp()) if days else 0
    try:
        csv_data = get_db().export_csv(table, since or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="netmon_{table}_{day_str()}.csv"'},
    )


# ---------------------------------------------------------------- api: settings

SENSITIVE_FIELDS = {"webhook_url", "password_hash"}


def _redact(data: dict) -> dict:
    out = json.loads(json.dumps(data))
    digest = out.get("digest", {})
    if digest.get("webhook_url"):
        url = digest["webhook_url"]
        digest["webhook_url_set"] = True
        digest["webhook_url_hint"] = f"…{url[-6:]}" if len(url) > 6 else "set"
        digest["webhook_url"] = ""
    auth = out.get("auth", {})
    if auth.get("password_hash"):
        auth["password_set"] = True
        auth["password_hash"] = ""
    return out


@app.get("/api/settings")
async def api_get_settings():
    return {"settings": _redact(get_settings().data), "defaults": DEFAULTS}


@app.post("/api/settings")
async def api_set_settings(payload: dict = Body(default={})):
    settings = get_settings()
    patch = dict(payload or {})

    # never let the redacted placeholder wipe a stored secret
    digest = patch.get("digest")
    if isinstance(digest, dict):
        if digest.get("webhook_url") in (None, ""):
            digest.pop("webhook_url", None)
        digest.pop("webhook_url_set", None)
        digest.pop("webhook_url_hint", None)
    auth = patch.get("auth")
    if isinstance(auth, dict):
        if auth.get("password_hash") in (None, ""):
            auth.pop("password_hash", None)
        auth.pop("password_set", None)
        password = auth.pop("password", None)
        if password:
            auth["password_hash"] = hash_password(str(password))
            auth["enabled"] = True
    patch.pop("defaults", None)

    merged = settings.save(patch)
    auth_cfg = merged.get("auth", {})
    if auth_cfg.get("enabled") and not auth_cfg.get("password_hash"):
        # Never leave the dashboard locked with no password: that is an
        # instant lockout with no way back in short of editing the DB.
        settings.save({"auth": {"enabled": False}})
        raise HTTPException(
            status_code=400,
            detail="set a password before enabling authentication (nothing was changed)",
        )
    get_probe().reload_settings()
    get_db().add_event("info", "settings updated from dashboard")
    return {"ok": True, "settings": _redact(merged)}


def main() -> None:
    import uvicorn

    uvicorn.run(
        "netmon.web:app",
        host=os.environ.get("NETMON_HOST", "0.0.0.0"),
        port=int(os.environ.get("NETMON_PORT", "9120")),
        workers=1,
        log_level=os.environ.get("NETMON_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
