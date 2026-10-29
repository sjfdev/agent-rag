# 企业知识库 Agent 部署指南

本文档提供了在私有化环境中部署企业级知识库 Agent 的详细步骤。

## 🏗 系统架构
- **API 层**: FastAPI (Python)
- **编排层**: LangGraph (有状态 Agent 状态机)
- **向量数据库**: Milvus (企业级向量存储)
- **大模型**: OpenAI 兼容接口 (vLLM / Ollama / DeepSeek)
- **重排序**: BGE-Reranker (Cross-Encoder)

---

## 🛠 1. 基础设施部署

### 1.0 Python 版本要求
**至关重要**: 本项目要求使用 **Python 3.11**。系统自带的 Python 3.13/3.14 没有 `torch` 预编译包，会安装失败。

推荐使用 Miniconda 创建独立环境（不要用系统 Python）：
```bash
# 用绝对路径调用 conda，避免 PATH 未初始化
~/miniconda3/bin/conda create -n agent-rag python=3.11 -y
~/miniconda3/bin/conda init zsh   # 只需执行一次，然后重启终端
conda activate agent-rag
python --version                  # 必须输出 Python 3.11.x
```

> 国内加速：在 `~/.condarc` 中配置清华源 `https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main`。

### 1.1 部署 Milvus (向量数据库)
Milvus 用于支持大规模向量存储和基于元数据的权限过滤。仓库内 `docker-compose.yml` 已针对国内网络与低资源环境调整：

- 只启动 **etcd + milvus**（用 `common.storageType: local` 本地存储，免去 MinIO 镜像拉取失败的问题）
- 镜像可覆盖：`ETCD_IMAGE` / `MILVUS_IMAGE`
- 数据持久化到 `./volumes/{etcd,milvus}`

```bash
# 启动（首次会自动拉镜像）
docker compose up -d
docker compose ps          # 两个容器都应是 healthy
curl -f http://localhost:9091/healthz   # 返回 OK

# 切到 Milvus 并入库
export VECTOR_STORE_TYPE=milvus
python run_ingestion.py
python scripts/verify_retrieval.py
```

镜像拉取失败时的替代方案（任选其一可用）：
```bash
# Milvus 官方镜像经 1panel 代理拉取后重命名
docker pull docker.1panel.live/milvusdb/milvus:v2.4.0
docker tag docker.1panel.live/milvusdb/milvus:v2.4.0 milvusdb/milvus:v2.4.0
# etcd 直接走 quay（本机实测可用）
docker pull quay.io/coreos/etcd:v3.5.5
```
也可用 `MILVUS_IMAGE=docker.1panel.live/milvusdb/milvus:v2.4.0 docker compose up -d` 直接指定镜像。

如果确实需要 MinIO/S3，可把 `COMMON_STORAGETYPE` 改回 `remote` 并补一个 minio 服务。

### 1.2 部署 LLM (模型层)
建议使用 vLLM 或 Ollama 部署私有化模型。
**以 Ollama 为例：**
```bash
# 安装 Ollama
curl -fsSL https://ollama.com/install.sh | sh

# 拉取并运行模型 (例如 DeepSeek)
ollama run deepseek-v2
```
*请确保 API 在 `http://localhost:11434/v1`（或您的指定端口）可访问。*

---

## 🐍 2. 应用环境配置

### 2.1 配置文件修改
修改 `src/config.py` 中的基础设施端点：
```python
LLM_API_BASE = "http://您的模型IP:端口/v1"
LLM_API_KEY = "您的密钥"
LLM_MODEL = "deepseek-chat"
```

### 2.2 安装 Python 依赖
必须在已激活的 `agent-rag` (Python 3.11) 环境中执行，并使用分步安装避免依赖冲突：

```bash
conda activate agent-rag

# 1. 核心依赖 + Torch
pip install "pydantic>=2.0,<3.0" "typing-extensions>=4.8.0" torch -i https://pypi.tuna.tsinghua.edu.cn/simple

# 2. LangChain 生态与向量库驱动
pip install langchain langchain-community langchain-core langchain-openai langchain-milvus faiss-cpu -i https://pypi.tuna.tsinghua.edu.cn/simple

# 3. 其余工具库
pip install fastapi uvicorn pydantic-settings requests PyPDF2 pypdf langchain-huggingface reportlab -i https://pypi.tuna.tsinghua.edu.cn/simple

# 4. 关键版本对齐
#    - torch 2.2.2 与 NumPy 2.x ABI 不兼容 -> numpy<2
#    - transformers 5.x 要求 torch>=2.6（macOS x86 上 torch 最高 2.2.2）-> 锁 4.49
pip install "numpy<2" "pandas<2.3" "transformers==4.49.0" "sentence-transformers==3.4.1" "huggingface-hub==0.36.0" -i https://pypi.tuna.tsinghua.edu.cn/simple
```

验证安装：
```bash
python -c "import numpy, torch, faiss, langchain; print(numpy.__version__, torch.__version__)"
# 期望输出: 1.26.4 2.2.2（无 NumPy ABI 警告）
```

