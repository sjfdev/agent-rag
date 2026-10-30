# Agent 优化计划（待办清单）

> 记录时间：2026-10-29；最后更新：2026-10-31
> 用途：继续开发的唯一入口。完成一项就把 `[ ]` 改成 `[x]`，并在第 7 节「进度记录」补一行。
> **下一次开发直接看第 10 节「明日待办（2026-11-01）」**；第 9 节是上一轮已完成的历史归档。
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
| Milvus 2.5.4 + 服务端稀疏 BM25 | 可用 | `GET /info` → `hybrid.engine=milvus`；`python scripts/check_hybrid_milvus.py` |
| 混合检索（进程内 BM25 回退） | 可用 | `python scripts/eval_retrieval.py`（`HYBRID_ENGINE=local`） |
| 健康检查 / Prometheus | 可用 | `python scripts/check_health.py --probe`；`curl /metrics` |
| 检索评测 | 通过 | `python scripts/eval_retrieval.py` → 18/18、MRR 0.89、0 泄漏 |

启动命令：

```bash
cd /Users/songjiangfeng/work/ai/agent-rag
conda activate agent-rag        # 或用 ~/miniconda3/envs/agent-rag/bin/python
VECTOR_STORE_TYPE=milvus uvicorn main:app --port 8000   # .env 仍写 faiss，用环境变量覆盖
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
- [x] 位置：`src/engine.py` `_LazyEngine` + `get_engine()` / `reset_engine()`
- [x] 问题：import 即加载 embedding；模型缺失时 import 直接失败
- [x] 方案：惰性代理（方法访问返回包装，`__dunder__` 抛 `AttributeError`）+ 显式获取/重建
- [x] FAISS/Milvus/OpenAI embeddings 改为函数内导入
- [x] 验收：`import src.engine` 12–14s → **0.72s**，且不加载模型

### 2.2 配置强类型与外置
- [x] 位置：`src/config.py`
- [x] `VECTOR_STORE_TYPE: Literal["faiss","milvus"]`
- [x] `EMBEDDING_BACKEND: Literal["local","openai"]`
- [x] 新增：`MILVUS_HOST`、`MILVUS_PORT`、`MILVUS_COLLECTION`、`MILVUS_URI`（优先级高于 host/port）
- [x] 新增：`RETRIEVAL_K=4`、`LLM_TIMEOUT=30`、`LLM_MAX_TOKENS=256`、`RERANK_ENABLED=false`
- [x] 新增（本轮）：`HYBRID_*`、`HEALTH_MIN_CHUNKS`、`METRICS_TOKEN`
- [x] 移除重复的 `HF_ENDPOINT` 设置（保留 `src/__init__.py`）
- [x] 验收：`VECTOR_STORE_TYPE=mysql` 启动即报校验错误；`.env` 改 collection 名生效

### 2.3 元数据与增量入库
- [x] 位置：`src/etl.py`
- [x] 新增：`page`/`slide`/`sheet`、`doc_id`、`doc_hash`、`kind`、`chunk_index`
- [x] 去重：内容哈希不变则跳过，变更则按 `doc_id` 替换（Milvus `delete(expr=...)`；FAISS 重建）
- [x] 验收：连续跑两次 `run_ingestion.py`，chunk 数不变；删除文件后由 prune 清理

### 2.4 收敛工具集
- [x] 位置：`src/tools.py` / `src/agent.py`
- [x] 检索已前置到 `call_model`，`search_knowledge_base` 只会诱导多一次 LLM 回路（+1–3s）
- [x] 已从 `agent_tools` 移除，只保留 `create_support_ticket`
- [x] `search_knowledge_base` 函数本体已删除
- [x] 验收：工单场景 `timings.llm_calls` 仍为 2，普通问答为 1；无额外检索回路

### 2.5 重排模块取舍
- [x] 位置：`src/reranker.py`
- [x] 已接入 `kb_engine.search`，`RERANK_ENABLED=true` 时先融合候选再重排取 top-k
- [x] 懒加载 + 失败降级（加载/推理失败保持原顺序），默认关闭
- [ ] `timings` 暂未单独暴露 `rerank_ms`（包含在 `retrieval_ms` 内）
- [x] 验收：24 条评测 MiniLM MRR 0.88→0.92（+205ms）、BGE 0.88→0.97（+1.2s）

### 2.6 工程基建
- [x] 生成 `requirements.txt`（精确 pin，含安装顺序注释）
- [ ] 可选 `pyproject.toml` + `uv`/`pip-tools` 锁文件（暂不需要）
- [x] `Makefile`：`install / data / ingest / run / test / lint / cov / milvus-up / milvus-down / milvus-logs / reset-faiss / reset-milvus / doctor / check / eval / clean`
- [x] `scripts/doctor.py`：Python 版本、numpy ABI、12 个关键包、embedding 缓存、数据目录、索引、Milvus 端口/版本、Docker、LLM 探针；`--no-llm` 跳过网络
- [x] `.gitignore` 补 `volumes/`、`logs/`、覆盖率与缓存目录
- [x] 验收：`make help`、`make lint`、`make doctor` 均已实测；`scripts/doctor.py --no-llm` 输出 `RESULT: OK`

---

## 3. P2 体验与规模化（有余力再做）

- [x] 离线启动开关：模型已缓存时设置 `HF_HUB_OFFLINE=1`，避免每次启动的远端元数据探测
- [x] 流式输出（SSE）降低首字延迟；`/chat/stream`（已实现，UI 逐字渲染）
- [x] 相似问题缓存：`src/cache.py`（LRU+TTL，键 `dept + 规范化问题`），另有查询向量缓存
- [x] `user_depts: list[str]` 支持多部门（Milvus `dept in [...]`，FAISS 后过滤，BM25 逐部门 RRF）
- [x] `/health` 返回向量库 / 入库任务 / 重建 / LLM 探测 / 缓存 / 混合引擎
- [x] 文档级 ACL：`src/acl.py`（`ACL_ENABLED` / `ACL_FILE`，用户与 `group:xxx`）
- [x] 评测集 `eval/cases.jsonl` + `scripts/eval_retrieval.py`（hit@k / MRR / 泄漏）
- [x] CI：`.github/workflows/ci.yml`（ruff + pytest + 覆盖率）
- [ ] 多 worker 说明：`uvicorn --workers N` 下 FAISS 写冲突、embedding 内存翻倍
- [x] 管理接口：`/admin/api` 上传/删除/入库/重建/任务/审计

---

## 4. 安装说明待补（`DEPLOYMENT.md` / `DEPLOYMENT_CN.md`）

- [x] 用 `requirements.txt` 替换三处重复 pip 命令，避免版本漂移
- [ ] 增加离线/内网安装：`pip download` 打包、`HF_HOME` 预置模型目录
- [x] 增加「Milvus 重置」完整流程：`docker compose down` → 清理 `./volumes` → `up -d`（schema 变更后必须重建）
- [x] 明确 `.env` 与文档默认值不一致时的优先级（环境变量 > `.env` > 代码默认）
- [x] `.env.example` 补 `DOCKER_VOLUME_DIRECTORY`、`MILVUS_*`、`RETRIEVAL_K`
- [x] `scripts/doctor.py` 已在 Quickstart / Makefile / AGENT_GUIDE 中说明；输出示例：`[OK] Python version 3.11.13` … `RESULT: OK`

## 5. 使用说明待补（`AGENT_GUIDE.md`）

- [x] API 错误码表（400/422/500/503）与请求限制（message 长度、history 条数）
- [x] 客户端示例：AGENT_GUIDE 已有 curl / Python / SSE 示例与错误码表
- [ ] 运维手册：新增部门、新增工具、重建索引、切换后端、回滚
- [x] 「切换后端必须重新入库」已用警告块强调
- [x] UI：来源面板显示文件/页码/原文、缓存命中标签；导出对话与部门配色未做
- [x] 补充评测方法与基线指标（hit@k、MRR、泄漏、P50/P95）

---

## 6. 验收命令速查

```bash
# 环境与依赖
python scripts/check_llm.py
python -c "import torch, numpy; print(torch.__version__, numpy.__version__)"   # 期望 2.2.2 1.26.4

