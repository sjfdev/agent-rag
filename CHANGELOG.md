# Changelog

All notable behavior changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`src/version.py` holds the running version, `docker-compose.prod.yml` uses
`APP_TAG` for the image tag, and `scripts/check_release.py` verifies that the
three agree before you push a tag.

## [Unreleased]

### Added

- **Chat sessions, citations and source files**: the chat UI now supports
  renaming sessions (`PATCH /sessions/{id}`) and server-side content search
  (`GET /sessions?q=`), persists thumbs up/down feedback per assistant message
  (`POST/GET /sessions/{id}/feedback`), turns `[n]` in answers into clickable
  citation anchors using the new `citations` numbering map, shares a session via
  `?session=<id>`, and opens/downloads the original upload through
  `GET /sources/file` after the same department + ACL check. The 「调试信息」
  toggle now expands the rewritten query plus recall/rerank candidates, the
  streaming answer renders Markdown incrementally, Esc stops generation, and
  account switching plus a forgot-password hint live in both UIs.
  <br>**对话页会话/引用/原文增强（中文）**：会话支持重命名（`PATCH /sessions/{id}`）
  与服务端正文搜索（`GET /sessions?q=`）；赞/踩按助手消息落库
  （`POST/GET /sessions/{id}/feedback`）；响应新增 `citations` 编号映射，正文
  `[n]` 可点击跳转引用；`?session=<id>` 可分享会话；`GET /sources/file` 在部门 +
  ACL 校验后打开/下载原始文件。「调试信息」可展开改写 query 与召回/重排候选，流式
  回答按节流增量渲染 Markdown，生成中 Esc 可停止，两个页面都提供切换账号与忘记
  密码提示。
- **Offline licensing**: private deployments can require a signed license
  without any network call. `src/license.py` verifies an Ed25519-signed JSON
  (`config/license.json`, public key compiled in, private key stays offline)
  and reports `valid`/`grace`/`expired`/`missing`/`invalid` through `/info` and
  `GET /admin/api/license`. A missing or forged license refuses startup (fail
  closed, like the document ACL); an expired one keeps chat, citations and
  sessions working and only blocks admin writes (uploads, ingestion, model
  changes) after `LICENSE_GRACE_DAYS`. Limits (`max_depts`/`max_docs`), edition
  and feature flags are carried by the license and surfaced by the console and
  `scripts/doctor.py`. The admin 许可 card flips its badge to "expiring soon"
  within `LICENSE_WARN_DAYS` and spells out the renewal path (paste a signed
  file, effective immediately) and the grace-period window.
  `scripts/issue_license.py` creates the signing key and issues/verifies
  licenses; `make license-init` / `make license-verify`.
  <br>**离线许可（中文）**：私有化部署可要求签名许可，无需联网。`src/license.py`
  校验 Ed25519 签名 JSON（`config/license.json`，公钥内置，私钥离线保存），通过
  `/info` 与 `GET /admin/api/license` 暴露 `valid`/`grace`/`expired`/`missing`/
  `invalid`。许可缺失或签名不符时拒绝启动（与文档 ACL 一样 fail closed）；过期后
  对话、引用与会话照常可用，仅在 `LICENSE_GRACE_DAYS` 宽限期结束后阻止管理端写入
  （上传、入库、改模型）。部门/文档上限、版本与功能开关随许可下发，控制台与
  `scripts/doctor.py` 会展示。管理台「许可」卡片在 `LICENSE_WARN_DAYS` 内把徽章
  切到「即将到期」，并写明续期方式（粘贴签名文件即刻生效）与宽限期剩余天数。
  `scripts/issue_license.py` 负责建密钥与签发/校验，入口 `make license-init` /
  `make license-verify`。
- **License console**: `scripts/license_console.py` (`make license-console`) is a
  vendor-side local web UI for issuing and verifying licenses: it reads the
  Ed25519 private key, shows the matching public key, signs the form, writes
  `licenses/<licensee>-<expiry>-<id>.json` (0600), re-verifies every issued file
  and lets you paste an existing license to check it. The server binds to
  loopback and every API call carries a per-process token injected into the
  page, so the private key never reaches the browser and a random website
  cannot drive it. `make package LICENSE=<file>` / `--license` embeds a
  customer-specific license as `config/license.json` (0600, listed in
  `PACKAGE.json`), and the admin 许可 card installs renewals online.
  <br>**许可控制台（中文）**：`scripts/license_console.py`（`make license-console`）
  是厂商侧本地网页工具，读取 Ed25519 私钥、显示对应公钥、签发并落盘
  `licenses/<授权方>-<到期日>-<编号>.json`（0600），逐个复验已签发文件，也可粘贴
  已有许可验签。服务仅绑定回环地址，页面注入每进程随机令牌保护所有 API，私钥
  永不进入浏览器，外部网站无法驱动它。`make package LICENSE=<文件>` 可把客户
  专属许可打进交付包的 `config/license.json`（0600，计入 `PACKAGE.json`）；客户
  续期在管理台「许可」卡片在线完成。
