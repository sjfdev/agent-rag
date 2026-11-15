# Changelog

All notable behavior changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`src/version.py` holds the running version, `docker-compose.prod.yml` uses
`APP_TAG` for the image tag, and `scripts/check_release.py` verifies that the
three agree before you push a tag.

## [Unreleased]

### Added

- **Shared console login and section menu**: `/admin` and `/ui/` mirror the JWT
  under a shared `ragBearer` sessionStorage key, so one login covers both pages
  (same tab) and either logout clears both; `ADMIN_TOKEN` and the chat
  `X-API-Key` stay page-local. The console also gained a sticky section menu
  that scrolls to and highlights each card, hides entries for unavailable
  panels, and turns into a horizontal menu on narrow screens.
  <br>**管理台共享登录与分区菜单（中文）**：`/admin` 与 `/ui/` 用共享的
  `ragBearer`（sessionStorage）复用 JWT，同标签页登录一次即可，任一页登出同时
  清除；`ADMIN_TOKEN` 与聊天 `X-API-Key` 仍各自保存。管理台新增左侧固定分区
  菜单，点击滚动到对应卡片并高亮当前分区，不可用入口自动隐藏，窄屏转横向。
- **Console model overrides**: the admin console's new 模型 card changes the
  chat endpoint (`api_base`/`model`/`api_key`/`temperature`/`max_tokens`) without
  a restart. `GET/PUT/DELETE /admin/api/model` and `POST /admin/api/model/test`
  (an 8-token probe) persist to `config/model_settings.json` (0600, gitignored),
  return only a masked key hint, and audit `model.update`/`model.reset` without
  the secret. `src/agent.py` rebuilds the bound model when the file changes, so
  the next turn uses it; `.env` stays the default and `DELETE` reverts. Ingestion
  and retrieval keep the `EMBEDDING_*` settings. `LLM_TEMPERATURE` is now
  configurable (default `0.0`).
  <br>**管理台模型覆盖（中文）**：管理台新增「模型」卡片，可不重启地修改对话端点
  （`api_base`/`model`/`api_key`/`temperature`/`max_tokens`）。
  `GET/PUT/DELETE /admin/api/model` 与 `POST /admin/api/model/test`（8 token 探测）
  将配置持久化到 `config/model_settings.json`（0600、已 gitignore），响应只返回
  掩码密钥提示，审计 `model.update`/`model.reset` 但不含密钥。`src/agent.py` 在文件
  变化后重建绑定模型，下一轮对话即生效；`.env` 仍为默认，`DELETE` 回退。入库与检索
  继续使用 `EMBEDDING_*`。`LLM_TEMPERATURE` 现可配置（默认 `0.0`）。
- **TLS edge**: the production compose adds a `caddy` sidecar (internal CA,
  `KB_SITE_ADDRESS`, default `kb.internal`) and publishes only `:443`; the app
  binds `127.0.0.1:8000`, so logins and chat never travel in cleartext. Caddy
  overwrites `X-Forwarded-For` with the real hop (a caller cannot spoof it to
  dodge per-IP limits) and answers `/metrics` with 404, keeping the scrape on
  the container network.
- **Boot recovery and the nightly backup** (macOS launchd,
  `scripts/install_launchd.py`): `com.agent-rag.boot-recover` starts colima when
  Docker is down, brings up the production and monitoring stacks and waits for
  `/info`; `com.agent-rag.daily-backup` runs at 03:30, silences
  `AgentRagHealthDown` for the window, pauses the stack, archives with
  `scripts/backup.py`, keeps the newest `BACKUP_KEEP` archives (14 by default),
  and restarts in a `finally` block. Entry points: `make daily-backup`,
  `make boot-recover`, `make agents-install|status|uninstall`. Measured on this
  host: cold start 2m03s, backup downtime about a minute.
