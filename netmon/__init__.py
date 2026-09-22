"""netmon — self-hosted home network quality monitor.

Probes latency (ICMP) to a configurable target list 24/7, runs optional
speedtests, stores everything in SQLite, renders a web dashboard with history
and sends a daily report to a Discord webhook.

Reuses the probe/report/notify engine proven on the 5G trial monitor
(github.com/ReinforceZwei/5g-network-test).
"""

__version__ = "1.0.1"
