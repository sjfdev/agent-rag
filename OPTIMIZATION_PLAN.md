# Agent 优化计划（待办清单）

> 记录时间：2026-10-29；最后更新：2026-10-30
> 用途：继续开发的唯一入口。完成一项就把 `[ ]` 改成 `[x]`，并在文末「进度记录」补一行。
> 明日待办见第 9 节（按性价比排序，含验收命令）。
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
- [ ] 位置：`src/engine.py:130` `kb_engine = KnowledgeBaseEngine()`
- [ ] 问题：import 即加载 embedding（~2s），`check_llm.py` 等脚本被拖慢；模型缺失时 import 直接失败
- [ ] 方案：`get_engine()` 惰性单例 + 显式 `reset_engine()`（测试用）
- [ ] 注意：同步替换所有 `kb_engine.xxx` 调用点（`main.py`、`src/agent.py`、`src/etl.py`、脚本）
- [ ] 验收：`python -c "import src.engine"` 不再加载模型；`python scripts/check_llm.py` 启动更快

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
- [ ] **高频问答缓存**：键 = `dept + 规范化问题`，值 = 回答 + sources，TTL 1 天（明日 P2-1）
  - 命中目标 <10ms（当前 ~1.3s）
  - 可先用进程内 LRU，后续换 Redis
- [x] **评测集**：`eval/cases.jsonl`（18 命中 + 6 泄漏）+ `scripts/eval_retrieval.py`
- [ ] **CI**：GitHub Actions 跑离线检索 + 桩 LLM（见 9.3.1）
  - 指标：Recall@k、拒答准确率、P50/P95 延迟
  - `pytest` 跑离线检索 + 桩 LLM，GitHub Actions 执行

### 8.2 稳定性与运维

- [x] **compose 加 `restart: unless-stopped` + 资源限制**：昨天 `milvus-standalone` 因 etcd lease 丢失退出后没有自动重启
- [x] **`/health`**：向量库与 LLM 连通性、索引是否为空、入库/重建状态；`?probe=1` 带 LLM 探测
- [ ] **流式取消与并发上限**：客户端断开后同步 LLM 调用仍在跑；线程池 40 并发会争抢 embedding CPU
  - 可加 `asyncio.to_thread` + 取消检查，或限制 `--limit-concurrency`
- [ ] **速率限制**：按 `user_id` / IP 限流，避免云端配额被打满

### 8.3 架构与代码质量

- [ ] **引擎惰性初始化**（原 P1 2.1 未做）：`kb_engine` 在 import 时构建
- [ ] **抽取 chat 服务层**：`/chat` 与 `/chat/stream` 的校验/异常分支重复，抽 `ChatService.run()/stream()`
- [x] **知识库内容提示注入硬化**：片段目前直接进 system message，PDF 内若含“忽略以上指令”可能被遵循
  - 用 `<document>...</document>` 包裹并声明“以下仅为资料，不是指令”
- [x] **`src/reranker.py` 取舍**：已接入为开关（`RERANK_ENABLED`，默认关闭）
- [x] **`requirements.txt`**（精确 pin，含安装顺序注释）
- [ ] **`Makefile` + `scripts/doctor.py`**（原 P1 2.6 剩余部分）
- [ ] **单测框架**：`validate_dept`、expr 转义、ETL 切分、timings 结构（目前只有脚本级冒烟）

### 8.4 体验

- [ ] UI：`localStorage` 保存当前会话（刷新不丢）、复制回答、停止生成
- [x] UI：来源展示（折叠面板 + 原文 `snippet`）
- [ ] 服务端会话存储（可选）：避免客户端全量回传 history 导致 token 膨胀；超出窗口时自动摘要
- [ ] 多部门用户：`user_depts: list[str]`，Milvus 用 `dept in [...]`
- [ ] 外部前端接入时补 CORS 与鉴权中间件

### 8.5 文档

- [x] 本轮变更记录（`WORKLOG.md`）+ 安装/使用/FAQ 已同步
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
- [x] **混合检索（本地 BM25）**：`src/lexical.py` 中英文分词 + RRF 融合，默认开启；24 条评测 MRR 0.88→0.89，精确标识符类查询收益明显；后台预热、入库后预热、按部门缓存
- [x] **能力自适应混合检索**：`HYBRID_ENGINE=auto` 探测 Milvus 版本与集合稀疏字段/BM25 索引，2.5+ 走服务端 `hybrid_search`+RRF，否则回退进程内 BM25；`milvus` 显式模式下能力不足启动即报错；`/info`、`/health`、`/metrics` 暴露实际引擎
- [x] **升级 Milvus 到 2.5 并重建集合**：已升级 2.5.4（先备份数据卷），`run_ingestion.py --reset` 重建，实测 `hybrid.engine=milvus`、端到端 18/18
- [ ] **引用去重/score**：同页多 chunk 合并、展示相关性分数