- **Backup freshness metrics**: `agent_rag_backup_age_seconds` and
  `agent_rag_backup_archives` stat `BACKUP_DIR` on scrape (production mounts
  `./backups` read-only at `/app/backups`); `AgentRagBackupStale` fires after
  48 hours without a new archive and the dashboard gains a "Newest backup
  (hours)" panel, so a silently failing nightly job is visible.

### Fixed

- **Image builds behind a flaky network**: the model-download layer sets
  `HF_HUB_DISABLE_XET=1`. Hugging Face's Xet CDN (`transfer.xethub.hf.co`) was
  dropping transfers mid-download even when the mirror itself worked, which
  broke `docker compose build` before the app layers were reached.

## [1.1.0] - 2026-11-14

### Added

- **Citation preview**: clicking a citation in the chat UI opens a modal with the
  full indexed text behind it instead of the truncated 220-character snippet.
  `GET /sources/content` resolves `source` plus page/slide/sheet through the
  vector store (both backends), filters by the caller's departments *inside* the
  store and then by the document ACL, and is gated by `CHAT_API_KEY` like
  `/chat`. Preview text is rendered with `textContent`, so indexed content
  cannot inject HTML. The chat page also adapts to phones (`100dvh`,
  safe-area insets, full-screen preview, 16px input to avoid iOS zoom,
  horizontally scrollable presets).
- **Backup and restore**: `scripts/backup.py` archives `data/`, `config/`
  (local accounts and ACL rules), `logs/` and the live vector store into a
  timestamped `tar.gz` with `manifest.json`, per-file `SHA256SUMS` and a
  `.sha256` sidecar. `scripts/restore.py` verifies every hash before swapping
  directories, refuses to run while the API or Milvus/etcd is up, keeps the
  previous copy by default and rolls back a half-finished swap. Entry points:
  `make backup` and `make restore ARCHIVE=...`; `make doctor` warns when the
  newest archive is older than `BACKUP_WARN_DAYS` (default 30).
- **Disk headroom in `/health` and `doctor`**: every directory the app writes to
  (`DATA_DIR`, `DB_PATH`, the job and audit logs) is checked against
  `HEALTH_MIN_FREE_MB` (default 512). A full disk never fails queries, so the
  endpoint stayed green while ingestion broke; Milvus is worse because it caches
  the first ENOSPC in RocksDB and keeps refusing writes after space is freed
  until the container is restarted. `/health` now returns `degraded` with a
  `storage` block, and `scripts/doctor.py` fails with the tightest path.
- **Chinese/English switch** in both UIs (`static/i18n.js`, follow the browser
  language by default, remembered in `localStorage`).
- **Server-side sessions**: `POST /sessions` creates a conversation and
  `ChatRequest.session_id` hands ownership of the transcript to the server, so
  the client sends only the newest message instead of replaying the whole
  history (body size stays constant over any number of turns). Sessions are
  isolated by `user_id`, can be listed/read/deleted via `/sessions`, and are
  stored in SQLite at `SESSIONS_DB_PATH` (default `./logs/sessions.db`, already
  mounted and backed up). Conversations longer than `2 × HISTORY_MAX_MESSAGES`
  are folded into a running summary with one extra LLM call per window
  (`SESSION_SUMMARY_*`); if summarization fails, the prompt falls back to the
  last window. New metric: `agent_rag_session_summaries_total{result}`.
- **Chat session sidebar**: the chat page lists server-side sessions in a left
  sidebar (new/switch/delete), creates one lazily on the first turn, sends
  `session_id` instead of local `history`, and restores the transcript from
  `/sessions/{id}` after a reload. `/info` exposes `sessions_enabled` and
  `history_max_messages`; disabling sessions hides the sidebar and keeps the
  localStorage fallback.
