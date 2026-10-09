# Inicio de sesión único (SSO) con Microsoft Entra ID u Okta

CloudCost acepta OpenID Connect (código de autorización + PKCE). El personal entra con la cuenta de su empresa; el segundo factor, las
bajas y las políticas de acceso las gestiona tu proveedor de identidad (IdP). Las cuentas con contraseña propia siguen funcionando salvo que
las desactives (`AUTH_LOCAL_ENABLED=false`).

> Estado de la verificación: el protocolo, la validación del ID token, el mapeo de roles y las reglas de cuentas están cubiertos por
> pruebas automáticas contra un IdP **simulado** (`tests/unit/test_sso_oidc.py`, `tests/integration/test_sso_db.py`). Aún no se probó
> contra un tenant real de Entra ni de Okta: haz la comprobación de la sección final antes de abrirlo a tu equipo.

## Cómo funciona

```
Navegador ──▶ /api/auth/sso/start ──▶ API: state + nonce + PKCE, token de flujo firmado
        ◀── 303 al IdP  (+ cookie httpOnly cc_sso con el token de flujo)
IdP ──▶ /api/auth/sso/callback?code&state ──▶ API: valida cookie+state, canjea el código, valida el ID token
        ◀── sesión ccs_… en cookie httpOnly (el JavaScript nunca ve ningún token)
```

La dirección de retorno que debes registrar en el IdP es **`<PUBLIC_WEB_URL>/api/auth/sso/callback`**
(p. ej. `https://app.acme.com/api/auth/sso/callback`). `PUBLIC_WEB_URL` debe ser `https://` en producción.

## 1. Microsoft Entra ID

1. *Entra admin center → Identidad → Aplicaciones → Registros de aplicaciones → Nuevo registro.*
   Cuentas de **este directorio organizativo únicamente** (un solo inquilino). Plataforma «Web» con la URI de redirección anterior.
2. Copia el **Id. de aplicación (cliente)** → `SSO_CLIENT_ID` y el **Id. de directorio (inquilino)** → `SSO_TENANT_ID`.
3. *Certificados y secretos → Nuevo secreto de cliente* → `SSO_CLIENT_SECRET` (guárdalo en tu gestor de secretos; vence: pon un recordatorio).
4. *Configuración de token → Agregar notificación opcional → ID* → `email` y, si puedes, `xms_edov` (correo con dominio verificado).
5. **Roles** (recomendado, sin límite de grupos): *Roles de aplicación → Crear rol de aplicación* con valores como `cloudcost-admin`,
   `cloudcost-finops`, `cloudcost-viewer`; asígnalos en *Aplicaciones empresariales → CloudCost → Usuarios y grupos*.
   Los roles llegan en el claim `roles`, que es el que usa CloudCost con Entra por defecto.
   *(Alternativa: grupos. Configura «Grupos asignados a la aplicación» y usa `SSO_ROLE_CLAIM=groups` con los GUID de los grupos. Con
   muchos grupos Entra no los incluye en el token («overage») y el acceso se rechaza si no hay `SSO_DEFAULT_ROLE`.)*
6. *Aplicaciones empresariales → CloudCost → Propiedades → Asignación requerida = Sí*: solo entra quien esté asignado.

```env
SSO_ENABLED=true
SSO_PROVIDER=entra
SSO_ISSUER=https://login.microsoftonline.com/<TENANT_ID>/v2.0
SSO_TENANT_ID=<TENANT_ID>
SSO_CLIENT_ID=<id de aplicación>
SSO_CLIENT_SECRET=<secreto>
SSO_ORG_ID=<UUID de la organización>
SSO_ROLE_MAP=cloudcost-admin:ADMIN,cloudcost-finops:FINOPS,cloudcost-viewer:VIEWER
```

## 2. Okta

1. *Applications → Create App Integration → OIDC → Web Application.* Grant type «Authorization Code»; *Sign-in redirect URI* la de arriba.
2. Copia **Client ID** y **Client secret**. El emisor es el de tu servidor de autorización: normalmente
   `https://<org>.okta.com/oauth2/default` (el de «Org» sin `/oauth2/...` no emite el claim `groups` personalizado).
3. *Security → API → Authorization Servers → default → Claims → Add claim*: nombre `groups`, tipo *ID Token*, «Groups» que coincidan con
   `cloudcost-.*` (expresión regular). Así solo viajan los grupos de CloudCost.
4. *Assignments*: asigna la aplicación solo a los grupos que deban entrar. Exige MFA en la *Authentication policy* de la aplicación.

```env
SSO_ENABLED=true
SSO_PROVIDER=okta
SSO_ISSUER=https://<org>.okta.com/oauth2/default
SSO_CLIENT_ID=<client id>
SSO_CLIENT_SECRET=<client secret>
SSO_ORG_ID=<UUID de la organización>
SSO_ROLE_MAP=cloudcost-admins:ADMIN,cloudcost-finops:FINOPS,cloudcost-readers:VIEWER
SSO_REQUIRED_AMR=mfa            # opcional: rechaza si el IdP no declara un segundo factor
```

## 3. Variables

