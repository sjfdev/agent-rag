# 工作记录（2026-10-29 ~ 10-30）

本文件记录本轮迭代完成的功能、验证结果与遗留事项。按提交顺序整理，便于回溯。
更早的历史见 `OPTIMIZATION_PLAN.md` 第 7 节与 git log。

---

## 1. 总览

| # | 主题 | 提交 | 状态 |
|---|---|---|---|
| 1 | 入库管理台 `/admin` | `2c323f4` | 完成 |
| 2 | 检索评测集与 eval 脚本 | `e69c57c`、`56daec0` | 完成 |
| 3 | 多轮检索改写 | `e69c57c` | 完成 |
| 4 | 引用原文 `snippet` + UI 折叠面板 | `e69c57c` | 完成 |
| 5 | 可选 Cross-Encoder 重排 | `e69c57c`、`033ac63` | 完成（默认关闭） |
| 6 | 全量重建期间返回 503 | `e69c57c` | 完成（后被 9.1.1 零停机重建取代） |
| 7 | `/health` 存活与就绪探针 | `cd75f5a` | 完成 |
| 8 | Prometheus `/metrics` | `d602d61` | 完成 |
| 9 | 混合检索（进程内 BM25 + RRF） | `d602d61` | 完成 |
| 10 | 混合检索缓存竞态修复 | `01b2076` | 完成 |
| 11 | 混合检索能力自适应 | `3c650d3` | 完成 |
| 12 | Milvus 升级 2.5.4 + 服务端稀疏 BM25 | `c87bccc` | 完成并实测 |
| 13 | 管理台冒烟测试清理残留部门 | `53e82b9` | 完成 |
| 14 | 性能与死代码清理（批量删除、哈希缓存、元数据缓存） | `c6a90c6` | 完成 |
| 15 | 零停机重建 + 安全替换 + 多轮相似度护栏 | `2701aba` | 完成 |
| 16 | FAQ 答案缓存 + 查询向量缓存 | `69190c1` | 完成 |
| 17 | pytest 测试套件 + ruff + GitHub Actions | `776e6c7` | 完成 |
| 18 | ChatService 抽取 + 惰性引擎 | `981e5d7` | 完成 |
| 19 | 管理台审计/任务持久化/上传配额 + 鉴权/多部门/文档 ACL | `63067f0` | 完成 |
| 20 | 测试覆盖率提升（73% → 92%） | `7fcc6a2` | 完成 |
| 21 | Makefile / doctor / 限流 / 请求超时 | `f64aa29` | 完成 |
| 22 | Quickstart + 计划校正 | `f8aa9c4` | 完成 |
| 23 | 收尾与明日待办 | `60cdbee` | 完成 |
| 24 | LLM 降级 + CORS（10.1） | 本轮 | 完成 |
| 25 | 前端体验 + 引用质量 + rerank_ms（10.2） | 本轮 | 完成 |
| 26 | README / 运维手册 / 离线安装（10.3） | 本轮 | 完成 |
| 27 | 修复 CI 依赖冲突与 torch-free 测试 | `5799504` | 完成 |
| 28 | 解析增强（表格分块 / PDF 表格 / OCR） | `f0a7632` | 完成 |
| 29 | 多轮上下文修复 + bench 重排对比 | `332567e` | 完成 |
| 30 | 产品化交付（Docker / 生产 compose / 路线图） | `63378ca` | 完成 |
| 31 | 待办收敛到第 11 节 | `97769f5` | 完成 |
| 32 | JWT 登录与身份来源（11.1.1） | `98c3cef` | 完成 |
| 33 | 管理台登录页与身份展示 | `56d0732` | 完成 |
| 34 | OIDC/JWKS 接入（无账号验证） | 本轮 | 完成 |
| 35 | 浏览器 OIDC 登录（`/auth/login` → `/auth/callback`） | 本轮 | 完成 |
| 36 | 本地账号登录（scrypt + CLI + 管理台，不接 IdP） | 本轮 | 完成 |
| 37 | 部署加固：ACL fail-closed + 账号库持久化 | 本轮 | 完成 |
| 38 | 发布流程：版本号 / CHANGELOG / 发布闸门 | 本轮 | 完成 |
| 39 | 手工测试修复：无部门账号 / 400 文案 / 审计表 | 本轮 | 完成 |
| 40 | 存储卡死不再拖垮控制台（分页查询 + 并行刷新 + 超时） | `051980c` | 完成 |
| 41 | 聊天页安全 Markdown 渲染 | `3ad6819` | 完成 |
| 42 | Milvus 写路径事故定位 + 磁盘余量哨兵 | `d8f65e7` | 完成 |
| 43 | schema 迁移说明 + 发布前清单收尾 | 本次（纯文档） | 完成 |

当前运行态：`vector_store=milvus`、`hybrid.engine=milvus`、Milvus `v2.5.4`、26 chunks / 4 部门。

> 注意：仓库里 `.env` 仍写着 `VECTOR_STORE_TYPE=faiss`，当前服务是用环境变量覆盖成 `milvus` 启动的。
> 若要让重启后默认使用 Milvus，把 `.env` 改成 `VECTOR_STORE_TYPE=milvus`。
> FAISS 索引（`db/vector_db`）与 Milvus 集合是两套独立数据，切换后端后需确认数据是否已入库。

---

## 2. 入库管理台 `/admin`

新增 `src/admin_api.py`、`static/admin.html`、`scripts/check_admin.py`。

- 部门切换/创建，显示文件总数与状态统计
- 拖拽/多选上传，可指定子目录，单文件默认 ≤50MB
- 文件列表：类型、大小、状态（已入库/待入库/有变更/不支持）、chunk 数、修改时间
- 当前部门增量入库 / 全部增量入库（自动 prune 已删文件）
- 删除文件同时按 `doc_id` 删除向量
- 全量重建（需确认），后台线程执行并实时轮询日志
- 安全：`ADMIN_TOKEN` 或仅回环可访问；扩展名白名单、大小限制、文件名/子目录净化
- 单部门入库不会误删其它部门数据

管理台侧边入口：对话页左下角「入库管理」。

---

## 3. 检索质量

### 3.1 评测集与脚本

- `eval/cases.jsonl`：24 条，18 条命中用例 + 6 条跨部门泄漏用例，覆盖 Tech/Finance/HR/Demo，含中文查询
- `scripts/eval_retrieval.py`：hit@k、MRR、泄漏、P50/P95；低于 `--min-hit` 或有泄漏时退出码非 0
- 混合模式下脚本会先预热索引，避免把一次性构建成本算进延迟

当前结果（两种后端一致）：`hit@4=18/18 (100%)`、`MRR=0.89`、`leaks=0`、P50 9–24ms。

### 3.2 多轮改写

`src/agent.py::_retrieval_query`：检测代词/指示词或极短问句，把上一轮问题拼进检索 query，不额外调用 LLM。

实测追问 “Who approves it?” 时目标文档从第 3 位升到第 1 位。

### 3.3 引用原文

`sources` 增加 `snippet`（去空白后截断 220 字符），UI「来源」改为可折叠面板，展开可见原文摘录。

### 3.4 可选重排

`src/reranker.py` 接入 `kb_engine.search`，由 `RERANK_ENABLED` 控制（默认关闭）。
懒加载模型，加载/推理失败时退回原顺序，不影响可用性。

24 条双语评测（纯向量阶段）：

| 配置 | MRR | 增量延迟 |
|---|---|---|
| 不重排 | 0.88 | — |
| `cross-encoder/ms-marco-MiniLM-L-6-v2` | 0.92 | +≈205ms |
| `BAAI/bge-reranker-base` | 0.97 | +≈1.2s（P95 4.8s） |

---

## 4. 可观测性

### 4.1 `/health`

`src/health.py`。返回 `status`（ok / degraded / down）及分项：

- `vector_store`：类型、chunk 数、延迟、错误
- `ingestion`：是否有任务在跑、最新任务快照
- `llm`：模型名；`?probe=1` 时真实调用一次（1 token，缓存 15s）
- `maintenance`：是否在重建
- `hybrid`：实际引擎、配置值、Milvus 版本、稀疏字段是否就绪
- `rerank_enabled`

