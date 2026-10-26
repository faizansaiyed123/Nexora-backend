# Nexora Backend

Nexora Backend is a FastAPI service for multi-tenant competitive price intelligence. It manages client organizations, tracked offerings, competitors, collection sources, offering matches, collection jobs, price observations, snapshots, website discovery, and competitive alerts. It provides the API used by the [Nexora frontend](https://github.com/faizansaiyed123/Nexora-Frontend).

## Key Features

- **Organization and access management:** Tenant-aware data models, user authentication, and role-based access for `SUPER_ADMIN`, `ORG_ADMIN`, `ANALYST`, and `VIEWER` roles.
- **Authentication and account security:** Registration, login, token handling, email verification, password recovery, password hashing, and configurable rate limiting.
- **Competitive catalog:** Manage tracked offerings, competitors, collection sources, and matches between offerings and competitor listings.
- **Website discovery:** Discover candidate offerings and source URLs, with optional Gemini-assisted discovery.
- **Price collection:** Retrieve and extract pricing, currency, availability, and additional attributes using HTTP-based collection and optional Playwright browser rendering.
- **Historical records:** Store collection jobs, price observations, and snapshots for competitive analysis.
- **Alerts:** Support competitive events and configurable alert workflows.
- **Background processing:** Celery task configuration for scheduled monitored-match refreshes and pending email-notification dispatch.
- **URL security:** Validate collection targets before making outbound requests to help prevent unsafe URL access.

## Architecture

Nexora uses the following components:

- **API:** FastAPI, with versioned routes under `/v1`.
- **Database:** PostgreSQL with asynchronous SQLAlchemy 2.x access.
- **Schema migrations:** Alembic.
- **Caching and task infrastructure:** Redis and Celery.
- **Collection and extraction:** HTTPX, BeautifulSoup, lxml, and Playwright.
- **Configuration:** Pydantic Settings and environment variables.
- **Deployment:** Docker, Docker Compose, and a Render Blueprint.

The data model represents client organizations and associates application records with their owning client. Role-aware authorization and tenant-scoped access checks are implemented in the application. The database schema is managed through Alembic migrations.

## Frontend

The companion frontend repository is available at:

[**Nexora Frontend**](https://github.com/faizansaiyed123/Nexora-Frontend)

## Quick Start

### Prerequisites

- Python 3.12
- Docker with Docker Compose
- `uv` for Python dependency and environment management

### Option 1: Run the full local stack with Docker Compose

Keep the frontend and backend repositories as sibling directories:

```text
projects/
├── Nexora-backend/
└── Nexora-Frontend/
```

From the backend repository root:

1. Copy `.env.example` to `.env`.
2. Generate a unique JWT secret:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

3. Set the generated value as `JWT_SECRET_KEY` in `.env`.
4. Start the stack:

   ```bash
   docker compose up --build
   ```

This Compose configuration includes PostgreSQL, Redis, the backend API, and the frontend development server.

### Option 2: Run the backend locally

1. Copy `.env.example` to `.env` and set a unique `JWT_SECRET_KEY`.
2. Start the required database and Redis services:

   ```bash
   docker compose up -d postgres redis
   ```

3. Install the locked Python dependencies:

   ```bash
   uv sync --frozen --python 3.12
   ```

4. Apply database migrations:

   ```bash
   uv run --python 3.12 alembic upgrade head
   ```

5. Start the API:

   ```bash
   uv run --python 3.12 uvicorn backend.main:app --host 0.0.0.0 --port 8000
   ```

On Windows PowerShell, use `Copy-Item .env.example .env` for the first step.

### Local endpoints

| Endpoint | Purpose |
|---|---|
| `http://localhost:8000/` | Root status response |
| `http://localhost:8000/v1/health` | API health check |
| `http://localhost:8000/docs` | Interactive API documentation |
| `http://localhost:8000/openapi.json` | OpenAPI schema |

## Environment Variables

The following variable names are defined in `.env.example`.

**Application and database**
- `ENVIRONMENT`
- `DEBUG`
- `DATABASE_URL`
- `REDIS_URL`

**Security and authentication**
- `JWT_SECRET_KEY`
- `ACCESS_TOKEN_EXPIRE_MINUTES`
- `REFRESH_TOKEN_EXPIRE_DAYS`
- `VERIFICATION_TOKEN_EXPIRE_HOURS`
- `PASSWORD_RESET_TOKEN_EXPIRE_MINUTES`
- `RATE_LIMIT_ENABLED`
- `BLOCK_DISPOSABLE_EMAILS`
- `EMAIL_VERIFICATION_BYPASS`

**Network and frontend**
- `ALLOWED_HOSTS`
- `CORS_ORIGINS`
- `FRONTEND_URL`

**Email**
- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `SMTP_FROM_EMAIL`
- `SMTP_FROM_NAME`

**Collection and discovery**
- `GEMINI_DISCOVERY_ENABLED`
- `GEMINI_API_KEY`
- `AUTO_RENDER_ENABLED`
- `RESIDENTIAL_PROXY_URL`
- `SCHEDULED_COLLECTION_INTERVAL_MINUTES`
- `JOB_LEASE_SECONDS`

See [DEPLOYMENT.md](DEPLOYMENT.md) for production configuration requirements and deployment-specific guidance. Never commit real credentials or application secrets.

## Running Tests

The test configuration is defined in `pyproject.toml`. The CI workflow runs tests against PostgreSQL and Redis after applying database migrations.

With the required services running and dependencies installed, execute:

```bash
uv run --python 3.12 alembic upgrade head
uv run --python 3.12 pytest -q
```

The API integration suite uses the configured database and Redis services. Use isolated development or test data when running integration tests.

## Project Structure

```text
.
├── src/
│   └── backend/
│       ├── api/
│       │   └── v1/          # Versioned API routes
│       ├── collectors/      # Collection and browser-rendering code
│       ├── core/            # Settings, security, dependencies, rate limiting
│       ├── db/              # Database base and session management
│       ├── models/          # SQLAlchemy models and domain enums
│       ├── services/        # Collection and application services
│       └── workers/         # Celery configuration and tasks
├── alembic/                 # Database migration environment and revisions
├── tests/                   # Automated tests and browser walkthrough
├── .github/
│   └── workflows/
│       └── ci.yml           # Backend CI workflow
├── Dockerfile
├── docker-compose.yml
├── render.yaml
├── alembic.ini
├── pyproject.toml
├── uv.lock
└── .env.example
```

## Project Resources

- [Frontend repository](https://github.com/faizansaiyed123/Nexora-Frontend)
- [Deployment guide](DEPLOYMENT.md)
- [Environment template](.env.example)
- [Docker Compose configuration](docker-compose.yml)
- [Render Blueprint](render.yaml)
- [Backend CI workflow](.github/workflows/ci.yml)
