# Developer shortcuts. Requires the `agent-rag` conda env (Python 3.11).
PYTHON ?= $(shell command -v python3.11 2>/dev/null || echo python)
CONDA_PY := $(HOME)/miniconda3/envs/agent-rag/bin/python
ifeq ($(wildcard $(CONDA_PY)),$(CONDA_PY))
PYTHON := $(CONDA_PY)
endif

.PHONY: help install data ingest run test lint cov milvus-up milvus-down milvus-logs \
        reset-faiss reset-milvus doctor check eval clean release release-check \
        backup restore daily-backup boot-recover agents-install agents-uninstall \
        agents-status monitor-config monitor-up monitor-down monitor-logs monitor-check \
        license-init license-verify license-console package package-image \
        usage-report demo-up demo-seed demo-down demo-logs

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
	$(PYTHON) -m uvicorn main:app --host 127.0.0.1 --port 8000

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

backup: ## Archive data, config, logs and the vector store (checksummed)
	$(PYTHON) scripts/backup.py

daily-backup: ## Consistent backup + retention + offsite copy (pauses the prod stack)
	$(PYTHON) scripts/daily_backup.py

boot-recover: ## Start colima and the prod + monitoring stacks after a reboot
	$(PYTHON) scripts/boot_recover.py

agents-install: ## Install launchd agents (login boot recovery + 03:30 backup)
	$(PYTHON) scripts/install_launchd.py install

agents-status: ## Show whether the launchd agents are loaded
	$(PYTHON) scripts/install_launchd.py status

agents-uninstall: ## Remove the launchd agents
	$(PYTHON) scripts/install_launchd.py uninstall

restore: ## Restore an archive (make restore ARCHIVE=backups/<file>.tar.gz)
	@test -n "$(ARCHIVE)" || (echo "usage: make restore ARCHIVE=backups/<file>.tar.gz"; exit 1)
	$(PYTHON) scripts/restore.py "$(ARCHIVE)"

monitor-config: ## Render Alertmanager config from .env (secrets stay local)
	$(PYTHON) scripts/render_alertmanager.py

monitor-up: monitor-config ## Start Prometheus + Alertmanager + blackbox + Grafana (needs the prod stack)
	docker compose -f docker-compose.monitoring.yml up -d

monitor-down: ## Stop the monitoring stack (keeps metric data)
	docker compose -f docker-compose.monitoring.yml down

monitor-logs: ## Tail monitoring stack logs
	docker compose -f docker-compose.monitoring.yml logs -f --tail=100

monitor-check: ## Validate Prometheus/Grafana configs offline
	$(PYTHON) scripts/check_monitoring.py

check: lint test ## Lint + tests

eval: ## Retrieval evaluation (hit@k / MRR / leakage)
	$(PYTHON) scripts/eval_retrieval.py

license-init: ## Create the offline license signing key (keep it out of git)
	$(PYTHON) scripts/issue_license.py --init-keys

license-verify: ## Verify a license file (make license-verify LICENSE=config/license.json)
	@test -n "$(LICENSE)" || (echo "usage: make license-verify LICENSE=config/license.json"; exit 1)
	$(PYTHON) scripts/issue_license.py --verify "$(LICENSE)"

license-console: ## Local web UI to issue and verify licenses (vendor machine only)
	$(PYTHON) scripts/license_console.py

usage-report: ## Usage/value report (MONTH=2026-10 FORMAT=markdown; default last month)
	$(PYTHON) scripts/usage_report.py $(if $(MONTH),--month "$(MONTH)") $(if $(FORMAT),--format "$(FORMAT)")

demo-up: ## Start the public demo stack on 127.0.0.1:8080 (FAISS, no license)
	@test -d data/Demo || (echo "data/Demo missing: run 'make data' first"; exit 1)
	@test -f .env.demo || cp .env.demo.example .env.demo
	docker compose -f docker-compose.demo.yml up -d --build

demo-seed: ## Ingest data/Demo into the running demo stack
	docker compose -f docker-compose.demo.yml exec app python run_ingestion.py

demo-down: ## Stop the demo stack (keeps volumes-demo/)
	docker compose -f docker-compose.demo.yml down

demo-logs: ## Tail the demo stack logs
	docker compose -f docker-compose.demo.yml logs -f --tail=100

package: ## Build the private-deployment delivery bundle (dist/; optional LICENSE=path)
	$(PYTHON) scripts/package_release.py $(if $(LICENSE),--license "$(LICENSE)")

package-image: ## Delivery bundle that also docker-saves the app image
	$(PYTHON) scripts/package_release.py --with-image $(if $(LICENSE),--license "$(LICENSE)")

release-check: ## Verify version/CHANGELOG/compose agree (no tests)
	$(PYTHON) scripts/check_release.py --no-tests

release: ## Full release gate: consistency + lint + tests
	$(PYTHON) scripts/check_release.py

clean: ## Remove caches and coverage artefacts
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
