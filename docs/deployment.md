# Despliegue en producción

Guía para un servidor único con Docker (lo más simple para empezar). Si luego necesitas escalar, el mismo contenedor de la API y la web funcionan
detrás de un balanceador con Postgres administrado (RDS, Cloud SQL); quita el servicio `postgres` del compose y apunta las URL ahí.

```
Internet ─▶ Caddy (80/443, TLS automático) ─▶ web (Next.js, BFF) ─▶ api (FastAPI) ─▶ postgres
                                                                      └▶ redis ◀─ worker (escaneos)
                                          backup (pg_dump diario + restauración de prueba)
```
Solo Caddy publica puertos. La API, Postgres y Redis no son alcanzables desde Internet.

## 1. Qué necesitas
| Cosa | Detalle |
|---|---|
| Servidor Linux | 2 vCPU y 4 GB de RAM alcanzan para empezar. Docker Engine con el plugin `compose`. |
| Dominio | Un registro DNS `A` (y `AAAA` si usas IPv6) de, por ejemplo, `app.tu-dominio.cl` hacia la IP del servidor. |
| Correo saliente | Cuenta en un proveedor SMTP con tu dominio verificado: ver [email.md](email.md). |
| Firewall | Abre solo 22 (idealmente restringido a tu IP), 80 y 443. |

## 2. Variables
```bash
mkdir -p /opt/cloudcost && cd /opt/cloudcost
# (copia aquí .env.production.example del repositorio)
cp .env.production.example .env.production && chmod 600 .env.production
openssl rand -base64 48        # una vez por cada secreto: AUTH_PEPPER, POSTGRES_PASSWORD, APP_DB_PASSWORD, GRAFANA_PASSWORD
```
Rellena todo lo que dice `CAMBIAR`. Docker Compose se niega a arrancar si falta una variable obligatoria, y la API se niega a arrancar en
producción con un pepper de desarrollo, sin SMTP o sin `https` (`apps/api/cloudcost/config.py`).

> **Guarda `AUTH_PEPPER` aparte** (gestor de contraseñas o gestor de secretos), no solo en el servidor. Las copias de seguridad **no** lo incluyen y
> sin él ninguna contraseña ni código de 2FA existente vuelve a funcionar. Tampoco lo cambies una vez que haya cuentas: ver [security.md](security.md).

## 3. Primer arranque
Las imágenes las publica `.github/workflows/release.yml` en GitHub Container Registry. Por defecto los paquetes son privados:
crea un token personal (*classic*) con el permiso `read:packages` y, en el servidor:
```bash
echo "<TOKEN>" | docker login ghcr.io -u <tu-usuario-github> --password-stdin
C="docker compose --env-file .env.production -f docker-compose.prod.yml"
$C pull && $C up -d && $C ps
```
Caddy pide el certificado al primer acceso: el DNS debe estar propagado y los puertos 80 y 443 abiertos, o Let's Encrypt lo rechazará (y limita los reintentos).

Después abre `https://app.tu-dominio.cl`, crea la primera cuenta (queda como administradora de su organización) y **activa la verificación en dos pasos**.
Cuando ya existan las cuentas fundadoras, pon `AUTH_SIGNUP_OPEN=false` y `$C up -d`: desde entonces solo se entra por invitación (Equipo → Invitar).

## 4. Publicar una versión y desplegarla
1. Que CI esté verde en `main` (incluye las pruebas de punta a punta con navegador real).
2. `git tag v1.0.0 && git push origin v1.0.0` → el workflow **release** construye las 4 imágenes, las escanea con Trivy y, si pasan, las publica como `1.0.0`.
3. Despliegue:
   - **A mano:** cambia `IMAGE_TAG=1.0.0` en `.env.production` y ejecuta `$C pull && $C up -d`. El servicio `migrate` aplica las migraciones antes de la API.
   - **Con el workflow `deploy`:** configura el entorno `production` (secretos y aprobadores, detallado al inicio de `.github/workflows/deploy.yml`) y ejecútalo con la versión.
