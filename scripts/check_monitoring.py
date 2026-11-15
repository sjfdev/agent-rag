"""Validate the monitoring stack configuration offline.

Checks that Prometheus, blackbox_exporter and Grafana files agree with the
metrics the application actually exports, so a typo in a rule or dashboard is
caught before a deploy rather than after an incident:

  - `monitoring/prometheus/prometheus.yml` scrapes the app and /health
  - `monitoring/prometheus/alerts.yml` has the required alerts and only
    references `agent_rag_*` metrics that exist in `src/metrics.py`
  - the Grafana dashboard/provisioning files parse and target the same metrics
  - the rendered Alertmanager config is valid and every route resolves
  - `docker-compose.monitoring.yml` wires the files and the prod network

Run `promtool check rules` (and `amtool check-config`) too when those binaries
are installed. Exit code is non-zero on any failure; no network access is
needed. Rendering `monitoring/alertmanager/alertmanager.yml` is the only write:
it is gitignored and (re)generated from `.env` / the environment.

    python scripts/check_monitoring.py
"""

import importlib.util
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
ALERTMANAGER = MONITORING / "alertmanager"
ALERTMANAGER_CONFIG = ALERTMANAGER / "alertmanager.yml"
LOKI = MONITORING / "loki"
PROMTAIL = MONITORING / "promtail"
GITIGNORE = ROOT / ".gitignore"

