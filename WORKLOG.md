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
| 16 | FAQ 答案缓存 + 查询向量缓存 | 本轮 | 完成 |

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
| `python test_agent.py` | 18/18 通过（需服务 + LLM，含缓存断言） |
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

## 10. 相关文档

- `AGENT_GUIDE.md`：安装、使用、接口、配置、FAQ、性能优化
- `OPTIMIZATION_PLAN.md`：待办清单与历史进度；**下一次开发直接看第 9 节「明日待办」**
- `DEPLOYMENT_CN.md` / `DEPLOYMENT.md`：私有化部署步骤
- `AGENTS.md`：协作约定与常用命令
