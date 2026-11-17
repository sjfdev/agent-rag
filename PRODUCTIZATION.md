# 从项目到产品：发布路径

这份文档说明如何把当前仓库变成可交付的产品，按「立即可做 → 产品化补强 → 商业化」三层排列，每项都给出具体文件和命令。

> **范围变更（2026-11-14）**：项目定位为**单机部署**，取消 SaaS 方向。§2.2 多租户与 §2.3 配额计费不再排期，保留作为历史参考；身份、可观测性与备份/恢复仍是有效内容。

---

## 0. 先明确：现在处于哪一步

当前已具备的能力（可直接对外试用）：

- 完整 RAG 链路、部门/文档权限、混合检索、引用、缓存、降级
- 管理台、健康检查、指标、审计、限流
- 746 项离线测试 + GitHub Actions
- 一键启动（Makefile）与离线安装说明

还缺的「产品」要素：

| 缺失 | 影响 | 优先级 |
|---|---|---|
| 容器化交付 | 客户无法一键部署 | P0 |
| 登录与用户体系 | 谁都能调 `/chat` | P0 |
| 多租户隔离 | 一家客户的数据会看到另一家 | 不做（单机部署；部门 + ACL 已足够） |
| 计费/配额 | 无法收费 | 不做（单机部署） |
| 可观测性接入（告警） | 出问题没人知道 | P1 |
| 前端产品化 | 体验像内部工具 | P1 |
| 版本发布流程 | 交付不可重复 | P1 |
| 数据备份/恢复演练 | 出事故无法兜底 | P1 |

---

## 1. 立即可做：打包与部署（本仓库已提供）

### 1.1 单机 Docker 部署

```bash
cp .env.example .env
# 必填：LLM_API_BASE / LLM_API_KEY / LLM_MODEL
# 建议：ADMIN_TOKEN、CHAT_API_KEY、CORS_ORIGINS
docker compose -f docker-compose.prod.yml up -d --build

# 首次入库（镜像内已带 embedding 模型，不联网）
docker compose -f docker-compose.prod.yml exec app python run_ingestion.py

# 验证
curl -fsS localhost:8000/health
curl -fsS localhost:8000/info
```

组成：`etcd` + `milvus` + `app`，`app` 只暴露 8000，Milvus 仅在内部网络。
数据卷：`./data`（原始文件）、`./logs`、`./volumes`（Milvus/etcd）。

### 1.2 交付物清单

| 产物 | 命令 | 说明 |
|---|---|---|
| 应用镜像 | `docker build -t agent-rag:x.y.z .` | CPU-only，模型内置 |
| 镜像归档 | `docker save agent-rag:x.y.z \| gzip > agent-rag-x.y.z.tgz` | 内网交付用 |
| 依赖 wheel | `pip download -r requirements.txt -d wheels/` | 完全离线安装 |
| 版本 | `git tag vx.y.z` | 见 3.1 |

### 1.3 反向代理（必须）

应用本身不做 TLS。生产前置 Caddy 或 Nginx：

```caddyfile
kb.example.com {
    encode gzip
    reverse_proxy 127.0.0.1:8000
    # 流式接口必须关闭缓冲
    @stream path /chat/stream
    reverse_proxy @stream 127.0.0.1:8000 {
        flush_interval -1
    }
}
```

Nginx 对应 `proxy_buffering off; proxy_read_timeout 300s;`（SSE 必需）。

---

## 2. 产品化补强

### 2.1 登录与用户体系（P0）

现在 `user_id` / `user_dept` 由调用方传入，任何拿到地址的人都能冒充任意部门。两条路线：

**A. 接入现有 OIDC（推荐，1–2 天）**

- 在反向代理或应用层做 OIDC 登录（Keycloak / Auth0 / 企业微信 / 飞书）
- 从 token 的 claim 中取 `sub` → `user_id`，取组/部门 → `user_dept(s)`
- 服务端只信任网关注入的 header，忽略客户端自己传的 `user_id`/`user_dept`
- 落点：`src/chat_service.py::prepare` 之前增加一层身份解析；`main.py` 增加依赖

**B. 自带账号（3–5 天）**