- **Monitoring stack**: `docker-compose.monitoring.yml` runs Prometheus,
  blackbox_exporter and Grafana on the `agent-rag-prod` network. Grafana
  provisions the "Agent RAG" dashboard (request/error rate, p95 latency, time
  to first token, cache hit ratio, live index chunks, retrieval engine,
  readiness, running build). Six alert rules cover scrape failure, `/health`
  non-2xx, an empty index, a chat error rate above 5%, a cache-hit drop, and a
  stuck rebuild. UIs bind to `127.0.0.1`; `make monitor-up` /
  `make monitor-down`; `make monitor-check` validates every config file
  offline and ensures each `agent_rag_*` metric in the rules/dashboard exists
  in `src/metrics.py`. See `monitoring/README.md`.
- **Alert delivery**: Prometheus forwards firing alerts to an Alertmanager
  service (`127.0.0.1:9093`). Delivery channels are opt-in via `.env` — email
  (`ALERTMANAGER_EMAIL_TO` + SMTP settings, password in the gitignored
  `monitoring/alertmanager/smtp_password`), Slack (`ALERTMANAGER_SLACK_WEBHOOK_URL`)
  and a generic webhook (`ALERTMANAGER_WEBHOOK_URL`); with none configured a
  `noop` receiver keeps the config valid. `scripts/render_alertmanager.py`
  (`make monitor-config`, also run by `monitor-up`/`monitor-check`) renders the
  gitignored `alertmanager.yml` and refuses an incomplete email setup.
  Grouping is `alertname`+`severity`; `AgentRagMetricsDown` inhibits the alerts
  derived from the same app metrics so an unreachable instance is not repeated.
- **Log shipping**: Loki (single node, tsdb, `LOKI_RETENTION` default 30 days)
  plus Promtail, which discovers `agent-rag-*` containers through the Docker
  socket, keeps their json-file logs and labels them with
  `container`/`service`/`stream`. Grafana provisions a Loki datasource and the
  dashboard gains an "Application logs (Loki)" panel (`{service="app"}`); use
  `|= "<request_id>"` to trace a failing request. `DOCKER_SOCKET` /
  `DOCKER_CONTAINERS_DIR` cover non-default daemons, and Promtail can be
  started alone or omitted without affecting metrics and alerts.
- **Stream cancellation on disconnect**: `/chat/stream` now runs the graph's
  async node (`src/agent.py`: `call_model_async` / `app_async` with
  `llm.ainvoke`). When the client closes the page or drops the connection,
  Starlette cancels the response task and that cancellation reaches the
  in-flight LLM request, which is aborted instead of generating an answer
  nobody will read. A cancelled turn is neither cached nor stored in the
  server-side session, and it is counted separately
  (`agent_rag_chat_cancelled_total{endpoint}` and
  `agent_rag_chat_requests_total{status="cancelled"}`) so the error-rate
  alert is not triggered by users navigating away. `/chat` remains
  synchronous and cannot interrupt a call already in flight.

### Changed

- `make run` binds the API to `127.0.0.1` by default; put a reverse proxy in
  front when the service must be reachable from other hosts.
- **Project scope is single-machine deployment**: SaaS multi-tenancy,
  per-tenant quotas and billing are out of scope. The existing department
  isolation, document ACL, authentication and backup/restore remain the
  supported isolation and operations model.

## [1.0.0] - 2026-11-01

First version cut from a green CI run and a repeatable release checklist. The
API surface below is what operators can rely on.

### Added

- **Multi-format ingestion** (`pdf/docx/pptx/xlsx/csv/md/txt/html/json`) with
  incremental updates: unchanged files are skipped by hash, changed files are
  written first and the old version is deleted afterwards, removed files are
  pruned.
- **Zero-downtime rebuild**: `--reset` and the console build into a shadow
  index (Milvus temp collection + alias swap, FAISS temp dir + `os.replace`), so
  queries keep hitting the live data.
- **Citations**: answers return `sources` with `source`, `page`/`slide`/`sheet`,
  `kind`, a text `snippet` and a relative `score`.
- **Hybrid retrieval**: server-side sparse BM25 on Milvus >= 2.5, in-process
  BM25 otherwise, fused with RRF (`HYBRID_ENGINE=auto|local|milvus`).
