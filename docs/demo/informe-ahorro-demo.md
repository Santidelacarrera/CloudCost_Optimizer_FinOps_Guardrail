# Informe de demostración: ahorro estimado, aprobado y observado

> **DATOS SINTÉTICOS DE LABORATORIO.** Los diez casos son inventados para enseñar cómo se separan y se explican las cifras. No son clientes, ni facturas, ni ahorros reales. Pasan por las mismas funciones del producto (reglas para el estimado, `domain/measurement.py` para el observado).

## Las tres cifras (y por qué nunca se suman)

| Cifra | Qué es | Qué NO es |
|---|---|---|
| **Estimado** | Lo que una regla calcula que se ahorraría si el cambio se aplicara y todo lo demás siguiera igual. Lleva fórmula, coste de referencia y supuestos. | Un ahorro conseguido. |
| **Aprobado** | El estimado **congelado** en el momento en que las personas autorizadas aprueban; queda ligado a la huella de la evidencia que vieron. | Una promesa: sigue siendo una proyección hasta que se despliega y se mide. |
| **Observado** | Lo que se mide DESPUÉS del despliegue con la facturación real: 14 días previos frente a 1-4 semanas posteriores, sin los días de transición, corregido por días activos y comparado con el resto de la cuenta. | Una prueba de causalidad: es compatible con el cambio, no demuestra que lo causara. |

Ventanas de comparación (iguales en todos los casos): **antes** 2026-08-18 → 2026-09-01 (exclusivo, 14 días) · **después** 2026-09-03 → 2026-09-24 (exclusivo, 21 días, semanas completas). Importes en USD por mes (30,4375 días).

## Resumen por caso

| Caso | Estimado | Aprobado | Observado (ajustado) | Diferencia bruta | Realización | Lectura | Confianza |
|---|---:|---:|---:|---:|---:|---|---|
| **A** Reducir api-staging: m5.2xlarge → m5.xlarge | 152.19 | 152.19 | 152.62 | 152.62 | 100.3 % | Coherente con el cambio | alta |
| **B** Reducir batch-staging: m5.2xlarge → m5.xlarge | 152.19 | 152.19 | 152.55 | 188.86 | 100.2 % | Coherente con el cambio | media |
| **C** Reducir reports-dev: m5.2xlarge → m5.xlarge | 152.19 | 152.19 | 0.22 | 0.22 | 0.1 % | El cambio no parece aplicado | alta |
| **D** Reducir ml-staging: m5.2xlarge → m5.xlarge | 152.19 | 152.19 | 152.62 | 152.62 | 100.3 % | No atribuible solo al cambio | baja |
| **E** Eliminar volumen huérfano vol-tmp-rollout | 45.66 | 45.66 | 45.75 | 45.75 | 100.2 % | Coherente con el cambio | alta |
| **F** Eliminar snapshot antiguo snap-2025-q1 | 20.00 | 20.00 | 11.02 | 11.02 | 55.1 % | Coherente con el cambio | alta |
| **G** Eliminar base de datos sin uso orders-legacy-dev | 147.83 | 147.83 | — | — | — | Aprobado, sin desplegar: nada que observar | — |
| **H** Reducir cache-prod: m5.2xlarge → m5.xlarge | 152.19 | 152.19 | 114.38¹ | 114.38 | 75.2 % | Declarado, no medido | baja |
| **I** Reducir dev-sandbox: m5.2xlarge → m5.xlarge | 152.19 | 152.19 | 152.62 | 152.62 | 100.3 % | No concluyente | baja |
| **J** Eliminar instancia ociosa old-batch | 304.38 | — | — | — | — | Pendiente de aprobación: solo una proyección | — |

¹ Cifra escrita a mano por una persona; la plataforma no la midió.

## Totales: cada tipo de cifra por separado

| Concepto | USD/mes | Casos |
|---|---:|---|
| Estimado, sin decidir | 304.38 | J |
| Aprobado, sin verificar | 147.83 | G |
| **Observado atribuible al cambio** | **362.16** | A, B, C, E, F |
| Observado no atribuible solo al cambio | 152.62 | D |
| Observado declarado o sin facturación | 267.00 | H, I |

**Realización del observado atribuible: 69.3 %** (observado atribuible ÷ aprobado de esos mismos casos). Mezclar las columnas daría un número más bonito y más falso: por eso el producto no lo calcula.

## Detalle de la medición

### A · Reducir api-staging: m5.2xlarge → m5.xlarge

Cambio aplicado sin incidencias; el coste baja lo que predice la proporción de precios.

- Línea base: USD 305.03/mes · observado bruto: USD 152.40/mes · observado ajustado por uso: USD 152.40/mes.
- Ahorro: bruto USD 152.62 · ajustado USD 152.62 · aprobado USD 152.19.
- Datos: **facturación** · lectura: Coherente con el cambio · confianza alta.
- Días activos: antes 100%, después 100% · desviación respecto a lo que explica el cambio de tamaño: -0.1%.
- Resto de la cuenta en el mismo servicio: +1.1%.

### B · Reducir batch-staging: m5.2xlarge → m5.xlarge

Igual que A, pero la instancia estuvo parada 5 días tras el cambio: la diferencia bruta se infla y el ajuste por uso la corrige.