# 索引与权限（离线）
python scripts/verify_retrieval.py
VECTOR_STORE_TYPE=milvus python scripts/verify_retrieval.py

# 检索评测（命中 / MRR / 泄漏）
python scripts/eval_retrieval.py
VECTOR_STORE_TYPE=milvus python scripts/eval_retrieval.py

# 混合检索
python scripts/check_hybrid.py
VECTOR_STORE_TYPE=milvus python scripts/check_hybrid_milvus.py

# 健康检查与指标
python scripts/check_health.py --probe
curl -s http://localhost:8000/metrics | grep '^agent_rag_'

# 管理台 / 解析器
python scripts/check_admin.py
python scripts/check_loaders.py

# 端到端（需要服务 + LLM）
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
| 2026-10-29 | 可观测与混合检索 | `/metrics`（9 组指标、可选 token）+ 进程内 BM25/RRF（默认开启、后台预热、12 项单测）；评测 MRR 0.89 |
| 2026-10-29 | 混合检索能力自适应 | `HYBRID_ENGINE=auto\|local\|milvus` + 版本/稀疏字段/BM25 索引探测；2.5+ 走服务端 hybrid_search+RRF，否则回退；`/info`、`/health`、`/metrics` 暴露实际引擎；42 项单测 + 2.5 端到端脚本 |
| 2026-10-30 | Milvus 升级 2.5.4 + 服务端稀疏 BM25 | colima 配置镜像源后拉取成功；备份数据卷；`--reset` 先 drop 再建；实测 18/18、MRR 0.89、P50 24ms；详见 `WORKLOG.md` |
| 2026-10-30 | 性能与清理：批量删除、`doc_summary()`、元数据/哈希缓存、检索分支收敛、删除死代码 | `/admin/api/meta` 451ms→6ms；`check_hybrid` 49/49；test_agent 13/13；评测 18/18 |
| 2026-10-30 | 9.1 正确性：零停机重建（Milvus alias / FAISS 原子替换）、先写新后删旧、多轮相似度护栏 | 重建期间 0 失败请求；问答/更新/删除无重复 chunk；`check_hybrid` 57/57、`check_hybrid_milvus` 19/19；详见 `WORKLOG.md` |
| 2026-10-30 | 9.2 缓存：FAQ 答案缓存 + 查询向量缓存 | 重复问答 4044ms→4ms、重复检索 1434ms→9ms；`check_hybrid` 69/69、test_agent 18/18；见 `WORKLOG.md` 5.8 |
| 2026-10-30 | 9.3.1 pytest + ruff + GitHub Actions | 离线 83 → 127 项；新增 `EMBEDDING_BACKEND=fake`、`requirements-test.txt`、`ci.yml`；修复 admin 非法部门 500→400 |
| 2026-10-30 | 9.3.2 ChatService + 惰性引擎 | `/chat` 与 `/chat/stream` 收敛到 `src/chat_service.py`；`import src.engine` 12–14s→0.72s；97 项测试 |
| 2026-10-30 | 9.3.3 / 9.3.4 管理台加固与权限 | 审计、任务持久化、上传配额/进度；API Key、多部门、文档 ACL；127 项测试 |
| 2026-10-30 | 测试覆盖率 73%→92% | 246 项；新增 engine(Milvus 假客户端)/health/metrics/reranker/agent/ETL/admin 补充测试 |
| 2026-10-30 | 工程基建与稳定性 | Makefile、doctor、限流（429+Retry-After）、请求超时（504）；265 项测试 |
| 2026-10-31 | Quickstart + 计划校正 | 新增 `QUICKSTART.md`；勾掉 35 项已实现条目，剩余待办收敛到第 10 节 |

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
- [x] **高频问答缓存**：`src/cache.py`（LRU+TTL，键 `dept + 规范化问题`）
  - 命中目标 <10ms（当前 ~1.3s）
  - 可先用进程内 LRU，后续换 Redis
