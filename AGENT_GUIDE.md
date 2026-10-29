# 企业知识库 Agent 说明文档

面向研发与运维的完整说明：技术栈、架构、安装、使用、常见问题与性能优化。

---

## 1. 项目概述

一个可私有化部署的企业知识库问答 Agent，核心能力：

- **多格式 RAG**：PDF / DOCX / PPTX / XLSX / CSV / Markdown / TXT / HTML / JSON → 切分 → 向量化 → 检索 → 答案附带来源
- **增量入库**：按内容哈希跳过未变文件、替换变更文件、清理已删除文件
- **部门级权限隔离**：每个 chunk 带 `dept` 元数据，检索时强制按部门过滤，跨部门数据不可见
- **多轮对话**：客户端回传 `history`，服务端按会话还原上下文
- **工具调用**：知识库无解时自动创建工单（`create_support_ticket`）
- **双向量后端**：本地 FAISS（零依赖）与 Milvus（企业级）随时切换
- **双 Embedding 后端**：本地 sentence-transformers（离线）或 OpenAI 兼容接口
- **内置测试台**：`/ui/` 网页端可直接按部门提问并查看分段耗时
- **入库管理台**：`/admin` 支持上传、状态检查、增量入库、删除、重建索引

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
| PDF | pypdf / reportlab | 6.21.3 / 5.1.0 | 解析与样例生成 |
| Office | python-docx / python-pptx / openpyxl | 1.2.0 / 1.0.2 / 3.1.5 | Word / PPT / Excel 抽取 |
| Web/文本 | beautifulsoup4 / chardet | 4.15.0 / 7.7.0 | HTML 清洗、编码识别 |
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
│   ├── eval_retrieval.py    # 离线评测 hit@k / MRR / 泄漏
│   ├── check_health.py      # /health 冒烟测试
│   ├── check_hybrid.py      # BM25/RRF 单元检查
│   ├── check_hybrid_milvus.py # 服务端稀疏 BM25 端到端
│   └── bench_retrieval.py   # 延迟基准（embedding/FAISS/Milvus/LLM）
└── src/
    ├── config.py            # 所有开关（pydantic-settings）
    ├── engine.py            # Embedding 工厂 + FAISS/Milvus 驱动 + 单例
    ├── etl.py               # PDF 加载、切分、打 dept 标签
    ├── agent.py             # LangGraph 状态机与检索注入
    ├── tools.py             # search_knowledge_base / create_support_ticket
    ├── lexical.py           # BM25 索引 + RRF 融合（混合检索）
    ├── reranker.py          # CrossEncoder 重排（可选，默认关闭）
    ├── health.py            # /health 探针
    ├── metrics.py           # Prometheus 指标
    ├── runtime.py           # 维护模式标志
    └── admin_api.py         # /admin 管理 API
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
  requests pypdf reportlab \
  python-docx python-pptx openpyxl beautifulsoup4 chardet

# 锁定与 torch 2.2.2 兼容的推理栈
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
  "transformers==4.49.0" "sentence-transformers==3.4.1" "huggingface-hub==0.36.0"
```

> 安装完成后务必验证：`python -c "import torch, numpy, faiss, langchain; print(torch.__version__, numpy.__version__)"`，应为 `2.2.2 1.26.4` 且无 NumPy ABI 警告。
> 完整版本清单见仓库根目录 `requirements.txt`（与本文档实测版本一致）。

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
python run_ingestion.py              # 增量入库，读取 data/<dept>/*.pdf
python run_ingestion.py --reset      # 清空索引后全量重建
python scripts/verify_retrieval.py   # 离线校验（不需要 LLM）
```

输出示例：

```
Vector store: milvus

[Finance]
  add      Finance/confidential_budget_2026.pdf (1 chunks)
  add      Finance/expense_reimbursement_policy.pdf (2 chunks)
  add      Finance/travel_policy.pdf (2 chunks)

[HR]
  add      HR/benefits_guide.pdf (2 chunks)
  ...

Done. docs: added=17 updated=0 skipped=0 pruned=0; chunks written=26
```

再次执行时未改动的文件会显示 `skip ... (unchanged)`，向量数不变。

支持的格式（`src/loaders.py`）：

