# Enterprise Knowledge Base Agent Deployment Guide

This document provides detailed instructions for deploying the Enterprise KB Agent in a private environment.

## 🏗 System Architecture
- **API Layer**: FastAPI (Python)
- **Orchestration**: LangGraph (Stateful Agents)
- **Vector Database**: Milvus (Enterprise Vector Store)
- **LLM**: OpenAI-compatible API (vLLM/Ollama/DeepSeek)
- **Reranker**: BGE-Reranker (Cross-Encoder)

---

## 🛠 1. Infrastructure Deployment

### 1.0 Python Version Requirement
**CRITICAL**: This project requires **Python 3.11**. System Python 3.13/3.14 has no prebuilt `torch` wheels and installation will fail.

Recommended setup with Miniconda (use the absolute path if `conda` is not on PATH):
```bash
~/miniconda3/bin/conda create -n agent-rag python=3.11 -y
~/miniconda3/bin/conda init zsh   # run once, then restart terminal
conda activate agent-rag
python --version                  # MUST print Python 3.11.x
```

> China mirror: add `https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main` to `~/.condarc`.

### 1.1 Deploy Milvus (Vector DB)
Milvus is required for scalable vector storage and metadata filtering. The bundled `docker-compose.yml` is tuned for restricted networks and small hosts:

- Runs **etcd + milvus** only, with `common.storageType: local` (no MinIO image needed)
- Image overrides: `ETCD_IMAGE` / `MILVUS_IMAGE`
- Data persisted under `./volumes/{etcd,milvus}`

```bash
docker compose up -d
docker compose ps                        # both containers should be healthy
curl -f http://localhost:9091/healthz    # returns OK

export VECTOR_STORE_TYPE=milvus
python run_ingestion.py
python scripts/verify_retrieval.py
```

If image pulls fail:
```bash
docker pull docker.1ms.run/milvusdb/milvus:v2.5.4
docker tag docker.1ms.run/milvusdb/milvus:v2.5.4 milvusdb/milvus:v2.5.4
docker pull quay.io/coreos/etcd:v3.5.5
```
Or pass the mirror directly: `MILVUS_IMAGE=docker.1ms.run/milvusdb/milvus:v2.5.4 docker compose up -d`.
If pulls keep failing, set `docker.registry-mirrors` in `~/.colima/default/colima.yaml` (on this machine `docker.1ms.run` worked) and run `colima restart`.

To use MinIO/S3 instead, set `COMMON_STORAGETYPE=remote` and add a minio service.

### 1.2 Deploy LLM (Model Layer)
You can use vLLM or Ollama to host a private model.
**Example using Ollama:**
```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Pull and run a model (e.g., DeepSeek)
ollama run deepseek-v2
```
*Ensure the API is accessible at `http://localhost:11434/v1` (or your specific port).*

---

## 🐍 2. Application Setup

### 2.1 Environment Configuration
Update `src/config.py` with your actual infrastructure endpoints:
```python
LLM_API_BASE = "http://your-llm-ip:port/v1"
LLM_API_KEY = "your-key"
LLM_MODEL = "deepseek-chat"
```

### 2.2 Install Dependencies
Run inside the activated `agent-rag` (Python 3.11) environment, in stages to avoid dependency conflicts:

```bash
conda activate agent-rag

# 1. Core deps + Torch
pip install "pydantic>=2.0,<3.0" "typing-extensions>=4.8.0" torch -i https://pypi.tuna.tsinghua.edu.cn/simple

# 2. LangChain ecosystem and vector store drivers
pip install langchain langchain-community langchain-core langchain-openai langchain-milvus faiss-cpu -i https://pypi.tuna.tsinghua.edu.cn/simple

# 3. Remaining utilities
pip install fastapi uvicorn pydantic-settings requests PyPDF2 pypdf langchain-huggingface reportlab -i https://pypi.tuna.tsinghua.edu.cn/simple

# 4. Version alignment
#    - torch 2.2.2 is incompatible with the NumPy 2.x ABI -> numpy<2
#    - transformers 5.x requires torch>=2.6 (max torch on macOS x86 is 2.2.2) -> pin 4.49
pip install "numpy<2" "pandas<2.3" "transformers==4.49.0" "sentence-transformers==3.4.1" "huggingface-hub==0.36.0" -i https://pypi.tuna.tsinghua.edu.cn/simple
```

Verify:
```bash
python -c "import numpy, torch, faiss, langchain; print(numpy.__version__, torch.__version__)"
# Expected: 1.26.4 2.2.2 (no NumPy ABI warning)
```