- [x] **评测集**：`eval/cases.jsonl`（18 命中 + 6 泄漏）+ `scripts/eval_retrieval.py`
- [x] **CI**：`.github/workflows/ci.yml`（ruff + pytest + 覆盖率）
  - 指标：Recall@k、拒答准确率、P50/P95 延迟
  - `pytest` 跑离线检索 + 桩 LLM，GitHub Actions 执行

### 8.2 稳定性与运维

- [x] **compose 加 `restart: unless-stopped` + 资源限制**：昨天 `milvus-standalone` 因 etcd lease 丢失退出后没有自动重启
- [x] **`/health`**：向量库与 LLM 连通性、索引是否为空、入库/重建状态；`?probe=1` 带 LLM 探测
- [x] **请求超时**：`CHAT_TIMEOUT` 限制整体耗时（非流式用线程池 `result(timeout)`，流式逐帧检查），超时返回 504 `timeout`
- [ ] **客户端断开即取消**：客户端断开后同步 LLM 调用仍会跑完；彻底取消需要异步客户端或协作式取消
  - 可加 `asyncio.to_thread` + 取消检查，或限制 `--limit-concurrency`
- [x] **速率限制**：`RATE_LIMIT_PER_MINUTE`（默认 0 关闭），滑动窗口，超限 429

### 8.3 架构与代码质量

- [x] **引擎惰性初始化**：`get_engine()`/`reset_engine()` + 代理，import 0.7s
- [x] **抽取 chat 服务层**：`src/chat_service.py` 的 `run()/open_stream()`
- [x] **知识库内容提示注入硬化**：片段目前直接进 system message，PDF 内若含“忽略以上指令”可能被遵循
  - 用 `<document>...</document>` 包裹并声明“以下仅为资料，不是指令”
- [x] **`src/reranker.py` 取舍**：已接入为开关（`RERANK_ENABLED`，默认关闭）
- [x] **`requirements.txt`**（精确 pin，含安装顺序注释）
- [x] **`Makefile` + `scripts/doctor.py`**
- [x] **单测框架**：`tests/` 246 项、覆盖率 92%（engine 86%），见 9.3.1

### 8.4 体验

