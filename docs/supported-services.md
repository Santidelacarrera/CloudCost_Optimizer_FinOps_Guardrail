# Qué está soportado de verdad

Una lista de «soportado» sin matices engaña. Esta tabla distingue lo que **existe en el código**, lo que **se ha probado y con qué** y lo que **nadie ha ejecutado contra un sistema real**.
Cada fila cita la prueba que la sostiene. Si algo no figura aquí, no está soportado.

**Niveles de verificación**

| Nivel | Significado |
|---|---|
| **L0** | Existe en el código; sin pruebas contra respuestas del proveedor |
| **L1** | Probado con respuestas simuladas escritas a mano |
| **L2** | Probado con respuestas **validadas por el modelo oficial de la API** (botocore `Stubber`: rechaza parámetros inexistentes, valores fuera de rango y respuestas que no cumplen el esquema) |
| **L3** | Ejecutado contra una **cuenta/entorno real** con informe reproducible adjunto |

Ningún elemento de esta página está en L3 a fecha de este documento. Para AWS existe la herramienta que lo consigue en una tarde: [aws-lab-validation.md](aws-lab-validation.md).

## AWS

| Capacidad | Soporte | Nivel | Prueba | Notas |
|---|---|---|---|---|
| Identidad de la cuenta (`sts:GetCallerIdentity`) | Sí | L2 | `test_credentials_of_another_account_stop_the_scan…` | Si las credenciales son de otra cuenta que la registrada, no se recoge nada |
| Asumir rol con ExternalId | Sí | L1 | `test_assume_role_failure_…` | Errores clasificados; el ExternalId nunca aparece en mensajes ni logs |
| EC2: instancias `running`/`stopped` (inventario) | Sí | L2 | `test_full_collection_normalizes_costs…` | Varias regiones; paginación de `DescribeInstances` |
| EC2: CPU (CloudWatch) y memoria (CWAgent `mem_used_percent`) | Sí | L2 | idem | Sin CWAgent no hay memoria: la regla de reducción no se propone (`require_memory_metric`) |
| EBS: volúmenes y tiempo sin adjuntar (CloudTrail `DetachVolume`, 90 días) | Sí | L2 | idem | Máx. 50 búsquedas de CloudTrail por región y escaneo (límite de 2 solicitudes/s) |
| EBS: snapshots propios y su relación con AMI | Sí | L2 | idem | Se excluyen los gestionados por AWS Backup/DLM (por etiqueta) y los usados por una AMI |
| **RDS** | **No** (solo datos de demostración o CSV) | — | — | La regla `rds_idle` existe y se prueba, pero el colector real **no inventaría** RDS |
| S3, Lambda, DynamoDB, ElastiCache, EKS/ECS, balanceadores, NAT, VPC… | **No** (inventario) | — | — | Sí aparecen en el **coste de la cuenta** por servicio (ver abajo) |
| Cost Explorer: coste **por recurso** (EC2, EBS, snapshots), 14 días | Sí | L2 | `test_full_collection…`, `test_failure_on_a_later_page…` | Requiere activar «datos a nivel de recurso» en Cost Explorer. Si falta, se degrada a la tabla de precios y se avisa |
| Cost Explorer: coste **de la cuenta por servicio y región**, diario (35 días) y mensual (hasta 12 meses) | Sí | L2 | `test_full_collection…` | Filtrado por `LINKED_ACCOUNT` = `account_ref`; una moneda por fila, nunca se suman monedas distintas; marca `Estimated` conservada |
| Cost Explorer: historial por etiqueta de asignación de costos | Sí | L1 | `test_aws_cost_explorer.py` | El coste de una etiqueta agrupa a todos los recursos que la comparten |
| Errores clasificados (permiso, no activado, credenciales, límite, datos no disponibles, red) | Sí | L2 | `test_error_classification`, `test_missing_inventory_permission_…` | Mensajes fijos con «qué hacer»; nunca el texto de AWS |
| Límites de solicitudes: reintentos con espera exponencial y presupuesto de Cost Explorer | Sí | L2 | `test_throttling_is_retried_…`, `test_request_budget_caps_…` | Cada solicitud a Cost Explorer cuesta USD 0,01: por defecto máx. 60 por escaneo (`AWS_CE_REQUEST_BUDGET`) |
| Guardia de solo lectura (lista cerrada de 10 operaciones) | Sí | L2 | `test_aws_readonly_guard.py` | Segundo cerrojo además del rol IAM; el rol concede exactamente esas acciones |
| Varias cuentas / AWS Organizations | **Parcial** | L0 | — | Una cuenta registrada = una cuenta consultada. No hay descubrimiento de cuentas vinculadas |
| GovCloud, China | **No probado** | — | — | Cost Explorer usa el endpoint de `us-east-1` |
| Recomendaciones de AWS (Trusted Advisor, Cost Optimization Hub, Compute Optimizer) | **No** | — | — | Evaluado en [ADR-0003](adr/0003-external-recommendation-sources.md): no se integra todavía |

