# Seguridad

- **Solo lectura por defecto / mínimo privilegio**: el rol AWS (`infrastructure/terraform/aws-readonly-role`) no concede escrituras; se asume con `ExternalId`.
- **Secretos por referencia**: la BD guarda `env:NOMBRE` o `aws-sm:id`, nunca valores (validado en la API).
- **LLM**: sin credenciales de nube, contexto en allowlist, salida validada (JSON Schema + reglas de negocio), no ejecuta acciones. Apagado por defecto.
- **Sin auto-merge ni despliegue**: la plataforma crea PRs; el merge y el despliegue son de tu repo/CI. Destructivo en producción ⇒ aprobación reforzada.
- **Auditoría inmutable**: sin UPDATE/DELETE/TRUNCATE para el rol de aplicación (permisos + triggers), cadena sha256 verificable.
- **Multi-tenant**: RLS `FORCE` con `app.current_org` por transacción; rol `cloudcost_app` sin superusuario ni BYPASSRLS.
- **AuthN/Z**: dev = JWT HS256 local; producción = OIDC/JWKS (la configuración falla al arrancar si `ENV=production` con auth dev o demo activa). Roles: ADMIN, FINOPS, SRE, DEVELOPER, AUDITOR, VIEWER.
- **Web**: token en cookie `httpOnly` + `SameSite=Strict`, BFF con comprobación de origen, CSP y cabeceras de seguridad.
- **Webhook**: HMAC SHA-256 con comparación en tiempo constante; `org_id` en la URL solo fija el tenant.
- **`/metrics`**: restringe a red interna en el ingress. **Swagger** se desactiva en producción.

## Pendiente para producción
Configurar OIDC real (la ruta `/api/session` de la UI es solo de desarrollo), TLS en el ingress, rotación de secretos, backups de Postgres y revisión de CSP.