- **Usage/value report**: `scripts/usage_report.py` (`make usage-report`) reads
  the session SQLite store, the admin audit log and the installed license
  offline and renders `text`/`markdown`/`csv`/`json`: sessions, active users,
  questions and answers, citation coverage, degraded answers, satisfaction,
  multi-turn sessions, department breakdown and admin action counts. Optional
  `--minutes-per-question` / `--hourly-rate` add a time/value estimate. The
  report contains aggregates only — no transcripts or user ids — so it can be
  shared with a customer's finance/procurement team or used in a renewal QBR.
  <br>**用量与价值月报（中文）**：`scripts/usage_report.py`（`make usage-report`）
  离线读取会话 SQLite、审计日志与当前许可，输出 `text`/`markdown`/`csv`/`json`：
  会话、活跃用户、提问/回答、引用覆盖、降级回答、满意度、多轮会话、部门分布与
  管理操作次数；可选 `--minutes-per-question` / `--hourly-rate` 生成节省工时与
  金额估算。报告只含聚合计数，不含对话原文或用户标识，可直接给客户财务/采购，
  也适合作为续费 QBR 的证据。
- **Trial licenses and demo stack**: `scripts/issue_license.py --trial` issues a
  30-day evaluation license (1 department, 5,000 documents, full feature flags),
  capped at 90 days by the validator; `edition=trial` is exposed as
  `/info.license.trial`, warns in the last 7 days and shows a trial note in the
  console. The license console gained a one-click "30-day trial" preset.
  `docker-compose.demo.yml` + `.env.demo.example` + `make demo-up/demo-seed/
  demo-down` run a license-free FAISS demo that mounts only `data/Demo`, disables
  server-side sessions, keeps the admin API loopback-only and rate-limits
  visitors.
  <br>**试用许可与演示站（中文）**：`scripts/issue_license.py --trial` 签发
  30 天试用许可（1 部门 / 5000 文档 / 完整功能开关），校验器硬性限制最长 90 天；
  `edition=trial` 通过 `/info.license.trial` 暴露，最后 7 天进入提醒窗口，管理台
  显示试用提示。许可控制台增加「填入 30 天试用」按钮。新增
  `docker-compose.demo.yml`、`.env.demo.example` 与 `make demo-up/demo-seed/
  demo-down`：无许可、FAISS、只挂载 `data/Demo`、关闭服务端会话、管理端仅回环，
  并默认按 IP 限流。
- **Visual chunking configuration**: the admin console's 分片 card edits the
  token window, overlap, Chinese punctuation preference and title prefix;
  values persist to `config/chunk_settings.json` (0600, gitignored) and apply
  to the next ingestion. Splitting is token-aware (embedding tokenizer, with a
  deterministic fallback), reserves the title-prefix tokens inside the window,
  and prefers Chinese sentence boundaries. The chunking/parser signature is
  folded into `doc_hash`, so changing a parameter re-chunks already indexed
  documents on the next incremental ingestion instead of skipping them; the
  card offers save + incremental re-ingest.
  <br>**分片可视化配置（中文）**：管理台「分片」卡片可配置 token 窗口、重叠、
  中文标点优先与标题前缀，保存到 `config/chunk_settings.json`（0600、gitignore），
  下一次入库生效。切分按 embedding tokenizer 计数（不可用时退回字符启发式），
  标题前缀会在窗口内预留 token，并优先在中文标点处切分。分片/解析签名并入
  `doc_hash`，改参数后增量入库会自动重切已入库文档，卡片提供「保存并重新入库」。
