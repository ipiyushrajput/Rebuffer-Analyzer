SHELL := /bin/bash
PY    := backend/.venv/bin/python
PIP   := backend/.venv/bin/pip
PORT  ?= 8010

.PHONY: help venv install dev backend frontend test test-backend test-frontend lint \
        lint-backend lint-frontend build up down logs rules clean hooks

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

hooks: ## Install the commit-msg hook that strips AI attribution trailers
	chmod +x .githooks/commit-msg
	git config core.hooksPath .githooks

venv: ## Create the backend virtualenv
	python3 -m venv backend/.venv
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
	cd frontend && npm run dev

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

rules: ## Regenerate docs/RULES.md from the rule registry
	cd backend && .venv/bin/python -m app.cli rules --markdown > ../docs/RULES.md
	@echo "docs/RULES.md regenerated"

up: ## Start the stack with docker compose
	docker compose up -d --build

down: ## Stop the stack
	docker compose down

logs: ## Follow container logs
	docker compose logs -f

clean: ## Remove build and cache artefacts
	rm -rf backend/.pytest_cache backend/.mypy_cache backend/.ruff_cache frontend/dist
	find backend -name '__pycache__' -type d -prune -exec rm -rf {} +
