"""Métricas Prometheus: recommendations_generated/approved/rejected, ahorro estimado/realizado, latencia LLM, errores de APIs cloud."""
from prometheus_client import Counter, Histogram

RECOMMENDATIONS_GENERATED = Counter("recommendations_generated_total", "Recomendaciones nuevas generadas", ["rule_id"])
RECOMMENDATIONS_APPROVED = Counter("recommendations_approved_total", "Recomendaciones aprobadas (aprobación final)")
RECOMMENDATIONS_REJECTED = Counter("recommendations_rejected_total", "Recomendaciones rechazadas")
ESTIMATED_SAVINGS_USD = Counter("estimated_savings_usd_total", "Ahorro mensual estimado acumulado de recomendaciones nuevas")
REALIZED_SAVINGS_USD = Counter("realized_savings_usd_total", "Ahorro mensual observado acumulado (verificado)")
LLM_LATENCY = Histogram("llm_latency_seconds", "Latencia de llamadas al asesor LLM",
                        buckets=(0.5, 1, 2, 5, 10, 20, 30, 60))
CLOUD_API_ERRORS = Counter("cloud_api_errors_total", "Errores al invocar APIs de proveedores cloud", ["provider", "api"])
SCANS_TOTAL = Counter("scans_total", "Escaneos finalizados", ["status"])

# --- tráfico HTTP y autenticación (alimentan infrastructure/docker/alerts.yml)
HTTP_REQUESTS = Counter("http_requests_total", "Peticiones HTTP atendidas por la API", ["method", "route", "status"])
HTTP_LATENCY = Histogram("http_request_duration_seconds", "Latencia de las peticiones HTTP", ["method", "route"],
                         buckets=(0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30))
AUTH_FAILURES = Counter("auth_failures_total", "Errores de autenticación por código estable (invalid_credentials, locked, rate_limited, ...)", ["code"])
AUTH_LOGINS = Counter("auth_logins_total", "Inicios de sesión que pasaron la contraseña", ["status"])        # ok | mfa_required
