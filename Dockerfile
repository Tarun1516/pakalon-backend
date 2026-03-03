# ─────────────────────────────────────────────────────────────────────────────
# Pakalon Backend — Multi-stage Docker image (T160)
#
# Stage 1 (builder): installs uv + Python deps into /install
# Stage 2 (runtime): copies only the app + deps, runs as non-root
#
# Build:   docker build -t pakalon-backend .
# Run:     docker run -p 8000:8000 --env-file .env pakalon-backend
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1: dependency installer ────────────────────────────────────────────
FROM python:3.12-slim AS builder

# Install uv for fast dependency resolution
RUN pip install --no-cache-dir uv==0.5.4

WORKDIR /build

# Copy only dependency definitions first (better layer caching)
COPY pyproject.toml ./
COPY README.md ./

# Install all project deps into a separate prefix
RUN uv pip install --system --no-cache \
    fastapi>=0.115.0 \
    uvicorn[standard]>=0.34.0 \
    sqlalchemy[asyncio]>=2.0.0 \
    alembic>=1.14.0 \
    psycopg[async]>=3.2.0 \
    "redis[hiredis]>=5.2.0" \
    clerk-backend-api>=2.0.0 \
    polar-sdk>=0.14.0 \
    resend>=2.8.0 \
    apscheduler>=3.10.0 \
    pydantic-settings>=2.7.0 \
    pydantic>=2.10.0 \
    httpx>=0.28.0 \
    "python-jose[cryptography]>=3.3.0" \
    stdlibs \
    python-multipart>=0.0.20 \
    svix>=1.45.0 \
    aiofiles>=24.1.0 \
    structlog>=24.4.0 \
    --target /install

# ── Stage 2: minimal runtime image ───────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Security: run as non-root user
RUN groupadd --system pakalon && \
    useradd --system --gid pakalon --no-create-home pakalon

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local/lib/python3.12/site-packages

# Copy application source
COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini ./

# Set ownership to non-root user
RUN chown -R pakalon:pakalon /app

USER pakalon

# Runtime environment defaults (override via --env-file or -e flags)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ENVIRONMENT=production \
    HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

# Health check — uses the /health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Start uvicorn
CMD ["python", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--no-access-log"]
