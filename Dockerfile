FROM python:3.12.15-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src UV_LINK_MODE=copy
WORKDIR /app

RUN pip install --no-cache-dir "uv==0.10.0"

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
COPY .env.example .env
COPY init_db.py ./
COPY render.yaml ./

RUN uv sync --frozen --no-dev --python 3.12
RUN .venv/bin/playwright install --with-deps chromium

RUN groupadd --system nexora && useradd --system --gid nexora --create-home --home-dir /home/nexora nexora
RUN chown -R nexora:nexora /app /home/nexora

USER nexora
EXPOSE 8000

CMD ["sh","-c","/app/.venv/bin/alembic upgrade head || true && /app/.venv/bin/python /app/init_db.py && /app/.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000"]
