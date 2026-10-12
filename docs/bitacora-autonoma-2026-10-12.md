# Bitácora autónoma — 2026-10-12 (cloudCost-red-teaming)

Proyecto de **Nexus Security & AI Lab**. Rama de trabajo: `feat/cloudcost-autonomo-2026-10-12` (base `main`). Alcance pedido: pruebas de los guardrails con datos simulados de Cost Explorer (sin llamadas reales a AWS), documentar umbrales y cálculo, y verificar que solo alertan y nunca borran.

## Bitácora de decisiones

| Fecha | Decisión | Motivo |
|---|---|---|
| 2026-10-12 | Escenarios de Cost Explorer en `tests/ce_scenarios.py` con el `Stubber` de botocore y el colector real de AWS | Las respuestas se validan contra el modelo oficial de la API; cero red y cero costo |
| 2026-10-12 | Probar los umbrales con importes de borde (0,59 / 0,60 / 0,63 / 0,64) | El comportamiento al centavo (comparación estricta `P > L`) queda fijado por pruebas y documentado |
| 2026-10-12 | Demostrar "nunca borra" con tres evidencias independientes: conteos antes/después, privilegios del rol de aplicación (sin DELETE/TRUNCATE) y prueba estática de los módulos | Una sola evidencia no excluye un camino alternativo |
| 2026-10-12 | `cloud_spend` sigue siendo informativo (no se añadió alerta automática tras cada escaneo) | Fuera del alcance pedido; queda como mejora en pendientes |
| 2026-10-12 | No cambiar dependencias web | `npm audit` sobre el `package-lock.json` versionado (desactualizado) marca `postcss`; resuelto como lo hace el CI (`--package-lock-only` con el `override` existente) da 0 vulnerabilidades |

## Definición de terminado — evidencia

Ver la descripción del PR (comandos y salidas). Archivos nuevos: `tests/ce_scenarios.py`, `tests/integration/test_budget_guardrail_cost_explorer.py`; documentación ampliada en `docs/budget-guardrails.md`.

## Pendientes para Santiago

* Aprobar y fusionar el PR (decisión humana).
* Aplicar las migraciones 010–013 en el entorno integrado (la 013 deja los límites sin definir: nadie está limitado hasta que un ADMIN cree uno).
* Regenerar y versionar `apps/web/package-lock.json` (está desincronizado con `package.json`; hace que `npm audit` local dé falsos positivos).
* Validar contra una cuenta AWS real (cuenta no activada) y contra un Qdrant real: todo lo de esta rama es simulado o local.
* Decidir si `cloud_spend` debe alertar automáticamente tras cada escaneo.

---
Volver a [[empresa]]