语义：索引为空或向量库不可达 → **503 down**；入库/重建中或 LLM 探测失败 → **200 degraded**。
`scripts/check_health.py`：10 项冒烟，`--probe` 时 12 项。

### 4.2 `/metrics`

`src/metrics.py`，Prometheus 文本格式：

| 指标 | 说明 |
|---|---|
| `agent_rag_chat_requests_total{endpoint,status}` | 请求数（ok/error/rejected） |
| `agent_rag_chat_duration_seconds` | 端到端耗时 |
| `agent_rag_retrieval_seconds` | 检索耗时（含重排） |
| `agent_rag_llm_seconds{calls}` | LLM 等待耗时 |
| `agent_rag_llm_calls_total` | LLM 调用数（工具回路 >1） |
| `agent_rag_first_token_seconds` | 流式首 token |
| `agent_rag_index_chunks` | 索引块数（抓取时最多每 5s 刷新） |
| `agent_rag_ingest_jobs_total{status}` | 入库任务结果 |
| `agent_rag_maintenance` | 是否重建中 |
| `agent_rag_hybrid_engine{engine}` | 当前混合检索引擎 |
| `agent_rag_hybrid_sparse_ready` | 服务端稀疏 BM25 是否可用 |
| `agent_rag_retrieval_engine_total{engine}` | 检索实际路径（vector/local/milvus/fallback） |

- 部门名**不作为标签**（敏感 + 高基数）
- 可选 `METRICS_TOKEN`，设置后需 `Authorization: Bearer ...`（实测 401/401/200）
- 指标为单进程，多 worker 需在 Prometheus 侧按 instance 聚合

### 4.3 错误码补充

- 向量库不可达时 `/chat`、`/chat/stream` 返回 `503 vector_store_unavailable`（原来是 500）
- 全量重建期间返回 `503 index rebuilding`（现在只有切换瞬间可能触发，见 5.6）
- 前端对两者都有提示与「重试」按钮

---

## 5. 混合检索

### 5.1 进程内 BM25（`local`）

`src/lexical.py`：

- 按部门构建 BM25 索引，向量与 BM25 结果用 RRF 融合
- 中英文分词：英文按词，中文按单字 + 二元组
- 索引按引擎写入版本缓存；命中写入竞态的构建结果不会被缓存
- 启动后与入库后后台预热；`HYBRID_MAX_DOCS` 限制单部门语料（0 为不限）
- `scripts/check_hybrid.py`：42 项单测（分词、BM25、RRF、缓存、能力判定、结果转换）

### 5.2 能力自适应（`HYBRID_ENGINE`）

`src/hybrid.py` + `src/engine.py`：

| 值 | 行为 |
|---|---|
| `auto`（默认） | Milvus ≥ 2.5 且集合含 `sparse` 字段 + BM25 索引 → 服务端；否则进程内 |
| `local` | 强制进程内（FAISS 只能走这个） |
| `milvus` | 强制服务端；能力不足时启动即报错，不静默降级 |

探测内容：服务端版本、集合是否存在、`SPARSE_FLOAT_VECTOR` 字段、该字段的 `BM25` 索引。
`/info`、`/health`、`/metrics` 均暴露实际引擎。

### 5.3 服务端稀疏 BM25（Milvus 2.5+）

- 集合 schema 增加 `sparse` 字段，`BM25BuiltInFunction` + analyzer（默认 `{"type": "chinese"}`）
- 检索用 `hybrid_search` + `RRFRanker`，一次请求完成稠密 + 稀疏融合
- 服务端调用失败时仅该次查询回退本地，不返回 500
- 稀疏集合不能走 `similarity_search()`（Milvus 拒绝返回稀疏原始数据），稠密检索改为显式查询 `vector` 字段
- `scripts/check_hybrid_milvus.py`：18 项端到端（建表、BM25 索引、精确标识符召回、部门隔离、删除、reset 后 schema 重建）；<2.5 自动跳过，使用临时集合

### 5.4 实测对比

| 引擎 | hit@4 | MRR | 泄漏 | P50 |
|---|---|---|---|---|
| 纯向量 | 18/18 | 0.88 | 0 | 15ms |
| 进程内 BM25 | 18/18 | 0.89 | 0 | 15–22ms |
| 服务端稀疏 BM25 | 18/18 | 0.89 | 0 | 24ms |

精确标识符查询（如 `DEMO-GUEST`）收益最明显；语义查询无退化。
分析器实测：`chinese` MRR 0.894、`english` 0.917、`standard` 0.894，默认保留 `chinese` 以兼顾中文。

---

## 5.5 性能与清理（本轮）

| 改动 | 位置 | 效果 |
|---|---|---|
| 批量删除 | `driver.delete_by_doc_ids` | prune/删除由「每文档一次 query + delete + flush」改为每 100 个 doc 一次，实测 3 个 doc 2452ms→单批 |
| 文档摘要接口 | `driver.doc_summary()` | 一次拉取 `doc_id/doc_hash` 并聚合 chunk 数，替代 `get_doc_hash` + `count_by_doc_id` 的 N+1 |
| 元数据缓存 | `KnowledgeBaseEngine.doc_summary(max_age=5)` | `/admin/api/meta` 热态 **451ms → 6ms**；本进程写入立即失效 |
| 文件哈希缓存 | `src/hashing.py` | 按 `(path, mtime_ns, size)` 缓存 sha1，避免每次刷新重读全部文件 |
| 删除死代码 | `src/tools.py` | 移除未暴露给模型的 `search_knowledge_base` |
| 检索路径指标 | `agent_rag_retrieval_engine_total{engine}` | 可观测实际走 vector/local/milvus/fallback |
| 检索分支收敛 | `KnowledgeBaseEngine.search` | 去掉重复的融合/重排分支，行为不变，评测回归通过 |

---

## 5.6 零停机重建与安全替换（本轮）

**目标**：重建/更新期间查询不中断，且崩溃不会丢文档。

| 能力 | 实现 |
|---|---|
| Milvus 重建 | 写入 `enterprise_kb__r<毫秒时间戳>` 影子集合；读仍走 `enterprise_kb` alias；提交时 `create_alias/alter_alias` 切到影子，随后删除旧物理集合；失败调用 `abort_rebuild()` 丢弃影子 |
| FAISS 重建 | 写入临时目录 → 校验可加载 → `os.replace` 原子替换，旧目录作备份后删除 |
| 维护窗口 | 只在提交的 alias 切换瞬间置位（毫秒级），写入阶段不加锁 |
| 变更文件 | 先写新 chunk（新 `doc_hash`），成功后按 `doc_id + doc_hash != new` 删除旧版本 |
| 读取分离 | `count()` = 写入目标，`live_count()` = 线上索引；`/health` 与 `/metrics` 用后者 |
| 旧部署兼容 | 首次重建自动把物理 `enterprise_kb` 改名为 `enterprise_kb__old_<ts>` 再建 alias |
| CLI | `run_ingestion.py --reset` 与 `/admin` 重建走同一套 `begin/commit/abort` |

实测：

- 重建期间 2 个并发 `/chat` 各轮询：**Milvus 0 失败 / FAISS 0 失败**；`/health` 全程 `ok`、26 chunks
- v1→v2 更新：`updated=1`、chunk 数不变、无重复；删除后立即不可见
- `abort_rebuild()` 后线上数据与计数完全不变
- `check_hybrid_milvus.py` 19/19（新增影子读写/提交/回滚断言）

### 5.7 多轮改写护栏

`decide_followup(previous, current, similarity)`：代词直接拼上一轮；指示词与一般问句需 embedding 余弦 ≥ 阈值（0.45 / 0.55）。换题（如上一问报销、这一问 “那 Kubernetes 呢？”）不再污染检索。8 项纯函数单测。

---

## 5.8 缓存（本轮，9.2）

