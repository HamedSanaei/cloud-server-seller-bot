# Grafana dashboards (M11-005)

Golden-signals dashboard for the cloud platform, built from the M11-001
Prometheus metric families (`cloud_platform_*`):

- **Latency** - API request p50/p95/p99, provider call p95, job duration p95
- **Traffic** - API request rate by status, provider call rate, job run rate,
  billing event rate
- **Errors** - API 5xx, provider errors by outcome, job errors, provisioning
  failures by stage, reconciliation outcomes
- **Saturation** - oldest pending operation age, daily provider cost,
  provider 429 rate

Files:

- `dashboards/cloud-platform-golden-signals.json` - the dashboard
  (importable; uses a `${DS_PROMETHEUS}` datasource input).
- `provisioning/datasources/prometheus.yaml` - datasource auto-provisioning.
- `provisioning/dashboards/default.yaml` - dashboard auto-provisioning.

## Running Grafana against Prometheus

The platform exposes `/metrics` (api container port 8000; the worker does
not expose HTTP - job metrics ride in the api process's registry and the
worker is instrumented for tracing/OTel). Prometheus must scrape it:

```yaml
# prometheus.yml (excerpt)
scrape_configs:
  - job_name: cloud-platform
    metrics_path: /metrics
    static_configs:
      - targets: ["api:8000"]
```

Then Grafana with the provisioning volumes:

```sh
docker run -d --name grafana -p 3000:3000 \
  -v /path/to/deploy/grafana/provisioning/datasources:/etc/grafana/provisioning/datasources:ro \
  -v /path/to/deploy/grafana/provisioning/dashboards:/etc/grafana/provisioning/dashboards:ro \
  -v /path/to/deploy/grafana/dashboards:/var/lib/grafana/dashboards:ro \
  grafana/grafana:latest
```

Adjust the datasource `url` in
`provisioning/datasources/prometheus.yaml` to wherever Prometheus lives.

## Keeping it honest

`tests/unit/test_grafana_dashboards.py` parses the dashboard JSON and
asserts that every metric referenced by a panel target is a metric family
actually emitted by `cloud_platform/observability/metrics.py` (extracted
by AST) and that all four golden signals have at least one panel. A
renamed/removed metric therefore breaks the gate instead of silently
producing an empty panel.