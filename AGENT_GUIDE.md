# 企业知识库 Agent 说明文档

面向研发与运维的完整说明：技术栈、架构、安装、使用、常见问题与性能优化。

---

## 1. 项目概述

一个可私有化部署的企业知识库问答 Agent，核心能力：

- **RAG 检索问答**：PDF 文档 → 切分 → 向量化 → 检索 → 交给 LLM 组织答案
- **部门级权限隔离**：每个 chunk 带 `dept` 元数据，检索时强制按部门过滤，跨部门数据不可见
- **多轮对话**：客户端回传 `history`，服务端按会话还原上下文
- **工具调用**：知识库无解时自动创建工单（`create_support_ticket`）
- **双向量后端**：本地 FAISS（零依赖）与 Milvus（企业级）随时切换
- **双 Embedding 后端**：本地 sentence-transformers（离线）或 OpenAI 兼容接口
- **内置测试台**：`/ui/` 网页端可直接按部门提问并查看分段耗时

### 1.1 整体架构

```
                 ┌──────────────────────────────┐
   PDF 文件        │  src/etl.py                  │
 data/<dept>/*.pdf│  PyPDFLoader → 切分 800/100   │
                 │  metadata = {dept, source}   │
                 └──────────────┬───────────────┘
                                │ add_documents()
                    ┌───────────▼────────────┐
                    │ src/engine.py           │
                    │  VECTOR_STORE_TYPE:     │
                    │   faiss  → ./db/vector_db│
                    │   milvus → 19530        │
                    │  EMBEDDING_BACKEND:     │
                    │   local / openai        │
                    └───────────┬────────────┘
                                │ search(query, dept)
                 ┌──────────────▼───────────────────────┐
  HTTP /chat     │ src/agent.py (LangGraph)             │
 ───────────────►│   call_model:                        │
                 │     1. 按 state["user_dept"] 检索     │
                 │     2. 片段注入 system message        │
                 │     3. LLM 生成 or tool_calls         │
                 │   tools: search / create_ticket       │
                 └──────────────┬───────────────────────┘
                                │
                    ┌───────────▼────────────┐
                    │ main.py (FastAPI)      │
                    │  /chat /info /ui/      │
                    └────────────────────────┘
```

关键设计：**权限来自会话状态，不来自模型**。`user_dept` 由 HTTP 请求传入 `AgentState`，检索与工具（`InjectedState`）都直接读取该值，LLM 无法通过参数伪造部门。

---

## 2. 技术栈

| 层次 | 选型 | 版本（本机验证） | 说明 |
|---|---|---|---|
| 语言 | Python | 3.11.13 | **必须 3.11**，3.13/3.14 无 torch wheel |
| Web | FastAPI + Uvicorn | 0.162.0 / 0.54.0 | 同步路由跑在线程池，不阻塞事件循环 |
| Agent 编排 | LangGraph | 1.2.18 | `StateGraph` + `ToolNode` + 条件边 |
| LLM 框架 | LangChain | 1.4.7 | 注意 1.x 有破坏性 API 变更 |
| LLM 客户端 | langchain-openai | 1.7.3 | 任意 OpenAI 兼容端点 |
| 向量库 | FAISS / Milvus | faiss-cpu 1.15.1 / pymilvus 3.0.2 | FAISS 默认；Milvus 2.4.0 容器 |
| Milvus 适配 | langchain-milvus | 0.4.0 | 支持 `expr` 元数据过滤 |
| Embedding | sentence-transformers | 3.4.1 | 本地 `BAAI/bge-small-zh-v1.5`（512 维） |
| 推理底座 | transformers / torch | 4.49.0 / 2.2.2 | 版本强绑定，见 FAQ |
| 数值 | numpy / pandas | 1.26.4 / 2.2.3 | 必须 `numpy<2` |
| PDF | pypdf / PyPDF2 / reportlab | 6.21.3 / - / 5.1.0 | 解析与样例生成 |
| 配置 | pydantic-settings | 2.14.1 | `.env` + 环境变量 |
| 容器 | Docker Compose | 28.4.0 | etcd + Milvus（本地存储，无 MinIO） |

默认 LLM：**智谱 GLM-4-Flash**（免费、国内直连、支持工具调用）。

---

## 3. 目录结构

