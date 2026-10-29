# AGENTS.md

## Core Architecture
- **RAG Pipeline**: LangGraph $\rightarrow$ Milvus $\rightarrow$ Cross-Encoder Reranker. Answers return `sources` (`source`+`page`) built from retrieval metadata; excerpts are wrapped in `<document>` tags and treated as untrusted data.
- **Ingestion**: multi-format (`pdf/docx/pptx/xlsx/csv/md/txt/html/json`) via `src/loaders.py`; incremental — chunks carry `dept/source/page|slide|sheet/doc_id/doc_hash/kind/chunk_index`; unchanged files skipped by hash, changed ones replaced by `doc_id`, removed ones pruned (`--reset` rebuilds).
- **Citations**: answers include `sources` (`source` + `page`/`slide`/`sheet` + `kind`) from retrieval metadata.
- **Permission Model**: Department-based isolation implemented via Milvus `expr` metadata filtering (`dept == 'dept_name'`). Department names are validated/escaped in `src/validation.py` before being embedded in the expression; the API returns 400 for invalid values. The department is injected into tools from `AgentState` (`InjectedState`), never supplied by the LLM.
- **State Management**: Handled by LangGraph `AgentState`, tracking `user_id` and `user_dept` per session.

## Developer Commands
All commands assume the conda env `agent-rag` is active (`conda activate agent-rag`).
- **API Server**: `uvicorn main:app --host 0.0.0.0 --port 8000` (test UI at `http://localhost:8000/ui/`, `GET /info` exposes backend/model)
- **Data Ingestion**: `python run_ingestion.py` (Must be run before API to populate the vector store).
- **LLM Check**: `python scripts/check_llm.py` (probes `/models` + one chat completion before starting the API).
- **Offline Retrieval Check**: `python scripts/verify_retrieval.py` (no LLM needed; asserts dept isolation).
- **Loader Check**: `python scripts/check_loaders.py` (extracts every supported format in /tmp, no vector store needed).
- **Latency Benchmark**: `python scripts/bench_retrieval.py` (embedding vs FAISS vs Milvus vs LLM). `/chat` responses include `timings` (`retrieval_ms`, `llm_ms`, `llm_calls`, `overhead_ms`, `total_ms`).
- **Verification**: `python test_agent.py` (requires the API server + a working LLM endpoint).

## Critical Gotchas & Setup
- **Python**: MUST be 3.11. System Python is 3.13/3.14 and has no `torch` wheels. Use conda env `agent-rag` (`~/miniconda3/envs/agent-rag/bin/python`).
- **Install Order**: `pydantic`/`typing-extensions` $\rightarrow$ `torch` $\rightarrow$ `langchain*` $\rightarrow$ app deps (`requirements.txt` lists tested pins; loaders need `python-docx python-pptx openpyxl beautifulsoup4 chardet`). Pin `numpy<2`, `pandas<2.3`, `transformers==4.49.0`, `sentence-transformers==3.4.1`, `huggingface-hub==0.36.0` (torch 2.2.2 breaks with NumPy 2.x ABI; transformers 5.x needs torch>=2.6).
- **Startup**: HF metadata lookups can stall ~10s per launch; `src/__init__.py` caps them with `HF_HUB_ETAG_TIMEOUT=3`. Set `HF_HUB_OFFLINE=1` once the model is cached.
- **LLM Config**: `.env` (gitignored, see `.env.example`) overrides defaults. Current default is free GLM-4-Flash (`https://open.bigmodel.cn/api/paas/v4/`). Local Ollama (`qwen2.5:latest`) works but CPU inference is ~3 tok/s, so cloud is preferred for tests.
- **Retrieval Flow**: `call_model` does deterministic first-stage retrieval (`kb_engine.search(query, state["user_dept"])`) and injects excerpts as a system message; the LLM only formats the answer or triggers `create_support_ticket`.
- **Embeddings**: `EMBEDDING_BACKEND="local"` by default (sentence-transformers + `HF_ENDPOINT=https://hf-mirror.com`). Only chat generation needs `LLM_API_BASE`; ingestion and `scripts/verify_retrieval.py` work offline.
- **Vector Backend**: `src/config.py` `VECTOR_STORE_TYPE` switches `"faiss"` (default, local) vs `"milvus"` (Docker). Same interface in `src/engine.py`.
- **Infra**: `docker compose up -d` starts etcd + Milvus only (local storage, no MinIO). Both must be `healthy` and `curl -f localhost:9091/healthz` must pass before setting `VECTOR_STORE_TYPE=milvus`. If `milvusdb/milvus` pull fails, use `docker.1panel.live/milvusdb/milvus:v2.4.0` and `docker tag` it, or pass `MILVUS_IMAGE=...`. etcd pulls from `quay.io` directly.
- **Data Structure**: Input files must be organized in `data/{department_name}/**/*` (PDF/DOCX/PPTX/XLSX/CSV/MD/TXT/HTML/JSON; `data/Demo` ships fixtures for every format, regenerate with `scripts/generate_demo_files.py`) for the ETL pipeline to tag them correctly. Switching FAISS/Milvus or changing chunk metadata requires `python run_ingestion.py --reset`.

## Testing & Verification
- **Permissions**: Always test with at least two different `user_dept` values to verify data isolation.
- **Reranking**: The `EnterpriseReranker` uses a Cross-Encoder; ensure the model is cached to prevent startup latency.

## Documentation
- **`AGENT_GUIDE.md`**: full Chinese guide (tech stack, install, usage, FAQ, performance tuning).
- **`OPTIMIZATION_PLAN.md`**: prioritized TODO checklist (P0 security/stability → P1 architecture → P2 scale, plus docs tasks) with file:line refs and acceptance commands.
