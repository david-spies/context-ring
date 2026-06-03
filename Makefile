# ─── Context-Ring — Makefile ──────────────────────────────────────────────────
# Common developer and CI tasks.
# Usage: make <target>
# ─────────────────────────────────────────────────────────────────────────────

PYTHON      := python3
VENV        := .venv
PIP         := $(VENV)/bin/pip
PYTEST      := $(VENV)/bin/pytest
RUFF        := $(VENV)/bin/ruff
MYPY        := $(VENV)/bin/mypy
UVICORN     := $(VENV)/bin/uvicorn
LOCUST      := $(VENV)/bin/locust

IMAGE_NAME  := context-ring-proxy
IMAGE_TAG   := $(shell git rev-parse --short HEAD 2>/dev/null || echo "dev")

.DEFAULT_GOAL := help

# ─── Help ─────────────────────────────────────────────────────────────────────
.PHONY: help
help:
	@echo ""
	@echo "  Context-Ring — available targets"
	@echo "  ─────────────────────────────────────────────────────"
	@echo "  setup          Create venv and install all dependencies"
	@echo "  dev            Run proxy locally with hot-reload"
	@echo "  test           Run full test suite with coverage"
	@echo "  test-unit      Run unit tests only (fast)"
	@echo "  test-watch     Re-run tests on file changes"
	@echo "  lint           Run ruff linter"
	@echo "  format         Auto-format source with ruff"
	@echo "  typecheck      Run mypy static type checker"
	@echo "  check          lint + typecheck (pre-commit equivalent)"
	@echo "  docker-build   Build the production Docker image"
	@echo "  docker-run     Run the proxy in Docker (standalone)"
	@echo "  up             Start the full docker-compose stack"
	@echo "  down           Stop and remove docker-compose stack"
	@echo "  logs           Tail proxy logs from docker-compose"
	@echo "  load-test      Start Locust load test (opens browser)"
	@echo "  bench          Run the hash-ring benchmark script"
	@echo "  seed           Register default agent nodes via API"
	@echo "  status         Print ring status from running proxy"
	@echo "  clean          Remove venv, cache, and build artefacts"
	@echo ""

# ─── Setup ────────────────────────────────────────────────────────────────────
.PHONY: setup
setup:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements-dev.txt
	@echo ""
	@echo "  ✓  Virtual environment ready. Activate with:"
	@echo "     source $(VENV)/bin/activate"
	@echo ""

# ─── Development server ───────────────────────────────────────────────────────
.PHONY: dev
dev:
	@cp -n .env.example .env 2>/dev/null || true
	CONTEXT_RING_API_KEY=dev-key \
	INITIAL_AGENT_NODES=http://localhost:8001,http://localhost:8002 \
	LOG_LEVEL=DEBUG \
	$(UVICORN) src.main:app \
		--reload \
		--host 0.0.0.0 \
		--port 8000 \
		--log-level debug

# ─── Testing ──────────────────────────────────────────────────────────────────
.PHONY: test
test:
	CONTEXT_RING_API_KEY=test-secret-key \
	INITIAL_AGENT_NODES="" \
	LOG_LEVEL=WARNING \
	$(PYTEST) tests/ \
		--cov=src \
		--cov-report=term-missing \
		--cov-report=html:htmlcov \
		--cov-fail-under=80 \
		-v

.PHONY: test-unit
test-unit:
	$(PYTEST) tests/test_ring.py tests/test_security.py -v

.PHONY: test-watch
test-watch:
	$(VENV)/bin/ptw tests/ src/ -- -v --tb=short

# ─── Code quality ─────────────────────────────────────────────────────────────
.PHONY: lint
lint:
	$(RUFF) check src/ tests/

.PHONY: format
format:
	$(RUFF) format src/ tests/
	$(RUFF) check --fix src/ tests/

.PHONY: typecheck
typecheck:
	$(MYPY) src/

.PHONY: check
check: lint typecheck
	@echo "✓  All checks passed."

# ─── Docker ───────────────────────────────────────────────────────────────────
.PHONY: docker-build
docker-build:
	docker build \
		--tag $(IMAGE_NAME):$(IMAGE_TAG) \
		--tag $(IMAGE_NAME):latest \
		--file Dockerfile \
		.
	@echo "Built $(IMAGE_NAME):$(IMAGE_TAG)"

.PHONY: docker-run
docker-run:
	docker run --rm -it \
		-p 8000:8000 \
		-e CONTEXT_RING_API_KEY=dev-key \
		-e INITIAL_AGENT_NODES="" \
		-e LOG_LEVEL=INFO \
		$(IMAGE_NAME):latest

# ─── Docker Compose ───────────────────────────────────────────────────────────
.PHONY: up
up:
	docker compose up --build -d
	@echo ""
	@echo "  ✓  Stack started:"
	@echo "     Proxy   → http://localhost:8000"
	@echo "     Docs    → http://localhost:8000/docs"
	@echo "     Metrics → http://localhost:8000/metrics"
	@echo ""

.PHONY: up-monitoring
up-monitoring:
	docker compose --profile monitoring up --build -d
	@echo "  Prometheus → http://localhost:9090"

.PHONY: down
down:
	docker compose down -v

.PHONY: logs
logs:
	docker compose logs -f context-ring-proxy

# ─── Load testing ─────────────────────────────────────────────────────────────
.PHONY: load-test
load-test:
	$(LOCUST) -f scripts/load_test.py --host http://localhost:8000

.PHONY: load-test-headless
load-test-headless:
	$(LOCUST) -f scripts/load_test.py \
		--host http://localhost:8000 \
		--headless \
		-u 50 -r 10 \
		--run-time 60s

# ─── Benchmarking ─────────────────────────────────────────────────────────────
.PHONY: bench
bench:
	$(PYTHON) scripts/benchmark.py

# ─── Live API helpers ─────────────────────────────────────────────────────────
PROXY_URL    ?= http://localhost:8000
CONTEXT_RING_API_KEY ?= dev-key

.PHONY: seed
seed:
	@echo "Registering default agent nodes..."
	curl -sf -X POST $(PROXY_URL)/v1/register \
		-H "X-Api-Key: $(CONTEXT_RING_API_KEY)" \
		-H "Content-Type: application/json" \
		-d '{"agent_url": "http://agent-worker-1:8001"}' | python3 -m json.tool
	curl -sf -X POST $(PROXY_URL)/v1/register \
		-H "X-Api-Key: $(CONTEXT_RING_API_KEY)" \
		-H "Content-Type: application/json" \
		-d '{"agent_url": "http://agent-worker-2:8001"}' | python3 -m json.tool

.PHONY: status
status:
	@curl -sf $(PROXY_URL)/v1/ring/status | python3 -m json.tool

.PHONY: healthz
healthz:
	@curl -sf $(PROXY_URL)/healthz | python3 -m json.tool

# ─── Clean ────────────────────────────────────────────────────────────────────
.PHONY: clean
clean:
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	@echo "✓  Workspace cleaned."
