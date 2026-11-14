"""Monitoring stack configuration (scripts/check_monitoring.py)."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_monitoring.py"


@pytest.fixture()
def monitoring():
    spec = importlib.util.spec_from_file_location("check_monitoring", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    yield module
    module.PASS.clear()
    module.FAIL.clear()


@pytest.fixture()
def renderer():
    path = ROOT / "scripts" / "render_alertmanager.py"
    spec = importlib.util.spec_from_file_location("render_alertmanager", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_monitoring_configs_pass(monitoring, capsys):
    assert monitoring.main() == 0
    output = capsys.readouterr().out
    assert "RESULT: OK" in output
    assert "alerts.yml has the required alerts" in output


def test_required_alerts_are_present(monitoring):
    import yaml

    data = yaml.safe_load((ROOT / "monitoring/prometheus/alerts.yml").read_text(encoding="utf-8"))
    names = {rule["alert"] for group in data["groups"] for rule in group["rules"]}
    assert monitoring.REQUIRED_ALERTS <= names


def test_alert_metrics_exist_in_code(monitoring):
    import yaml

    known = monitoring.source_metric_names()
    data = yaml.safe_load((ROOT / "monitoring/prometheus/alerts.yml").read_text(encoding="utf-8"))
    referenced = set()
    for expr in monitoring.all_exprs(data):
        referenced |= monitoring.referenced_metrics(expr, known)
    assert referenced <= known


def test_referenced_metrics_strips_histogram_suffixes(monitoring):
    known = {"agent_rag_chat_duration_seconds"}
    assert monitoring.referenced_metrics(
        "histogram_quantile(0.95, rate(agent_rag_chat_duration_seconds_bucket[5m]))", known
    ) == known
    assert monitoring.referenced_metrics("agent_rag_missing_total", known) == {
        "agent_rag_missing_total"
    }


def test_dashboard_is_valid_json_with_panels():
    dashboard = json.loads(
        (ROOT / "monitoring/grafana/dashboards/agent-rag.json").read_text(encoding="utf-8")
    )
    assert dashboard["uid"] == "agent-rag-overview"
    assert len(dashboard["panels"]) >= 8
    ids = [panel["id"] for panel in dashboard["panels"]]
    assert len(ids) == len(set(ids))


def test_compose_uses_external_prod_network():
    import yaml

    compose = yaml.safe_load(
        (ROOT / "docker-compose.monitoring.yml").read_text(encoding="utf-8")
    )
    assert {"prometheus", "blackbox", "grafana"} <= set(compose["services"])
    assert compose["networks"]["prod"]["external"] is True


def test_alertmanager_without_channels_still_starts(renderer, tmp_path):
    dest = tmp_path / "alertmanager.yml"
    config = renderer.render(env={}, dest=dest)
    assert [receiver["name"] for receiver in config["receivers"]] == ["noop"]
    assert config["route"]["receiver"] == "noop"
    assert config["route"]["routes"] == []
    written = pytest.importorskip("yaml").safe_load(dest.read_text(encoding="utf-8"))
    assert written == config


def test_alertmanager_all_channels_are_routed(renderer, tmp_path):
    env = {
        "ALERTMANAGER_EMAIL_TO": "oncall@example.com, lead@example.com",
        "ALERTMANAGER_SMTP_SMARTHOST": "smtp.example.com:587",
        "ALERTMANAGER_SMTP_FROM": "agent-rag@example.com",
        "ALERTMANAGER_SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/x",
        "ALERTMANAGER_SLACK_CHANNEL": "#alerts",
        "ALERTMANAGER_WEBHOOK_URL": "https://example.com/hook",
    }
    config = renderer.render(env=env, dest=tmp_path / "alertmanager.yml")
    names = {receiver["name"] for receiver in config["receivers"]}
    assert names == {"noop", "email", "slack", "webhook"}
    routed = {route["receiver"] for route in config["route"]["routes"]}
    assert routed == {"email", "slack", "webhook"}
    assert all(route.get("continue") is True for route in config["route"]["routes"])

    email = next(r for r in config["receivers"] if r["name"] == "email")
    assert email["email_configs"][0]["to"] == "oncall@example.com,lead@example.com"
    slack = next(r for r in config["receivers"] if r["name"] == "slack")
    assert slack["slack_configs"][0]["channel"] == "#alerts"
    assert config["global"]["smtp_smarthost"] == "smtp.example.com:587"


def test_alertmanager_email_requires_smtp_host(renderer, tmp_path):
    with pytest.raises(ValueError, match="SMTP"):
        renderer.render(
            env={"ALERTMANAGER_EMAIL_TO": "oncall@example.com"},
            dest=tmp_path / "alertmanager.yml",
        )


def test_alertmanager_reads_password_file(renderer, tmp_path, monkeypatch):
    password_file = tmp_path / "smtp_password"
    password_file.write_text("s3cret\n", encoding="utf-8")
    config = renderer.render(
        env={
            "ALERTMANAGER_EMAIL_TO": "oncall@example.com",
            "ALERTMANAGER_SMTP_SMARTHOST": "smtp.example.com:587",
            "ALERTMANAGER_SMTP_FROM": "agent-rag@example.com",
            "ALERTMANAGER_SMTP_AUTH_USERNAME": "agent-rag@example.com",
            "ALERTMANAGER_SMTP_AUTH_PASSWORD_FILE": str(password_file),
        },
        dest=tmp_path / "alertmanager.yml",
    )
    assert config["global"]["smtp_auth_password"] == "s3cret"


def test_alertmanager_inhibits_metrics_derived_alerts(renderer, tmp_path):
    config = renderer.render(env={}, dest=tmp_path / "alertmanager.yml")
    rule = config["inhibit_rules"][0]
    assert rule["source_matchers"] == ['alertname = "AgentRagMetricsDown"']
    assert "AgentRagErrorRate" in rule["target_matchers"][0]


def test_env_file_parser(renderer, tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        '# comment\nALERTMANAGER_WEBHOOK_URL=https://example.com/hook\n'
        'ALERTMANAGER_SLACK_CHANNEL="#alerts"\n\nIGNORED\n',
        encoding="utf-8",
    )
    values = renderer.read_env_file(path)
    assert values == {
        "ALERTMANAGER_WEBHOOK_URL": "https://example.com/hook",
        "ALERTMANAGER_SLACK_CHANNEL": "#alerts",
    }


def test_prometheus_forwards_to_alertmanager():
    import yaml

    config = yaml.safe_load(
        (ROOT / "monitoring/prometheus/prometheus.yml").read_text(encoding="utf-8")
    )
    targets = [
        target
        for manager in config["alerting"]["alertmanagers"]
        for static in manager["static_configs"]
        for target in static["targets"]
    ]
    assert "alertmanager:9093" in targets


def test_compose_wires_alertmanager():
    import yaml

    text = (ROOT / "docker-compose.monitoring.yml").read_text(encoding="utf-8")
    compose = yaml.safe_load(text)
    package = compose["services"]["alertmanager"]
    assert package["image"].startswith("${ALERTMANAGER_IMAGE")
    assert "./monitoring/alertmanager:/etc/alertmanager:ro" in text
    assert "127.0.0.1:${ALERTMANAGER_PORT" in text
    assert "alertmanager" in compose["services"]["prometheus"]["depends_on"]


def test_loki_retains_logs_with_env_tunable_window():
    import yaml

    loki = yaml.safe_load((ROOT / "monitoring/loki/loki.yml").read_text(encoding="utf-8"))
    assert loki["auth_enabled"] is False
    assert loki["server"]["http_listen_port"] == 3100
    assert loki["limits_config"]["retention_period"] == "${LOKI_RETENTION:-720h}"
    assert loki["compactor"]["retention_enabled"] is True
    assert loki["schema_config"]["configs"][0]["store"] == "tsdb"


def test_promtail_ships_only_stack_containers():
    import yaml

    promtail = yaml.safe_load(
        (ROOT / "monitoring/promtail/promtail.yml").read_text(encoding="utf-8")
    )
    assert promtail["clients"][0]["url"] == "http://loki:3100/loki/api/v1/push"
    scrape = promtail["scrape_configs"][0]
    assert scrape["docker_sd_configs"][0]["host"] == "unix:///var/run/docker.sock"
    keeps = [item for item in scrape["relabel_configs"] if item.get("action") == "keep"]
    assert keeps and keeps[0]["regex"] == "/agent-rag-.*"
    assert promtail["positions"]["filename"].startswith("/var/lib/promtail/")


def test_dashboard_logs_panel_uses_loki():
    dashboard = json.loads(
        (ROOT / "monitoring/grafana/dashboards/agent-rag.json").read_text(encoding="utf-8")
    )
    logs = [panel for panel in dashboard["panels"] if panel["type"] == "logs"]
    assert logs, "expected a Loki log panel"
    assert logs[0]["datasource"]["uid"] == "loki"
    assert logs[0]["targets"][0]["expr"] == '{service="app"}'


def test_grafana_provisions_loki_datasource():
    import yaml

    entries = yaml.safe_load(
        (ROOT / "monitoring/grafana/provisioning/datasources/loki.yml").read_text(
            encoding="utf-8"
        )
    )["datasources"]
    assert entries[0]["uid"] == "loki"
    assert entries[0]["type"] == "loki"
    assert entries[0]["url"] == "http://loki:3100"


def test_compose_wires_loki_and_promtail():
    import yaml

    text = (ROOT / "docker-compose.monitoring.yml").read_text(encoding="utf-8")
    compose = yaml.safe_load(text)
    assert {"loki", "promtail"} <= set(compose["services"])
    assert "./monitoring/loki:/etc/loki:ro" in text
    assert "./monitoring/promtail:/etc/promtail:ro" in text
    assert "${DOCKER_SOCKET:-/var/run/docker.sock}:/var/run/docker.sock:ro" in text
    assert compose["services"]["promtail"]["depends_on"] == ["loki"]
    assert "127.0.0.1:${LOKI_PORT" in text


def test_missing_file_is_reported(monitoring, monkeypatch, capsys):
    monkeypatch.setattr(monitoring, "PROMETHEUS", ROOT / "monitoring" / "does-not-exist")
    assert monitoring.main() == 1
    output = capsys.readouterr().out
    assert "FAIL  monitoring/does-not-exist/prometheus.yml exists" in output
    assert "RESULT: FAILED" in output