### 2.3 Embedding 配置（默认离线，无需 API Key）
`src/config.py`：
- `EMBEDDING_BACKEND="local"`（默认）使用本地 `sentence-transformers` 模型，首次运行会下载模型（约 100MB），之后完全离线。
- `EMBEDDING_BACKEND="openai"` 则调用 `LLM_API_BASE` 的 `/embeddings` 接口。
- `LOCAL_EMBEDDING_MODEL` 默认 `BAAI/bge-small-zh-v1.5`（中英双语，512 维）。

国内下载已自动走 `HF_ENDPOINT=https://hf-mirror.com`（见 `src/__init__.py`），可用环境变量覆盖。

> 只有 **对话生成** 需要 LLM 端点；数据入库（ingestion）和检索验证可完全离线运行。



---

## 📂 3. 知识库数据入库 (ETL)

在启动 API 之前，必须将文档导入数据库。

0. **生成样本数据（可选）**:
   仓库内置生成脚本，可直接生成 HR/Finance/Tech 三个部门的测试 PDF：
   ```bash
   python scripts/generate_samples.py
   ```

1. **组织数据结构**:
   将 PDF 文档按部门放入对应文件夹：
   ```text
   data/
   ├── HR/
   │   └── 员工手册.pdf
   ├── Finance/
   │   └── 2024预算方案.pdf
   └── Tech/
       └── 代码规范.pdf
   ```

2. **运行入库脚本**:
   创建 `run_ingestion.py` 文件：
   ```python
   from src.etl import EnterpriseETL
   etl = EnterpriseETL()
   etl.process_department_folder("HR", "./data/HR")
   etl.process_department_folder("Finance", "./data/Finance")
   etl.process_department_folder("Tech", "./data/Tech")
   ```
   执行脚本：`python run_ingestion.py`

---

## 🚀 4. 运行与验证

### 4.1 启动 API 服务
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

### 4.1.1 可视化测试台
启动 API 后打开 `http://localhost:8000/ui/`，可选择部门、连续多轮提问，并查看每条回答的耗时（总计/检索/LLM）。

### 4.1.2 健康检查与探针
```bash
curl -s http://localhost:8000/health            # 轻量探针（不调用 LLM）
curl -s 'http://localhost:8000/health?probe=1'  # 额外探测 LLM，结果缓存 15s
python scripts/check_health.py --probe          # 冒烟测试
```
- `status=ok` → HTTP 200，可接收流量
- `status=degraded` → HTTP 200（入库中 / 重建中 / LLM 探测失败），可继续提供已有能力
- `status=down` → HTTP 503（索引为空或向量库不可达），负载均衡应摘除

K8s 示例：
```yaml
livenessProbe:
  httpGet: { path: /health, port: 8000 }
  initialDelaySeconds: 20
readinessProbe:
  httpGet: { path: /health, port: 8000 }
  periodSeconds: 10
```

### 4.1.3 Prometheus 监控
```bash
curl -s http://localhost:8000/metrics | grep '^agent_rag_'
```
- 指标覆盖：请求量/成功率、端到端与检索/LLM 耗时、首 token 延迟、索引块数、入库任务、重建状态
- 部门名不作为标签（避免敏感信息与高基数）
- 如需鉴权：`.env` 设置 `METRICS_TOKEN=xxx`，Prometheus 配置 `authorization: { credentials: xxx }`
- 多 worker/多副本时各进程指标独立，请在 Prometheus 侧按 instance 聚合

### 4.2 功能验证
先做**离线检索验证**（不需要 LLM，检查入库与部门隔离）：
```bash
python scripts/verify_retrieval.py
# 期望最后一行: RESULT: OK - isolation holds
```

再验证完整对话链路（需要已配置 LLM 端点）：
```bash
python test_agent.py
```

---

## 📈 5. 生产环境优化建议
- **重排序 (Reranking)**: 默认关闭；开启前先把模型缓存到本地（`RERANK_MODEL`）。CPU 上 BGE-base 约 +1.2s/次，MiniLM 约 +200ms，按数据量选择。
- **健康检查**: 用 `GET /health` 做存活/就绪探针；`/health?probe=1` 会真的调用一次 LLM，建议只在内网监控使用（结果缓存 15s）。
- **混合检索**: 默认 `HYBRID_ENGINE=auto`。Milvus 为 2.5+ 且集合含稀疏字段时自动使用服务端 BM25（推荐，多副本/多 worker 安全）；否则回退进程内 BM25（启动与入库后台预热，`HYBRID_MAX_DOCS` 限制语料）。FAISS 只能走进程内 BM25。
- **升级到 Milvus 2.5 后**: 旧集合没有稀疏字段，需 `python run_ingestion.py --reset` 重建；重建前先备份 `volumes/milvus`。可用 `GET /info` 的 `hybrid.engine` 确认，或运行 `python scripts/check_hybrid_milvus.py`。
- **性能扩展**: 对于高并发场景，建议使用 `gunicorn` 配合 `uvicorn` 工作进程：
  `gunicorn -w 4 -k uvicorn.workers.UvicornWorker main:app`
- **数据备份**: 定期备份 `docker-compose.yml` 中映射的 Milvus 数据卷。

### 4.3 云端模型（免费 GLM-4-Flash）
在 `.env` 中配置（见 `.env.example`）：
```bash
LLM_API_BASE=https://open.bigmodel.cn/api/paas/v4/
LLM_API_KEY=你的key
LLM_MODEL=glm-4-flash
```
验证：`python scripts/check_llm.py`，看到 `LLM endpoint is ready` 即可。

