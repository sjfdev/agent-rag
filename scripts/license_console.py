"""Local web console for issuing and verifying offline licenses.

    python scripts/license_console.py          # opens http://127.0.0.1:8765
    python scripts/license_console.py --no-browser --port 9000

This is a *vendor* tool. It runs on your own machine, reads the Ed25519 private
key (``config/license_private.pem``) and writes signed license files to
``./licenses``. The key never leaves the process: the browser only receives the
issued JSON, never the key material.

Run as the vendor only (the customer side installs licenses from the admin
console's 许可 card). The server binds to loopback by default and every API call
carries a per-process token injected into the page, so a random website cannot
drive it through the browser.

    --key   path to the signing key (default config/license_private.pem)
    --out   where issued files are kept (default ./licenses, gitignored)
"""

import argparse
import base64
import hmac
import html
import json
import os
import re
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives import serialization  # noqa: E402

from scripts import issue_license  # noqa: E402
from src import license as license_module  # noqa: E402

DEFAULT_KEY = "./config/license_private.pem"
DEFAULT_OUT = "./licenses"
MAX_BODY = 1024 * 1024


class ConsoleState:
    def __init__(self, key_path: str, out_dir: str):
        self.key_path = Path(key_path)
        self.out_dir = Path(out_dir)
        self.token = secrets.token_urlsafe(24)

    # --- key ---------------------------------------------------------------
    def key_info(self) -> dict:
        info = {
            "path": str(self.key_path),
            "exists": self.key_path.is_file(),
            "public_key": "",
            "error": None,
        }
        if not info["exists"]:
            info["error"] = "signing key not found; run scripts/issue_license.py --init-keys"
            return info
        try:
            key = license_module.load_private_key(str(self.key_path))
            raw = key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
            info["public_key"] = base64.b64encode(raw).decode("ascii")
        except license_module.LicenseError as exc:
            info["error"] = str(exc)
        return info

    # --- issued files ------------------------------------------------------
    def public_key(self) -> str:
        return self.key_info()["public_key"]

    def list_licenses(self) -> list:
        if not self.out_dir.is_dir():
            return []
        public = self.public_key()
        items = []
        files = sorted(
            self.out_dir.glob("*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:200]
        for path in files:
            item = {"file": path.name, "licensee": "?", "expires_at": "?", "state": "invalid"}
            try:
                payload = license_module.verify_blob(
                    json.loads(path.read_text(encoding="utf-8")), public
                )
                item.update(
                    licensee=payload["licensee"],
                    license_id=payload.get("license_id", ""),
                    expires_at=payload["expires_at"],
                    state="valid",
                )
            except Exception:
                item["error"] = "signature or format is invalid"
            items.append(item)
        return items


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", (text or "").strip()).strip("-")
    return cleaned[:40] or "license"


def issue_form(state: ConsoleState, form: dict) -> dict:
    """Sign a form submission and write it to the out directory."""
    licensee = (form.get("licensee") or "").strip()
    if not licensee:
        raise ValueError("licensee is required")
    payload = issue_license.build_payload(
        licensee,
        edition=form.get("edition") or "enterprise",
        days=form.get("days") or 365,
        expires=form.get("expires") or None,
        issued_at=form.get("issued_at") or None,
        not_before=form.get("not_before") or None,
        depts=form.get("depts") or 0,
        docs=form.get("docs") or 0,
        features=form.get("features") or "",
        license_id=form.get("license_id") or "",
    )
    key = license_module.load_private_key(str(state.key_path))
    blob = license_module.sign_payload(payload, key)
    license_module.verify_blob(blob, state.public_key())  # self-check before writing

    state.out_dir.mkdir(parents=True, exist_ok=True)
    suffix = payload["license_id"].rsplit("-", 1)[-1].lower()
    filename = f"{_slug(payload['licensee'])}-{payload['expires_at']}-{suffix}.json"
    target = state.out_dir / filename
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(blob, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    return {"file": filename, "blob": blob, "payload": payload}


def verify_text(text: str, public_key: str = "") -> dict:
    try:
        blob = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON: {exc}") from exc
    payload = license_module.verify_blob(
        blob, public_key or license_module.PUBLIC_KEY_B64
    )
    return payload


def state_payload(state: ConsoleState) -> dict:
    return {
        "key": state.key_info(),
        "out": str(state.out_dir),
        "today": date.today().isoformat(),
        "licenses": state.list_licenses(),
    }


# --- HTTP ------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    state: ConsoleState = None  # type: ignore[assignment]
    server_version = "agent-rag-license-console"

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    # --- helpers -----------------------------------------------------------
    def _token_ok(self) -> bool:
        provided = self.headers.get("X-Console-Token", "")
        return bool(provided) and hmac.compare_digest(provided, self.state.token)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            raise ValueError("empty or oversized request body")
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise ValueError("Content-Type must be application/json")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    # --- routes ------------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            page = build_page(self.state)
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if not self._token_ok():
            self._json(403, {"error": "invalid console token"})
            return
        if parsed.path == "/api/state":
            self._json(200, state_payload(self.state))
            return
        if parsed.path == "/api/download":
            query = urllib.parse.parse_qs(parsed.query)
            name = (query.get("name") or [""])[0]
            target = self.state.out_dir / Path(name).name
            if not name or target.name != name or not target.is_file():
                self._json(404, {"error": "license file not found"})
                return
            self._send(200, target.read_bytes(), "application/json; charset=utf-8")
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._token_ok():
            self._json(403, {"error": "invalid console token"})
            return
        parsed = urllib.parse.urlparse(self.path)
        try:
            body = self._read_json()
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
            return
        try:
            if parsed.path == "/api/issue":
                self._json(200, issue_form(self.state, body))
                return
            if parsed.path == "/api/verify":
                text = body.get("text") or ""
                payload = verify_text(str(text), self.state.public_key())
                self._json(200, {"ok": True, "payload": payload})
                return
        except (ValueError, license_module.LicenseError, OSError) as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(404, {"error": "not found"})


def build_page(state: ConsoleState) -> str:
    token = html.escape(state.token)
    key = state.key_info()
    key_status = (
        f'<span class="ok">已加载</span> <code>{html.escape(key["public_key"])}</code>'
        if key["public_key"]
        else f'<span class="bad">{html.escape(key["error"] or "不可用")}</span>'
    )
    return PAGE.replace("__CONSOLE_TOKEN__", token).replace("__KEY_STATUS__", key_status)


PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agent-rag 许可控制台</title>
<style>
  :root { color-scheme: light dark; --bg:#f6f7f9; --panel:#fff; --text:#1b1f24;
          --muted:#5c6672; --border:#d8dee6; --accent:#2f6feb; --ok:#1a7f4b; --bad:#c62828; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#14181d; --panel:#1c2229; --text:#e8edf3; --muted:#9aa7b4; --border:#2f3944; --accent:#6b9aff; }
  }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,"PingFang SC","Microsoft YaHei",sans-serif;
         background:var(--bg); color:var(--text); }
  header { padding:18px 24px; border-bottom:1px solid var(--border); background:var(--panel); }
  header h1 { margin:0 0 4px; font-size:18px; }
  header .sub { color:var(--muted); font-size:12px; }
  main { max-width:1080px; margin:20px auto; padding:0 20px 40px; display:grid; gap:16px;
         grid-template-columns: minmax(0,1.4fr) minmax(0,1fr); }
  @media (max-width: 860px) { main { grid-template-columns: 1fr; } }
  section { background:var(--panel); border:1px solid var(--border); border-radius:12px; padding:16px 18px; }
  h2 { margin:0 0 12px; font-size:15px; }
  .hint { color:var(--muted); font-size:12px; margin:0 0 12px; }
  label { display:grid; gap:4px; font-size:12px; color:var(--muted); }
  input, select, textarea { width:100%; padding:7px 9px; border-radius:8px; border:1px solid var(--border);
      background:var(--bg); color:var(--text); font:inherit; }
  textarea { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; resize:vertical; }
  .grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }
  .grid .wide { grid-column:1 / -1; }
  @media (max-width: 620px) { .grid { grid-template-columns:1fr; } }
  .row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-top:12px; }
  button { padding:7px 12px; border-radius:8px; border:1px solid var(--border); background:var(--bg);
           color:var(--text); font:inherit; cursor:pointer; }
  button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:11px; word-break:break-all; }
  .ok { color:var(--ok); font-weight:600; }
  .bad { color:var(--bad); font-weight:600; }
  .msg { font-size:12px; margin-top:10px; min-height:17px; color:var(--muted); }
  .msg.error { color:var(--bad); }
  .msg.ok { color:var(--ok); }
  .result { margin-top:12px; padding:12px; border:1px solid var(--border); border-radius:10px; display:none; }
  .result.show { display:block; }
  .result dl { display:grid; grid-template-columns:auto 1fr; gap:4px 12px; margin:8px 0 0; font-size:12px; }
  .result dt { color:var(--muted); }
  .result dd { margin:0; }
  ul.files { list-style:none; padding:0; margin:10px 0 0; display:grid; gap:6px; font-size:12px; }
  ul.files li { display:flex; justify-content:space-between; gap:10px; align-items:center;
                border-top:1px solid var(--border); padding-top:6px; }
  .mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