| 缓存 | 位置 | 行为 |
|---|---|---|
| FAQ 答案缓存 | `src/cache.py` | 键 `dept + 规范化问题`；LRU 512 / TTL 1 天；只存单轮、`llm_calls==1`、无错误的回答；任何 KB 写入清空 |
| 查询向量缓存 | `src/embedding_cache.py` | 包装 `Embeddings`，按 `(模型, 文本)` 缓存 `embed_query`；LRU 2048；入库 `embed_documents` 不缓存 |
| 旁路 | `main.py` | `Cache-Control: no-cache` / `X-No-Cache: 1` 跳过缓存；命中响应带 `X-Cache: hit` 且 `timings.cached=true` |

实测：

- 同问题重复请求：**4044ms → 4.2ms**，响应与来源完全一致，`llm_calls=0`
- 同查询重复检索：**1434ms → 9ms**（首次含 embedding 冷启动）；预热后 RPC 约 13ms
- 跨部门同问题不共享（Finance 命中同一问句会重新走 LLM）
- 工具类回答（工单）`llm_calls=2`，不写入缓存
- 入库/删除后 `cache.answer.entries` 立即归零
- `/health` 新增 `cache` 段，`/metrics` 新增 `agent_rag_cache_total{cache,result}` 与 `agent_rag_cache_entries{cache}`

---

## 5.9 测试与 CI（本轮，9.3.1）

| 文件 | 内容 |
|---|---|
| `tests/conftest.py` | 离线环境（fake embeddings、临时 FAISS、无网络）+ 共享 fixtures |
| `tests/test_validation.py` | 15 项：合法/非法部门、注入串 |
| `tests/test_loaders.py` | 15 项：9 种格式、表格/备注/页码/sheet、脚本剥离 |
| `tests/test_hybrid.py` | 23 项：分词、BM25、RRF、能力判定、删除表达式、fake embedding |
| `tests/test_etl.py` | 5 项：首次入库、增量跳过、安全替换、prune、解析失败保留旧版 |
| `tests/test_cache.py` | 9 项：答案缓存 LRU/TTL/部门隔离、向量缓存 |
| `tests/test_admin.py` | 6 项：meta、令牌、上传→入库→删除、扩展名/路径、部门校验 |
| `tests/test_api.py` | 10 项：答案+来源、部门隔离、缓存命中/旁路、流式、health |

配套：

- `EMBEDDING_BACKEND=fake`：`FakeEmbeddings` 用 sha256 生成确定性单位向量，跑测试不下载模型、不联网
- `pytest.ini`、`ruff.toml`、`requirements-test.txt`
- `.github/workflows/ci.yml`：Python 3.11 → 安装测试依赖 → `ruff check` → `pytest --cov` → 上传覆盖率

本机结果：**83 passed / ~25s**，`ruff` 全绿，覆盖率 68%（cache/config/validation 100%，engine 51%，reranker 0% 因默认关闭）。

顺带修复：admin 路由抛出的 `InvalidDepartment` 以前会变成 500，现在由 `main.py` 的全局异常处理器统一返回 400。

---

## 5.10 ChatService 与惰性引擎（本轮，9.3.2）

**重复逻辑收敛**

`/chat` 与 `/chat/stream` 原来各自实现一遍：request_id、维护期 503、`_prepare`、非法部门 400、答案缓存读写、异常分类（上游/向量库/内部）、指标与日志。现在统一在 `src/chat_service.py`：

| 函数 | 职责 |
|---|---|
| `prepare(request, endpoint)` | 维护检查、部门校验、构造图输入、拒绝指标 |
| `run(...)` | 缓存命中 → 调用图 → 计时 → 写缓存 → 指标 → `ChatResponse` |
| `open_stream(...)` | 先 `prepare`（保证错误以 HTTP 返回），返回 `(request_id, frames)` |
| `classify_error` / `ChatFailure` | 异常 → `(status, code, message)`，路由只做映射 |
| `cache_lookup / cache_store / cache_bypassed` | 缓存策略集中一处 |

`main.py` 从 500+ 行降到 ~160 行；请求/响应模型移到 `src/schemas.py`。

**惰性引擎**

原来是 `kb_engine = KnowledgeBaseEngine()`，import 即加载 embedding（~12–14s）。现在 `_LazyEngine` 代理 + `get_engine()`：

| 操作 | 之前 | 现在 |
|---|---|---|
| `import src.engine` | ~12–14s | **0.72s** |
| `from src.engine import kb_engine` | 立即加载模型 | 0.42s，不加载 |
| `import src.agent` | ~12s | **4.6s** |
| `import main` | ~13s | **5.7s** |

细节：LangGraph 在 `workflow.compile()` 时会内省 `call_model` 的自由变量并触碰 `kb_engine.search`，所以代理对**方法**返回轻量包装而不是立即构造；`__dunder__` 直接 `AttributeError`，避免 `hasattr(obj, "__self__")` 触发生成。`agent.py` 改用显式 `get_engine()`。`reset_engine()` 供测试重置。

**行为保持**

- 响应头 `X-Request-ID`、`X-Cache` 在成功/缓存/失败/流式路径都返回
- 维护期与非法部门仍为 503/400；流式在开流前完成校验，不会先 200 再报错
- 新增 `tests/test_chat_service.py` 14 项覆盖上述分支

实测：缓存命中 4ms；`X-Cache: hit`；强制 LLM 失败时非流式 500、流式返回 `internal_error` 事件并带 request_id。

---

## 5.11 管理台加固与权限（本轮，9.3.3 / 9.3.4）

### 管理台

| 能力 | 实现 |
|---|---|
| 上传总量上限 | `MAX_UPLOAD_TOTAL_MB`（默认 200）；按 `Content-Length` 之和预检，超限 413 |
| 上传进度条 | `api()` 在传 `onProgress` 时改用 XHR（fetch 无上传进度事件），管理台显示百分比 |
| 操作审计 | `src/audit.py`：`dept.create` / `file.upload` / `file.delete` / `ingest.start`，JSON Lines；令牌只记 `sha1` 前 8 位；`GET /admin/api/audit`；控制台新增审计卡片 |
| 任务持久化 | `src/jobs.py`：任务 + 日志镜像到 `logs/ingest_jobs.json`，临时文件 + `os.replace` 原子写；重启自动恢复，中断任务标记为失败 |

实测：重启后 `restored 2 ingestion job(s)`；上传 2MB（上限 1MB）返回 `413 upload is 2.0MB; at most 1MB per request`；审计记录含 `dept.create` / `file.upload` / `file.delete` / `ingest.start`，不含令牌明文。

### 权限

| 能力 | 实现 |
|---|---|
| `/chat` 可选 API Key | `CHAT_API_KEY`；接受 `X-API-Key` 或 `Authorization: Bearer`；缺失/错误 401；流式同样生效 |
| 多部门 | `user_depts: list[str]`（与 `user_dept` 合并去重）；Milvus `dept in [...]`；FAISS 多值后过滤；BM25 逐部门检索后 RRF 融合 |
| 文档级 ACL | `src/acl.py` + `ACL_ENABLED` / `ACL_FILE`；按 source 映射到用户或 `group:xxx`；无规则的文档在部门内公开；mtime 变化自动重载；检索后统一过滤 |
| 缓存隔离 | ACL 开启时缓存键追加用户组，防止高权限答案被复用 |

实测：

- `user_depts=["Tech","Finance"]` 提问预算 → 命中 Finance 文档；单部门请求行为不变
- `CHAT_API_KEY` 设置后：无 key 401、错误 key 401、`X-API-Key`/Bearer 200、`/health` 仍 200
- ACL 规则 `Finance/confidential_budget_2026.pdf -> group:exec`：无组时只返回报销/差旅政策并明确拒答；带 `user_groups=["exec"]` 可看到预算文件

---

## 5.12 测试覆盖率（本轮）

| 模块 | 之前 | 现在 |
|---|---|---|
| 总计 | 73% | **92%** |
| `engine.py` | 53% | **86%** |
| `admin_api.py` | 77% | **90%** |
| `agent.py` | 87% | **98%** |
| `etl.py` | 83% | **97%** |
| `loaders.py` | 89% | **97%** |
| `hybrid.py` | 86% | **97%** |
| `health.py` | 62% | **92%** |
| `metrics.py` | 58% | **100%** |
| `reranker.py` | 0% | **100%** |
| `runtime.py` | 33% | **100%** |
| `tools.py` | 67% | **100%** |

