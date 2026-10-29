# Agent 优化计划（待办清单）

> 记录时间：2026-10-29
> 用途：明天继续开发的唯一入口。完成一项就把 `[ ]` 改成 `[x]`，并在文末「进度记录」补一行。
> 相关文档：`AGENT_GUIDE.md`（使用说明）、`DEPLOYMENT_CN.md` / `DEPLOYMENT.md`（部署）、`AGENTS.md`（协作约定）

---

## 0. 当前基线（已可用，勿回退）

| 能力 | 状态 | 验证方式 |
|---|---|---|
| PDF 入库（FAISS） | 可用 | `python run_ingestion.py` |
| PDF 入库（Milvus） | 可用 | `VECTOR_STORE_TYPE=milvus python run_ingestion.py` |
| 部门隔离（FAISS 后过滤 / Milvus expr） | 可用 | `python scripts/verify_retrieval.py` → `OK - isolation holds` |
| 云端 LLM（GLM-4-Flash） | 可用 | `python scripts/check_llm.py` |
| 端到端 7 场景 | 通过 | `python test_agent.py`（需服务已启动） |
| 网页测试台 | 可用 | `http://localhost:8000/ui/` |
| 分段耗时 | 可用 | `/chat` 返回 `timings`；`python scripts/bench_retrieval.py` |
| Milvus 容器 | 可用 | `docker compose ps` 两个 healthy；`curl -f localhost:9091/healthz` |

启动命令：

```bash
cd /Users/songjiangfeng/work/ai/agent-rag
conda activate agent-rag        # 或用 ~/miniconda3/envs/agent-rag/bin/python
VECTOR_STORE_TYPE=milvus uvicorn main:app --port 8000   # 当前 .env 是 faiss，可去掉前缀
```

基线延迟（本机）：embedding 18ms / FAISS 14ms / Milvus 24ms / LLM ≈1.3s / 单次问答 1–5.4s。

---

## 1. P0 安全与稳定性（优先做，预计半天）

### 1.1 部门过滤表达式注入
- [x] 位置：`src/engine.py`（已抽出 `src/validation.py`）（`MilvusDriver.search`）
- [x] 现状：`expr = f"dept == '{user_dept}'"`，部门名含引号可破坏过滤
- [x] 方案：白名单正则 + 转义
  ```python
  DEPT_RE = re.compile(r"[A-Za-z0-9_\-\u4e00-\u9fa5]{1,64}")
  def _safe_dept(dept: str) -> str:
      if not DEPT_RE.fullmatch(dept or ""):
          raise ValueError("invalid department")
      return dept.replace("\\", "\\\\").replace("'", "\\'")
  ```
- [x] 同时在 `main.py` 入口做一次校验，非法部门返回 400 而不是 500
- [x] 验收（已手工验证 4 种注入串均被拦截）：`scripts/verify_retrieval.py` 追加用例 `dept="Tech' or dept != '"` 应报错/返回空，不得泄漏

### 1.2 切换部门/用户时清空对话历史
- [x] 位置：`static/index.html`（`deptSel` / `uidInput`）
- [x] 现状：切换部门后 `history` 仍保留上一部门回答，会回灌进上下文造成跨部门泄漏
- [x] 方案：`dept`、`uid` 的 `change` 事件里 `history = []; chat.innerHTML = "";` 并提示「已切换，新对话开始」
- [x] 验收：UI 上 Tech 问薪资拿不到 → 切 Finance 问预算，回答不应包含 Tech 上下文；Network 里 `history` 为空数组

### 1.3 错误信息不外泄
- [x] 位置：`main.py`
- [x] 现状：`raise HTTPException(500, detail=str(e))` 暴露内部异常/路径
- [x] 方案：`logging.exception(...)` 记录完整堆栈，返回 `{"detail": "internal error", "request_id": ...}`
- [x] 顺带引入 `logging` 配置（替换 `print`），格式含 request_id / dept / 耗时
- [x] 验收：故意让 LLM 端点不可用，响应体不含堆栈与 URL

