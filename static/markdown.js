/* Safe Markdown subset for the chat UI.
 *
 * Loaded by static/index.html via <script src="/ui/markdown.js">. Kept in a
 * standalone file so tests/test_markdown_js.py can exercise it with Node:
 * the escaping rules are security-relevant and deserve real assertions.
 *
 * Security model: model output is escaped BEFORE any tag is emitted, and the
 * only tags in the result are the ones written here. Raw HTML from the model
 * is therefore displayed as text, never interpreted. Links are limited to
 * http(s)/mailto.
 */
(function (root) {
  "use strict";

// Renders a deliberate subset of Markdown: bold/italic, inline and fenced
// code, links (http/https/mailto only), ordered/unordered lists, tables,
// blockquotes and horizontal rules.
function esc(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

const MD_LINK_SCHEMES = /^(https?:|mailto:)/i;
const MD_FENCE = /^\s*```\s*([\w+-]*)\s*$/;
const MD_HEADING = /^\s*(#{1,6})\s+(.*)$/;
const MD_HR = /^\s*([-*_])(?:\s*\1){2,}\s*$/;
const MD_QUOTE = /^\s*>\s?(.*)$/;
const MD_UL = /^(\s*)[-*+]\s+(.*)$/;
const MD_OL = /^(\s*)\d+[.)]\s+(.*)$/;
const MD_TABLE_SEP = /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/;

function mdInline(text) {
  const slots = [];
  const slot = (html) => {
    slots.push(html);
    return "\u0000" + (slots.length - 1) + "\u0000";
  };

  // 1. Inline code and links are pulled out of the RAW text first, so markup
  //    characters inside them can never be re-interpreted by the emphasis
  //    pass. Everything else is escaped before any tag is emitted.
  let s = String(text).replace(/`([^`]+)`/g, (_, code) =>
    slot("<code>" + esc(code) + "</code>")
  );
  s = s.replace(/\[([^\]]*)\]\(([^)\s]*)\)/g, (whole, label, url) => {
    // Only http(s) and mailto links stay clickable. Anything else (javascript:,
    // data:, vbscript:, relative) is kept as inert text, label included, so
    // the answer still reads correctly.
    if (!MD_LINK_SCHEMES.test(url)) return whole;
    return slot(
      '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer nofollow">' +
        esc(label) + "</a>"
    );
  });

  // 2. Escape whatever is left: this is the only path that reaches the DOM
  //    as text, and it never produces tags.
  s = esc(s);

  // 3. Emphasis on the escaped text (the characters we emit are ours).
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^\w*])\*([^*\n]+)\*(?=[^\w*]|$)/g, "$1<em>$2</em>");
  s = s.replace(/(^|[^\w_])_([^_\n]+)_(?=[^\w_]|$)/g, "$1<em>$2</em>");

  // 4. Restore the pre-rendered safe fragments.
  return s.replace(/\u0000(\d+)\u0000/g, (_, index) => slots[Number(index)]);
}

function mdTableRow(line) {
  let body = line.trim();
  if (body.startsWith("|")) body = body.slice(1);
  if (body.endsWith("|")) body = body.slice(0, -1);
  return body.split("|").map((cell) => cell.trim());
}

function renderMarkdown(source) {
  const lines = String(source == null ? "" : source).replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Fenced code: nothing inside is parsed.
    const fence = line.match(MD_FENCE);
    if (fence) {
      const body = [];
      i += 1;
      while (i < lines.length && !MD_FENCE.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1; // closing fence (or EOF)
      const language = fence[1] ? ' class="language-' + esc(fence[1]) + '"' : "";
      out.push("<pre><code" + language + ">" + esc(body.join("\n")) + "</code></pre>");
      continue;
    }

    if (!line.trim()) {
      i += 1;
      continue;
    }

    if (MD_HR.test(line)) {
      out.push("<hr>");
      i += 1;
      continue;
    }

    const heading = line.match(MD_HEADING);
    if (heading) {
      // Demote to h4-h6 so the answer never competes with the page headings.
      const level = Math.min(6, 3 + heading[1].length);
      out.push("<h" + level + ">" + mdInline(heading[2].trim()) + "</h" + level + ">");
      i += 1;
      continue;
    }

    if (MD_QUOTE.test(line) && line.trim().startsWith(">")) {
      const body = [];
      while (i < lines.length) {
        const quote = lines[i].match(MD_QUOTE);
        if (!quote) break;
        body.push(mdInline(quote[1]));
        i += 1;
      }
      out.push("<blockquote>" + body.join("<br>") + "</blockquote>");
      continue;
    }

    // Table: a header row followed by a separator row.
    if (line.includes("|") && i + 1 < lines.length && MD_TABLE_SEP.test(lines[i + 1])) {
      const header = mdTableRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        rows.push(mdTableRow(lines[i]));
        i += 1;
      }
      const head = header.map((cell) => "<th>" + mdInline(cell) + "</th>").join("");
      const body = rows.map((cells) => {
        const tds = header.map((_, index) => "<td>" + mdInline(cells[index] || "") + "</td>").join("");
        return "<tr>" + tds + "</tr>";
      }).join("");
      out.push("<table><thead><tr>" + head + "</tr></thead><tbody>" + body + "</tbody></table>");
      continue;
    }

    if (MD_UL.test(line) || MD_OL.test(line)) {
      const ordered = !MD_UL.test(line);
      const pattern = ordered ? MD_OL : MD_UL;
      const items = [];
      while (i < lines.length) {
        const item = lines[i].match(pattern);
        if (!item) break;
        items.push({ indent: item[1].length, html: mdInline(item[2]) });
        i += 1;
      }
      // One nesting level: deeper indents fold into the previous item.
      out.push(ordered ? "<ol>" : "<ul>");
      let base = items[0].indent;
      items.forEach((item, index) => {
        if (index > 0 && item.indent > base) {
          out.push("<li class=\"md-sub\">" + item.html + "</li>");
        } else {
          out.push("<li>" + item.html + "</li>");
        }
      });
      out.push(ordered ? "</ol>" : "</ul>");
      continue;
    }

    // Paragraph: consecutive plain lines become one block with soft breaks.
    const paragraph = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !MD_FENCE.test(lines[i]) &&
      !MD_HEADING.test(lines[i]) &&
      !MD_HR.test(lines[i]) &&
      !lines[i].trim().startsWith(">") &&
      !MD_UL.test(lines[i]) &&
      !MD_OL.test(lines[i]) &&
      !(lines[i].includes("|") && i + 1 < lines.length && MD_TABLE_SEP.test(lines[i + 1]))
    ) {
      paragraph.push(mdInline(lines[i]));
      i += 1;
    }
    out.push("<p>" + paragraph.join("<br>") + "</p>");
  }

  return out.join("");
}

// Fill a bubble with rendered markdown. While a reply streams we keep using
// plain text (cheap, no layout thrash) and call this once it finishes.
function setMarkdown(bubble, text) {
  bubble.classList.add("md");
  bubble.innerHTML = renderMarkdown(text);
}

  root.renderMarkdown = renderMarkdown;
  root.setMarkdown = setMarkdown;
  root.escapeHtml = esc;
  if (typeof module === "object" && module.exports) {
    module.exports = { renderMarkdown: renderMarkdown, escapeHtml: esc };
  }
})(typeof globalThis !== "undefined" ? globalThis : window);
