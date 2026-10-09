<div align="center">


# CloudCost Optimizer & FinOps Guardrail 💸🛡️

> **Detecta el desperdicio en tu infraestructura cloud y conviértelo en ahorro mediante un Pull Request. Riesgo cero. Cero permisos de escritura en producción.**

AWS · Azure · GCP · Kubernetes — Terraform · Helm — GitHub · GitLab

---

## 🛑 El Problema: El dilema del FinOps tradicional

Las empresas hoy pierden miles de dólares al mes en instancias EC2 sobredimensionadas, bases de datos ociosas y discos huérfanos. ¿El motivo? **A los equipos de infraestructura les aterra apagar recursos y romper producción.**

Las herramientas FinOps del mercado te obligan a elegir entre dos opciones inaceptables:
1. **El Dashboard Pasivo:** Te muestran un gráfico alarmante diciendo *"Tienes USD 40,000 de desperdicio"*, pero te dejan el trabajo manual de buscar el código de Terraform y parchearlo.
2. **El "God-Mode" Inseguro:** Te ofrecen un botón de *"Auto-Remediar"* que exige darle permisos de escritura en producción a una herramienta de terceros. Ningún CTO en su sano juicio aprueba esto en una empresa con auditorías estrictas.

## 💡 La Solución: FinOps Defensivo y GitOps

**CloudCost Optimizer** adopta una postura radicalmente distinta: **la plataforma nunca escribe en tu nube.** 

Operamos bajo el principio de *Mínimo Privilegio (Solo Lectura)*. La plataforma escanea tu nube, correlaciona el costo real con la utilización, lee tu código de infraestructura (IaC) y **genera automáticamente el parche de código como un Pull Request en tu repositorio**.

Tú lo revisas. Tú lo apruebas. Tú haces el merge usando tus propios pipelines (CI/CD). La responsabilidad y el control nunca salen de tu equipo.

## 🚀 Valor de Negocio (Por qué CloudCost)

* 💰 **ROI Inmediato y Explicable:** No arrojamos alertas vacías. Cada hallazgo incluye la métrica de uso exacta, el costo financiero real asociado y el ahorro proyectado tras aplicar el parche.
* 🛡️ **Seguridad Zero Trust:** Funciona 100% con roles de *Solo Lectura*. Si nos hackean, tus servidores de producción en AWS/Azure están intactos porque no tenemos la llave para apagarlos.
* 🤝 **Sin Fricción Operativa:** No obligamos a tus ingenieros a usar una plataforma nueva para aplicar cambios. Les entregamos el trabajo hecho directamente en GitHub o GitLab.
* ⚖️ **Gobernanza y Compliance:** Auditoría inmutable encadenada por hashes (append-only) y aprobación reforzada. Las acciones destructivas requieren la firma digital de dos personas (incluyendo un rol SRE/Admin) antes de liberar el PR.

---

### ⚙️ El Pipeline de Optimización
`Detectar → Analizar → Explicar → Proponer → Validar (OPA/Rego) → Aprobar (Humano) → Crear PR → Desplegar (Tu CI/CD) → Verificar Ahorro`

**Detecta el desperdicio en tu infraestructura cloud y lo corrige por Pull Request — sin tocar producción jamás.**