- [ ] UI：`localStorage` 保存当前会话（刷新不丢）、复制回答、停止生成
- [x] UI：来源展示（折叠面板 + 原文 `snippet`）
- [ ] 服务端会话存储（可选）：避免客户端全量回传 history 导致 token 膨胀；超出窗口时自动摘要
- [x] 多部门用户：`user_depts: list[str]`，Milvus 用 `dept in [...]`
- [ ] 外部前端接入时补 CORS（鉴权已可选：`CHAT_API_KEY`）

### 8.5 文档

- [x] 本轮变更记录（`WORKLOG.md`）+ 安装/使用/FAQ 已同步
- [x] 一页式 Quickstart：`QUICKSTART.md`（安装 → 配置 → 入库 → 启动 → 验证 + 排错表）
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
- [x] 上传进度条（XHR upload progress）
- [x] 操作审计日志（`logs/admin_audit.log`，`GET /admin/api/audit`）；多角色未做

### 8.9 复审（管理台完成后，2026-10-29）

#### P1 质量与正确性

- [x] **检索评测**：`eval/cases.jsonl`（18 命中 + 6 泄漏用例）+ `scripts/eval_retrieval.py`；两后端 hit@4=100%、MRR=0.91、leaks=0，p50 10–16ms
- [x] **多轮检索改写**：`_retrieval_query` 检测代词/短句并拼接上一轮问题；追问时目标文档从第 3 升到第 1
- [x] **重排接入**：`kb_engine.search` 支持 `RERANK_ENABLED`；24 条评测中 MiniLM MRR 0.88→0.92（+205ms）、BGE-base 0.88→0.97（+1.2s），默认关闭
- [x] **引用增强**：`sources` 增加 `snippet`，UI 折叠面板可展开原文
- [x] **重建期间降级**：reset 置维护标志，`/chat` 立即返回 503 `index rebuilding`，完成后自动恢复（已实测）
- [x] **零停机 alias 切换**：Milvus 写影子集合并切换 alias；FAISS 临时目录原子替换；见 9.1.1
- [x] **混合检索（本地 BM25）**：`src/lexical.py` 中英文分词 + RRF 融合，默认开启；24 条评测 MRR 0.88→0.89，精确标识符类查询收益明显；后台预热、入库后预热、按部门缓存
- [x] **能力自适应混合检索**：`HYBRID_ENGINE=auto` 探测 Milvus 版本与集合稀疏字段/BM25 索引，2.5+ 走服务端 `hybrid_search`+RRF，否则回退进程内 BM25；`milvus` 显式模式下能力不足启动即报错；`/info`、`/health`、`/metrics` 暴露实际引擎
- [x] **升级 Milvus 到 2.5 并重建集合**：已升级 2.5.4（先备份数据卷），`run_ingestion.py --reset` 重建，实测 `hybrid.engine=milvus`、端到端 18/18
- [ ] **引用去重/score**：同页多 chunk 合并、展示相关性分数

#### P2 稳定与运维

- [x] **限流与超时**：`src/ratelimit.py` 滑动窗口（按转发 IP 或 user_id，默认关闭），超限 429 + `Retry-After`；`CHAT_TIMEOUT` 为 `/chat` 与 `/chat/stream` 增加整体截止时间，超时映射 504 `timeout`
- [ ] **降级回答**：LLM 失败时返回检索摘要或备用模型（目前只返回 503/504）

- [x] **`/health`**：向量库块数、入库任务、重建状态；`?probe=1` 带 LLM 探测（15s 缓存）；空库/不可达返回 503，`check_health.py` 10 项冒烟
- [x] **指标（Prometheus）**：`/metrics` 覆盖请求/错误、端到端与检索/LLM 耗时、首 token、索引块数、入库任务、重建状态；可选 `METRICS_TOKEN`
- [ ] **多进程指标聚合**：多 worker 时各进程独立，需 Pushgateway 或按 instance 聚合；指标也未持久化
- [x] **N+1 查询与重复哈希**：批量删除（每 100 doc 一次）、`doc_summary()` 一次聚合替代逐文件查询、元数据缓存 5s、文件哈希按 stat 缓存；`/admin/api/meta` 451ms → 6ms
- [x] **缓存**：答案 LRU + 查询向量缓存，命中 4ms
- [x] **限流**：`RATE_LIMIT_PER_MINUTE`（429 + Retry-After）
- [x] **管理台增强**：审计、任务持久化、上传总量上限、进度条
- [ ] **单 worker 约束**：jobs/锁都在进程内，`--workers>1` 会状态分裂；多副本需 Redis/RQ 等外部队列

#### P3 工程与体验

