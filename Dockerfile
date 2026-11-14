FROM python:3.13-slim

# git is required AT RUNTIME — /api/analyze shells out to `git clone`.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY engine/ ./engine/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8099

EXPOSE 8099

# Run as non-root.
RUN useradd -m -u 10001 foundry && chown -R foundry /app
USER foundry

# Workers: defaults to 1 (safe with the in-memory store). With DATABASE_URL set (Postgres),
# analyze/generate/download are multi-worker-safe — raise via WEB_CONCURRENCY. (Deep-report
# still reads the cloned repo on the node that handled analyze, so it stays best-effort across
# workers/nodes — see app/store.py.)
CMD ["sh", "-c", "python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers ${WEB_CONCURRENCY:-1}"]
