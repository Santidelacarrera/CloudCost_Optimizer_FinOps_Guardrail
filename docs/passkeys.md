# Llaves de acceso (WebAuthn / passkeys)

Permiten entrar con huella, rostro, PIN del dispositivo o una llave de seguridad (YubiKey, etc.). Están activas por defecto (`PASSKEYS_ENABLED=true`)
y sirven de dos formas:

1. **Segundo factor.** Si una cuenta tiene al menos una llave, después de la contraseña se pide la llave (o, si también tiene app de códigos, cualquiera de las dos).
2. **Entrada sin contraseña.** En la pantalla de acceso, «Entrar con llave de acceso». La llave siempre verifica a la persona (PIN o biometría) y está atada al
   dominio de CloudCost, así que un sitio falso no puede reutilizarla.

## 1. Cómo se usa

*Cuenta y seguridad → Llaves de acceso → Añadir una llave de acceso.* Pide la contraseña (una sesión robada no basta para añadir la llave del atacante), luego el
navegador muestra su propio diálogo. Se pueden registrar hasta 10 llaves por cuenta (conviene tener **al menos dos**: el portátil y una llave de seguridad).
Quitar una llave también pide la contraseña y cierra las demás sesiones.

Los *passkeys sincronizados* (iCloud, Google, 1Password…) aparecen con la etiqueta «Sincronizada»: viajan entre los dispositivos de la persona. Las llaves de seguridad
físicas no se sincronizan.

## 2. Si alguien pierde todas sus llaves

- Si tiene **código de recuperación o app de códigos**, entra con eso, quita la llave perdida y añade otra.
- Si no tiene nada más, quien opera el servidor, después de verificar su identidad por otro canal, ejecuta `python -m cloudcost.cli reset-mfa` para esa cuenta: quita el
  2FA **y las llaves**, cierra sus sesiones, deja constancia en la auditoría y avisa por correo. Luego entra con su contraseña y registra una llave nueva.

## 3. Variables

| Variable | Por defecto | Notas |
|---|---|---|
| `PASSKEYS_ENABLED` | `true` | `false` oculta los botones y desactiva los endpoints. Las llaves ya registradas se conservan. |
| `WEBAUTHN_RP_ID` | host de `PUBLIC_WEB_URL` | Puede ser un dominio superior (`acme.com` sirve para `app.acme.com`). Debe contener al host de `PUBLIC_WEB_URL`. |
| `WEBAUTHN_ORIGINS` | origen de `PUBLIC_WEB_URL` | Lista separada por comas; coincidencia **exacta** (incluye puerto). En producción, solo `https://`. |
| `WEBAUTHN_RP_NAME` | `CloudCost` | Nombre que muestra el navegador. |

En desarrollo con Docker (`PUBLIC_WEB_URL=http://localhost:5985`) funciona porque `localhost` se trata como contexto seguro.

## 4. Lo que no se puede deshacer: el RP ID

Cada llave queda ligada al **RP ID** con el que se creó. Si cambias de dominio, las llaves existentes dejan de funcionar (la gente seguirá entrando con contraseña + app o
códigos de recuperación y tendrá que registrar llaves nuevas). Fija el dominio definitivo antes de invitar a tu equipo; si piensas usar varios subdominios, define
`WEBAUTHN_RP_ID` con el dominio superior desde el principio.

## 5. Qué se verifica y qué no

Se verifica: tipo de operación, reto idéntico y de un solo uso, origen exacto, hash del RP ID, presencia y verificación del usuario, firma (ES256, EdDSA, RS256),
contador de firmas y coherencia de las banderas de respaldo. **No** se valida la atestación del fabricante (se pide `attestation: none`): no se puede restringir qué
modelos de llave se aceptan. Si necesitas esa política (p. ej. solo YubiKey certificadas), requiere añadir verificación de atestación con la lista de metadatos FIDO MDS.

## 6. Verificación realizada

- 17 pruebas unitarias con un autenticador de software (`tests/fake_authenticator.py`): registro y autenticación con ES256/EdDSA/RS256, origen/RP/UV/UP/reto erróneos, firma de
  otra llave, datos alterados, contador (repetido, retrocede, passkeys sincronizados con 0), CBOR malformado o anidado, configuración del RP.
- Pruebas con PostgreSQL (`tests/integration/test_passkeys_db.py`): reutilización de respuestas, retos atados a la cuenta, llaves de otra cuenta, límite de intentos, entrada sin
  contraseña (cuentas deshabilitadas, sin verificar o SSO), retirada, `reset-mfa` del operador y aislamiento por organización (RLS).
- Prueba de punta a punta (`apps/web/e2e/passkeys.spec.ts`) con un autenticador **virtual** de Chromium: registro, 2FA, entrada sin contraseña y retirada.
- **No probado** con dispositivos reales (Windows Hello, Touch ID, Android, YubiKey) ni con Safari/Firefox: hazlo en tu dominio definitivo antes del lanzamiento.