</style>
</head>
<body>
<header>
  <h1>agent-rag 许可控制台</h1>
  <div class="sub">供应商本地工具 · 监听 127.0.0.1 · 私钥不会发送到浏览器</div>
  <div class="sub">签名密钥：__KEY_STATUS__</div>
</header>
<main>
  <section>
    <h2>签发许可</h2>
    <p class="hint">填写客户与授权范围，生成的 license.json 发给客户；客户在管理台「许可」卡片中安装，或放到 config/license.json 后启动。</p>
    <form id="issueForm">
      <div class="grid">
        <label class="wide">客户名称 <span class="bad">*</span>
          <input name="licensee" required maxlength="200" placeholder="ACME 有限公司">
        </label>
        <label>版本
          <select name="edition">
            <option value="enterprise">enterprise</option>
            <option value="standard">standard</option>
            <option value="evaluation">evaluation</option>
          </select>
        </label>
        <label>签发日期
          <input name="issued_at" type="date" id="issuedAt">
        </label>
        <label>有效天数
          <input name="days" type="number" min="1" max="3650" value="365">
        </label>
        <label>指定到期日（可选，优先于天数）
          <input name="expires" type="date">
        </label>
        <label>部门上限（0 = 不限）
          <input name="depts" type="number" min="0" max="1000" value="0">
        </label>
        <label>文档上限（0 = 不限）
          <input name="docs" type="number" min="0" max="100000000" value="0">
        </label>
        <label class="wide">功能开关（逗号分隔，可选）
          <input name="features" placeholder="sso,audit,monitoring">
        </label>
        <label class="wide">许可编号（留空自动生成）
          <input name="license_id" placeholder="LIC-20261117-AB12CD">
        </label>
      </div>
      <div class="row">
        <button type="submit" class="primary" id="issueBtn">签发</button>
        <span class="hint">签发后自动保存到 licenses/ 目录。</span>
      </div>
      <div class="msg" id="issueMsg" aria-live="polite"></div>
      <div class="result" id="issueResult">
        <div><b id="resultTitle"></b></div>
        <dl id="resultMeta"></dl>
        <div class="row">
          <button type="button" id="downloadBtn">下载 license.json</button>
          <button type="button" id="copyLicenseBtn">复制内容</button>
        </div>
      </div>
    </form>
  </section>

  <section>
    <h2>校验与历史</h2>
    <p class="hint">粘贴客户收到的文件，确认签名和有效期；下方列出本机已签发文件。</p>
    <label>license.json
      <textarea id="verifyText" rows="7" placeholder='{"payload": {...}, "signature": "..."}'></textarea>
    </label>
    <div class="row">
      <button type="button" id="verifyBtn">校验</button>
    </div>
    <div class="msg" id="verifyMsg" aria-live="polite"></div>
    <div class="result" id="verifyResult"><dl id="verifyMeta"></dl></div>
    <ul class="files" id="fileList"></ul>
  </section>