| 格式 | 扩展名 | 定位字段 | 说明 |
|---|---|---|---|
| PDF | `.pdf` | `page` | 按页解析 |
| Word | `.docx` | - | 段落 + 表格（表格转 `a | b` 行） |
| PowerPoint | `.pptx` | `slide` | 文本框 + 表格 + 演讲者备注 |
| Excel | `.xlsx` | `sheet` | 每个工作表一条，最多 5000 行/表 |
| CSV | `.csv` | - | 自动识别 `, ; \t |` 分隔符 |
| Markdown | `.md` `.markdown` | - | 纯文本 |
| 文本 | `.txt` | - | UTF-8 / GB18030 / chardet 兜底 |
| HTML | `.html` `.htm` | - | 去脚本样式，保留 `<title>` |
| JSON | `.json` | - | 解析失败时按原文入库 |

限制与建议：

- 单文件 ≤50MB；忽略 `.` 开头与 Office 临时文件 `~$*`
- 支持部门目录下多级子目录（`data/Tech/runbooks/*.md`）
- `.doc/.ppt/.xls` 旧格式需先转成新格式（会打印提示并跳过）
- 扫描版 PDF 需 OCR，当前未内置；可先用 `ocrmypdf` 生成可搜索 PDF
- 无 LLM 快速自检：`python scripts/check_loaders.py`

### 内置多格式 Demo 数据

仓库自带 `data/Demo/`（9 个文件，覆盖全部格式），可直接用于联调：

```bash
python scripts/generate_demo_files.py   # 重新生成（幂等）
python run_ingestion.py                 # 增量入库
```

| 文件 | 格式 | 示例问题 |
|---|---|---|
| `program_overview.pdf` | PDF | Demo 环境多久刷新一次？ |
| `release_checklist.docx` | Word | 发布前必须完成哪些步骤？ |
| `onboarding.pptx` | PPT | 入职第一周做什么？ |
| `metrics.xlsx` | Excel | test coverage 是多少？ |
| `contacts.csv` | CSV | 谁负责 on-call？ |
| `policy.md` | Markdown | 如何申请 VPN？ |
| `faq.txt` | 文本 | 访客 Wi-Fi 密码？ |
| `runbook.html` | HTML | 错误率超过多少要叫 on-call？ |
| `metadata.json` | JSON | demo-api 的 SLA 是多少？ |

在 `/ui/` 中选择 **Demo** 部门，或点「Demo VPN（多格式）」「Demo 指标（Excel）」预设，即可验证多格式检索与来源展示（`slide` / `sheet` 标签）。

每个 chunk 的元数据：

| 字段 | 说明 |
|---|---|
| `dept` | 部门（权限过滤依据） |
| `source` | 相对路径，如 `Finance/travel_policy.pdf`（不含绝对路径，可直接展示） |
| `page` | PDF 页码（从 1 开始） |
| `doc_id` | `sha1(dept|source)`，文档级去重/删除的键 |
| `doc_hash` | 文件内容 sha1，用于判断是否变更 |
| `chunk_index` | 文档内切片序号 |
| `kind` | 来源类型（pdf/docx/pptx/xlsx/csv/markdown/text/html/json） |
| `slide` | PPT 页码（仅 pptx） |
| `sheet` | 工作表名（仅 xlsx） |
| `title` | 文档标题（HTML `<title>`、DOCX 属性，若存在） |

增量行为：

- 内容未变 → 跳过（不重复 embedding）
- 内容变更 → 删除该 `doc_id` 的旧 chunk 后重新写入
- 文件删除 → 自动清理其 chunk（`--no-prune` 可关闭）
- 切换 FAISS/Milvus 或元数据结构变化后，用 `--reset` 重建

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
| `POST /chat` | 对话接口（一次性返回） |
| `POST /chat/stream` | 流式对话（SSE，逐 token 返回） |

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
  "sources": [
    {"source": "Tech/coding_standards.pdf", "dept": "Tech", "kind": "pdf", "page": 1, "snippet": "Every change must go through a pull request..."},
    {"source": "Tech/security_policy.pdf", "dept": "Tech", "kind": "pdf", "page": 1, "snippet": "Rotate credentials every 90 days..."}
  ],
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

