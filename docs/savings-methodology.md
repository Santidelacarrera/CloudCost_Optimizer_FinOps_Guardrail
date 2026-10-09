# Metodología del ahorro: cómo se calcula, qué se guarda y cómo se mide

Este documento responde a tres preguntas sobre cualquier cifra de ahorro que muestre la plataforma: **¿de dónde sale?**, **¿qué evidencia la sostiene?** y
**¿se consiguió?** Es la versión humana de `domain/estimates.py` (fórmulas) y `domain/measurement.py` (medición): una prueba
(`test_documented_formulas_match_the_code`) falla si una fórmula cambia en el código y no aquí.

## 1. Tres cifras distintas que nunca se suman

| Cifra | Cuándo se fija | Qué es | Dónde queda |
|---|---|---|---|
| **Estimado** | En cada escaneo, mientras la recomendación está `PROPOSED` o `PENDING_APPROVAL` | Lo que costaría menos si el cambio se aplicara y todo lo demás siguiera igual | `recommendations.estimated_monthly_savings` + una versión de la evidencia |
| **Aprobado** | Al completarse las aprobaciones (`APPROVED`) | La cifra **congelada** que vieron las personas que aprobaron, ligada a la huella de su evidencia | `approved_monthly_savings`, `approved_baseline_cost`, `approved_at`, `approved_evidence_id`; no la modifican los escaneos posteriores |
| **Observado** | Al verificar, tras el despliegue (`VERIFIED`) | Lo que se midió con la facturación posterior al cambio | `savings_verifications` (con método, calidad de los datos, factores y limitaciones) |

Un panel o informe que sumara las tres mezclaría una proyección, una decisión y un resultado. El producto las muestra en columnas separadas (`savings_breakdown`
en `/dashboard/summary`, y el informe ejecutivo) y el observado se divide a su vez en **atribuible al cambio**, **no atribuible solo al cambio** (otros factores se
movieron) y **declarado o sin facturación** (cifra escrita a mano o calculada con la tabla de precios: no es una observación).

## 2. Fórmulas del ahorro estimado

Las reglas son deterministas y explicables; el LLM, si está activado, solo redacta. Cada hallazgo guarda en `evidence.estimate` la fórmula aplicada, sus entradas, el coste de
referencia y los supuestos (se muestran en el detalle de la recomendación y en `GET /recommendations/{id}/evidence`).

**Supuestos comunes a todas las fórmulas**
- Importes en USD por mes (30,4375 días); precios bajo demanda, sin Reserved Instances ni Savings Plans, créditos ni impuestos.
- El resto de la infraestructura y el uso se mantienen igual tras aplicar el cambio.

**Coste de referencia.** Es el coste mensual del recurso con el que se aplica la fórmula:

1. *Cost Explorer por recurso* (últimos 14 días, `GetCostAndUsageWithResources`): coste diario representativo × 30,4375. Si la serie tiene señales de calidad se usa un valor
   **prudente** que nunca infla el ahorro: ante un **pico aislado** (un día > 3× la mediana) la mediana; ante un **cambio de nivel** (los últimos 3 días difieren > 30 % del resto)
   el menor entre la media y el nivel reciente. Si hay **importes negativos** (créditos o reembolsos) el coste deja de contar como «verificado».
2. *Archivo importado*: el coste declarado en el archivo (verificado).
3. *Tabla de precios* (AWS us-east-1, Linux, bajo demanda): estimación marcada como tal («Costo ESTIMADO»). Con `require_verified_cost` no se propone nada apoyado solo en ella.

Un coste real que se aleje de la tabla de precios para el mismo tipo (cociente fuera de 0,3 – 3) se marca como **atípico**: no se bloquea, pero resta 0,10 de confianza y se
explica en los supuestos (licencias, transferencia, créditos o un pico).

### `ec2_downsize.v1` · ec2_downsize (ratio)

Reducir hasta 2 tamaños dentro de la misma familia si el uso proyectado en el tipo destino queda bajo los techos configurados.

```
ahorro_mensual = coste_mensual_actual × (1 − precio_hora(tipo_destino) / precio_hora(tipo_actual))
```

**Supuestos**
- El coste mensual actual es el observado (Cost Explorer, últimos 14 días) o, si no hay, el de la tabla de precios.
- El ahorro escala con la proporción de precios bajo demanda de ambos tipos (misma región y sistema operativo), no con el uso.
- La instancia seguirá funcionando el mismo número de horas.
- El uso proyectado (CPU, memoria, pico) se calcula multiplicando el uso observado por vCPU/GiB actuales entre los del destino.

**Límites**
- Si la instancia tiene Reserved Instances o Savings Plans el ahorro efectivo es menor (el compromiso se sigue pagando).
- No incluye EBS, transferencia ni licencias; el cambio puede requerir reinicio.

