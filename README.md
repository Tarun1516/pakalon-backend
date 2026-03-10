# Pakalon Backend

FastAPI backend service for the Pakalon AI CLI — handles authentication, billing, usage tracking, and model catalog.

## Stack

| Component | Technology |
|-----------|-----------|
| Framework | FastAPI 0.115 |
| Database | PostgreSQL 16 (async via psycopg3) |
| ORM | SQLAlchemy 2.0 async |
| Migrations | Alembic |
| Cache / Rate-limit | Redis 7 |
| Auth | Clerk JWT (HS256, 90-day) |
| Billing | Polar SDK ($22/month) |
| Webhooks | Standard Webhooks (svix) |
| Email | Resend |
| Jobs | APScheduler 3 |
| Containerisation | Docker (multi-stage) |

---

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (`pip install uv`)
- Docker Desktop (for PostgreSQL & Redis)
- A [Clerk](https://clerk.com) account
- A [Polar](https://polar.sh) account
- A [Resend](https://resend.com) account

---

## Setup

### 1. Clone and install

```bash
cd pakalon-backend
uv sync
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your credentials (see Environment Variables below)
```

### 3. Start infrastructure

```bash
docker compose up -d
# Starts PostgreSQL on :5433 and Redis 7 on :6379
```

### 4. Run migrations

```bash
uv run alembic upgrade head
```

### 5. Start the server

```bash
# Development (auto-reload)
uv run uvicorn app.main:app --reload --port 8000

# Production
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
```

The API is now available at `http://localhost:8000`.  
Interactive docs: `http://localhost:8000/docs` (development only).

---

## Environment Variables

Copy `.env.example` and fill in all values:

| Variable | Description | Required |
|----------|-------------|---------|
| `DATABASE_URL` | PostgreSQL connection string | ✅ |
| `REDIS_URL` | Redis connection string | ✅ |
| `CLERK_SECRET_KEY` | Clerk backend secret | ✅ |
| `CLERK_PUBLISHABLE_KEY` | Clerk publishable key | ✅ |
| `CLERK_WEBHOOK_SECRET` | Clerk webhooks signing secret | ✅ |
| `POLAR_ACCESS_TOKEN` | Polar API access token | ✅ |
| `POLAR_WEBHOOK_SECRET` | Polar Standard Webhooks secret | ✅ |
| `POLAR_PRODUCT_ID` | Polar product ID for Pro plan | ✅ |
| `POLAR_PRODUCT_PRICE_ID` | Polar price ID ($22/month) | ✅ |
| `RESEND_API_KEY` | Resend email API key | ✅ |
| `JWT_SECRET` | HS256 JWT signing secret (min 32 chars) | ✅ |
| `OPENROUTER_MASTER_KEY` | OpenRouter API key for model cache | ✅ |
| `ENVIRONMENT` | `development` / `staging` / `production` | optional |
| `FRONTEND_URL` | Public URL of the web frontend | optional |

---

## API Routes Overview

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | — | Deep health check (DB + Redis) |
| POST | `/auth/devices` | — | Initiate device code flow |
| GET | `/auth/devices/{id}/token` | — | Poll for JWT |
| POST | `/auth/devices/{id}/confirm` | Clerk JWT | Approve device code |
| GET | `/auth/me` | Bearer JWT | Current user profile |
| PATCH | `/users/{id}` | Bearer JWT | Update display name |
| DELETE | `/users/{id}` | Bearer JWT | Delete account |
| GET | `/models` | Bearer JWT | List available AI models |
| GET | `/sessions` | Bearer JWT | List chat sessions |
| POST | `/sessions` | Bearer JWT | Create session |
| GET | `/usage` | Bearer JWT | Token usage stats |
| POST | `/billing/checkout` | Bearer JWT | Polar checkout URL |
| GET | `/billing/subscription` | Bearer JWT | Subscription status |
| DELETE | `/billing/cancel` | Bearer JWT (pro) | Cancel subscription |
| POST | `/webhooks/polar` | svix signature | Polar billing events |
| POST | `/webhooks/clerk` | svix signature | Clerk user events |

---

## Running Tests

```bash
# Unit + integration tests
uv run pytest -v

# With coverage
uv run pytest --cov=app --cov-report=html

# Specific file
uv run pytest tests/test_auth.py -v
```

Test infrastructure uses:
- `aiosqlite` in-memory SQLite (fast, no PostgreSQL required)  
- `fakeredis` for Redis mocking
- `httpx.AsyncClient` via ASGI transport

---

## Docker (Production)

```bash
# Build image
docker build -t pakalon-backend .

# Run with .env file
docker run -p 8000:8000 --env-file .env pakalon-backend
```

---

## Background Jobs

APScheduler runs two cron jobs in the same process:

| Job | Schedule | Description |
|-----|----------|-------------|
| `expiry_checker` | Daily at 00:30 UTC | Queues email reminders for trials/subscriptions expiring within 7 days |
| `email_queue` | Every 5 minutes | Processes pending email_queue rows via Resend |

---

## Deployment

1. Set `ENVIRONMENT=production` — disables Swagger UI, tightens CORS
2. Use the Docker image with at least 2 workers
3. Run behind nginx or a load balancer that terminates TLS
4. Ensure `ALLOWED_ORIGINS` in `.env` is set to `https://pakalon.com`
5. Run `alembic upgrade head` before deploying a new version
# pakalon-backend