时间单位均为**毫秒**，`*_calls` 为次数。`sources` 为本次回答实际使用的去重片段（文档 + 页码 + `snippet` 原文摘录），UI 中折叠为「来源」面板，可展开查看摘录；改用工具建单时仍保留首次检索到的来源。

健康检查：

```bash
curl -s http://localhost:8000/health            # 轻量，适合负载均衡探针
curl -s 'http://localhost:8000/health?probe=1'  # 额外调用一次 LLM（结果缓存 15s）
python scripts/check_health.py --probe          # 冒烟测试
```

返回 `status` 为 `ok` / `degraded` / `down`，并附带各分项：

| 字段 | 含义 |
|---|---|
| `vector_store.chunks` | 当前索引块数；低于 `HEALTH_MIN_CHUNKS` 时整体 `down`（HTTP 503） |
| `ingestion.active` / `latest` | 是否有入库任务在跑；有任务时整体 `degraded` |
| `maintenance` | 全量 rebuild 期间为 `true`，此时 `/chat` 返回 503 |
| `llm.probe` | 仅在 `?probe=1` 时为 `ok`/`failed`，含 `latency_ms` |
| `rerank_enabled` | 当前是否启用二阶段重排 |

`down` 才会返回 503，`degraded` 仍为 200，便于负载均衡只摘除空库/故障实例。`hybrid.engine` 表示实际生效的混合检索引擎（`off`/`local`/`milvus`）。

Prometheus 指标：

```bash
curl -s http://localhost:8000/metrics | grep '^agent_rag_'
# 设置 METRICS_TOKEN 后需带 Authorization: Bearer <token>
```

| 指标 | 类型 | 说明 |
|---|---|---|
| `agent_rag_chat_requests_total{endpoint,status}` | counter | 请求数，status 为 ok/error/rejected |
| `agent_rag_chat_duration_seconds{endpoint,status}` | histogram | 端到端耗时 |
| `agent_rag_retrieval_seconds` | histogram | 检索（含重排）耗时 |
| `agent_rag_llm_seconds{calls}` | histogram | LLM 等待耗时（按调用次数分桶标签） |
| `agent_rag_llm_calls_total{endpoint}` | counter | LLM 调用总数（工具回路会 >1） |
| `agent_rag_first_token_seconds` | histogram | 流式首 token 延迟 |
| `agent_rag_index_chunks` | gauge | 当前索引块数（抓取时最多每 5s 刷新） |
| `agent_rag_ingest_jobs_total{status}` | counter | 入库任务结果 |
| `agent_rag_maintenance` | gauge | 1 表示正在全量重建 |

部门名**不作为标签**，避免敏感信息与高基数。

错误响应：

| 状态码 | 含义 | 示例 |
|---|---|---|
| 400 | 部门名非法（含引号/空格等） | `{"detail":"invalid department name"}` |
| 422 | 参数校验失败（缺字段、history 超过 20 条、role 非 user/assistant、文本过长） | FastAPI 标准校验结构 |
| 503 | LLM 上游不可用（网络/DNS/超时） | `{"detail":{"error":"llm upstream unavailable","request_id":"..."}}` |
| 503 | 正在全量重建索引 | `{"detail":{"error":"index rebuilding","request_id":"..."}}` |
| 503 | 向量库不可达 | `{"detail":{"error":"vector_store_unavailable","request_id":"..."}}` |
| 500 | 内部错误，不回传堆栈 | `{"detail":{"error":"internal error","request_id":"85c68605"}}` |

服务端日志会打印完整堆栈与 `request_id`，响应头也带 `X-Request-ID`，便于对账。

约束：`user_id` ≤64、`user_dept` ≤64（仅字母/数字/下划线/中划线/中文）、`message` 1–4000 字符、`history` ≤20 条。

流式接口（推荐给前端）：

```bash
curl -N http://localhost:8000/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"u1","user_dept":"Finance","message":"What is the total 2026 operating budget?","history":[]}'
```

事件格式（每条以 `data: ` 开头，空行分隔）：

