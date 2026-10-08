# Runbook

## 1. Conectar AWS (solo lectura)
```bash
cd infrastructure/terraform/aws-readonly-role
terraform init && terraform apply -var trusted_principal_arn=<ARN de CloudCost> -var external_id=<valor secreto>
```
Guarda `external_id` en tu gestor de secretos y registra la cuenta (`POST /api/v1/cloud-accounts`) con `role_arn` y `external_id_ref` = `env:NOMBRE` o `aws-sm:id`. 
**Costos reales (Cost Explorer).** Por defecto (`enable_cost_explorer=true`, `AWS_COST_EXPLORER_RESOURCES=true`) el ahorro se calcula con el costo REAL de cada recurso de los últimos 14 días (`GetCostAndUsageWithResources`, límite de AWS), no con la tabla de precios. Requisitos y límites:
- Habilita *Cost Explorer* y *Resource-level data* en la cuenta de pagos (Billing → Cost Explorer → Settings). Sin eso, o sin el permiso `ce:*`, el escaneo sigue funcionando con la estimación y deja un aviso en `scans.stats.warnings`; las recomendaciones indican «Costo ESTIMADO».
- Historial largo (hasta 12 meses): define `AWS_COST_TAG_KEY` con una etiqueta de asignación de costos activada en Billing (p. ej. `Name` o `app`). Se guarda la serie mensual y la tendencia por valor de etiqueta en `evidence.cost_basis.history`; es el costo agregado de todos los recursos con esa etiqueta, no se reparte entre recursos.
- `AWS_COST_METRIC` (`UnblendedCost` por defecto; `AmortizedCost`/`NetAmortizedCost` si usas Reserved Instances o Savings Plans y quieres ver el costo amortizado).
- Coste de la propia consulta: AWS cobra USD 0.01 por solicitud de Cost Explorer (≈ 2-5 por escaneo y cuenta).
- Política estricta: la configuración `require_verified_cost: true` (tabla `policies`) impide proponer apagar/reducir/eliminar recursos cuyo costo sea solo estimado.
Memoria: requiere CloudWatch Agent (`CWAgent`); sin ella las reglas de downsize no se activan por falta de evidencia.

## 2. Conectar GitHub
1. Token fine-grained con permisos *Contents: write* y *Pull requests: write* solo en el repo de IaC (sin permiso de merge).
2. Registra el repo (`POST /api/v1/repositories`, `token_ref: env:GITHUB_TOKEN`).
3. Webhook (evento *Pull requests*): `https://<api>/api/v1/webhooks/github/<org_id>`, secreto = `GITHUB_WEBHOOK_SECRET`.
4. Protege `main` con revisión obligatoria y checks (incluye `opa test`/plan guardrail de `packages/policies`).

## 3. Producción
Guía completa (servidor, TLS, imágenes, copias, alertas): [deployment.md](deployment.md). Con cuentas propias basta `AUTH_MODE=local`; con un proveedor de identidad externo:
`ENV=production AUTH_MODE=oidc DEMO_ENABLED=false OIDC_ISSUER=… OIDC_AUDIENCE=… OIDC_JWKS_URL=…`. El claim de organización (`org_id`) y rol (`role`) son configurables (`OIDC_ORG_CLAIM`, `OIDC_ROLE_CLAIM`). Ejecuta `scripts/migrate.sh` con el usuario propietario antes de desplegar.

## 4. Operación
- Salud: `/health`, `/ready`. Métricas: `/metrics` (API) y puerto 9100 (worker).
- Integridad de auditoría: `GET /api/v1/audit/verify`.
- Escaneo fallido: `GET /api/v1/scans/{id}` (`error`, `attempts`); los reintentos están acotados.

## 5. Alertas: qué hacer
| Alerta | Primer paso |
|---|---|
| `ApiCaida` | `docker compose ps` y `logs api`. Si `/ready` da 503, Postgres no responde: `logs postgres`, disco lleno, memoria. |
| `ErroresDelServidor` | `logs api`: cada petición lleva su id. Mira si empezó tras un despliegue y, de ser así, vuelve a la versión anterior (deployment.md §4). |
| `LatenciaAlta` | CPU del servidor; muchos inicios de sesión simultáneos (cada uno cuesta ~330 ms de CPU). |
| `PosibleRellenoDeCredenciales` | Auditoría: IP de origen. La API ya limita por cuenta e IP; bloquea el rango en el firewall o en Caddy si persiste. |
| `MuchosBloqueosDeCuenta` | Avisa a las personas afectadas; si son legítimas, esperan unos minutos (el bloqueo no se alarga mientras dura). |
| `EscaneosFallando` / `ErroresDeApiCloud` | `GET /api/v1/scans/{id}`; revisa permisos del rol de solo lectura y límites del proveedor. |
| Contenedor `backup` *unhealthy* | `docker compose logs backup`: suele ser disco lleno o credenciales de S3. Ejecuta una copia manual: `docker compose exec backup sh /scripts/backup.sh`. |

## 6. Una persona perdió el acceso
- **Olvidó la contraseña:** «Olvidé mi contraseña» en el login. Si no le llega el correo, revisa spam y los logs de la API.
- **Perdió el teléfono del 2FA:** puede entrar con un código de recuperación (Entrar → «Usar un código de recuperación»). Si tampoco los tiene, verifica su identidad por otro canal
  (videollamada, su jefatura, un ticket) y quita el 2FA como operador:
  ```bash
  docker compose --env-file .env.production -f docker-compose.prod.yml exec api \
    python -m cloudcost.cli reset-mfa persona@empresa.cl --reason "Verificada por videollamada, ticket 123"
  ```
  Cierra todas sus sesiones, deja el evento `MFA_DISABLED` (con el motivo y `via: operator`) en la auditoría y le avisa por correo. Después debe volver a activar el 2FA desde Cuenta y seguridad.
