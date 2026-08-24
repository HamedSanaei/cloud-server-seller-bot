"""Observability: Prometheus metrics (M11-001) + OpenTelemetry tracing (M11-002)."""

from cloud_platform.observability.metrics import PlatformMetrics, metrics

__all__ = ["PlatformMetrics", "metrics"]