```
data: {"type":"token","content":"The total "}
data: {"type":"token","content":"2026 operating budget is ..."}
data: {"type":"done","sources":[{"source":"Finance/confidential_budget_2026.pdf","dept":"Finance","page":1}],"timings":{"retrieval_ms":20.1,"llm_ms":1287.2,"llm_calls":1,"total_ms":1311.3,"overhead_ms":4.0}}
```

出错时返回 `{"type":"error","code":"internal_error|upstream_unavailable|vector_store_unavailable|maintenance","message":"...","request_id":"..."}`。实测热态首 token ≈0.68s，整段 ≈1.1–1.3s。

非流式接口：

```bash
curl -s http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"u1","user_dept":"Finance","message":"What is the total 2026 operating budget?","history":[]}'
```

### 5.3 网页测试台

- 选择部门、填写 user_id 后提问；Enter 发送，Shift+Enter 换行
- 多轮对话会自动携带历史；「新对话（新窗口）」打开独立会话，「清空当前」只清空本标签页
- 回答**逐字流式渲染**（首字约 0.7s 出现），结束后显示来源与总计 / 检索 / LLM / 框架开销
- 预设包含「越权测试（薪资）」与 Demo 多格式示例；左下角「入库管理」进入 `/admin`

### 5.4 测试与诊断脚本

```bash
python scripts/check_llm.py          # 探测 /models + 一次 chat completion
python scripts/verify_retrieval.py   # 无需 LLM，检查入库与隔离
python scripts/eval_retrieval.py     # 无需 LLM，hit@k / MRR / 跨部门泄漏
python scripts/check_health.py       # /health 冒烟（加 --probe 会调一次 LLM）
python scripts/check_hybrid.py       # 混合检索单元检查（无向量库/LLM）
python scripts/check_hybrid_milvus.py # 服务端稀疏 BM25 端到端（需 Milvus 2.5+，低版本自动跳过）
python scripts/check_loaders.py      # 9 种格式解析（不写向量库）
python scripts/check_admin.py        # 管理台上传→入库→删除冒烟
python scripts/bench_retrieval.py    # embedding / FAISS / Milvus / LLM 延迟
python scripts/generate_demo_files.py  # 重建 data/Demo 多格式样例
python test_agent.py                 # 端到端 13 项断言（需服务已启动）
```

### 5.5 数据组织与增量更新

- 目录名即部门名：`data/Finance/...` → `dept="Finance"`，支持多级子目录
- 每个 chunk 记录 `source/page/slide/sheet/doc_id/doc_hash/kind`，答案返回 `sources`
- 增量：未变跳过（哈希一致）、变更替换（按 `doc_id` 先删后插）、删除文件自动清理
- 全量重建：`python run_ingestion.py --reset`（切换后端或元数据结构变化时必须执行）
- 保留已删除文件的旧 chunk：加 `--no-prune`

```bash
python run_ingestion.py            # 增量
python run_ingestion.py --reset    # 清空后全量重建
python run_ingestion.py --no-prune # 不清理已删除文件
```

> FAISS 与 Milvus 索引互相独立；切换 `VECTOR_STORE_TYPE` 后需对目标后端单独入库。也可以在 `/admin` 中点击「重建索引」。

### 5.6 入库管理台 `/admin`

打开 `http://localhost:8000/admin`（对话页左下角也有入口）：

- **部门**：切换/创建部门，显示文件总数与各状态数量
- **上传**：拖拽或选择多文件，可指定子目录；单文件上限 `MAX_UPLOAD_MB`
- **文件列表**：显示类型、大小、状态（已入库/待入库/有变更/不支持）、chunk 数、修改时间
- **操作**：增量入库当前部门、全量入库（含清理）、删除文件（同时移除其 chunk）
- **重建索引**：勾选确认后删除现有集合/索引并全量重导
- **任务日志**：实时轮询，展示每个文件的 add/update/skip 与最终统计

