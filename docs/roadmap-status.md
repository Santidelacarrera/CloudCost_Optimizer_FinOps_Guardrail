# Estado de la hoja de ruta

Qué se construyó, en qué PR, dónde se documenta y **qué no se ha podido verificar**. Los enlaces a documentos nuevos funcionan una vez fusionados los PR correspondientes; este documento debe fusionarse al final.

## Resumen

| # | Capacidad | PR | Documento | Verificado con |
|---|---|---|---|---|
| 1 | Costo real por recurso (Cost Explorer) | #9 | [runbook](runbook.md) | pruebas con respuestas simuladas; **no contra una cuenta AWS real** |
| 2 | Informe ejecutivo PDF y Excel | #10 | — | pruebas de generación; revisión visual de los archivos pendiente |
| 3 | GitLab (Merge Requests) | #11 | [runbook](runbook.md) | servidor GitLab simulado; **no contra gitlab.com/self-managed** |
| 4 | Evaluación OPA/Rego nativa antes del PR | #12 | [policies](policies.md) | pruebas unitarias e integración |
| 5 | Incorporación AWS con CloudFormation de un clic | #13 | [onboarding-aws](onboarding-aws.md) | pruebas de la plantilla; **no desplegada en una cuenta real** |
| 6 | Rotación del pepper | #14 | [pepper-rotation](pepper-rotation.md) | pruebas con dos peppers; ensayo en staging pendiente |
| 7 | CSP con nonce (sin `'unsafe-inline'` en scripts) | #15 | [security](security.md) | e2e con navegador real |
| 8 | Gráfico de ahorro proyectado | #16 | — | compilación y e2e |
| 9 | Casos de demo (RDS abandonadas, volúmenes de despliegues fallidos) | #17 | — | pruebas unitarias |
| 10 | Colectores Azure y GCP (solo lectura, REST) | #18 | [onboarding-azure-gcp](onboarding-azure-gcp.md) | respuestas simuladas; **no contra Azure/GCP reales** |
| 11 | Kubernetes/Helm (Prometheus) y parche de `values.yaml` | #19 | [kubernetes-helm](kubernetes-helm.md) | Prometheus simulado; **no contra un clúster real** |
| 12 | SSO OIDC (Entra ID / Okta) | #20 | [sso](sso.md) | IdP simulado (`tests/fake_idp.py`); **nunca contra Entra/Okta reales** (lista en sso.md §5) |
| 13 | Passkeys / WebAuthn | #21 | [passkeys](passkeys.md) | autenticador de software y virtual de Chromium; **sin llaves físicas ni Safari/Firefox** |
| 14 | Paquete de pentest externo + corrección de referencias de secreto | #22 | [pentest/](pentest/scope.md) | pruebas automáticas de aislamiento y rutas; **el pentest humano no se ha realizado** |

## Revisión de calidad: integración real, recomendaciones, aislamiento y ahorro medido
| Área | Qué se hizo | Verificado con | Pendiente |
|---|---|---|---|
| Integración AWS | Coste de la cuenta por servicio/región/período normalizado, errores clasificados, reintentos con espera exponencial, presupuesto de Cost Explorer, paginación todo-o-nada, calidad de la serie diaria, comprobación de identidad, guardia de solo lectura en código, permisos IAM reducidos a lo usado | Clientes `botocore.Stubber` (validan parámetros y respuestas contra el modelo real de la API) | **Ejecutar `aws-lab` contra una cuenta real** ([guía](aws-lab-validation.md)); inventario de RDS |
| Fuente adicional | Evaluada: no se integra ([ADR-0003](adr/0003-external-recommendation-sources.md)) | Solo se comprobó que el SDK expone los servicios; requisitos de plan sin verificar | Reabrir con datos de una cuenta real |
| Calidad de recomendaciones | Fórmulas versionadas, evidencia append-only con huella en la auditoría, estimado/aprobado/observado separados, identidad estable, sustitución, caducidad y bloqueo por cambios en curso | Pruebas unitarias y de integración con PostgreSQL real | — |
| Seguridad | Aislamiento por API en ambos sentidos; PR sin ejecución; matriz Rego (**corrige un hueco: RDS y Azure/GCP no estaban protegidos**); redacción de secretos; auditoría v2 (**corrige una ambigüedad de canonicalización**) y anclaje de la cabeza | `opa` real, concurrencia real, manipulación como propietario de la base | Anclaje externo automático de la cabeza; pentest humano |
| Ahorro observado | Línea base al aprobar y al desplegar, ventanas alineadas, control de uso y del resto del servicio, grados de confianza, informe de demostración | Pruebas puras e integración; el informe usa datos **sintéticos** | Medir un cambio real tras desplegarlo |

