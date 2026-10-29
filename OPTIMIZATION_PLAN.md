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

- [ ] 离线启动开关：模型已缓存时设置 `HF_HUB_OFFLINE=1`，避免每次启动的远端元数据探测
- [ ] 流式输出（SSE）降低首字延迟；`/chat/stream`
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
|  |  |  |