管理 API（前缀 `/admin/api`）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/meta` | 后端、格式、部门与状态统计 |
| GET | `/files?dept=` | 文件清单与入库状态 |
| POST | `/dept` | 创建部门目录 `{"name":"QA"}` |
| POST | `/upload` | multipart：`dept`、`subdir`、`files` |
| DELETE | `/file?dept=&source=` | 删除文件并移除对应 chunk |
| POST | `/ingest` | `{"dept": "QA"}` 或 `{"reset": true}` |
| GET | `/jobs/{id}` | 任务状态与日志 |

安全：

- 未设置 `ADMIN_TOKEN` 时，管理 API 仅接受**本机回环**（127.0.0.1/::1），局域网访问返回 403
- 设置后所有请求需带 `X-Admin-Token`（管理台右上角可保存到 sessionStorage）
- 上传会做扩展名白名单、大小限制、文件名与子目录净化；`../` 会被拒绝或剥离
- 删除/重建会修改知识库，生产环境请配合反向代理鉴权与备份

---

## 6. 配置项

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_API_BASE` | `http://localhost:11434/v1` | OpenAI 兼容端点（Ollama/vLLM/GLM/DeepSeek/OpenAI） |
| `LLM_API_KEY` | `ollama` | API Key |
| `LLM_MODEL` | `qwen2.5:latest` | 模型名，必须与端点一致 |
| `LLM_TIMEOUT` | `30` | 单次 LLM 请求超时（秒） |
| `LLM_MAX_TOKENS` | `256` | 生成上限，调小可提速 |
| `LLM_MAX_RETRIES` | `2` | LLM 失败重试次数 |
| `AGENT_RECURSION_LIMIT` | `6` | LangGraph 最大步数，防止工具循环 |
| `VECTOR_STORE_TYPE` | `faiss` | `faiss` 或 `milvus` |
| `DB_PATH` | `./db/vector_db` | FAISS 索引目录 |
| `RETRIEVAL_K` | `4` | 注入提示词的片段数 |
| `RERANK_ENABLED` | `false` | 开启 CrossEncoder 二阶段重排 |
| `RERANK_MODEL` | `BAAI/bge-reranker-base` | 重排模型（中文推荐；英文可用 `cross-encoder/ms-marco-MiniLM-L-6-v2`） |
| `RERANK_CANDIDATES` | `12` | 送入重排的候选数 |
| `HEALTH_MIN_CHUNKS` | `1` | 低于该块数时 `/health` 判定为 down |
| `HYBRID_ENABLED` | `true` | 是否启用混合检索 |
| `HYBRID_ENGINE` | `auto` | `auto` / `local` / `milvus`，按服务端能力自适应 |
| `HYBRID_SPARSE_FIELD` | `sparse` | Milvus BM25 函数输出的稀疏字段名 |
| `HYBRID_ANALYZER` | `{"type":"chinese"}` | 服务端 BM25 分析器（升级 2.5 后生效） |
| `HYBRID_CANDIDATES` | `20` | 融合前每路取多少候选 |
| `HYBRID_MAX_DOCS` | `20000` | 单部门 BM25 语料上限（0 为不限） |
| `METRICS_TOKEN` | 空 | 非空时 `/metrics` 需要 Bearer token |
| `MILVUS_HOST` / `MILVUS_PORT` | `localhost` / `19530` | Milvus 连接地址 |
| `MILVUS_COLLECTION` | `enterprise_kb` | 集合名 |
| `MILVUS_URI` | 空 | 非空时优先于 host/port（如 `http://host:19530`） |
| `EMBEDDING_BACKEND` | `local` | `local` 或 `openai` |
| `LOCAL_EMBEDDING_MODEL` | `BAAI/bge-small-zh-v1.5` | 本地 embedding（512 维） |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | `EMBEDDING_BACKEND=openai` 时使用 |
| `HF_ENDPOINT` | `https://hf-mirror.com` | 模型下载镜像（`src/__init__.py` 设定） |
| `DATA_DIR` | `./data` | 入库根目录（CLI 与管理台共用） |
| `ADMIN_TOKEN` | 空 | 管理 API 令牌；为空时仅回环可访问 |
| `MAX_UPLOAD_MB` | `50` | 单文件上传/入库大小上限 |

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

### Q3.1 启动时卡在 huggingface 元数据请求（每次 ~10s）
即使模型已缓存，`huggingface_hub` 仍会做 HEAD 校验。已在 `src/__init__.py` 将 `HF_HUB_ETAG_TIMEOUT` 设为 3s；如需彻底跳过，在 `.env` 加 `HF_HUB_OFFLINE=1`（模型必须已下载完成）。

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

