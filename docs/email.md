# Correo de CloudCost (verificación, recuperación e invitaciones)

Sin correo, nadie puede confirmar su cuenta ni recuperar su contraseña: por eso `SMTP_HOST` es obligatorio en producción. Para que los mensajes lleguen a la bandeja
(y no al spam) el **dominio remitente** debe demostrar que eres tú quien envía.

## 1. Elige un proveedor SMTP
Cualquiera con SMTP sirve. Tres opciones habituales: Amazon SES (barato, exige salir del *sandbox*), Postmark o Resend (más simples). Te darán `SMTP_HOST`, puerto (587 con STARTTLS),
usuario y clave. Usa un remitente de tu dominio: `SMTP_FROM=CloudCost <no-reply@tu-dominio.cl>`.

## 2. Registros DNS del dominio remitente
Los valores exactos los entrega tu proveedor al verificar el dominio; el esquema es siempre el mismo:

| Registro | Para qué sirve | Ejemplo |
|---|---|---|
| **SPF** (TXT en el dominio) | Lista quién puede enviar por tu dominio | `v=spf1 include:amazonses.com ~all` (el `include` lo da tu proveedor) |
| **DKIM** (normalmente 3 CNAME o 1 TXT) | Firma cada mensaje; evita suplantación | los entrega el proveedor, tipo `abc._domainkey.tu-dominio.cl` |
| **DMARC** (TXT en `_dmarc.tu-dominio.cl`) | Indica qué hacer con lo que falla SPF/DKIM y te envía informes | `v=DMARC1; p=none; rua=mailto:dmarc@tu-dominio.cl` |

Empieza DMARC con `p=none` (solo observar) durante un par de semanas, revisa los informes y súbelo a `p=quarantine` y luego `p=reject` cuando todo lo legítimo pase.
Si tienes un dominio de marketing y otro transaccional, usa un subdominio para CloudCost (`mail.tu-dominio.cl`): protege la reputación de cada uno.

## 3. Comprobar antes de lanzar
1. Con la API ya en producción, crea una cuenta con un correo tuyo de Gmail/Outlook y revisa que el mensaje llega a la **bandeja principal**.
2. En Gmail: *Mostrar original* → `SPF: PASS`, `DKIM: PASS`, `DMARC: PASS`.
3. Herramientas gratuitas como mail-tester.com puntúan la configuración (envía un mensaje a su dirección de prueba).
4. Prueba también «Olvidé mi contraseña» y una invitación desde Equipo.

## 4. Qué hace el sistema por ti
- El envío va en segundo plano: la respuesta de la API tarda lo mismo exista o no el correo (no se puede averiguar quién tiene cuenta).
- Límites: 3 correos por dirección y 10 por IP cada hora, para que nadie use el formulario para inundar una bandeja.
- Si el proveedor falla, el error queda en el log de la API (`no se pudo enviar el correo a …`) y la persona puede pedir otro desde la pantalla.
- Los enlaces valen 24 h (confirmar), 1 h (recuperar) o 7 días (invitar) y sirven una sola vez.
