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
docker pull docker.1panel.live/milvusdb/milvus:v2.4.0
docker tag docker.1panel.live/milvusdb/milvus:v2.4.0 milvusdb/milvus:v2.4.0
docker pull quay.io/coreos/etcd:v3.5.5
```
Or pass the mirror directly: `MILVUS_IMAGE=docker.1panel.live/milvusdb/milvus:v2.4.0 docker compose up -d`.

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
uvicorn main:app --host 0.0.0.0 --port 8000
```

### 4.1.1 Web test console
Open `http://localhost:8000/ui/` after starting the API. Pick a department, chat multi-turn, and inspect per-answer timings (total / retrieval / LLM).

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
- **Reranking**: Ensure the `BGE-Reranker` model is cached locally to avoid download delays on startup.
- **Scaling**: For high traffic, replace the `uvicorn` worker with `gunicorn`:
  `gunicorn -w 4 -k uvicorn.workers.UvicornWorker main:app`
- **Backup**: Regularly backup the Milvus data volume mapped in `docker-compose.yml`.

### 4.3 Cloud model (free GLM-4-Flash)
Configure `.env` (see `.env.example`):
```bash
LLM_API_BASE=https://open.bigmodel.cn/api/paas/v4/
LLM_API_KEY=your-key
LLM_MODEL=glm-4-flash
```
Verify with `python scripts/check_llm.py`; expect `LLM endpoint is ready`.