```
agent-rag/
├── main.py                  # FastAPI 入口：/chat /info /ui /docs
├── run_ingestion.py         # 遍历 data/* 部门目录批量入库
├── test_agent.py            # 7 个端到端场景（需服务已启动）
├── docker-compose.yml       # etcd + Milvus（storageType=local）
├── .env / .env.example      # 运行配置（.env 已 gitignore）
├── static/index.html        # 网页测试台
├── data/<dept>/*.pdf        # 按部门组织的原始文档
│   ├── HR/ Finance/ Tech/
├── db/vector_db/            # FAISS 索引与元数据
├── volumes/                 # Milvus/etcd 数据卷
├── scripts/
│   ├── generate_samples.py  # 生成 8 份样例 PDF
│   ├── check_llm.py         # 探测 LLM 端点
│   ├── verify_retrieval.py  # 离线验证入库 + 部门隔离
│   └── bench_retrieval.py   # 延迟基准（embedding/FAISS/Milvus/LLM）
└── src/
    ├── config.py            # 所有开关（pydantic-settings）
    ├── engine.py            # Embedding 工厂 + FAISS/Milvus 驱动 + 单例
    ├── etl.py               # PDF 加载、切分、打 dept 标签
    ├── agent.py             # LangGraph 状态机与检索注入
    ├── tools.py             # search_knowledge_base / create_support_ticket
    └── reranker.py          # CrossEncoder 重排（可选，默认未接入）
```

---

## 4. 安装说明

### 4.1 前置条件

- macOS / Linux，x86_64 或 arm64
- conda（推荐 Miniconda）
- Docker Desktop（仅 Milvus 模式需要）
- 一个 OpenAI 兼容的 LLM 端点（云端免费 GLM-4-Flash 或本地 Ollama/vLLM）

### 4.2 创建环境

```bash
conda create -n agent-rag python=3.11 -y
conda activate agent-rag
```

国内建议先配清华源（`~/.condarc`）：

```yaml
channels:
  - defaults
show_channel_urls: true
default_channels:
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/r
custom_channels:
  conda-forge: https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud
```

### 4.3 安装依赖（顺序与版本都很关键）

```bash
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
  "pydantic>=2" "typing-extensions>=4.8"

# Intel macOS 上 torch 最高 2.2.2；Linux 可按需装 CPU/GPU 版
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple torch==2.2.2

pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
  "numpy<2" "pandas<2.3" \
  "langchain" "langgraph" "langchain-openai" "langchain-milvus" \
  "langchain-huggingface" faiss-cpu fastapi uvicorn pydantic-settings \
  requests pypdf PyPDF2 reportlab

# 锁定与 torch 2.2.2 兼容的推理栈
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
  "transformers==4.49.0" "sentence-transformers==3.4.1" "huggingface-hub==0.36.0"
```

> 安装完成后务必验证：`python -c "import torch, numpy, faiss, langchain; print(torch.__version__, numpy.__version__)"`，应为 `2.2.2 1.26.4` 且无 NumPy ABI 警告。

### 4.4 配置 `.env`

```bash
cp .env.example .env
```

最小配置（云端免费模型）：

```ini
LLM_API_BASE=https://open.bigmodel.cn/api/paas/v4/
LLM_API_KEY=你的key
LLM_MODEL=glm-4-flash

VECTOR_STORE_TYPE=faiss
EMBEDDING_BACKEND=local
LOCAL_EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
```

本地 Ollama 写法：

```ini
LLM_API_BASE=http://localhost:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=qwen2.5:latest
```

### 4.5 准备数据并入库

```bash
python scripts/generate_samples.py   # 可选：生成 8 份样例 PDF
python run_ingestion.py              # 读取 data/<dept>/*.pdf
python scripts/verify_retrieval.py   # 离线校验（不需要 LLM）
```

输出示例：

```
Vector store: faiss
Successfully ingested 5 chunks for Finance.
Successfully ingested 4 chunks for HR.
Successfully ingested 6 chunks for Tech.
RESULT: OK - isolation holds
```

### 4.6 启动 Milvus（可选）

```bash
docker compose up -d
docker compose ps                     # 两个容器都应为 healthy
curl -f http://localhost:9091/healthz
```

Milvus 使用 `common.storageType: local`，只需 etcd + milvus 两个容器，无需 MinIO。

```bash
VECTOR_STORE_TYPE=milvus python run_ingestion.py
VECTOR_STORE_TYPE=milvus python scripts/verify_retrieval.py
```

