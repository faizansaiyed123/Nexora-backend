# Nexora deployment and local development

Nexora is a multi-tenant API and React workspace. A working deployment needs **PostgreSQL**, **Redis-compatible Key Value**, a correctly configured frontend origin, and SMTP credentials for email-based account flows. SQLite and an empty Redis URL are not supported production configurations.

## Run locally with Docker Compose

Keep the two repositories as sibling directories:

```text
projects/
  Nexora-backend/
  Nexora-Frontend/
```

From `Nexora-backend`:

1. Copy `.env.example` to `.env`.
2. Generate a unique development JWT secret with `python -c "import secrets; print(secrets.token_urlsafe(48))"` and set `JWT_SECRET_KEY` in `.env`.
3. Configure `SMTP_USERNAME`, `SMTP_PASSWORD`, and `SMTP_FROM_EMAIL` with a mailbox/provider that supports STARTTLS if you want to exercise registration, verification, password reset, and email notifications. Do not enable email verification bypass.
4. Start the stack:

```bash
docker compose up --build
```

Open the frontend at `http://localhost:5173`. The API is at `http://localhost:8000`; its health endpoint is `http://localhost:8000/v1/health`. PostgreSQL and Redis are bound to loopback ports 5432 and 6379 for local tools. Compose runs migrations when the backend container starts and persists local PostgreSQL/Redis data in named volumes.

The frontend container uses the Vite development server so the local HTTP API URL is permitted by the development build configuration. Production frontend builds must use HTTPS for `VITE_API_URL`.

To stop services without deleting development data, use `docker compose down`. **Do not use `docker compose down -v` unless you intend to delete the local database and Redis volumes.**

## Required production configuration

Configure the values in the hosting provider's environment-variable interface or synced Blueprint. Do not commit credentials into the repository.

| Variable | Requirement |
|---|---|
| `DATABASE_URL` | Managed PostgreSQL connection string. The API normalizes standard `postgres://` and `postgresql://` URLs to the async SQLAlchemy driver. |
| `REDIS_URL` | Required Redis-compatible connection string. Email verification/reset tokens and refresh-session rotation depend on Redis. An empty URL is not a supported production mode. |
| `JWT_SECRET_KEY` | Unique, random secret of at least 32 characters. Never reuse the local value. |
| `ENVIRONMENT` | Set to `production`. |
| `ALLOWED_HOSTS` | Explicit comma-separated hostnames for the API service; do not use `*` in production. |
| `CORS_ORIGINS` | Exact browser-facing frontend origin(s), comma separated. Use origins only, with no path or trailing slash. |
| `FRONTEND_URL` | Exact deployed frontend origin used to build verification and reset links in email. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL` | A reachable STARTTLS-capable SMTP sender and its credentials. Without these, account email workflows cannot complete. |
| `EMAIL_VERIFICATION_BYPASS` | Keep `false` in production. The bypass is for isolated CI only. |
| `VITE_API_URL` | Frontend build-time variable set to the API origin, for example `https://api.example.com`—**not** the `/v1` path. The frontend adds versioned paths itself. |

Also configure any optional AI discovery credential deliberately; do not enable an external provider by accident. SMTP credentials and application secrets belong in the host's secret/environment manager, not in `render.yaml`, examples, or logs.

## Render Blueprint

The repository's `render.yaml` defines the Nexora API, PostgreSQL connection, and health-check route. The API route is `/v1/health`; checking `/health` returns the wrong path.

The Blueprint now references the existing `nexora-redis` Key Value service and declares a generated JWT secret for new/missing variables. **Syncing a Blueprint is separate from pushing application code.** For an existing service, Render ignores `sync: false` variables during later Blueprint updates, so set/confirm `FRONTEND_URL`, `CORS_ORIGINS`, and SMTP credentials in the service's Environment page, then deploy the updated values. Confirm that the service's configured health check is `/v1/health` and that `GET /v1/health` reports both database and Redis as `ok`.

The current Blueprint keeps the Postgres resource on Render's Free plan for evaluation. Free database/Key Value resources are not a production durability plan; Render documents that paid Key Value plans support disk-backed persistence. Before storing real customer data, choose appropriate durable plans and backup/retention policies. A free database can expire, and in-memory Redis state can be lost on restart. Do not change plans without reviewing cost and migration requirements.

## Post-deploy smoke checks

Run these against the real deployed origin after the service has deployed with its current environment variables:

```bash
curl --fail --silent https://YOUR_API_HOST/v1/health
```

Expected response shape:

```json
{"status":"ok","checks":{"database":"ok","redis":"ok"}}
```

Then open the deployed frontend, register a separate test account, receive the actual verification email, verify it, sign in, create a test offering and competitor source, run a collection, and confirm the resulting observation and snapshot from the workspace. Use a disposable test tenant; do not seed fake records into real customer tenants. The CI walkthrough runs against disposable PostgreSQL/Redis and a local STARTTLS inbox, so its passing result does not certify an existing production deployment.