- **Delivery package**: `scripts/package_release.py` (or `make package`) builds
  `dist/agent-rag-<version>-delivery.tar.gz` with the production compose files,
  Dockerfile, source/static/scripts, docs and `.env.example`, plus a generated
  `INSTALL.md` and `PACKAGE.json` (version, commit, per-file SHA256) and a
  `.sha256` sidecar. Runtime state (`data/`, `logs/`, `backups/`, `volumes/`),
  tests, `.env`, rendered Alertmanager secrets and the license private key are
  never included. `--with-image` adds a `docker save` of the app image.
  <br>**交付打包（中文）**：`scripts/package_release.py`（或 `make package`）生成
  `dist/agent-rag-<版本>-delivery.tar.gz`，包含生产 compose、Dockerfile、源码/
  静态资源/脚本、文档与 `.env.example`，并附自动生成的 `INSTALL.md` 与
  `PACKAGE.json`（版本、提交、逐文件 SHA256）及 `.sha256` 边车。运行态目录
  （`data/`、`logs/`、`backups/`、`volumes/`）、测试、`.env`、渲染后的
  Alertmanager 密钥与许可私钥一律不打入；`--with-image` 额外执行 `docker save`。
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
- **Chat UI follow-ups: language-aware presets, paging and richer export**:
  preset chips now send the question in the current UI language, session-delete
  failures surface in the sidebar instead of the transcript, 401/429/413/5xx
  restore the draft (and transient failures offer retry), exports include
  timestamps and citations, long transcripts render the newest 60 messages with
  a 「显示更早」 break, and `GET /sessions` accepts `limit`/`offset` so the
  sidebar has a 「加载更多」 button.
  <br>**对话页收尾（中文）**：预设问题按当前界面语言发送；删除会话失败改为侧栏
  状态而不是污染对话；401/429/413/5xx 恢复草稿（瞬时错误提供重试）；Markdown 导出
  带时间戳与引用；长会话只先渲染最近 60 条并提供「显示更早」；`GET /sessions` 支持
  `limit`/`offset`，侧栏新增「加载更多」。

- **Ingestion monitor**: the admin console now has a 运行监视 card and a
  header shortcut. It polls `GET /admin/api/jobs/{id}` and shows status,
  processed/total documents and percent, chunks written, elapsed time,
  throughput, ETA (after 40% progress), the current file and the live log. The
  API derives `progress` from the persisted job log, so it also works for
  jobs that were already running before the panel existed; completed jobs keep
  their summary.
  <br>**入库运行监视（中文）**：管理台新增「运行监视」卡片和顶栏入口，轮询
  `GET /admin/api/jobs/{id}`，展示状态、已处理/总文档与百分比、已写 chunk、已用时、
  速度、预计剩余（进度 40% 后）、当前文件与实时日志。后端从持久化任务日志推导
  `progress`，因此面板上线前已启动的任务也能显示；完成后保留汇总。

- **Ingestion throughput diagnostics**: each processed file now records
  parse/chunk/embed/write timings in the job log and `stats.timings_ms`, and
  the 运行监视 panel shows the phase breakdown. `EMBEDDING_BATCH_SIZE` (default
  32, clamped to 512) controls local sentence-transformers ingestion batching,
  and token counts are cached so the recursive splitter does not re-encode the
  same candidates. These changes do not alter vectors or chunk boundaries.
  <br>**入库吞吐诊断（中文）**：每个文件的解析/切分/嵌入/写入耗时会写入任务日志与
  `stats.timings_ms`，并在「运行监视」面板显示阶段耗时；`EMBEDDING_BATCH_SIZE`
  （默认 32，最大 512）控制本地 sentence-transformers 入库 batch；token 计数增加
  缓存，避免递归切分重复 encode。这些改动不改变向量与分片边界。

- **Fast PDF parsing via pdfium**: `src/loaders.py` now extracts PDF text with
  `pypdfium2` (C++/pdfium) when available and falls back to pypdf otherwise;
  `PDF_PARSER=auto|pdfium|pypdf` pins the choice. On this repository's PDFs the
  same books went from 11–15s to 0.3–1.0s (10–23x) with identical keyword
  coverage, which moves ingestion from parse-bound to embedding-bound. The
  resolved parser is part of the chunk signature, so installing pypdfium2 (or
  switching `PDF_PARSER`) re-chunks indexed documents on the next ingestion
  instead of silently keeping pypdf-era chunks. The OCR path renders pages with
  pdfium when present, and the Docker image now ships the tesseract engine with
  `chi_sim` data, so `OCR_ENABLED=true` works for scanned PDFs.
  <br>**pdfium 快速 PDF 解析（中文）**：`src/loaders.py` 优先用 `pypdfium2`
  （C++/pdfium）抽取 PDF 文本，未安装时回退 pypdf；`PDF_PARSER=auto|pdfium|pypdf`
  可强制指定。实测同一批 PDF 从 11–15s 降到 0.3–1.0s（10–23x），关键词覆盖一致，
  入库瓶颈从解析转为 embedding。解析器名称并入分片签名，安装 pypdfium2 或切换
  `PDF_PARSER` 后下次入库会自动重切，不会静默保留旧分片。OCR 在可用时也用 pdfium
  渲染页面；镜像内新增 tesseract 引擎与 `chi_sim` 语言包，`OCR_ENABLED=true`
  即可处理扫描件。