### 1.4 LangGraph 循环上限
- [x] 位置：`src/agent.py`（`app.invoke` 调用处，`main.py`）
- [x] 方案：`agent_app.invoke(inputs, config={"recursion_limit": settings.AGENT_RECURSION_LIMIT})`
- [x] 验收：构造一个反复调工具的问题，最多 6 步后返回而不是无限循环

### 1.5 LLM 超时与重试
- [x] 位置：`src/agent.py:ChatOpenAI(...)`
- [x] 方案：`timeout=settings.LLM_TIMEOUT, max_retries=settings.LLM_MAX_RETRIES`
- [x] 验收：把 `LLM_API_BASE` 指向不可达地址，`/chat` 在 ~30s 内返回 503/500，不挂死

### 1.6 测试断言化
- [x] 位置：`test_agent.py`
- [x] 现状：只 `print`，失败也退 0，无法进 CI
- [x] 方案：改为 `check()` + `sys.exit(1)`（7 个 case，含非法部门 400）
- [x] 断言点：
  - `Tech` 问薪资 → 回答含 "not available"/不含具体金额
  - `Finance` 问预算 → 含 `48,000,000`
  - 工单场景 → 含 `TICK-`
  - 每条响应 `timings.total_ms > 0`、`llm_calls >= 1`
- [x] 验收：`python test_agent.py` 全绿退出码 0；人为改错断言退出码 1

### 1.7 `history` 参数校验
- [x] 位置：`main.py`（`HistoryItem` + `Field`）
- [x] 方案：`Field(default_factory=list)`；校验 `role in {"user","assistant"}`、`content` 非空、history 条数上限（如 20）
- [x] 验收：传 `role="tool"` 或缺字段返回 422/400

---

## 2. P1 架构与可维护性（约 1 天）

### 2.1 引擎惰性初始化
- [ ] 位置：`src/engine.py:130` `kb_engine = KnowledgeBaseEngine()`
- [ ] 问题：import 即加载 embedding（~2s），`check_llm.py` 等脚本被拖慢；模型缺失时 import 直接失败
- [ ] 方案：`get_engine()` 惰性单例 + 显式 `reset_engine()`（测试用）
- [ ] 注意：同步替换所有 `kb_engine.xxx` 调用点（`main.py`、`src/agent.py`、`src/etl.py`、脚本）
- [ ] 验收：`python -c "import src.engine"` 不再加载模型；`python scripts/check_llm.py` 启动更快

### 2.2 配置强类型与外置
- [ ] 位置：`src/config.py`
- [ ] `VECTOR_STORE_TYPE: Literal["faiss","milvus"]`
- [ ] `EMBEDDING_BACKEND: Literal["local","openai"]`
- [ ] 新增：`MILVUS_HOST`、`MILVUS_PORT`、`MILVUS_COLLECTION`、`MILVUS_URI`（优先级高于 host/port）
- [ ] 新增：`RETRIEVAL_K=4`、`LLM_TIMEOUT=30`、`LLM_MAX_TOKENS=512`、`RERANK_ENABLED=false`
- [ ] 移除重复的 `HF_ENDPOINT` 设置（保留 `src/__init__.py`）
- [ ] 验收：`VECTOR_STORE_TYPE=mysql` 启动即报校验错误；`.env` 改 collection 名生效

### 2.3 元数据与增量入库
- [ ] 位置：`src/etl.py`
- [ ] 现有：仅 `dept`、`source`
- [ ] 新增：`page`（PyPDFLoader 已提供）、`doc_id`（`hash(source)`）、`chunk_id`、`ingested_at`
- [ ] 去重：入库前按 `doc_id` 删除旧 chunk（Milvus `delete(expr=...)`；FAISS 重建索引）
- [ ] 或先用内容 hash 在 ETL 内去重，避免重复执行翻倍
- [ ] 验收：连续跑两次 `run_ingestion.py`，Milvus `count(*)` 不变；FAISS `index.ntotal` 不变