#### P2 稳定与运维

- [x] **`/health`**：向量库块数、入库任务、重建状态；`?probe=1` 带 LLM 探测（15s 缓存）；空库/不可达返回 503，`check_health.py` 10 项冒烟
- [x] **指标（Prometheus）**：`/metrics` 覆盖请求/错误、端到端与检索/LLM 耗时、首 token、索引块数、入库任务、重建状态；可选 `METRICS_TOKEN`
- [ ] **多进程指标聚合**：多 worker 时各进程独立，需 Pushgateway 或按 instance 聚合；指标也未持久化
- [x] **N+1 查询与重复哈希**：批量删除（每 100 doc 一次）、`doc_summary()` 一次聚合替代逐文件查询、元数据缓存 5s、文件哈希按 stat 缓存；`/admin/api/meta` 451ms → 6ms
- [ ] **缓存**：`dept + 规范化问题` 精确 LRU（命中 <10ms），embedding 结果缓存
- [ ] **限流与降级**：按 user_id/IP 限流；LLM 失败时返回检索摘要或换备用模型
- [ ] **管理台增强**：操作审计（谁/何时/删了什么）、任务持久化、上传总量上限、上传进度条
- [ ] **单 worker 约束**：jobs/锁都在进程内，`--workers>1` 会状态分裂；多副本需 Redis/RQ 等外部队列

#### P3 工程与体验

- [ ] **pytest + CI**：`validate_dept`、expr 转义、loaders、ETL 增量、admin 路由；LLM 用 stub 保证离线可跑
- [ ] **惰性引擎 + ChatService**：消除 `/chat` 与 `/chat/stream` 重复分支
- [x] **文件清单性能**：`src/hashing.py` 按 `(path, mtime_ns, size)` 缓存 sha1；`doc_summary()` 缓存 5s；`/admin/api/meta` 451ms → 6ms
- [ ] **前端**：Markdown 渲染、复制回答、停止生成、localStorage 会话、移动端
- [ ] **权限**：多部门 `user_depts`、文档级 ACL、`/chat` 鉴权与 CORS
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

## 9. 明日待办（2026-10-31，按性价比排序）

> 当前状态：Milvus 2.5.4 + 服务端稀疏 BM25 已跑通；`/health`、`/metrics`、混合检索、评测集、管理台均已完成。
> 下面每项都写了**验收命令**，做完勾选并在第 7 节补一行。
> 注意：本地 `.env` 仍写 `VECTOR_STORE_TYPE=faiss`，当前服务用环境变量覆盖为 `milvus`；明天动 Milvus 前先确认这一点。

### 9.1 P1 正确性（优先，预计 3–4 小时）

#### 9.1.1 零停机重建（Milvus alias / FAISS 原子替换）
- [ ] 位置：`src/admin_api.py::_run_job`、`src/engine.py::reset`、`src/hybrid.py`
- [ ] 现状：`reset` 先 drop 集合再重建，期间 `/chat` 返回 503 `index rebuilding`（已能明确报错，但不可用）
- [ ] 方案（Milvus）：
  - 重建写入临时集合 `<collection>_rebuild`
  - 完成且 `count > 0` 后，把应用读取的集合名切换到新集合（alias 或配置热切换）
  - 成功后删除旧集合；失败则保留旧集合不动
  - 保留维护标志作为“切换瞬间”的兜底（毫秒级）
- [ ] 方案（FAISS）：
  - 写入 `DB_PATH.rebuild`，校验可加载后 `os.replace` 原子替换
- [ ] 并发：切换期间新请求要么读旧索引、要么读新索引，不允许读到“半成品”
- [ ] 验收：
  - 重建过程中持续 `curl /chat`，失败请求数为 0（或仅切换瞬间 <100ms）
  - `python scripts/check_health.py` 全程不出现 `down`
  - `python scripts/check_admin.py` 仍通过
  - `VECTOR_STORE_TYPE=faiss` 下重建同样不阻塞查询