## Reglas de ahorro (todas deterministas)

| Regla | Servicios | Acción | Fórmula |
|---|---|---|---|
| `ec2_downsize` | AWS EC2, Azure VM, GCP GCE | Reducir hasta 2 tamaños | `ec2_downsize.v1` |
| `ec2_idle` | AWS EC2, Azure VM, GCP GCE | Eliminar (destructiva) | `ec2_idle.v1` |
| `ebs_orphan` | AWS EBS, Azure Disk, GCP PD | Eliminar (destructiva) | `ebs_orphan.v1` |
| `snapshot_old` | AWS, Azure, GCP | Eliminar (destructiva), ahorro = cota superior | `snapshot_old.v1` |
| `rds_idle` | AWS RDS (**solo demo/CSV**) | Eliminar (destructiva) | `rds_idle.v1` |
| `k8s_overprovisioned` | Kubernetes vía Prometheus | Ajustar `values.yaml` de Helm | `k8s_overprovisioned.v1` |

Detalle de fórmulas y supuestos: [savings-methodology.md](savings-methodology.md).

## Otras nubes y orígenes

| Origen | Qué lee | Nivel | Notas |
|---|---|---|---|
| Azure | VM, discos administrados, snapshots (REST); métricas de Azure Monitor | L1 | Coste = tabla de precios (sin coste real). Sin pruebas contra una suscripción real |
| GCP | Compute Engine, discos persistentes, snapshots; Cloud Monitoring | L1 | Coste = tabla de precios. Sin pruebas contra un proyecto real |
| Kubernetes | `requests`/`limits` frente a uso, vía **Prometheus** (kube-state-metrics + cAdvisor) | L1 | No habla con la API de Kubernetes. Herramienta para validarlo en un clúster real local: `python scripts/k8s_lab.py` ([guía](k8s-lab-validation.md)); **todavía sin ejecutar** |
| CSV importado | EC2, EBS, snapshots con uso y coste (≤ 5 000 filas / 2 MB) | L1 | Coste del archivo tratado como verificado |
| Demostración | 21 recursos sintéticos de AWS | — | Solo con `DEMO_ENABLED=true` (prohibido en producción) |

## Git, IaC y políticas

| Capacidad | Soporte | Nivel | Notas |
|---|---|---|---|
| GitHub: rama + commit + Pull Request (borrador si procede), idempotente | Sí | L1 | Solo esas llamadas; nunca fusiona ni despliega (`test_git_no_merge.py`) |
| GitLab: rama + commit + Merge Request | Sí | L1 | Ídem; el borrador se marca con el prefijo `Draft:` |
| IaC: Terraform (literales) y `values.yaml` de Helm | Sí | L1 | Sin CDK, Pulumi, Bicep ni manifiestos YAML sueltos |
| Políticas Rego | `aws_instance`, `aws_ebs_volume`, `aws_ebs_snapshot`, `aws_db_instance`, VM/discos/snapshots de Azure, instancias/discos/snapshots de GCP | L2 (motor `opa` real) | `test_guardrail_matrix.py`. Un tipo de recurso que no esté en la lista **no** está protegido |

## Qué falta para subir de nivel

1. **AWS L2 → L3**: ejecutar `python scripts/aws_lab.py` contra una cuenta de laboratorio y adjuntar `report.md`/`snapshot.json`.
2. **RDS**: inventario real (`DescribeDBInstances` + `AWS/RDS` en CloudWatch). Hoy no existe.
3. **Kubernetes L1 → L3**: ejecutar `python scripts/k8s_lab.py` contra minikube ([k8s-lab-validation.md](k8s-lab-validation.md)); gratis y sin cuenta. **Azure/GCP**: cuentas de laboratorio, como en AWS.
4. **Cuentas múltiples**: descubrimiento de cuentas vinculadas y un rol por cuenta.

Hasta entonces, esta página y el README dicen lo mismo: lo no validado contra un sistema real se presenta como no validado.