REQUIRED_ALERTS = {
    "AgentRagMetricsDown",
    "AgentRagHealthDown",
    "AgentRagIndexEmpty",
    "AgentRagErrorRate",
    "AgentRagCacheHitDrop",
    "AgentRagMaintenanceStuck",
    "AgentRagBackupStale",
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

    alerting = config.get("alerting", {})
    alert_targets = [
        target
        for manager in alerting.get("alertmanagers", [])
        for static in manager.get("static_configs", [])
        for target in static.get("targets", [])
    ]
    check(
        "prometheus.yml sends alerts to Alertmanager",
        "alertmanager:9093" in alert_targets,
        ", ".join(alert_targets) if alert_targets else "no alerting.alertmanagers target",
    )

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


def grafana_datasources() -> Dict[str, Dict[str, Any]]:
    """Provisioned datasources keyed by uid, across every *.yml file."""
    entries: Dict[str, Dict[str, Any]] = {}
    folder = GRAFANA / "provisioning" / "datasources"
    for path in sorted(folder.glob("*.yml")):
        data = load_yaml(path)
        if data is None:
            continue
        for entry in data.get("datasources") or []:
            uid = entry.get("uid")
            if uid:
                entries[uid] = entry
    return entries


def check_grafana(known: Set[str]) -> None:
    declared = grafana_datasources()
    check("Grafana datasources parse", bool(declared))
    prometheus = next(
        (entry for entry in declared.values() if entry.get("type") == "prometheus"), {}
    )
    check(
        "Grafana datasource points at Prometheus",
        prometheus.get("url") == "http://prometheus:9090",
    )
    loki = next((entry for entry in declared.values() if entry.get("type") == "loki"), {})
    check(
        "Grafana datasource points at Loki",
        loki.get("url") == "http://loki:3100",
        loki.get("url", "no loki datasource"),
    )

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
    unknown_ds = {
        panel.get("datasource", {}).get("uid")
        for panel in panels
        if isinstance(panel.get("datasource"), dict)
    } - set(declared)
    check(
        "dashboard panels use provisioned datasources",
        not unknown_ds,
        ", ".join(sorted(filter(None, unknown_ds))) if unknown_ds else "",
    )
    log_panels = [panel for panel in panels if panel.get("type") == "logs"]
    check(
        "dashboard has a Loki log panel",
        bool(log_panels)
        and all(p.get("datasource", {}).get("uid") == "loki" for p in log_panels),
    )
    compare_metrics("dashboard", all_exprs(dashboard), known)


def route_receivers(route: Dict[str, Any]) -> Set[str]:
    """Receiver names referenced anywhere in a (nested) route tree."""
    names: Set[str] = set()
    receiver = route.get("receiver")
    if isinstance(receiver, str):
        names.add(receiver)
    for child in route.get("routes", []) or []:
        names |= route_receivers(child)
    return names


def check_alertmanager() -> None:
    """Render the gitignored config from the environment, then validate it."""
    renderer_path = Path(__file__).resolve().parent / "render_alertmanager.py"
    spec = importlib.util.spec_from_file_location("render_alertmanager", renderer_path)
    renderer = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(renderer)
    try:
        renderer.render(dest=ALERTMANAGER_CONFIG)
    except ValueError as exc:
        check("Alertmanager config renders", False, str(exc)[:200])
        return
    check("Alertmanager config renders", True)

    config = load_yaml(ALERTMANAGER_CONFIG)
    if config is None:
        return
    declared = {
        receiver.get("name") for receiver in config.get("receivers", [])
    }
    referenced = route_receivers(config.get("route", {}))
    check(
        "alertmanager.yml routes only to defined receivers",
        referenced <= declared,
        ", ".join(sorted(referenced - declared)) if referenced - declared else "",
    )
    check(
        "alertmanager.yml has a default receiver",
        bool(config.get("route", {}).get("receiver")) and "noop" in declared,
    )

    gitignore = GITIGNORE.read_text(encoding="utf-8") if GITIGNORE.is_file() else ""
    check(
        "rendered Alertmanager config is gitignored",
        "monitoring/alertmanager/alertmanager.yml" in gitignore,
    )

    if shutil.which("amtool"):
        proc = subprocess.run(
            ["amtool", "check-config", str(ALERTMANAGER_CONFIG)],
            capture_output=True,
            text=True,
            check=False,
        )
        check("amtool check-config", proc.returncode == 0, proc.stderr.strip()[:200])
    else:
        print("SKIP  amtool not installed; structural Alertmanager checks only")


def check_logs() -> None:
    """Loki store + Promtail shipping configuration."""
    loki = load_yaml(LOKI / "loki.yml")
    if loki is not None:
        check("loki.yml disables auth (single-node)", loki.get("auth_enabled") is False)
        check(
            "loki.yml stores to the filesystem",
            (loki.get("common", {}).get("storage", {}).get("filesystem") or {}).get(
                "chunks_directory"
            )
            == "/loki/chunks",
        )
        configs = loki.get("schema_config", {}).get("configs") or [{}]
        check(
            "loki.yml uses the tsdb schema",
            configs[0].get("store") == "tsdb"
            and configs[0].get("object_store") == "filesystem",
        )
        retention = (loki.get("limits_config") or {}).get("retention_period", "")
        check(
            "loki.yml retention is environment-tunable",
            "${LOKI_RETENTION" in str(retention),
            str(retention),
        )
        check(
            "loki.yml compactor deletes expired chunks",
            (loki.get("compactor") or {}).get("retention_enabled") is True,
        )

    promtail = load_yaml(PROMTAIL / "promtail.yml")
    if promtail is None:
        return
    clients = promtail.get("clients") or [{}]
    check(
        "promtail.yml pushes to Loki",
        clients[0].get("url") == "http://loki:3100/loki/api/v1/push",
        clients[0].get("url", ""),
    )
    scrape = (promtail.get("scrape_configs") or [{}])[0]
    discovery = (scrape.get("docker_sd_configs") or [{}])[0]
    check(
        "promtail.yml discovers containers via the Docker socket",
        discovery.get("host") == "unix:///var/run/docker.sock",
    )
    relabels = scrape.get("relabel_configs") or []
    check(
        "promtail.yml keeps only agent-rag containers",
        any(item.get("regex") == "/agent-rag-.*" and item.get("action") == "keep" for item in relabels),
    )
    stages = scrape.get("pipeline_stages") or []
    check(
        "promtail.yml unwraps the docker json-file envelope",
        any("docker" in stage for stage in stages),
    )
    check(
        "promtail.yml persists read positions",
        str(promtail.get("positions", {}).get("filename", "")).startswith("/var/lib/promtail/"),
    )


def check_compose() -> None:
    path = ROOT / "docker-compose.monitoring.yml"
    compose = load_yaml(path)
    if compose is None:
        return
    services = compose.get("services", {})
    check(
        "monitoring compose has prometheus/alertmanager/blackbox/grafana/loki/promtail",
        {"prometheus", "alertmanager", "blackbox", "grafana", "loki", "promtail"}
        <= set(services),
    )
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
                "./monitoring/alertmanager:/etc/alertmanager:ro",
                "./monitoring/loki:/etc/loki:ro",
                "./monitoring/promtail:/etc/promtail:ro",
                "./monitoring/blackbox:/etc/blackbox_exporter:ro",
                "./monitoring/grafana/provisioning:/etc/grafana/provisioning:ro",
                "./monitoring/grafana/dashboards:/var/lib/grafana/dashboards:ro",
            )
        ),
    )
    check(
        "promtail mounts the Docker socket and container logs",
        "${DOCKER_SOCKET:-/var/run/docker.sock}:/var/run/docker.sock:ro" in text
        and "${DOCKER_CONTAINERS_DIR:-/var/lib/docker/containers}:/var/lib/docker/containers:ro"
        in text,
    )
    check(
        "monitoring UIs bind to loopback by default",
        "127.0.0.1:${PROMETHEUS_PORT" in text
        and "127.0.0.1:${GRAFANA_PORT" in text
        and "127.0.0.1:${ALERTMANAGER_PORT" in text
        and "127.0.0.1:${LOKI_PORT" in text,
    )


def main() -> int:
    known = source_metric_names()
    check("src/metrics.py exports metrics", bool(known), str(len(known)))
    check_prometheus(known)
    check_alertmanager()
    check_logs()
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
