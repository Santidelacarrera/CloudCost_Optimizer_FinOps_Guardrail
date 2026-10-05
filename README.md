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

## Analizar documentos financieros (gastos comunes, presupuestos, estados de pago de obra)
En la UI, **Analizar gastos**: sube uno o varios CSV (un mes por archivo, o una columna `mes`); detecta solo de qué tipo es cada uno.
- **Estados de gastos**: tablas planas `descripción;monto[;categoría][;mes]` o informes con secciones y filas «Sub-Total/Total» (típico de gastos comunes), incluidos los numerados (`1.` / `1.1.` / `1.1.1.`) con proveedor, N° de documento, fecha y descripción: las hojas son las partidas y cada nivel superior se comprueba contra la suma de su detalle. Separador `;` `,` tab o `|`; montos `$1.234.567` o `1,234.56`; UTF-8 o Windows-1252.
  Reglas: cuadratura de resúmenes y totales, fondo de reserva contra el % declarado, mismo documento cobrado dos veces, cobros idénticos repetidos, cobro muy superior a los demás del mismo proveedor, posible mismo beneficiario en dos secciones, concentración y, con varios meses, alzas/bajas ≥15 %, partidas nuevas y desaparecidas (no compara archivos que casi no comparten partidas).
- **Estados de pago / flujo financiero de obra** (UF u otra moneda): bloques Contrato · Proyectado actualizado · Real con `Avance % · Avance · Retenciones · Dev Anticipo · Total`. Comprueba el anticipo, que cada Total = Avance − Retención − Devolución, los totales declarados, la tabla de acumulados y la devolución de anticipo acumulada (nunca puede superar el anticipo); mide el atraso real vs proyectado, la tendencia de los últimos 3 EP y estima el término al ritmo reciente.
Todo es determinista (sin IA). Los hallazgos son pistas para revisar con el monto involucrado, no ahorros garantizados. El contenido **no se guarda** (suele traer nombres y sueldos): la auditoría registra solo cifras agregadas. API: `POST /api/v1/expenses/analyze` (roles ADMIN/FINOPS/SRE).

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