新增测试文件：

| 文件 | 覆盖内容 |
|---|---|
| `tests/test_engine_faiss.py` | 写入/检索/删除/摘要、影子重建提交与中止、缓存状态、embeddings 工厂 |
| `tests/test_engine_milvus.py` | 用假客户端覆盖混合检索、dense 检索、计数、删除、doc_summary、alias 切换、abort/reset（无需 Milvus） |
| `tests/test_agent_logic.py` | 引用构造、`<document>` 注入中和、ACL 过滤、工具后跳过检索、多轮改写、路由 |
| `tests/test_health.py` | ok / down / degraded / maintenance、LLM 探测成功/失败/429/缓存 |
| `tests/test_metrics.py` | 指标写入、混合引擎/维护 gauge、索引 gauge 刷新与节流、异常降级 |
| `tests/test_reranker_runtime.py` | 重排排序、加载/推理失败降级、开关、维护标志、工单工具 |
| `tests/test_etl_extra.py` | 空文件/超限/无文本/隐藏与旧格式、日志回调异常、重建期间不 prune |
| `tests/test_admin_extra.py` | 404/400/409/413 路径、全量重建成功与失败回滚、无部门、任务列表、审计 limit |
| `tests/test_misc_edges.py` | 连接参数、版本解析、Milvus 探测失败、编码回退、JSON/CSV/XLSX 边界、审计/任务降级 |

顺带修复一个真实缺陷：`MilvusDriver.shadow_collection` 在 `abort_rebuild()` 把 `_shadow_name` 置空后返回 `None`（`getattr` 的默认值不会在键存在但为 `None` 时生效），现在回退到默认名称。

---

## 5.13 工程基建与稳定性（本轮）

### Makefile

`make help` 列出全部目标，Python 解释器优先使用 conda 环境：

| 目标 | 作用 |
|---|---|
| `make install` | 安装运行 + 测试依赖 |
| `make data` / `make ingest` | 生成 Demo 数据 / 增量入库 |
| `make run` | 启动 API |
| `make test` / `make cov` / `make lint` / `make check` | 测试、覆盖率、ruff、两者一起 |
| `make milvus-up/down/logs` | 启停 Milvus 并等待健康检查 |
| `make reset-faiss` / `make reset-milvus` | 影子索引全量重建 |
| `make doctor` / `make eval` / `make clean` | 体检 / 检索评测 / 清理 |

### doctor

`scripts/doctor.py`（`--no-llm` 跳过网络）逐项检查：Python 3.11、12 个关键包、NumPy ABI、向量库与 embedding 后端、模型缓存、数据目录、索引块数与混合引擎、Milvus 端口与版本、Docker、可选 LLM 探针。本机输出 `RESULT: OK`，缺数据目录时返回非零。

### 限流与超时

- `src/ratelimit.py`：滑动窗口，键优先取 `X-Forwarded-For` / `X-Real-IP`，否则 `user_id`；默认关闭（`RATE_LIMIT_PER_MINUTE=0`）
- 超限返回 `429 {"detail":{"error":"rate_limited",...}}` + `Retry-After`
- `CHAT_TIMEOUT`（默认 120s）：非流式用线程池 `result(timeout)`；流式逐帧检查，超时产生 `timeout` 错误事件
- 异常分类抽到 `src/errors.py`，新增 504 `timeout`

实测（`RATE_LIMIT_PER_MINUTE=3`）：前 3 次 200，第 4 次 429，响应头带 `retry-after: 56`。

---

## 5.14 降级、CORS、引用质量与文档（2026-10-31）

### LLM 失败降级

`call_model` 在**已完成检索**且异常属于传输类（连接/超时/上游）时，不再抛错，而是用前 3 条 snippet + 来源拼确定性摘要：

- `/chat` 仍返回 200，body 带 `degraded=true`、`degraded_reason=llm_unavailable`
- 流式 `done` 事件带同样字段；UI 显示「降级回答」与提示
- 降级答案**不写入** FAQ 缓存（调用点跳过 + `cache_store` 二次校验）
- 开关 `LLM_FALLBACK_ENABLED`（默认开）；工具回路中的失败、无检索结果时仍按原错误返回

实测（`LLM_API_BASE` 指向 `127.0.0.1:9`）：`/chat` 200、3 条来源、`timings.degraded=true`、`X-Cache: miss`、缓存条目 0；恢复真实 LLM 后 `degraded=false`。

### CORS + 管理台防护

- `CORS_ORIGINS`（逗号分隔，默认空=不启用）；`allow_headers` 含 `X-API-Key`，`expose_headers` 含 `X-Request-ID`/`X-Cache`
- 管理台额外校验 `Origin`：既不在白名单又非同源时返回 403
- 实测：允许来源预检通过；`https://evil.example.com` 访问 `/admin/api/meta` → 403

### 引用质量

- 同页/同 sheet 的多 chunk 合并为一条来源，取最高分与最长 snippet
- 底层 `retrieval_score`：Milvus 稠密 L2→相似度、混合检索用融合分、FAISS `similarity_search_with_score`
- 对外 `sources[].score` 用相对排序（1.0、0.667、0.333），避免不同引擎分数不可比
- UI 显示分数并按分数排序

### 可观测性

`timings` 新增独立 `rerank_ms`（默认 0），`overhead_ms` 已扣除；UI 仅非零时显示。

### 前端

`localStorage` 保存会话（history + 部门/用户 + 已渲染对话），`?new=` 新窗口不恢复；复制回答；`AbortController` 停止生成；切换部门/清空时同步清理本地会话。

### 文档

`README.md`（定位/架构/能力表/接口/快速开始）、`AGENT_GUIDE.md` 第 12 节运维手册（新增部门/工具、重建、切换后端、升级、回滚、备份、多 worker）、`DEPLOYMENT_CN.md` 第 7 节离线/内网安装。

### CI 首跑排查

GitHub Actions 前 6 次 run 均为失败，失败步骤是 `Install test dependencies`（lint 与测试被跳过）。

**根因 1**：`ci.yml` 手写依赖，把 `langchain-core` 钉在 `0.3.79`，而 `langchain-community==0.4.2` 要求 `langchain-core>=1.4.0`：

```
ERROR: Cannot install langchain-community==0.4.2 and langchain-core==0.3.79 ...
ResolutionImpossible
```

**根因 2**：`tests/test_reranker_runtime.py` 直接 `import sentence_transformers`，CI 不装 torch，即使依赖修好也会有 4 个用例失败。

**修复**：

- CI 不再维护第二份依赖清单，改为 `pip install -r requirements-test.txt`（本地跑 287 项用的就是它）
- `ruff` 检查范围补上 `scripts/` 与 `test_agent.py`
- reranker 测试通过 `monkeypatch.setitem(sys.modules, "sentence_transformers", fake)` 注入假模块

**本地复现**：新建干净 venv → 按 CI 步骤安装 → `ruff` 全绿、`pytest` **287 passed**、覆盖率 92%。

**线上确认**：推送 `5799504` 后 run **#11 全部步骤成功**（装依赖 → ruff → pytest → 上传覆盖率）。

## 5.15 解析增强

| 能力 | 实现 | 默认 |
|---|---|---|
| 大表分块 | `_row_blocks()` 把 XLSX/CSV 切成重复表头的块；`XLSX_MAX_ROWS` 封顶并告警 | 20 行/块，2 万行上限 |
| PDF 表格 | `pdfplumber.extract_tables()` 按页提取，追加到该页正文之后 | `PDF_TABLES_ENABLED=true`（未安装依赖则跳过） |
| 扫描版 OCR | 页面文本 < `OCR_MIN_CHARS` 时用 `pdf2image + pytesseract` 补字 | `OCR_ENABLED=false` |
| 元数据 | PDF 表格页保留正文；OCR 页带 `metadata.ocr="1"` | 键集合不变，无需重建 |

设计取舍：

