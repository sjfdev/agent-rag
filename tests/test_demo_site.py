"""Demo stack stays read-only, unlicensed and disconnected from real data.

演示栈必须：无需许可即可启动、不持久化访客会话、只挂载 Demo 数据。
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def test_demo_compose_is_locked_down():
    config = yaml.safe_load((ROOT / "docker-compose.demo.yml").read_text(encoding="utf-8"))
    app = config["services"]["app"]
    env = app["environment"]
    assert env["LICENSE_ENABLED"] == "false"
    assert env["SESSIONS_ENABLED"] == "false"
    assert env["AUTH_MODE"] == "none"
    assert env["VECTOR_STORE_TYPE"] == "faiss"
    assert env["DATA_DIR"] == "/app/data"

    # Only the synthetic Demo department is mounted, read-only.
    volumes = app["volumes"]
    assert "./data/Demo:/app/data/Demo:ro" in volumes
    assert not any(volume.startswith("./data:") for volume in volumes)

    # Loopback-only port: a TLS proxy must sit in front of a public demo.
    assert app["ports"] == ["127.0.0.1:${DEMO_PORT:-8080}:8000"]


def test_demo_env_example_has_no_production_defaults():
    text = (ROOT / ".env.demo.example").read_text(encoding="utf-8")
    assert "LICENSE_ENABLED=false" in text
    assert "SESSIONS_ENABLED=false" in text
    assert "AUTH_MODE=none" in text
    assert "ADMIN_TOKEN=" in text
    assert "RATE_LIMIT_PER_MINUTE=10" in text


def test_demo_targets_are_documented():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    for target in ("demo-up:", "demo-seed:", "demo-down:", "usage-report:"):
        assert target in makefile
