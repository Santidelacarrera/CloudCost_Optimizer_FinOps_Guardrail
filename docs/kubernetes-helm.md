# Kubernetes y Helm: requests sobredimensionados

CloudCost compara lo que cada contenedor **reserva** (`requests`/`limits`) con lo que **usa** de verdad y propone un Pull Request
que baja los valores en tu `values.yaml` de Helm. No necesita acceso a la API de Kubernetes ni un kubeconfig: lee un Prometheus que ya
recoge [kube-state-metrics](https://github.com/kubernetes/kube-state-metrics) y las métricas del kubelet/cAdvisor. Solo hace
consultas de lectura (`GET /api/v1/query`).

## Qué necesita tu Prometheus
- **kube-state-metrics** (≥ 2.x): `kube_pod_container_resource_requests/limits`, `kube_pod_owner`, `kube_replicaset_owner`,
  `kube_horizontalpodautoscaler_info`, `kube_pod_container_status_last_terminated_reason`, `kube_*_created`.
- **cAdvisor** (kubelet): `container_cpu_usage_seconds_total`, `container_memory_working_set_bytes`.
- Para localizar el release de Helm de cada workload, expón las etiquetas de los pods (opcional, mejora la coincidencia):
  `--metric-labels-allowlist=pods=[app.kubernetes.io/instance,app.kubernetes.io/name,helm.sh/chart]`.
- Retención de al menos 7 días (idealmente 14: es la ventana que se analiza).

## Conectar un clúster
```bash
curl -X POST $API/api/v1/cloud-accounts -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{
  "provider": "kubernetes", "account_ref": "prod-eks", "display_name": "EKS producción",
  "prometheus_url": "https://prometheus.example.com",
  "credentials_ref": "env:PROM_TOKEN",
  "exclude_namespaces": ["kube-system", "monitoring"],
  "cpu_hour_usd": 0.0316, "mem_gib_hour_usd": 0.0042}'
```
- `prometheus_url`: **https**, o `http` solo dentro del clúster (`*.svc`, `*.svc.cluster.local`, `localhost`). Se rechazan credenciales en la URL y direcciones de metadatos de la nube (`169.254.x.x`, `metadata.google.internal`).
- `credentials_ref`: token Bearer opcional (referencia `env:`/`aws-sm:`, nunca el valor). Sin él no se envía cabecera `Authorization`.
- `namespaces` / `exclude_namespaces`: por defecto se excluyen `kube-system`, `kube-public` y `kube-node-lease`.
- `cpu_hour_usd` / `mem_gib_hour_usd`: lo que te cuesta un vCPU-hora y un GiB-hora (por defecto ≈ un nodo de propósito general). El ahorro es **de lo reservado**, no la factura del nodo: si el clúster hace *bin packing* no se libera dinero hasta que los nodos se reduzcan.
- El entorno (producción/staging/…) se deduce del nombre del namespace (`shop-prod`, `team-dev`…) o de `environment`; lo desconocido se trata como producción.
- Regístralo también como repositorio (el de tus charts/GitOps) para que se generen los parches.

## Cómo decide
Por cada (workload, contenedor) —Deployment, StatefulSet y DaemonSet— se agrega el uso de **todos los pods de la ventana, incluidos los
ya borrados** (un despliegue reciente no borra el historial):

| | Recomendación | Condición para proponerla |
|---|---|---|
| CPU | máx(p95 × 1,30 ; máximo × 0,8 ; 50m) | recorte ≥ 30 % de lo pedido |
| Memoria | máximo observado × 1,25, redondeado hacia arriba a 16 MiB (mín. 64 MiB) | recorte ≥ 30 % de lo pedido |

No se propone nada si: hay menos de 7 días de datos; **hay un HPA** (los requests fijan el % que escala: la CPU no se toca); el contenedor
**murió por OOM** (la memoria no se toca); los pods tienen requests distintos entre sí (despliegue en curso); o el recurso lleva
`finops:ignore`. Si el límite era igual al request (QoS *Guaranteed*) se baja también el límite para mantener la igualdad; si el límite era mayor, no se toca.

## El parche de `values.yaml`
- El bloque `resources:` se identifica porque sus requests **coinciden numéricamente** con los del clúster (`500m` = `0.5`). Si ninguno coincide (otra plantilla, `--set`, deriva entre Git y el clúster) o coinciden varios (mismos valores en `staging` y `prod`), **no se parchea** y la recomendación explica por qué. Con varios candidatos se desempata por chart (`Chart.yaml`), release (en la ruta) y nombre del contenedor; nunca se adivina.
- Se edita el texto por posiciones: **comentarios, orden, sangría y comillas quedan igual**. Solo cambian los números.
- Nunca sube un valor; rechaza alias/anclas YAML (`*default`) y llaves de mezcla (`<<`); y tras editar relee el archivo y comprueba que **solo** cambiaron las rutas previstas.
- Solo se leen `values*.yaml`/`values*.yml` y `Chart.yaml` del repositorio (no todo el YAML). Kustomize y manifiestos planos no están soportados todavía.
- Con el proveedor GitHub y el local ya funciona; para GitLab hay que ampliar el filtro de archivos igual que en GitHub (`git/base.py: is_iac_file`).

## Rendimiento
El colector lanza ~20 consultas, tres de ellas con subconsulta de 14 días a 10 minutos (CPU media, p95 y máximo). En clústeres grandes
acota con `namespaces` o crea *recording rules* si Prometheus tarda: cada consulta tiene un límite de 120 s y un fallo de una consulta
deja el escaneo como **parcial** (sin marcar nada como inactivo) en vez de abortarlo.

## Qué se ha probado
Colector, regla y parche se prueban con un Prometheus simulado y archivos YAML reales (incluidos CRLF, estilo de flujo, multi-documento,
alias, bomba de alias, deriva y ambigüedad); el alta, el escaneo, la recomendación, la aprobación y el PR se prueban contra PostgreSQL real en CI.
**No se han ejecutado las consultas PromQL contra un Prometheus real**: en el primer escaneo compara los requests y el uso de un par de
workloads con tu Grafana antes de aprobar nada.