- 用户表 + 部门表 + 角色（管理员/普通用户）
- 密码哈希（argon2）+ 会话（JWT 或服务端 session）
- 管理台用同一套登录，不再依赖 `ADMIN_TOKEN`

无论哪条路线，都必须保留「文档级 ACL」作为最后一道闸。

### 2.2 多租户（SaaS 必需）

当前隔离维度是「部门」，SaaS 需要「租户」：

- chunk 元数据增加 `tenant`，检索表达式变成 `tenant == 'x' and dept in [...]`
- 租户与向量集合的关系二选一：
  - **共享集合 + tenant 过滤**（省资源，需严格测试）
  - **一租户一集合**（隔离更彻底，运维成本高）
- 管理台、审计日志、任务队列都要带租户维度
- 注意：Milvus schema 变更需要重建，先把方案定下来再改

### 2.3 配额与计费（P1）

- 按 `user`/`tenant` 统计 token 与请求数：在 `src/metrics.py` 增加计数器，落库而非只存进程内
- 配额检查放在 `chat_service.prepare`，与限流同一位置
- 计费口径建议：只统计成功完成的 LLM 调用（`llm_calls`），失败/降级不计费

### 2.4 可观测性（P1，告警已完成 2026-11-10）

- [x] Prometheus 抓取 + Grafana 面板（QPS、P95、错误率、缓存命中、索引块数）：
  `docker-compose.monitoring.yml` + `monitoring/`，`make monitor-up` / `make monitor-check`
- [x] 告警项（`monitoring/prometheus/alerts.yml`，6 条）：
  - `AgentRagErrorRate`：chat 错误率 5 分钟 > 5%
  - `AgentRagCacheHitDrop`：10 分钟缓存命中率低于 1 小时基线的一半
  - `AgentRagIndexEmpty`：`agent_rag_index_chunks == 0` 持续 5 分钟
  - `AgentRagHealthDown`：blackbox 探测 `/health` 连续 3 次非 2xx
  - `AgentRagMetricsDown`：`up{job="agent-rag"} == 0`
  - `AgentRagMaintenanceStuck`：重建标志持续 1 小时
- [ ] 告警通知落地：加 Alertmanager + 邮件/Slack/Webhook 路由
- [ ] 日志：当前是 stdout，接入 Loki/ELK；`request_id` 已贯穿

### 2.5 前端产品化（P1）

现有 `/ui/` 是测试台。要变产品至少需要：

- 登录、租户/部门切换（来自身份，不由用户填）
- 会话列表与历史（服务端存储，见 2.6）
- Markdown 渲染（当前纯文本）
- 移动端适配
- 引用预览：点击来源直接看原文片段

### 2.6 服务端会话（P1，已完成 2026-11-10）

- [x] 会话表（id、user、dept、title、时间）+ 消息表（role、content、sources、degraded）：`src/sessions.py`（SQLite，`SESSIONS_DB_PATH`）
- [x] 服务端只使用最近 N 条，超出 `2 × HISTORY_MAX_MESSAGES` 自动摘要（每个窗口多一次 LLM 调用，`SESSION_SUMMARY_*`；失败降级为最近窗口）
- [x] `POST/GET/DELETE /sessions` + `ChatRequest.session_id`；会话按 `user_id` 隔离；`logs/` 已纳入备份
- [ ] 多租户维度（做 SaaS 时随 11.1.2 一起加 `tenant`）

### 2.7 发布流程（P1）

```bash
# 版本号遵循 semver
git tag -a v1.2.0 -m "release"
git push origin v1.2.0
```

建议补充：

- `CHANGELOG.md`（按版本记录行为变更）
- 镜像 tag 与 git tag 对齐（`APP_TAG=v1.2.0`）
- 发布前跑：`make check && make doctor && python scripts/eval_retrieval.py`
- 数据库/schema 变更写迁移说明（当前只有 `--reset` 重建）

### 2.8 备份与恢复演练（P1）

- 备份 `data/`（原始文件）+ `volumes/`（索引）+ `logs/` + `config/`（`users.json` 账号库与 `acl.json` 规则）
- 每季度做一次「删库 → 恢复 → 验证」演练
- 恢复后跑 `scripts/verify_retrieval.py` 与 `eval_retrieval.py` 确认数据完整