- **表头必须进每个 chunk**：块大小默认 20 行，配合 800 字符切分器，绝大多数块都能带上表头；实测 Demo `Roster` 24 行被切成 20 + 4 两块，两块都含表头
- **依赖缺失不算错误**：`pdfplumber` / `pytesseract` / `pdf2image` 都是可选依赖，缺失或运行失败只写日志，退回纯文本解析
- **不新增元数据键**：Milvus schema 在首次入库时固定，新增键会要求全量重建，所以只复用 `ocr` 这个已有可选键

实测：`data/Demo/metrics.xlsx` 重新入库后 `Roster` 产生 2 个带表头的 chunk（20 行 + 4 行），总数 28 chunks；`test_agent`、两种后端评测、admin、loaders 全部通过。

## 5.16 多轮上下文与重排基准

### 多轮上下文修复

**问题**（真实缺陷）：`ChatRequest.history` 上限 20 条，而前端每轮追加 2 条且从不裁剪，第 12 轮起必然 422；页面还把原始校验 JSON 直接显示出来。

| 位置 | 改动 |
|---|---|
| `src/config.py` | 新增 `HISTORY_MAX_MESSAGES`（默认 20） |
| `src/schemas.py` | 硬上限放宽到 200（仅防滥用） |
| `src/chat_service.py` | `trim_history()` 取最近 N 条，若首条是 assistant 一并丢弃，保证序列从 user 开始 |
| `static/index.html` | 发送与持久化都只带最近 20 条；422/429/413 给出中文提示；超长输入在本地拦截 |

实测：22 / 40 / 80 / 200 条历史均返回 200；300 条（超过硬上限）仍返回 422，符合预期。

### 重排基准

`scripts/bench_retrieval.py` 新增 `--rerank`（默认在 `RERANK_ENABLED=true` 时自动启用）与 `--no-llm`、`--queries`、`--dept`，把**模型加载、单次重排、端到端**分开计时：

| 配置 | 加载 | 单次重排 p50 | 检索+重排 p50 | 纯检索 p50 |
|---|---|---|---|---|
| `BAAI/bge-reranker-base` | 2970ms | 4081ms | 4116ms | 17ms |
| `ms-marco-MiniLM-L-6-v2` | 162ms | 467ms | 444ms | 24ms |

结论：BGE-base 在 CPU 上每查询约 4 秒，只适合离线评测或 GPU；线上若开重排建议 MiniLM 或 GPU 部署。

## 5.17 产品化交付

| 产物 | 说明 |
|---|---|
| `Dockerfile` | `python:3.11-slim`；torch 走 CPU 索引；embedding 模型构建期下载并内置（`HF_ENDPOINT` 可换镜像）；非 root（uid 10001）；存活探针用 `/info`；单 worker |
| `docker-compose.prod.yml` | etcd + Milvus + app；只有 app 发布 8000；`./data`、`./logs`、`./volumes-prod`；`env_file: .env` 且容器网络覆盖 `MILVUS_URI` |
| `.dockerignore` | 排除 tests、db、volumes、logs、data、.env |
| `PRODUCTIZATION.md` | 发布路径：打包 → 登录（OIDC/自建）→ 多租户 → 配额计费 → 可观测 → 前端 → 发布流程 → 备份；含实施顺序与风险 |

**构建取舍**：最初版本用 `apt-get install curl` 做健康检查，本机网络下 apt 卡住十分钟；改为纯 Python 探针后无需任何 Debian 包。PyPI 在本机被重置连接，构建参数默认指向清华镜像与 SJTU 的 torch 轮子镜像（均可用构建参数覆盖）。

**端到端验证**（真实容器）：

- `docker compose -f docker-compose.prod.yml up -d --build` → app `healthy`
- 容器内 `run_ingestion.py` 写入 28 chunks；`/health` = ok
- `/chat` 200 带 3 条来源；`/chat/stream` 正常 SSE；`/ui/`、`/admin`、`/metrics` 均 200
- 故障演练：停掉 Milvus 后 `/health` 503、`/info` 200、容器保持 `healthy`；恢复 Milvus 后 `/health` 自动回到 ok
- 生产栈移除后，开发栈与本地 uvicorn 恢复正常

---

## 6. Milvus 升级（2.4.0 → 2.5.4）

- 升级前备份数据卷：`volumes/milvus` + `volumes/etcd` → `milvus-2.4-<时间戳>.tgz`
- `docker-compose.yml` 默认镜像改为 `milvusdb/milvus:v2.5.4`
- 拉取问题：直连 Docker Hub 超时；最终通过 **colima** 配置镜像源成功
  - `~/.colima/default/colima.yaml` → `docker.registry-mirrors`：
    `docker.1ms.run`、`docker.xuanyuan.me`、`docker.m.daocloud.io`、`docker.1panel.live`
  - `colima restart` 后一次拉取成功
- `run_ingestion.py --reset` 在升级时曾改为「先 drop 再构造引擎」以应用新 schema；现在重建走影子索引，不再需要先 drop（见 5.6）
- 重建后集合字段含 `sparse`，索引 `sparse (BM25)` + `vector (L2)`

升级注意：

1. Milvus 不支持降级，升级前必须备份
2. 旧集合没有稀疏字段，`HYBRID_ENGINE=milvus` 会启动报错并提示 `--reset`
3. `auto` 模式下旧集合会静默回退进程内 BM25，不会中断服务

---

## 7. 配置新增

| 配置 | 默认 | 说明 |
|---|---|---|
| `RERANK_ENABLED` | `false` | 开启 Cross-Encoder 重排 |
| `RERANK_MODEL` | `BAAI/bge-reranker-base` | 重排模型 |
| `RERANK_CANDIDATES` | `12` | 送入重排的候选数 |
| `HEALTH_MIN_CHUNKS` | `1` | 低于该块数 `/health` 判定 down |
| `HYBRID_ENABLED` | `true` | 是否启用混合检索 |
| `HYBRID_ENGINE` | `auto` | `auto` / `local` / `milvus` |
| `HYBRID_CANDIDATES` | `20` | 融合前每路候选数 |
| `HYBRID_MAX_DOCS` | `20000` | 单部门 BM25 语料上限 |
| `HYBRID_SPARSE_FIELD` | `sparse` | 稀疏字段名 |
| `HYBRID_ANALYZER` | `{"type":"chinese"}` | 服务端 BM25 分析器 |
| `METRICS_TOKEN` | 空 | `/metrics` Bearer token |

依赖新增：`prometheus-client==0.24.1`、`rank-bm25==0.2.2`。

---

## 8. 验证矩阵

| 检查 | 结果 |
|---|---|
| `python test_agent.py` | 20/20 通过（需服务 + LLM，含缓存断言） |
| `pytest -q`（离线，含 CI） | 490/490，约 30s；覆盖率 92% |
| `ruff check src tests main.py run_ingestion.py` | 全绿 |
| `python scripts/check_hybrid.py` | 69/69 |
| `python scripts/check_hybrid_milvus.py` | 19/19（2.5+），低版本 SKIP |
| `python scripts/check_health.py [--probe]` | 12/12、14/14 |
| `python scripts/check_admin.py` | 10/10 |
| `python scripts/check_loaders.py` | 全部格式通过 |
| `python scripts/verify_retrieval.py` | FAISS / Milvus 两种后端均 OK |
| `python scripts/eval_retrieval.py` | 18/18、MRR 0.89、0 泄漏 |

手工验证：

- ~~全量重建期间 `/chat` 503~~ → 已改为影子索引，重建期间 0 失败（见 5.6）
- `docker stop milvus-standalone` → `/health` 503、`/chat` 503、流式返回 `vector_store_unavailable`；恢复后自动 `ok`
- 空 FAISS 实例 → `/health` 503
- 6 个并发跨部门请求全部 200 且来源正确
- 管理台令牌 401/200、局域网 403、路径穿越被剥离
- `HYBRID_ENGINE=milvus` 在 2.4 上启动报错；在 2.5.4 上正常运行

---

## 9. 遗留事项

