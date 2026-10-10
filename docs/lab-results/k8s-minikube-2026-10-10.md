# Resultado: Kubernetes en minikube (2026-10-10)

Primera ejecución del colector de Kubernetes contra un clúster **real** (minikube v1.39.0, Kubernetes v1.37.0 sobre Docker Desktop, Windows 11) con
Prometheus real (chart `prometheus-community/prometheus`: kube-state-metrics + cAdvisor). Procedimiento: [k8s-lab-validation.md](../k8s-lab-validation.md).

**Alcance honesto de este resultado**
- Las cargas llevaban **30 minutos** en marcha y se usó `--min-observation-days 0`: demuestra que el flujo y las consultas funcionan contra un Prometheus real,
  **no** que los percentiles sean representativos ni que los hallazgos sirvan para decidir en producción.
- Un solo clúster local con 3 cargas sintéticas; sin facturación real (coste = reservado × precios por defecto).
- Los valores coinciden con los previstos antes de ejecutar (coste reservado 26,13 / 2,50 / 20,74; ahorro 23,44 + 6,32): el informe que sigue es el obtenido,
  transcrito del texto de `report.md` (se omiten el texto de reproducción y las limitaciones, que son los de la guía). Huella de la instantánea: `5e5fb0dd8d46a5a0b67963ab7d925a2d608b96316b22dbf0f5d8cb0a88e6345c`.
- Pendiente: repetir con ≥ 24 h de datos (y, para el umbral de producción, 7 días).

---

# Validación del colector de Kubernetes · clúster `minikube`

> Generado el 2026-10-10T02:22:09+00:00 con `cloudcost-k8s-lab` (Python 3.12.10). Huella de la instantánea: `5e5fb0dd8d46a5a0b67963ab7d925a2d608b96316b22dbf0f5d8cb0a88e6345c`.

## 1. Resultado de la validación

- Prometheus: `localhost`. Namespaces evaluados: lab-dev.
- Consultas enviadas: 19 (todas `GET /api/v1/query`, solo lectura); fallidas: 0.
- Resultado: **sin incidencias**.
- Umbral de observación reducido a 0 días (el del producto es 7). Solo para laboratorio.

## 2. Inventario de workloads

| Workload | Réplicas | CPU pedida | CPU p95 / máx | Mem pedida | Mem máx | HPA | Días de datos | Coste reservado/mes |
|---|---:|---:|---|---:|---:|---|---:|---:|
| lab-dev/hpa-protected/app | 3 | 200m | 200m / 200m | 768Mi | 4Mi | sí | 0 | 20.74 |
| lab-dev/overprovisioned-web/app | 2 | 500m | 0m / 0m | 512Mi | 10Mi | no | 0 | 26.13 |
| lab-dev/well-sized/app | 1 | 100m | 100m / 100m | 64Mi | 4Mi | no | 0 | 2.50 |

## 3. Hallazgos (ahorro ESTIMADO, no observado)

2 hallazgos; ahorro estimado de lo reservado **USD 29.76/mes**.

| Workload | Cambio propuesto | Ahorro/mes | Riesgo | Confianza |
|---|---|---:|---|---:|
| overprovisioned-web | cpu: 50m, memory: 64Mi | 23.44 | MEDIUM | 74% |
| hpa-protected | memory: 64Mi | 6.32 | MEDIUM | 74% |

## 4. Comprobaciones del laboratorio

| Carga | Debía ocurrir | Ocurrió | Resultado |
|---|---|---|---|
| hpa-protected | solo memoria, sin tocar la CPU | memory | OK |
| overprovisioned-web | bajar CPU y memoria | cpu, memory | OK |
| well-sized | nada | sin hallazgos | OK |

**3 de 3 comprobaciones correctas.**
