# Seguridad

- **Solo lectura por defecto / mínimo privilegio**: el rol AWS (`infrastructure/terraform/aws-readonly-role`) no concede escrituras; se asume con `ExternalId`.
- **Secretos por referencia**: la BD guarda `env:NOMBRE` o `aws-sm:id`, nunca valores (validado en la API).
- **LLM**: sin credenciales de nube, contexto en allowlist, salida validada (JSON Schema + reglas de negocio), no ejecuta acciones. Apagado por defecto.
- **Sin auto-merge ni despliegue**: la plataforma crea PRs; el merge y el despliegue son de tu repo/CI. Destructivo en producción ⇒ aprobación reforzada.
- **Auditoría inmutable**: sin UPDATE/DELETE/TRUNCATE para el rol de aplicación (permisos + triggers), cadena sha256 verificable.
- **Multi-tenant**: RLS `FORCE` con `app.current_org` por transacción; rol `cloudcost_app` sin superusuario ni BYPASSRLS.
- **AuthN/Z**: tres modos. `dev` = JWT HS256 local; `local` = cuentas propias (abajo); `oidc` = IdP externo por JWKS. La configuración falla al arrancar si `ENV=production` con auth dev o demo activa. Roles: ADMIN, FINOPS, SRE, DEVELOPER, AUDITOR, VIEWER.
- **Web**: token en cookie `httpOnly` + `SameSite=Strict` (con HTTPS, prefijo `__Host-`), BFF con comprobación de origen, CSP y cabeceras de seguridad. El navegador nunca ve el token de sesión.
- **Webhook**: HMAC SHA-256 con comparación en tiempo constante; `org_id` en la URL solo fija el tenant.
- **`/metrics`**: restringe a red interna en el ingress. **Swagger** se desactiva en producción.

## Políticas (OPA/Rego)
Las políticas de guardrail se evalúan **dentro de la API, antes de crear el PR**, y en `enforce` fallan cerrado si el motor no responde. Detalle, modos y cómo desbloquear un PR denegado: [policies.md](policies.md).

## Cuentas propias (`AUTH_LOCAL_ENABLED`)
- **Contraseñas**: scrypt (N=2^15, r=8, p=3) sobre HMAC-SHA256 con un *pepper* del servidor (`AUTH_PEPPER`, fuera de la base): un volcado de la tabla no basta para atacarlas offline. Sal propia, formato autodescriptivo y re-hash automático si suben los parámetros. Política: ≥12 caracteres, rechaza contraseñas comunes, secuencias, repeticiones y datos personales; opcional `AUTH_HIBP_ENABLED` (k-anonimato, falla abierto). Como mucho 4 scrypt simultáneos para que un ataque de volumen no agote la memoria.
- **Sin enumeración de cuentas**: el registro, el login y la recuperación responden igual exista o no el correo (hash de relleno para igualar tiempos; el correo se envía en segundo plano).
- **Bloqueo progresivo**: 5 fallos seguidos → 30 s, 60 s, 2 min… hasta 15 min, por cuenta (también para correos inexistentes) y límites por IP, registrados en la base (valen con varias réplicas). El bloqueo no se alarga mientras dura.
- **Sesiones**: token opaco `ccs_…` de 256 bits; en la base solo su sha256. Caducidad absoluta (12 h) e inactividad (2 h), revocables, máx. 10 activas; cambiar o restablecer la contraseña cierra las demás.
- **2FA TOTP** (RFC 6238) con anti-reutilización del intervalo, secreto cifrado en reposo (AES-256-GCM, clave derivada del pepper, ligado a la cuenta) y 10 códigos de recuperación de un solo uso (solo su HMAC en la base). Desactivarlo o regenerar códigos exige contraseña y segundo factor. Tras la contraseña se emite una sesión `mfa_pending` de 10 min que no sirve para la API.
- **Correo**: enlaces de un solo uso con hash en la base (confirmar 24 h, recuperar 1 h, invitar 7 días); la confirmación se hace con un botón (los antivirus que abren enlaces no la consumen). Avisos por cambios de contraseña/2FA.
- **Aislamiento**: tablas con RLS `FORCE`; el módulo de autenticación opera con `app.auth='on'` y la gestión de equipo corre dentro del tenant. Los códigos de recuperación y los intentos solo los ve el módulo de autenticación. Auditoría: alta, confirmación, login, bloqueo, 2FA, cambios de contraseña/sesiones/equipo; nunca contraseñas ni códigos.
- **IP de cliente**: con `AUTH_TRUST_FORWARDED=true` la API toma la **última** entrada de `X-Forwarded-For`. Úsalo solo si la API únicamente es alcanzable por el BFF o un proxy propio que sobrescriba esa cabecera (en `docker-compose.yml` el puerto de la API solo se publica en 127.0.0.1).
- **Una cuenta pertenece a una organización**; una invitación solo sirve para el correo invitado.

## Producción
`ENV=production` exige `AUTH_PEPPER` propio (≥32), `SMTP_HOST` y `PUBLIC_WEB_URL` https, y se niega a arrancar con auth de desarrollo o demo activa. La web solo habilita el acceso de desarrollo con `DEV_LOGIN=dev` explícito (el compose de producción no lo define). El camino completo (TLS, copias, alertas, despliegue) está en [deployment.md](deployment.md) y la lista de comprobación en [launch-checklist.md](launch-checklist.md).

- **Pepper**: guárdalo en un gestor de secretos **y fuera del servidor**; las copias de seguridad no lo incluyen. No se puede rotar hoy sin invalidar contraseñas y 2FA (ver la lista de lanzamiento).
- **Registro**: `AUTH_SIGNUP_OPEN=false` limita la creación de cuentas a invitaciones.
- **Copias**: `pg_dump` cifrado en tránsito hacia S3 (`BACKUP_S3_URI`); cada copia se restaura en una base temporal y se verifica la cadena de auditoría. El rol que copia debe saltarse RLS.
- **Observabilidad**: métricas `http_requests_total`, `http_request_duration_seconds`, `auth_failures_total{code}` y `auth_logins_total{status}`; reglas en `infrastructure/docker/alerts.yml`. `/metrics` solo es alcanzable dentro de la red de Docker.
- **Límites conocidos**: la CSP mantiene `'unsafe-inline'` en scripts (las páginas son estáticas; los nonces exigen renderizado dinámico). Pendiente: rotación del pepper, passkeys (WebAuthn), prueba de penetración externa.