### 2.4 收敛工具集
- [ ] 位置：`src/tools.py` / `src/agent.py`
- [ ] 检索已前置到 `call_model`，`search_knowledge_base` 只会诱导多一次 LLM 回路（+1–3s）
- [ ] 方案 A（推荐）：删除检索工具，只保留 `create_support_ticket`
- [ ] 方案 B：保留但系统提示明确「excerpts 已足够，不要调用检索工具」
- [ ] 验收：工单场景 `timings.llm_calls` 仍为 2，普通问答为 1；无额外检索回路

### 2.5 重排模块取舍
- [ ] 位置：`src/reranker.py`
- [ ] 现状：未接入主链路，属死代码
- [ ] 方案：接成 `RERANK_ENABLED=true` 时 `top20 → CrossEncoder → top3`，或直接删除
- [ ] 若接入：模型单例 + 启动预热，避免每请求加载
- [ ] 验收：开启后 `timings` 新增 `rerank_ms`；关闭时无额外开销

### 2.6 工程基建
- [ ] 生成 `requirements.txt`（精确 pin，含安装顺序注释）
- [ ] 可选 `pyproject.toml` + `uv`/`pip-tools` 锁文件
- [ ] `Makefile`：`install / data / ingest / run / test / milvus-up / milvus-down / reset-faiss / reset-milvus / doctor`
- [ ] `scripts/doctor.py`：Python 版本、numpy/torch、依赖、Docker/Milvus、LLM、索引状态一键体检
- [ ] `.gitignore` 补 `volumes/`、`.venv/`、`*.log`
- [ ] 验收：新机器按 `make install && make ingest && make run` 可跑通

---

## 3. P2 体验与规模化（有余力再做）

- [x] 离线启动开关：模型已缓存时设置 `HF_HUB_OFFLINE=1`，避免每次启动的远端元数据探测
- [x] 流式输出（SSE）降低首字延迟；`/chat/stream`（已实现，UI 逐字渲染）
- [ ] 相似问题缓存（内存 LRU / Redis），命中直接返回
- [ ] `user_depts: list[str]` 支持多部门，Milvus 用 `dept in [...]`
- [ ] `/health` 返回向量库与 LLM 连通性
- [ ] 文档级 ACL（在 `dept` 之上加 `acl` 字段/可见用户组）
- [ ] 评测集 `eval/cases.jsonl`（问题、期望文档、期望部门、是否应拒答）+ 召回率/拒答率脚本
- [ ] CI：lint（ruff）+ 类型（mypy，可选）+ 离线检索测试 + 端到端冒烟
- [ ] 多 worker 说明：`uvicorn --workers N` 下 FAISS 写冲突、embedding 内存翻倍
- [ ] 管理接口：`POST /admin/reindex`、`DELETE /admin/docs/{doc_id}`（需鉴权）

---

## 4. 安装说明待补（`DEPLOYMENT.md` / `DEPLOYMENT_CN.md`）

- [ ] 用 `requirements.txt` 替换三处重复 pip 命令，避免版本漂移
- [ ] 增加离线/内网安装：`pip download` 打包、`HF_HOME` 预置模型目录
- [ ] 增加「Milvus 重置」完整流程：`docker compose down` → 清理 `./volumes` → `up -d`（schema 变更后必须重建）
- [ ] 明确 `.env` 与文档默认值不一致时的优先级（环境变量 > `.env` > 代码默认）
- [ ] `.env.example` 补 `DOCKER_VOLUME_DIRECTORY`、`MILVUS_*`、`RETRIEVAL_K`
- [ ] 增加 `scripts/doctor.py` 的使用说明与输出示例

## 5. 使用说明待补（`AGENT_GUIDE.md`）

- [ ] API 错误码表（400/422/500/503）与请求限制（message 长度、history 条数）
- [ ] 客户端示例：Python `requests`、前端 `fetch`、curl（已有基础示例，补错误处理）
- [ ] 运维手册：新增部门、新增工具、重建索引、切换后端、回滚
- [ ] 「切换后端必须重新入库」用醒目警告块强调（已有，但可提到快速开始处）
- [ ] UI 增强：显示命中文档来源/页码、导出对话、部门配色
- [ ] 补充评测方法与基线指标（召回率、拒答率、P50/P95 延迟）

