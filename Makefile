SHELL := /bin/bash
# Ubuntu 22.04's python3 is 3.10: make install PYTHON=python3.12
PYTHON ?= python3
PY    := backend/.venv/bin/python
PIP   := backend/.venv/bin/pip
PORT  ?= 8010
FRONTEND_PORT ?= 8080

.PHONY: help venv install dev backend frontend test test-backend test-frontend lint \
        lint-backend lint-frontend build serve analyse diagnose up down logs rules migrate \
        clean hooks

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

hooks: ## Install the commit-msg hook that strips AI attribution trailers
	chmod +x .githooks/commit-msg
	git config core.hooksPath .githooks

venv: ## Create the backend virtualenv
	$(PYTHON) -m venv backend/.venv
	$(PIP) install --upgrade pip

install: venv ## Install backend and frontend dependencies
	$(PIP) install -e "backend[dev]"
	cd frontend && npm install

dev: ## Run backend and frontend together
	@trap 'kill 0' EXIT; \
	$(MAKE) backend & \
	$(MAKE) frontend & \
	wait

backend: ## Run the backend with reload
	cd backend && .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port $(PORT) --reload

frontend: ## Run the Vite dev server
	cd frontend && RBA_PORT=$(PORT) npm run dev

test: test-backend test-frontend ## Run every test

test-backend: ## Run the backend test suite
	cd backend && .venv/bin/python -m pytest -q

test-frontend: ## Run the frontend test suite
	cd frontend && npm run test -- --run

lint: lint-backend lint-frontend ## Run every linter and type checker

lint-backend: ## ruff + mypy
	cd backend && .venv/bin/ruff check app tests
	cd backend && .venv/bin/ruff format --check app tests
	cd backend && .venv/bin/mypy

lint-frontend: ## eslint + tsc
	cd frontend && npm run lint
	cd frontend && npx tsc --noEmit

build: ## Build the frontend bundle
	cd frontend && npm run build

serve: ## Build the bundle and serve it on FRONTEND_PORT (8080) with the backend
	@if [ "$(PORT)" = 8001 ] || [ "$(FRONTEND_PORT)" = 8001 ]; then \
	  echo "Port 8001 belongs to the Metanalyser backend and is never used."; exit 1; fi
	$(MAKE) build
	@trap 'kill 0' EXIT; \
	(cd backend && .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port $(PORT)) & \
	(cd frontend && RBA_PORT=$(PORT) npm run preview -- --port $(FRONTEND_PORT) --host) & \
	wait

analyse: ## Analyse one channel headlessly: make analyse URL=... ARGS="--duration 5m --html out.html"
	@test -n "$(URL)" || { echo 'usage: make analyse URL=<playback url> [ARGS="--duration 5m --html out.html"]'; exit 2; }
	cd backend && .venv/bin/python -m app.cli analyse "$(URL)" $(ARGS)

diagnose: ## Say what segment sampling did for one channel: make diagnose URL=...
	@test -n "$(URL)" || { echo 'usage: make diagnose URL=<playback url>'; exit 2; }
	cd backend && .venv/bin/python -m app.cli diagnose "$(URL)" $(ARGS)

rules: ## Regenerate docs/RULES.md from the rule registry
	cd backend && .venv/bin/python -m app.cli rules --markdown > ../docs/RULES.md
	@echo "docs/RULES.md regenerated"

migrate: ## Bring the database schema up to this build
	cd backend && .venv/bin/python -m alembic upgrade head
	@echo "database schema is at head"

up: ## Start the stack with docker compose
	docker compose up -d --build

down: ## Stop the stack
	docker compose down

logs: ## Follow container logs
	docker compose logs -f

clean: ## Remove build and cache artefacts
	rm -rf backend/.pytest_cache backend/.mypy_cache backend/.ruff_cache frontend/dist
	find backend -name '__pycache__' -type d -prune -exec rm -rf {} +
