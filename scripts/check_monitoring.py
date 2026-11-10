"""Validate the monitoring stack configuration offline.

Checks that Prometheus, blackbox_exporter and Grafana files agree with the
metrics the application actually exports, so a typo in a rule or dashboard is
caught before a deploy rather than after an incident:

  - `monitoring/prometheus/prometheus.yml` scrapes the app and /health
  - `monitoring/prometheus/alerts.yml` has the required alerts and only
    references `agent_rag_*` metrics that exist in `src/metrics.py`
  - the Grafana dashboard/provisioning files parse and target the same metrics
  - `docker-compose.monitoring.yml` wires the files and the prod network

Run `promtool check rules` too when promtool is installed. Exit code is
non-zero on any failure; no network access is needed.

    python scripts/check_monitoring.py
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

ROOT = Path(__file__).resolve().parent.parent
MONITORING = ROOT / "monitoring"
PROMETHEUS = MONITORING / "prometheus"
GRAFANA = MONITORING / "grafana"

REQUIRED_ALERTS = {
    "AgentRagMetricsDown",
    "AgentRagHealthDown",
    "AgentRagIndexEmpty",
    "AgentRagErrorRate",
    "AgentRagCacheHitDrop",
}

# Metrics that come from Prometheus itself or blackbox_exporter, not the app.
EXTERNAL_METRICS = {"up", "probe_success", "probe_http_status_code"}

PASS: List[str] = []
FAIL: List[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name)
    line = f"{'PASS' if ok else 'FAIL'}  {name}"
    if detail:
        line += f"  {detail}"
    print(line)
    return ok


def load_yaml(path: Path) -> Optional[Dict[str, Any]]:
    try:
        import yaml
    except ImportError:
        check("PyYAML is installed", False)
        return None
    rel = path.relative_to(ROOT)
    if not path.is_file():
        check(f"{rel} exists", False)
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except Exception as exc:  # malformed YAML must be a readable failure
        check(f"{rel} parses", False, str(exc)[:200])
        return None
    return data if isinstance(data, dict) else None


def load_json(path: Path) -> Optional[Dict[str, Any]]:
    rel = path.relative_to(ROOT)
    if not path.is_file():
        check(f"{rel} exists", False)
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        check(f"{rel} parses", False, str(exc)[:200])
        return None


def source_metric_names() -> Set[str]:
    text = (ROOT / "src" / "metrics.py").read_text(encoding="utf-8")
    return set(re.findall(r'"(agent_rag_[a-zA-Z0-9_]+)"', text))


def referenced_metrics(expr: str, known: Set[str]) -> Set[str]:
    """Metric names in a PromQL expression, tolerating histogram suffixes."""
    names = set(re.findall(r"\bagent_rag_[a-zA-Z0-9_]+", expr))
    resolved = set()
    for name in names:
        if name in known:
            resolved.add(name)
            continue
        for suffix in ("_bucket", "_count", "_sum"):
            if name.endswith(suffix) and name[: -len(suffix)] in known:
                resolved.add(name[: -len(suffix)])
                break
        else:
            resolved.add(name)
    return resolved


def all_exprs(node: Any) -> Iterable[str]:
    """Walk an alert/dashboard structure and yield every `expr` value."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "expr" and isinstance(value, str):
                yield value
            else:
                yield from all_exprs(value)
    elif isinstance(node, list):
        for item in node:
            yield from all_exprs(item)


def compare_metrics(label: str, exprs: Iterable[str], known: Set[str]) -> None:
    unknown: Set[str] = set()
    for expr in exprs:
        unknown |= {name for name in referenced_metrics(expr, known) if name not in known}
    check(
        f"{label} only references known agent_rag_* metrics",
        not unknown,
        ", ".join(sorted(unknown)) if unknown else "",
    )


