FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    NETMON_DATA_DIR=/data \
    NETMON_PORT=9120 \
    MPLBACKEND=Agg

# iputils-ping: the probe shells out to `ping` (no root needed for raw sockets,
# Debian's ping carries cap_net_raw). curl: Discord webhook POSTs (Python's TLS
# fingerprint gets 403'd by Cloudflare) + container healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends iputils-ping curl ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY netmon ./netmon
COPY pyproject.toml README.md ./

RUN mkdir -p /data/reports
VOLUME ["/data"]
EXPOSE 9120

HEALTHCHECK --interval=60s --timeout=10s --start-period=25s --retries=3 \
  CMD curl -fsS http://127.0.0.1:9120/api/health || exit 1

CMD ["python", "-m", "netmon.web"]
