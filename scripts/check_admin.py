"""End-to-end smoke test for the admin API (upload -> ingest -> delete).

Usage:
    uvicorn main:app --port 8000          # in another terminal
    python scripts/check_admin.py
    ADMIN_URL=http://host:8000 ADMIN_TOKEN=... python scripts/check_admin.py
"""

import os
import shutil
import sys
import time
import uuid
from pathlib import Path

import requests

BASE = os.getenv("ADMIN_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.getenv("ADMIN_TOKEN", "")
TIMEOUT = int(os.getenv("ADMIN_TIMEOUT", "300"))

HEADERS = {"X-Admin-Token": TOKEN} if TOKEN else {}
failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"PASS  {name}")
    else:
        failures.append(name)
        print(f"FAIL  {name}  {detail}")


def api(method, path, **kwargs):
    kwargs.setdefault("timeout", 30)
    headers = dict(HEADERS)
    headers.update(kwargs.pop("headers", {}))
    resp = requests.request(method, BASE + path, headers=headers, **kwargs)
    if resp.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
    if resp.headers.get("content-type", "").startswith("application/json"):
        return resp.json()
    return resp.text


def main():
    dept = "AdminSmoke" + uuid.uuid4().hex[:6]
    source = f"{dept}/smoke.md"
    meta = api("GET", "/admin/api/meta")
    data_dir = Path(meta["data_dir"])

    print(f"Admin API: {BASE}  backend={meta['vector_store']}  dept={dept}")

    created = api("POST", "/admin/api/dept", json={"name": dept})
    check("create department", created.get("name") == dept)

    marker = f"ADMIN_SMOKE_{uuid.uuid4().hex[:8]}"
    upload = api(
        "POST",
        "/admin/api/upload",
        data={"dept": dept, "subdir": ""},
        files={"files": ("smoke.md", f"# Smoke\n\nmarker {marker}\n".encode(), "text/markdown")},
    )
    check("upload file", any(s["source"] == source for s in upload["saved"]), str(upload)[:200])

    listed = api("GET", f"/admin/api/files?dept={dept}")
    check("file listed as pending", listed["files"] and listed["files"][0]["status"] == "pending")

    job = api("POST", "/admin/api/ingest", json={"dept": dept})
    job_id = job["job_id"]
    deadline = time.time() + TIMEOUT
    status = "queued"
    while time.time() < deadline:
        state = api("GET", f"/admin/api/jobs/{job_id}")
        status = state["status"]
        if status in {"done", "error"}:
            break
        time.sleep(1)
    check("ingestion finished", status == "done", str(state.get("log"))[-300:])
    check("one chunk ingested", (state.get("stats") or {}).get("chunks") == 1)

    listed = api("GET", f"/admin/api/files?dept={dept}")
    item = listed["files"][0] if listed["files"] else {}
    check("file indexed", item.get("status") == "indexed", str(item))
    check("chunk count visible", item.get("chunks") == 1, str(item))

    deleted = api("DELETE", f"/admin/api/file?dept={dept}&source={source}")
    check("delete file", deleted.get("deleted") == source)
    check("chunks removed", deleted.get("chunks_removed") == 1)

    listed = api("GET", f"/admin/api/files?dept={dept}")
    check("file gone from listing", not listed["files"])

    # Files are gone, but vectors are only removed by a full prune. Remove the
    # (now empty) department folder and prune so the smoke test leaves no trace.
    shutil.rmtree(data_dir / dept, ignore_errors=True)
    job = api("POST", "/admin/api/ingest", json={})
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        if api("GET", f"/admin/api/jobs/{job['job_id']}")["status"] in {"done", "error"}:
            break
        time.sleep(1)

    print()
    if failures:
        print(f"RESULT: FAILED ({len(failures)}): {', '.join(failures)}")
        return 1
    print("RESULT: OK - admin API checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
