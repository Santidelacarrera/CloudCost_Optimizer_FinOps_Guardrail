# Validar el colector de AWS contra una cuenta de laboratorio

**Estado honesto:** el colector de AWS se ha probado con respuestas validadas por el modelo oficial de la API (nivel L2 de [supported-services.md](supported-services.md)), **no contra una cuenta real**.
Este documento y la herramienta `aws-lab` existen para hacerlo en una tarde, con permisos estrictamente de solo lectura, y dejar un informe que cualquiera pueda reproducir.

## 1. Preparar la cuenta de laboratorio
1. Una cuenta con algo de actividad: unas instancias EC2 (mejor si una está ociosa), un volumen sin adjuntar, un snapshot. Cost Explorer necesita ≥ 24 h de datos.
2. **Cost Explorer activado** (Billing → Cost Explorer) y, para el coste por recurso, «Datos a nivel de recurso» para EC2 (Billing → Cost Explorer → Settings).
3. Opcional: CloudWatch Agent para la memoria (`mem_used_percent`).
4. Rol de solo lectura: despliega `infrastructure/cloudformation/readonly-role.yaml` (o el módulo Terraform). Concede **exactamente** estas acciones, nada más:
   `ec2:DescribeInstances/Volumes/Snapshots/Images`, `cloudwatch:GetMetricData/ListMetrics`, `rds:DescribeDBInstances` (solo si usas `--include-rds`), `cloudtrail:LookupEvents`, `ce:GetCostAndUsage`, `ce:GetCostAndUsageWithResources`.
   (`sts:GetCallerIdentity` no requiere permiso.) Una prueba comprueba que el rol y la lista de operaciones permitidas del código coinciden.

## 2. Ejecutar
Requisitos: Python ≥ 3.11 y `pip install boto3` (o `pip install -r requirements.txt`). El paquete `cloudcost` vive en `apps/api` y **no se instala**: usa el lanzador `scripts/aws_lab.py` desde la raíz del repo (o `cd apps/api` y `python -m cloudcost.cli aws-lab …`).

Credenciales: las de siempre de boto3 (`aws configure`, `AWS_PROFILE`, variables `AWS_ACCESS_KEY_ID`…). Con `--role-arn` asume el rol de solo lectura.

macOS/Linux:
```bash
export CC_SECRET_LAB_EXT='<ExternalId del rol>'          # el valor no se guarda ni se registra
python scripts/aws_lab.py \
  --account-ref 123456789012 --regions us-east-1,eu-west-1 \
  --role-arn arn:aws:iam::123456789012:role/CloudCostOptimizerReadOnly --external-id-env CC_SECRET_LAB_EXT \
  --months 6 --ce-budget 40 --out-dir lab-out
# datos para compartir: añade  --anonymize --scale 0.3
```
Windows (PowerShell):
```powershell
$env:CC_SECRET_LAB_EXT = '<ExternalId del rol>'
python scripts\aws_lab.py --account-ref 123456789012 --regions us-east-1 `
  --role-arn arn:aws:iam::123456789012:role/CloudCostOptimizerReadOnly --external-id-env CC_SECRET_LAB_EXT `
  --months 6 --ce-budget 40 --out-dir lab-out
```
No usa la base de datos ni escribe en AWS. Coste: cada solicitud a Cost Explorer vale USD 0,01 (un escaneo típico: 4-6; tope `--ce-budget`).

Salida en `lab-out/`: `snapshot.json` (lo recogido), `report.md` (informe) y `report.sha256`.

## 3. Reproducir sin acceso a la cuenta
```bash
python scripts/aws_lab.py --from-snapshot lab-out/snapshot.json --out-dir otro
diff lab-out/report.md otro/report.md && diff lab-out/report.sha256 otro/report.sha256   # sin diferencias
```
El informe depende solo de la instantánea (nada de reloj ni de red): misma instantánea, mismo texto, byte a byte.

## 4. Criterios para dar la validación por buena
| Criterio | Dónde mirarlo en `report.md` |
|---|---|
| Identidad verificada y cuenta correcta | §1 «Identidad … verificada: sí» |
| Solo se invocaron las 10 operaciones de lectura; 0 errores inesperados | §2 |
| El inventario coincide con la consola de EC2 (recursos y estados) | §3 |
| El coste por recurso coincide con Cost Explorer en la consola (±1 %) | §4 y comparación manual de 2-3 recursos |
| El coste mensual por servicio coincide con la factura/consola | §5 |
| Cada hallazgo tiene fórmula, base de coste «real» y ahorro razonable | §6 |
| Las incidencias, si las hay, son las esperadas y dicen qué hacer | §7 |

## 5. Si algo falla
| Tipo en §7 | Causa habitual | Qué hacer |
|---|---|---|
| `permission_denied` | Falta una acción IAM o la cuenta usa una SCP restrictiva | Revisar la política del rol (no ampliarla más allá de la lista) |
| `not_enabled` | Cost Explorer sin activar / sin nivel de recurso | Activarlo y esperar 24 h |
| `credentials` | ExternalId mal, rol sin confianza o sesión caducada | Revisar la confianza del rol |
| `throttled` | Límite de solicitudes de CloudTrail/Cost Explorer | Reducir regiones y repetir |
| `data_unavailable` | Cuenta nueva o período sin datos | Esperar |
| `budget` | Se agotó `--ce-budget` | Subirlo conscientemente |

## 6. Qué NO prueba esta validación
Una cuenta, una fecha. No prueba cuentas con Organizations, miles de recursos, otras particiones (GovCloud, China) ni servicios sin inventario (RDS…). El ahorro de §6 es **estimado**; el
observado solo existe tras desplegar un cambio ([savings-methodology.md](savings-methodology.md)).

## 7. Registro de ejecuciones reales
| Fecha | Cuenta (últimos 4) | Regiones | Resultado | Huella del informe |
|---|---|---|---|---|
| — | — | — | **Ninguna ejecución real todavía** | — |

Añade aquí cada ejecución (y adjunta `snapshot.json` anonimizado) para que el estado «validado contra una cuenta real» sea verificable.