- **多进程指标聚合**：`/metrics` 为单进程，多 worker 需 Pushgateway 或按 instance 聚合
- **单 worker 约束**：入库任务与锁在进程内，`--workers>1` 状态会分裂
- **限流与降级**：按 user/IP 限流、LLM 失败降级未做（缓存已完成，见 5.8）
- **缓存跨进程**：答案/向量缓存是进程内的，多 worker 各存一份，也不持久化；需要共享时换 Redis
- **pytest + CI**：目前是脚本级冒烟，没有单测框架与流水线
- **引用**：同页多 chunk 合并、展示相关性分数
- **前端**：Markdown 渲染、复制回答、停止生成、localStorage 会话
- **权限**：多部门、文档级 ACL、`/chat` 鉴权与 CORS
- **解析**：OCR、PDF 表格结构化、XLSX 按行切块

---

## 9. 当日收尾（2026-10-31）

今天结束时的状态：

- 服务：`vector_store=milvus`、26 chunks、`hybrid.engine=milvus`、`/health=ok`
- 测试：`pytest -q` **265 passed**，覆盖率 **92%**，全离线（fake embedding + stub LLM）
- 质量：`ruff check` 全绿；`test_agent.py` 20/20；评测 18/18、MRR 0.89、0 泄漏
- 工具：`make help` / `make doctor` / `make test` / `make lint` 可用

今天完成（按提交）：

| 提交 | 内容 |
|---|---|
| `7fcc6a2` | 覆盖率 73% → 92%（246 项），修复 `shadow_collection` 回退缺陷 |
| `f64aa29` | Makefile、doctor、限流（429）、请求超时（504）、`src/errors.py` |
| `f8aa9c4` | `QUICKSTART.md`、计划校正（勾掉 35 项已实现条目） |

明天（2026-11-01）的待办已写入 `OPTIMIZATION_PLAN.md` **第 10 节**：

1. LLM 失败降级（检索摘要兜底）
2. CORS
3. 确认 CI 首跑
4. 前端体验（会话持久化、复制、停止生成、Markdown）
5. 引用合并与 score
6. README / 运维手册 / 离线安装
7. 规模化（按需）：FAISS 按部门索引、批量 embedding、Redis

### 当日回归（16 项，全部通过）

`pytest` 265、`ruff` 全绿、`doctor`（含 LLM 探针）OK、`test_agent` 20/20、`check_hybrid` 69/69、`check_health --probe` 14/14、`check_admin`、`check_loaders`、两后端隔离、评测 18/18 / MRR 0.89 / 0 泄漏、`check_hybrid_milvus` 19/19、`/health` ok。

明细见 `OPTIMIZATION_PLAN.md` 10.5.1。

---

## 9.1 下一步入口

所有未完成事项已收敛到 `OPTIMIZATION_PLAN.md` **第 11 节**，共 39 项，按优先级分三层：

- **11.1 P0 对外发布前必须先做**：登录/身份、多租户、发布流程、备份演练
- **11.2 P1 产品化**：告警、配额计费、前端产品化、服务端会话、断开取消
- **11.3 P2 规模化（按需）**：多进程指标、Redis、批量 embedding、FAISS 分部门、大文件流式

恢复开发时的自检命令见 §11.5。

---

## 5.18 JWT 登录（11.1.1）

| 能力 | 实现 |
|---|---|
| Token | HS256；claims `sub`/`depts`/`groups`/`admin`/`iat`/`exp`，可选 `iss`/`aud` |
| 模式 | `AUTH_MODE=none`（默认，body 身份）或 `jwt` |
| 身份优先级 | jwt 模式下 token 唯一有效；body 里的 `user_id`/`user_dept`/`user_groups` 被丢弃 |
| 管理台 | `admin: true` 的 token 放行；`ADMIN_TOKEN` 保留应急；非管理员 403 |
| 缓存 | 键 = token 部门 + ACL 组（`cache_scope`），跨身份不共享 |
| 限流 | 键优先转发 IP，否则 token 的 `user_id` |
| 启动校验 | `AUTH_JWT_SECRET` < 32 字符 → 拒绝启动 |
| 签发 | `scripts/mint_token.py --user alice --dept Tech --group exec --admin` |

实测：

- 无 token → 401；`AUTH_MODE=none` 缺 `user_id` → 400（向后兼容）
- token 部门是 Tech、body 伪称 Finance → 来源仍只有 Tech
- alice/bob 同问题各自缓存（`cache_scope` 相同部门时共享，跨部门不共享）
- admin token `/admin/api/meta` 200；普通 token 403；无 token 401
- `/chat/stream` 带 token 正常 SSE

### 管理台登录页

- 新增 `GET /admin/api/session`：`none` 模式返回免登录；jwt 模式无凭据 401、普通 token 返回 `is_admin=false`（前端据此提示 403），管理令牌返回 `source=admin_token`
- 管理台首次访问显示登录遮罩：JWT（按三段式识别）或管理令牌；凭据只存 `sessionStorage`
- 右上角显示「用户 · 部门 · 管理员」和「退出」；任意接口 401 会自动回登录框并清空凭据
- 审计 `actor` 改为 `user:<sub>`（token）或 `token:break-glass`（静态令牌）
- `tests/test_admin_session.py`（7 项）覆盖 none/jwt/非管理员/break-glass/坏 token/审计 actor

### OIDC / JWKS 接入

现在有两种校验后端：

| 方式 | 配置 | 适用 |
|---|---|---|
| 共享密钥 HS256 | `AUTH_JWT_SECRET` | 网关代理、脚本签发、内部服务 |
| 公钥 JWKS | `AUTH_JWKS_URL` 或 `AUTH_JWKS_FILE` | Keycloak / Auth0 / Entra ID / 飞书 OIDC |

JWKS 细节：

- 按 `kid` 选公钥，未知 `kid` 自动刷新一次（密钥轮换不会锁死用户）
- 公钥缓存 `AUTH_JWKS_CACHE_SECONDS`（默认 300s），失败时**拒绝**而不是放行
- 接受 RS256/384/512、ES256/384/512；拒绝 `alg=none` 与 HS 混淆
- claim 映射可配置：`AUTH_CLAIM_ROLES`（支持 `realm_access.roles` 点号路径）、`AUTH_ADMIN_ROLE`、`AUTH_ROLE_DEPT_PREFIX`（`kb:dept:Tech` → 部门 Tech）

无账号验证：

```
python scripts/check_oidc.py --self-test     # 5/5：校验、角色→管理员、角色→部门、伪造 token 拒绝
python scripts/check_oidc.py --issuer https://login.microsoftonline.com/common/v2.0
                                              # 3/3：JWKS 可达、密钥列表、发现文档
```

本地真实链路：用临时 RSA 私钥签 RS256 token、JWKS 指向本地文件启动服务，`/admin/api/session` 返回 `is_admin=true, depts=["Tech"]`，`/admin/api/meta` 200，`/chat` 正常，篡改 token 401。验证后已清理临时文件并恢复 `AUTH_MODE=none`。

### 浏览器 OIDC 登录（authorization-code flow）

配置 `AUTH_OIDC_CLIENT_ID` / `AUTH_OIDC_CLIENT_SECRET` / `AUTH_OIDC_AUTHORIZE_URL` / `AUTH_OIDC_TOKEN_URL` / `AUTH_OIDC_REDIRECT_URI` 后：

