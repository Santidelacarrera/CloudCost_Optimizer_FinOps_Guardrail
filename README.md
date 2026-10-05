# CloudCost Optimizer & FinOps Guardrail

Plataforma FinOps **defensiva** para AWS: detecta desperdicio (EC2/EBS/snapshots), correlaciona IaC (Terraform), utilización y costo, explica cada hallazgo y **propone cambios mediante Pull Request**. Nunca modifica producción: todo pasa por validaciones técnicas, política y aprobación humana.

```
Detectar → Analizar → Explicar → Proponer → Validar → Aprobar → Crear PR → Desplegar → Verificar ahorro
```

## Qué incluye esta Fase 1 (MVP)

| Área | Implementación |
|---|---|
| Colectores | AWS (boto3, solo lectura: EC2, EBS, snapshots, CloudWatch, CloudTrail, AMI/Backup/DLM, Cost Explorer opcional) y **demo** sintético |
| Reglas deterministas | `ec2_downsize`, `ec2_idle`, `ebs_orphan`, `snapshot_old` → riesgo → política |
| IaC | Parser léxico de Terraform + parche mínimo por offsets; se niega ante drift, valores no literales, `count`/`for_each` o referencias |
| Aprobación | Estado explícito; producción + destructivo ⇒ **aprobación reforzada** (2 aprobadores distintos, ≥1 ADMIN/SRE), PR en borrador |
| Git | GitHub (rama + commit + PR idempotentes, **nunca merge**) y proveedor local para demo |
| Auditoría | `audit_events` append-only con **cadena de hashes** verificable (`/api/v1/audit/verify`) |
| Multi-tenant | PostgreSQL con **Row Level Security** forzada y rol sin BYPASSRLS |
| LLM (opcional) | Solo asesor: contexto en allowlist, sin credenciales, salida validada por JSON Schema y reglas de negocio |
| Observabilidad | Logs JSON, métricas Prometheus (API y worker), OpenTelemetry opcional |
| Calidad | Pruebas unitarias + integración contra Postgres real, CI (ruff, pytest, trivy, checkov, terraform validate, OPA) |

Fases posteriores (no incluidas): GitLab, Azure, GCP, Kubernetes/Helm, evaluación OPA dentro de la API, Cost Explorer por recurso por defecto.

## Arranque rápido (demo, sin cuenta AWS)

```bash
cp .env.example .env            # revisa contraseñas
docker compose up --build       # postgres, redis, migraciones+seed, api, worker, web
```
- UI: http://localhost:5985 (puertos configurables con `WEB_PORT`/`API_PORT` en `.env`) (login de desarrollo: elige rol, p. ej. `FINOPS`)
- API/Swagger: http://localhost:5986/docs
- Pulsa **Ejecutar escaneo** → 10 recursos sintéticos, 6 hallazgos, ≈ USD 777/mes.
- Flujo por consola: `pip install requests && python scripts/demo_flow.py`

En modo demo el "PR" se escribe en disco (proveedor local) y el merge se simula desde la UI.

## Desarrollo

```bash
make install
SEED_DEV=true DATABASE_ADMIN_URL=postgresql://postgres:pw@localhost:5432/cloudcost APP_DB_PASSWORD=pw2 sh scripts/migrate.sh
export DATABASE_URL=postgresql://cloudcost_app:pw2@localhost:5432/cloudcost DATABASE_ADMIN_URL=...
make test
```
Sin `DATABASE_URL` las pruebas de integración se omiten.

## Importar un CSV (sin conectar AWS)
En la UI, **Importar CSV**: descarga la plantilla, rellénala con una fila por recurso (EC2, EBS o snapshots) con su uso y costo, y súbela. Se aplican las mismas reglas, políticas, aprobaciones y PR. Excel: *Guardar como → CSV UTF-8*. Límites: 5000 filas / 2 MB. Un presupuesto genérico sin recursos ni uso no permite detectar desperdicio. API: `POST /api/v1/imports`.

## Uso real con AWS y GitHub
Ver [docs/runbook.md](docs/runbook.md) (rol IAM de solo lectura con Terraform en `infrastructure/terraform/aws-readonly-role`, token y webhook de GitHub, OIDC).

## Documentación
- [Arquitectura](docs/architecture.md) · [Seguridad](docs/security.md) · [Runbook](docs/runbook.md) · [ADRs](docs/adr/)

## Estructura
```
apps/api            FastAPI + Celery (paquete cloudcost: domain, iac, git, llm, collectors, services, routers)
apps/web            Next.js 15 (BFF con cookie httpOnly)
migrations          SQL (esquema, RLS, auditoría) + seed de desarrollo
packages/policies   Políticas OPA (Rego) para planes de Terraform
infrastructure      Docker, Terraform (rol de solo lectura, IaC de ejemplo), Kubernetes (Fase 2)
scripts             migrate.sh, demo_flow.py, dev_token.py
tests               unit/ e integration/
```
> No se incluye licencia: añade la que corresponda a tu organización.
