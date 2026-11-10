# Changelog

All notable behavior changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`src/version.py` holds the running version, `docker-compose.prod.yml` uses
`APP_TAG` for the image tag, and `scripts/check_release.py` verifies that the
three agree before you push a tag.

## [Unreleased]

### Added

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

### Changed

- `make run` binds the API to `127.0.0.1` by default; put a reverse proxy in
  front when the service must be reachable from other hosts.

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

[Unreleased]: https://github.com/sjfdev/agent-rag/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/sjfdev/agent-rag/releases/tag/v1.0.0