</main>
<script>
(function () {
  "use strict";
  const TOKEN = "__CONSOLE_TOKEN__";

  async function call(path, options) {
    const opts = Object.assign({}, options || {});
    opts.headers = Object.assign({ "X-Console-Token": TOKEN }, opts.headers || {});
    const res = await fetch(path, opts);
    const type = res.headers.get("content-type") || "";
    const data = type.includes("application/json") ? await res.json() : await res.text();
    if (!res.ok) {
      throw new Error((data && data.error) || ("HTTP " + res.status));
    }
    return data;
  }

  function setMsg(el, text, kind) {
    el.textContent = text || "";
    el.className = "msg" + (kind ? " " + kind : "");
  }

  function formValues(form) {
    const data = {};
    new FormData(form).forEach((value, key) => { data[key] = value; });
    return data;
  }

  let lastBlob = null;
  let lastFile = "";

  const issuedAt = document.getElementById("issuedAt");
  if (issuedAt && !issuedAt.value) {
    issuedAt.value = new Date().toISOString().slice(0, 10);
  }

  function renderResult(payload, file) {
    document.getElementById("issueResult").classList.add("show");
    document.getElementById("resultTitle").textContent =
      "已签发：" + payload.licensee + "（" + (payload.edition || "standard") + "）";
    const meta = document.getElementById("resultMeta");
    meta.textContent = "";
    const rows = [
      ["许可编号", payload.license_id || "-"],
      ["有效期", (payload.issued_at || "-") + " → " + (payload.expires_at || "-")],
      ["部门上限", payload.max_depts || "不限"],
      ["文档上限", payload.max_docs || "不限"],
      ["功能", (payload.features || []).join(", ") || "-"],
      ["保存文件", file || "-"],
    ];
    rows.forEach(([key, value]) => {
      const dt = document.createElement("dt");
      dt.textContent = key;
      const dd = document.createElement("dd");
      dd.textContent = String(value);
      meta.appendChild(dt);
      meta.appendChild(dd);
    });
  }

  async function refreshFiles() {
    const list = document.getElementById("fileList");
    try {
      const data = await call("/api/state");
      list.textContent = "";
      if (!data.licenses.length) {
        const empty = document.createElement("li");
        empty.textContent = "还没有签发文件。";
        list.appendChild(empty);
        return;
      }
      data.licenses.forEach((item) => {
        const li = document.createElement("li");
        const label = document.createElement("span");
        label.textContent = item.licensee + " · " + item.expires_at + " · " + item.file;
        const actions = document.createElement("span");
        const download = document.createElement("button");
        download.type = "button";
        download.textContent = "下载";
        download.onclick = () => downloadFile(item.file);
        const verify = document.createElement("button");
        verify.type = "button";
        verify.textContent = "校验";
        verify.onclick = () => {
          document.getElementById("verifyText").value = "";
          call("/api/download?name=" + encodeURIComponent(item.file), { method: "GET" })
            .then((text) => {
              document.getElementById("verifyText").value =
                typeof text === "string" ? text : JSON.stringify(text, null, 2);
              document.getElementById("verifyBtn").click();
            })
            .catch((err) => setMsg(document.getElementById("verifyMsg"), err.message, "error"));
        };
        actions.appendChild(download);
        actions.appendChild(document.createTextNode(" "));
        actions.appendChild(verify);
        li.appendChild(label);
        li.appendChild(actions);
        list.appendChild(li);
      });
    } catch (err) {
      list.textContent = "无法读取历史：" + err.message;
    }
  }

  async function downloadFile(name) {
    try {
      const text = await call("/api/download?name=" + encodeURIComponent(name), { method: "GET" });
      const body = typeof text === "string" ? text : JSON.stringify(text, null, 2);
      const blob = new Blob([body], { type: "application/json" });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = name;
      link.click();
      URL.revokeObjectURL(link.href);
    } catch (err) {
      setMsg(document.getElementById("issueMsg"), err.message, "error");
    }
  }

  document.getElementById("issueForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = document.getElementById("issueBtn");
    button.disabled = true;
    setMsg(document.getElementById("issueMsg"), "正在签发…");
    try {
      const data = await call("/api/issue", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(formValues(event.target)),
      });
      lastBlob = data.blob;
      lastFile = data.file;
      renderResult(data.payload, data.file);
      setMsg(document.getElementById("issueMsg"), "已签发并保存。", "ok");
      await refreshFiles();
    } catch (err) {
      setMsg(document.getElementById("issueMsg"), err.message, "error");
    } finally {
      button.disabled = false;
    }
  });

  document.getElementById("downloadBtn").onclick = () => {
    if (lastBlob) downloadFile(lastFile);
  };

  document.getElementById("copyLicenseBtn").onclick = async () => {
    if (!lastBlob) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(lastBlob, null, 2));
      setMsg(document.getElementById("issueMsg"), "内容已复制。", "ok");
    } catch (err) {
      setMsg(document.getElementById("issueMsg"), "复制失败，请用下载按钮。", "error");
    }
  };

  document.getElementById("verifyBtn").onclick = async () => {
    const text = document.getElementById("verifyText").value.trim();
    const result = document.getElementById("verifyResult");
    result.classList.remove("show");
    if (!text) {
      setMsg(document.getElementById("verifyMsg"), "请先粘贴或载入文件内容。", "error");
      return;
    }
    try {
      const data = await call("/api/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      const p = data.payload;
      const meta = document.getElementById("verifyMeta");
      meta.textContent = "";
      [
        ["结果", "签名有效"],
        ["客户", p.licensee],
        ["编号", p.license_id || "-"],
        ["有效期", (p.issued_at || "-") + " → " + (p.expires_at || "-")],
        ["上限", (p.max_depts || 0) + " 部门 / " + (p.max_docs || 0) + " 文档"],
        ["功能", (p.features || []).join(", ") || "-"],
      ].forEach(([key, value]) => {
        const dt = document.createElement("dt");
        dt.textContent = key;
        const dd = document.createElement("dd");
        dd.textContent = String(value);
        meta.appendChild(dt);
        meta.appendChild(dd);
      });
      result.classList.add("show");
      setMsg(document.getElementById("verifyMsg"), "签名有效。", "ok");
    } catch (err) {
      setMsg(document.getElementById("verifyMsg"), err.message, "error");
    }
  };

  refreshFiles();
})();
</script>
</body>
</html>
"""


def serve(state: ConsoleState, host: str, port: int, open_browser: bool) -> None:
    Handler.state = state
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"license console: {url}")
    print(f"signing key: {state.key_path} ({'found' if state.key_path.is_file() else 'MISSING'})")
    print(f"output dir: {state.out_dir}")
    print("press Ctrl+C to stop")
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", default=DEFAULT_KEY, help=f"signing key (default {DEFAULT_KEY})")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"output dir (default {DEFAULT_OUT})")
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default loopback)")
    parser.add_argument("--port", type=int, default=8765, help="port (default 8765)")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    args = parser.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "refusing to bind a non-loopback host: this console holds the signing key",
            file=sys.stderr,
        )
        return 1

    state = ConsoleState(args.key, args.out)
    info = state.key_info()
    if not info["exists"]:
        print(
            f"{args.key} not found. Create it first:\n"
            "  python scripts/issue_license.py --init-keys",
            file=sys.stderr,
        )
        return 1
    if info["error"]:
        print(f"cannot read signing key: {info['error']}", file=sys.stderr)
        return 1
    serve(state, args.host, args.port, not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