### 2.3 Embedding Configuration (offline by default)
`src/config.py`:
- `EMBEDDING_BACKEND="local"` (default) uses a local `sentence-transformers` model. First run downloads ~100MB, then works fully offline.
- `EMBEDDING_BACKEND="openai"` calls the `/embeddings` endpoint at `LLM_API_BASE`.
- `LOCAL_EMBEDDING_MODEL` defaults to `BAAI/bge-small-zh-v1.5` (bilingual, 512-dim).

Downloads use `HF_ENDPOINT=https://hf-mirror.com` by default (set in `src/__init__.py`); override with an env var if needed.

> Only **chat generation** needs an LLM endpoint. Ingestion and retrieval checks run fully offline.

---

## 📂 3. Knowledge Ingestion (ETL)

Before starting the API, you must populate the database.

0. **Generate sample data (optional)**:
   A generator script is included for HR/Finance/Tech test PDFs:
   ```bash
   python scripts/generate_samples.py
   ```

1. **Organize Data**:
   Place your PDF documents in the following structure:
   ```text
   data/
   ├── HR/
   │   └── policy.pdf
   ├── Finance/
   │   └── budget_2024.pdf
   └── Tech/
       └── coding_standards.pdf
   ```

2. **Run Ingestion Script**:
   Create a `run_ingestion.py` file:
   ```python
   from src.etl import EnterpriseETL
   etl = EnterpriseETL()
   etl.process_department_folder("HR", "./data/HR")
   etl.process_department_folder("Finance", "./data/Finance")
   etl.process_department_folder("Tech", "./data/Tech")
   ```
   Execute it: `python run_ingestion.py`

---

## 🚀 4. Running the Agent

### 4.1 Start the API Server
```bash
uvicorn main:app --host 127.0.0.1 --port 8000
```

### 4.1.1 Web test console
Open `http://localhost:8000/ui/` after starting the API. Pick a department, chat multi-turn, and inspect per-answer timings (total / retrieval / LLM).

### 4.1.2 TLS edge (Caddy, internal CA)
The production compose publishes the app on `127.0.0.1` only and puts Caddy on `:443`, so credentials never travel over plaintext HTTP:
- `config/Caddyfile` uses `tls internal`: a single machine has no public domain, so Caddy signs its own certificate.
- The hostname comes from `KB_SITE_ADDRESS` (default `kb.internal`). Add a hosts entry on each client: `<server-ip> kb.internal`.
- Install the root certificate (host path `volumes-prod/caddy/data/caddy/pki/authorities/local/root.crt`) on clients to avoid warnings.
- Visit `https://kb.internal/ui/` and `https://kb.internal/admin`; verify with `curl -k https://kb.internal/info`.
- When configuring OIDC, `AUTH_OIDC_REDIRECT_URI` must be `https://kb.internal/auth/callback`.
- Host port 80 is not published (macOS ships Apache on `:80`); always use https.
- `/metrics` is answered with 404 at the edge; Prometheus scrapes `app:8000` over the Docker network. If LAN access is needed, use `METRICS_TOKEN` + the scrape `authorization` block (`monitoring/README.md`).
- Known limitation: Docker's port forwarding makes every client appear as the Docker gateway IP, so per-IP login limits and audit IPs share one bucket. Fine for a 2-4 person LAN; raise `AUTH_LOGIN_RATE_LIMIT` otherwise.
- `volumes-prod/caddy` is not in `make backup`: losing it just issues a new root certificate (clients re-trust once).

### 4.1.3 Boot recovery and the nightly backup (macOS)
Two launchd agents (`scripts/install_launchd.py`, installed under `~/Library/LaunchAgents`) run after login:
```bash
make agents-install     # boot recovery (com.agent-rag.boot-recover)
make agents-status      # is each agent loaded?
make agents-uninstall
```
- boot-recover: starts colima when Docker is down, waits for the daemon, runs `compose up -d` for the production and monitoring stacks, then waits for `/info` (log: `logs/boot-agent.log`).
- daily-backup: at 03:30 it silences `AgentRagHealthDown` for 15 minutes, pauses the stack, archives with `backup.py`, keeps `BACKUP_KEEP` (default 14) archives, copies to `OFFSITE_BACKUP_DIR` with a sha256 check, then restarts everything in a `finally` block (log: `logs/backup-agent.log`). Run it manually with `make daily-backup`; the downtime is about a minute.
- The prod compose pins Milvus, so the archive holds `volumes-prod/{milvus,etcd}` even when the host `.env` says `faiss`.
- LaunchAgents run after login: a reboot needs one login (a lid-open wake is not a login).

