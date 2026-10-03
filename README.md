# Nexora Backend

Nexora is a multi-tenant B2B competitive price-intelligence API. Organizations manage a private catalog, competitors, collection sources, offering matches, website discovery jobs, observations/snapshots, alerts, and account security.

## Stack

FastAPI, async SQLAlchemy, PostgreSQL, Redis, HTTPX/BeautifulSoup, Playwright, and optional Gemini-assisted discovery.

## Local run

1. Start PostgreSQL and Redis with `docker compose up -d postgres redis`.
2. Install the package: `pip install .`.
3. Set environment variables as needed (see `.env.example`). `JWT_SECRET_KEY` is required and must be a unique secret of at least 32 characters.
4. Apply the current schema with `alembic upgrade head`.
5. Start the API with `uvicorn backend.main:app --host 0.0.0.0 --port 8000`.

The API is mounted under `/v1`. The health endpoint is `GET /v1/health`.