- **Optional cross-encoder reranking** (`RERANK_ENABLED`, off by default).
- **Degraded answers**: when the LLM is unreachable after retrieval the graph
  answers from the top excerpts (`degraded=true`) and never caches it.
- **Retrieval evaluation**: `scripts/eval_retrieval.py` reports hit@k, MRR and
  cross-department leakage from `eval/cases.jsonl`.
- **Safe Markdown replies** in the chat UI (`static/markdown.js`): bold/italic,
  inline and fenced code, tables, lists, quotes and links, rendered from an
  escaped allowlist so model output can never inject HTML; raw HTML is shown as
  text and only `http(s)`/`mailto` links become clickable.
- **Admin console** `/admin`: upload, list, delete, incremental/full ingestion,
  background jobs persisted to disk, audit log, upload quotas, account card for
  local users.
- **Authentication** `AUTH_MODE=none|jwt`: the bearer token is the only source of
  user/departments/groups/admin; body identity is ignored, the cache scope is
  derived from the token, and a secret shorter than 32 characters refuses
  startup.
- **OIDC**: `AUTH_JWKS_URL`/`AUTH_JWKS_FILE` verification with `kid` rotation,
  configurable claim mapping (`AUTH_CLAIM_*`, `AUTH_ADMIN_ROLE`,
  `AUTH_ROLE_DEPT_PREFIX`, `AUTH_DENY_ROLE`, `AUTH_DEFAULT_DEPTS`), plus a full
  browser code flow (`/auth/login` → `/auth/callback` → `/admin#token=...`)
  protected by a signed state and a state cookie.
- **Offline local accounts** `AUTH_LOCAL_LOGIN_ENABLED`: scrypt password hashes
  in `AUTH_USERS_FILE`, `POST /auth/login` mints the same HS256 bearer token,
  `scripts/manage_users.py` and the console's account card manage users, and a
  `ver` claim revokes outstanding tokens on disable/password/permission change.
- **Observability**: `/health` (readiness, `?probe=1` for the LLM), Prometheus
  `/metrics`, request ids, per-request `timings`.
- **Operational tooling**: `scripts/doctor.py`, `check_llm.py`, `check_oidc.py`,
  `check_health.py`, `check_admin.py`, `check_loaders.py`, `check_hybrid*.py`,
  `verify_retrieval.py`, `bench_retrieval.py`.
- **Delivery**: CPU-only `Dockerfile`, `docker-compose.prod.yml` (etcd + Milvus
  + app), healthcheck on `/info` with `/health` as the readiness probe.

### Security

- Department names are validated before being embedded in Milvus expressions;
  invalid values return 400.
- Optional document-level ACL (`ACL_ENABLED`) applied after retrieval for both
  backends. **Fails closed**: an unusable rules file refuses startup, makes
  `/health` report down, and drops all retrieved documents.
- The admin API rejects foreign `Origin` headers and is loopback-only unless
  `ADMIN_TOKEN` is set; `/metrics` can require `METRICS_TOKEN`.
- OIDC login binds the signed `state` to an `HttpOnly; SameSite=Lax` cookie and
  only accepts same-origin `next` targets; the returned token is escaped for
  `<script>`.
- Local login is rate limited per IP and audited; failures answer one generic
  401 so usernames cannot be enumerated, and the last enabled administrator
  cannot be demoted, disabled or deleted.

### Known limitations

- Session state is process-local: ingestion jobs, caches, rate limits and the
  BM25 index assume one worker; run one app replica, or add Redis first.
- `/metrics` aggregates per process, not across workers.
- Conversations are not stored server-side; the client sends `history` back.
- Multi-tenancy (`tenant` metadata) is not implemented.
- The ACL is a static JSON file without read/write/download distinctions.

[Unreleased]: https://github.com/sjfdev/agent-rag/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/sjfdev/agent-rag/releases/tag/v1.1.0
[1.0.0]: https://github.com/sjfdev/agent-rag/releases/tag/v1.0.0
