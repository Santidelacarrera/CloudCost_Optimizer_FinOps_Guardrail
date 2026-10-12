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

## Umbrales y cálculo (exactos)

Todo lo calcula `domain/budgets.py::evaluate` (función pura, sin red ni base de datos). Con `L` = límite mensual, `G` = gasto medido del mes, `T` = coste estimado de la tarea y `P = G + T`:

| Nivel | Condición | Con `mode=alert` | Con `degrade` / `block` |
|---|---|---|---|
| `exceeded` | `P > L` **o** `T > per_task_limit_usd` | La tarea sigue + alerta | `degrade`: sigue sin gasto variable · `block`: no arranca |
| `warn` | `P ≥ warn_ratio · L` (por defecto 0,80) y no `exceeded` | Alerta | Alerta (la tarea sigue) |
| `forecast` | `G / día_del_mes · días_del_mes > L` (solo desde el día 3) y no `warn`/`exceeded` | Alerta | Alerta (la tarea sigue) |
| `ok` | ninguna de las anteriores | — | — |

Reglas finas: la comparación es **estricta** (`P == L` se permite; un centavo más se supera); con `L = 0` solo se admiten tareas gratuitas; los importes se redondean a 2 decimales (4 en el coste de tarea) con redondeo "mitad hacia arriba".

**Qué cuesta un escaneo.** Estimación previa (peor caso): `T = AWS_CE_REQUEST_BUDGET × USD 0,01` (por defecto 60 → **USD 0,60**; 0 para cuentas que no son AWS o con Cost Explorer desactivado). Gasto real medido después: `solicitudes_realizadas × 0,01` (el escenario simulado de pruebas hace 4 → USD 0,04). Lo medido se acumula en `usage_meter` por mes UTC.

**Ejemplo con los números de las pruebas** (`tests/integration/test_budget_guardrail_cost_explorer.py`):

| Situación | Cálculo | Resultado |
|---|---|---|
| Primer escaneo, `L = 100` | `P = 0 + 0,60` = 0,6 % de `L` | `ok`, sin alerta |
| Primer escaneo, `L = 0,70` | `0,60 ≥ 0,8 · 0,70 = 0,56` | `warn` |
| Primer escaneo, `L = 0,60` | `0,60 > 0,60` es falso | permitido (`warn`) |
| Primer escaneo, `L = 0,59`, `block` | `0,60 > 0,59` | bloqueado, **0 llamadas a AWS** |
| Segundo escaneo (ya gastó 0,04), `L = 0,63`, `block` | `0,04 + 0,60 = 0,64 > 0,63` | bloqueado |
| Ídem con `L = 0,64` | `0,64 > 0,64` es falso | permitido |
| `cloud_spend`, gasto diario simulado 16,5, día 8, 7 días medidos | `G = 7 × 16,5 = 115,5`; previsión `115,5 / 8 × 31 = 447,56` | con `L = 200`: `forecast` |

## Solo alerta, degrada o impide arrancar: nunca borra

Lo que hace cada modo está acotado por diseño y por pruebas:

* **Nunca borra ni desactiva datos.** Un escaneo bloqueado no se ejecuta: no toca recursos, recomendaciones ni costes (prueba `test_block_and_degrade_leave_existing_data_untouched`). El modo `alert` no recorta la tarea (`test_alert_mode_thresholds_for_the_first_scan`).
* **Nunca escribe en la nube.** El guardrail no importa clientes de nube ni ejecuta procesos (prueba estática) y el colector sigue limitado a la lista cerrada de operaciones de lectura (`aws_guard`); las pruebas comprueban que cada llamada simulada pertenece a ella.
* **Los datos del guardrail son solo-añadir.** El rol de aplicación no tiene `DELETE` ni `TRUNCATE` sobre `tenant_budgets`, `usage_meter` ni `budget_alerts`, y no puede `UPDATE` el medidor ni las alertas (`test_application_role_cannot_delete_or_truncate_budget_data`).
* **`cloud_spend` solo alerta.** Tras cada escaneo se evalúa el gasto observado del mes y, si corresponde, se registra la alerta (`stats.cloud_spend_budget` lo refleja); también se consulta con `GET /budgets` y `POST /budgets/check`. El gasto de la cuenta ya ocurrió, así que aunque el modo sea `block` el escaneo termina y no se toca nada (prueba `test_scan_raises_a_cloud_spend_alert_but_never_blocks_or_changes_data`).
* **Las recomendaciones destructivas** (p. ej. `DROP INDEX`) siguen siendo texto para revisión humana; el presupuesto no las ejecuta ni las aprueba.

## Cómo se prueba (sin llamadas reales a AWS)

```bash
make install
SEED_DEV=true DATABASE_ADMIN_URL=postgresql://postgres:pw@localhost:5432/cloudcost APP_DB_PASSWORD=pw2 sh scripts/migrate.sh
export DATABASE_URL=postgresql://cloudcost_app:pw2@localhost:5432/cloudcost DATABASE_ADMIN_URL=postgresql://postgres:pw@localhost:5432/cloudcost
python -m pytest tests/unit/test_budgets.py tests/integration/test_budgets_db.py tests/integration/test_budget_guardrail_cost_explorer.py -q
```

Los escenarios de Cost Explorer (`tests/ce_scenarios.py`) pasan por el `Stubber` de botocore: se validan contra el modelo oficial de la API, con datos inventados (cuenta `111122223333`). No sustituyen una prueba con una cuenta real.

*Nexus Security & AI Lab — componente reutilizable: el guardrail no depende de ningún cliente ni de otro proyecto.*

---
Volver a [[empresa]]