- `GET /auth/login?next=/admin`：生成 `nonce|target` 并用 HMAC-SHA256 签名（`AUTH_JWT_SECRET`，缺省用 client secret），302 到 IdP 授权页；同时写入 `HttpOnly; SameSite=Lax; Path=/auth` 的 10 分钟 state cookie
- `GET /auth/callback`：校验 `state` 签名**且**必须与 state cookie 一致（防 login CSRF）→ 用 code 换 token → `decode_token(id_token)`；JWKS 模式把 provider 的 `id_token` 直接交给前端，共享密钥模式用同一身份重新签发 HS256（有效期上限 1h）；成功时清除 state cookie
- 回调页用 `location.replace(target + '#token=' + token)` 返回 `/admin`，`Cache-Control: no-store`；token 用 JSON 编码并转义 `<>&'`，防止 `</script>` 逃逸
- 管理台启动时读取 `#token=`，写入 `sessionStorage` 后 `history.replaceState` 清掉 fragment；`/info.oidc_login` 为真时显示「使用企业账号登录」
- 安全：`next` 仅接受站内绝对路径（`//host`、带空白字符的一律回退 `/admin`）；未配置 client 时两个路由 404；交换失败（网络/4xx/无 `id_token`/签名不通过）统一 401 并记录日志
- 新增 `AUTH_DEFAULT_DEPTS`（无 dept claim 时兜底）与 `AUTH_DENY_ROLE`（命中即拒绝）
- 测试：`tests/test_auth_oidc.py`（34 项）覆盖登录跳转、state cookie 绑定与跨会话拒绝、TLS 代理下的 `Secure`、state 篡改/缺失、code 缺失、交换失败（HTTP 错误/无 id_token/网络异常/非 JSON/坏签名）、两种模式的 token 返回、`next` 净化、脚本转义、默认部门与拒绝角色

### 本地账号登录（不接 IdP）

`11.1.1` 里"在真实租户上完成一次登录"需要 IdP 管理员，这里给出完全离线的等价能力：账号密码登录，签出来的仍是普通 bearer token。

- `src/local_auth.py`：`hashlib.scrypt`（n=2^14，约 150 ms/次，标准库无新依赖），编码 `scrypt$n$r$p$salt$hash`；未知用户也跑一次等价计算，避免用响应时间枚举账号
- 账号库 `AUTH_USERS_FILE`（默认 `./config/users.json`，已 gitignore）：原子写入 + 0600 权限 + 按 mtime 缓存，手工编辑无需重启；文件损坏时记日志并按空库处理，服务不崩
- `POST /auth/login`（JSON 或表单）→ 校验 → 签发 HS256，token 带 `ver`；按 IP 限流（`AUTH_LOGIN_RATE_LIMIT`，429 + `Retry-After`），成功/失败都写 `auth.login` 审计；失败响应统一 `invalid username or password`
- 吊销：改密码、改部门/组/管理员、停用、删除都会 bump `token_version`，`auth.identity_from_headers` 比对失败即 401。**没有 `ver` 的 IdP/网关 token 不受影响**，两种方式共存
- 管理台新增「账号」卡片（`/admin/api/users`）：创建、改密码、勾选管理员、停用、删除；只剩一个启用管理员时，降级/停用/删除返回 409
- 命令行 `scripts/manage_users.py add|list|show|set|passwd|disable|enable|delete`，密码从终端读（不进 shell 历史），无 TTY 时从 stdin 读
- `/ui/` 修复：jwt 模式下原本只发 body 身份、必然 401；现在有登录框、`Authorization` 头、401 自动回登录框、退出按钮，部门/用户字段在 jwt 模式下隐藏
- 实测（uvicorn + 真实 HTTP）：登录 200 → `/admin/api/session` 返回 `user:alice / Tech,HR / admin`；bob（仅 Finance）登录后 `/admin/api/meta` 403、body 伪造 `user_dept=Tech` 仍只能用 Finance；改密码后旧 token 401；停用后登录 401；降级唯一管理员 409
- 测试：`tests/test_auth_local.py`（55 项）覆盖哈希/盐/篡改参数、用户名与部门校验、原子写与 0600、缓存失效、吊销（禁用/删除/改密/改权限）、外部 token 不受影响、登录限流与审计、管理台 CRUD、最后管理员保护

### 部署加固：ACL fail-closed 与账号库持久化

复盘时发现两个"部署后才会暴露"的问题，都属于会静默出错的那一类：

- **ACL 静默放行（fail-open）**：`ACL_ENABLED=true` + 规则文件缺失/损坏时，`_load()` 返回 `{}`，`is_allowed()` 于是对任何文档都返回 `True`。生产 compose 里 `ACL_FILE=/app/config/acl.json` 的挂载原本是注释状态，一旦有人打开 `ACL_ENABLED` 却忘了取消注释，机密文档**完全不过滤**，且 `/health` 报 ok
  - 现在 `acl._load()` 在文件缺失、不可解析、非对象、规则值不是列表时抛 `ACLConfigError`；`is_allowed` / `filter_documents` 一律按"受控"处理（丢弃文档）
  - 启动时 `acl.validate_config()` 失败即拒绝启动（与弱密钥同样的处理）；`/health` 新增 `acl` 字段并在不可用时返回 503；`scripts/doctor.py` 新增 `Document ACL` 检查
  - 修好文件后按 mtime 自动恢复，无需重启
- **账号库随容器消失**：`AUTH_USERS_FILE` 默认落在 `/app/config/users.json`，但镜像只 `mkdir /app/data /app/logs /models`，compose 也只挂了 `data/` 和 `logs/`。功能能跑（`_save()` 会建目录），但 `up --build` 一重建容器，**所有本地账号全部丢失**
  - `Dockerfile` 增加 `/app/config`；`docker-compose.prod.yml` 挂 `./config:/app/config`（读写，因为运行时要写 users.json）并显式设 `AUTH_USERS_FILE`
- `config/acl.json` 加入 `.gitignore`；`config/acl.example.json` 保持提交
- `scripts/doctor.py` 新增 `Local accounts` 检查：启用但没有账号、或没有可用管理员时 FAIL
- 测试：`tests/test_acl.py` 覆盖文件缺失/非法 JSON/非对象/规则值类型错误全部 fail-closed 以及修复后自动恢复；`tests/test_health.py` 覆盖 `/health` 的 acl 字段、不可用时 503、启动拒绝；`tests/test_doctor.py` 覆盖两项新检查。全套 **490 passed**，`ruff` 通过

### 发布流程（11.1.3）

- `src/version.py` 是版本号唯一来源（`1.0.0`）；`GET /info` 新增 `version`，`/metrics` 新增 `agent_rag_build_info{version=...}`，Prometheus 里能直接看出线上跑的是哪个版本
- `CHANGELOG.md` 按 Keep a Changelog 编写，首个版本 `1.0.0` 记录了既有全部能力（含安全策略与已知限制），并留出 `[Unreleased]`
- `scripts/check_release.py` 校验四处一致：`__version__`、`CHANGELOG` 的 `## [x.y.z]`、HEAD 上的 git tag、compose 的 `APP_TAG`；同时检查工作区干净，可选跑 lint + pytest。失败退出码非 0
- `make release-check`（只做一致性）/ `make release`（含 lint + 测试）
- 发布步骤：
  ```bash
  make release                                   # 一致性 + lint + pytest
  git tag -a v1.0.0 -m "release 1.0.0" && git push origin v1.0.0
  APP_TAG=v1.0.0 docker compose -f docker-compose.prod.yml build
  ```
- 测试：`tests/test_release.py`（8 项）覆盖 semver 格式、CHANGELOG 中存在该版本号、缺失版本号能被识别、compose 使用 `APP_TAG`、脏工作区/标签不匹配会失败

### 手工测试发现的三处问题

按真实用户路径点了一遍管理台「账号」卡片（建号 → 勾管理员 → 改密码 → 停用 → 删除），发现三个问题：

- **能建出没有部门的账号**：UI 和 API 都允许 `depts: []`，账号能登录，但 `/chat` 返回 `400 invalid department name` —— 文案完全误导（部门名没写错，是根本没有部门）。现在 `local_auth._clean_depts` 在创建/修改时直接拒绝（除非设置了 `AUTH_DEFAULT_DEPTS`），管理台也在提交前提示「请至少填写一个部门」；`/chat` 新增 `missing_department` 错误码，`/ui/` 显示「当前账号没有分配部门，请联系管理员补上」
- **审计表根本不存在**：`renderAudit()` 一直在找 `#auditBody`，但 HTML 里从来没有这个表格（从 `63067f0` 起就缺失），所以管理操作一条都看不到。已补上「操作审计」卡片（时间/操作/操作者/对象/结果）
- **停用的管理员无法删除**：`delete_user` 用 `admin_count()`（只统计启用中的管理员）判断，但条件却是"只要目标是 admin"，于是「唯一启用管理员之外的那个已停用管理员」也删不掉，返回 409。已改成只在目标**是启用中的管理员**且没有其他启用管理员时才拒绝；CLI 同步修正

