# Guardrail de presupuesto por tenant

Alerta temprana y bloqueo preventivo cuando una tarea (o el mes) supera los límites de coste de la organización.

## Qué mide

| Métrica | Qué cuenta | De dónde sale |
|---|---|---|
| `api_usage` | Gasto **variable que genera la propia plataforma**: solicitudes a Cost Explorer (USD 0,01 c/u) y cualquier consulta/tarea que un servicio del ecosistema registre | Tabla `usage_meter` (solo-añadir) |
| `cloud_spend` | Gasto **observado** de la cuenta en el mes (USD) | `account_costs` (Cost Explorer / importación) |

## Política (`PUT /api/v1/budgets`, solo ADMIN)

`monthly_limit_usd`, `per_task_limit_usd` (opcional), `warn_ratio` (por defecto 0,8), `mode` y `enabled`. Un límite por métrica y organización.

| Modo | Si la tarea se pasa del límite o del tope por tarea |
|---|---|
| `alert` | La tarea sigue; se registra una alerta |
| `degrade` | La tarea sigue **sin gasto variable** (el escaneo omite Cost Explorer y usa la tabla de precios, avisándolo) |
| `block` | La tarea **no arranca** (`BudgetExceeded`, sin reintentos); no se crea el colector ni se gasta nada |

Niveles de alerta: `warn` (≥ `warn_ratio` del límite), `forecast` (a ritmo lineal el mes cierra por encima; solo desde el día 3), `exceeded`, `blocked`, `degraded`. Una alerta por nivel y mes (sin spam), siempre con evento en la cadena de auditoría (`BUDGET_ALERT`).

## Dónde se aplica

* **Escaneos**: `scan_service.run_scan` evalúa antes de crear el colector (coste máximo = `AWS_CE_REQUEST_BUDGET` × 0,01) y, después de recolectar, mide las solicitudes reales a Cost Explorer. Cuentas que no son AWS (Kubernetes, Qdrant, PostgreSQL, importación) cuestan 0 y nunca se bloquean.
* **Otros servicios**: `POST /budgets/check` (¿cabe esta tarea?) y `POST /budgets/usage` (registrar lo ejecutado). Los roles ADMIN/FINOPS/SRE pueden usarlos.
* `GET /budgets` (estado: gastado, restante, previsión) y `GET /budgets/alerts` para cualquier rol.

## Límites honestos

* Es un guardrail **de la plataforma**: no limita lo que gastan tus cargas en la nube; para eso usa AWS Budgets en la cuenta.
* `cloud_spend` depende de que haya costes recolectados; sin datos, el gasto es 0 y no alerta.
* El medidor se alimenta del que llama: un servicio que no registre su uso no queda contabilizado.
* Probado con PostgreSQL real local (RLS, solo-añadir, aislamiento entre organizaciones) y sin AWS real.
