# Developer shortcuts. Requires the `agent-rag` conda env (Python 3.11).
PYTHON ?= $(shell command -v python3.11 2>/dev/null || echo python)
CONDA_PY := $(HOME)/miniconda3/envs/agent-rag/bin/python
ifeq ($(wildcard $(CONDA_PY)),$(CONDA_PY))
PYTHON := $(CONDA_PY)
endif

.PHONY: help install data ingest run test lint cov milvus-up milvus-down milvus-logs \
        reset-faiss reset-milvus doctor check eval clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Install runtime + test dependencies
	$(PYTHON) -m pip install -r requirements.txt -r requirements-test.txt

data: ## Generate the Demo dataset (all supported formats)
	$(PYTHON) scripts/generate_demo_files.py

ingest: ## Incremental ingestion of data/
	$(PYTHON) run_ingestion.py

run: ## Start the API server on :8000
	$(PYTHON) -m uvicorn main:app --host 0.0.0.0 --port 8000

test: ## Offline unit/API tests (no network)
	$(PYTHON) -m pytest -q

lint: ## Ruff lint
	$(PYTHON) -m ruff check src tests main.py run_ingestion.py scripts

cov: ## Tests with coverage report
	$(PYTHON) -m pytest -q --cov=src --cov-report=term-missing

milvus-up: ## Start etcd + Milvus (v2.5.4)
	docker compose up -d
	@echo "waiting for Milvus health..."
	@for i in $$(seq 1 60); do curl -sf localhost:9091/healthz >/dev/null && break; sleep 2; done
	@curl -sf localhost:9091/healthz >/dev/null && echo "Milvus is healthy" || echo "Milvus did not become healthy"

milvus-down: ## Stop etcd + Milvus (keeps volumes)
	docker compose down

milvus-logs: ## Tail Milvus logs
	docker compose logs -f --tail=100 standalone

reset-faiss: ## Rebuild the FAISS index from scratch
	VECTOR_STORE_TYPE=faiss $(PYTHON) run_ingestion.py --reset

reset-milvus: ## Rebuild the Milvus collection from scratch
	VECTOR_STORE_TYPE=milvus $(PYTHON) run_ingestion.py --reset

doctor: ## Environment / dependency / connectivity health check
	$(PYTHON) scripts/doctor.py

check: lint test ## Lint + tests

eval: ## Retrieval evaluation (hit@k / MRR / leakage)
	$(PYTHON) scripts/eval_retrieval.py

clean: ## Remove caches and coverage artefacts
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
