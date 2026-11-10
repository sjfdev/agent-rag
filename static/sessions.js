/* Pure helpers for the server-side session UI.
 *
 * Loaded by static/index.html before the chat script. Keeping the request body
 * construction here makes it testable under Node (tests/test_sessions_js.py)
 * without a browser: a session id must win over local history, and the local
 * fallback must still start on a user turn.
 */
(function (root) {
  "use strict";

  function normalizeHistory(history, max) {
    if (!Array.isArray(history)) return [];
    const limit = Number(max);
    let kept = limit > 0 ? history.slice(-limit) : history.slice();
    while (kept.length && (!kept[0] || kept[0].role !== "user")) kept.shift();
    return kept
      .filter((item) => item && typeof item.content === "string")
      .map((item) => ({ role: item.role, content: item.content }));
  }

  function buildChatBody(opts) {
    const options = opts || {};
    const body = {
      user_id: options.userId || "",
      user_dept: options.dept || "",
      message: options.message || "",
    };
    if (options.sessionId) {
      body.session_id = options.sessionId;
    } else {
      const history = normalizeHistory(options.history, options.maxHistory);
      if (history.length) body.history = history;
    }
    return body;
  }

  function sessionQuery(userId) {
    return "user_id=" + encodeURIComponent(userId || "");
  }

  function formatDate(iso, lang) {
    if (!iso) return "";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "";
    const locale = String(lang || "").toLowerCase().startsWith("en") ? "en-US" : "zh-CN";
    return date.toLocaleDateString(locale, { month: "short", day: "numeric" });
  }

  const api = { buildChatBody, normalizeHistory, sessionQuery, formatDate };
  root.ChatSessions = api;
  if (typeof module === "object" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
