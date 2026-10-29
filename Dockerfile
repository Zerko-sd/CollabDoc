# Production image for the Collaborative Document Server (WBS 10.1).
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so a code change does not invalidate the install layer.
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install ".[postgres,kafka,s3,ai]" "uvicorn[standard]"

COPY server/ ./server/
COPY web/ ./web/

# The app runs unprivileged; nothing it does needs root.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2).status==200 else 1)"

# One worker per container: the room registry and its OT state are per-process,
# so horizontal scale comes from more containers plus a Kafka broadcaster
# (COLLABDOC_BROADCASTER), not from more workers sharing a socket.
CMD ["uvicorn", "app.main:app", "--app-dir", "server", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
