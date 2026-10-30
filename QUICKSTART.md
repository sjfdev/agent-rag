# Quickstart

5 分钟从零跑通一个可提问的知识库。

## 0. 前置

- Python **3.11**（3.13/3.14 没有 torch wheels）
- Docker（只有用 Milvus 时需要；默认 FAISS 不需要）

## 1. 安装

```bash
conda create -n agent-rag python=3.11 -y
conda activate agent-rag
make install            # 等价于 pip install -r requirements.txt -r requirements-test.txt
```

## 2. 配置 LLM

```bash
cp .env.example .env
# 编辑 .env，填入一个 OpenAI 兼容端点（示例为智谱 GLM-4-Flash）
# LLM_API_BASE=https://open.bigmodel.cn/api/paas/v4/
# LLM_API_KEY=你的key
# LLM_MODEL=glm-4-flash
python scripts/doctor.py        # 检查环境与 LLM 连通性
```

## 3. 准备数据并入库

```bash
make data               # 生成 data/Demo 全格式样例（PDF/DOCX/PPTX/XLSX/CSV/MD/TXT/HTML/JSON）
make ingest             # 增量入库
```

> **切换向量后端必须重新入库**：FAISS 与 Milvus 是两套独立存储。改
> `VECTOR_STORE_TYPE` 后请执行 `make reset-faiss` 或 `make reset-milvus`。

## 4. 启动

```bash
make run                # http://localhost:8000
```

| 地址 | 用途 |
|---|---|
| `http://localhost:8000/ui/` | 对话测试台（选择部门提问，查看来源与耗时） |
| `http://localhost:8000/admin` | 入库管理台（上传/删除/入库/重建/审计） |
| `http://localhost:8000/docs` | OpenAPI |
| `http://localhost:8000/health` | 健康检查 |
| `http://localhost:8000/metrics` | Prometheus 指标 |

## 5. 验证

```bash
pytest -q                       # 离线测试，无需服务与网络
python test_agent.py            # 端到端（需要服务与可用 LLM）
python scripts/eval_retrieval.py  # 检索质量（hit@k / MRR / 泄漏）
```

## 6. 可选：Milvus

```bash
make milvus-up
VECTOR_STORE_TYPE=milvus make reset-milvus
VECTOR_STORE_TYPE=milvus make run
```

Milvus 2.5+ 会自动启用服务端稀疏 BM25；用 `GET /info` 的 `hybrid.engine` 确认。

## 常见问题

| 现象 | 排查 |
|---|---|
| `/health` 返回 503 `down` | 索引为空：先 `make ingest`；或 Milvus 未启动 |
| `/chat` 返回 503 `upstream_unavailable` | LLM 端点不可达或 key 错误：`python scripts/check_llm.py` |
| `/chat` 返回 504 `timeout` | 超过 `CHAT_TIMEOUT`（默认 120s），检查网络或调大 |
| `/chat` 返回 429 `rate_limited` | 触发了 `RATE_LIMIT_PER_MINUTE`，看 `Retry-After` |
| 检索不到刚上传的文件 | 上传后需要点「增量入库」或 `make ingest` |
| 切换后端后检索为空 | 新后端尚未入库，执行对应的 `reset-*` |

更多见 `AGENT_GUIDE.md`、`WORKLOG.md`、`OPTIMIZATION_PLAN.md`。
