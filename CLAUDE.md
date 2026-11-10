# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A self-hostable enterprise knowledge-base RAG service: FastAPI + LangGraph, department-scoped retrieval over FAISS or Milvus 2.5, mixed-format ingestion, citations down to page/slide/sheet, an admin console and a chat test UI. Answers are generated only from retrieved excerpts; identity and department filtering never come from the LLM. `AGENTS.md` is the longer-form collaboration guide (feature inventory, config reference); read it when you need more detail than this file gives.

## Environment and setup

- **Python 3.11 is mandatory** (system 3.13/3.14 have no torch wheels). Use the conda env: `conda activate agent-rag`, or invoke `~/miniconda3/envs/agent-rag/bin/python`.
- `make install` installs `requirements.txt` + `requirements-test.txt`. Pin order matters if installing manually: pydantic/typing-extensions → torch → langchain* → app deps.
- Copy `.env.example` to `.env` and set `LLM_API_BASE` / `LLM_API_KEY` / `LLM_MODEL` (any OpenAI-compatible endpoint). `.env` is gitignored. Ingestion and `scripts/verify_retrieval.py` work offline; only chat generation needs the LLM.
- `HF_ENDPOINT` defaults to the mirror and `src/__init__.py` caps HF metadata lookups at 3s. Set `HF_HUB_OFFLINE=1` once the embedding model is cached.

## Commands

```bash
make help                 # list all shortcuts
make data                 # generate data/Demo fixtures (all formats)
make ingest               # incremental ingestion of DATA_DIR
make run                  # uvicorn main:app --host 127.0.0.1 --port 8000
make reset-faiss          # full rebuild with a shadow index, FAISS
make reset-milvus         # full rebuild with a shadow index, Milvus
make milvus-up            # etcd + Milvus 2.5.4; waits for :9091/healthz
make milvus-down
make test                 # pytest -q (offline)
make lint                 # ruff check src tests main.py run_ingestion.py scripts
make cov                  # pytest with coverage
make doctor               # env/deps/store/LLM connectivity report
make backup               # archive data/config/logs/vector store, with checksums
make restore ARCHIVE=...  # verify an archive, then swap directories back
make eval                 # retrieval hit@k / MRR / leak check
make check                # lint + test
make release              # version/CHANGELOG/git-tag consistency + lint + tests
```

Single test / focused run:

```bash
pytest -q tests/test_api.py::test_chat_returns_answer_and_sources
pytest -q tests/test_api.py -k chat
```

Verification scripts (requirements vary per script):

```bash
python test_agent.py                              # needs API server + working LLM
python scripts/check_llm.py                       # /models + one completion
python scripts/verify_retrieval.py                # offline; asserts dept isolation
python scripts/eval_retrieval.py                  # offline; needs an ingested index
python scripts/check_hybrid.py                    # offline unit checks
VECTOR_STORE_TYPE=milvus python scripts/check_hybrid_milvus.py   # needs Milvus
python scripts/check_loaders.py                   # every format, no store needed
python scripts/check_admin.py                     # needs API server; upload -> ingest -> delete
python scripts/check_health.py [--probe]          # needs API server
python scripts/bench_retrieval.py [--no-llm]      # embedding vs FAISS vs Milvus vs LLM
```

Accounts / tokens: `python scripts/manage_users.py add|list|show|set|passwd|disable|enable|delete`, `python scripts/mint_token.py --user ... --dept ...`. OIDC: `python scripts/check_oidc.py --self-test` (offline) or `--issuer/--jwks`.

Containers:

```bash
docker build -t agent-rag:dev .                   # CPU-only torch, model baked in
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml exec app python run_ingestion.py
```

## Architecture

### Request path

