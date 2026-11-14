"""Render monitoring/alertmanager/alertmanager.yml from environment / .env.

Alertmanager does not expand environment variables in its own config file, and
the official image ships no shell tooling to do it at container start. Secrets
therefore never belong in the versioned config; this renders the real file on
the host (``make monitor-config``, also run by ``make monitor-up`` and
``make monitor-check``) into a gitignored path.

Channels are opt-in and each is only emitted when its variables are set:

  - email   ALERTMANAGER_EMAIL_TO + ALERTMANAGER_SMTP_SMARTHOST/FROM
  - slack   ALERTMANAGER_SLACK_WEBHOOK_URL
  - webhook ALERTMANAGER_WEBHOOK_URL

With no channel configured the stack still starts: a single ``noop`` receiver
is routed to, so the file is always a valid Alertmanager config.

    python scripts/render_alertmanager.py
"""

import os
import sys
from pathlib import Path
from typing import Dict, Mapping, Optional

ROOT = Path(__file__).resolve().parent.parent
ALERTMANAGER_DIR = ROOT / "monitoring" / "alertmanager"
DEFAULT_DEST = ALERTMANAGER_DIR / "alertmanager.yml"
DEFAULT_PASSWORD_FILE = ALERTMANAGER_DIR / "smtp_password"
ENV_FILE = ROOT / ".env"

# Accepted values for the optional TLS switch (587 usually needs it, 465 does
# not because the connection is already wrapped).
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

SEVERITY_MATCHER = 'severity =~ "critical|warning"'


def read_env_file(path: Path) -> Dict[str, str]:
    """Minimal ``.env`` parser: KEY=VALUE, ``#`` comments, optional quotes."""
    values: Dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def default_env() -> Dict[str, str]:
    """``.env`` first, real environment wins (matches docker compose)."""
    merged = read_env_file(ENV_FILE)
    merged.update(os.environ)
    return merged


def _truthy(value: str) -> bool:
    return value.strip().lower() in _TRUE


def _split_addresses(value: str) -> str:
    """Alertmanager expects one comma-separated string for ``to``."""
    return ",".join(part.strip() for part in value.split(",") if part.strip())


def _read_password(env: Mapping[str, str]) -> Optional[str]:
    direct = env.get("ALERTMANAGER_SMTP_AUTH_PASSWORD", "").strip()
    if direct:
        return direct
    path = Path(env.get("ALERTMANAGER_SMTP_AUTH_PASSWORD_FILE", "") or DEFAULT_PASSWORD_FILE)
    if path.is_file():
        return path.read_text(encoding="utf-8").strip() or None
    return None


def build_config(env: Mapping[str, str]) -> Dict[str, object]:
    """Turn the ALERTMANAGER_* variables into an Alertmanager config dict."""
    group_wait = env.get("ALERTMANAGER_GROUP_WAIT", "").strip() or "30s"
    group_interval = env.get("ALERTMANAGER_GROUP_INTERVAL", "").strip() or "5m"
    repeat_interval = env.get("ALERTMANAGER_REPEAT_INTERVAL", "").strip() or "4h"

    global_config: Dict[str, object] = {"resolve_timeout": "5m"}
    receivers = [{"name": "noop"}]
    routes = []

    email_to = _split_addresses(env.get("ALERTMANAGER_EMAIL_TO", ""))
    if email_to:
        smarthost = env.get("ALERTMANAGER_SMTP_SMARTHOST", "").strip()
        sender = env.get("ALERTMANAGER_SMTP_FROM", "").strip()
        if not smarthost or not sender:
            raise ValueError(
                "ALERTMANAGER_EMAIL_TO is set but ALERTMANAGER_SMTP_SMARTHOST/"
                "ALERTMANAGER_SMTP_FROM is missing"
            )
        global_config["smtp_smarthost"] = smarthost
        global_config["smtp_from"] = sender
        username = env.get("ALERTMANAGER_SMTP_AUTH_USERNAME", "").strip()
        if username:
            password = _read_password(env)
            if not password:
                raise ValueError(
                    "ALERTMANAGER_SMTP_AUTH_USERNAME is set but no password: put it in "
                    "ALERTMANAGER_SMTP_AUTH_PASSWORD or monitoring/alertmanager/smtp_password"
                )
            global_config["smtp_auth_username"] = username
            global_config["smtp_auth_password"] = password
        tls = env.get("ALERTMANAGER_SMTP_REQUIRE_TLS", "").strip()
        if tls:
            global_config["smtp_require_tls"] = _truthy(tls)
        receivers.append(
            {"name": "email", "email_configs": [{"to": email_to, "send_resolved": True}]}
        )
        routes.append(
            {"receiver": "email", "matchers": [SEVERITY_MATCHER], "continue": True}
        )

    slack_url = env.get("ALERTMANAGER_SLACK_WEBHOOK_URL", "").strip()
    if slack_url:
        slack: Dict[str, object] = {"api_url": slack_url, "send_resolved": True}
        channel = env.get("ALERTMANAGER_SLACK_CHANNEL", "").strip()
        if channel:
            slack["channel"] = channel
        receivers.append({"name": "slack", "slack_configs": [slack]})
        routes.append(
            {"receiver": "slack", "matchers": [SEVERITY_MATCHER], "continue": True}
        )

    webhook_url = env.get("ALERTMANAGER_WEBHOOK_URL", "").strip()
    if webhook_url:
        receivers.append(
            {
                "name": "webhook",
                "webhook_configs": [{"url": webhook_url, "send_resolved": True}],
            }
        )
        routes.append(
            {"receiver": "webhook", "matchers": [SEVERITY_MATCHER], "continue": True}
        )

    return {
        "global": global_config,
        "route": {
            "receiver": "noop",
            "group_by": ["alertname", "severity"],
            "group_wait": group_wait,
            "group_interval": group_interval,
            "repeat_interval": repeat_interval,
            "routes": routes,
        },
        "receivers": receivers,
        # A failed scrape silences the derived signal alerts: they all read the
        # same app metrics and would just repeat "instance is unreachable".
        "inhibit_rules": [
            {
                "source_matchers": ['alertname = "AgentRagMetricsDown"'],
                "target_matchers": [
                    'alertname =~ "AgentRagErrorRate|AgentRagCacheHitDrop|AgentRagMaintenanceStuck"'
                ],
                "equal": ["instance"],
            }
        ],
    }


def render(
    env: Optional[Mapping[str, str]] = None,
    dest: Path = DEFAULT_DEST,
) -> Dict[str, object]:
    """Build the config and write it atomically (YAML) to ``dest``."""
    import yaml

    config = build_config(default_env() if env is None else env)
    dest.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(config, sort_keys=False, allow_unicode=True, default_flow_style=False)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, dest)
    return config


def main() -> int:
    try:
        config = render()
    except ValueError as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        return 1
    names = ",".join(receiver["name"] for receiver in config["receivers"])  # type: ignore[index]
    print(f"PASS  wrote {DEFAULT_DEST.relative_to(ROOT)} (receivers: {names})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
