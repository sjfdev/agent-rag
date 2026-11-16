# Changelog

All notable behavior changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`src/version.py` holds the running version, `docker-compose.prod.yml` uses
`APP_TAG` for the image tag, and `scripts/check_release.py` verifies that the
three agree before you push a tag.

## [Unreleased]

### Added

- **Console file & vector management**: the 入库文件 card and the vector
  browser are merged into one 文件与向量 card that inspects what is actually
  indexed: `GET /admin/api/vectors` returns backend
  stats plus per-document chunk counts (`indexed`/`changed`/`pending`/`orphan`,
  filterable by department), `GET /admin/api/vectors/chunks` pages through the
  stored text for one `doc_id`, and `POST /admin/api/vectors/search` replays a
  retrieval query with scores. `DELETE /admin/api/vectors/{doc_id}` drops only
  the vectors (the file stays, so the next incremental ingestion rebuilds them)
  and `POST /admin/api/vectors/prune` removes orphan chunks whose source file is
  gone; both audit as `vector.delete`/`vector.prune` and return 409 while an
  ingestion job is running.
  <br>**管理台文件与向量管理（中文）**：把「入库文件」与向量浏览合并成一张
  「文件与向量」卡片，查看真正入库的数据：`GET /admin/api/vectors` 返回后端统计与按文档的 chunk 数（`indexed`/
  `changed`/`pending`/`orphan`，可按部门筛选），`GET /admin/api/vectors/chunks`
  分页查看某个 `doc_id` 的原文，`POST /admin/api/vectors/search` 重放一次检索
  并显示分数。`DELETE /admin/api/vectors/{doc_id}` 只删向量、保留磁盘文件（再
  次增量入库会重建），`POST /admin/api/vectors/prune` 清理源文件已不存在的孤儿
  向量；两者审计为 `vector.delete`/`vector.prune`，入库任务运行时返回 409。
- **Shared console login and section menu**: `/admin` and `/ui/` mirror the JWT
  under a shared `ragBearer` localStorage key, so one login covers both pages,
  survives new tabs and browser restarts, and either logout clears both;
  older `sessionStorage` sessions are migrated on first read. `ADMIN_TOKEN` and
  the chat `X-API-Key` stay page-local. The console also gained a tab-style
  section menu: only the selected card is shown, entries for unavailable panels
  are hidden, and it turns into a horizontal menu on narrow screens.
  <br>**管理台共享登录与分区菜单（中文）**：`/admin` 与 `/ui/` 用共享的
  `ragBearer`（localStorage）复用 JWT，登录一次后新标签页/重启都保持，任一页
  登出同时清除（旧的 `sessionStorage` 会话在首次读取时迁移）；`ADMIN_TOKEN` 与
  聊天 `X-API-Key` 仍各自保存。管理台左侧分区菜单改为标签页式切换，右侧只显示
  当前分区，不可用入口自动隐藏，窄屏转横向。
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

- **Console themes**: the admin header can switch between the dark and light
  presets or open a custom palette (background, panel, text, accent); borders,
  hover and status colors are derived from those four colors. The choice is
  stored in `localStorage` and applied before first paint. `static/theme.js`
  holds the palette math shared by the console.
  <br>**管理台主题（中文）**：顶栏可在深色/浅色预设与自定义配色之间切换
  （背景、面板、文字、主色），边框/悬停/状态色由这四个颜色自动推导；选择存在
  `localStorage` 并在首屏前应用，避免闪烁。`static/theme.js` 负责配色计算。

- **Console tab and feedback polish**: the section menu remembers the last
  panel and refreshes it on switch, wide tables scroll horizontally on narrow
  screens instead of squashing columns, and every `setMsg` result is mirrored
  in a fixed toast (the inline message stays, with `aria-live`). Primary
  buttons pick a black/white foreground from the accent luminance, so light
  custom accents stay readable.
  <br>**管理台标签与反馈优化（中文）**：分区菜单记住上次面板并在切换时刷新，宽表格
  在窄屏横向滚动而不再挤压列；`setMsg` 的结果除行内提示外还镜像到固定 toast（行内
  消息保留并带 `aria-live`）。主按钮根据主色亮度自动选黑/白前景，浅色自定义主色
  也保持可读。

- **Console information architecture**: the section menu is grouped into
  内容/配置/运维 with the rebuild entry marked as dangerous; tabs expose
  `role=tab`/`aria-selected` and switch with arrow keys. The header folds
  backend/model/upload-limit into a status popover and adds a current-department
  badge plus an "ingesting" pill that jumps to the job log. Retrieval replay is
  collapsed inside 文件与向量, and the model/account forms use explicit labels;
  the model card shows an "unsaved" badge and warns before leaving the page.
  <br>**管理台信息架构（中文）**：分区菜单分为内容/配置/运维，重建索引标红；标签页
  具备 `role=tab`/`aria-selected` 并支持方向键切换。顶栏把后端/模型/上限折叠进“状态”
  弹层，并新增当前部门徽标与可跳转任务日志的“入库中”指示；检索测试折叠在“文件与
  向量”内，模型/账号表单改用显式标签；模型卡显示“未保存”并在离开页面前提醒。

