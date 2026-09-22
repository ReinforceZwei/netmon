"""Probe engine: continuous ping rounds, speedtest worker, digest scheduler.

Threads (all daemon, started by `ProbeEngine.start()`):
- ping loop      : one round of ICMP probes to every target each interval
- speedtest worker: consumes a queue -> manual "run now" or scheduled runs
- digest scheduler: sends the daily report (+ catch-up for missed days)
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__
from .db import DB, day_str, iso, now_local
from .notify import NotifyError, send_report, send_webhook
from .ping import detect_default_gateway, probe_once
from .speedtest import SpeedtestError, available_engines, run_speedtest

log = logging.getLogger("netmon.probe")


class ProbeEngine:
    def __init__(self, db: DB, settings, data_dir: Path):
        self.db = db
        self.settings = settings
        self.data_dir = Path(data_dir)
        self.reports_dir = self.data_dir / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)

        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=12, thread_name_prefix="probe")
        self._speedtest_q: queue.Queue[str] = queue.Queue()
        self._threads: list[threading.Thread] = []

        self.started_at = now_local()
        self.speedtest_running = False
        self.speedtest_queued = 0
        self.next_speedtest_epoch: float | None = None
        self.last_digest_sent: dict | None = None
        self.status: dict = {
            "version": __version__,
            "targets": [],
            "gateway": None,
            "per_target": {},
            "last_round_epoch": None,
            "rounds": 0,
            "errors": 0,
            "alert_active": False,
            "last_alert_epoch": None,
        }
        self._consecutive_outages = 0
        self._last_alert_epoch = 0.0

    # ------------------------------------------------------------- control
    def start(self) -> None:
        for name, target in (
            ("ping", self._ping_loop),
            ("speedtest", self._speedtest_loop),
            ("digest", self._digest_loop),
        ):
            thread = threading.Thread(target=target, name=f"netmon-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)
        self.db.add_event("info", f"netmon {__version__} started")
        self._log_startup()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self.db.add_event("info", "netmon stopping")
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _log_startup(self) -> None:
        cfg = self.settings.data
        targets = self.settings.effective_targets()
        gateway = detect_default_gateway()
        with self._lock:
            self.status["targets"] = targets
            self.status["gateway"] = gateway
        engines = available_engines(cfg.get("speedtest", {}).get("ookla_bin", "speedtest"))
        self.db.add_event(
            "info",
            f"probe config: targets={targets} interval={self.settings.ping_interval:g}s "
            f"speedtest={'on' if cfg.get('speedtest', {}).get('enabled') else 'off'} "
            f"(engines: {', '.join(engines) or 'none'})",
        )

    def reload_settings(self) -> None:
        """Called by the web layer after a settings save: apply immediately."""
        self.settings.reload()
        self._log_startup()
        if self.settings.speedtest.get("enabled") and self.next_speedtest_epoch is None:
            self.next_speedtest_epoch = time.time() + 30

    def request_speedtest(self, reason: str = "manual") -> bool:
        """Queue a speedtest (manual button or scheduler). Returns False if busy."""
        if self.speedtest_running:
            return False
        self.speedtest_queued += 1
        self._speedtest_q.put(reason)
        return True

    # ------------------------------------------------------------- status
    def status_dict(self) -> dict:
        cfg = self.settings.data
        with self._lock:
            status = {k: v for k, v in self.status.items()}
        now = now_local()
        status["now"] = iso(now)
        status["uptime_sec"] = int((now - self.started_at).total_seconds())
        status["started_at"] = iso(self.started_at)
        status["settings"] = {
            "ping_interval_sec": self.settings.ping_interval,
            "ping_timeout_sec": self.settings.ping_timeout,
            "speedtest_enabled": bool(cfg.get("speedtest", {}).get("enabled")),
            "speedtest_interval_min": cfg.get("speedtest", {}).get("interval_min"),
            "digest_enabled": bool(cfg.get("digest", {}).get("enabled")),
            "digest_time": cfg.get("digest", {}).get("time"),
            "webhook_configured": bool(cfg.get("digest", {}).get("webhook_url")),
            "retention_days": cfg.get("retention_days", 0),
        }
        status["speedtest"] = {
            "running": self.speedtest_running,
            "queued": self.speedtest_queued,
            "next_epoch": self.next_speedtest_epoch,
            "last": self.db.last_speedtest(),
        }
        status["alerts"] = {
            "active": status.get("alert_active", False),
            "consecutive_outage_rounds": self._consecutive_outages,
        }
        return status

    # ------------------------------------------------------------- ping
    def _ping_loop(self) -> None:
        """Run one probe round per interval, keeping the cadence drift-free."""
        next_round = time.monotonic()
        while not self._stop.is_set():
            try:
                self._ping_round()
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                log.warning("ping round failed: %s", exc)
                try:
                    self.db.add_event("error", f"ping round failed: {exc}")
                except Exception:  # noqa: BLE001
                    pass
            interval = self.settings.ping_interval
            next_round += interval
            delay = next_round - time.monotonic()
            if delay <= 0:  # we fell behind (long timeouts): resync instead of bursting
                next_round = time.monotonic()
                delay = interval
            self._stop.wait(delay)

    def _ping_round(self) -> None:
        targets = self.settings.effective_targets()
        timeout = self.settings.ping_timeout
        ts = now_local()
        ts_iso = iso(ts)
        epoch = int(ts.timestamp())

        futures = {
            self._pool.submit(probe_once, target, timeout): target for target in targets
        }
        rows: list[dict] = []
        results: dict[str, bool] = {}
        for future, target in futures.items():
            try:
                ok, rtt = future.result(timeout=timeout + 3)
            except Exception:  # noqa: BLE001 - a broken probe must not kill the loop
                ok, rtt = False, None
            rows.append({
                "ts": ts_iso,
                "epoch": epoch,
                "target": target,
                "ok": ok,
                "rtt_ms": round(rtt, 2) if ok and rtt is not None else None,
            })
            results[target] = ok

        self.db.add_ping(rows)
        gateway = detect_default_gateway() if self.settings.data.get("auto_add_gateway", True) else None
        with self._lock:
            per_target = self.status["per_target"]
            for target in targets:
                ok = results.get(target, False)
                info = per_target.setdefault(
                    target, {"probes": 0, "losses": 0, "consecutive_losses": 0, "last_ok": None,
                             "last_rtt_ms": None}
                )
                info["probes"] += 1
                if ok:
                    info["last_ok"] = ts_iso
                    info["last_rtt_ms"] = next(
                        (r["rtt_ms"] for r in rows if r["target"] == target), None
                    )
                    info["consecutive_losses"] = 0
                else:
                    info["losses"] += 1
                    info["consecutive_losses"] += 1
            self.status["targets"] = targets
            self.status["gateway"] = gateway
            self.status["last_round_epoch"] = epoch
            self.status["rounds"] += 1
            wan = [t for t in targets if t != gateway]
            all_down = bool(wan) and all(not results.get(t, False) for t in wan)
        if all_down:
            self._consecutive_outages += 1
            self._maybe_alert_outage(True)
        else:
            if self._consecutive_outages:
                self.db.add_event("info", f"connectivity recovered after {self._consecutive_outages} failed round(s)")
            self._consecutive_outages = 0
            self._maybe_alert_outage(False)

    def _maybe_alert_outage(self, down: bool) -> None:
        alerts = self.settings.data.get("alerts", {})
        if not alerts.get("enabled") or not down:
            if not down:
                was_active = self.status.get("alert_active")
                if was_active:
                    with self._lock:
                        self.status["alert_active"] = False
                    self._notify_plain("outage", "✅ **Connectivity recovered** — all monitored targets are responding again.")
            return
        threshold = max(1, int(alerts.get("consecutive_losses", 3)))
        if self._consecutive_outages < threshold or self.status.get("alert_active"):
            return
        cooldown = max(1, int(alerts.get("cooldown_min", 30))) * 60
        if time.time() - self._last_alert_epoch < cooldown:
            return
        self._last_alert_epoch = time.time()
        with self._lock:
            self.status["alert_active"] = True
            self.status["last_alert_epoch"] = int(self._last_alert_epoch)
        targets = ", ".join(self.status.get("targets", []))
        self.db.add_event("warn", f"outage alert: {self._consecutive_outages} rounds 100% loss")
        self._notify_plain(
            "outage",
            f"🚨 **Network outage** — no response from any monitored target for "
            f"{self._consecutive_outages} consecutive rounds "
            f"(~{self._consecutive_outages * self.settings.ping_interval:.0f}s).\n"
            f"Targets: {targets}",
        )

    # ------------------------------------------------------------- speedtest
    def _speedtest_loop(self) -> None:
        while not self._stop.is_set():
            cfg = self.settings.speedtest
            enabled = bool(cfg.get("enabled"))
            interval = max(1, int(cfg.get("interval_min", 60) or 60)) * 60
            if not enabled:
                self.next_speedtest_epoch = None
            elif self.next_speedtest_epoch is None:
                self.next_speedtest_epoch = time.time() + interval
            elif time.time() >= self.next_speedtest_epoch:
                if self.request_speedtest("scheduled"):
                    self.next_speedtest_epoch = time.time() + interval

            try:
                reason = self._speedtest_q.get(timeout=1.0)
            except queue.Empty:
                continue
            self.speedtest_queued = max(0, self.speedtest_queued - 1)
            self._run_speedtest(reason)

    def _run_speedtest(self, reason: str) -> None:
        cfg = self.settings.speedtest
        self.speedtest_running = True
        self.db.add_event("info", f"speedtest starting ({reason})")
        result: dict
        try:
            res = run_speedtest(
                cfg.get("ookla_bin", "speedtest"),
                int(cfg.get("timeout_sec", 180) or 180),
            )
            ts = now_local()
            result = {
                "ts": iso(ts),
                "epoch": int(ts.timestamp()),
                "ok": 1,
                "engine": res.get("engine"),
                "server": res.get("server"),
                "isp": res.get("isp"),
                "external_ip": res.get("external_ip"),
                "ping_ms": _num(res.get("ping_ms")),
                "jitter_ms": _num(res.get("jitter_ms")),
                "download_mbps": _num(res.get("download_mbps")),
                "upload_mbps": _num(res.get("upload_mbps")),
                "loss_pct": _num(res.get("packet_loss_pct")),
                "error": None,
            }
            self.db.add_speedtest(result)
            self.db.add_event(
                "info",
                f"speedtest ok: dl={result['download_mbps']} Mbps "
                f"ul={result['upload_mbps']} Mbps ping={result['ping_ms']} ms",
            )
            floor = float(cfg.get("min_download_mbps") or 0)
            if floor > 0 and (result["download_mbps"] or 0) < floor:
                self._notify_plain(
                    "speedtest",
                    f"⚠️ **Speed test below floor** — {result['download_mbps']:.1f} Mbps "
                    f"(expected ≥ {floor:.0f} Mbps), upload {result['upload_mbps']:.1f} Mbps, "
                    f"ping {result['ping_ms']} ms.",
                )
        except SpeedtestError as exc:
            ts = now_local()
            self.db.add_speedtest({
                "ts": iso(ts), "epoch": int(ts.timestamp()), "ok": 0,
                "engine": None, "server": None, "isp": None, "external_ip": None,
                "ping_ms": None, "jitter_ms": None, "download_mbps": None,
                "upload_mbps": None, "loss_pct": None, "error": str(exc)[:300],
            })
            self.db.add_event("error", f"speedtest failed: {exc}")
            with self._lock:
                self.status["errors"] += 1
        finally:
            self.speedtest_running = False

    # ------------------------------------------------------------- digest
    def _digest_loop(self) -> None:
        # short delay so startup events are written first
        self._stop.wait(20)
        while not self._stop.is_set():
            try:
                self._maybe_send_digests()
            except Exception as exc:  # noqa: BLE001 - scheduler must survive
                log.warning("digest scheduler error: %s", exc)
            self._stop.wait(60)

    def digest_due_days(self) -> list[str]:
        """Days that should have a digest but don't (yesterday + catch-up)."""
        cfg = self.settings.digest
        if not cfg.get("enabled") or not cfg.get("webhook_url"):
            return []
        now = now_local()
        hour, minute = _parse_hhmm(cfg.get("time", "08:00"))
        days: list[str] = []
        yesterday = (now - timedelta(days=1)).date()
        if (now.hour, now.minute) >= (hour, minute):
            days.append(yesterday.strftime("%Y%m%d"))
        sent = self.db.last_report_days()
        if cfg.get("catch_up", True):
            prior = [d for d in sent if sent[d].get("ok")]
            lookback = 7 if prior else 1
            for i in range(2, lookback + 2):
                day = (now - timedelta(days=i)).strftime("%Y%m%d")
                if day not in sent:
                    days.append(day)
        out = []
        for day in days:
            if day in out:
                continue
            stored = sent.get(day)
            if stored is not None:
                if stored.get("ok"):
                    continue  # already delivered
                # a FAILED send is retried after a cooldown (Discord hiccup,
                # temporary network loss) instead of silently skipping the day
                sent_at = _parse_iso(stored.get("sent_ts"))
                if sent_at and (now - sent_at) < timedelta(minutes=30):
                    continue
            # only days we actually have data for
            bounds = self.db.coverage(
                int(datetime.strptime(day, "%Y%m%d").timestamp()),
                int((datetime.strptime(day, "%Y%m%d") + timedelta(days=1)).timestamp()),
            )
            if bounds["samples"]:
                out.append(day)
        return sorted(out)

    def _maybe_send_digests(self) -> None:
        from .report import generate_report

        for day in self.digest_due_days():
            report_path = generate_report(self.db, day, self.reports_dir, self.settings)
            header = f"Daily report {day[:4]}-{day[4:6]}-{day[6:]}"
            try:
                send_report(self.settings.digest.get("webhook_url", ""), report_path, header)
                self.db.mark_report(day, True)
                self.db.add_event("info", f"daily report sent for {day}")
                self.last_digest_sent = {"day": day, "ok": True, "ts": iso()}
            except NotifyError as exc:
                self.db.mark_report(day, False, str(exc)[:300])
                self.db.add_event("error", f"daily report failed for {day}: {exc}")
                self.last_digest_sent = {"day": day, "ok": False, "ts": iso(), "error": str(exc)[:300]}

    def send_digest_now(self, day: str | None = None) -> dict:
        """Manual 'send report' action from the dashboard."""
        from .report import generate_report

        webhook = self.settings.digest.get("webhook_url", "")
        if not webhook:
            return {"ok": False, "error": "no webhook url configured"}
        target_day = day or self._default_report_day()
        report_path = generate_report(self.db, target_day, self.reports_dir, self.settings)
        header = f"Report {target_day[:4]}-{target_day[4:6]}-{target_day[6:]} (manual)"
        try:
            send_report(webhook, report_path, header)
        except NotifyError as exc:
            self.db.add_event("error", f"manual report failed for {target_day}: {exc}")
            return {"ok": False, "error": str(exc), "report": str(report_path)}
        self.db.mark_report(target_day, True)
        self.db.add_event("info", f"manual report sent for {target_day}")
        return {"ok": True, "day": target_day, "report": str(report_path)}

    def _default_report_day(self) -> str:
        """Yesterday if it has samples, else today's partial day."""
        now = now_local()
        yesterday = now - timedelta(days=1)
        for day in (yesterday, now):
            start = int(day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            if self.db.coverage(start, start + 86400)["samples"]:
                return day.strftime("%Y%m%d")
        return yesterday.strftime("%Y%m%d")

    def test_webhook(self) -> dict:
        webhook = self.settings.digest.get("webhook_url", "")
        if not webhook:
            return {"ok": False, "error": "no webhook url configured"}
        try:
            send_webhook(
                webhook,
                f"✅ Test message from netmon {__version__} on {now_local().strftime('%Y-%m-%d %H:%M:%S %Z')}",
            )
        except NotifyError as exc:
            return {"ok": False, "error": str(exc)}
        self.db.add_event("info", "webhook test sent")
        return {"ok": True}

    def _notify_plain(self, kind: str, content: str) -> None:
        webhook = self.settings.digest.get("webhook_url", "")
        if not webhook:
            log.info("no webhook configured, dropping %s alert", kind)
            return
        try:
            send_webhook(webhook, content)
        except NotifyError as exc:
            self.db.add_event("error", f"alert send failed ({kind}): {exc}")


def _num(value):
    if value in (None, ""):
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _parse_iso(value) -> datetime | None:
    """Parse a stored ISO timestamp (with offset) into an aware datetime."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _parse_hhmm(value: str) -> tuple[int, int]:
    try:
        hour, minute = str(value).split(":")
        return max(0, min(23, int(hour))), max(0, min(59, int(minute)))
    except (ValueError, TypeError):
        return 8, 0