- [x] **pytest + CI**：`validate_dept`、expr 转义、loaders、ETL 增量、admin 路由；LLM 用 stub 保证离线可跑
- [x] **惰性引擎 + ChatService**：已消除重复分支
- [x] **文件清单性能**：`src/hashing.py` 按 `(path, mtime_ns, size)` 缓存 sha1；`doc_summary()` 缓存 5s；`/admin/api/meta` 451ms → 6ms
- [ ] **前端**：Markdown 渲染、复制回答、停止生成、localStorage 会话、移动端
- [x] **权限**：多部门、文档级 ACL、`/chat` API Key；CORS 未做
- [ ] **规模**：批量 embedding、任务队列、Milvus 分区/索引参数、备份恢复
- [ ] **解析增强**：OCR、PDF 表格结构化、XLSX 按行切块避免行被截断

#### 快速收益（1–3 小时/项）

1. ~~多轮 query 改写~~（已完成）
2. ~~sources 增加 snippet~~（已完成）
3. ~~文件清单哈希缓存~~（已完成）
4. ~~`/health`~~（已完成）
5. FAQ 缓存（见 9.2）
6. 上传总量上限（见 9.3.3）

---

## 9. 上一轮待办归档（2026-10-31，已完成）

> 本节保留作为记录：9.1 正确性、9.2 缓存、9.3 工程与运维均已完成（两项例外见下方）。
> 剩余未完成的两项已挪到第 10 节。下一次开发请看 **第 10 节**。

### 9.1 P1 正确性（优先，预计 3–4 小时）

#### 9.1.1 零停机重建（Milvus alias / FAISS 原子替换）
- [x] 位置：`src/admin_api.py::_run_job`、`src/engine.py`、`run_ingestion.py`
- [x] 方案（Milvus）：重建写入 `enterprise_kb__r<ts>` 影子集合；读仍走 live；提交时创建/切换 alias `enterprise_kb`，旧物理集合在提交后删除；失败则丢弃影子集合，live 不变
- [x] 方案（FAISS）：写入临时目录 → 校验可加载 → `os.replace` 原子替换（旧目录作为备份后删除）
- [x] 维护标志只在提交的切换瞬间置位（毫秒级），写入阶段完全不阻塞查询
- [x] 验收（已实测）：
  - 重建过程中持续请求 `/chat`：**0 个非 200**（Milvus 与 FAISS 各测 2 轮）
  - `/health` 全程 `ok`，chunk 数保持 26
  - `check_admin.py`、`test_agent.py`、两种后端隔离与评测全部通过
  - CLI `run_ingestion.py --reset` 与 `/admin` 重建都走同一路径

#### 9.1.2 变更文件「先写新、再删旧」
- [x] 位置：`src/etl.py::_process_file`、`src/engine.py::delete_stale_versions`
- [x] 方案：先 `add_documents()` 写入新版本（`doc_hash` 变化），成功后按 `doc_id + doc_hash != new` 删除旧 chunk；不再先删后写
- [x] Milvus 用 `delete_by_expr`（批量），FAISS 直接按 id 删除；两种后端都保持 `doc_id` 语义不变，无需重建
- [x] `doc_summary()` / `get_doc_hash()` 在重建期间读写已分离（写影子，读线上）
- [x] 验收（已实测）：上传 v1→v2 后 `updated=1`、chunk 数不变；删除后立即不可见
- [x] 注意：仍有一个小窗口（写入成功、旧版本删除前）可能短暂返回新旧两份；这是刻意的取舍——宁可重复，不丢文档

#### 9.1.3 多轮改写加相似度护栏
- [x] 位置：`src/agent.py::decide_followup` / `_retrieval_query`
- [x] 方案：代词（it/它 等）直接视为追问；指示词（this/这个/那）与一般问句都需要 embedding 余弦相似度达标（指示词 0.45、一般 0.55）
- [x] 判定逻辑抽成纯函数 `decide_followup(previous, current, similarity)`，无需加载模型即可单测
- [x] 验收（已实测）：
  - 追问 “Who approves it?” 命中 `coding_standards.pdf`
  - 换题 “What is the VPN policy?”（上一问是年假）不再继承上下文
  - 8 条新单测覆盖代词/指示词/换题/近义，`check_hybrid.py` 57 项全过
  - 评测 MRR 保持 0.89，无泄漏

### 9.2 P2 性能（预计 3 小时）

#### 9.2.1 FAQ 精确缓存
- [x] 位置：`src/cache.py`，接入 `main.py` 的 `/chat` 与 `/chat/stream`
- [x] 键：`dept + 规范化问题`（去空白/标点、小写）；值：`answer + sources`
- [x] LRU 512、TTL 1 天；**入库/删除/重建后清空**（`_touch()` 统一处理）
- [x] 只缓存单轮、无工具调用、无错误、`llm_calls == 1` 的回答
- [x] 旁路：`Cache-Control: no-cache` / `X-No-Cache: 1`
- [x] 验收（实测）：重复问题 **4044ms → 4.2ms**，`timings.cached=true`、`llm_calls=0`、`X-Cache: hit`；`/metrics` 新增 `agent_rag_cache_total{cache,result}`、`agent_rag_cache_entries{cache}`

