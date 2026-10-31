# Agent RAG — application image.
#
# Deliberately CPU-only: torch is installed from the CPU wheel index so the
# image does not pull ~2GB of CUDA libraries. Milvus/etcd run as separate
# containers (see docker-compose.yml); the app talks to them over the network.
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models/huggingface \
    HF_HUB_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false \
    VECTOR_STORE_TYPE=milvus \
    MILVUS_URI=http://milvus:19530

WORKDIR /app

# No apt packages: the healthcheck uses Python's stdlib, and every dependency
# ships a manylinux wheel, so the image builds without touching Debian mirrors
# (a common failure point in restricted networks).

# PyPI is slow or blocked in some networks; both indexes are overridable so
# CI/offline builds can point at a mirror.
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG TORCH_INDEX_URL=https://mirror.sjtu.edu.cn/pytorch-wheels/cpu

# CPU-only torch first: the pinned transformers/sentence-transformers stack
# expects torch 2.x and the default index would install the CUDA build.
COPY requirements.txt ./
RUN pip install --index-url "${TORCH_INDEX_URL}" "torch==2.2.2" \
 && sed '/^torch==/d' requirements.txt > requirements.runtime.txt \
 && pip install --index-url "${PIP_INDEX_URL}" -r requirements.runtime.txt \
 && rm requirements.runtime.txt

COPY main.py run_ingestion.py ./
COPY src ./src
COPY static ./static
COPY scripts ./scripts
COPY eval ./eval

# Embedding model is baked into the image so a cold start never downloads it.
# Offline mode is turned off for this step only (the runtime keeps it on), and
# the HF mirror is used by default for networks that cannot reach huggingface.co.
ARG EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
ARG HF_ENDPOINT=https://hf-mirror.com
RUN HF_HUB_OFFLINE=0 HF_ENDPOINT="${HF_ENDPOINT}" \
    python -c "from sentence_transformers import SentenceTransformer as S; S('${EMBEDDING_MODEL}')"

# Non-root runtime user; /app/data and /app/logs stay writable for ingestion.
RUN useradd --create-home --uid 10001 app \
 && mkdir -p /app/data /app/logs /models \
 && chown -R app:app /app /models
USER app

EXPOSE 8000

# Liveness only: /info answers as soon as the process serves requests. Use
# /health for readiness (it returns 503 when the index is empty or the store
# is unreachable), so a fresh deployment is not marked unhealthy before the
# first ingestion.
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/info', timeout=5).status == 200 else 1)"

# One worker only: FAISS writes, ingestion jobs, caches, rate limiting and job
# state are all process-local. Scale out by running more app replicas against
# Milvus instead (see DEPLOYMENT docs).
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