---

## 5. 使用说明

### 5.1 启动服务

```bash
conda activate agent-rag
cd /Users/songjiangfeng/work/ai/agent-rag
uvicorn main:app --host 0.0.0.0 --port 8000
# 或指定后端：VECTOR_STORE_TYPE=milvus uvicorn main:app --port 8000
```

| 地址 | 用途 |
|---|---|
| `http://localhost:8000/ui/` | 网页测试台 |
| `http://localhost:8000/docs` | Swagger |
| `GET /info` | 当前向量库 / 模型 / embedding 后端 |
| `POST /chat` | 对话接口 |

### 5.2 `/chat` 接口

请求：

```json
{
  "user_id": "u1",
  "user_dept": "Tech",
  "message": "What are the code review requirements?",
  "history": [
    {"role": "user", "content": "上一轮问题"},
    {"role": "assistant", "content": "上一轮回答"}
  ]
}
```

响应：

```json
{
  "response": "Every change must go through a pull request ...",
  "timings": {
    "retrieval_ms": 20.2,
    "retrieval_calls": 1,
    "llm_ms": 1888.5,
    "llm_calls": 1,
    "total_ms": 1912.6,
    "overhead_ms": 3.9
  }
}
```

时间单位均为**毫秒**，`*_calls` 为次数。

```bash
curl -s http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"u1","user_dept":"Finance","message":"What is the total 2026 operating budget?","history":[]}'
```

### 5.3 网页测试台

- 选择部门、填写 user_id 后提问；Enter 发送，Shift+Enter 换行
- 多轮对话会自动携带历史；「新对话」清空
- 每条回答显示：总计 / 检索 / LLM / 框架开销 / 调用次数
- 预设按钮包含「越权测试（薪资）」，可直接验证 Tech 用户拿不到 Finance 薪资

### 5.4 测试与诊断脚本

```bash
python scripts/check_llm.py          # 探测 /models + 一次 chat completion
python scripts/verify_retrieval.py   # 无需 LLM，检查入库与隔离
python scripts/bench_retrieval.py    # embedding / FAISS / Milvus / LLM 延迟
python test_agent.py                 # 端到端 7 场景（需服务已启动）
```

### 5.5 数据组织与增量更新

- 目录名即部门名：`data/Finance/*.pdf` → `dept="Finance"`
- 修改文档后重新执行 `python run_ingestion.py`
- 两种后端**都不做去重**，重复执行会产生重复 chunk。重建方式：

```bash
# FAISS
rm -rf db/vector_db && python run_ingestion.py

# Milvus
python - <<'PY'
from pymilvus import connections, utility
connections.connect(host="localhost", port="19530")
utility.drop_collection("enterprise_kb")
print("dropped")
PY
VECTOR_STORE_TYPE=milvus python run_ingestion.py
```

> 切换后端后必须各自重新入库：FAISS 与 Milvus 的索引互相独立。

---

## 6. 配置项

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_API_BASE` | `http://localhost:11434/v1` | OpenAI 兼容端点（Ollama/vLLM/GLM/DeepSeek/OpenAI） |
| `LLM_API_KEY` | `ollama` | API Key |
| `LLM_MODEL` | `qwen2.5:latest` | 模型名，必须与端点一致 |
| `VECTOR_STORE_TYPE` | `faiss` | `faiss` 或 `milvus` |
| `DB_PATH` | `./db/vector_db` | FAISS 索引目录 |
| `EMBEDDING_BACKEND` | `local` | `local` 或 `openai` |
| `LOCAL_EMBEDDING_MODEL` | `BAAI/bge-small-zh-v1.5` | 本地 embedding（512 维） |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | `EMBEDDING_BACKEND=openai` 时使用 |
| `HF_ENDPOINT` | `https://hf-mirror.com` | 模型下载镜像（`src/__init__.py` 设定） |

优先级：**环境变量 > `.env` > 代码默认值**。因此可以临时覆盖而不改文件：

```bash
VECTOR_STORE_TYPE=milvus python run_ingestion.py
```

---

## 7. 常见问题（FAQ）

### Q1. `Failed to initialize NumPy: _ARRAY_API not found`
torch 2.2.2 与 NumPy 2.x ABI 不兼容。