#### 9.2.2 embedding 缓存
- [x] 位置：`src/embedding_cache.py`，由 `build_embeddings()` 统一包装（检索、多轮相似度、混合检索都走它）
- [x] 键：`(backend:model, query)`，换模型自动失效；LRU 2048（`EMBEDDING_CACHE_MAX_ENTRIES=0` 可关闭）
- [x] 入库 `embed_documents` 不缓存，避免占内存
- [x] 验收（实测）：重复检索 **1434ms → 9ms**；热态 `embed_query` 0.03ms，Milvus RPC 约 13ms

#### 9.2.3 FAISS 按部门索引（可选）
- [ ] 现状：全库检索后本地过滤，`pool = max(k*20, 50)`，部门多/数据偏斜时召回不足
- [ ] 方案：每个部门一个 FAISS 索引文件 + 默认 index 元数据
- [ ] 验收：新增“多部门大数据”基准，Recall@4 不低于 Milvus 模式；`verify_retrieval` 隔离仍通过

### 9.3 P3 工程与运维（预计 4–6 小时）

#### 9.3.1 pytest + CI

> **什么是 GitHub Actions**：GitHub 提供的一台临时机器。每次 push / PR，它会按
> `.github/workflows/ci.yml` 自动 checkout 代码、装依赖、跑 lint 和测试，结果直接显示在
> PR 页面上。作用是「防止把坏代码合进 main」——本地忘了跑测试时 CI 会拦下来。
> 它不部署、不碰生产数据；也可以关掉，只留本地 `pytest`。
- [x] 新建 `tests/`（validation / loaders / hybrid / etl / cache / admin / api / engine / health / metrics / agent / reranker / jobs / audit / acl / auth / ratelimit / errors / doctor），共 **265 个用例**，约 25 秒跑完
- [x] 离线可跑：新增 `EMBEDDING_BACKEND=fake` 确定性向量；LLM 用 `monkeypatch` 替换为 stub；向量库用临时 FAISS 目录（`conftest.py` 自动配置）
- [x] `pytest.ini` + `ruff.toml`；`requirements-test.txt` 只列测试所需依赖（不含 torch）
- [x] GitHub Actions `.github/workflows/ci.yml`：ruff → pytest(含覆盖率) → 上传 coverage
- [x] 同时修掉一个真实缺陷：admin 路由抛 `InvalidDepartment` 现在由全局处理器映射为 400（此前是 500）
- [x] 验收（本机）：`pytest -q` **265 passed**；`ruff check` 全绿；覆盖率 **92%**（engine 86%，reranker/metrics/runtime/tools 100%）
- [ ] CI 首次在 GitHub 上跑通（需要推送后在仓库 Actions 页面观察；本机没有 `gh`，本地已验证同样的命令全绿）

#### 9.3.2 `ChatService` 抽取 + 惰性引擎
- [x] 新增 `src/chat_service.py`：`prepare / run / open_stream` 统一校验、维护检查、答案缓存、异常分类、指标与日志；`/chat` 与 `/chat/stream` 各剩约 15 行
- [x] 新增 `src/schemas.py` 存放请求/响应模型；`ChatResponse` 增加 `request_id`，响应头 `X-Request-ID` 与 `X-Cache` 始终返回
- [x] 新增 `ChatFailure` 携带分类后的状态码/code/request_id，路由只负责映射成 HTTP 响应
- [x] `kb_engine` 改为惰性代理：方法访问返回轻量包装（LangGraph 在 import 时内省会触碰 `kb_engine.search`，直接构造会在 import 阶段加载模型），`get_engine()` / `reset_engine()` 显式获取与重建
- [x] `src/engine.py` 的 FAISS/Milvus/OpenAI embeddings 改为函数内导入
- [x] 验收：`import src.engine` **0.72s**（原 ~12–14s）、`from src.engine import kb_engine` 0.42s 且不构建；`src.agent` 4.6s、`main` 5.7s（原 ~12–13s）
- [x] 新增 `tests/test_chat_service.py`（14 项）：准备/维护 503/错误分类/缓存/失败与流式错误帧
- [x] 回归：`pytest -q` **97 passed**；`test_agent.py` 20 项全过；两后端评测 18/18；`check_hybrid` 69/69；`check_hybrid_milvus` 19/19