- Línea base: USD 305.03/mes · observado bruto: USD 116.17/mes · observado ajustado por uso: USD 152.47/mes.
- Ahorro: bruto USD 188.86 · ajustado USD 152.55 · aprobado USD 152.19.
- Datos: **facturación** · lectura: Coherente con el cambio · confianza media.
- Días activos: antes 100%, después 76% · desviación respecto a lo que explica el cambio de tamaño: -0.0%.
- Resto de la cuenta en el mismo servicio: +1.1%.
- ⚠ **active_days_changed** (medium): El recurso estuvo activo el 100% de los días antes y el 76% después: se comparó el coste por día activo, proyectado a los días activos de la línea base.

### C · Reducir reports-dev: m5.2xlarge → m5.xlarge

El Pull Request se fusionó pero el despliegue no llegó a producción: el coste no cambió.

- Línea base: USD 305.03/mes · observado bruto: USD 304.81/mes · observado ajustado por uso: USD 304.81/mes.
- Ahorro: bruto USD 0.22 · ajustado USD 0.22 · aprobado USD 152.19.
- Datos: **facturación** · lectura: El cambio no parece aplicado · confianza alta.
- Días activos: antes 100%, después 100% · desviación respecto a lo que explica el cambio de tamaño: +99.9%.
- Resto de la cuenta en el mismo servicio: +1.1%.

### D · Reducir ml-staging: m5.2xlarge → m5.xlarge

El cambio se aplicó, pero ese mes el resto de EC2 en la cuenta subió un 45 % (cargas nuevas): no se puede atribuir el resultado solo al cambio.

- Línea base: USD 305.03/mes · observado bruto: USD 152.40/mes · observado ajustado por uso: USD 152.40/mes.
- Ahorro: bruto USD 152.62 · ajustado USD 152.62 · aprobado USD 152.19.
- Datos: **facturación** · lectura: No atribuible solo al cambio · confianza baja.
- Días activos: antes 100%, después 100% · desviación respecto a lo que explica el cambio de tamaño: -0.1%.
- Resto de la cuenta en el mismo servicio: +45.0% · ahorro corregido por esa tendencia: USD 289.88 (supone que el recurso habría evolucionado como el resto).
- ⚠ **service_wide_change** (high): El resto de la cuenta en el mismo servicio subió un 45% en el mismo período: el cambio de coste no es atribuible solo a esta acción.

### E · Eliminar volumen huérfano vol-tmp-rollout

Volumen de un despliegue fallido: tras eliminarlo deja de facturarse y la cuenta sigue con facturación.

- Línea base: USD 45.75/mes · observado bruto: USD 0.00/mes · observado ajustado por uso: USD 0.00/mes.
- Ahorro: bruto USD 45.75 · ajustado USD 45.75 · aprobado USD 45.66.
- Datos: **facturación** · lectura: Coherente con el cambio · confianza alta.
- Coste residual tras eliminar: 0.0% del anterior.

### F · Eliminar snapshot antiguo snap-2025-q1

La estimación del snapshot es una COTA SUPERIOR (tamaño del volumen × precio); la facturación real era menor: la realización lo muestra.

- Línea base: USD 11.02/mes · observado bruto: USD 0.00/mes · observado ajustado por uso: USD 0.00/mes.
- Ahorro: bruto USD 11.02 · ajustado USD 11.02 · aprobado USD 20.00.
- Datos: **facturación** · lectura: Coherente con el cambio · confianza alta.
- Coste residual tras eliminar: 0.0% del anterior.

### I · Reducir dev-sandbox: m5.2xlarge → m5.xlarge

Los costes de la ventana vienen de la tabla de precios (la cuenta no tenía Cost Explorer por recurso): repetir la estimación no es observar.

- Línea base: USD 305.03/mes · observado bruto: USD 152.40/mes · observado ajustado por uso: USD 152.40/mes.
- Ahorro: bruto USD 152.62 · ajustado USD 152.62 · aprobado USD 152.19.
- Datos: **tabla de precios (no es una observación)** · lectura: No concluyente · confianza baja.
- Días activos: antes 100%, después 100% · desviación respecto a lo que explica el cambio de tamaño: -0.1%.
- ⚠ **model_data** (high): Los costes de la ventana provienen de la tabla de precios, no de la facturación: la cifra repite la estimación, no la observa.

## Qué demuestra este informe y qué no

- Demuestra que las cifras se separan, que cada una dice de dónde sale y que el sistema distingue «el cambio funcionó» de «no se aplicó», «otra cosa se movió» y «nadie lo midió».
- No demuestra ahorros reales: los datos son inventados. El primer informe con una cuenta real debe generarse con `python -m cloudcost.cli aws-lab` y, pasado el despliegue, con la verificación de ahorro del producto.
- Ningún método basado en facturación puede probar causalidad. Reserved Instances, Savings Plans, créditos, cambios de precio y de carga pueden mover el coste por su cuenta; los controles de este informe reducen ese riesgo, no lo eliminan.
- El ajuste por uso supone que el precio por hora activa no cambió por otros motivos; si cambió, aparece como desviación o como «mayor de lo que explica el cambio».
- Con menos de 7 días completos posteriores al despliegue (descontando transición y retraso de facturación) el producto se niega a calcular en lugar de dar una cifra frágil.

## Cómo reproducirlo

```bash
python -m cloudcost.demo_report > docs/demo/informe-ahorro-demo.md   # mismo código → mismo texto, byte a byte
pytest tests/unit/test_demo_report.py                                # comprueba que este archivo está al día
```
