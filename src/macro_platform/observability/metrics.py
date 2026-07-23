from prometheus_client import Counter, Gauge, Histogram

PROVIDER_REQUESTS = Counter(
    "provider_request_total",
    "External provider requests",
    ("provider_role", "dataset", "status"),
)
PROVIDER_DURATION = Histogram(
    "provider_request_duration_seconds",
    "External provider request duration",
    ("provider_role", "dataset"),
)
PROVIDER_LAST_SUCCESS = Gauge(
    "provider_last_success_timestamp_seconds",
    "Unix timestamp of the last successful provider request",
    ("provider_role", "dataset", "region"),
)
CONTEXT_BUILD_DURATION = Histogram(
    "context_build_duration_seconds",
    "Editor context build duration",
    ("preset_id",),
)