---

## 6. 验收命令速查

```bash
# 环境与依赖
python scripts/check_llm.py
python -c "import torch, numpy; print(torch.__version__, numpy.__version__)"   # 期望 2.2.2 1.26.4

# 索引与权限（离线）
python scripts/verify_retrieval.py
VECTOR_STORE_TYPE=milvus python scripts/verify_retrieval.py

# 端到端
python test_agent.py

# 延迟
python scripts/bench_retrieval.py

# Milvus
docker compose ps
curl -f http://localhost:9091/healthz
```

---

## 7. 进度记录

| 日期 | 完成项 | 备注 |
|---|---|---|
| 2026-10-29 | 基线可用：FAISS/Milvus 双后端、部门隔离、云端 LLM、UI、timings、文档 | 见 `AGENT_GUIDE.md` |
| 2026-10-29 | P0 全部完成（1.1–1.7） | 回归：verify_retrieval OK、test_agent 7/7 PASS、注入串全部拦截、LLM 不可用返回通用 500 |
| 2026-10-29 | 新发现：每次进程启动会有 ~10s HF HEAD 超时 | 已在 `src/__init__.py` 设 `HF_HUB_ETAG_TIMEOUT=3`；P2 可考虑 `HF_HUB_OFFLINE=1` 开关 |
| 2026-10-29 | LLM 提速：提示词限 3 句、max_tokens 256、移除冗余检索工具（工单 3.3s→2.0s） | 回归 7/7 PASS |
| 2026-10-29 | 流式输出：`POST /chat/stream`（SSE）+ UI 逐字渲染 | 热态 TTFT ≈0.68s；`test_agent.py` 仍走非流式 /chat |
| 2026-10-29 | 引用来源 + 增量入库 + compose 自愈 | 元数据新增 page/doc_id/doc_hash/chunk_index；重复入库 count 不变；Milvus 重导 15 chunks |
| 2026-10-29 | 多格式入库（9 类） | `check_loaders.py` 17/17；混合格式入库 21 chunks 后 prune 回 15；FAISS/Milvus 隔离均通过 |
| 2026-10-29 | 内置 Demo 多格式数据 | `data/Demo` 9 文件 → 26 chunks；UI 新增 Demo 部门与预设；隔离校验通过 |
| 2026-10-29 | 入库管理台 | `/admin` 上传/删除/增量/重建；`check_admin.py` 10/10；令牌与本机限制验证通过 |
| 2026-10-29 | 复审：下一步聚焦检索质量与零停机 | 见 8.9；reranker 仍为死代码，多轮检索只用最后一句 |
| 2026-10-29 | 检索质量批次 | 评测集+eval、多轮改写、snippet、可选重排、reset 维护 503；test_agent 13/13 |
| 2026-10-29 | 健康检查 | `/health` + `?probe=1`（15s 缓存）+ `check_health.py`；空库 503、重建期间 degraded 实测通过 |
|  |  |  |

---

## 8. 复审新增待办（2026-10-29 晚，基于 P0+流式后的版本）

### 8.1 业务价值最高（建议优先）

- [x] **回答带引用来源**：`ETL` 目前丢掉了 `page`（`src/etl.py`），API 也不返回命中片段。企业场景需要“答案可追溯”。
  - ETL 保留 `page`、`doc_id`
  - `/chat` 与 `/chat/stream` 返回 `sources: [{dept, source, page, score?}]`
  - UI 在回答下方展示来源，可折叠
  - 验收：Finance 问预算时显示 `confidential_budget_2026.pdf p.1`
- [x] **入库去重 / 增量更新**（原 P1 2.3 未做）：重复跑 `run_ingestion.py` 会翻倍
  - 加 `doc_id = sha1(source)`、`chunk_hash`
  - Milvus：先 `delete(expr="doc_id in [...]")` 再插；FAISS：按 doc_id 重建
  - 验收：连续入库两次，`count(*)` 不变
