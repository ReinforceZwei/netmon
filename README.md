# netmon

Self-hosted **home network quality monitor** in one Docker container:

- **Continuous latency probes** (ICMP) to a configurable target list, every N seconds, 24/7
- **Packet loss** and **outage detection** per target (plus the LAN gateway, so LAN faults are separated from WAN faults)
- **Optional speedtests** — *off by default*, because a speedtest saturates the link for ~30 s. Run one from the dashboard ("Run speedtest now") or enable a schedule
- **Daily Discord report** — markdown summary + matplotlib chart (PNG), sent at a configurable local time, with catch-up for days missed while netmon was down
- **Web dashboard** for history browsing: latency / loss / throughput charts over 1 h → 1 y, per-target stats, outage list, event log
- **Everything configurable from the dashboard** — targets, intervals, speedtest policy, webhook, thresholds, alerts, retention; changes apply live, no restart
- **Instant Discord alert** when every monitored target stops responding (configurable runs + cooldown, with a recovery message)
- **SQLite storage, kept forever by default**, with an explicit prune tool (older than N days / wipe all) and CSV export from the Data page

Grew out of [5g-network-test](https://github.com/ReinforceZwei/5g-network-test) (the 7×24 ping+speedtest trial monitor for a 5G router) — the probe engine, report layout, Discord posting and unit-handling fixes are reused; storage moved from daily CSVs to SQLite and a FastAPI dashboard/settings layer was added.

## Quick start

```bash
git clone https://github.com/ReinforceZwei/netmon.git
cd netmon
docker compose up -d --build
```

Then open `http://<host>:9120/`, go to **Settings**, paste your Discord webhook URL and hit **Save settings**. Everything else has sane defaults.

> Speedtests are **disabled** by default. Enable "run speedtests on a schedule" in Settings, or press **Run speedtest now** on the dashboard when you want one.

### Compose

```yaml
services:
  netmon:
    build: .
    container_name: netmon
    restart: unless-stopped
    network_mode: host          # probes measure the host's real path
    environment:
      TZ: Asia/Taipei           # local time for the daily report + charts
      NETMON_DATA_DIR: /data
      NETMON_PORT: "9120"
    volumes:
      - ./data:/data
    cap_add:
      - NET_RAW                 # ICMP
```

**Why host networking:** a latency monitor must measure the *host's* path. On a bridge network the probes take an extra NAT hop, and gateway auto-detection would find the docker bridge (`172.17.0.1`) instead of your router, so LAN faults would look like WAN faults. With `network_mode: host` the container uses the host's routing and its real default gateway. (Bridge also works — drop `network_mode`, add a `ports: ["9120:9120"]` mapping, and put your router IP in the target list by hand.)

Either way, **do not** attach this container to a VPN container's network namespace (gluetun etc.): every probe would then measure the tunnel instead of your ISP link.

## Configuration

Everything below is editable in the dashboard and stored in SQLite (`settings` row). Environment variables only *seed* the first boot (they are ignored once settings exist):

| Env | Meaning |
|---|---|
| `NETMON_DATA_DIR` | data directory (default `/data`); SQLite + reports live here |
| `NETMON_PORT` | HTTP port (default `9120`) |
| `NETMON_TARGETS` | comma-separated seed target list |
| `NETMON_PING_INTERVAL_SEC` | seed ping interval |
| `NETMON_WEBHOOK_URL` | seed Discord webhook |
| `NETMON_SPEEDTEST_ENABLED` | `true` to enable scheduled speedtests on first boot |
| `NETMON_USER` / `NETMON_PASSWORD_HASH` | seed HTTP Basic auth (`hash_password` format) |
| `TZ` | container timezone (report time, chart axes, "today") |

Settings sections in the dashboard:

- **Probe** — target list (one per line), optional gateway probing, interval, timeout
- **Speedtests** — enable/disable, interval, timeout, Ookla binary name, "warn below N Mbps" alert floor
- **Daily Discord report** — enable, send time, catch-up toggle, webhook URL (write-only; stored on the host, shown back only as a hint)
- **Thresholds & alerts** — loss %, avg ping, min download/upload (used by the report verdict), plus the instant outage alert (consecutive failed rounds, cooldown)
- **Access** — optional HTTP Basic auth (recommended if the dashboard is reachable beyond your LAN/tailnet)

### Discord webhook

Discord → *Server Settings → Integrations → Webhooks → New Webhook* → copy URL → paste in Settings.

Not sure it works? **Settings → Send test message**, or **Dashboard → Send report now** to get a full report (markdown + chart PNG attached) immediately.

## How it works

```
netmon container
├── ping loop          one ICMP probe per target per interval  ─┐
├── speedtest worker   queue: manual button + optional schedule ─┤→ SQLite (WAL)
├── digest scheduler   daily report + catch-up for missed days  ─┘   /data/netmon.db
└── FastAPI app        dashboard, settings, data tools, JSON API
```

- Probes shell out to the system `ping` (iputils) — unprivileged, and matches the host's own networking stack exactly. A round fans out across a thread pool, so one dead target doesn't delay the others.
- Ping rows (`ts`, `epoch`, `target`, `ok`, `rtt_ms`) and speedtest rows are appended per round; the dashboard aggregates them in SQL (time-bucketed averages), so a year of history stays fast.
- The report is generated from SQLite for one local calendar day: coverage, per-target min/avg/max/p95/p99/jitter, outage events, speedtest table + min/avg/max, external-IP change check, and a verdict against your thresholds.
- Chart PNGs use a pinned x-axis for the report window (sparse panels otherwise autoscale to years and print meaningless ticks).
- Speedtest engines: Ookla CLI if a `speedtest` binary is on PATH, otherwise `speedtest-cli` (pip, included). Ookla's JSON reports bandwidth in **bytes/s** — converted with `×8/1e6`.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | live state: per-target RTT/loss, 24 h summary, counts, digest due days |
| GET | `/api/series?range=24h` (or `from`/`to` ISO) | bucketed latency/loss series, speedtests, hourly profile, events |
| GET | `/api/speedtests?limit=n`, `/api/events?limit=n` | tables for the UI |
| GET/POST | `/api/settings` | read (secrets redacted) / patch settings |
| POST | `/api/speedtest/run` | queue a speedtest now (409 if one is running) |
| POST | `/api/report/send` | build + send a report (`{"day":"YYYYMMDD"}` optional) |
| POST | `/api/webhook/test` | send a test message |
| POST | `/api/prune` | `{"days":N}` or `{"everything":true,"confirm":"DELETE"}` |
| GET | `/api/export.csv?table=ping\|speedtest\|events&days=N` | CSV export |
| GET | `/api/reports` | generated reports |
| GET | `/api/health` | container healthcheck |

## Data & retention

`./data/netmon.db` (SQLite, WAL) plus `./data/reports/report_YYYYMMDD.md|.png`. Nothing leaves the host except the Discord webhook payload.

Retention is **keep everything** by default. The **Data** page shows row counts + database size and offers:

- *prune older than N days* (immediate, `VACUUM` after)
- *delete all history* (typed confirmation) — for a fresh measurement window after an ISP/router change
- CSV export of ping samples, speedtests, events
- a browser for previously generated reports

Backup = copy `./data/`. Stop the container first (or accept a WAL-consistent copy of `netmon.db` plus `-wal`/`-shm`).

## Troubleshooting

- **All targets show timeout inside the container** — the container needs `NET_RAW` (compose default). Check with `docker compose exec netmon ping -c1 1.1.1.1`.
- **Speedtest fails with "no speedtest engine found"** — the image ships `speedtest-cli`; for higher accuracy with jitter/packet-loss, install the Ookla CLI in the image or mount the binary and set its name in Settings.
- **Speedtest measures the VPN** — the container is sharing a VPN container's network namespace. Move it to the default bridge.
- **No Discord message** — the webhook URL is empty or pre-dates the save; use **Settings → Send test message** to get the HTTP error verbatim. Discord's Cloudflare returns 403 for some TLS fingerprints, which is why the notifier shells out to `curl`.
- **Report time looks wrong** — set `TZ`; reports are cut on local calendar days.

## License

MIT — see [LICENSE](LICENSE).