#### 9.3.3 管理台增强
- [x] 上传总量上限（`MAX_UPLOAD_TOTAL_MB`，默认 200MB），超限返回 413；管理台显示单文件与合计上限
- [x] 上传进度条（XMLHttpRequest `upload.onprogress`，fetch 不支持上传进度）
- [x] 操作审计 `src/audit.py`：创建部门/上传/删除/启动入库逐条写入 `logs/admin_audit.log`（JSON Lines），令牌只记录 sha1 前 8 位；`GET /admin/api/audit` 可查，管理台新增「操作审计」卡片
- [x] 任务持久化 `src/jobs.py`：任务与日志镜像到 `logs/ingest_jobs.json`（临时文件 + `os.replace` 原子写），重启后恢复；启动时把中断的 queued/running 任务标记为失败
- [x] 验收：`check_admin.py` 新增 3 项审计断言（全部通过）；`tests/test_admin.py` 新增配额/审计/重启恢复 3 项

#### 9.3.4 安全与权限
- [x] `/chat` 可选 API Key：`CHAT_API_KEY` 非空时要求 `X-API-Key` 或 `Authorization: Bearer`；`/chat/stream` 同样生效，`/health`、`/info`、`/metrics` 不受影响
- [x] 多部门：请求可传 `user_depts: list[str]`（与 `user_dept` 合并去重）；Milvus 用 `dept in [...]`，FAISS 本地多值过滤；进程内 BM25 逐部门检索后用 RRF 融合
- [x] 文档级 ACL：`src/acl.py` + `ACL_ENABLED` / `ACL_FILE`；规则按 source（可省略部门前缀）映射到用户或 `group:xxx`；未配置规则的文档保持部门内公开；文件 mtime 变化自动重载；检索后统一过滤，两种后端一致
- [x] 缓存安全：ACL 开启时缓存键包含用户组，避免高权限答案被低权限用户命中
- [x] 验收：`tests/test_acl.py`（9 项）、`tests/test_auth.py`（5 项）全部通过；`verify_retrieval.py` 增加 Tech+HR 多部门用例（两后端 OK）

### 9.4 建议排期

| 时段 | 内容 |
|---|---|
| 上午 | 9.1.1 零停机 + 9.1.2 安全替换（先做备份，改完跑全量回归） |
| 下午前半 | 9.2.1 FAQ 缓存 + 9.2.2 embedding 缓存 + 指标 |
| 下午后半 | 9.3.1 pytest + CI（这是后续所有改动的安全网） |
| 收尾 | 跑完整验证矩阵，更新 `WORKLOG.md` 与本文件进度记录 |

### 9.5 每次改完的回归清单

```bash
python test_agent.py
python scripts/check_hybrid.py
python scripts/check_hybrid_milvus.py
python scripts/check_health.py --probe
python scripts/check_admin.py
python scripts/check_loaders.py
VECTOR_STORE_TYPE=milvus python scripts/verify_retrieval.py
VECTOR_STORE_TYPE=faiss  python scripts/verify_retrieval.py
VECTOR_STORE_TYPE=milvus python scripts/eval_retrieval.py
VECTOR_STORE_TYPE=faiss  python scripts/eval_retrieval.py
```

通过标准：全部 `RESULT: OK`；评测 `18/18`、`leaks=0`、MRR ≥ 0.89。

---

## 10. 明日待办（2026-11-01）

> 起点：265 项测试全绿、覆盖率 92%、Milvus 2.5.4 + 服务端稀疏 BM25 在生产化配置下运行。
> 今天结束时的状态：`vector_store=milvus`、26 chunks、`hybrid.engine=milvus`、`/health=ok`。
> 本地 `.env` 仍写 `VECTOR_STORE_TYPE=faiss`；当前服务是**环境变量覆盖**为 milvus，改动前先确认。

### 10.1 P1 稳定性（预计 2–3 小时）

#### 10.1.1 LLM 失败降级（检索摘要兜底）
- [ ] 位置：`src/chat_service.py::run` / `_stream`，`src/errors.py`
- [ ] 现状：LLM 不可用或超时时只返回 `503 upstream_unavailable` / `504 timeout`，用户拿不到任何信息
- [ ] 方案：
  - 捕获 `is_upstream_error` / `ChatTimeout` 后，用本次已检索到的 `sources` 拼一段确定性摘要（前 3 条 snippet + 来源）
  - 响应带 `degraded=true` 与 `degraded_reason=llm_unavailable`，HTTP 仍 200（或按配置返回 503 + body）
  - 流式：先发一个 `warning` 事件，再发 `done`（`cached=false`, `degraded=true`）
  - 开关：`LLM_FALLBACK_ENABLED=true`（默认开），可回退到旧行为
- [ ] 注意：只对**已经完成检索**的请求降级；检索本身失败仍返回 503
- [ ] 验收：
  - 把 `LLM_API_BASE` 指向不可达地址，`/chat` 返回来源摘要而非空错误
  - `test_agent.py` 增加“LLM 不可用”用例；`pytest` 新增降级测试
  - `degraded` 不计入缓存（避免把兜底答案缓存一天）