`main.py` is only the HTTP adapter: routes, CORS mount, dependency wiring, static UI mount (`static/index.html` at `/ui/`, `static/admin.html` at `/admin`). **All chat behavior lives in `src/chat_service.py`** for both `/chat` and `/chat/stream`: identity resolution, rate limiting, maintenance gate, department validation, answer-cache lookup/store, graph invocation with a deadline, error classification, metrics, SSE framing. Change behavior there once; do not duplicate it in endpoints. `src/schemas.py` holds `ChatRequest`/`ChatResponse`; the 200-message history cap there is an abuse guard, while `HISTORY_MAX_MESSAGES` (applied by `trim_history`) is what the model actually sees.

Identity: with `AUTH_MODE=none`, `user_id`/`user_dept(s)` come from the body (checked against `CHAT_API_KEY` when set). With `AUTH_MODE=jwt`, `src/auth.py` makes the bearer token the only source of identity — body identity is discarded entirely, and the admin claim gates `/admin/api`. Local username/password accounts (`src/local_auth.py`) mint ordinary tokens and carry a `ver` claim so disable/password-change revokes outstanding tokens.

### Agent graph

`src/agent.py` is a LangGraph `StateGraph` (`AgentState` → `agent` ↔ `tools`). Retrieval is **deterministic inside `call_model`, not an LLM tool**: it calls `kb_engine.search` with departments from state. `_retrieval_query` prepends the previous question only for real follow-ups (`decide_followup`: pronouns always; demonstratives/other only above cosine thresholds), avoiding a rewrite LLM call and avoiding topic-switch pollution. Excerpts become a system message in `<document>` tags and are treated as untrusted data. The only tool is `create_support_ticket`, which takes identity from `InjectedState`, never from the prompt. If the LLM is unreachable after retrieval, `call_model` answers from the top excerpts and sets `degraded=true` + `degraded_reason=llm_unavailable`; degraded answers are never cached.

### Vector engine and retrieval

`src/engine.py` has two layers:

- `_LazyEngine` (`kb_engine`) builds the real `KnowledgeBaseEngine` on first use, so `import src.engine` stays fast. Use `get_engine()` when you need the object itself; call `reset_engine()` in tests after changing settings.
- `KnowledgeBaseEngine` picks `FAISSDriver` or `MilvusDriver`, both implementing `BaseVectorStore`. Keep backend-specific behavior inside the drivers; everything above sees one interface. `count()` is the write target (shadow collection during rebuild); `live_count()` is what queries and `/health` see.

`search()`: vector candidates + BM25 candidates → reciprocal-rank fusion (`src/lexical.py`) → optional Cross-Encoder rerank (`src/reranker.py`, off by default). `HYBRID_ENGINE=auto` uses Milvus server-side sparse BM25 when the server is ≥2.5 and the collection has the sparse field; otherwise in-process BM25. Pure capability/version logic lives in `src/hybrid.py`.

Department isolation: Milvus uses an `expr` filter (`dept == '...'` / `dept in [...]`), FAISS filters after retrieval. Values are validated and escaped in `src/validation.py`; invalid departments are a 400. Document-level ACL (`src/acl.py`, optional) runs after retrieval for both backends and **fails closed** when enabled but unusable. The answer cache is scoped by departments + ACL groups for the same reason.

Every engine write calls `_touch()`: bumps the version counter, drops BM25 indexes and the doc-summary cache, and clears the answer cache.

### Ingestion

`run_ingestion.py` (CLI) and `src/admin_api.py` (background job) both drive `src/etl.py`; parsing is in `src/loaders.py` and hashing in `src/hashing.py`.

