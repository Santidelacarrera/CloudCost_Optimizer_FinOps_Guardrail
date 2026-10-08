# Costos reales y reportes ejecutivos

## De dónde sale el costo de cada recurso

El colector de AWS ya no se queda con una tabla de precios. Para cada escaneo pide a **Cost Explorer** (boto3 `ce`) el costo facturado
y lo asigna al recurso en este orden:

| Fuente (`cost_source`) | Cómo se obtiene | Precisión |
|---|---|---|
| `cost_explorer` | `GetCostAndUsageWithResources` agrupado por `RESOURCE_ID` (acepta ID o ARN) | Costo diario real del recurso, últimos 14 días |
| `cost_explorer_tag` | `GetCostAndUsage` agrupado por la etiqueta de asignación de costos de la cuenta; si varios recursos la comparten, se reparte según su costo estimado | Aproximada: puede incluir otros servicios con la misma etiqueta |
| `estimate` | Tabla de precios (`domain/pricing.py`) | Solo orientativa |

Requisitos en la cuenta de pagos de AWS:

1. **Cost Explorer** habilitado y, para la fuente por recurso, *Preferencias → Datos a nivel de recurso* activado (AWS lo limita a 14 días).
2. Para la fuente por etiqueta: activar la etiqueta en *Billing → Cost allocation tags* y registrarla al crear la cuenta
   (`cost_allocation_tag`, p. ej. `Project`).
3. Permisos `ce:GetCostAndUsage` y `ce:GetCostAndUsageWithResources` en el rol de solo lectura
   (`infrastructure/terraform/aws-readonly-role`, variable `enable_cost_explorer`, activada por defecto).

Cada llamada a Cost Explorer cuesta USD 0.01. Un escaneo hace pocas llamadas paginadas (no una por recurso).
Si Cost Explorer falla o no tiene datos, el escaneo **no** se marca como parcial: el recurso conserva su estimación y el escaneo deja
una advertencia con el motivo.

## «Sin costo real no se propone apagar»

Cada propuesta lleva `evidence.cost_basis` (`source`, `verified`, `monthly_cost`, `window_days`), que la pantalla de aprobación muestra
a quien decide. Con `REQUIRE_REAL_COST=true` (o `require_real_cost` en la política `rule_config` de una organización) se omiten las
propuestas cuyo costo es solo una estimación, y el escaneo informa cuántas dejó fuera y por qué.

| Variable | Defecto | Efecto |
|---|---|---|
| `AWS_COST_EXPLORER_RESOURCES` | `true` | Usa Cost Explorer; `false` vuelve a la estimación |
| `REQUIRE_REAL_COST` | `false` | `true` exige costo facturado para proponer cambios |

## Reportes para la gerencia

`GET /api/v1/reports/waste?format=pdf|xlsx` (cualquier rol con lectura) genera el reporte con las oportunidades abiertas en ese momento.
En la interfaz: **Panel → Reporte PDF / Reporte Excel**. Cada descarga queda en la auditoría (`REPORT_EXPORTED`).

- **PDF** (2–3 páginas): resumen en una frase, cuatro cifras clave, gráfico por categoría, mayores oportunidades con su origen del costo,
  desglose por entorno y notas de lectura.
- **Excel**: `Resumen`, `Oportunidades`, `Por categoría`, `Gasto diario`, `Ahorro verificado`, `Notas`. Los totales son **fórmulas** sobre
  la hoja `Oportunidades`: si la gerencia quita una fila, todo se recalcula.

El PDF y el Excel se generan desde los mismos datos, así que sus cifras coinciden.

## Demo

El escenario sintético cuenta una historia reconocible (20 recursos, 15 hallazgos, ≈ USD 2.384/mes): un clúster Aurora que quedó
encendido tras una migración, una base de desarrollo sin uso, cuatro volúmenes huérfanos de un despliegue blue/green fallido
(CloudFormation) y dos volúmenes de Kubernetes cuyo namespace se borró. La base `billing-prod` está en uso y no se toca.
Las bases de datos exigen aprobación reforzada, y un `aws_db_instance` sin `final_snapshot_identifier` no genera parche.
