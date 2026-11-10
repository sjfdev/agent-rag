# Monitoring stack

Prometheus + blackbox_exporter + Grafana for the metrics exposed by
`GET /metrics` and the readiness probe `GET /health`.

```
app :8000/metrics  ─┐
                    ├─ prometheus :9090 ── grafana :3000
app :8000/health ── blackbox :9115 ─┘
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

| UI | URL | Notes |
|---|---|---|
| Grafana | http://127.0.0.1:3000 | folder **Agent RAG**, dashboard **Agent RAG**; default `admin` / `admin`, set `GRAFANA_ADMIN_PASSWORD` in `.env` |
| Prometheus | http://127.0.0.1:9090 | **Alerts** tab lists the `agent-rag-*` rules |

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

Rules live in `prometheus/alerts.yml`. Prometheus itself does not send
notifications: add a `alertmanager` service and an `alerting:` block when you
want email/Slack/Webhook delivery.

`AgentRagHealthDown` uses blackbox_exporter because `/health` returns 503 only
when the index is empty or unreachable; `degraded` (ingestion/rebuild or low
disk) stays HTTP 200. Watch the `storage` block in `/health` and
`agent_rag_maintenance` for those cases.

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
  prometheus/prometheus.yml          scrape jobs + rule_files
  prometheus/alerts.yml              alert rules (validated by scripts/check_monitoring.py)
  blackbox/blackbox.yml              http_2xx probe module
  grafana/provisioning/...           datasource + dashboard provider
  grafana/dashboards/agent-rag.json  dashboard (request/error/latency/cache/index)
```