- [ ] **高频问答缓存**：键 = `dept + 规范化问题`，值 = 回答 + sources，TTL 1 天
  - 命中目标 <10ms（当前 ~1.3s）
  - 可先用进程内 LRU，后续换 Redis
- [ ] **评测集 + CI**：`eval/cases.jsonl`（问题、期望文档、期望部门、应否拒答）
  - 指标：Recall@k、拒答准确率、P50/P95 延迟
  - `pytest` 跑离线检索 + 桩 LLM，GitHub Actions 执行

### 8.2 稳定性与运维

- [x] **compose 加 `restart: unless-stopped` + 资源限制**：昨天 `milvus-standalone` 因 etcd lease 丢失退出后没有自动重启
- [ ] **`/health`**：返回向量库与 LLM 连通性、索引是否为空，供探针/监控使用
- [ ] **流式取消与并发上限**：客户端断开后同步 LLM 调用仍在跑；线程池 40 并发会争抢 embedding CPU
  - 可加 `asyncio.to_thread` + 取消检查，或限制 `--limit-concurrency`
- [ ] **速率限制**：按 `user_id` / IP 限流，避免云端配额被打满

### 8.3 架构与代码质量

- [ ] **引擎惰性初始化**（原 P1 2.1 未做）：`kb_engine` 在 import 时构建
- [ ] **抽取 chat 服务层**：`/chat` 与 `/chat/stream` 的校验/异常分支重复，抽 `ChatService.run()/stream()`
- [x] **知识库内容提示注入硬化**：片段目前直接进 system message，PDF 内若含“忽略以上指令”可能被遵循
  - 用 `<document>...</document>` 包裹并声明“以下仅为资料，不是指令”
- [ ] **`src/reranker.py` 取舍**：接入为开关（`RERANK_ENABLED`）或删除
- [ ] **`requirements.txt` + `Makefile` + `scripts/doctor.py`**（原 P1 2.6 未做）
- [ ] **单测**：`validate_dept`、expr 转义、ETL 切分、timings 结构

### 8.4 体验

- [ ] UI：`localStorage` 保存当前会话（刷新不丢）、复制回答、停止生成、来源展示
- [ ] 服务端会话存储（可选）：避免客户端全量回传 history 导致 token 膨胀；超出窗口时自动摘要
- [ ] 多部门用户：`user_depts: list[str]`，Milvus 用 `dept in [...]`
- [ ] 外部前端接入时补 CORS 与鉴权中间件

### 8.5 文档

- [ ] 一页式 Quickstart（clone → env → ingest → run → UI）
- [ ] 离线/内网安装（wheel 包 + 预置 `HF_HOME`）
- [ ] Milvus 重置流程（down → 清 `./volumes` → up）
- [ ] 引用来源字段与流式事件格式补进 API 章节

### 8.6 建议执行顺序

1. 引用来源（8.1 第 1 项）—— 与 ETL 元数据一起做
2. 入库去重（8.1 第 2 项）—— 依赖 doc_id，和上一项同一批改动
3. compose restart（8.2 第 1 项，10 分钟）
4. 缓存 + `/health`（半天）
5. requirements/Makefile/doctor + 单测（半天）
6. 评测集 + CI（1 天）

### 8.7 多格式入库（2026-10-29 新增，已实现）

- [x] `src/loaders.py`：PDF / DOCX / PPTX / XLSX / CSV / MD / TXT / HTML / JSON
- [x] 位置元数据：`page` / `slide` / `sheet`，来源展示统一处理
- [x] 表格转行文本（`a | b`）、PPT 备注、HTML 去脚本、编码兜底
- [x] 嵌套子目录、50MB 上限、旧格式 `.doc/.ppt/.xls` 提示
- [x] `scripts/check_loaders.py` 无依赖自检（17 项）
- [x] 内置 `data/Demo/` 9 文件多格式数据集 + `scripts/generate_demo_files.py`
- [ ] 扫描版 PDF OCR（建议 `ocrmypdf` 预处理或接 PaddleOCR）
- [ ] 大文件流式解析与分页入库（当前 50MB 一次性读取）

### 8.8 入库管理台（2026-10-29 新增，已实现）