#### 10.1.2 CORS（外部前端接入）
- [ ] 位置：`main.py`
- [ ] 现状：`/ui/`、`/admin` 同源可用；第三方页面调用 `/chat` 会被浏览器拦截
- [ ] 方案：`CORS_ORIGINS`（逗号分隔，默认空=不启用）→ `CORSMiddleware`
  - 只对 `/chat`、`/chat/stream`、`/info`、`/health` 放行；`/admin/api` 不开放
  - `allow_credentials` 与 `*` 互斥，需显式列出域名
- [ ] 验收：`curl -H 'Origin: http://example.com' -X OPTIONS` 返回正确的 `Access-Control-Allow-Origin`；默认配置下无 CORS 头

#### 10.1.3 CI 首跑确认
- [ ] 位置：GitHub 仓库 Actions 页面（本机没有 `gh`）
- [ ] 待确认：`lint-and-test` 是否通过、依赖安装是否缺包、缓存是否生效
- [ ] 若失败：把报错贴回，按缺失依赖补 `requirements-test.txt`
- [ ] 验收：main 分支最近一次 run 为绿色

### 10.2 P2 体验（预计 3 小时）

#### 10.2.1 会话持久化与前端体验
- [ ] `static/index.html`：`localStorage` 保存 `history` + 部门/用户，刷新不丢
- [ ] 复制回答按钮、停止生成（`AbortController` + 后端已有超时兜底）
- [ ] Markdown 渲染（仅安全子集，避免 XSS；先做粗体/列表/代码块）
- [ ] 验收：刷新后对话仍在；点停止后请求确实中断（Network 面板）；回答中的 `**粗体**` 正确渲染

#### 10.2.2 引用质量
- [ ] 同页多 chunk 合并为一条 source（保留最高分/最长 snippet）
- [ ] `sources` 增加 `score`（Milvus 有 distance；FAISS 用 L2 距离换算）
- [ ] UI 来源面板显示分数并按分数排序
- [ ] 验收：预算问题只出现一条 `confidential_budget_2026.pdf p.1`，不再重复；评测脚本不受影响

#### 10.2.3 检索可观测性补齐
- [ ] `timings` 增加 `rerank_ms`（目前混在 `retrieval_ms`）
- [ ] `bench_retrieval.py` 增加重排开关对比
- [ ] 验收：`RERANK_ENABLED=true` 时响应含独立 `rerank_ms`；关闭时字段为 0

### 10.3 P3 规模化与文档（有余力）

#### 10.3.1 文档补全
- [ ] `README.md`（仓库首页）：一句话定位、架构图、5 分钟 Quickstart 链接、能力表、许可证
- [ ] 运维手册：新增部门、新增工具、重建索引、切换后端、回滚、备份
- [ ] 离线/内网安装：`pip download` 打包 + 预置 `HF_HOME` 模型目录
- [ ] 明确多 worker 约束（FAISS 单写者、进程内缓存/任务/限流、指标聚合）
- [ ] 验收：新机器只读 README → QUICKSTART 可跑通

#### 10.3.2 前端/服务端会话
- [ ] 服务端会话存储（可选）：避免客户端全量回传 history；超窗口自动摘要
- [ ] 外部前端 CORS 已在 10.1.2 覆盖
- [ ] 验收：连续 20 轮对话后请求体不再线性增长

#### 10.3.3 解析增强
- [ ] 扫描版 PDF OCR（`ocrmypdf` 预处理或 PaddleOCR）
- [ ] XLSX 按行切块（当前整表可能被截断）
- [ ] 大文件流式解析（当前 50MB 一次性读入）
- [ ] 验收：新增扫描版样例可检索；`check_loaders.py` 覆盖新格式

#### 10.3.4 规模化（按需）
- [ ] 9.2.3 FAISS 按部门索引（仅当单部门数据量 > 数万时才需要）
- [ ] 批量 embedding（入库逐文档 → batch）
- [ ] Redis 版缓存/限流/任务队列（多副本时）
- [ ] 客户端断开即取消（异步 LLM 客户端）
- [ ] 验收：对应基准脚本给出前后对比

### 10.4 建议排期

| 时段 | 内容 |
|---|---|
| 上午 | 10.1.1 LLM 降级（含测试）+ 10.1.2 CORS |
| 下午前半 | 10.2.1 前端体验 + 10.2.2 引用合并/分数 |
| 下午后半 | 10.3.1 README + 运维手册 |
| 收尾 | 跑 9.5 回归清单，更新 `WORKLOG.md` 与本文件进度记录 |

### 10.5 明日回归清单（同 9.5，另加）

```bash
pytest -q
ruff check src tests main.py run_ingestion.py scripts
python scripts/doctor.py
# 降级验证：LLM 指向不可达地址后
curl -s -X POST localhost:8000/chat -H 'Content-Type: application/json' \
  -d '{"user_id":"u1","user_dept":"Tech","message":"What is the code review process?"}' | head -c 300
```