- **Prefetch ingestion pipeline**: `INGEST_PREFETCH=true` parses and chunks the
  next changed document on a worker thread while the main thread embeds and
  writes the current one, so PDF text extraction and embedding overlap instead
  of running one after the other. PDFs are roughly half parse / half embed
  (e.g. `ionic.pdf`: 20.2s parse vs 23.7s embed), so ingestion wall time drops
  by roughly 20–25% (measured 110.9s → 89.4s on three large PDFs). Embedding
  threads are capped to `cpu_count - 2` while the worker parses, otherwise both
  phases fight for every core and the overlap becomes a net loss.
  `INGEST_PREFETCH_DEPTH` (1–8, default 1) bounds how many
  prepared documents are held in memory. All vector-store access stays on the
  main thread: skip detection, stats/log lines, crash-safe replacement and the
  shadow rebuild are unchanged, and turning the flag off restores the exact
  previous path. CLI overrides: `--prefetch` / `--no-prefetch` /
  `--prefetch-depth N`.
  <br>**入库预取流水线（中文）**：`INGEST_PREFETCH=true` 时后台线程解析/切分
  下一份变更文档，主线程同时 embedding 并写入，让 PDF 解析与嵌入重叠执行。PDF
  通常解析与嵌入各占一半（如 `ionic.pdf` 解析 20.2s、嵌入 23.7s），因此入库总
  耗时约降 20–25%（三份大 PDF 实测 110.9s → 89.4s）；同时把 embedding 线程数
  限制为 `cpu_count - 2`，给解析线程留出 CPU，否则两阶段互相抢核反而更慢。
  `INGEST_PREFETCH_DEPTH`（1–8，默认 1）限制内存中缓存几份已
  解析文档。所有向量库访问仍在主线程，增量跳过、统计/日志、崩溃安全替换与影子
  重建行为不变；关闭开关即回到原路径。CLI 可用 `--prefetch` / `--no-prefetch` /
  `--prefetch-depth N` 覆盖。

### Changed

- **No per-document Milvus flush during ingestion**: replacing a document used
  to `flush()` the collection after every delete (~9s on a 10k-chunk
  collection), which dominated ingestion once embedding got fast. Deletes are
  already persisted through Milvus' WAL and visible to queries immediately, so
  the flush was removed; a replaced 200-chunk document now writes in <1s
  instead of ~9s. The batched prune/admin-delete path still flushes once per
  batch.
  <br>**入库不再逐文档 flush（中文）**：替换文档时原先每次删除后都 `flush()`
  整个集合（1 万 chunk 时约 9 秒），在 embedding 提速后成为主要瓶颈。删除已通过
  Milvus WAL 持久化且查询立即可见，因此去掉该 flush；替换一份 200 chunk 的文档
  写入从约 9 秒降到 1 秒内。批量清理/管理台删除仍按批次 flush 一次。

- **Faster image rebuilds**: the Dockerfile now bakes the embedding model into
  the image *before* copying `src/`, `static/`, `scripts/` and `eval/`. The
  model layer only depends on the installed packages and `EMBEDDING_MODEL` /
  `HF_ENDPOINT`, so a code-only rebuild downloads the model once and reuses the
  cached layer afterwards (previously every rebuild re-downloaded it and took
  minutes even for a one-line change).
  <br>**镜像重构建提速（中文）**：Dockerfile 把嵌入模型预热移到复制 `src/` 等
  源码之前。模型层只依赖已安装的包与 `EMBEDDING_MODEL`/`HF_ENDPOINT`，因此只改
  代码时模型层可复用缓存，不再每次重新下载（此前即使改一行代码也要等数分钟）。

### Fixed

- **Admin account management and account switching**: the 账号 card now edits
  an existing account's departments, groups, admin flag and enabled status in a
  dialog (previously only creation and password reset were exposed), password
  changes use a themed dialog with a confirmation field and revoke old tokens,
  and the header's 「切换账号」 / 「退出」 no longer leave the console visible
  with a cleared credential (`stopPolling` was called without being defined, so
  the handler threw before reaching `showGate`).
  <br>**管理台账号管理与切换账号（中文）**：账号卡片可在弹窗中编辑既有账号的部门、
  组、管理员与启停状态（此前只能新建和改密码）；改密码改为带二次确认的主题化弹窗，
  改后旧 token 立即失效；顶栏「切换账号」/「退出」不再出现“凭据已清空但控制台仍在”
  的问题（`stopPolling` 未定义，回调在显示登录页前抛错）。
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
