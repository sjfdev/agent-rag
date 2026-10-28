import os
import time

import requests

URL = "http://localhost:8000/chat"


def test_agent(user_id, dept, message, history=None):
    print(f"[{dept}] {message}")
    payload = {
        "user_id": user_id,
        "user_dept": dept,
        "message": message,
        "history": history or [],
    }
    try:
        start = time.time()
        response = requests.post(URL, json=payload, timeout=int(os.getenv("CHAT_TIMEOUT", "300")))
        response.raise_for_status()
        data = response.json()
        t = data.get("timings", {})
        breakdown = ", ".join(
            f"{k}={v}" + ("" if k.endswith("_calls") else "ms")
            for k, v in t.items()
            if k != "total_ms"
        )
        print(f"  -> [{time.time() - start:.1f}s] {data['response']}")
        if breakdown:
            print(f"     timings: {breakdown}\n")
    except Exception as e:
        print(f"  -> Error: {e}\n")


if __name__ == "__main__":
    # 1. Permission granted: Tech asks about coding standards
    test_agent("u1", "Tech", "What are the code review requirements?")

    # 2. Permission denied: Tech user must NOT see Finance budget
    test_agent("u1", "Tech", "What are the salary bands in the 2026 budget?")

    # 3. Permission granted: Finance sees the confidential budget
    test_agent("u2", "Finance", "What is the total 2026 operating budget?")

    # 4. HR policy retrieval
    test_agent("u3", "HR", "How many days of annual leave do I get and when do they expire?")

    # 5. Multi-turn conversation (same user, pass prior history)
    test_agent("u3", "HR", "How many days of annual leave do I get?")
    history = [
        {"role": "user", "content": "How many days of annual leave do I get?"},
        {"role": "assistant", "content": "Full-time employees accrue 15 days per year."},
    ]
    test_agent("u3", "HR", "Can I carry them over to next year?", history=history)

    # 6. Out of scope: should trigger the support ticket tool
    test_agent("u1", "Tech", "My laptop is broken and I need a replacement.")