- **Console polish (P2)**: below 1000px the menu becomes a sticky horizontal
  tab strip; cards show an in-flight spinner and the status popover reports the
  last refresh time. The file table sorts on any column and pages at 50 rows;
  audit entries can be filtered by text and time window with a 20–200 limit.
  The custom-theme dialog gains four presets and live preview (cancel reverts),
  and states that it applies to the admin console only.
  <br>**管理台细节（P2，中文）**：≤1000px 菜单变为顶部吸附的横向标签栏；卡片
  加载时显示角标，状态弹层显示最后刷新时间。文件表可按任意列排序并按 50 条分页；
  审计支持文本与时间筛选及 20–200 条切换。自定义主题弹窗新增四套预设与实时预览
  （取消即回滚），并标注仅作用于管理台。

- **Console: global department, split index card, shared table states**: the
  header badge is now a department picker that stays in sync with the
  department card and re-scopes uploads/ingestion immediately. Index
  maintenance (ingest, orphan cleanup, retrieval replay) moved to its own
  「索引维护」 section, leaving the file list focused on browsing. Tables share
  skeleton, empty and error+retry states.
  <br>**管理台：全局部门、索引维护分卡、统一三态（中文）**：顶栏部门徽标改为
  可切换的下拉，与部门卡同步，并立即影响上传/入库范围；索引维护（入库、清理孤儿、
  检索测试）拆到独立「索引维护」分区，文件列表专注浏览；表格统一骨架/空态/
  错误+重试三态。
- **Console: dangerous zone, counts, cached sections, audit export**: the
  rebuild entry now sits in its own 危险区 group with a separator and requires
  typing `RESET` after the checkbox; menu badges show pending files and
  accounts; tab switches reuse data for 30s, and narrow tab bars fade the right
  edge and scroll the active section into view; file actions stay fixed while
  the table scrolls and sortable headers work from the keyboard; audit times
  render locally with success/failure badges and export to CSV.
  <br>**管理台：危险区、菜单计数、分区缓存、审计导出（中文）**：重建索引移入
  独立「危险区」并加分隔线，勾选后仍需输入 `RESET`；菜单显示待入库与账号数；
  分区切换 30 秒内复用数据，窄屏标签栏右缘渐隐并把当前分区滚入视野；文件表滚动
  时操作列固定、表头支持键盘排序；审计时间本地化、成败徽章并支持 CSV 导出。
- **Console: themed confirmations, job recovery, section titles**: destructive
  actions (delete user/file/vectors, prune orphans) now use one themed dialog
  instead of native `confirm()`; a page reload reattaches to the running
  ingestion job and keeps polling its log; the browser title follows the active
  section.
  <br>**管理台：统一确认、任务恢复、分区标题（中文）**：删除用户/文件/向量、
  清理孤儿改用统一主题化确认弹窗，不再用原生 `confirm()`；刷新页面会重新接入
  正在运行的入库任务并继续轮询日志；浏览器标题跟随当前分区。
- **Console: upload folded into files, compact header**: the upload card was
  merged into the file list with an inline target-department selector, so
  picking a file and seeing its ingest status happens in one place; identity,
  theme, back-to-chat and logout moved into one account menu (language stays a
  first-level control), and the separate 配色 button became a 「配色…」 option in
  the theme selector.
  <br>**管理台：上传并入文件、顶栏收纳（中文）**：上传卡并入文件列表，卡内即可
  选择上传部门，选文件与看入库状态在同一处；身份、语言、主题、返回对话与退出收进
  账户菜单（语言仍在顶栏一级），原「配色」按钮改为主题下拉里的「配色…」选项。
- **Chat UI: safer sending, retry and accessibility**: Enter no longer submits
  while an IME is composing; switching chat/department/user aborts the running
  stream instead of leaving a detached response; retry reuses the question
  without adding a duplicate user bubble; the transcript only auto-scrolls when
  the reader is at the bottom, with a 「回到底部」 button; answers announce
  completion through a live region, and the source preview and mobile session
  drawer trap focus and restore it on close.
  <br>**对话页：输入安全、重试与无障碍（中文）**：输入法组词时回车不再误发送；
  切换会话/部门/用户会中止正在生成的回答，避免串台；重试复用原问题、不再重复
  用户气泡；阅读上翻时不再强制滚底，并提供「回到底部」；回答完成通过读屏区域
  播报，引用预览与移动会话抽屉会锁定焦点并在关闭后归还。
- **Chat UI: production identity, citations and session controls**: JWT mode
  now hides the editable user field and offers only the token's departments
  (read-only when there is one), with the backend/model badges folded into a
  「状态」 popover that also carries a dark/light/custom theme shared with the
  console. Sources are numbered, expand automatically for small sets and rank
  by order instead of showing a misleading score; the preview can copy its
  text. Answers gain a copy/regenerate toolbar, answers can be exported as
  Markdown, the composer grows with the text and warns near the 4,000-character
  cap, stopped answers keep their Markdown, and timing chips stay behind a
  「调试信息」 toggle. Session deletion uses the themed dialog, the list has
  search plus loading/error/retry states, and the demo prompts disappear once a
  conversation starts or when their department is not available.
  <br>**对话页：生产身份、引用与会话控制（中文）**：JWT 模式隐藏可编辑的用户名，
  只提供 token 内的部门（单部门只读）；后端/模型徽标收进「状态」弹层，并可在其中
  切换与管理台共享的深色/浅色/自定义主题。引用带编号、少量引用自动展开、按顺序
  标注排名而不再显示易误解的分数，预览支持复制全文。回答新增复制/重新生成工具条，
  可导出 Markdown；输入框随内容增高并在接近 4000 字时提示；停止生成保留 Markdown；
  耗时 chip 收进「调试信息」开关。删除会话改用统一确认弹窗，会话列表支持搜索及
  载入/失败/重试状态，对话开始或部门不可用时隐藏示例问题。

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
