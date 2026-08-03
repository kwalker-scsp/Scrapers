PY := ./.venv/bin/python
PIP := ./.venv/bin/pip

.PHONY: help venv install migrate seed reset dev test openapi docker-build docker-up

help:
	@echo "make install   - create .venv and install dependencies"
	@echo "make migrate   - run alembic migrations (creates the DB)"
	@echo "make seed      - load Russo-Ukrainian_War_Timeline_Dates.json as published events"
	@echo "make dev       - run the API + UI at http://127.0.0.1:8000"
	@echo "make test      - run the test suite"
	@echo "make reset     - DESTROY the local sqlite DB, re-migrate, re-seed"
	@echo "make openapi   - dump the OpenAPI schema to openapi.json (scraper contract)"

venv:
	test -d .venv || python3 -m venv .venv

install: venv
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -e ".[dev]"
	test -f .env || cp .env.example .env
	@echo "Installed. Now: make migrate && make seed && make dev"

migrate:
	mkdir -p data
	./.venv/bin/alembic upgrade head

seed:
	$(PY) -m scripts.seed Russo-Ukrainian_War_Timeline_Dates.json

reset:
	rm -f data/timeline.db
	$(MAKE) migrate
	$(MAKE) seed

dev:
	./.venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

test:
	./.venv/bin/pytest -q

openapi:
	$(PY) -c "import json;from app.main import app;print(json.dumps(app.openapi(),indent=2))" > openapi.json
	@echo "Wrote openapi.json"

docker-build:
	docker build -t ukr-timeline .

docker-up:
	docker compose up --build
