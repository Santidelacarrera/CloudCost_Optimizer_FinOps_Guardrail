# Runbook

## 1. Conectar AWS (solo lectura)
```bash
cd infrastructure/terraform/aws-readonly-role
terraform init && terraform apply -var trusted_principal_arn=<ARN de CloudCost> -var external_id=<valor secreto>
```
Guarda `external_id` en tu gestor de secretos y registra la cuenta (`POST /api/v1/cloud-accounts`) con `role_arn` y `external_id_ref` = `env:NOMBRE` o `aws-sm:id`. Opcional: `-var enable_cost_explorer=true` y `AWS_COST_EXPLORER_RESOURCES=true`.
Memoria: requiere CloudWatch Agent (`CWAgent`); sin ella las reglas de downsize no se activan por falta de evidencia.

## 2. Conectar GitHub
1. Token fine-grained con permisos *Contents: write* y *Pull requests: write* solo en el repo de IaC (sin permiso de merge).
2. Registra el repo (`POST /api/v1/repositories`, `token_ref: env:GITHUB_TOKEN`).
3. Webhook (evento *Pull requests*): `https://<api>/api/v1/webhooks/github/<org_id>`, secreto = `GITHUB_WEBHOOK_SECRET`.
4. Protege `main` con revisión obligatoria y checks (incluye `opa test`/plan guardrail de `packages/policies`).

## 3. Producción
`ENV=production AUTH_MODE=oidc DEMO_ENABLED=false OIDC_ISSUER=… OIDC_AUDIENCE=… OIDC_JWKS_URL=…`. El claim de organización (`org_id`) y rol (`role`) son configurables (`OIDC_ORG_CLAIM`, `OIDC_ROLE_CLAIM`). Ejecuta `scripts/migrate.sh` con el usuario propietario antes de desplegar.

## 4. Operación
- Salud: `/health`, `/ready`. Métricas: `/metrics` (API) y puerto 9100 (worker).
- Integridad de auditoría: `GET /api/v1/audit/verify`.
- Escaneo fallido: `GET /api/v1/scans/{id}` (`error`, `attempts`); los reintentos están acotados.
