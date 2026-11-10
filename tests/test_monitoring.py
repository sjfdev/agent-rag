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


def test_missing_file_is_reported(monitoring, monkeypatch, capsys):
    monkeypatch.setattr(monitoring, "PROMETHEUS", ROOT / "monitoring" / "does-not-exist")
    assert monitoring.main() == 1
    output = capsys.readouterr().out
    assert "FAIL  monitoring/does-not-exist/prometheus.yml exists" in output
    assert "RESULT: FAILED" in output