---

## 3. 商业化形态（P2）

按投入从小到大：

| 形态 | 说明 | 适合 |
|---|---|---|
| 私有化交付 | 卖镜像 + 部署文档，客户自己运维 | 政企、金融 |
| 私有化 + 托管运维 | 你远程运维，按年收费 | 中型企业 |
| ~~SaaS 多租户~~ | ~~统一运维，按席位/调用量收费~~ | 不做（2026-11-14 取消） |
| 行业模板 | 在通用产品上预置行业知识库与提示词 | 垂直行业 |

无论哪种形态，**先解决 2.1 身份与 2.2 租户**，否则无法对外。

### 3.1 试用、演示与用量月报（已落地）

**30 天试用许可（最长 90 天）**

```bash
python scripts/issue_license.py --trial --licensee "ACME 有限公司" \
  --out /tmp/acme-trial.json
```

默认 `edition=trial`、30 天、1 个部门、5000 份文档、完整功能开关（`sso,audit,monitoring`）。`--days` 可缩短，超过 90 天会被拒绝；显式 `--depts/--docs/--features` 仍可覆盖。管理台「许可」卡片会显示试用提示，`GET /info.license.trial` 为 `true`。到期后对话与引用照常，宽限期结束只暂停管理端写入——试用转正式不需要停机。

签发入口：`make license-console` 页面有「填入 30 天试用」按钮；打包时 `make package LICENSE=<trial.json>` 可把试用许可直接放进交付包。

**演示站（公开试用/获客）**

```bash
cp .env.demo.example .env.demo     # 填 LLM_API_BASE / LLM_API_KEY / LLM_MODEL
make data                          # 生成 data/Demo（已存在则跳过）
make demo-up                       # 127.0.0.1:8080，FAISS，无许可
make demo-seed                     # 入库 data/Demo
```

演示栈只挂载 `data/Demo`（不暴露真实部门）、关闭服务端会话（不保存访客对话）、关闭许可、管理端保持回环限制，并默认限流 10 次/分钟。对公网开放前必须在前面加 TLS 反向代理，并把 `DEMO_PORT` 改成内网端口；不要复用真实客户的 `.env`。

**用量与价值月报**

```bash
python scripts/usage_report.py --month 2026-10 --format markdown \
  --minutes-per-question 3 --hourly-rate 80 --out reports/acme-2026-10.md
```

读取 `logs/sessions.db`、`logs/admin_audit.log` 与当前许可，输出会话/活跃用户/提问/回答/引用覆盖/降级/满意度/部门分布与管理操作次数；`--minutes-per-question` + `--hourly-rate` 会额外生成「节省工时/折算金额」估算。报告只含聚合计数，不含对话原文或用户标识，可直接交给客户财务/采购，也是续费 QBR 的证据。`--format` 支持 `text/markdown/csv/json`；`--out` 的后缀会自动推断格式。


---

## 4. 建议的实施顺序

1. **本周**：`make demo-up` 准备演示环境 → 找 1 个真实用户试用，发 30 天 `--trial` 许可，收集问题
2. **第 2 周**：接 OIDC 登录（2.1-A），把 `user_id`/`user_dept` 变成服务端可信
3. **第 3 周**：Grafana 告警 + 备份演练（2.4、2.8）
4. **第 4 周**：前端产品化最小版（2.5）+ 服务端会话（2.6）；用 `scripts/usage_report.py` 出第一份月报
5. **之后**：不再引入多租户/计费；按需做版本迭代，并完成真实 IdP 登录与每季度恢复演练

---

## 5. 已知的产品化风险

- **单 worker**：入库任务、缓存、限流都是进程内状态。多副本前必须换成 Redis/RQ，否则状态不一致
- **多副本写入**：FAISS 只允许单写者；单机部署保持单进程即可，扩容再切 Milvus
- **LLM 成本不可控**：缓存与降级已缓解，但仍建议在网关层设月度预算上限
- **schema 变更成本**：Milvus 集合加字段需要全量重建，改动前要评估停机窗口
- **数据合规**：审计日志含操作者标识，需按客户要求设置保留期与脱敏策略