- Files live at `DATA_DIR/<Department>/**/*`; supported: PDF/DOCX/PPTX/XLSX/CSV/MD/TXT/HTML/JSON. Legacy `.doc/.ppt/.xls` are skipped with a hint.
- Document identity is `sha1(dept|relative source path)`. Chunk metadata keys are fixed: `dept/source/page|slide|sheet/doc_id/doc_hash/kind/chunk_index/title` (sentinels when absent). **Every chunk must carry every key**, because the Milvus collection schema is derived from the first inserted batch; adding a metadata key requires a full `--reset`.
- Incremental: unchanged files skipped by content hash; changed files are parsed first (a parse failure must not delete the old copy), new chunks are written, then old versions deleted (crash-safe); files removed from disk are pruned unless `--no-prune`.
- XLSX/CSV are split into header-repeating row blocks (`XLSX_ROWS_PER_CHUNK`/`CSV_ROWS_PER_CHUNK`), so each block keeps column context.
- `--reset`/admin rebuild writes into a **shadow index** (Milvus: temp collection + alias swap; FAISS: temp dir + `os.replace`). Queries keep hitting the live index; maintenance is set only around the millisecond swap. An empty rebuild aborts and keeps the previous index.

### Operations

- `/health` is readiness: down (503) when the store is empty/unreachable, degraded (200) during ingestion/rebuild or low disk (`src/health.py`, `HEALTH_MIN_FREE_MB`). `/info` is liveness and also exposes backend/model/`hybrid.engine`/auth capabilities.
- `/metrics` is Prometheus (`src/metrics.py`); counters are per-process.
- Ingestion jobs persist to `JOBS_PATH` atomically (`src/jobs.py`); interrupted jobs are marked failed on startup. Admin actions go to `AUDIT_LOG_PATH` (`src/audit.py`).
- The admin API (`src/admin_api.py`) is loopback-only unless `ADMIN_TOKEN` is set; in jwt mode a non-admin token is rejected and foreign `Origin` headers are refused. `/admin/api/session` tells the console its auth mode and claims.
- One uvicorn worker only: FAISS writes, caches, rate limiting, jobs and rebuild state are process-local. Scale with replicas against Milvus, not workers.

## Invariants and gotchas

- **Tests must stay offline.** `tests/conftest.py` sets `EMBEDDING_BACKEND=fake`, `VECTOR_STORE_TYPE=faiss`, a temp DB/data dir and `HF_HUB_OFFLINE=1` before importing settings. Do not add fixtures that call the LLM or download models; CI installs only `requirements-test.txt` (no torch).
- Changing `VECTOR_STORE_TYPE`, the embedding model, or any chunk metadata key requires `python run_ingestion.py --reset`. FAISS and Milvus are independent stores; switching one to the other without re-ingestion looks like "retrieval is broken".
- Dependency pins are load-bearing: `numpy<2` (torch 2.2.2 ABI), `torch==2.2.2`, `transformers==4.49.0` (5.x needs torch≥2.6), `sentence-transformers==3.4.1`, `huggingface-hub==0.36.0`.
- Milvus must be ≥2.5 for server-side BM25; upgrading an older collection needs `--reset`. Both etcd and Milvus must be healthy and `curl -f localhost:9091/healthz` must pass before using the Milvus backend.
- A full disk breaks writes, not queries. Milvus caches the first ENOSPC in RocksDB, so after freeing space you must `docker restart milvus-standalone` — `df -h` will otherwise look fine while collection creation keeps failing.
- Keep the two engine access styles straight: `kb_engine` (lazy proxy, safe at import/module level) vs `get_engine()` (real object). LangGraph introspects `call_model`, so method access on the proxy must never trigger model loading.
- Never let the LLM decide permissions or scope: departments come from `AgentState` (`InjectedState`/`prepare`), cache keys include departments + groups, and degraded/cached answers must not leak across identities.
- `ruff`: line length 100, target py311, rules E/F/W/I/B (E501, B008, B904, B027 ignored deliberately).

## Documentation

`QUICKSTART.md` (5-minute setup + troubleshooting), `AGENT_GUIDE.md` (full Chinese guide), `DEPLOYMENT.md` / `DEPLOYMENT_CN.md` (deployment), `WORKLOG.md` (benchmarks and verification matrix), `OPTIMIZATION_PLAN.md` (§11 is the current backlog), `PRODUCTIZATION.md` (login/multi-tenant/billing roadmap).