### 4.2 Verification
First run the **offline retrieval check** (no LLM needed; validates ingestion + department isolation):
```bash
python scripts/verify_retrieval.py
# Expected last line: RESULT: OK - isolation holds
```

Then test the full chat flow (requires a configured LLM endpoint):
```bash
python test_agent.py
```

---

## 📈 5. Production Optimization
- **Health checks**: use `GET /health` for liveness/readiness (`down` = 503 on an empty/unreachable index, `degraded` = 200 while ingesting/rebuilding). `/health?probe=1` also calls the LLM (cached 15s).
- **Metrics**: `GET /metrics` for Prometheus (requests, latency, retrieval/LLM time, index size, ingestion, rebuild state). Set `METRICS_TOKEN` to require a bearer token; per-process values.
- **Monitoring stack**: `docker-compose.monitoring.yml` adds Prometheus, blackbox_exporter and Grafana, joining the `agent-rag-prod` network (`make monitor-up` / `make monitor-down`). Grafana provisioning includes the "Agent RAG" dashboard (request/error rate, p95 latency, cache hit ratio, index chunks, readiness) and seven alert rules: scrape down, `/health` non-2xx, empty index, chat error rate > 5%, cache-hit drop, stuck rebuild, and stale backups (`agent_rag_backup_age_seconds` > 48h; the app sees `./backups` through a read-only mount). UIs bind to loopback; `make monitor-check` validates every config file offline. See `monitoring/README.md`.
- **Alert delivery check**: before pointing Alertmanager at a real channel, verify the route with a local sink — `nc -l 9999 &`, then `ALERTMANAGER_WEBHOOK_URL=http://host.docker.internal:9999/alert make monitor-config`, `docker restart agent-rag-alertmanager`, and POST a `severity: warning` alert to `localhost:9093/api/v2/alerts`; `"status":"firing"` should show up within `group_wait` (30s). Use `host.docker.internal`, not `172.x.0.1` (that is the VM bridge under colima). Resolved notifications are gated by `group_interval` (5m). Re-render without the variable to return to `noop`.
- **Sessions**: `POST /sessions` creates a conversation and `ChatRequest.session_id` makes the server own the transcript (the body `history` is ignored). Sessions are scoped to `user_id`; `/sessions` lists, reads and deletes them. They live in SQLite at `SESSIONS_DB_PATH` (default `./logs/sessions.db`, mounted and backed up in the production compose). Long chats are folded into a running summary once they exceed `2 × HISTORY_MAX_MESSAGES` (one extra LLM call per window, `SESSION_SUMMARY_*`). Set `SESSIONS_ENABLED=false` to keep the legacy client-history contract.
- **Hybrid retrieval**: `HYBRID_ENGINE=auto` (default) prefers Milvus server-side sparse BM25 on 2.5+ and falls back to in-process BM25 otherwise. Check `GET /info` → `hybrid.engine`.
- **Milvus 2.5+**: the compose default is `v2.5.4`. After upgrading, rebuild the collection with `python run_ingestion.py --reset` so the sparse field and BM25 index exist; back up `volumes/milvus` first (Milvus cannot downgrade).
- **Reranking**: off by default; cache `RERANK_MODEL` before enabling (CPU cost ≈+200ms for MiniLM, ≈+1.2s for BGE-base).
- **Scaling**: For high traffic, replace the `uvicorn` worker with `gunicorn`:
  `gunicorn -w 4 -k uvicorn.workers.UvicornWorker main:app`
- **Backup**: `make backup` archives `data/`, `config/` (accounts + ACL), `logs/` and the vector store with per-file checksums; `make restore ARCHIVE=...` verifies and swaps them back. Stop Milvus/etcd before backing up their volumes (`make milvus-down`) — a copy of a running RocksDB is not guaranteed to recover. See `AGENT_GUIDE.md` §12.7.

### 4.3 Cloud model (free GLM-4-Flash)
Configure `.env` (see `.env.example`):
```bash
LLM_API_BASE=https://open.bigmodel.cn/api/paas/v4/
LLM_API_KEY=your-key
LLM_MODEL=glm-4-flash
```
Verify with `python scripts/check_llm.py`; expect `LLM endpoint is ready`.

---

## 📚 6. Related docs
- `WORKLOG.md`: what changed in the 2026-10-29/30 iteration, verification matrix, remaining work
- `AGENT_GUIDE.md`: full Chinese guide (install, usage, FAQ, tuning)
- `OPTIMIZATION_PLAN.md`: prioritized backlog and progress log