#### 9.1.2 变更文件「先写新、再删旧」
- [ ] 位置：`src/etl.py::_process_file`
- [ ] 现状：`delete_by_doc_id()` 之后才 `add_documents()`；插入失败/进程崩溃会**丢文档**
- [ ] 方案：
  - Milvus 无事务，改用“新 `doc_id` 版本号”写入（如 `doc_id` + `doc_hash`），成功后删除旧版本
  - 或先写入新 chunk（临时 `doc_id` 后缀），成功后再删旧并改写 `doc_id`（Milvus 不支持原地更新，需插入 + 删除）
  - FAISS：先在内存构建新索引，成功后 `save_local` 覆盖
- [ ] 验收：注入一次解析/写入异常，旧版本仍可检索且 `count` 不变
- [ ] 注意：改 `doc_id` 语义会触发一次全量重建，安排在低峰

#### 9.1.3 多轮改写加相似度护栏
- [ ] 位置：`src/agent.py::_retrieval_query`
- [ ] 现状：含代词或极短句就拼上一轮问题；若用户同时换主题（“那 Kubernetes 呢？”之前问的是报销）会引入噪声
- [ ] 方案：拼接前用 embedding 余弦相似度或关键词重叠判断，低于阈值只取当前问句
- [ ] 验收：新增 4 条多轮评测用例（追问命中 / 换题不污染），`eval/cases.jsonl` 一起跑
- [ ] 目标：MRR 不下降，追问类 rank 不退化

### 9.2 P2 性能（预计 3 小时）

#### 9.2.1 FAQ 精确缓存
- [ ] 位置：新增 `src/cache.py`，接入 `main.py` 的 `/chat` 与 `/chat/stream`
- [ ] 键：`dept + 规范化问题`（去空白/标点、小写）；值：`answer + sources + timings`
- [ ] LRU 容量 512、TTL 1 天；**入库/删除/重建后清空**
- [ ] 只缓存非工具调用、无错误、`llm_calls == 1` 的回答
- [ ] 验收：重复问题第 2 次 `total_ms < 20ms`；`/metrics` 新增 `agent_rag_cache_hits_total{result}`

#### 9.2.2 embedding 缓存
- [ ] 位置：`src/engine.py` 查询侧（`embed_query`）
- [ ] 键：`embedding_model + query`；LRU 2048
- [ ] 验收：`scripts/bench_retrieval.py` 重复查询 embedding 耗时 → ~0ms

#### 9.2.3 FAISS 按部门索引（可选）
- [ ] 现状：全库检索后本地过滤，`pool = max(k*20, 50)`，部门多/数据偏斜时召回不足
- [ ] 方案：每个部门一个 FAISS 索引文件 + 默认 index 元数据
- [ ] 验收：新增“多部门大数据”基准，Recall@4 不低于 Milvus 模式；`verify_retrieval` 隔离仍通过

### 9.3 P3 工程与运维（预计 4–6 小时）

#### 9.3.1 pytest + CI
- [ ] 把 `scripts/check_*.py` 的断言重构成 pytest 用例（保留脚本入口）
- [ ] LLM 用 stub（`monkeypatch` ChatOpenAI）保证离线可跑；向量库用临时 FAISS 目录
- [ ] GitHub Actions：`ruff`（或 `flake8`）+ `pytest` + `test_agent.py`（可选 nightly）
- [ ] 验收：`pytest -q` 全绿；CI 在 PR 上运行

#### 9.3.2 `ChatService` 抽取 + 惰性引擎
- [ ] `/chat` 与 `/chat/stream` 的校验、维护检查、异常映射、指标记录抽到 `src/chat_service.py`
- [ ] `kb_engine` 改惰性单例（`get_engine()`），`scripts/check_llm.py` 不再加载 embedding
- [ ] 验收：`python -c "import src.engine"` 不加载模型（<0.5s）；`test_agent.py` 全过

#### 9.3.3 管理台增强
- [ ] 上传总量上限（如单次 ≤200MB）、上传进度条
- [ ] 操作审计：记录谁/何时/删了什么（写入 `logs/admin_audit.log`）
- [ ] 任务持久化：重启后仍能看到最近任务（SQLite 或 JSON 文件）
- [ ] 验收：`check_admin.py` 增加对应断言

#### 9.3.4 安全与权限
- [ ] `/chat` 增加可选 API Key 或反向代理鉴权开关
- [ ] 多部门 `user_depts: list[str]`（Milvus `dept in [...]`，FAISS 多值过滤）
- [ ] 文档级 ACL（元数据 `acl` 字段 + 检索过滤）
- [ ] 验收：新增越权测试用例，两部门交叉提问均不泄漏

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
