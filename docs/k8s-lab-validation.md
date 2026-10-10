# Validar el colector de Kubernetes con minikube (100 % gratis, sin cuenta de nube)

**Estado honesto:** el colector de Kubernetes se ha probado con un Prometheus simulado (nivel L1 de [supported-services.md](supported-services.md)). Esta guía permite subirlo a **L3 contra un clúster real** en tu PC, sin pagar nada y sin ninguna cuenta.

## Qué valida y qué no
| Valida | No valida |
|---|---|
| Que las consultas PromQL funcionan contra un Prometheus real (kube-state-metrics + cAdvisor) | Facturación real: un clúster local no tiene factura. El coste es el de lo **reservado** (requests × réplicas) con precios por vCPU y GiB declarados |
| Inventario por workload (réplicas, requests, límites, HPA) | Que funcione igual en clústeres grandes, con muchos namespaces u otras distribuciones de Prometheus |
| La regla `k8s_overprovisioned` con tres comportamientos conocidos (ver abajo) | La medición de ahorro contra costes reales (eso solo lo cubre una cuenta de nube con facturación) |
| Que el informe es reproducible byte a byte | AWS: sigue pendiente de una cuenta activada ([aws-lab-validation.md](aws-lab-validation.md)) |

## 1. Requisitos
Docker Desktop (en marcha), [minikube](https://minikube.sigs.k8s.io/docs/start/), `kubectl` y [helm](https://helm.sh/docs/intro/install/). Unos 6 GB de RAM libres para el clúster. En Windows: `winget install Kubernetes.minikube Kubernetes.kubectl Helm.Helm`.
Python ≥ 3.11 con `pip install -r requirements.txt` (solo se usa `requests`).

## 2. Levantar el laboratorio
```powershell
.\scripts\k8s_lab_setup.ps1          # macOS/Linux: ./scripts/k8s_lab_setup.sh
```
Crea el clúster (4 CPU, 6 GB), instala Prometheus con kube-state-metrics y despliega en el namespace `lab-dev` tres cargas
([`infrastructure/k8s-lab/workloads.yaml`](../infrastructure/k8s-lab/workloads.yaml)):

| Carga | Qué hace | Qué DEBE proponer el sistema |
|---|---|---|
| `overprovisioned-web` | Pide 500m / 512Mi × 2 réplicas y casi no usa nada | Bajar CPU **y** memoria |
| `well-sized` | Consume lo que pide (100m / 64Mi) | **Nada** |
| `hpa-protected` | Pide 200m / 768Mi × 3 réplicas, con HPA | Bajar **solo la memoria** (con HPA la CPU no se toca) |

## 3. Esperar y medir
Las medias y percentiles necesitan datos: espera **al menos 30 minutos** (mejor 2 horas o más; con 24 h los números son mucho más fiables).
```powershell
# Terminal A (déjala abierta):
kubectl -n monitoring port-forward svc/prometheus-server 9090:80
# Terminal B:
python scripts\k8s_lab.py --min-observation-days 0 --out-dir k8s-lab-out
Get-Content k8s-lab-out\report.md -Encoding UTF8
```
`--min-observation-days 0` es **solo para laboratorio**: el producto exige 7 días de datos antes de proponer nada, y el informe lo avisa. Para probar el umbral real, deja el clúster 7 días y omite la opción.

Opciones útiles: `--namespaces lab-dev,otro`, `--cpu-hour` y `--mem-gib-hour` (USD por núcleo-hora y GiB-hora), `--cluster-ref`.
El comando termina con código 0 si el informe se generó y **todas las comprobaciones** de la sección 4 salieron OK; con 1 si Prometheus no responde o alguna comprobación falla.

## 4. Reproducir sin el clúster
```powershell
python scripts\k8s_lab.py --from-snapshot k8s-lab-out\snapshot.json --out-dir otro
fc.exe k8s-lab-out\report.md otro\report.md       # sin diferencias
```

## 5. Criterios para dar la validación por buena
| Criterio | Dónde mirarlo en `report.md` |
|---|---|
| Solo `GET /api/v1/query`; 0 consultas fallidas | §1 |
| Las tres cargas aparecen con sus réplicas y requests correctos (compáralo con `kubectl -n lab-dev get deploy -o wide`) | §2 |
| 3 de 3 comprobaciones del laboratorio correctas | §4 |
| Sin avisos de consultas fallidas | §5 |

## 6. Problemas frecuentes
- **«No se pudo conectar con Prometheus»**: falta la terminal con el `port-forward`, o se cerró.
- **Inventario vacío**: kube-state-metrics aún no ha arrancado (`kubectl -n monitoring get pods`) o los namespaces no coinciden.
- **Sin hallazgos y 0 comprobaciones OK**: faltan datos todavía (espera más) o olvidaste `--min-observation-days 0`.
- **Pods en `Pending`**: poca memoria en el clúster; `minikube delete` y vuelve a crearlo con más (`minikube start --memory 8192`).

Para borrarlo todo: `minikube delete`.
