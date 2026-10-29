"""End-to-end smoke tests against a running API server.

Usage:
    uvicorn main:app --port 8000      # in another terminal
    python test_agent.py

Exit code is non-zero when any assertion fails, so this can gate CI.
"""

import os
import sys
import time

import requests

URL = os.getenv("CHAT_URL", "http://localhost:8000/chat")
TIMEOUT = int(os.getenv("CHAT_TIMEOUT", "300"))

failures = []


def ask(user_id, dept, message, history=None):
    payload = {
        "user_id": user_id,
        "user_dept": dept,
        "message": message,
        "history": history or [],
    }
    started = time.time()
    resp = requests.post(URL, json=payload, timeout=TIMEOUT)
    elapsed = time.time() - started

    if resp.status_code != 200:
        print(f"  HTTP {resp.status_code}: {resp.text[:300]}\n")
        return None, elapsed

    data = resp.json()
    t = data.get("timings", {})
    breakdown = ", ".join(
        f"{k}={v}" + ("" if k.endswith("_calls") else "ms")
        for k, v in t.items()
        if k != "total_ms"
    )
    print(f"  -> [{elapsed:.1f}s] {data['response']}")
    if breakdown:
        print(f"     timings: {breakdown}\n")
    return data, elapsed


def check(name, condition, detail=""):
    if condition:
        print(f"PASS  {name}")
    else:
        failures.append(name)
        print(f"FAIL  {name}  {detail}")


def has_timings(data):
    t = (data or {}).get("timings", {})
    return all(k in t for k in ("retrieval_ms", "llm_ms", "total_ms")) and t[
        "llm_calls"
    ] >= 1


def main():
    # 1. Permission granted: Tech asks about coding standards
    print("[1] Tech: code review requirements")
    data, _ = ask("u1", "Tech", "What are the code review requirements?")
    text = (data or {}).get("response", "").lower()
    check("code review answered", "review" in text and "pull request" in text, text[:120])
    check("code review timings", has_timings(data))

    # 2. Permission denied: Tech must not see Finance salary bands
    print("[2] Tech: salary bands (cross-department)")
    data, _ = ask("u1", "Tech", "What are the salary bands in the 2026 budget?")
    text = (data or {}).get("response", "")
    check("no budget figure leaked", "48,000,000" not in text, text[:120])
    check("refusal is explicit", "not available" in text.lower() or "no access" in text.lower(), text[:120])

    # 3. Permission granted: Finance sees the confidential budget
    print("[3] Finance: 2026 operating budget")
    data, _ = ask("u2", "Finance", "What is the total 2026 operating budget?")
    text = (data or {}).get("response", "")
    check("budget figure returned", "48,000,000" in text, text[:120])

    # 4. HR policy retrieval
    print("[4] HR: annual leave")
    data, _ = ask("u3", "HR", "How many days of annual leave do I get?")
    text = (data or {}).get("response", "")
    check("leave days returned", "15" in text, text[:120])

    # 5. Multi-turn conversation (same user, pass prior history)
    print("[5] HR: multi-turn carry-over")
    history = [
        {"role": "user", "content": "How many days of annual leave do I get?"},
        {"role": "assistant", "content": "Full-time employees accrue 15 days per year."},
    ]
    data, _ = ask("u3", "HR", "Can I carry them over to next year?", history=history)
    text = (data or {}).get("response", "")
    check("carry-over answered", "5" in text or "five" in text.lower(), text[:120])

    # 6. Out of scope: should trigger the support ticket tool
    print("[6] Tech: broken laptop -> ticket")
    data, _ = ask("u1", "Tech", "My laptop is broken and I need a replacement.")
    text = (data or {}).get("response", "")
    check("ticket created", "TICK-" in text, text[:120])
    check("ticket used two LLM calls", (data or {}).get("timings", {}).get("llm_calls", 0) >= 2)

    # 7. Invalid input: department names are validated before reaching the store
    print("[7] Invalid department is rejected")
    try:
        resp = requests.post(
            URL,
            json={"user_id": "u1", "user_dept": "Tech' or dept != '", "message": "hi", "history": []},
            timeout=30,
        )
        check("invalid dept -> 400", resp.status_code == 400, f"got {resp.status_code}")
    except Exception as e:
        check("invalid dept -> 400", False, str(e))

    print()
    if failures:
        print(f"RESULT: FAILED ({len(failures)}): {', '.join(failures)}")
        return 1
    print("RESULT: OK - all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
