FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    NETMON_DATA_DIR=/data \
    NETMON_PORT=9120 \
    MPLBACKEND=Agg

# iputils-ping: the probe shells out to `ping` (Debian's ping works unprivileged
# via net.ipv4.ping_group_range, which Docker sets). iproute2: `ip route` for
# default-gateway auto-detection. curl: Discord webhook POSTs (Python's TLS
# fingerprint gets 403'd by Cloudflare) + the container healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends iputils-ping iproute2 curl ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY netmon ./netmon
COPY pyproject.toml README.md ./

RUN mkdir -p /data/reports
VOLUME ["/data"]
EXPOSE 9120

# Build metadata, passed by the release workflow (harmless defaults for a local
# build). Kept at the end of the file so a new release only rebuilds these
# trivial layers instead of invalidating the dependency install.
ARG APP_VERSION=dev
ARG COMMIT=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="netmon" \
      org.opencontainers.image.description="Self-hosted home network quality monitor: latency probes, optional speedtests, daily Discord report, web dashboard" \
      org.opencontainers.image.url="https://github.com/ReinforceZwei/netmon" \
      org.opencontainers.image.source="https://github.com/ReinforceZwei/netmon" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${COMMIT}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.licenses="MIT"

ENV NETMON_BUILD_VERSION=${APP_VERSION} \
    NETMON_BUILD_COMMIT=${COMMIT} \
    NETMON_BUILD_DATE=${BUILD_DATE}

HEALTHCHECK --interval=60s --timeout=10s --start-period=25s --retries=3 \
  CMD curl -fsS http://127.0.0.1:9120/api/health || exit 1

CMD ["python", "-m", "netmon.web"]
