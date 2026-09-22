"""Runtime settings, stored in SQLite so the dashboard can edit them live.

Precedence: SQLite row (written by the dashboard) > environment bootstrap >
DEFAULTS. `NETMON_WEBHOOK_URL` is only used to seed the webhook on first boot;
after that the dashboard value wins.
"""
from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

DEFAULTS: dict = {
    "targets": ["1.1.1.1", "8.8.8.8", "223.5.5.5", "168.95.1.1"],
    "auto_add_gateway": True,
    "ping_interval_sec": 10,
    "ping_timeout_sec": 2,
    "speedtest": {
        "enabled": False,          # off by default: a test saturates the link
        "interval_min": 60,
        "timeout_sec": 180,
        "ookla_bin": "speedtest",
        "min_download_mbps": 0.0,  # 0 = no post-test alert
    },
    "digest": {
        "enabled": True,
        "time": "08:00",           # local time, report covers yesterday
        "webhook_url": "",
        "catch_up": True,          # report days missed while netmon was down
    },
    "thresholds": {
        "loss_pct": 1.0,
        "avg_ping_ms": 60.0,
        "min_download_mbps": 20.0,
        "min_upload_mbps": 5.0,
    },
    "alerts": {
        "enabled": True,
        "consecutive_losses": 3,   # rounds of 100% loss before alerting
        "cooldown_min": 30,
    },
    "retention_days": 0,           # 0 = keep everything (prune from the dashboard)
    "auth": {"enabled": False, "username": "admin", "password_hash": ""},
    "ui": {"default_range": "24h"},
}

SETTINGS_KEY = "config"


def _deep_merge(base: dict, override: dict) -> dict:
    out = deepcopy(base)
    for key, value in (override or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def bootstrap_overrides() -> dict:
    """Environment seeding (docker compose), used only when no DB row exists."""
    out: dict = {}
    if os.environ.get("NETMON_TARGETS"):
        out["targets"] = [t.strip() for t in os.environ["NETMON_TARGETS"].split(",") if t.strip()]
    if os.environ.get("NETMON_PING_INTERVAL_SEC"):
        out["ping_interval_sec"] = float(os.environ["NETMON_PING_INTERVAL_SEC"])
    if os.environ.get("NETMON_WEBHOOK_URL"):
        out["digest"] = {"webhook_url": os.environ["NETMON_WEBHOOK_URL"]}
    if os.environ.get("NETMON_SPEEDTEST_ENABLED", "").lower() in ("1", "true", "yes"):
        out["speedtest"] = {"enabled": True}
    if os.environ.get("NETMON_USER") and os.environ.get("NETMON_PASSWORD_HASH"):
        out["auth"] = {
            "enabled": True,
            "username": os.environ["NETMON_USER"],
            "password_hash": os.environ["NETMON_PASSWORD_HASH"],
        }
    return out


class Settings:
    """Live settings holder: cached dict + reload from DB on demand."""

    def __init__(self, db, data_dir: Path):
        self.db = db
        self.data_dir = Path(data_dir)
        self._cache: dict | None = None
        self.reload()

    def reload(self) -> dict:
        raw = self.db.get_setting(SETTINGS_KEY)
        stored = json.loads(raw) if raw else {}
        if not raw:
            stored = bootstrap_overrides()
            if stored:
                self.db.set_setting(SETTINGS_KEY, json.dumps(stored))
        self._cache = _deep_merge(DEFAULTS, stored)
        return self._cache

    @property
    def data(self) -> dict:
        if self._cache is None:
            self.reload()
        return self._cache

    def save(self, patch: dict) -> dict:
        """Merge a (possibly partial) patch into the stored settings."""
        raw = self.db.get_setting(SETTINGS_KEY)
        stored = json.loads(raw) if raw else {}
        merged = _deep_merge(stored, patch or {})
        self.db.set_setting(SETTINGS_KEY, json.dumps(merged))
        return self.reload()

    def effective_targets(self) -> list[str]:
        from .ping import detect_default_gateway

        data = self.data
        targets = [t for t in (data.get("targets") or []) if t]
        if data.get("auto_add_gateway", True):
            gateway = detect_default_gateway()
            if gateway and gateway not in targets:
                targets.insert(0, gateway)
        return targets

    # convenience accessors ------------------------------------------------
    @property
    def ping_interval(self) -> float:
        return max(1.0, float(self.data.get("ping_interval_sec", 10)))

    @property
    def ping_timeout(self) -> float:
        return max(0.3, float(self.data.get("ping_timeout_sec", 2)))

    @property
    def speedtest(self) -> dict:
        return self.data.get("speedtest", {})

    @property
    def digest(self) -> dict:
        return self.data.get("digest", {})
