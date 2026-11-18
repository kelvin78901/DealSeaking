# Open Box Monitor — long-running container (checks on a schedule + serves the page)
# Multi-arch base: works on x86_64 (Windows/Linux) and Apple Silicon (arm64).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=America/New_York

# tzdata so quiet_hours and timestamps follow TZ
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY obm ./obm
COPY config.example.yaml docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh \
 && useradd --uid 1000 --create-home obm \
 && mkdir -p /data && chown obm:obm /data

USER obm
VOLUME ["/data"]
EXPOSE 8787

# /healthz returns 503 if the check loop stalls → Docker marks the container unhealthy
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=4)" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
