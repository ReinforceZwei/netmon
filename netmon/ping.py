"""ICMP ping probes (one probe per target per interval).

Uses the system `ping` (iputils-ping) so no root is required and behaviour
matches the platform's networking stack exactly. Unchanged from the engine
proven on the 5G trial monitor.
"""
from __future__ import annotations

import re
import subprocess

_TIME_RE = re.compile(r"time=([0-9.]+)\s*ms")


def probe_once(target: str, timeout: float = 2.0, ping_bin: str = "ping") -> tuple[bool, float | None]:
    """Run a single ping probe. Returns (ok, rtt_ms).

    ok=False means timeout / unreachable / error. rtt_ms is None when not ok.
    """
    try:
        proc = subprocess.run(
            [ping_bin, "-n", "-c", "1", "-W", str(int(max(1, round(timeout)))), target],
            capture_output=True,
            text=True,
            timeout=timeout + 2,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False, None
    m = _TIME_RE.search(proc.stdout or "")
    if m:
        return True, float(m.group(1))
    return False, None


def detect_default_gateway() -> str | None:
    """Return the default gateway IP (LAN side of the router), if detectable."""
    try:
        out = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    parts = out.split()
    if "via" in parts:
        gw = parts[parts.index("via") + 1]
        if gw:
            return gw
    return None