```bash
pip install "numpy<2" "pandas<2.3" -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### Q2. `NameError: name 'nn' is not defined` / `is_torch_available()` 为 False
`transformers 5.x` 要求 `torch>=2.6`，而 Intel macOS 上 torch 最高 2.2.2。固定版本：

```bash
pip install "transformers==4.49.0" "sentence-transformers==3.4.1" "huggingface-hub==0.36.0"
```

### Q3. 无法连接 huggingface.co
已默认走 `HF_ENDPOINT=https://hf-mirror.com`。若仍失败，检查该环境变量是否被覆盖，或手动预下载模型：

```bash
HF_ENDPOINT=https://hf-mirror.com python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-zh-v1.5')"
```

### Q4. `ModuleNotFoundError: No module named 'src'`
在 `scripts/` 下直接运行时，Python 找不到项目根目录。脚本已内置 `sys.path` 注入；自己写脚本时注意同样处理，或从根目录以模块方式运行。

### Q5. `docker compose up` 后 Milvus 一直 unhealthy
```bash
docker compose logs --tail=100 milvus-standalone
curl -f http://localhost:9091/healthz
```
常见原因：etcd 未 ready（`depends_on` 已处理）、端口 19530/9091 被占用、首次启动需要 60–120 秒。也可以 `docker compose restart standalone`。

### Q6. 镜像拉取 403 / not found
本项目实测可用组合：
```bash
docker pull quay.io/coreos/etcd:v3.5.5
docker pull docker.1panel.live/milvusdb/milvus:v2.4.0
docker tag docker.1panel.live/milvusdb/milvus:v2.4.0 milvusdb/milvus:v2.4.0
```
或直接：`MILVUS_IMAGE=docker.1panel.live/milvusdb/milvus:v2.4.0 docker compose up -d`。

### Q7. `docker compose` 里为什么没有 MinIO
Milvus 2.4 支持 `common.storageType: local`，数据直接落盘到 `./volumes/milvus`，避免受限网络下拉取 minio 镜像。需要 S3 时改回 `remote` 并补 minio 服务。

### Q8. 启动时报 “vector store is empty”
未入库。执行 `python run_ingestion.py`（Milvus 模式需先 `docker compose up -d`）。

### Q9. 问答很慢（单次 > 30 秒）
- 是否在用本地 7B 模型跑 CPU 推理：约 3 tok/s，且 Ollama 默认 5 分钟卸载模型，冷启动重载 5GB。
- 看响应里的 `timings`：若 `llm_ms` 占比 >95%，换云端模型即可；若 `retrieval_ms` 很大，见 Q10。

### Q10. 第一个请求特别慢
embedding 模型/连接冷启动，`retrieval_ms` 可能 1–2 秒；后续稳定在 15–25ms。属正常现象，可在启动后预热一次。

### Q11. `/chat` 超时
`test_agent.py` 默认 300 秒（`CHAT_TIMEOUT` 可覆盖）。云端限流或本地模型过慢时建议换模型并减小 `max_tokens`。

### Q12. 越权测试为什么能答出来
正常行为：Tech 用户问 Finance 薪资，应返回「not available for the Tech department」。若真的返回了薪资数字，检查：
- 入库时 `dept` 是否正确（`verify_retrieval.py` 会拦截）
- `VECTOR_STORE_TYPE` 是否与入库时一致（切换后端会导致检索空/错库）
- Milvus 查询是否带 `expr`（`src/engine.py:MilvusDriver.search`）

### Q13. 多轮对话上下文丢失
`/chat` 需要客户端回传 `history`（服务端本身无会话存储）。网页测试台会自动维护。

### Q14. `uvicorn` 启动后 `/ui/` 404
确认 `static/index.html` 存在，且从项目根目录启动（`STATIC_DIR` 基于 `main.py` 所在目录解析，通常不受 cwd 影响）。

### Q15. LangChain 1.x 导入报错
旧路径已移除：`langchain.text_splitter` → `langchain_text_splitters`，`langchain.memory` 等也不存在。项目内已修正。

---

## 8. 性能优化

### 8.1 先定位瓶颈

每个 `/chat` 响应都带 `timings`，或运行：

```bash
python scripts/bench_retrieval.py
```

本机实测（17 chunks，Milvus 模式）：

| 阶段 | 平均 | p50 |
|---|---|---|
| 本地 embedding | 141ms（首条预热 1.25s） | 18ms |
| FAISS 检索 | 14ms | 16ms |
| Milvus 检索 | 24ms | 24ms |
| 云端 LLM 单次 | 1318ms | 1397ms |

