"""SQLite storage: probes, speedtests, events, settings, digest bookkeeping.

Design notes
- Every row carries `ts` (local ISO-8601, for display) AND `epoch` (UTC unix
  seconds, for cheap/portable bucketing + indexing).
- One connection per thread (ThreadingHTTPServer/uvicorn + probe threads),
  WAL mode so readers never block the writer.
- Retention is "keep forever" by default (retention_days = 0); pruning is an
  explicit action from the dashboard.
"""
from __future__ import annotations

import csv
import io
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS ping (
    ts      TEXT    NOT NULL,
    epoch   INTEGER NOT NULL,
    target  TEXT    NOT NULL,
    ok      INTEGER NOT NULL,
    rtt_ms  REAL
);
CREATE INDEX IF NOT EXISTS idx_ping_epoch        ON ping(epoch);
CREATE INDEX IF NOT EXISTS idx_ping_target_epoch ON ping(target, epoch);

CREATE TABLE IF NOT EXISTS speedtest (
    ts            TEXT    NOT NULL,
    epoch         INTEGER NOT NULL,
    ok            INTEGER NOT NULL,
    engine        TEXT,
    server        TEXT,
    isp           TEXT,
    external_ip   TEXT,
    ping_ms       REAL,
    jitter_ms     REAL,
    download_mbps REAL,
    upload_mbps   REAL,
    loss_pct      REAL,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_speed_epoch ON speedtest(epoch);

CREATE TABLE IF NOT EXISTS events (
    ts      TEXT    NOT NULL,
    epoch   INTEGER NOT NULL,
    level   TEXT,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_epoch ON events(epoch);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    day     TEXT PRIMARY KEY,
    sent_ts TEXT,
    ok      INTEGER,
    error   TEXT
);
"""

PING_FIELDS = ["ts", "epoch", "target", "ok", "rtt_ms"]
SPEED_FIELDS = [
    "ts", "epoch", "ok", "engine", "server", "isp", "external_ip",
    "ping_ms", "jitter_ms", "download_mbps", "upload_mbps", "loss_pct", "error",
]


def now_local() -> datetime:
    return datetime.now().astimezone()


def iso(dt: datetime | None = None) -> str:
    return (dt or now_local()).isoformat(timespec="seconds")


def day_str(dt: datetime | None = None) -> str:
    return (dt or now_local()).strftime("%Y%m%d")


class DB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        with self._write_lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # ------------------------------------------------------------ plumbing
    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=15000")
            self._local.conn = conn
        return conn

    def _exec(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        with self._write_lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def _query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    # ------------------------------------------------------------ writes
    def add_ping(self, rows: list[dict]) -> None:
        if not rows:
            return
        with self._write_lock:
            self.conn.executemany(
                "INSERT INTO ping (ts, epoch, target, ok, rtt_ms) VALUES (?,?,?,?,?)",
                [
                    (r["ts"], int(r["epoch"]), r["target"], int(r["ok"]), r.get("rtt_ms"))
                    for r in rows
                ],
            )
            self.conn.commit()

    def add_speedtest(self, row: dict) -> None:
        with self._write_lock:
            self.conn.execute(
                "INSERT INTO speedtest (ts, epoch, ok, engine, server, isp, external_ip,"
                " ping_ms, jitter_ms, download_mbps, upload_mbps, loss_pct, error)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["ts"], int(row["epoch"]), int(row.get("ok", 1)),
                    row.get("engine"), row.get("server"), row.get("isp"),
                    row.get("external_ip"), row.get("ping_ms"), row.get("jitter_ms"),
                    row.get("download_mbps"), row.get("upload_mbps"),
                    row.get("loss_pct"), row.get("error"),
                ),
            )
            self.conn.commit()

    def add_event(self, level: str, message: str) -> None:
        ts = now_local()
        self._exec(
            "INSERT INTO events (ts, epoch, level, message) VALUES (?,?,?,?)",
            (iso(ts), int(ts.timestamp()), level, message),
        )

    def recent_events(self, limit: int = 50) -> list[dict]:
        rows = self._query("SELECT * FROM events ORDER BY epoch DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ settings
    def get_setting(self, key: str) -> str | None:
        rows = self._query("SELECT value FROM settings WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set_setting(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO settings (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ------------------------------------------------------------ queries
    def ping_summary(self, since_epoch: int, until_epoch: int | None = None) -> list[dict]:
        """Per-target aggregates over a window."""
        until_epoch = until_epoch or int(now_local().timestamp()) + 1
        rows = self._query(
            """
            SELECT target,
                   COUNT(*)                                   AS samples,
                   SUM(ok)                                    AS ok_count,
                   AVG(CASE WHEN ok = 1 THEN rtt_ms END)      AS avg_rtt,
                   MIN(CASE WHEN ok = 1 THEN rtt_ms END)      AS min_rtt,
                   MAX(CASE WHEN ok = 1 THEN rtt_ms END)      AS max_rtt,
                   MAX(epoch)                                 AS last_epoch
            FROM ping WHERE epoch >= ? AND epoch < ?
            GROUP BY target ORDER BY target
            """,
            (since_epoch, until_epoch),
        )
        out = []
        for r in rows:
            samples = r["samples"] or 0
            out.append({
                "target": r["target"],
                "samples": samples,
                "ok": r["ok_count"] or 0,
                "loss_pct": (100.0 * (samples - (r["ok_count"] or 0)) / samples) if samples else None,
                "avg_rtt": r["avg_rtt"],
                "min_rtt": r["min_rtt"],
                "max_rtt": r["max_rtt"],
                "last_epoch": r["last_epoch"],
            })
        return out

    def latency_series(
        self, since_epoch: int, until_epoch: int, bucket_sec: int
    ) -> dict[str, list[list[float]]]:
        """Bucketed per-target series: {target: [[epoch, avg_rtt, loss_pct], ...]}."""
        rows = self._query(
            """
            SELECT (epoch / ?) * ? AS bucket,
                   target,
                   AVG(CASE WHEN ok = 1 THEN rtt_ms END) AS avg_rtt,
                   COUNT(*)                              AS samples,
                   SUM(ok)                               AS ok_count
            FROM ping
            WHERE epoch >= ? AND epoch < ?
            GROUP BY bucket, target
            ORDER BY bucket
            """,
            (bucket_sec, bucket_sec, since_epoch, until_epoch),
        )
        series: dict[str, list[list[float]]] = {}
        for r in rows:
            samples = r["samples"] or 0
            loss = (100.0 * (samples - (r["ok_count"] or 0)) / samples) if samples else 0.0
            series.setdefault(r["target"], []).append(
                [int(r["bucket"]), round(r["avg_rtt"], 2) if r["avg_rtt"] is not None else None,
                 round(loss, 2)]
            )
        return series

    def loss_series(self, since_epoch: int, until_epoch: int, bucket_sec: int) -> list[dict]:
        """Buckets with any loss at all, for outage/incident display."""
        rows = self._query(
            """
            SELECT (epoch / ?) * ? AS bucket, target, COUNT(*) AS samples, SUM(ok) AS ok_count
            FROM ping WHERE epoch >= ? AND epoch < ?
            GROUP BY bucket, target
            HAVING samples > ok_count
            ORDER BY bucket
            """,
            (bucket_sec, bucket_sec, since_epoch, until_epoch),
        )
        return [
            {
                "epoch": int(r["bucket"]),
                "target": r["target"],
                "samples": r["samples"],
                "loss_pct": round(100.0 * (r["samples"] - (r["ok_count"] or 0)) / r["samples"], 2),
            }
            for r in rows
        ]

    def speedtests(self, since_epoch: int | None = None, limit: int | None = None) -> list[dict]:
        sql = "SELECT * FROM speedtest"
        params: list[Any] = []
        if since_epoch is not None:
            sql += " WHERE epoch >= ?"
            params.append(since_epoch)
        sql += " ORDER BY epoch DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [dict(r) for r in self._query(sql, params)]

    def speedtest_stats(self, since_epoch: int) -> dict:
        rows = self._query(
            """
            SELECT COUNT(*) AS n,
                   AVG(download_mbps) AS dl_avg, MIN(download_mbps) AS dl_min, MAX(download_mbps) AS dl_max,
                   AVG(upload_mbps)   AS ul_avg, MIN(upload_mbps)   AS ul_min, MAX(upload_mbps)   AS ul_max,
                   AVG(ping_ms)       AS ping_avg, AVG(jitter_ms)  AS jitter_avg
            FROM speedtest WHERE epoch >= ? AND ok = 1
            """,
            (since_epoch,),
        )
        return dict(rows[0]) if rows else {}

    def last_speedtest(self) -> dict | None:
        rows = self._query("SELECT * FROM speedtest ORDER BY epoch DESC LIMIT 1")
        return dict(rows[0]) if rows else None

    def coverage(self, since_epoch: int, until_epoch: int | None = None) -> dict:
        """Samples we actually have vs samples we should have (probe uptime)."""
        until_epoch = until_epoch or int(now_local().timestamp()) + 1
        rows = self._query(
            """
            SELECT COUNT(*) AS samples,
                   COUNT(DISTINCT target) AS targets,
                   MIN(epoch) AS first_epoch, MAX(epoch) AS last_epoch
            FROM ping WHERE epoch >= ? AND epoch < ?
            """,
            (since_epoch, until_epoch),
        )
        r = rows[0]
        return {
            "samples": r["samples"] or 0,
            "targets": r["targets"] or 0,
            "first_epoch": r["first_epoch"],
            "last_epoch": r["last_epoch"],
        }

    def hourly_profile(self, since_epoch: int) -> list[dict]:
        """Average latency + loss per local hour-of-day (all targets, WAN only)."""
        rows = self._query(
            """
            SELECT CAST(strftime('%H', epoch, 'unixepoch', 'localtime') AS INTEGER) AS hour,
                   AVG(CASE WHEN ok = 1 THEN rtt_ms END) AS avg_rtt,
                   COUNT(*) AS samples, SUM(ok) AS ok_count
            FROM ping WHERE epoch >= ?
            GROUP BY hour ORDER BY hour
            """,
            (since_epoch,),
        )
        return [
            {
                "hour": r["hour"],
                "avg_rtt": r["avg_rtt"],
                "loss_pct": (100.0 * (r["samples"] - (r["ok_count"] or 0)) / r["samples"])
                if r["samples"] else None,
                "samples": r["samples"],
            }
            for r in rows
        ]

    def distinct_targets(self) -> list[str]:
        rows = self._query("SELECT DISTINCT target FROM ping ORDER BY target")
        return [r["target"] for r in rows]

    def ping_rows(self, since_epoch: int, until_epoch: int, target: str | None = None) -> list[dict]:
        sql = "SELECT ts, epoch, target, ok, rtt_ms FROM ping WHERE epoch >= ? AND epoch < ?"
        params: list[Any] = [since_epoch, until_epoch]
        if target:
            sql += " AND target = ?"
            params.append(target)
        sql += " ORDER BY epoch"
        return [dict(r) for r in self._query(sql, params)]

    def events_between(self, since_epoch: int, until_epoch: int) -> list[dict]:
        rows = self._query(
            "SELECT * FROM events WHERE epoch >= ? AND epoch < ? ORDER BY epoch",
            (since_epoch, until_epoch),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ reports
    def last_report_days(self) -> dict[str, dict]:
        rows = self._query("SELECT * FROM reports")
        return {r["day"]: dict(r) for r in rows}

    def mark_report(self, day: str, ok: bool, error: str | None = None) -> None:
        self._exec(
            "INSERT INTO reports (day, sent_ts, ok, error) VALUES (?,?,?,?)"
            " ON CONFLICT(day) DO UPDATE SET sent_ts = excluded.sent_ts,"
            " ok = excluded.ok, error = excluded.error",
            (day, iso(), int(ok), error),
        )

    # ------------------------------------------------------------ admin
    def counts(self) -> dict:
        out = {}
        for table in ("ping", "speedtest", "events"):
            out[table] = self._query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
        out["reports"] = self._query("SELECT COUNT(*) AS n FROM reports")[0]["n"]
        out["db_bytes"] = self.path.stat().st_size if self.path.exists() else 0
        for suffix in ("-wal", "-shm"):
            extra = Path(str(self.path) + suffix)
            if extra.exists():
                out["db_bytes"] += extra.stat().st_size
        return out

    def range_bounds(self) -> dict:
        rows = self._query(
            "SELECT MIN(epoch) AS first, MAX(epoch) AS last FROM ("
            "  SELECT epoch FROM ping UNION ALL SELECT epoch FROM speedtest)"
        )
        return {"first_epoch": rows[0]["first"], "last_epoch": rows[0]["last"]}

    def prune(self, older_than_days: int | None = None, everything: bool = False) -> dict:
        """Delete history. everything=True wipes all samples; else keep N days."""
        deleted = {}
        if everything:
            for table in ("ping", "speedtest", "events"):
                cur = self._exec(f"DELETE FROM {table}")
                deleted[table] = cur.rowcount if cur.rowcount is not None else 0
            self._exec("DELETE FROM reports")
            self._exec("VACUUM")
            return deleted
        if not older_than_days or older_than_days <= 0:
            return deleted
        cutoff = int((now_local() - timedelta(days=older_than_days)).timestamp())
        for table in ("ping", "speedtest", "events"):
            cur = self._exec(f"DELETE FROM {table} WHERE epoch < ?", (cutoff,))
            deleted[table] = cur.rowcount if cur.rowcount is not None else 0
        self._exec("VACUUM")
        return deleted

    def export_csv(self, table: str, since_epoch: int | None = None) -> str:
        if table not in ("ping", "speedtest", "events"):
            raise ValueError(f"unknown table {table!r}")
        fields = {"ping": PING_FIELDS, "speedtest": SPEED_FIELDS,
                  "events": ["ts", "epoch", "level", "message"]}[table]
        sql = f"SELECT {', '.join(fields)} FROM {table}"
        params: list[Any] = []
        if since_epoch:
            sql += " WHERE epoch >= ?"
            params.append(since_epoch)
        sql += " ORDER BY epoch"
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(fields)
        for row in self._query(sql, params):
            writer.writerow([row[f] for f in fields])
        return buf.getvalue()
