# Agent RAG — 企业知识库问答

一个可私有化部署的企业知识库 RAG Agent：多格式文档入库、部门级权限隔离、混合检索、可追溯引用，附带对话测试台与入库管理台。

```
文档 (PDF/DOCX/PPTX/XLSX/CSV/MD/TXT/HTML/JSON)
        │  按部门目录组织
        ▼
   ETL（切分 + 元数据 + 内容哈希增量）
        ▼
 FAISS / Milvus 2.5（稠密向量 + 稀疏 BM25）
        ▼
 混合检索（RRF 融合）→ 可选 CrossEncoder 重排
        ▼
 LangGraph Agent ──► 答案 + 来源（文件/页码/原文摘录）
```

## 能力

| 能力 | 说明 |
|---|---|
| 多格式入库 | 9 种格式；`page`/`slide`/`sheet` 定位信息随 chunk 保存 |
| 增量更新 | 按内容哈希跳过未变文件；变更文件先写新再删旧；删除的文件自动 prune |
| 零停机重建 | Milvus 写影子集合 + alias 切换；FAISS 临时目录 + 原子替换 |
| 部门隔离 | Milvus `dept` 表达式 / FAISS 后过滤；支持多部门用户 |
| 文档级 ACL | 可选 JSON 规则，按用户或 `group:xxx` 授权 |
| 混合检索 | 向量 + BM25（Milvus 2.5 服务端稀疏 / 进程内回退），RRF 融合 |
| 可选重排 | CrossEncoder，默认关闭；实测 MRR 0.88 → 0.97 |
| 引用可追溯 | 答案附带来源、页码与原文摘录，UI 可展开 |
| 缓存 | FAQ 答案缓存（4ms 命中）+ 查询向量缓存 |
| 治理 | `/health`、Prometheus `/metrics`、操作审计、任务持久化、限流、请求超时 |
| 安全 | 部门名校验、`/chat` 可选 API Key、管理台令牌或仅回环 |
| 质量保障 | 309 项离线测试、92% 覆盖率、ruff、GitHub Actions |

## 快速开始

```bash
conda create -n agent-rag python=3.11 -y && conda activate agent-rag
make install
cp .env.example .env        # 填入 LLM_API_BASE / LLM_API_KEY / LLM_MODEL
make data && make ingest
make run                    # http://localhost:8000/ui/
```

本地开发按上面的步骤；**容器化部署**用生产 compose：

```bash
cp .env.example .env      # 填 LLM_*，建议设置 ADMIN_TOKEN / CHAT_API_KEY
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml exec app python run_ingestion.py
```

完整步骤与排错见 **[QUICKSTART.md](QUICKSTART.md)**；要把它做成对外产品（登录、多租户、计费、发布流程），见 **[PRODUCTIZATION.md](PRODUCTIZATION.md)**。

## 接口

| 端点 | 用途 |
|---|---|
| `POST /chat` | 问答（返回答案、来源、耗时） |
| `POST /chat/stream` | SSE 流式问答 |
| `GET /health` | 存活/就绪探针（`?probe=1` 探测 LLM） |
| `GET /metrics` | Prometheus 指标 |
| `GET /info` | 后端、模型与实际检索引擎 |
| `/ui/` | 对话测试台 |
| `/admin` | 上传/删除/入库/重建/审计 |

```bash
curl -s http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"u1","user_dept":"Tech","message":"代码评审有什么要求？"}'
```

## 技术栈

Python 3.11 · FastAPI · LangGraph / LangChain · FAISS / Milvus 2.5.4 · sentence-transformers · rank-bm25 · Prometheus · pytest / ruff

## 文档

| 文件 | 内容 |
|---|---|
| [QUICKSTART.md](QUICKSTART.md) | 5 分钟跑通 |
| [AGENT_GUIDE.md](AGENT_GUIDE.md) | 完整使用说明、配置项、FAQ、性能调优 |
| [DEPLOYMENT_CN.md](DEPLOYMENT_CN.md) / [DEPLOYMENT.md](DEPLOYMENT.md) | 私有化部署 |
| [WORKLOG.md](WORKLOG.md) | 迭代记录、基准数据、验证矩阵 |
| [OPTIMIZATION_PLAN.md](OPTIMIZATION_PLAN.md) | 待办清单（第 10 节为当前计划） |
| [PRODUCTIZATION.md](PRODUCTIZATION.md) | 打包发布、登录/多租户/计费路线图 |
| [AGENTS.md](AGENTS.md) | 协作约定与常用命令 |

## 已知限制

- 服务端不存储会话，多轮依赖客户端回传 `history`
- 文档级 ACL 为静态 JSON 规则；`/chat` 鉴权为可选的静态 API Key（生产建议接网关/OIDC）
- 进程内缓存/限流/任务状态不跨 worker；多副本需 Redis 等外部组件
- 扫描版 PDF 需要先 OCR；超大文件为一次性读入

## 许可证

MIT，见 [LICENSE](LICENSE)。