| Variable | Por defecto | Para qué sirve |
|---|---|---|
| `SSO_ENABLED` | `false` | Activa el botón «Entrar con …» y los endpoints. |
| `SSO_PROVIDER` | `entra` | `entra`, `okta` o `generic` (cualquier OIDC estándar). |
| `SSO_ISSUER` | — | URL https del emisor; debe coincidir exactamente con el `iss` del ID token. Entra: con tu inquilino, nunca `common`. |
| `SSO_TENANT_ID` | — | Solo Entra: GUID del directorio; se comprueba el claim `tid`. |
| `SSO_CLIENT_ID` / `SSO_CLIENT_SECRET` | — | Credenciales de la aplicación registrada. |
| `SSO_ORG_ID` | — | UUID de la organización de CloudCost a la que entra este IdP (una organización por IdP). |
| `SSO_ALLOWED_DOMAINS` | vacío | Dominios de correo permitidos, separados por comas. Vacío = cualquier dominio de tu inquilino/Okta. |
| `SSO_ROLE_CLAIM` | `roles` (Entra) / `groups` (Okta) | Claim de donde salen los grupos o roles. |
| `SSO_ROLE_MAP` | vacío | `valor:ROL,valor:ROL`. Si una persona coincide con varios, gana el de más privilegio (ADMIN > FINOPS > SRE > DEVELOPER > AUDITOR > VIEWER). |
| `SSO_DEFAULT_ROLE` | `VIEWER` | Rol sin coincidencia. Vacío = rechazar el acceso (recomendado si el IdP ya filtra quién entra). |
| `SSO_JIT` | `true` | Crea la cuenta en el primer acceso. `false`: solo entran cuentas ya vinculadas. |
| `SSO_LINK_EXISTING` | `false` | Permite que una cuenta local con el mismo correo pase a SSO (ver abajo). |
| `SSO_REQUIRED_AMR` | vacío | Valores de `amr` aceptados (p. ej. `mfa`): exige que el IdP declare un segundo factor. |
| `SSO_LABEL` | nombre del proveedor | Texto del botón. |
| `AUTH_LOCAL_ENABLED` | `true` | `false` para dejar el SSO como única vía. Con `AUTH_MODE=local`. No exige SMTP. |

`AUTH_PEPPER` debe ser un secreto propio (≥32 caracteres) en producción: de él se deriva la clave que firma el token de flujo.

## 4. Cuentas y roles: reglas que conviene conocer

- **Se identifica por (emisor, sub), no por correo.** En Entra el correo es un atributo editable: si se enlazara por correo, alguien con
  permiso para cambiar su propio correo podría quedarse con la cuenta de otra persona (ataque «nOAuth»).
- **Un correo ya registrado no se reasigna.** Sin `SSO_LINK_EXISTING`, quien intente entrar con un correo que ya existe en CloudCost
  recibe «Ya existe una cuenta…». Para migrar cuentas locales: activa `SSO_LINK_EXISTING=true`, pide que cada persona entre una vez con SSO y
  desactívalo después. Solo se enlaza si el IdP afirma que verificó el correo (`email_verified` en Okta, `xms_edov` en Entra) y la cuenta es
  de la misma organización. Al enlazar, la cuenta **pierde su contraseña y su 2FA locales** y se cierran sus sesiones.
- **El rol lo manda el IdP.** Se recalcula en cada inicio de sesión: un cambio manual en *Equipo* se pisa en el siguiente acceso. Quitar a
  alguien de un grupo surte efecto en su próximo inicio de sesión (las sesiones abiertas duran como máximo `AUTH_SESSION_HOURS`, 12 h por
  defecto). Para cortar el acceso de inmediato, **deshabilítalo en *Equipo*** (cierra sus sesiones y el SSO ya no lo deja entrar) y en el IdP.
- Las cuentas SSO no tienen contraseña: «Olvidé mi contraseña» no les envía nada y *Cuenta y seguridad* oculta contraseña y 2FA.
- El 2FA lo aplica tu IdP. `SSO_REQUIRED_AMR=mfa` lo verifica si el IdP informa `amr`.

## 5. Comprobación con tu IdP real (checklist)

1. Con `SSO_ENABLED=true`, `GET /api/v1/auth/config` devuelve `sso_enabled: true` y el login muestra el botón.
2. Entra con una persona del grupo ADMIN: debe quedar como ADMIN y verse en *Auditoría* un `ACCOUNT_CREATED` y un `LOGIN_SUCCEEDED` (con `sso`).
3. Entra con una persona sin grupo: según `SSO_DEFAULT_ROLE` debe quedar como VIEWER o ver «Tu cuenta no tiene ningún grupo/rol…».
4. Entra con una cuenta de otro dominio/inquilino: debe rechazarse (`domain_not_allowed` / `wrong_tenant`) y aparecer `SSO_LOGIN_FAILED`.
5. Cambia a esa persona de grupo, vuelve a entrar y comprueba el cambio de rol (`MEMBER_UPDATED`).
6. Deshabilítala en *Equipo* e intenta entrar: «Tu cuenta está deshabilitada».
7. Abre `/api/auth/sso/callback?code=x&state=y` directamente: debe volver al login con «no se completó en este navegador».

## 6. Problemas frecuentes

| Mensaje | Causa probable |
|---|---|
| «El proveedor devolvió un emisor distinto del configurado» | `SSO_ISSUER` con barra final, `/v2.0` omitido (Entra) o servidor de autorización de Okta equivocado. |
| `invalid_token` tras volver del IdP | El `iss` del token no coincide con `SSO_ISSUER`; reloj del servidor desfasado (>60 s); `SSO_CLIENT_ID` equivocado. |
| «El inicio de sesión no se completó en este navegador» | Cookie de flujo ausente: el callback se abrió en otro navegador, tardó más de 10 min, o `PUBLIC_WEB_URL` no coincide con el dominio real. |
| «Perteneces a demasiados grupos…» | Entra no incluye los grupos (más de ~200). Usa roles de aplicación. |
| «Ya existe una cuenta con tu correo…» | Ver la sección 4 sobre `SSO_LINK_EXISTING`. |