### Q10.5 页面显示「模型服务暂时不可用（网络或 DNS 异常）」
说明请求没到智谱服务器，通常是本机 DNS/网络问题（例如连手机热点时 DNS 失效）。
```bash
nslookup open.bigmodel.cn            # 本机 DNS
nslookup open.bigmodel.cn 223.5.5.5  # 公共 DNS 对比
```
若公共 DNS 能解析，把 Wi-Fi 的 DNS 改成 `223.5.5.5` / `119.29.29.29` 即可。UI 上会显示「重试」按钮，网络恢复后点一下即可，无需重开服务。

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
| MiniLM 重排（12→4，可选） | +205ms | +190ms |
| BGE reranker-base（12→4，可选） | +1226ms | +1116ms |
| 云端 LLM 单次 | 1318ms | 1397ms |

结论：**LLM 占 80–95%**，向量库占比 <2%。优化优先级依次为 LLM → 调用次数 → 输出长度 → 检索/切分。

### 8.2 LLM 层（收益最大）

1. **用云端或 GPU 推理**：本地 7B CPU ≈3 tok/s；GLM-4-Flash ≈1–2s。
2. **减少 LLM 调用次数**：当前 `agent → tools → agent` 回路在触发工具时是 2 次。检索已改为确定性前置注入，可考虑直接去掉 `search_knowledge_base` 工具，只保留工单工具，从而减少无谓回路。
3. **限制输出长度**：`LLM_MAX_TOKENS=256`，系统提示要求 ≤3 句 / ≤60 词。
4. **启用流式输出**：`stream`/SSE 降低首字延迟，用户感知更快（总时长不变）。
5. **精简提示词**：系统提示与注入的 4 个片段都会进入上下文；把 `k=4` 降到 3、或对片段做截断，可减少 prompt token。

### 8.3 检索层

1. **FAISS vs Milvus**：小数据量差异仅约 10ms。数据量到十万级后，Milvus 的 `expr` 服务端过滤优势明显——FAISS 需要取 50 个候选再本地过滤（`pool = max(k*20, 50)`），过滤选择性高时会浪费算力。
2. **建立索引**：Milvus 默认可为 `enterprise_kb` 建 HNSW 索引并 `load`，大规模下显著降低搜索延迟。
3. **控制候选数**：`k` 从 4 提到更高会增加 prompt 长度和 LLM 时间。
4. **切分参数**：`RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=100)`。chunk 太小召回碎片化，太大增加 prompt；按文档类型调整。
5. **二阶段重排**：`src/reranker.py` 已接入 `kb_engine.search`，设置 `RERANK_ENABLED=true` 后先对融合后的候选做 CrossEncoder 重排再取 top-k。默认关闭。24 条中英评测（纯向量）：不重排 MRR 0.88 / p50≈16ms；MiniLM +≈205ms、MRR 0.92；`bge-reranker-base` +≈1.2s（p95 4.8s）、MRR 0.97。混合检索 + MiniLM 同样为 0.92。中文优先 BGE，英文/低延迟选 MiniLM，二者都需先缓存模型。
6. **多轮检索改写**：`src/agent.py::_retrieval_query` 检测代词/指示词或极短问句，把上一轮问题拼进检索 query，不额外调用 LLM。实测追问 "Who approves it?" 时目标文档从第 3 位升到第 1 位。
7. **混合检索（能力自适应）**：`HYBRID_ENGINE=auto` 默认。Milvus ≥ 2.5 且集合含 BM25 稀疏字段时，用服务端 `hybrid_search` + `RRFRanker`（一次请求完成稠密+稀疏融合）；否则自动回退到 `src/lexical.py` 的进程内 BM25（中英文分词：英文按词、中文按字 + 二元组，RRF 融合）。FAISS 永远走进程内 BM25。`local` 强制进程内，`milvus` 在能力不足时启动即报错（不会静默降级）。进程内 BM25 首次查询需加载该部门全部 chunk，启动后与入库后都会后台预热；`HYBRID_MAX_DOCS` 限制单部门语料。24 条评测 MRR 0.88 → 0.89，精确标识符（如 `DEMO-GUEST`）类查询收益最明显。

    从 2.4 升级到 2.5 后，旧集合没有稀疏字段：要么 `python run_ingestion.py --reset` 重建（推荐），要么先设 `HYBRID_ENGINE=local` 继续跑。可用 `GET /info` 的 `hybrid.engine` 确认当前实际走哪条路径；2.5 上运行 `python scripts/check_hybrid_milvus.py` 做端到端验证（使用临时集合，完成后删除）。
