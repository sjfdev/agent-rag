# Changelog

All notable behavior changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`src/version.py` holds the running version, `docker-compose.prod.yml` uses
`APP_TAG` for the image tag, and `scripts/check_release.py` verifies that the
three agree before you push a tag.

## [Unreleased]

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