### `ec2_idle.v1` · ec2_idle (exact)

Eliminar una instancia en ejecución con CPU media y pico mínimos durante toda la ventana observada.

```
ahorro_mensual = coste_mensual_actual de la instancia
```

**Supuestos**
- Al eliminar la instancia deja de facturarse su cómputo; sus volúmenes EBS, IP elásticas y transferencia se evalúan aparte y no se suman.
- La instancia no sirve tráfico que las métricas de CPU no reflejen (p. ej. colas, tareas por lotes poco frecuentes).

**Límites**
- Con Reserved Instances o Savings Plans el compromiso se sigue pagando: el ahorro real puede ser cero.
- Es una acción destructiva: la estimación no mide el coste de reponerla si se equivocara el diagnóstico.

### `ebs_orphan.v1` · ebs_orphan (exact)

Eliminar un volumen sin adjuntar durante al menos el número de días del umbral.

```
ahorro_mensual = tamaño_GB × precio_GB_mes(tipo_de_volumen)  [o el coste real del volumen si lo hay]
```

**Supuestos**
- El volumen se elimina sin snapshot final; si se toma uno, el ahorro disminuye en el coste de ese snapshot.
- El tiempo sin adjuntar se toma del último DetachVolume en CloudTrail (90 días) o de la primera vez que el servicio lo vio huérfano.

**Límites**
- Un volumen ya sin adjuntar puede ser una pieza de recuperación o de un despliegue en curso.

### `snapshot_old.v1` · snapshot_old (upper_bound)

Eliminar un snapshot antiguo, sin imagen asociada y no gestionado por un servicio de copias.

```
ahorro_mensual ≤ tamaño_GB × precio_GB_mes(snapshot)
```

**Supuestos**
- Se calcula sobre el tamaño del volumen, no sobre los bloques realmente almacenados.

**Límites**
- Cota SUPERIOR: los snapshots son incrementales y comparten bloques con los siguientes; borrar uno suele liberar menos.

### `rds_idle.v1` · rds_idle (exact)

Eliminar una base de datos RDS sin conexiones y con CPU en reposo toda la ventana observada.

```
ahorro_mensual = (precio_hora(clase) × 730 + precio_GB_mes × almacenamiento_GB) × (2 si Multi-AZ)
```

**Supuestos**
- Precios de RDS MySQL/PostgreSQL bajo demanda; clases desconocidas se valoran solo por almacenamiento.
- No se suman copias de seguridad automáticas ni snapshots manuales.

**Límites**
- Una base «sin conexiones» puede ser un entorno dormido que se usa a fin de mes o en una recuperación.

### `k8s_overprovisioned.v1` · k8s_overprovisioned (reserved)

Bajar los requests de un contenedor hasta el p95 de uso × holgura (CPU) o el máximo × holgura (memoria).

```
ahorro_mensual = (Δcpu_cores × precio_vCPU_hora + Δmemoria_GiB × precio_GiB_hora) × 730 × réplicas
```

**Supuestos**
- Se valora lo reservado (requests × réplicas) con precios por vCPU y GiB; no es la factura del nodo.
- La holgura (CPU 1,30; memoria 1,25) cubre la variabilidad observada en la ventana.

**Límites**
- El ahorro solo se materializa si el planificador/autoscaler de nodos retira capacidad.

## 3. Evidencia: qué se guarda de cada recomendación

Cada versión de la evidencia (`recommendation_evidence`, solo INSERT para el rol de aplicación) guarda: fecha de los datos (`data_as_of`), coste de referencia, su origen y su
ventana, ahorro, confianza, fórmula (`formula_id`), supuestos, entradas y la evidencia completa (métricas, umbrales, parche propuesto). Se calcula una **huella SHA-256** de la forma
canónica (sin marcas de tiempo volátiles ni orden de claves) y esa huella:

- queda en el evento de auditoría de creación (`RECOMMENDATION_CREATED`) y de cambio (`EVIDENCE_UPDATED`);
- queda en `approvals.context` y en el evento `APPROVAL_GRANTED`: **cada aprobación nombra la evidencia exacta que vio la persona**;
- se recalcula al consultarla (`integrity_ok`): si alguien alterara la evidencia guardada, la huella ya no coincidiría con la que está sellada en la cadena de auditoría.

No se crea una versión nueva en cada escaneo (las métricas oscilan): solo cuando cambia lo que importa para decidir —ahorro en ≥ máx(1 USD, 1 %), confianza en ≥ 0,05, origen del
coste o parámetros de la acción—. La **versión** de la recomendación (que invalida las aprobaciones ya dadas) sube con umbrales algo más amplios: cambio de riesgo, de aprobadores
exigidos, de parámetros, o del ahorro en ≥ máx(1 USD, 5 %) respecto a lo que veía quien aprueba. Un cambio de céntimos no anula una aprobación a medias.