## Orden de fusión sugerido
1. Independientes entre sí: #9, #10, #11, #12, #13, #14, #15, #16, #22.
2. Apiladas: **#17 → #18 → #19** (cada una apunta a la anterior; al fusionar la base, GitHub reorienta la siguiente a `main`).
3. Identidad: #20 y #21 (tocan los mismos archivos: `config.py`, `docs/security.md`, `docs/launch-checklist.md`, compose, `.env.example`, `pytest.ini`; la segunda requiere resolver conflictos).
4. Este documento y el README, al final.

Conflictos previsibles: `apps/web/app/(app)/page.tsx` (#10 y #16), `schemas.py`, `docs/*`, numeración de migraciones (006 Azure/GCP, 007 K8s, 008 SSO, 009 passkeys; los huecos son tolerados) y secciones de `docker-compose*.yml`/`.env.example` entre #20 y #21.

## Qué falta (y no puede hacerse desde el repositorio)
- **Lanzamiento**: servidor y dominio, SMTP con SPF/DKIM/DMARC, revisión legal de `/terms` y `/privacy`, ensayo de restauración de copias, prueba con una cuenta AWS real, monitor externo, protección de `main` ([launch-checklist](launch-checklist.md)).
- **SSO**: probar contra un tenant real de Entra ID y de Okta ([sso.md §5](sso.md)).
- **Passkeys**: probar con dispositivos reales y **fijar el RP ID antes de invitar usuarios** (cambiarlo después invalida todas las llaves).
- **Pentest externo**: contratarlo con el paquete de [pentest/](pentest/scope.md); corregir críticos y altos antes de aceptar clientes de pago.
- **Multinube real**: validar Azure, GCP y Kubernetes contra entornos reales con datos reales.
- **Referencias de secreto**: tras #22, `env:` solo admite `CC_SECRET_*` y `aws-sm:` solo `cloudcost/<org_id>/…`. Los colectores de Azure/GCP, GitLab y Kubernetes deben llamar a `secrets.resolve(ref, org_id)` al fusionarse (sin `org_id` se aplica el prefijo pero no el aislamiento por organización).
- **GitLab**: `is_iac_file` del proveedor solo reconoce `.tf`; ampliarlo a YAML/values para los parches de Helm.
- **Dependencias web**: ejecutar `npm install` en `apps/web` (el `package-lock.json` quedó obsoleto tras el override de seguridad del PR #7).


## Actualización posterior (PR #29)
| Tema | Estado |
|---|---|
| Laboratorios de validación | `aws-lab` (cuenta real: **pendiente**, la cuenta de laboratorio aún no está activada) y `k8s-lab` (minikube: ejecutado con 30 min y 16 h de datos, cargas sintéticas) |
| Análisis de gasto desde archivo | CSV/Excel; AWS CUR y Cost Explorer, Azure, GCP, FOCUS, sinónimos y columnas a mano; pantalla con gráficos propios. Solo con archivos ficticios |
| Importación de inventario | 11 servicios (EC2, EBS, snapshots, RDS, Azure vm/disk/snapshot, GCP gce/pd/snapshot, Kubernetes) |
| RDS en el colector de AWS | Opt-in (`include_rds`), L2; Aurora no se evalúa |
| Web: tres cifras de ahorro | Estimado, aprobado y observado en la pantalla de cada recomendación (con atribución, límites y líneas base) |
| Anclaje de la auditoría | `audit-anchor`: archivo de solo-añadir encadenado + webhook + verificación; **falta programarlo y guardar el archivo fuera del servidor** (lista de lanzamiento) |
