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
