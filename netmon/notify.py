"""Discord webhook notifications (reused from the 5G monitor, UA updated).

Sending prefers the system `curl` binary: Discord's Cloudflare front door
blocks Python urllib's TLS fingerprint on many networks (HTTP 403 / error
1010), while curl passes. urllib remains as a last-resort fallback.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path

log = logging.getLogger("netmon.notify")

DISCORD_FILE_LIMIT = 8 * 1024 * 1024  # 8 MB per attachment
USER_AGENT = "netmon/1 (https://github.com/ReinforceZwei/netmon)"


class NotifyError(Exception):
    pass


def send_webhook(
    webhook_url: str,
    content: str,
    files: list[tuple[str, bytes, str]] | None = None,
    timeout: int = 30,
) -> dict | None:
    """Send a message (with optional attachments) to a Discord webhook."""
    if not webhook_url:
        raise NotifyError("no webhook url configured")
    curl = shutil.which("curl")
    if curl:
        return _send_via_curl(curl, webhook_url, content, files or [], timeout)
    return _send_via_urllib(webhook_url, content, files or [], timeout)


def _wait_url(webhook_url: str) -> str:
    return webhook_url + ("&" if "?" in webhook_url else "?") + "wait=true"


def _send_via_curl(
    curl_bin: str, webhook_url: str, content: str, files: list, timeout: int
) -> dict | None:
    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp(prefix="netmon-wh-")
        content_file = Path(tmpdir) / "content.txt"
        content_file.write_text(content, encoding="utf-8")
        args = [
            curl_bin, "-sS", "--max-time", str(timeout),
            "-H", f"User-Agent: {USER_AGENT}",
            "-F", f"content=<{content_file}",
        ]
        for i, (filename, data, ctype) in enumerate(files):
            if len(data) > DISCORD_FILE_LIMIT:
                raise NotifyError(
                    f"attachment {filename} exceeds Discord's "
                    f"{DISCORD_FILE_LIMIT // (1024 * 1024)} MB limit"
                )
            path = Path(tmpdir) / f"file{i}_{filename}"
            path.write_bytes(data)
            # Discord only honors the FIRST multipart field literally named
            # "file"; extra attachments need distinct names (file, file2, ...).
            field = "file" if i == 0 else f"file{i + 1}"
            args += ["-F", f"{field}=@{path};filename={filename};type={ctype}"]
        args += ["-w", "\n%{http_code}", _wait_url(webhook_url)]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout + 10)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    out = proc.stdout.strip()
    lines = out.rsplit("\n", 1)
    body = lines[0] if len(lines) > 1 else ""
    try:
        code = int(lines[-1]) if lines else 0
    except ValueError:
        code = 0
    if proc.returncode != 0 or not (200 <= code < 300):
        detail = body or proc.stderr.strip()
        raise NotifyError(
            f"webhook returned HTTP {code} (curl rc={proc.returncode}): {detail[:300]!r}"
        )
    if body:
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None
    return None


def _send_via_urllib(webhook_url: str, content: str, files: list, timeout: int) -> dict | None:
    if not content and not files:
        raise NotifyError("nothing to send")
    body, boundary = _multipart([("content", content)], files)
    req = urllib.request.Request(
        _wait_url(webhook_url),
        data=bytes(body),
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if not (200 <= resp.status < 300):
                raise NotifyError(f"webhook returned HTTP {resp.status}")
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        raise NotifyError(f"webhook returned HTTP {exc.code}: {exc.read()[:300]!r}") from exc
    except urllib.error.URLError as exc:
        raise NotifyError(f"webhook request failed: {exc.reason}") from exc
    if payload:
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None
    return None


def _multipart(
    fields: list[tuple[str, str]],
    files: list[tuple[str, bytes, str]],
) -> tuple[bytearray, str]:
    boundary = uuid.uuid4().hex
    body = bytearray()
    for name, value in fields:
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()
    for i, (filename, data, ctype) in enumerate(files):
        if len(data) > DISCORD_FILE_LIMIT:
            raise NotifyError(
                f"attachment {filename} exceeds Discord's "
                f"{DISCORD_FILE_LIMIT // (1024 * 1024)} MB limit"
            )
        field = "file" if i == 0 else f"file{i + 1}"
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode()
        body += data
        body += b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, boundary


# ---------------------------------------------------------------- summary

_RE_VERDICT = re.compile(r"\*\*Overall verdict:\s*([^*]+?)\*\*")
_RE_ASSESS = re.compile(
    r"^\|\s*(worst target loss %|average ping \(ms\)|avg ping \(ms\)|average download \(Mbps\)"
    r"|average upload \(Mbps\))\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|",
    re.M,
)
_RE_COVERAGE = re.compile(r"^- Samples: ([^\n]+)$", re.M)
_RE_SPEED_SUMMARY = re.compile(
    r"^\|\s*(download \(Mbps\)|upload \(Mbps\)|ping \(ms\)|jitter \(ms\))"
    r"\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|$",
    re.M,
)


def summarize_report(report_path: Path) -> str:
    """Compact markdown summary (<= 2000 chars, Discord message limit)."""
    md = report_path.read_text(encoding="utf-8", errors="replace")
    lines: list[str] = []
    m = _RE_VERDICT.search(md)
    if m:
        lines.append(f"**Verdict:** {m.group(1).strip()}")
        lines.append("")
    lines.append("**Assessment**")
    found = False
    for m in _RE_ASSESS.finditer(md):
        lines.append(
            f"• {m.group(1)}: {m.group(2).strip()} "
            f"(threshold {m.group(3).strip()}) — {m.group(4).strip()}"
        )
        found = True
    if not found:
        lines.append("• (no assessment — not enough data yet)")
    m = _RE_COVERAGE.search(md)
    if m:
        lines.append(f"• coverage: {m.group(1).strip()}")
    lines.append("")
    lines.append("**Speed test summary (min / avg / max)**")
    for m in _RE_SPEED_SUMMARY.finditer(md):
        lines.append(
            f"• {m.group(1)}: {m.group(2).strip()} / {m.group(3).strip()} / {m.group(4).strip()}"
        )
    return "\n".join(lines)[:2000]


def send_report(webhook_url: str, report_path: Path, header: str) -> dict | None:
    """Send one report: header + markdown summary text, .md + .png attached."""
    if not webhook_url:
        raise NotifyError("no webhook url configured")
    content = f"**{header}**\n\n{summarize_report(report_path)}"
    files: list[tuple[str, bytes, str]] = []
    if report_path.exists():
        files.append((report_path.name, report_path.read_bytes(), "text/markdown"))
    png = report_path.with_suffix(".png")
    if png.exists():
        files.append((png.name, png.read_bytes(), "image/png"))
    log.info("sending report to discord webhook: %s", header)
    msg = send_webhook(webhook_url, content, files)
    log.info("report sent to discord: %s", header)
    return msg