测试：新增 6 项（无部门被拒 / 有 `AUTH_DEFAULT_DEPTS` 时放行 / 清空部门被拒且不落盘 / API 400 / `missing_department` 与 `invalid department name` 区分 / 已停用管理员可删除），全套 **490 passed**，`ruff` 通过。

### Milvus 卡死不再拖垮控制台

现象：管理台打开后「账号」卡片一直不出现，`/admin/api/meta` 和 `/admin/api/files` 请求挂住 120s+，但 `/admin/api/users`、`/admin/api/audit` 正常，`/health` 还是 200。

根因是宿主机磁盘写满（`No space left on device`），Milvus 的 rootcoord 写不了 WAL，于是 **`query_iterator` 永远阻塞**；而 liveness 只看进程活着，所以健康检查全绿。

三处修复：

- **`src/engine.py`**：`doc_summary()` / `fetch_documents()` 改用带 `timeout` 的分页 `query`（每页 500，最多 10 万行），删掉两个 `query_iterator` 调用；`_count` / `_query` / `_query_in` 也补上超时。`doc_summary()` 失败时改为**抛出**而不是返回空字典——空字典会让界面把每个文件都显示成"待入库"，掩盖真实故障
- **`src/admin_api.py`**：`_iter_files()` 在 `doc_summary()` 失败时返回 **503**「vector store unavailable」，不再假装索引为空；`/admin/api/meta` 与 `/admin/api/files` 因此能明确报错
- **`static/admin.html`**：`refreshAll()` 从串行 `try/catch` 改成 `Promise.allSettled` 并行，四个面板各自独立成功/失败（以前 `loadMeta` 一挂，后面三个永远轮不到，账号卡片保持 `hidden`）；`api()` 增加 20s `AbortController` 超时，超时提示「请求超时（20 秒无响应）」；`loadUsers()` 重新抛出错误以便面板级提示

实测（磁盘释放出 9.5G 后）：`/admin/api/meta` 79ms、`/admin/api/files` 16ms、`/admin/api/audit` 13ms、`/admin/api/users` 10ms，全部 200。

测试：新增 7 项（多页分页、行数上限、超时参数透传、`doc_summary` 失败抛出、`/meta` 与 `/files` 的 503、`/health` 在 `live_count` 抛错时 down），全套 **490 passed**，`ruff` 通过。

### 聊天页安全 Markdown 渲染（11.2.3）

模型返回的内容一直带 Markdown（列表、表格、`**粗体**`），但 `/ui/` 是 `esc(text)` 原样转义，用户看到的是竖线和星号。实测确认：

```
问：Show the 2026 budget as a markdown table
答：| Department | Budget Allocation (CNY) |
    |------------|-------------------------|
    | Marketing  | 12,000,000              |
```

所以这不是"锦上添花"，是日常问答的可读性问题。

实现要点：

- 新增 `static/markdown.js`（独立文件，便于 Node 单测；`<script src="/ui/markdown.js">` 加载）
- **先转义再产标签**：代码段与链接先从原文中"挖出"成占位符，剩余文本全部 `esc()`，再套强调/列表/表格，最后回填占位符。模型输出里的 `<script>`、`<img onerror>` 只会显示成文字
- 链接白名单：仅 `http(s)` / `mailto` 生成 `<a>`，并带 `rel="noopener noreferrer nofollow"`；`javascript:`、`data:`、`vbscript:`、相对路径一律保留为纯文本（仍能看到 label，但不可点击）
- 标题降级到 `h4`–`h6`，避免和页面自身的 `h1` 抢层级
- 流式回答期间仍用 `textContent`（避免半截表格反复重排），`done` 后一次性 `renderMarkdown`
- 错误/中止路径主动移除 `.md` 类，保持纯文本；降级提示改成独立 `div`（否则会被 `innerHTML` 覆盖）
- `esc()` 移到 `markdown.js` 并导出 `escapeHtml`，页面里用别名引用，避免两处实现漂移

安全验证（`tests/test_markdown_js.py`，17 项，Node 驱动）：
`<script>` / `<img onerror>` 转义、`javascript:`/`data:`/`vbscript:`/`//evil` 不生成 `<a>`、链接 label 也转义、代码块内不解析 Markdown、嵌套强调、标题降级、不重复转义。

实测：带表格的回答渲染成 `<table>` + `<strong>`，`R&D` 正确变成 `R&amp;D`。全套 **490 passed**，`ruff` 通过。

### Milvus 写路径事故与磁盘哨兵（2026-11-01）

`create_collection` 突然全线失败：

```
MilvusException(65535): IO error: No space left on device:
While open a file for appending: /var/lib/milvus/rdb_data/000463.log
```

排查过程与结论：

| 检查 | 结果 |
|---|---|
| 宿主机 `df -h` | 11G 可用（磁盘先前被写满过一次，已清理） |
| 容器内写 20MB 新文件 | 成功，241 MB/s |
| 容器内往同一目录 append 8MB | 成功，235 MB/s |
| `rdb_data/000463.log` | **已不存在**（早已轮转） |
| `docker restart milvus-standalone` 后重试 | 立即可写 |

所以这不是磁盘问题，而是 **RocksDB 把第一次 ENOSPC 固化成了后台错误**：错误信息是缓存的，空间释放后仍然拒绝写入，直到重新打开 DB。读路径全程正常（28 rows，alias 可解析），所以服务看起来还是好的 —— 只有写入（首次入库、`--reset`、零停机重建的临时集合）悄悄失效。

恢复后实测：

```
check_hybrid_milvus.py        19/19 PASS（含 shadow 重建 + alias 交换）
run_ingestion.py --reset      28 chunks
真实 /chat                    "15 days of paid annual leave" + 3 条引用
```

**真正的缺陷是 `/health` 全程 `ok`。** 补上的东西：

- `/health` 新增 `storage` 块：`DATA_DIR` / `DB_PATH` / 任务与审计日志取最小余量，低于 `HEALTH_MIN_FREE_MB`（默认 512，0 关闭）→ `degraded`，并指出最紧的路径
- `scripts/doctor.py` 新增 `Disk space` 检查（FAIL 时给出路径与余量）
- `QUICKSTART.md` 写明处置顺序：清空间 **并且** `docker restart milvus-standalone`，只清空间没用
- 5 项新测试（阈值、取最紧目录、可关闭、路径不可读不误报）

同期把发布闸门补全：`make release` + `doctor` + 双后端 eval 全部跑通，`v1.0.0` tag 与 `agent-rag:v1.0.0` 镜像落地；镜像构建时发现 `hf-mirror.com` 对模型文件返回 403（宿主机有缓存所以本地无感），已把 `--build-arg HF_ENDPOINT=...` 的处置写进 `Dockerfile`。

### 2026-11-01 收尾

当天最后状态（明天从这里继续）：

```
版本      v1.0.0（tag 已推送，CI success）
镜像      agent-rag:v1.0.0 与 :latest 内容一致
测试      490 passed，ruff 干净
后端      Milvus 28 chunks，hybrid=server-side BM25；FAISS 26 chunks 保留为后备
索引评估  Milvus / FAISS 均 hit@4 100%、MRR 0.89、leaks 0
服务      本地 127.0.0.1:8000（脚本 ~/.agent-rag-run.sh，日志 ~/agent-rag-server.log）
健康      /health=ok，storage.ok=true（约 17G 可用）
```

明天第一件：**11.1.4 备份与恢复演练**（落点、验收与开工前环境清理见 `OPTIMIZATION_PLAN.md` §11.4.1）。

---

## 10. 相关文档

- `AGENT_GUIDE.md`：安装、使用、接口、配置、FAQ、性能优化
- `OPTIMIZATION_PLAN.md`：待办清单与历史进度；**下一次开发直接看第 11 节「后续待办（按优先级）」**
- `DEPLOYMENT_CN.md` / `DEPLOYMENT.md`：私有化部署步骤
- `AGENTS.md`：协作约定与常用命令
