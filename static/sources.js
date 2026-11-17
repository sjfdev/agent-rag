/* Pure helpers for the citation preview.
 *
 * Loaded by static/index.html before the chat script. Keeping the query-string
 * construction here makes it testable under Node (tests/test_sources_js.py)
 * without a browser: only the fields a citation actually carries are sent, and
 * values are always URL-encoded.
 */
(function (root) {
  "use strict";

  function add(parts, key, value) {
    if (value === undefined || value === null || value === "") return;
    parts.push(encodeURIComponent(key) + "=" + encodeURIComponent(value));
  }

  function previewQuery(opts) {
    const options = opts || {};
    const parts = [];
    add(parts, "source", options.source);
    add(parts, "user_id", options.userId);
    add(parts, "user_dept", options.dept);
    (options.depts || []).forEach((dept) => add(parts, "user_depts", dept));
    (options.groups || []).forEach((group) => add(parts, "user_groups", group));
    add(parts, "page", options.page);
    add(parts, "slide", options.slide);
    add(parts, "sheet", options.sheet);
    return parts.join("&");
  }

  function previewPath(opts) {
    const query = previewQuery(opts);
    return query ? "/sources/content?" + query : "/sources/content";
  }

  function filePath(opts) {
    const options = opts || {};
    const query = previewQuery(options);
    const download = options.download ? "download=1" : "";
    const joined = [query, download].filter(Boolean).join("&");
    return joined ? "/sources/file?" + joined : "/sources/file";
  }

  const api = { previewQuery, previewPath, filePath };
  root.ChatSources = api;
  if (typeof module === "object" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