8. **评测闭环**：`eval/cases.jsonl` + `scripts/eval_retrieval.py`，覆盖 Tech/Finance/HR/Demo 共 18 个命中用例和 6 个跨部门泄漏用例；调参、换重排模型或改混合策略后先跑它。

### 8.4 服务层

1. **同步路由 + 线程池**：`/chat` 使用 `def`，FastAPI 自动放到线程池，长请求不会阻塞事件循环。
2. **多 worker**：`uvicorn main:app --workers 2` 可提升并发吞吐，但每个 worker 会各自加载 embedding 模型（内存翻倍），Milvus 模式更合适；FAISS 多进程写同一目录需谨慎。
3. **缓存**：相同/相似问题可加 Redis 或内存 LRU 缓存整个回答；embedding 结果也可缓存。
4. **预热**：启动后先发一个空/短查询，避免首个真实用户承担 1–2s 冷启动。
5. **超时与重试**：`test_agent.py` 的 `CHAT_TIMEOUT`、`check_llm.py` 的超时可按网络情况调整；对云端 429 做指数退避。

### 8.5 入库层

1. `run_ingestion.py` 按部门循环、逐个 `add_documents`；文档多时可改为批量/并发 embedding（`encode_kwargs={"batch_size": 64}`）。
2. 大文件可先做 OCR/表格抽取，避免 PDF 解析丢内容导致检索命中率低。
3. 增量入库已按 `doc_hash` 跳过未变文件、按 `doc_id` 替换与清理；如需彻底清空用 `--reset`。
4. 新增格式只需在 `src/loaders.py` 实现 `_load_xxx` 并注册扩展名；解析器应返回带 `kind` 与定位元数据的 `Document`。

---

## 9. 安全注意事项

- `.env` 含 API Key，已在 `.gitignore` 中忽略；不要提交或贴到公开渠道。
- 部门隔离依赖 `user_dept` 的来源。生产环境应从前端登录态/网关注入，**不要信任客户端任意传值**；当前实现假设上游已完成身份认证。
- `create_support_ticket` 目前是占位实现（固定返回 `TICK-12345`），接入 Jira/Zendesk 时需补充鉴权与审计。
- Milvus/etcd 默认无认证，仅建议在内网或本机使用；生产需开启 TLS/认证并限制 19530 端口暴露。
- `/admin` 可以写文件、删文件、重建索引：默认仅本机可用；对外开放前必须设置 `ADMIN_TOKEN` 并置于 HTTPS 反向代理之后。

---

## 10. 已知限制与后续方向

- 服务端不存储会话，多轮依赖客户端回传 `history`
- 无文档权限的细粒度控制（目前仅到部门级）
- 对话接口无鉴权，`user_id`/`user_dept` 由调用方提供（管理台另有 `ADMIN_TOKEN`）
- 入库为同步 CPU/IO 操作；全量 reset 期间 `/chat` 返回 503 `index rebuilding`，增量入库期间索引保持可读
- 多轮检索为启发式改写（代词/短句拼接上轮问题），复杂指代仍可能失败；更稳的方案是 LLM 改写（会多一次调用）
- 进程内 BM25（FAISS 模式或 Milvus < 2.5）是**单进程**索引：多 worker 各自构建，重启需预热；超大数据量请升级 Milvus 2.5 并用 `HYBRID_ENGINE=milvus`
- `/metrics` 为单进程指标，多 worker 需在 Prometheus 侧聚合
- 可扩展：OCR、会话持久化、文档级 ACL、零停机 alias 切换、召回率监控

---

## 11. 许可证

本项目采用 [MIT License](./LICENSE)，Copyright (c) 2026 sjfdev。可自由使用、修改、分发与商用，需保留版权与许可声明。
