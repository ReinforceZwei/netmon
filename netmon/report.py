"""Report generation from the SQLite store: markdown summary + PNG chart.

The markdown keeps the exact section/table shape of the 5G trial monitor so
`notify.summarize_report()` (regex-based Discord summariser) works unchanged.
"""
from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("netmon.report")


def _fmt(value, digits: int = 2) -> str:
    if value is None or value == "":
        return "-"
    try:
        return f"{float(value):.{digits}f}"
    except (ValueError, TypeError):
        return str(value)


def _pct(sorted_vals, p):
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, int(round(p * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def _day_bounds(day: str) -> tuple[int, int]:
    start = datetime.strptime(day, "%Y%m%d")
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def per_target_stats(ping_rows: list[dict]) -> dict[str, dict]:
    by_target: dict[str, list[dict]] = defaultdict(list)
    for row in ping_rows:
        by_target[row.get("target", "")].append(row)
    stats: dict[str, dict] = {}
    for target, rows in by_target.items():
        rtts = [float(r["rtt_ms"]) for r in rows if r.get("ok") and r.get("rtt_ms") is not None]
        sorted_rtts = sorted(rtts)
        timeouts = len(rows) - len(rtts)
        stats[target] = {
            "probes": len(rows),
            "ok": len(rtts),
            "timeouts": timeouts,
            "loss_pct": 100.0 * timeouts / len(rows) if rows else None,
            "min": sorted_rtts[0] if sorted_rtts else None,
            "avg": statistics.mean(rtts) if rtts else None,
            "max": sorted_rtts[-1] if sorted_rtts else None,
            "p95": _pct(sorted_rtts, 0.95),
            "p99": _pct(sorted_rtts, 0.99),
            "jitter": statistics.pstdev(rtts) if len(rtts) > 1 else (0.0 if rtts else None),
        }
    return stats


def find_outages(ping_rows: list[dict], interval_sec: float, min_consecutive: int = 3) -> list[dict]:
    """Consecutive timeout runs (>= min_consecutive) per target, as periods."""
    by_target: dict[str, list[dict]] = defaultdict(list)
    for row in ping_rows:
        by_target[row.get("target", "")].append(row)
    outages: list[dict] = []
    for target, rows in by_target.items():
        rows.sort(key=lambda r: r["epoch"])
        run: list[dict] = []
        gap = interval_sec * 3
        for row in rows:
            lost = not row.get("ok")
            if lost:
                if run and (row["epoch"] - run[-1]["epoch"]) > gap:
                    if len(run) >= min_consecutive:
                        outages.append(_outage(target, run, interval_sec))
                    run = []
                run.append(row)
            else:
                if len(run) >= min_consecutive:
                    outages.append(_outage(target, run, interval_sec))
                run = []
        if len(run) >= min_consecutive:
            outages.append(_outage(target, run, interval_sec))
    outages.sort(key=lambda o: o["start_epoch"])
    return outages


def _outage(target: str, run: list[dict], interval_sec: float) -> dict:
    return {
        "target": target,
        "start_epoch": run[0]["epoch"],
        "end_epoch": run[-1]["epoch"],
        "start": run[0]["ts"],
        "end": run[-1]["ts"],
        "count": len(run),
        "duration_s": (run[-1]["epoch"] - run[0]["epoch"]) + interval_sec,
    }


def speed_summary(speed_rows: list[dict]) -> dict | None:
    rows = [r for r in speed_rows if r.get("ok")]
    if not rows:
        return None

    def series(key: str) -> list[float]:
        return [float(r[key]) for r in rows if r.get(key) is not None]

    def mm(vals):
        if not vals:
            return (None, None, None)
        return (min(vals), statistics.mean(vals), max(vals))

    return {
        "count": len(rows),
        "download": mm(series("download_mbps")),
        "upload": mm(series("upload_mbps")),
        "ping": mm(series("ping_ms")),
        "jitter": mm(series("jitter_ms")),
    }


# ---------------------------------------------------------------- chart

def make_chart(
    ping_rows: list[dict],
    speed_rows: list[dict],
    out_png: Path,
    since: int | None = None,
    until: int | None = None,
) -> bool:
    """3-panel chart: RTT per target, speedtests, loss % per 10-min bucket.

    `since`/`until` (epoch) pin every panel to the report window: sparse panels
    (a day with one speedtest) otherwise autoscale their date axis to years and
    print meaningless ticks.
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except Exception:  # pragma: no cover - matplotlib optional
        return False
    try:
        fig, axes = plt.subplots(3, 1, figsize=(13, 11), dpi=110)
        if since is None or until is None:
            points = [r["epoch"] for r in ping_rows] + [r["epoch"] for r in speed_rows]
            since = min(points) if points else None
            until = max(points) if points else None
        x_start = datetime.fromtimestamp(since) if since else None
        x_end = datetime.fromtimestamp(until) if until else None

        ax = axes[0]
        by_target: dict[str, list] = defaultdict(list)
        for row in ping_rows:
            if row.get("ok") and row.get("rtt_ms") is not None:
                by_target[row["target"]].append(
                    (datetime.fromtimestamp(row["epoch"]), float(row["rtt_ms"]))
                )
        for target, pts in sorted(by_target.items()):
            pts.sort(key=lambda x: x[0])
            if len(pts) > 4000:
                pts = pts[:: (len(pts) // 4000) + 1]
            ax.plot([p[0] for p in pts], [p[1] for p in pts], lw=0.7, label=target)
        ax.set_ylabel("RTT (ms)")
        ax.set_title("Latency per target")
        ax.legend(loc="upper right", fontsize=8)
        ax.grid(alpha=0.3)

        ax = axes[1]
        pts = []
        for row in speed_rows:
            if not row.get("ok"):
                continue
            pts.append(
                (datetime.fromtimestamp(row["epoch"]), row.get("download_mbps"), row.get("upload_mbps"))
            )
        if pts:
            pts.sort(key=lambda x: x[0])
            xs = [p[0] for p in pts]
            ax.plot(xs, [p[1] for p in pts], marker="o", ms=4, label="download")
            ax.plot(xs, [p[2] for p in pts], marker="s", ms=4, label="upload")
            ax.legend(fontsize=8)
        ax.set_ylabel("Mbps")
        ax.set_title("Speed tests")
        ax.set_ylim(bottom=0)
        ax.grid(alpha=0.3)

        ax = axes[2]
        buckets: dict[str, dict[int, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
        for row in ping_rows:
            bucket = row["epoch"] - (row["epoch"] % 600)
            cell = buckets[row["target"]][bucket]
            cell[1] += 1
            if row.get("ok"):
                cell[0] += 1
        for target, bmap in sorted(buckets.items()):
            items = sorted(bmap.items())
            ax.plot(
                [datetime.fromtimestamp(i[0]) for i in items],
                [100.0 * (1 - i[1][0] / i[1][1]) for i in items],
                marker=".", ms=3, lw=0.7, label=target,
            )
        ax.set_ylabel("Loss % (10-min)")
        ax.set_title("Packet loss")
        ax.set_ylim(bottom=0, top=max(1.0, _max_loss(buckets) * 1.2))
        ax.legend(fontsize=8)
        ax.grid(alpha=0.3)

        for axis in axes:
            if x_start and x_end and x_end > x_start:
                axis.set_xlim(x_start, x_end)
            axis.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
            axis.tick_params(axis="x", labelsize=7)
        fig.tight_layout()
        fig.savefig(out_png)
        plt.close(fig)
        return True
    except Exception as exc:  # pragma: no cover
        log.warning("chart generation failed: %s", exc)
        try:
            plt.close("all")
        except Exception:
            pass
        return False


def _max_loss(buckets) -> float:
    """Worst loss % across all targets/buckets (for a sensible y-axis top)."""
    worst = 0.0
    for bmap in buckets.values():
        for counts in bmap.values():
            total = counts[1]
            if total:
                worst = max(worst, 100.0 * (1 - counts[0] / total))
    return worst


# ---------------------------------------------------------------- report

def generate_report(db, day: str, out_dir: Path, settings) -> Path:
    """Generate md (± png) for one local calendar day (YYYYMMDD). Returns md path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    since, until = _day_bounds(day)
    ping_rows = db.ping_rows(since, until)
    speed_rows = [r for r in db.speedtests(since_epoch=since) if r["epoch"] < until]
    speed_rows = sorted(speed_rows, key=lambda r: r["epoch"])
    cfg = settings.data
    threshold = cfg.get("thresholds", {})
    interval = float(cfg.get("ping_interval_sec", 10) or 10)

    label = day
    lines: list[str] = []
    lines.append(f"# Network Quality Report — {day[:4]}-{day[4:6]}-{day[6:]}")
    lines.append("")
    lines.append(f"- Generated: {datetime.now().astimezone().isoformat(timespec='seconds')}")
    lines.append(f"- Targets: {', '.join(settings.effective_targets())}")
    lines.append(
        f"- Ping interval: {interval:g}s (timeout {settings.ping_timeout:g}s), "
        f"speedtests: {'enabled every ' + str(cfg.get('speedtest', {}).get('interval_min')) + ' min' if cfg.get('speedtest', {}).get('enabled') else 'disabled'}"
    )
    lines.append("")

    if not ping_rows and not speed_rows:
        lines.append("**No data available for this period.**")
        lines.append("")
        lines.append("netmon may not have been running, or the data was pruned.")
        Path(out_dir / f"report_{label}.md").write_text("\n".join(lines) + "\n")
        return out_dir / f"report_{label}.md"

    if ping_rows:
        first = min(r["epoch"] for r in ping_rows)
        last = max(r["epoch"] for r in ping_rows)
        n_targets = len({r["target"] for r in ping_rows})
        expected = int((last - first) / interval) + 1
        expected *= n_targets
        coverage = 100.0 * len(ping_rows) / expected if expected else None
        lines.append("## Coverage")
        lines.append("")
        lines.append(
            f"- Period: {datetime.fromtimestamp(first).astimezone():%Y-%m-%d %H:%M} → "
            f"{datetime.fromtimestamp(last).astimezone():%Y-%m-%d %H:%M}"
        )
        lines.append(
            f"- Samples: {len(ping_rows)} / {expected} expected "
            f"({_fmt(coverage, 1)}% — a gap means netmon was down or the host was unreachable)"
        )
        lines.append("")

        stats = per_target_stats(ping_rows)
        lines.append("## Ping results (per target)")
        lines.append("")
        lines.append("| target | probes | ok | loss % | min | avg | max | p95 | p99 | jitter |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for target, s in stats.items():
            lines.append(
                f"| {target} | {s['probes']} | {s['ok']} | {_fmt(s['loss_pct'], 2)} | "
                f"{_fmt(s['min'])} | {_fmt(s['avg'])} | {_fmt(s['max'])} | "
                f"{_fmt(s['p95'])} | {_fmt(s['p99'])} | {_fmt(s['jitter'])} |"
            )
        lines.append("")

        outages = find_outages(ping_rows, interval)
        if outages:
            shown = outages[:20]
            lines.append(f"## Outage events (≥3 consecutive timeouts; showing {len(shown)}/{len(outages)})")
            lines.append("")
            lines.append("| start | end | duration (s) | target |")
            lines.append("|---|---|---|---|")
            for o in shown:
                lines.append(
                    f"| {o['start']} | {o['end']} | {o['duration_s']:.0f} | {o['target']} |"
                )
            lines.append("")

    if speed_rows:
        lines.append("## Speed tests")
        lines.append("")
        ok_rows = [r for r in speed_rows if r.get("ok")]
        if ok_rows:
            lines.append("| ts | engine | server | ISP | external IP | ping ms | jitter ms | dl Mbps | ul Mbps | loss % |")
            lines.append("|---|---|---|---|---|---|---|---|---|---|")
            for r in ok_rows:
                lines.append(
                    f"| {r.get('ts', '')} | {r.get('engine', '')} | {r.get('server', '')} | "
                    f"{r.get('isp', '')} | {r.get('external_ip', '')} | {_fmt(r.get('ping_ms'))} | "
                    f"{_fmt(r.get('jitter_ms'))} | {_fmt(r.get('download_mbps'))} | "
                    f"{_fmt(r.get('upload_mbps'))} | {_fmt(r.get('loss_pct'))} |"
                )
            lines.append("")
        failed = [r for r in speed_rows if not r.get("ok")]
        if failed:
            lines.append(f"- {len(failed)} speedtest(s) failed: {failed[0].get('error') or 'unknown error'}")
            lines.append("")
        summary = speed_summary(speed_rows)
        if summary:
            lines.append("### Speed test summary (min / avg / max)")
            lines.append("")
            lines.append("| metric | min | avg | max |")
            lines.append("|---|---|---|---|")
            for name, vals in (
                ("download (Mbps)", summary["download"]),
                ("upload (Mbps)", summary["upload"]),
                ("ping (ms)", summary["ping"]),
                ("jitter (ms)", summary["jitter"]),
            ):
                lines.append(f"| {name} | {_fmt(vals[0])} | {_fmt(vals[1])} | {_fmt(vals[2])} |")
            lines.append("")

        external_ips = list(dict.fromkeys(r["external_ip"] for r in speed_rows if r.get("external_ip")))
        if external_ips:
            lines.append("## External IP (rebind check)")
            lines.append("")
            lines.append(f"- Distinct IPs seen: {len(external_ips)} — {', '.join(external_ips)}")
            lines.append("")

    if ping_rows and threshold:
        stats = per_target_stats(ping_rows)
        wan_stats = {t: s for t, s in stats.items() if t not in _lan_targets(settings)}
        wan_stats = wan_stats or stats
        worst_loss = max((s["loss_pct"] or 0.0) for s in wan_stats.values())
        avg_pings = [s["avg"] for s in wan_stats.values() if s["avg"] is not None]
        avg_ping = statistics.mean(avg_pings) if avg_pings else None
        summary = speed_summary(speed_rows)
        avg_dl = summary["download"][1] if summary else None
        avg_ul = summary["upload"][1] if summary else None

        lines.append("## Assessment (vs configured thresholds)")
        lines.append("")
        lines.append("| metric | value | threshold | verdict |")
        lines.append("|---|---|---|---|")
        verdicts: list[str] = []
        checks = [
            ("worst target loss %", worst_loss, threshold.get("loss_pct"), "low"),
            ("average ping (ms)", avg_ping, threshold.get("avg_ping_ms"), "low"),
            ("average download (Mbps)", avg_dl, threshold.get("min_download_mbps"), "high"),
            ("average upload (Mbps)", avg_ul, threshold.get("min_upload_mbps"), "high"),
        ]
        for name, value, thr, better in checks:
            if value is None or thr is None:
                verdicts.append("N/A")
                lines.append(f"| {name} | {_fmt(value)} | {_fmt(thr)} | N/A |")
            else:
                ok = value <= thr if better == "low" else value >= thr
                verdicts.append("PASS" if ok else "FAIL")
                lines.append(
                    f"| {name} | {_fmt(value)} | {_fmt(thr)} | {'PASS ✅' if ok else 'FAIL ❌'} |"
                )
        lines.append("")
        verdict = (
            "DEGRADED — one or more metrics missed their threshold"
            if "FAIL" in verdicts
            else "HEALTHY — all metrics within thresholds"
        )
        lines.append(f"**Overall verdict: {verdict}**")
        lines.append("")
        lines.append("> Mechanical check against the thresholds configured in the dashboard.")

    md = "\n".join(lines) + "\n"
    report_path = out_dir / f"report_{label}.md"
    report_path.write_text(md)
    (out_dir / "latest.md").write_text(md)

    png = out_dir / f"report_{label}.png"
    if make_chart(ping_rows, speed_rows, png, since=since, until=until):
        (out_dir / "latest.png").write_bytes(png.read_bytes())
    else:
        log.warning("chart generation failed for %s (md report still written)", label)
    return report_path


def _lan_targets(settings) -> set[str]:
    from .ping import detect_default_gateway

    if not settings.data.get("auto_add_gateway", True):
        return set()
    gateway = detect_default_gateway()
    return {gateway} if gateway else set()
