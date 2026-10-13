# KaamSetu API, background worker and migration image (same image, different command).

# --- build: install hash-pinned dependencies into an isolated venv ---------
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

COPY requirements.lock /tmp/requirements.lock
# --require-hashes: every package (and its exact version) must match the lock.
RUN pip install --require-hashes --only-binary=:all: -r /tmp/requirements.lock

# --- runtime ----------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/srv/api \
    # Trust X-Forwarded-* only from these proxies; set to the load balancer's range.
    FORWARDED_ALLOW_IPS=127.0.0.1

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent app

COPY --from=build /opt/venv /opt/venv

WORKDIR /srv/api
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

USER app:app
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]

# Exec form: uvicorn is PID 1 and receives SIGTERM directly for graceful shutdown.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--no-server-header", "--timeout-graceful-shutdown", "20"]
