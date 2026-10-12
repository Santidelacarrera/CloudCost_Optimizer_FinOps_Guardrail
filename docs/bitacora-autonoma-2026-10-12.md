# Bitácora autónoma — 2026-10-12 (cloudCost-red-teaming)

Proyecto de **Nexus Security & AI Lab**. Rama de trabajo: `feat/cloudcost-autonomo-2026-10-12` (base `main`). Alcance pedido: pruebas de los guardrails con datos simulados de Cost Explorer (sin llamadas reales a AWS), documentar umbrales y cálculo, y verificar que solo alertan y nunca borran.

## Bitácora de decisiones

| Fecha | Decisión | Motivo |
|---|---|---|
| 2026-10-12 | Escenarios de Cost Explorer en `tests/ce_scenarios.py` con el `Stubber` de botocore y el colector real de AWS | Las respuestas se validan contra el modelo oficial de la API; cero red y cero costo |
| 2026-10-12 | Probar los umbrales con importes de borde (0,59 / 0,60 / 0,63 / 0,64) | El comportamiento al centavo (comparación estricta `P > L`) queda fijado por pruebas y documentado |
| 2026-10-12 | Demostrar "nunca borra" con tres evidencias independientes: conteos antes/después, privilegios del rol de aplicación (sin DELETE/TRUNCATE) y prueba estática de los módulos | Una sola evidencia no excluye un camino alternativo |
| 2026-10-12 | `cloud_spend` alerta automáticamente tras cada escaneo (nunca bloquea) | Pedido de terminar el proyecto completo; el gasto ya ocurrió, así que solo cabe avisar |
| 2026-10-12 | No cambiar dependencias web | `npm audit` sobre el `package-lock.json` versionado (desactualizado) marca `postcss`; resuelto como lo hace el CI (`--package-lock-only` con el `override` existente) da 0 vulnerabilidades |
| 2026-10-12 | Triaje de los 15 hallazgos Medium de bandit: falsos positivos | `B608` interpola constantes del módulo (p. ej. `_UNDECIDED`) o columnas de listas cerradas, nunca entrada del usuario; `B310` es una URL https fija (HIBP, k-anonimato); `B108` es el directorio de la demo |
| 2026-10-12 | `apps/web/package-lock.json` regenerado (`npm install --package-lock-only`) | Estaba desincronizado; `npm audit` queda en 0 vulnerabilidades y `tsc --noEmit` pasa |

## Definición de terminado — evidencia

Ver la descripción del PR (comandos y salidas). Archivos nuevos: `tests/ce_scenarios.py`, `tests/integration/test_budget_guardrail_cost_explorer.py`; documentación ampliada en `docs/budget-guardrails.md`.

## Pendientes para Santiago

* Aprobar y fusionar el PR (decisión humana).
* Aplicar las migraciones 010–013 en el entorno integrado (la 013 deja los límites sin definir: nadie está limitado hasta que un ADMIN cree uno).
* Validar contra una cuenta AWS real (cuenta no activada) y contra un Qdrant real: todo lo de esta rama es simulado o local.

---
Volver a [[empresa]]