结论：**LLM 占 80–95%**，向量库占比 <2%。优化优先级依次为 LLM → 调用次数 → 输出长度 → 检索/切分。

### 8.2 LLM 层（收益最大）

1. **用云端或 GPU 推理**：本地 7B CPU ≈3 tok/s；GLM-4-Flash ≈1–2s。
2. **减少 LLM 调用次数**：当前 `agent → tools → agent` 回路在触发工具时是 2 次。检索已改为确定性前置注入，可考虑直接去掉 `search_knowledge_base` 工具，只保留工单工具，从而减少无谓回路。
3. **限制输出长度**：`src/agent.py` 中 `max_tokens=512`，系统提示已要求 ≤120 词，可进一步降到 256。
4. **启用流式输出**：`stream`/SSE 降低首字延迟，用户感知更快（总时长不变）。
5. **精简提示词**：系统提示与注入的 4 个片段都会进入上下文；把 `k=4` 降到 3、或对片段做截断，可减少 prompt token。

### 8.3 检索层

1. **FAISS vs Milvus**：小数据量差异仅约 10ms。数据量到十万级后，Milvus 的 `expr` 服务端过滤优势明显——FAISS 需要取 50 个候选再本地过滤（`pool = max(k*20, 50)`），过滤选择性高时会浪费算力。
2. **建立索引**：Milvus 默认可为 `enterprise_kb` 建 HNSW 索引并 `load`，大规模下显著降低搜索延迟。
3. **控制候选数**：`k` 从 4 提到更高会增加 prompt 长度和 LLM 时间。
4. **切分参数**：`RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=100)`。chunk 太小召回碎片化，太大增加 prompt；按文档类型调整。
5. **可选重排**：`src/reranker.py` 提供 BGE CrossEncoder 两阶段重排（top20 → top3），能提升准确率但增加 100–300ms/次及模型加载开销，当前未接入主链路。

### 8.4 服务层

1. **同步路由 + 线程池**：`/chat` 使用 `def`，FastAPI 自动放到线程池，长请求不会阻塞事件循环。
2. **多 worker**：`uvicorn main:app --workers 2` 可提升并发吞吐，但每个 worker 会各自加载 embedding 模型（内存翻倍），Milvus 模式更合适；FAISS 多进程写同一目录需谨慎。
3. **缓存**：相同/相似问题可加 Redis 或内存 LRU 缓存整个回答；embedding 结果也可缓存。
4. **预热**：启动后先发一个空/短查询，避免首个真实用户承担 1–2s 冷启动。
5. **超时与重试**：`test_agent.py` 的 `CHAT_TIMEOUT`、`check_llm.py` 的超时可按网络情况调整；对云端 429 做指数退避。

### 8.5 入库层

1. `run_ingestion.py` 按部门循环、逐个 `add_documents`；文档多时可改为批量/并发 embedding（`encode_kwargs={"batch_size": 64}`）。
2. 大文件可先做 OCR/表格抽取，避免 PDF 解析丢内容导致检索命中率低。
3. 定期重建索引，避免重复入库带来的冗余向量与噪声。

---

## 9. 安全注意事项

- `.env` 含 API Key，已在 `.gitignore` 中忽略；不要提交或贴到公开渠道。
- 部门隔离依赖 `user_dept` 的来源。生产环境应从前端登录态/网关注入，**不要信任客户端任意传值**；当前实现假设上游已完成身份认证。
- `create_support_ticket` 目前是占位实现（固定返回 `TICK-12345`），接入 Jira/Zendesk 时需补充鉴权与审计。
- Milvus/etcd 默认无认证，仅建议在内网或本机使用；生产需开启 TLS/认证并限制 19530 端口暴露。

---

## 10. 已知限制与后续方向

- 服务端不存储会话，多轮依赖客户端回传 `history`
- 入库无去重/增量更新机制，重复执行会产生重复 chunk
- 无文档权限的细粒度控制（目前仅到部门级）
- 无鉴权中间件，`user_id`/`user_dept` 由调用方提供
- 可扩展：流式输出、会话持久化、文档级 ACL、重排接入、评测集与召回率监控

---

## 11. 许可证

本项目采用 [MIT License](./LICENSE)，Copyright (c) 2026 sjfdev。可自由使用、修改、分发与商用，需保留版权与许可声明。
