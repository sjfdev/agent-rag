# Monitoring stack

Prometheus + Alertmanager + blackbox_exporter + Grafana for the metrics exposed
by `GET /metrics` and the readiness probe `GET /health`.

```
app :8000/metrics  ─┐
                    ├─ prometheus :9090 ── alertmanager :9093 ── email / slack / webhook
app :8000/health ── blackbox :9115 ─┘        └──── grafana :3000 ── loki :3100
container stdout ── promtail ────────────────────────────────────▲
```

Everything is versioned here; only metric data and Grafana state live in the
gitignored `volumes-monitoring/` directory.

## Start

```bash
# 1. Application stack (creates the agent-rag-prod network)
docker compose -f docker-compose.prod.yml up -d

# 2. Monitoring overlay (joins agent-rag-prod)
make monitor-up

# 3. Validate the config files offline at any time
make monitor-check
```

`make monitor-up` first runs `make monitor-config`, which renders the
gitignored `alertmanager/alertmanager.yml` from `.env` (SMTP/Slack secrets must
not be committed). Re-run it after changing any `ALERTMANAGER_*` variable.

| UI | URL | Notes |
|---|---|---|
| Grafana | http://127.0.0.1:3000 | folder **Agent RAG**, dashboard **Agent RAG**; default `admin` / `admin`, set `GRAFANA_ADMIN_PASSWORD` in `.env` |
| Prometheus | http://127.0.0.1:9090 | **Alerts** tab lists the `agent-rag-*` rules |
| Alertmanager | http://127.0.0.1:9093 | silences and notification status |
| Loki | http://127.0.0.1:3100 | log store; queried through Grafana (`Explore` → Loki) |

Both ports bind to `127.0.0.1`. Put a TLS reverse proxy in front for remote
access; do not publish 3000/9090 directly.

## Alerts

| Alert | Condition | Severity |
|---|---|---|
| `AgentRagMetricsDown` | `up{job="agent-rag"} == 0` for 2m | critical |
| `AgentRagHealthDown` | `/health` non-2xx for 45s (3 scrapes) | critical |
| `AgentRagIndexEmpty` | `agent_rag_index_chunks == 0` for 5m | warning |
| `AgentRagErrorRate` | chat error ratio > 5% for 5m | critical |
| `AgentRagCacheHitDrop` | 10m cache-hit ratio < half the 1h baseline for 15m | warning |
| `AgentRagMaintenanceStuck` | rebuild flag held for 1h | warning |

Rules live in `prometheus/alerts.yml`; Prometheus forwards firing alerts to
Alertmanager (`alerting:` block in `prometheus.yml`). Delivery is opt-in and
configured entirely through `.env`:

| Channel | Variables |
|---|---|
| Email | `ALERTMANAGER_EMAIL_TO`, `ALERTMANAGER_SMTP_SMARTHOST`, `ALERTMANAGER_SMTP_FROM`, optional `ALERTMANAGER_SMTP_AUTH_USERNAME` + `ALERTMANAGER_SMTP_REQUIRE_TLS`; the password goes in the gitignored `alertmanager/smtp_password` (or `ALERTMANAGER_SMTP_AUTH_PASSWORD`) |
| Slack | `ALERTMANAGER_SLACK_WEBHOOK_URL`, optional `ALERTMANAGER_SLACK_CHANNEL` |
| Webhook | `ALERTMANAGER_WEBHOOK_URL` (Alertmanager's JSON payload, `send_resolved: true`) |

All enabled channels receive every `critical`/`warning` alert; `info` is
ignored. Grouping defaults to `alertname` + `severity` (`group_wait` 30s,
`group_interval` 5m, `repeat_interval` 4h, all tunable). With no channel set
the stack still starts and routes to a `noop` receiver.

`AgentRagMetricsDown` inhibits the alerts derived from the same app metrics
(error rate, cache drop, stuck rebuild): a failed scrape already says the
instance is unreachable. `AgentRagHealthDown` is not inhibited because it
probes readiness through blackbox and can fail on its own.

`AgentRagHealthDown` uses blackbox_exporter because `/health` returns 503 only
when the index is empty or unreachable; `degraded` (ingestion/rebuild or low
disk) stays HTTP 200. Watch the `storage` block in `/health` and
`agent_rag_maintenance` for those cases.

## Logs

Promtail discovers containers through the Docker socket, keeps only
`agent-rag-*` containers, unwraps the json-file envelope and pushes to Loki;
the dashboard's **Application logs (Loki)** panel queries `{service="app"}`.
`request_id` is in the app's own log lines (and the `X-Request-ID` response
header), so a failing request can be traced from its id:

```logql
{service="app"} |= "<request_id>"
{container="agent-rag-app"} |= "ERROR"
{container=~"agent-rag-(app|milvus)"} |~ "Exception|Traceback"
```

`LOKI_RETENTION` (default `720h`) is enforced by Loki's compactor; set it in
`.env` and `make monitor-up` recreates Loki with the new value. Log data lives
in `volumes-monitoring/loki/` (gitignored).

Docker socket assumptions (see `DOCKER_SOCKET` / `DOCKER_CONTAINERS_DIR` in
`.env.example`):

- **Linux host**: defaults work (`/var/run/docker.sock`,
  `/var/lib/docker/containers`).
- **Docker Desktop / colima**: the daemon lives in a VM, so `/var/run/docker.sock`
  is resolved inside that VM and normally works. If Promtail exits with
  "permission denied" on the socket, point `DOCKER_SOCKET` at the path the
  compose project can actually mount and restart it.
- **Rootless Docker**: set `DOCKER_SOCKET=$XDG_RUNTIME_DIR/docker.sock` and
  `DOCKER_CONTAINERS_DIR` to the rootless log directory, then restart Promtail.

If the socket or log directory cannot be mounted at all, disable `promtail`
(`docker compose -f docker-compose.monitoring.yml up -d prometheus alertmanager
blackbox grafana loki`): metrics, alerts and the dashboard still work, only
log queries are empty.

## Protected metrics

If `METRICS_TOKEN` is set in `.env`, write the same value to a gitignored file
and uncomment the `authorization` block in `prometheus/prometheus.yml`:

```bash
printf '%s' "$METRICS_TOKEN" > monitoring/prometheus/metrics_token
docker compose -f docker-compose.monitoring.yml up -d --force-recreate prometheus
```

## Custom targets

`prometheus.yml` assumes the application is reachable as `app:8000` on the
`agent-rag-prod` Docker network. For another topology, edit the two targets in
`prometheus.yml` (metrics and the `/health` URL) and mount the file into
Prometheus.

## Files

```
monitoring/
  prometheus/prometheus.yml          scrape jobs + rule_files + alerting target
  prometheus/alerts.yml              alert rules (validated by scripts/check_monitoring.py)
  alertmanager/alertmanager.yml      rendered from .env (gitignored); smtp_password also gitignored
  blackbox/blackbox.yml              http_2xx probe module
  loki/loki.yml                      single-node log store (tsdb, retention via LOKI_RETENTION)
  promtail/promtail.yml              Docker log shipping for agent-rag-* containers
  grafana/provisioning/...           Prometheus + Loki datasources, dashboard provider
  grafana/dashboards/agent-rag.json  dashboard (request/error/latency/cache/index + logs)
```
