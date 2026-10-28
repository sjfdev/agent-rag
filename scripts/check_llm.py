"""Probe the configured LLM endpoint before starting the API server.

Usage:
    python scripts/check_llm.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from src.config import settings


def main():
    base = settings.LLM_API_BASE.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.LLM_API_KEY}"}

    print(f"LLM_API_BASE = {base}")
    print(f"LLM_MODEL    = {settings.LLM_MODEL}")

    try:
        r = requests.get(f"{base}/models", headers=headers, timeout=10)
        r.raise_for_status()
        ids = [m.get("id") for m in r.json().get("data", [])]
        print(f"OK  /models reachable, {len(ids)} model(s) available")
        if settings.LLM_MODEL not in ids:
            print(f"WARN model '{settings.LLM_MODEL}' not in list; available: {ids[:10]}")
    except Exception as e:
        # Not every OpenAI-compatible provider implements GET /models.
        print(f"WARN /models unavailable ({e}); continuing with a chat probe")

    try:
        payload = {
            "model": settings.LLM_MODEL,
            "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
            "temperature": 0,
            "max_tokens": 16,
        }
        r = requests.post(f"{base}/chat/completions", headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]["content"].strip()
        print(f"OK  chat completion works: {msg!r}")
    except Exception as e:
        print(f"FAIL /chat/completions: {e}")
        print("Hints:")
        print("  - 401/403: LLM_API_KEY is wrong or missing")
        print("  - 404: LLM_MODEL name is wrong for this provider")
        print("  - timeout: network/proxy issue, or model is too slow")
        print("  - Ollama: run `ollama serve` and `ollama pull <model>`")
        return 1

    print("\nLLM endpoint is ready. You can start: uvicorn main:app --port 8000")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
