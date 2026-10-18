# AEON in a box: a small, restart-safe image for running the trader 24/7 on your own VPS.
#
#   docker compose up -d --build        # the normal way (see docker-compose.yml and DOCKER.md)
#   docker compose run --rm aeon --check
#
# The journal (SQLite, plus its -wal/-shm files) is written to /data, so mount a volume there.
# config.toml is read from /app/config.toml (mount your own over it) and every secret comes from the
# environment (.env) — nothing sensitive is ever baked into the image.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC

WORKDIR /app

# Dependencies first, at the exact versions in requirements.lock (so a rebuild next month cannot silently pull
# different code into a trading process), then the code on top of them. Every package ships as a wheel: no compiler.
COPY requirements.lock ./
RUN pip install -r requirements.lock
COPY pyproject.toml ./
COPY aeon ./aeon
RUN pip install --no-deps . && rm -rf /app/aeon /app/pyproject.toml /app/build

# A sane default config; docker-compose mounts the real one over this (read-only).
COPY config.toml ./config.toml

# Unprivileged runtime user. /data is the only place the agent writes: it holds the journal.
RUN useradd --create-home --uid 1000 aeon \
 && mkdir -p /data \
 && chown -R aeon:aeon /data
USER aeon

# Run from /data so the default journal path (aeon-<mode>.db) lands in the volume, not in the image.
WORKDIR /data
VOLUME ["/data"]

# SIGINT is the shutdown path the agent has always handled (SIGTERM is handled too). 30s is enough to close clients
# and flush the last Discord alerts; open positions keep their exchange-side stops either way.
STOPSIGNAL SIGINT

# "Healthy" = the monitor loop has checked in within the last 3 minutes. Docker only REPORTS this; the agent's own
# watchdog (AEON_WATCHDOG_S) is what exits a wedged process so that `restart:` brings it back.
HEALTHCHECK --interval=60s --timeout=15s --start-period=180s --retries=3 \
  CMD ["python", "-m", "aeon", "--config", "/app/config.toml", "--healthcheck"]

# --config is pinned so the agent always reads the real config file; append flags to override the rest,
# e.g.  docker compose run --rm aeon --check   or   docker compose run --rm aeon --once
ENTRYPOINT ["python", "-m", "aeon", "--config", "/app/config.toml"]
CMD ["--headless"]
