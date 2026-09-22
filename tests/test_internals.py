"""netmon internal checks: storage, digest scheduling, stats, report, notify.

Deliberately dependency-light (no pytest): run it from the repo root.

    python tests/test_internals.py
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from netmon.db import DB, now_local
from netmon.notify import summarize_report
from netmon.probe import ProbeEngine, _parse_hhmm, _parse_iso
from netmon.report import find_outages, generate_report, per_target_stats
from netmon.settings import Settings
from netmon.web import hash_password, verify_password

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")
    if not cond:
        FAILURES.append(name)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="netmon-test-"))
    db = DB(tmp / "t.db")
    settings = Settings(db, tmp)
    settings.save({
        "digest": {"enabled": True, "webhook_url": "http://127.0.0.1:9/none"},
        "ping_interval_sec": 10,
    })
    engine = ProbeEngine(db, settings, tmp)

    # ---- a day of samples: 60 good, then 5 consecutive losses
    yesterday = now_local() - timedelta(days=1)
    day = yesterday.strftime("%Y%m%d")
    start = int(yesterday.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    rows = [
        {
            "ts": datetime.fromtimestamp(start + i * 10).astimezone().isoformat(timespec="seconds"),
            "epoch": start + i * 10, "target": "1.1.1.1", "ok": 1, "rtt_ms": 12.0 + (i % 5),
        }
        for i in range(60)
    ] + [
        {
            "ts": datetime.fromtimestamp(start + 600 + i * 10).astimezone().isoformat(timespec="seconds"),
            "epoch": start + 600 + i * 10, "target": "1.1.1.1", "ok": 0, "rtt_ms": None,
        }
        for i in range(5)
    ]
    db.add_ping(rows)

    status = engine.status_dict()
    check("status_dict exposes live state", bool(status["version"]) and "per_target" in status)

    # ---- digest scheduling: due, delivered, retry-after-failure
    check("day without a report is due", day in engine.digest_due_days(), str(engine.digest_due_days()))
    db.mark_report(day, True)
    check("delivered day is not re-sent", day not in engine.digest_due_days())
    db.mark_report(day, False, "boom")
    check("just-failed day respects the cooldown", day not in engine.digest_due_days())
    db._exec(
        "UPDATE reports SET sent_ts = ? WHERE day = ?",
        ((now_local() - timedelta(minutes=45)).isoformat(timespec="seconds"), day),
    )
    check("old failed day is retried", day in engine.digest_due_days(), str(engine.digest_due_days()))

    # ---- stats + outage detection
    stats = per_target_stats(rows)
    s = stats["1.1.1.1"]
    check("per-target stats", s["ok"] == 60 and s["timeouts"] == 5 and abs(s["loss_pct"] - 7.69) < 0.1,
          f"ok={s['ok']} loss={s['loss_pct']:.2f}%")
    outages = find_outages(rows, 10)
    check("outage detected (5 consecutive losses)",
          len(outages) == 1 and outages[0]["count"] == 5,
          str([(o["count"], round(o["duration_s"])) for o in outages]))

    # ---- settings: live reload + partial-patch merge
    settings.save({"ping_interval_sec": 3})
    check("settings reload", engine.settings.ping_interval == 3, str(engine.settings.ping_interval))
    settings.save({"speedtest": {"enabled": True}})  # e.g. the dashboard toggle
    check("partial patch merges into stored settings",
          settings.data["digest"]["webhook_url"].endswith("/none")
          and settings.data["speedtest"]["enabled"] is True
          and settings.data["speedtest"]["interval_min"] == 60
          and bool(settings.data["targets"]))

    # ---- auth helpers
    digest = hash_password("hunter2")
    check("password verify", verify_password("hunter2", digest) and not verify_password("nope", digest))
    check("hash format", digest.startswith("sha256$") and len(digest.split("$")) == 3)

    # ---- report + Discord summariser round trip
    report = generate_report(db, day, tmp / "reports", settings)
    text = report.read_text()
    check("report has the tables and a verdict",
          "| target | probes" in text and "Overall verdict" in text)
    check("report chart written", (tmp / "reports" / f"report_{day}.png").exists())
    summary = summarize_report(report)
    check("summarizer parses our markdown",
          "worst target loss %" in summary and "coverage" in summary, summary[:60])

    # ---- small helpers
    check("hhmm parse", _parse_hhmm("08:30") == (8, 30) and _parse_hhmm("junk") == (8, 0))
    check("iso parse", _parse_iso("2026-01-02T03:04:05+00:00") is not None and _parse_iso(None) is None)

    print()
    print("FAILURES:", FAILURES or "none")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
