# Rotación del pepper

El *pepper* (`AUTH_PEPPER`) es el secreto del servidor que protege tres cosas: los hashes de contraseña, el cifrado de los secretos TOTP y
los códigos de recuperación. Rotarlo es necesario si se filtró, si alguien con acceso al servidor se fue, o como higiene periódica.

**Sin cambios no pasa nada:** el pepper actual es el id `1` y conserva exactamente el formato de siempre. Todo lo de esta guía se
activa solo cuando pones un id nuevo.

## Cómo funciona

Cada dato guarda el **id** del pepper con el que se creó, y la API acepta varios peppers a la vez:

| Dato | Formato con pepper `2` | Cómo migra |
|---|---|---|
| Contraseña | `scrypt$n$r$p$sal$hash$2` | **Sola, en el siguiente inicio de sesión** (es el único momento en que existe la contraseña en claro) |
| Secreto TOTP | `v2.2.<blob>` | Sola al usar el 2FA, o de golpe con `reencrypt-totp` |
| Código de recuperación | `2:<hmac>` | No migra (solo existe el hash): los códigos viejos siguen valiendo mientras exista el pepper viejo |

Cuentas, sesiones, tokens de correo y claves de invitación **no** dependen del pepper. Los contadores de bloqueo por intentos fallidos sí
usan el pepper actual como clave: al rotar se reinician una vez (son datos de ventana corta, no hay nada que migrar).

## Procedimiento

1. **Genera el pepper nuevo** y guárdalo en tu gestor de secretos: `openssl rand -base64 48` (no uses `,` ni `:`).
2. **Configura la API y el worker** (`.env.production`):
   ```
   AUTH_PEPPER=<el nuevo>
   AUTH_PEPPER_ID=2
   AUTH_PEPPER_PREVIOUS=1:<el pepper de hasta ahora>
   ```
   El siguiente id debe ser distinto de cualquiera usado antes (`2`, luego `3`…). Para una segunda rotación:
   `AUTH_PEPPER_PREVIOUS=1:<viejo1>,2:<viejo2>`. Reinicia: `docker compose --env-file .env.production -f docker-compose.prod.yml up -d api worker`.
   En producción la API se niega a arrancar si el formato es incorrecto, si un pepper anterior tiene menos de 32 caracteres o si se repite un id.
3. **Comprueba el estado** (solo imprime contadores):
   ```
   docker compose ... exec api python -m cloudcost.auth.rotation status
   ```
4. **Re-cifra los secretos TOTP** (ensayo primero; no escribe nada):
   ```
   docker compose ... exec api python -m cloudcost.auth.rotation reencrypt-totp --dry-run
   docker compose ... exec api python -m cloudcost.auth.rotation reencrypt-totp
   ```
   Si alguna cuenta aparece en `failed_accounts` el comando termina con código 1: su secreto está cifrado con un pepper que ya no está en la
   configuración. Vuelve a poner ese pepper en `AUTH_PEPPER_PREVIOUS`.
5. **Espera** a que la gente inicie sesión (las contraseñas se migran solas). Repite `status` cada cierto tiempo.
6. **Retira el pepper viejo** cuando quieras:
   ```
   docker compose ... exec api python -m cloudcost.auth.rotation retire-check 1     # código 0 = no queda nada que lo use
   ```
   y borra `1:...` de `AUTH_PEPPER_PREVIOUS`.

## Qué pasa si retiras el pepper con datos pendientes

| Pendiente con el pepper retirado | Consecuencia | Remedio |
|---|---|---|
| Contraseñas de personas que no han vuelto a entrar | No pueden iniciar sesión con la contraseña | «Olvidé mi contraseña» (correo) restablece una nueva; nadie pierde la cuenta ni el 2FA |
| Secretos TOTP | **Ninguna** si hiciste el paso 4 | — |
| Códigos de recuperación sin usar | Dejan de servir | La persona genera otros desde su cuenta (con su 2FA). El 2FA en sí no se invalida |

Por eso `retire-check` existe: te dice cuántos quedan de cada tipo. Las contraseñas de cuentas dormidas son lo único que normalmente
queda; decide tú cuánto esperar (p. ej. 30–90 días) y retira.

## Si el pepper se filtró (no solo higiene)

La rotación protege los datos **a partir de ahora** pero un atacante con el pepper viejo y un volcado de la base puede atacar offline los
hashes que aún no migraron. Entonces: rota, ejecuta `reencrypt-totp`, y pide restablecer contraseñas (cierra todas las sesiones y
obliga a un hash nuevo) o retira el pepper viejo cuanto antes aceptando el costo de la tabla anterior.

## Verificación

- Local: formatos, anillo, compatibilidad con los formatos históricos, retiro sin romper, igualdad de trabajo de scrypt cuando falta un
  pepper, plan de re-cifrado y validación de configuración (`tests/unit/test_pepper_rotation.py`).
- Contra PostgreSQL real (corre en CI): `tests/integration/test_pepper_rotation_db.py` — contraseña, 2FA y códigos de recuperación creados
  con el pepper viejo funcionan tras rotar, se migran en el login y el comando de re-cifrado no pierde el segundo factor.
- **No verificado:** un ensayo en tu entorno real. Hazlo una vez en una copia (restaurada de un backup) antes de depender de esto.