def check_prometheus(known: Set[str]) -> None:
    config = load_yaml(PROMETHEUS / "prometheus.yml")
    if config is None:
        return
    check("prometheus.yml parses", True)
    check(
        "prometheus.yml loads alerts.yml",
        any("alerts.yml" in str(item) for item in config.get("rule_files", [])),
    )
    jobs = {job.get("job_name"): job for job in config.get("scrape_configs", [])}
    check("prometheus.yml scrapes the app", "agent-rag" in jobs)
    app = jobs.get("agent-rag", {})
    app_targets = [
        target
        for static in app.get("static_configs", [])
        for target in static.get("targets", [])
    ]
    check("app scrape targets include app:8000", "app:8000" in app_targets)

    health = jobs.get("agent-rag-health", {})
    check("prometheus.yml probes /health", bool(health))
    check(
        "health probe uses the blackbox module",
        health.get("metrics_path") == "/probe"
        and health.get("params", {}).get("module") == ["http_2xx"]
        and "blackbox:9115" in json.dumps(health.get("relabel_configs", [])),
    )

    alerts = load_yaml(PROMETHEUS / "alerts.yml")
    if alerts is None:
        return
    rules = [rule for group in alerts.get("groups", []) for rule in group.get("rules", [])]
    names = {rule.get("alert") for rule in rules}
    missing = REQUIRED_ALERTS - names
    check("alerts.yml has the required alerts", not missing, ", ".join(sorted(missing)))
    check(
        "every alert has expr, severity and summary",
        all(
            rule.get("expr")
            and rule.get("labels", {}).get("severity")
            and rule.get("annotations", {}).get("summary")
            for rule in rules
        ),
    )
    compare_metrics("alerts.yml", all_exprs(alerts), known)

    blackbox = load_yaml(MONITORING / "blackbox" / "blackbox.yml")
    if blackbox is not None:
        module = blackbox.get("modules", {}).get("http_2xx", {})
        check(
            "blackbox.yml defines http_2xx",
            module.get("prober") == "http" and "timeout" in module,
        )

    if shutil.which("promtool"):
        proc = subprocess.run(
            ["promtool", "check", "rules", str(PROMETHEUS / "alerts.yml")],
            capture_output=True,
            text=True,
            check=False,
        )
        check("promtool check rules", proc.returncode == 0, proc.stderr.strip()[:200])
    else:
        print("SKIP  promtool not installed; structural rule checks only")


def check_grafana(known: Set[str]) -> None:
    datasource = load_yaml(GRAFANA / "provisioning" / "datasources" / "prometheus.yml")
    if datasource is not None:
        entries = datasource.get("datasources") or []
        uid = entries[0].get("uid") if entries else None
        check(
            "Grafana datasource points at Prometheus",
            bool(entries)
            and entries[0].get("type") == "prometheus"
            and entries[0].get("url") == "http://prometheus:9090",
        )
    else:
        uid = None

    provider = load_yaml(GRAFANA / "provisioning" / "dashboards" / "agent-rag.yml")
    if provider is not None:
        options = (provider.get("providers") or [{}])[0].get("options", {})
        check(
            "Grafana dashboard provider points at the mounted directory",
            options.get("path") == "/var/lib/grafana/dashboards",
        )

    dashboard = load_json(GRAFANA / "dashboards" / "agent-rag.json")
    if dashboard is None:
        return
    check("Grafana dashboard parses", True)
    panels = dashboard.get("panels", [])
    check("dashboard has a meaningful number of panels", len(panels) >= 8, str(len(panels)))
    check("dashboard has a stable uid", bool(dashboard.get("uid")))
    panel_ids = [panel.get("id") for panel in panels]
    check("dashboard panel ids are unique", len(panel_ids) == len(set(panel_ids)))
    check(
        "dashboard panels use the provisioned datasource",
        all(
            panel.get("datasource", {}).get("uid") == uid
            for panel in panels
            if isinstance(panel.get("datasource"), dict)
        ),
    )
    compare_metrics("dashboard", all_exprs(dashboard), known)


def check_compose() -> None:
    path = ROOT / "docker-compose.monitoring.yml"
    compose = load_yaml(path)
    if compose is None:
        return
    services = compose.get("services", {})
    check("monitoring compose has prometheus/blackbox/grafana", {"prometheus", "blackbox", "grafana"} <= set(services))
    prod_network = compose.get("networks", {}).get("prod", {})
    check(
        "monitoring compose joins the external prod network",
        prod_network.get("name") == "agent-rag-prod" and prod_network.get("external") is True,
    )
    text = path.read_text(encoding="utf-8")
    check(
        "monitoring compose mounts the versioned configs",
        all(
            marker in text
            for marker in (
                "./monitoring/prometheus:/etc/prometheus:ro",
                "./monitoring/blackbox:/etc/blackbox_exporter:ro",
                "./monitoring/grafana/provisioning:/etc/grafana/provisioning:ro",
                "./monitoring/grafana/dashboards:/var/lib/grafana/dashboards:ro",
            )
        ),
    )
    check(
        "monitoring UIs bind to loopback by default",
        "127.0.0.1:${PROMETHEUS_PORT" in text and "127.0.0.1:${GRAFANA_PORT" in text,
    )


def main() -> int:
    known = source_metric_names()
    check("src/metrics.py exports metrics", bool(known), str(len(known)))
    check_prometheus(known)
    check_grafana(known)
    check_compose()

    print()
    if FAIL:
        print(f"RESULT: FAILED ({len(FAIL)} failed, {len(PASS)} passed)")
        return 1
    print(f"RESULT: OK ({len(PASS)} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