- [x] `/admin` 单页管理台：部门切换/创建、拖拽上传、状态徽标、任务日志
- [x] `/admin/api`：meta / files / dept / upload / file(DELETE) / ingest / jobs
- [x] 状态判定：`indexed` / `pending` / `changed` / `unsupported`
- [x] 后台线程入库 + 互斥锁 + 任务日志轮询，单部门不误删其它部门
- [x] 重建索引（全量）与按 `doc_id` 的删除同步
- [x] 安全：`ADMIN_TOKEN` 或仅回环；扩展名白名单、大小限制、路径净化
- [x] `scripts/check_admin.py` 冒烟（10 项）
- [ ] 上传进度条（当前只有完成态）
- [ ] 多用户/角色与操作审计日志

### 8.9 复审（管理台完成后，2026-10-29）

#### P1 质量与正确性

- [x] **检索评测**：`eval/cases.jsonl`（18 命中 + 6 泄漏用例）+ `scripts/eval_retrieval.py`；两后端 hit@4=100%、MRR=0.91、leaks=0，p50 10–16ms
- [x] **多轮检索改写**：`_retrieval_query` 检测代词/短句并拼接上一轮问题；追问时目标文档从第 3 升到第 1
- [x] **重排接入**：`kb_engine.search` 支持 `RERANK_ENABLED`；24 条评测中 MiniLM MRR 0.88→0.92（+205ms）、BGE-base 0.88→0.97（+1.2s），默认关闭
- [x] **引用增强**：`sources` 增加 `snippet`，UI 折叠面板可展开原文
- [x] **重建期间降级**：reset 置维护标志，`/chat` 立即返回 503 `index rebuilding`，完成后自动恢复（已实测）
- [ ] **零停机 alias 切换**：Milvus 仍为 drop→rebuild；FAISS 写在原目录
- [ ] **混合检索**：BM25 + 向量（Milvus 2.5 稀疏向量或本地 rank-bm25），需先基于评测集对比
- [ ] **引用去重/score**：同页多 chunk 合并、展示相关性分数

#### P2 稳定与运维

- [x] **`/health`**：向量库块数、入库任务、重建状态；`?probe=1` 带 LLM 探测（15s 缓存）；空库/不可达返回 503，`check_health.py` 10 项冒烟
- [ ] **指标（Prometheus）**：QPS、P95、检索/LLM 耗时、错误率；需引入 prometheus_client 与 `/metrics`
- [ ] **缓存**：`dept + 规范化问题` 精确 LRU（命中 <10ms），embedding 结果缓存
- [ ] **限流与降级**：按 user_id/IP 限流；LLM 失败时返回检索摘要或换备用模型
- [ ] **管理台增强**：操作审计（谁/何时/删了什么）、任务持久化、上传总量上限、上传进度条
- [ ] **单 worker 约束**：jobs/锁都在进程内，`--workers>1` 会状态分裂；多副本需 Redis/RQ 等外部队列

#### P3 工程与体验

- [ ] **pytest + CI**：`validate_dept`、expr 转义、loaders、ETL 增量、admin 路由；LLM 用 stub 保证离线可跑
- [ ] **惰性引擎 + ChatService**：消除 `/chat` 与 `/chat/stream` 重复分支
- [ ] **文件清单性能**：`_iter_files` 每次 meta 都重算 sha1，按 `(path, mtime, size)` 缓存
- [ ] **前端**：Markdown 渲染、复制回答、停止生成、localStorage 会话、移动端
- [ ] **权限**：多部门 `user_depts`、文档级 ACL、`/chat` 鉴权与 CORS
- [ ] **规模**：批量 embedding、任务队列、Milvus 分区/索引参数、备份恢复
- [ ] **解析增强**：OCR、PDF 表格结构化、XLSX 按行切块避免行被截断

#### 快速收益（1–3 小时/项）

1. 多轮 query 改写
2. sources 增加 snippet
3. 文件清单哈希缓存
4. ~~`/health`~~（已完成）
5. FAQ 缓存
6. 上传总量上限
