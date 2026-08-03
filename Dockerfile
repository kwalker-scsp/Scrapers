FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

COPY pyproject.toml ./
RUN pip install --no-cache-dir -e . && pip install --no-cache-dir "uvicorn[standard]>=0.30"

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY scripts ./scripts
COPY static ./static
COPY Russo-Ukrainian_War_Timeline_Dates.json ./

# SQLite lives on a mounted volume so the DB survives container replacement.
ENV DATABASE_URL=sqlite+pysqlite:////data/timeline.db
VOLUME ["/data"]

EXPOSE 8000

# Migrations are idempotent, so running them on boot is safe.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