4. **Volver atrás:** vuelve a desplegar la versión anterior. Ojo: las migraciones no se revierten solas; escríbelas siempre compatibles hacia atrás (agregar, no borrar) en el mismo release.

## 5. Copias de seguridad
El servicio `backup` hace un `pg_dump` cada 24 h (`BACKUP_INTERVAL_HOURS`), comprueba que el archivo es legible, guarda su SHA-256, **lo restaura en una base temporal y recorre la
cadena de auditoría de cada organización**. Si algo falla, queda escrito en `docker compose logs backup` y el contenedor pasa a *unhealthy* a las 26 h sin copia correcta.

- Las copias viven en el volumen `backups` **del mismo servidor**: eso no te protege si el servidor se pierde. Define `BACKUP_S3_URI` (con credenciales o rol de instancia) para sacarlas a un bucket
  con versionado y una regla de ciclo de vida, o copia el volumen fuera por otro medio.
- Define `BACKUP_PING_URL` con un monitor tipo *dead-man's switch* (healthchecks.io, Uptime Kuma): si dejan de llegar avisos, te escribe.
- El rol que hace la copia debe saltarse RLS (las tablas usan `FORCE ROW LEVEL SECURITY`); en este compose es el superusuario `postgres`. Con Postgres administrado, usa un rol con `BYPASSRLS`
  o el propietario; con otro rol `pg_dump` falla (probado) en vez de guardar datos incompletos.

**Restaurar en un servidor nuevo** (con el mismo `AUTH_PEPPER`):
```bash
C="docker compose --env-file .env.production -f docker-compose.prod.yml"
$C up -d postgres                                   # base vacía
$C run --rm --no-deps -v "$PWD/cloudcost-AAAAMMDDTHHMMSSZ.dump:/restore/dump:ro" backup \
   sh -c 'sh /scripts/restore.sh /restore/dump "$DATABASE_ADMIN_URL"'
$C up -d                                            # migrate reconoce las migraciones ya aplicadas
```
`restore.sh` se niega a escribir sobre una base que ya tiene tablas (salvo `--force`) y rechaza una copia cuya suma de verificación no coincide.
**Ensaya esta restauración una vez antes del lanzamiento**, en una máquina cualquiera: una copia que nunca se restauró es solo una esperanza.

## 6. Monitoreo y alertas
`$C --profile monitoring up -d` levanta Prometheus (reglas en `infrastructure/docker/alerts.yml`) y Alertmanager.
**Antes edita `infrastructure/docker/alertmanager.yml`**: con los valores de ejemplo las alertas se evalúan pero no llegan a nadie.
Reglas incluidas: API o worker caídos, más de 5 % de errores 5xx, latencia p95 > 2 s, ráfagas de intentos de acceso fallidos (posible relleno de credenciales),
bloqueos de cuenta masivos, escaneos fallando y errores de la API del proveedor cloud. Prometheus (9090) y Alertmanager (9093) solo escuchan en `127.0.0.1` del servidor:
desde tu equipo entra con un túnel, `ssh -L 9090:localhost:9090 usuario@servidor`.

## 7. Capacidad de inicio de sesión
El hash de contraseñas (scrypt, `N=2^15, r=8, p=3`) cuesta a propósito ~330 ms de CPU y ~32 MiB por verificación; la API ejecuta como máximo 4 a la vez por proceso (≈128 MiB de pico).
Medido en una máquina de 2 vCPU: **~6 inicios de sesión por segundo** de rendimiento sostenido, que son más de 500 000 al día si la CPU no hiciera otra cosa: de sobra para cientos de usuarios.
Lo que importa es un ataque de volumen: los límites por cuenta y por IP lo frenan y la regla `PosibleRellenoDeCredenciales` avisa. Si tu tráfico real crece, sube vCPU o réplicas de la API
(los límites viven en la base de datos, así que se comparten entre réplicas).