## 4. Recomendaciones duplicadas, incompatibles y obsoletas

- **Identidad estable.** La clave de deduplicación es (regla, cuenta, recurso, acción, parámetros). Para «reducir tamaño» la identidad es *el recurso*, no el tipo destino: si las métricas
  cambian y el destino pasa de `m5.xlarge` a `m5.large`, es la misma recomendación (se actualizan sus parámetros y sube la versión), no una segunda incompatible.
- **Sustitución.** Una instancia ociosa (eliminar) cubre a «reducir tamaño»; esta queda como alternativa.
- **Cambios en curso.** Si un recurso ya tiene una recomendación aprobada, con PR, fusionada o desplegada sin verificar, un hallazgo nuevo sobre él nace **bloqueado por política**
  (`IN_FLIGHT_CONFLICT`): no se aprueban dos cambios a la vez sobre lo mismo.
- **Caducidad.** Tras cada escaneo **completo**, las recomendaciones `PROPOSED`/`PENDING_APPROVAL` que ya no se detectan pasan a `EXPIRED` con el motivo (`resource_gone`, `superseded`,
  `condition_cleared`). Las `APPROVED`/`PR_CREATED` se marcan **obsoletas** (`stale_since`) sin cancelarse: la decisión humana existe, pero quien revisa el PR ve que la premisa cambió.
  Un escaneo parcial o degradado por límites de solicitudes no caduca nada («no lo vi» no es «ya no existe»). No se puede aprobar un cambio sobre un recurso que ya no figura en el inventario.

## 5. Medición del ahorro observado

Se mide tras el despliegue (`POST /recommendations/{id}/verify-savings` sin cifra). La plataforma no demuestra causalidad —con facturación no se puede—: reduce lo que engaña y declara lo que queda.

**Línea base.** Al completarse la aprobación se registra el coste de los 14 días anteriores y la utilización de ese momento (`savings_baselines`, fase `approval`); al marcar el despliegue
(con su fecha real, `deployed_on`) se vuelve a registrar con la ventana que termina ese día (fase `deployment`). Son append-only. La de despliegue es la que se usa; la de aprobación, el respaldo.

**Períodos.** Se comparan 14 días previos con 7-28 días posteriores, **en semanas completas** (misma mezcla de días de la semana), sin el día del despliegue ni el siguiente (cobro parcial) ni los
últimos 2 días (retraso de facturación). Se compara el promedio diario × 30,4375, nunca totales de períodos de longitud distinta ni meses naturales. Con menos de
7 días completos posteriores la plataforma **se niega** a calcular.

**Uso.** Si el recurso estuvo activo distinto número de días antes y después, se compara el coste por día activo y se proyecta a los días activos de la línea base («mismas horas que antes»). Se
comprueba la CPU observada frente a la esperada tras el cambio (desviación > 50 % ⇒ la carga cambió) y se compara con el **resto de la cuenta en el mismo servicio sin este recurso**
(Cost Explorer diario por servicio): si el servicio entero se movió más de un 15 % (30 % = grave) el resultado queda como *no atribuible solo al cambio*.

**Datos.** Una ventana calculada con la tabla de precios repite la estimación: se marca `model` y la lectura es *no concluyente*. Tras **eliminar** un recurso, un día sin filas cuenta como coste 0 solo
si ese día la cuenta tiene facturación real; un hueco de datos no es un cero.

**Lectura del resultado** (`attribution`): `confirmed` (el coste bajó lo que explica el cambio, ±15 %), `partial` (menos), `exceeds_model` (más de lo que explica el cambio), `not_applied`
(no parece aplicado), `confounded` (otros factores se movieron), `inconclusive` (datos insuficientes o de modelo) y `unverified` (cifra declarada a mano). Junto con el grado de confianza
(`high`/`medium`/`low`), los factores (`confounders`), los controles aplicados y las limitaciones, se guarda en `savings_verifications`.

**La realización** es ahorro observado ajustado ÷ ahorro **aprobado** (lo que vio la persona que aprobó), no ÷ la última estimación.

### Qué NO garantiza
- Reserved Instances, Savings Plans, créditos y cambios de precio alteran el coste efectivo sin que el recurso cambie.
- Cost Explorer puede revisar sus cifras unos días después.
- El ajuste por uso supone que el precio por hora activa no cambió por otros motivos; si cambió aparece como desviación.
- La corrección por la tendencia del resto del servicio (`control_adjusted_savings`) supone que el recurso habría evolucionado como los demás; es informativa, no la cifra de realización.
- Kubernetes (`RIGHTSIZE_WORKLOAD`) no tiene coste facturado por contenedor: solo admite ahorro declarado.

El informe de demostración (`docs/demo/informe-ahorro-demo.md`, datos sintéticos) enseña cada desenlace posible.