[![CI](https://github.com/Santidelacarrera/CloudCost_Optimizer_FinOps_Guardrail/actions/workflows/ci.yml/badge.svg)](https://github.com/Santidelacarrera/CloudCost_Optimizer_FinOps_Guardrail/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![Next.js](https://img.shields.io/badge/Next.js-15-000000?logo=nextdotjs&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16%20·%20RLS-4169E1?logo=postgresql&logoColor=white)
![OPA](https://img.shields.io/badge/policy-OPA%20%2F%20Rego-7D9199?logo=openpolicyagent&logoColor=white)

AWS · Azure · GCP · Kubernetes — Terraform · Helm — GitHub · GitLab

</div>

```
Detectar → Analizar → Explicar → Proponer → Validar → Aprobar → Crear PR → Desplegar → Verificar ahorro
```

---

## Contenido
[Por qué existe](#por-qué-existe) · [Garantías de diseño](#garantías-de-diseño) · [Cómo funciona](#cómo-funciona) · [Qué detecta](#qué-detecta) · [Capacidades](#capacidades) · [Arquitectura](#arquitectura) · [Arranque rápido](#arranque-rápido-demo-sin-cuenta-cloud) · [Conectar un entorno real](#conectar-un-entorno-real) · [Seguridad](#seguridad) · [Calidad y CI](#calidad-y-ci) · [Configuración](#configuración) · [Desarrollo](#desarrollo) · [Estado y límites](#estado-y-límites-conocidos) · [Documentación](#documentación)

---

## Por qué existe

Las herramientas FinOps suelen quedarse en el tablero («tienes USD 40 000 de desperdicio») o, en el otro extremo, ejecutan cambios con credenciales de escritura en producción. Ninguna de las dos cosas escala en una organización con equipos, auditoría y gestión del cambio.

CloudCost adopta una postura distinta: **la plataforma nunca escribe en la nube.** Solo lee (con roles de solo lectura), correlaciona **costo real + utilización + configuración IaC** y convierte cada hallazgo en un **Pull Request revisable** sobre el repositorio de infraestructura del cliente. El merge, el despliegue y la responsabilidad del cambio siguen en el flujo que el cliente ya tiene.

| Lo que ves en otras herramientas | Lo que hace CloudCost |
|---|---|
| «Esta instancia parece ociosa» | Evidencia explicable: CPU/memoria observadas, días de ventana, costo real del recurso, alternativas más seguras |
| Botón «aplicar» con permisos de escritura | Parche mínimo de Terraform/Helm en un PR en borrador; **nunca** merge ni despliegue automático |
| Aprobación en un chat | Máquina de estados con aprobación reforzada (dos personas, una con rol ADMIN/SRE) para cambios destructivos en producción |
| Políticas que dependen del CI del cliente | Políticas **OPA/Rego evaluadas por la propia plataforma antes de crear el PR**, fallando cerrado |
| Log de actividad editable | Auditoría **append-only encadenada por hash**, verificable por API y tras cada restauración de copia |
| «Ahorramos X» | Verificación posterior: ahorro esperado vs. observado tras el despliegue |

---

## Garantías de diseño

Estas son invariantes del sistema, no buenas intenciones; cada una tiene una prueba que la sostiene.

1. **Solo lectura hacia la nube.** Roles IAM/RBAC de solo lectura con `ExternalId` anti confused-deputy (AWS) y plantillas de Terraform/CloudFormation incluidas. Las credenciales se guardan **por referencia** (`env:CC_SECRET_*`, `aws-sm:cloudcost/<org_id>/…`), nunca por valor, y cada referencia queda confinada al espacio de nombres de su organización.
2. **Nunca merge, nunca despliegue.** La plataforma crea ramas, commits y PR (idempotentes). Lo destructivo en producción abre el PR en **borrador**.
3. **Humano en el circuito.** Nada avanza de `PENDING_APPROVAL` sin aprobación; las aprobaciones cuentan por versión de la recomendación y se invalidan si el hallazgo cambia materialmente.
4. **Los parches se niegan antes de equivocarse.** Si hay *drift* entre IaC y nube, valores no literales, `count`/`for_each` o referencias cruzadas, no se genera parche: se explica por qué.
5. **Aislamiento multi-tenant en la base de datos.** Row Level Security `FORCE` con contexto por transacción; el rol de aplicación no es superusuario ni `BYPASSRLS`. Una prueba por introspección falla si aparece una tabla sin RLS.
6. **Auditoría inmutable.** Sin `UPDATE`/`DELETE`/`TRUNCATE` para el rol de aplicación (permisos + triggers) y cadena SHA-256 verificable.
7. **El LLM no decide.** Opcional y apagado por defecto: sin credenciales de nube, contexto en allowlist, salida validada por JSON Schema y reglas de negocio. Redacta; jamás aprueba ni ejecuta.
8. **Producción no arranca insegura.** `ENV=production` rechaza arrancar con autenticación de desarrollo, demo activa, pepper por defecto o URLs sin HTTPS.

---

## Cómo funciona

```mermaid
flowchart LR
  subgraph DA["Detectar y analizar"]
    C[Colectores de solo lectura<br/>AWS · Azure · GCP · Kubernetes · CSV] --> N[Normalización<br/>recursos + métricas + costo real]
    N --> R[Reglas deterministas<br/>→ riesgo → política]
  end
  R --> E[Explicación<br/>evidencia + alternativas]
  E -.->|opcional, validado| L[LLM asesor]
  E --> A{Aprobación humana<br/>estándar o reforzada}
  A -->|aprobado| P[Parche mínimo<br/>Terraform / Helm values]
  P --> O{OPA / Rego<br/>enforce}
  O -->|permitido| PR[Rama + commit + PR<br/>GitHub · GitLab]
  O -->|denegado| X[422 — nada se crea]
  PR --> M[Merge y despliegue<br/>en el flujo del cliente]
  M --> V[Verificación de ahorro<br/>esperado vs. observado]
```

**Máquina de estados** de cada recomendación:
`DETECTED → ANALYZED → PROPOSED → PENDING_APPROVAL → APPROVED | REJECTED → PR_CREATED → MERGED → DEPLOYED → VERIFIED`
(una recomendación que la política no permite se queda en `PROPOSED`; un PR cerrado sin merge vuelve a `APPROVED`).

**Aprobación.** Estándar: un aprobador (ADMIN, FINOPS o SRE). Reforzada —acciones destructivas en producción o entorno desconocido, o riesgo ALTO—: dos usuarios distintos, al menos uno ADMIN/SRE, PR en borrador y automatización bloqueada. Un entorno ausente o no reconocido (`Prod-EU`) cuenta como producción: es la opción prudente.

---

## Qué detecta

Reglas deterministas, explicables y con umbrales configurables (`RuleConfig`); ninguna ejecuta nada.

| Regla | Detecta | Acción propuesta |
|---|---|---|
| `ec2_downsize` | Instancia sobredimensionada (CPU y memoria bajo umbral durante la ventana, con proyección de uso en el tipo destino) | Reducir hasta 2 tamaños |
| `ec2_idle` | Instancia ociosa (CPU media y pico mínimos) | Eliminar *(destructiva; alternativa: reducir)* |
| `ebs_orphan` | Volumen sin adjuntar ≥ 14 días | Eliminar *(alternativa: snapshot final)* |
| `snapshot_old` | Snapshot antiguo no gestionado por AWS Backup/DLM/Azure Backup ni usado por una imagen | Eliminar *(ahorro como cota superior)* |
| `rds_idle` | Base de datos RDS sin conexiones y CPU en reposo toda la ventana *(hoy solo con datos de demostración: el colector real de AWS aún no inventaría RDS)* | Eliminar *(destructiva)* |
| `k8s_overprovisioned` | Workload con *requests* de CPU/memoria muy por encima del uso | Ajustar `values.yaml` de Helm |

El **ahorro** se calcula con el **costo real de cada recurso** (Cost Explorer a nivel de recurso en AWS) y, si no está disponible, con una tabla de precios marcada como *«Costo ESTIMADO»*. Una política estricta (`require_verified_cost`) impide proponer apagar o reducir recursos cuyo costo solo sea estimado.

---

## Capacidades

| Área | Qué incluye |
|---|---|
| **Nubes** | **AWS** (EC2, EBS y snapshots, con CloudWatch, CloudTrail, AMI/Backup/DLM y Cost Explorer por recurso con historial por etiqueta) · **Azure** y **GCP** (REST de solo lectura) · **Kubernetes** (Prometheus + kube-state-metrics) · **CSV** importado · **demo** sintético |
| **Onboarding** | CloudFormation de un clic para AWS · módulos de Terraform de solo lectura para AWS, Azure y GCP |
| **IaC** | Parser léxico de Terraform y parche mínimo por *offsets* (el resultado debe volver a parsear) · parche de `values.yaml` de Helm |
| **Git** | GitHub y GitLab (Merge Requests), con webhooks autenticados · proveedor local para la demo |
| **Políticas** | OPA/Rego nativo en la API (`enforce` / `audit` / `off`, falla cerrado), contexto de la plataforma (`reinforced_approval`, `change_ticket`) y políticas propias del cliente |
| **Informes** | Informe ejecutivo PDF y Excel · gráfico de ahorro proyectado (gasto actual vs. optimizado) · análisis de documentos financieros (gastos comunes, estados de pago de obra) |
| **Identidad** | Cuentas propias (scrypt + *pepper*, bloqueo progresivo, TOTP + códigos de recuperación, sesiones revocables) · **passkeys / WebAuthn** · **SSO OIDC** (Entra ID, Okta) con mapeo de roles · invitaciones con rol · rotación del *pepper* sin invalidar credenciales |
| **Roles** | `ADMIN` · `FINOPS` · `SRE` · `DEVELOPER` · `AUDITOR` · `VIEWER` (RBAC verificado en CI sobre **todas** las rutas) |
| **Operación** | Logs JSON · métricas Prometheus (API y worker) + reglas de alerta · OpenTelemetry opcional · copias a S3 con **restauración de prueba y verificación de la cadena de auditoría** · despliegue con Caddy (TLS automático) |

---

## Arquitectura

Monolito modular ([ADR-0001](docs/adr/0001-modular-monolith.md)): un paquete Python (`cloudcost`) lo comparten la API y el worker; la separación en servicios llegará cuando el dominio se estabilice, no antes.

```mermaid
flowchart LR
  B[Navegador] -->|cookie httpOnly · SameSite=Strict| W[Next.js 15<br/>BFF + UI]
  W -->|Bearer opaco, red interna| API[FastAPI]
  API --> PG[(PostgreSQL 16<br/>RLS FORCE)]
  API -->|encola| RD[(Redis)] --> WK[Worker Celery]
  WK -->|solo lectura| CL[(AWS · Azure · GCP<br/>Prometheus)]
  WK -->|lee IaC| GIT[(GitHub · GitLab)]
  API -->|rama + commit + PR<br/>nunca merge| GIT
  GIT -->|webhook firmado| API
  API -->|opa eval| OPA[[OPA / Rego]]
  WK -.->|contexto en allowlist| LLM[LLM asesor<br/>opcional]
```

| Capa | Tecnología y decisión |
|---|---|
| API | FastAPI + psycopg 3; transacciones con contexto de tenant (`tenant_tx`) y de autenticación (`auth_tx`) |
| Worker | Celery + Redis ([ADR-0002](docs/adr/0002-celery-mvp.md)); la red (colectores) queda fuera de las transacciones |
| Web | Next.js 15 como *backend for frontend*: el navegador nunca ve el token de sesión; CSP con *nonce* |
| Datos | PostgreSQL 16; migraciones SQL versionadas (`migrations/`), RLS y auditoría en el propio esquema |
| Políticas | `packages/policies/*.rego` con pruebas `opa test`; ejecutadas con el binario oficial (subproceso sin shell, límites de tiempo y tamaño, entorno sin secretos) |
| Despliegue | Docker Compose para desarrollo y producción (`docker-compose.prod.yml` + Caddy), imágenes con escaneo Trivy |

Detalle: [docs/architecture.md](docs/architecture.md).

---

## Arranque rápido (demo, sin cuenta cloud)

Requisitos: Docker con Compose.

```bash
cp .env.example .env            # revisa las contraseñas
docker compose up --build       # postgres, redis, migraciones + seed, api, worker, web
```

| Qué | Dónde |
|---|---|
| Interfaz | http://localhost:5985 |
| API / Swagger | http://localhost:5986/docs |

1. Crea tu cuenta con **Crear cuenta** (sin servidor de correo, el enlace de confirmación aparece en pantalla) o entra con el acceso de desarrollo eligiendo un rol (p. ej. `FINOPS`).
2. Pulsa **Ejecutar escaneo**: **21 recursos sintéticos de AWS, 13 hallazgos, ≈ USD 1.573/mes** de ahorro estimado.
3. Abre una recomendación, revisa la evidencia, **apruébala** y **crea el PR**: en modo demo se escribe en disco (proveedor local) y el merge se simula desde la UI.
4. Revisa **Auditoría** y verifica la cadena (`GET /api/v1/audit/verify`).

Flujo completo por consola: `pip install requests && python scripts/demo_flow.py`.
Puertos configurables con `WEB_PORT` / `API_PORT`.

---

## Conectar un entorno real

| Quiero conectar… | Guía |
|---|---|
| AWS (rol de solo lectura, CloudFormation o Terraform, GitHub/GitLab, webhooks) | [Runbook](docs/runbook.md) · [Onboarding AWS](docs/onboarding-aws.md) |
| Azure y GCP | [docs/onboarding-azure-gcp.md](docs/onboarding-azure-gcp.md) |
| Kubernetes / Helm | [docs/kubernetes-helm.md](docs/kubernetes-helm.md) |
| Políticas propias de OPA | [docs/policies.md](docs/policies.md) |
| SSO (Entra ID / Okta) y passkeys | [docs/sso.md](docs/sso.md) · [docs/passkeys.md](docs/passkeys.md) |
| Producción (dominio, TLS, copias, alertas) | [docs/deployment.md](docs/deployment.md) · [docs/email.md](docs/email.md) · [docs/launch-checklist.md](docs/launch-checklist.md) |

También puedes **importar un CSV** (instancias EC2, volúmenes EBS o snapshots con uso y costo; hasta 5 000 filas / 2 MB) y se aplican las mismas reglas, políticas y aprobaciones, o **analizar documentos financieros** (estados de gastos, estados de pago de obra) con reglas deterministas de cuadratura y anomalías; ese contenido **no se guarda**: la auditoría registra solo cifras agregadas.

---

## Seguridad

El diseño de seguridad asume un entorno hostil y opera bajo el principio de **Fail-Closed**. Todo el sistema está diseñado para proteger la infraestructura del cliente, aislar a los tenants y prevenir la escalada de privilegios.

**Aspectos destacados:**
- **Solo lectura hacia la nube:** Roles IAM/RBAC sin permisos de escritura, asegurados mediante `ExternalId`.
- **Criptografía robusta:** Contraseñas en *scrypt*, protección contra ataques *Mix-up* en SSO/OIDC, y soporte nativo para **Passkeys (WebAuthn)**.
- **Aislamiento Multi-Tenant:** Implementado mediante Row Level Security (`RLS FORCE`) en la base de datos y resolución de secretos por referencia.
- **Auditoría inmutable:** Cadena SHA-256 verificable (append-only) protegida por triggers en el motor de base de datos.
- **Prueba de penetración:** El repositorio incluye el paquete para auditarlo (alcance, modelo STRIDE y checklist con cobertura automática).

> 🔒 **Para un análisis profundo del modelo de amenazas, criptografía, autenticación y mitigaciones, lee nuestro [Whitepaper de Seguridad y Modelo de Amenazas](docs/security.md).**

*Si encuentras una vulnerabilidad, no publiques los detalles en una incidencia abierta: avisa a los mantenedores por un canal privado.*

---

## Calidad y CI

Más de 370 pruebas entre unitarias, de integración contra **PostgreSQL real** y de punta a punta con navegador (Playwright). Cada PR ejecuta:

| Job | Qué valida |
|---|---|
| `api` | `ruff`, migraciones, `pytest` (incluye aislamiento RLS, matriz de rutas/roles, auditoría y OPA real) |
| `web` | `tsc --noEmit` y `next build` |
| `e2e` | Pila completa en Docker con navegador real y buzón de correo de prueba (cuenta, 2FA, passkeys, informes, gráfico, cabeceras de seguridad) |
| `policies` | `opa check --strict`, `opa fmt` y `opa test` de las políticas Rego; `terraform validate` de los módulos de solo lectura |
| `security` | Trivy (HIGH/CRITICAL, bloquea) y Checkov (informativo) |
| `backups` | Copia → restauración en base temporal → verificación de la cadena de auditoría (incluye copia dañada y auditoría alterada) |
| `docker` | Construcción de imágenes y validación de `docker-compose.prod.yml` |
| `cloudformation` | `cfn-lint` de la plantilla de onboarding y paridad con el módulo de Terraform |

---

## Configuración

Las variables completas están comentadas en [`.env.example`](.env.example) y [`.env.production.example`](.env.production.example). Las que más importan:

| Variable | Para qué | Por defecto |
|---|---|---|
| `ENV` | `development` · `test` · `production` (activa las guardas de arranque) | `development` |
| `AUTH_MODE` / `AUTH_LOCAL_ENABLED` | `dev`, `oidc` o `local`; cuentas propias | `dev` / `true` |
| `AUTH_PEPPER`, `AUTH_PEPPER_ID`, `AUTH_PEPPER_PREVIOUS` | Secreto de contraseñas y su rotación | valor de desarrollo (prohibido en producción) |
| `AUTH_SIGNUP_OPEN` | `false`: solo se crean cuentas por invitación | `true` |
| `SSO_ENABLED`, `SSO_PROVIDER`, `SSO_ROLE_MAP` | SSO OIDC (Entra / Okta) | apagado |
| `PASSKEYS_ENABLED`, `WEBAUTHN_RP_ID` | Passkeys (fija el RP ID antes de invitar usuarios) | encendido |
| `OPA_MODE` | `enforce` · `audit` · `off` | `audit` en desarrollo, `enforce` en producción |
| `GITHUB_TOKEN` / `GITLAB_TOKEN` + secretos de webhook | Proveedores Git | — |
| `AWS_COST_EXPLORER_RESOURCES`, `AWS_COST_TAG_KEY` | Costo real por recurso e historial por etiqueta | `true` / — |
| `LLM_ENABLED`, `LLM_PROVIDER` | Asesor opcional (OpenAI o Gemini) | `false` |
| `DEMO_ENABLED` | Colector y proveedor Git de demostración | `true` (prohibido en producción) |
| `SMTP_*`, `PUBLIC_WEB_URL` | Correo transaccional y URL pública (HTTPS en producción) | — |

---

### Capturas para el README
Con la demo en marcha (`docker compose up --build`, `AUTH_MODE=dev` en `.env`), genera las imágenes de `docs/images/` automáticamente:

```bash
cd apps/web
npm install
npx playwright install chromium
npm run screenshots          # entra como demostración, ejecuta un escaneo y guarda 10 PNG
```
Si cambias el puerto, usa `E2E_BASE_URL=http://localhost:<WEB_PORT>`. Luego referencia las imágenes así:

```markdown
![Panel](docs/images/03-panel.png)
![Recomendaciones](docs/images/04-recomendaciones.png)
![Detalle de una recomendación](docs/images/05-detalle-recomendacion.png)
```

## Desarrollo

```bash
make install                    # dependencias Python (requirements-dev.txt)
make lint                       # ruff check apps tests
SEED_DEV=true DATABASE_ADMIN_URL=postgresql://postgres:pw@localhost:5432/cloudcost \
  APP_DB_PASSWORD=pw2 sh scripts/migrate.sh
export DATABASE_URL=postgresql://cloudcost_app:pw2@localhost:5432/cloudcost
export DATABASE_ADMIN_URL=postgresql://postgres:pw@localhost:5432/cloudcost
make test                       # pytest
```

Sin `DATABASE_URL` / `DATABASE_ADMIN_URL` las pruebas de integración se omiten (las unitarias no necesitan base de datos). Las pruebas de OPA usan el binario real: instala [`opa`](https://www.openpolicyagent.org/docs/latest/#running-opa) para correrlas en local. El front vive en `apps/web` (`npm install && npm run dev`; el e2e con `npx playwright test`).

**Convenciones del repositorio**
- Una ruta nueva debe clasificarse en `tests/integration/test_route_auth.py`; una tabla nueva debe tener RLS (la prueba por introspección lo exige).
- Todo `secrets.resolve(...)` recibe el `organization_id` del dueño de la referencia (hay una prueba estática).
- Un hallazgo de seguridad corregido añade una prueba de regresión y actualiza el [modelo de amenazas](docs/pentest/threat-model.md).

---

## Estado y límites conocidos

Esto es lo que **no** está verificado, dicho sin adornos (detalle en [docs/roadmap-status.md](docs/roadmap-status.md)):

- **Contra sistemas reales:** el colector de AWS, Azure, GCP y Kubernetes se probó con respuestas simuladas y datos de demostración/importados; **falta una prueba con cuentas reales**. SSO nunca se ejecutó contra un tenant real de Entra ID u Okta (hay una lista de comprobación en [docs/sso.md](docs/sso.md)). Passkeys solo con autenticadores de software y el virtual de Chromium: sin llaves físicas ni Safari/Firefox.
- **Sin pentest humano** (ver [Seguridad](#seguridad)).
- **Cobertura de inventario:** el colector real de AWS lee EC2, EBS y snapshots; RDS y otros servicios solo existen hoy en la demostración.
- **Verificación de ahorro** compara costos posteriores con la estimación; no sustituye a una conciliación de facturación.
- **Alcance de IaC:** Terraform (literales) y `values.yaml` de Helm; no hay parches para CDK, Pulumi, Bicep ni manifiestos YAML sueltos.
- **Operación pendiente de tu lado:** dominio y servidor, SMTP con SPF/DKIM/DMARC, revisión legal de `/terms` y `/privacy`, ensayo de restauración y monitor externo ([lista de lanzamiento](docs/launch-checklist.md)).
- Un mismo *pepper* firma todo el estado criptográfico de contraseñas y TOTP: guárdalo fuera del servidor ([rotación](docs/pepper-rotation.md)).

---

## Documentación

| | |
|---|---|
| **Diseño** | [Arquitectura](docs/architecture.md) · [ADRs](docs/adr/) · [Seguridad](docs/security.md) · [Estado de la hoja de ruta](docs/roadmap-status.md) |
| **Operación** | [Runbook](docs/runbook.md) · [Despliegue](docs/deployment.md) · [Correo](docs/email.md) · [Lista de lanzamiento](docs/launch-checklist.md) · [Rotación del pepper](docs/pepper-rotation.md) |
| **Integraciones** | [AWS](docs/onboarding-aws.md) · [Azure y GCP](docs/onboarding-azure-gcp.md) · [Kubernetes/Helm](docs/kubernetes-helm.md) · [Políticas OPA](docs/policies.md) · [SSO](docs/sso.md) · [Passkeys](docs/passkeys.md) |
| **Seguridad externa** | [Alcance](docs/pentest/scope.md) · [Modelo de amenazas](docs/pentest/threat-model.md) · [Checklist](docs/pentest/checklist.md) |

### Estructura del repositorio
```
apps/api              FastAPI + Celery — paquete cloudcost:
                        domain/ (reglas, riesgo, precios)  collectors/ (aws, azure, gcp, kubernetes, demo, import)
                        iac/ (Terraform, Helm, parches)    git/ (github, gitlab, local)    guardrail.py (OPA)
                        auth/ (scrypt, TOTP, WebAuthn, OIDC)  services/  routers/  reports/  llm/
apps/web              Next.js 15 — UI + BFF (cookies httpOnly), pruebas e2e con Playwright
migrations            SQL versionado: esquema, RLS, auditoría, cuentas, SSO, passkeys + seed de desarrollo
packages/policies     Políticas OPA/Rego y sus pruebas
infrastructure        Terraform (roles de solo lectura AWS/Azure/GCP, IaC de ejemplo), CloudFormation, Docker, Caddy, alertas
scripts               migrate, backup/restore/verify, demo_flow, dev_token
tests                 unit/ e integration/
```

> No se incluye licencia: añade la que corresponda a tu organización.
