/* Small dependency-free i18n layer for the chat and admin UIs.
 *
 * Loaded by static/index.html and static/admin.html via
 * <script src="/ui/i18n.js"></script>. Static markup carries data-i18n
 * attributes; generated markup can carry data-i18n + data-i18n-vars so a
 * language switch also retranslates restored chat history.
 *
 * Keep the zh and en dictionaries in sync: tests/test_i18n_js.py asserts the
 * key sets and placeholders match.
 */
(function (root) {
  "use strict";

  const LANG_KEY = "agent-rag-lang";

  const TRANSLATIONS = {
    zh: {
      "page.chatTitle": "企业知识库 Agent · 测试台",
      "page.adminTitle": "知识库管理 - agent-rag",

      "common.store": "向量库",
      "common.model": "模型",
      "common.backend": "后端",
      "common.maxUpload": "上传上限",
      "common.language": "语言",
      "common.refresh": "刷新",
      "common.delete": "删除",
      "common.unknown": "未知",

      "auth.title": "登录知识库",
      "auth.hint": "使用账号密码登录；部门与权限由账号决定，无需手工填写。",
      "auth.username": "用户名",
      "auth.password": "密码",
      "auth.tokenSummary": "使用 Bearer token 登录",
      "auth.tokenPlaceholder": "粘贴 token",
      "auth.login": "登录",
      "auth.logout": "退出登录",
      "auth.expired": "登录已失效，请重新登录。",
      "auth.badCredentials": "用户名或密码错误。",
      "auth.tooMany": "尝试次数过多，请稍后再试。",
      "auth.enterCredentials": "请输入用户名和密码，或粘贴 token。",
      "auth.signedInNew": "已登录，开始新的对话。",
      "auth.signedOut": "已退出登录。",
      "auth.unreachable": "无法连接服务：{error}",
      "auth.invalidSso": "登录凭据无效或该账号没有使用权限。",

      "auth.adminTitle": "登录知识库管理",
      "auth.adminHint": "用账号密码登录，或直接粘贴管理员 Bearer token／管理令牌（ADMIN_TOKEN）。凭据只保存在当前标签页的 sessionStorage 中。",
      "auth.adminTokenSummary": "使用 token / 管理令牌",
      "auth.adminTokenPlaceholder": "Bearer token 或管理令牌",
      "auth.adminJwtHint": "JWT 需带",
      "auth.adminSso": "使用企业账号登录",
      "auth.adminNoAccess": "该账号没有管理权限（token 需要 admin: true）。",
      "auth.adminConnectFailed": "无法连接管理接口：{error}",
      "auth.adminInvalidSso": "登录凭据无效或该账号没有管理权限。",

      "chat.greeting": "选择一个部门后提问。不同部门只能检索到各自授权范围内的文档。",
      "chat.user": "用户",
      "chat.dept": "部门",
      "chat.newChat": "新对话（新窗口）",
      "chat.newChatTitle": "在新标签页打开一个全新会话",
      "chat.clear": "清空当前",
      "chat.inputPlaceholder": "输入问题，Enter 发送 / Shift+Enter 换行",
      "chat.send": "发送",
      "chat.stop": "停止",
      "chat.adminLink": "入库管理",
      "chat.hint": "多轮对话会出现在左侧会话列表；时间单位为毫秒。",
      "chat.preset.codeReview": "代码评审要求",
      "chat.preset.annualLeave": "年假与过期",
      "chat.preset.budget": "2026 预算",
      "chat.preset.ticket": "报修/工单",
      "chat.preset.salary": "越权测试（薪资）",
      "chat.preset.demoVpn": "Demo VPN（多格式）",
      "chat.preset.demoMetric": "Demo 指标（Excel）",
      "chat.thinking": "思考中…",
      "chat.tooLong": "问题太长了（上限 4000 字符），请精简后再发送。",
      "chat.sources": "来源（{count}）",
      "chat.sourceDept": "部门：{dept}",
      "chat.sourceKind": "类型：{kind}",
      "chat.sourceScore": "相关度（相对排序）",
      "chat.page": " · 第 {page} 页",
      "chat.slide": " · 幻灯片 {slide}",
      "chat.sheet": " · 工作表 {sheet}",
      "chat.unknownSource": "未知",
      "chat.copy": "复制",
      "chat.copied": "已复制",
      "chat.copyFailed": "复制失败",
      "chat.retry": "重试",
      "chat.timing.total": "总计 {value}ms",
      "chat.timing.retrieval": "检索 {value}ms",
      "chat.timing.rerank": "重排 {value}ms",
      "chat.timing.llm": "LLM {value}ms",
      "chat.timing.overhead": "框架 {value}ms",
      "chat.llmCalls": "LLM {count} 次",
      "chat.retrievalCalls": "检索 {count} 次",
      "chat.cached": "缓存命中",
      "chat.degraded": "降级回答",
      "chat.degradedNote": "模型服务暂时不可用，以上为知识库原文摘录。",
      "chat.error.upstream": "模型服务暂时不可用（网络或 DNS 异常），请稍后重试。",
      "chat.error.maintenance": "系统正在重建索引，请稍后重试。",
      "chat.error.vectorStore": "知识库暂时不可用（向量库连接失败），请稍后重试。",
      "chat.error.internal": "内部错误，请稍后重试。",
      "chat.error.missingDepartment": "当前账号没有分配部门，无法检索。请联系管理员在「账号」里补上部门。",
      "chat.error.invalidDepartment": "部门名称不合法。",
      "chat.error.tooManyMessages": "请求内容过长或历史消息过多，请点击「清空当前」后重试。",
      "chat.error.rateLimited": "请求过于频繁，请稍后再试。",
      "chat.error.payloadTooLarge": "请求内容过大。",
      "chat.error.requestFailed": "请求失败：{detail}",
      "chat.empty": "（没有返回内容）",
      "chat.stopped": "已停止生成。",
      "chat.stoppedWithAnswer": "{answer}（已停止生成）",
      "chat.networkError": "网络错误：{error}",
      "chat.switchedDept": "已切换到 {dept} 部门，开始新对话。",
      "chat.switchedUser": "已切换用户 {user}，开始新对话。",
      "chat.cleared": "已清空当前对话。",

      "sessions.title": "会话",
      "sessions.new": "新对话",
      "sessions.empty": "暂无会话",
      "sessions.untitled": "未命名会话",
      "sessions.messages": "{count} 条消息",
      "sessions.delete": "删除会话",
      "sessions.deleteConfirm": "删除这个会话及全部消息？",
      "sessions.deleted": "已删除会话，开始新对话。",
      "sessions.started": "已开始新对话。",
      "sessions.missing": "会话不存在或已被删除，开始新对话。",
      "sessions.loadFailed": "无法加载会话：{error}",
      "sessions.deleteFailed": "删除会话失败，请稍后重试。",

      "admin.brand": "知识库管理",
      "admin.brandSub": "ingestion console",
      "admin.backToChat": "返回对话",
      "admin.deptTitle": "部门",
      "admin.newDeptPlaceholder": "新部门名称",
      "admin.createDept": "创建部门",
      "admin.loginFailedNoToken": "登录失败：服务端没有返回 token。",
      "admin.enterToken": "请输入 token 或管理令牌。",
      "admin.totalFiles": "文件总数",
      "admin.uploadTitle": "上传文件",
      "admin.dropText": "拖拽文件到这里，或",
      "admin.chooseFile": "选择文件",
      "admin.formats": "支持：{formats}",
      "admin.subdirPlaceholder": "子目录（可选，如 runbooks）",
      "admin.uploadButton": "上传到当前部门",
      "admin.filesTitle": "入库文件",
      "admin.ingestDept": "增量入库（当前部门）",
      "admin.ingestAll": "增量入库（全部）",
      "admin.table.file": "文件",
      "admin.table.type": "类型",
      "admin.table.size": "大小",
      "admin.table.status": "状态",
      "admin.table.chunks": "chunks",
      "admin.table.modified": "修改时间",
      "admin.accountsTitle": "账号",
      "admin.accountsHintPrefix": "仅",
      "admin.accountsHintMiddle": " 时可用。密码用 scrypt 哈希保存在",
      "admin.accountsHintSuffix": "，改密码或停用会立即使该账号的旧 token 失效。",
      "admin.newUserPlaceholder": "用户名",
      "admin.newPassPlaceholder": "密码（≥10 位）",
      "admin.newDeptsPlaceholder": "部门（必填），逗号分隔，如 Tech,HR",
      "admin.newGroupsPlaceholder": "组（可选）",
      "admin.adminLabel": "管理员",
      "admin.createUser": "创建账号",
      "admin.userTable.user": "用户",
      "admin.userTable.deptsGroups": "部门 / 组",
      "admin.userTable.admin": "管理员",
      "admin.userTable.status": "状态",
      "admin.rebuildTitle": "重建索引",
      "admin.rebuildHint": "删除当前索引/集合并重新入库全部部门。Embedding 会重新计算，期间检索结果可能不完整。",
      "admin.rebuildConfirm": "我确认要重建全部索引",
      "admin.rebuildButton": "执行重建",
      "admin.jobTitle": "任务",
      "admin.auditTitle": "操作审计",
      "admin.auditRecent": "最近 20 条",
      "admin.auditTable.time": "时间",
      "admin.auditTable.action": "操作",
      "admin.auditTable.actor": "操作者",
      "admin.auditTable.target": "对象",
      "admin.auditTable.result": "结果",
      "admin.status.indexed": "已入库",
      "admin.status.pending": "待入库",
      "admin.status.changed": "有变更",
      "admin.status.unsupported": "不支持",
      "admin.status.unknown": "未知",
      "admin.noDept": "（暂无部门）",
      "admin.noFiles": "暂无文件，先上传或创建部门。",
      "admin.all": "（全部）",
      "admin.deptInParens": "（{dept}）",
      "admin.countInParens": "（{count}）",
      "admin.maxUploadTotal": "（单次合计 {mb} MB）",
      "admin.ingestRunning": "已有入库任务在运行",
      "admin.deptReady": "部门 {name} 就绪",
      "admin.selectDeptFirst": "请先选择部门。",
      "admin.createDeptFirst": "请先创建并选择部门。",
      "admin.selectFiles": "请先选择文件。",
      "admin.pendingUpload": "待上传：{names}",
      "admin.uploading": "上传中…",
      "admin.uploaded": "已上传：{saved}。点击「增量入库」后生效。",
      "admin.uploadFailedDetail": " 失败：{errors}",
      "admin.overwritten": "（覆盖）",
      "admin.uploadFailed": "上传失败：{error}",
      "admin.jobStarting": "启动中…",
      "admin.jobFailed": "失败",
      "admin.jobPollFailed": "轮询失败：{error}",
      "admin.jobStatus.queued": "排队中",
      "admin.jobStatus.running": "运行中",
      "admin.jobStatus.done": "已完成",
      "admin.jobStatus.error": "失败",
      "admin.requestTimeout": "请求超时（{seconds} 秒无响应）",
      "admin.connectFailed": "无法连接管理接口：{error}",
      "admin.panelFailed": "{panel}加载失败：{error}",
      "admin.panel.overview": "概览",
      "admin.panel.files": "文件",
      "admin.panel.audit": "审计",
      "admin.panel.accounts": "账号",
      "admin.identity.user": "用户 {name}",
      "admin.identity.depts": "部门 {names}",
      "admin.identity.adminToken": "管理令牌",
      "admin.identity.admin": "管理员",
      "admin.userNoDepts": "（无部门）",
      "admin.disable": "停用",
      "admin.changePassword": "改密码",
      "admin.resetPasswordPrompt": "为 {user} 设置新密码（至少 10 位）。\n改密码后该账号已签发的 token 会立即失效。",
      "admin.passwordUpdated": "密码已更新",
      "admin.deleteUserConfirm": "删除账号 {user}？该账号将无法再登录。",
      "admin.userDeleted": "已删除 {user}",
      "admin.deleteFailed": "删除失败：{error}",
      "admin.updateFailed": "更新失败：{error}",
      "admin.permissionsUpdated": "权限已更新",
      "admin.accountDisabled": "账号已停用",
      "admin.accountEnabled": "账号已启用",
      "admin.enterUserPass": "请填写用户名和密码。",
      "admin.enterDept": "请至少填写一个部门（账号权限由部门决定）。",
      "admin.userCreated": "已创建账号 {user}",
      "admin.createFailed": "创建失败：{error}",
      "admin.deleteFileConfirm": "删除文件并从索引移除？\n{source}",
      "admin.fileDeleted": "已删除 {dept}/{source}（移除 {chunks} 个 chunk）",
      "admin.auditResult.success": "成功",
      "admin.auditResult.failure": "失败",
      "admin.auditAction.auth.login": "登录",
      "admin.auditAction.dept.create": "创建部门",
      "admin.auditAction.file.upload": "上传文件",
      "admin.auditAction.file.delete": "删除文件",
      "admin.auditAction.ingest.start": "开始入库",
      "admin.auditAction.user.create": "创建账号",
      "admin.auditAction.user.update": "更新账号",
      "admin.auditAction.user.delete": "删除账号",
      "admin.resetConfirm": "将删除现有索引并重新入库全部文件，确定继续？",
      "admin.confirmRequired": "请先勾选确认。",
      "admin.loadUsersFailed": "账号列表加载失败：{error}",

      "error.crossOrigin": "跨域管理请求被拒绝。",
      "error.invalidToken": "凭据无效或已过期。",
      "error.adminRequired": "需要管理员权限。",
      "error.deptNotFound": "部门不存在。",
      "error.fileNotFound": "文件不存在。",
      "error.localLoginDisabled": "本地账号登录未启用。",
      "error.pathOutsideData": "路径超出数据目录。",
      "error.invalidPath": "路径不合法。",
      "error.invalidSourcePath": "文件路径不合法。",
      "error.userNotFound": "用户不存在。",
      "error.adminLastDemote": "最后一个启用的管理员不能被降权或停用。",
      "error.adminLastDelete": "最后一个启用的管理员不能被删除。",
      "error.ingestRunning": "已有入库任务在运行。",
      "error.resetScope": "重建必须针对整个知识库。",
      "error.noFilesUploaded": "没有上传任何文件。",
      "error.vectorStoreUnavailable": "向量库不可用：{reason}",
      "error.tooManyFiles": "单次上传文件过多（上限 {limit} 个）。",
      "error.fileTooLarge": "{name} 超过 {mb}MB 上限。",
    },

    en: {
      "page.chatTitle": "Enterprise Knowledge Base Agent · Test Console",
      "page.adminTitle": "Knowledge Base Admin - agent-rag",

      "common.store": "Vector store",
      "common.model": "Model",
      "common.backend": "Backend",
      "common.maxUpload": "Upload limit",
      "common.language": "Language",
      "common.refresh": "Refresh",
      "common.delete": "Delete",
      "common.unknown": "Unknown",

      "auth.title": "Sign in to the knowledge base",
      "auth.hint": "Sign in with your account. Your departments and permissions come from the account, so no manual input is needed.",
      "auth.username": "Username",
      "auth.password": "Password",
      "auth.tokenSummary": "Sign in with a Bearer token",
      "auth.tokenPlaceholder": "Paste token",
      "auth.login": "Sign in",
      "auth.logout": "Sign out",
      "auth.expired": "Your session has expired. Please sign in again.",
      "auth.badCredentials": "Incorrect username or password.",
      "auth.tooMany": "Too many attempts. Please try again later.",
      "auth.enterCredentials": "Enter a username and password, or paste a token.",
      "auth.signedInNew": "Signed in. Starting a new conversation.",
      "auth.signedOut": "Signed out.",
      "auth.unreachable": "Cannot reach the service: {error}",
      "auth.invalidSso": "Invalid credentials or this account has no access.",

      "auth.adminTitle": "Sign in to knowledge base admin",
      "auth.adminHint": "Sign in with an account, or paste an admin Bearer token / ADMIN_TOKEN. Credentials are kept only in this tab's sessionStorage.",
      "auth.adminTokenSummary": "Use a token / admin token",
      "auth.adminTokenPlaceholder": "Bearer token or admin token",
      "auth.adminJwtHint": "A JWT needs",
      "auth.adminSso": "Sign in with a company account",
      "auth.adminNoAccess": "This account has no admin permission (the token needs admin: true).",
      "auth.adminConnectFailed": "Cannot reach the admin API: {error}",
      "auth.adminInvalidSso": "Invalid credentials or this account has no admin permission.",

      "chat.greeting": "Pick a department and ask a question. Each department can only retrieve documents within its own authorized scope.",
      "chat.user": "User",
      "chat.dept": "Department",
      "chat.newChat": "New chat (new window)",
      "chat.newChatTitle": "Open a fresh session in a new tab",
      "chat.clear": "Clear current",
      "chat.inputPlaceholder": "Ask a question. Enter to send / Shift+Enter for a new line",
      "chat.send": "Send",
      "chat.stop": "Stop",
      "chat.adminLink": "Ingestion console",
      "chat.hint": "Multi-turn chats appear in the chats list. Timing values are in milliseconds.",
      "chat.preset.codeReview": "Code review",
      "chat.preset.annualLeave": "Annual leave",
      "chat.preset.budget": "2026 budget",
      "chat.preset.ticket": "Repair / ticket",
      "chat.preset.salary": "Permission test (salary)",
      "chat.preset.demoVpn": "Demo VPN (multi-format)",
      "chat.preset.demoMetric": "Demo metrics (Excel)",
      "chat.thinking": "Thinking…",
      "chat.tooLong": "Your question is too long (4,000 characters max). Please shorten it.",
      "chat.sources": "Sources ({count})",
      "chat.sourceDept": "Department: {dept}",
      "chat.sourceKind": "Type: {kind}",
      "chat.sourceScore": "Relevance (relative rank)",
      "chat.page": " · p.{page}",
      "chat.slide": " · slide {slide}",
      "chat.sheet": " · sheet {sheet}",
      "chat.unknownSource": "unknown",
      "chat.copy": "Copy",
      "chat.copied": "Copied",
      "chat.copyFailed": "Copy failed",
      "chat.retry": "Retry",
      "chat.timing.total": "Total {value}ms",
      "chat.timing.retrieval": "Retrieval {value}ms",
      "chat.timing.rerank": "Rerank {value}ms",
      "chat.timing.llm": "LLM {value}ms",
      "chat.timing.overhead": "Framework {value}ms",
      "chat.llmCalls": "LLM calls: {count}",
      "chat.retrievalCalls": "Retrieval calls: {count}",
      "chat.cached": "Cache hit",
      "chat.degraded": "Degraded answer",
      "chat.degradedNote": "The model service is unavailable. The text above is extracted directly from the knowledge base.",
      "chat.error.upstream": "The model service is temporarily unavailable (network or DNS error). Please try again later.",
      "chat.error.maintenance": "The index is being rebuilt. Please try again later.",
      "chat.error.vectorStore": "The knowledge base is temporarily unavailable (vector store connection failed). Please try again later.",
      "chat.error.internal": "Internal error. Please try again later.",
      "chat.error.missingDepartment": "This account has no department, so retrieval is unavailable. Ask an administrator to assign one in the Accounts card.",
      "chat.error.invalidDepartment": "Invalid department name.",
      "chat.error.tooManyMessages": "The request or history is too long. Click “Clear current” and try again.",
      "chat.error.rateLimited": "Too many requests. Please try again shortly.",
      "chat.error.payloadTooLarge": "The request payload is too large.",
      "chat.error.requestFailed": "Request failed: {detail}",
      "chat.empty": "(No content returned)",
      "chat.stopped": "Generation stopped.",
      "chat.stoppedWithAnswer": "{answer} (generation stopped)",
      "chat.networkError": "Network error: {error}",
      "chat.switchedDept": "Switched to the {dept} department. Starting a new conversation.",
      "chat.switchedUser": "Switched to user {user}. Starting a new conversation.",
      "chat.cleared": "Current conversation cleared.",

      "sessions.title": "Chats",
      "sessions.new": "New chat",
      "sessions.empty": "No chats yet",
      "sessions.untitled": "Untitled chat",
      "sessions.messages": "{count} messages",
      "sessions.delete": "Delete chat",
      "sessions.deleteConfirm": "Delete this chat and all of its messages?",
      "sessions.deleted": "Chat deleted. Started a new chat.",
      "sessions.started": "Started a new chat.",
      "sessions.missing": "This chat no longer exists. Started a new chat.",
      "sessions.loadFailed": "Could not load the chat: {error}",
      "sessions.deleteFailed": "Could not delete the chat. Please try again.",

      "admin.brand": "Knowledge Base Admin",
      "admin.brandSub": "ingestion console",
      "admin.backToChat": "Back to chat",
      "admin.deptTitle": "Departments",
      "admin.newDeptPlaceholder": "New department name",
      "admin.createDept": "Create department",
      "admin.loginFailedNoToken": "Sign-in failed: the server did not return a token.",
      "admin.enterToken": "Enter a token or admin token.",
      "admin.totalFiles": "Total files",
      "admin.uploadTitle": "Upload files",
      "admin.dropText": "Drag files here, or",
      "admin.chooseFile": "choose files",
      "admin.formats": "Supported: {formats}",
      "admin.subdirPlaceholder": "Subdirectory (optional, e.g. runbooks)",
      "admin.uploadButton": "Upload to current department",
      "admin.filesTitle": "Ingested files",
      "admin.ingestDept": "Incremental ingest (current department)",
      "admin.ingestAll": "Incremental ingest (all)",
      "admin.table.file": "File",
      "admin.table.type": "Type",
      "admin.table.size": "Size",
      "admin.table.status": "Status",
      "admin.table.chunks": "Chunks",
      "admin.table.modified": "Modified",
      "admin.accountsTitle": "Accounts",
      "admin.accountsHintPrefix": "Available only when",
      "admin.accountsHintMiddle": ". Passwords are stored as scrypt hashes in",
      "admin.accountsHintSuffix": ". Changing the password or disabling the account immediately revokes its old tokens.",
      "admin.newUserPlaceholder": "Username",
      "admin.newPassPlaceholder": "Password (10+ characters)",
      "admin.newDeptsPlaceholder": "Departments (required), comma-separated, e.g. Tech,HR",
      "admin.newGroupsPlaceholder": "Groups (optional)",
      "admin.adminLabel": "Admin",
      "admin.createUser": "Create account",
      "admin.userTable.user": "User",
      "admin.userTable.deptsGroups": "Departments / groups",
      "admin.userTable.admin": "Admin",
      "admin.userTable.status": "Status",
      "admin.rebuildTitle": "Rebuild index",
      "admin.rebuildHint": "Delete the current index/collection and re-ingest every department. Embeddings are recomputed, and retrieval may be incomplete while this runs.",
      "admin.rebuildConfirm": "I understand this rebuilds the entire index",
      "admin.rebuildButton": "Run rebuild",
      "admin.jobTitle": "Job",
      "admin.auditTitle": "Audit log",
      "admin.auditRecent": "Latest 20",
      "admin.auditTable.time": "Time",
      "admin.auditTable.action": "Action",
      "admin.auditTable.actor": "Actor",
      "admin.auditTable.target": "Target",
      "admin.auditTable.result": "Result",
      "admin.status.indexed": "Indexed",
      "admin.status.pending": "Pending",
      "admin.status.changed": "Changed",
      "admin.status.unsupported": "Unsupported",
      "admin.status.unknown": "Unknown",
      "admin.noDept": "(No departments)",
      "admin.noFiles": "No files yet. Upload files or create a department first.",
      "admin.all": "(All)",
      "admin.deptInParens": "({dept})",
      "admin.countInParens": "({count})",
      "admin.maxUploadTotal": " ({mb} MB total per request)",
      "admin.ingestRunning": "An ingestion job is already running",
      "admin.deptReady": "Department {name} is ready",
      "admin.selectDeptFirst": "Select a department first.",
      "admin.createDeptFirst": "Create and select a department first.",
      "admin.selectFiles": "Choose files first.",
      "admin.pendingUpload": "Pending upload: {names}",
      "admin.uploading": "Uploading…",
      "admin.uploaded": "Uploaded: {saved}. Click “Incremental ingest” to apply.",
      "admin.uploadFailedDetail": " Failed: {errors}",
      "admin.overwritten": " (overwritten)",
      "admin.uploadFailed": "Upload failed: {error}",
      "admin.jobStarting": "Starting…",
      "admin.jobFailed": "Failed",
      "admin.jobPollFailed": "Polling failed: {error}",
      "admin.jobStatus.queued": "Queued",
      "admin.jobStatus.running": "Running",
      "admin.jobStatus.done": "Done",
      "admin.jobStatus.error": "Failed",
      "admin.requestTimeout": "Request timed out ({seconds}s with no response)",
      "admin.connectFailed": "Cannot reach the admin API: {error}",
      "admin.panelFailed": "Failed to load {panel}: {error}",
      "admin.panel.overview": "Overview",
      "admin.panel.files": "Files",
      "admin.panel.audit": "Audit",
      "admin.panel.accounts": "Accounts",
      "admin.identity.user": "User {name}",
      "admin.identity.depts": "Departments {names}",
      "admin.identity.adminToken": "Admin token",
      "admin.identity.admin": "Admin",
      "admin.userNoDepts": "(No departments)",
      "admin.disable": "Disabled",
      "admin.changePassword": "Change password",
      "admin.resetPasswordPrompt": "Set a new password for {user} (at least 10 characters).\nExisting tokens for this account stop working immediately.",
      "admin.passwordUpdated": "Password updated",
      "admin.deleteUserConfirm": "Delete account {user}? This account will no longer be able to sign in.",
      "admin.userDeleted": "Deleted {user}",
      "admin.deleteFailed": "Delete failed: {error}",
      "admin.updateFailed": "Update failed: {error}",
      "admin.permissionsUpdated": "Permissions updated",
      "admin.accountDisabled": "Account disabled",
      "admin.accountEnabled": "Account enabled",
      "admin.enterUserPass": "Enter a username and a password.",
      "admin.enterDept": "Enter at least one department (account permissions are department-based).",
      "admin.userCreated": "Created account {user}",
      "admin.createFailed": "Create failed: {error}",
      "admin.deleteFileConfirm": "Delete this file and remove it from the index?\n{source}",
      "admin.fileDeleted": "Deleted {dept}/{source} ({chunks} chunks removed)",
      "admin.auditResult.success": "Success",
      "admin.auditResult.failure": "Failed",
      "admin.auditAction.auth.login": "Sign in",
      "admin.auditAction.dept.create": "Create department",
      "admin.auditAction.file.upload": "Upload file",
      "admin.auditAction.file.delete": "Delete file",
      "admin.auditAction.ingest.start": "Start ingestion",
      "admin.auditAction.user.create": "Create account",
      "admin.auditAction.user.update": "Update account",
      "admin.auditAction.user.delete": "Delete account",
      "admin.resetConfirm": "This deletes the current index and re-ingests all files. Continue?",
      "admin.confirmRequired": "Check the confirmation box first.",
      "admin.loadUsersFailed": "Failed to load accounts: {error}",

      "error.crossOrigin": "Cross-origin admin requests are rejected.",
      "error.invalidToken": "The credential is invalid or expired.",
      "error.adminRequired": "Admin permission is required.",
      "error.deptNotFound": "Department not found.",
      "error.fileNotFound": "File not found.",
      "error.localLoginDisabled": "Local account login is not enabled.",
      "error.pathOutsideData": "Path is outside the data directory.",
      "error.invalidPath": "Invalid path.",
      "error.invalidSourcePath": "Invalid file path.",
      "error.userNotFound": "User not found.",
      "error.adminLastDemote": "The last enabled administrator cannot be demoted or disabled.",
      "error.adminLastDelete": "The last enabled administrator cannot be deleted.",
      "error.ingestRunning": "An ingestion job is already running.",
      "error.resetScope": "A rebuild must target the whole knowledge base.",
      "error.noFilesUploaded": "No files were uploaded.",
      "error.vectorStoreUnavailable": "Vector store unavailable: {reason}",
      "error.tooManyFiles": "Too many files per upload (limit: {limit}).",
      "error.fileTooLarge": "{name} exceeds the {mb}MB limit.",
    },
  };

  let current = detectLanguage();

  function normalizeLanguage(value) {
    const lang = String(value || "").toLowerCase();
    if (lang.startsWith("zh")) return "zh";
    if (lang.startsWith("en")) return "en";
    return "";
  }

  function detectLanguage() {
    if (root.location && root.location.search) {
      const params = new URLSearchParams(root.location.search);
      const fromQuery = normalizeLanguage(params.get("lang"));
      if (fromQuery) return fromQuery;
    }
    try {
      const stored = normalizeLanguage(root.localStorage.getItem(LANG_KEY));
      if (stored) return stored;
    } catch (error) {
      /* private mode: fall through */
    }
    return normalizeLanguage(root.navigator && root.navigator.language) || "zh";
  }

  function t(key, vars) {
    const table = TRANSLATIONS[current] || TRANSLATIONS.zh;
    let value = table[key];
    if (value === undefined) value = TRANSLATIONS.zh[key];
    if (value === undefined) value = key;
    if (!vars) return value;
    return String(value).replace(/\{(\w+)\}/g, (match, name) =>
      vars[name] === undefined ? match : String(vars[name])
    );
  }

  function readVars(el) {
    const raw = el.getAttribute("data-i18n-vars");
    if (!raw) return null;
    try {
      return JSON.parse(raw);
    } catch (error) {
      return null;
    }
  }

  function applyElement(el) {
    const key = el.getAttribute("data-i18n");
    if (key) el.textContent = t(key, readVars(el));
    const placeholder = el.getAttribute("data-i18n-placeholder");
    if (placeholder) el.setAttribute("placeholder", t(placeholder));
    const title = el.getAttribute("data-i18n-title");
    if (title) el.setAttribute("title", t(title));
    const aria = el.getAttribute("data-i18n-aria-label");
    if (aria) el.setAttribute("aria-label", t(aria));
  }

  function translateDom(scope) {
    const dom = scope || (typeof document !== "undefined" ? document : null);
    if (!dom || !dom.querySelectorAll) return;
    dom
      .querySelectorAll(
        "[data-i18n], [data-i18n-placeholder], [data-i18n-title], [data-i18n-aria-label]"
      )
      .forEach(applyElement);
  }

  function applyLanguage(lang) {
    current = normalizeLanguage(lang) || "zh";
    if (typeof document !== "undefined") {
      document.documentElement.lang = current === "zh" ? "zh-CN" : "en";
      translateDom(document);
      const titleEl = document.querySelector("title[data-i18n]");
      if (titleEl) titleEl.textContent = t(titleEl.getAttribute("data-i18n"));
    }
    return current;
  }

  function setLanguage(lang) {
    applyLanguage(lang);
    try {
      root.localStorage.setItem(LANG_KEY, current);
    } catch (error) {
      /* private mode: keep the in-memory choice */
    }
    if (typeof document !== "undefined" && typeof CustomEvent !== "undefined") {
      document.dispatchEvent(new CustomEvent("i18n:change", { detail: { lang: current } }));
    }
    return current;
  }

  function boot() {
    if (typeof document === "undefined") return;
    current = detectLanguage();
    const select = document.getElementById("lang");
    if (select) {
      select.value = current;
      select.addEventListener("change", () => setLanguage(select.value));
    }
    applyLanguage(current);
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", boot, { once: true });
    } else {
      boot();
    }
  }

  const api = {
    LANG_KEY,
    TRANSLATIONS,
    detectLanguage,
    currentLanguage: () => current,
    setLanguage,
    t,
    translateDom,
  };

  root.i18n = api;
  if (typeof module === "object" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
