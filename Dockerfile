FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic

RUN pip install --no-cache-dir .

EXPOSE 8000

CMD ["uvicorn","backend.main:app","--host","0.0.0.0","--port","8000"]