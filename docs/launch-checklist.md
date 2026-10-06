# Lista de lanzamiento

Qué está resuelto en el repositorio, qué depende de ti y qué conviene hacer después. Marca cada punto cuando lo hayas verificado en **tu** entorno.

## Resuelto en el repositorio
- [x] Cuentas propias: registro, confirmación por correo, login con bloqueo progresivo, 2FA con códigos de recuperación, sesiones revocables, recuperación de contraseña, invitaciones.
- [x] Producción no arranca insegura: pepper propio, SMTP, `https`, sin auth de desarrollo ni demo (`config.py`, probado).
- [x] Compose de producción con TLS automático (Caddy), solo Caddy expuesto, secretos obligatorios, migraciones previas a la API (`docker-compose.prod.yml`, validado en CI).
- [x] Acceso de desarrollo de la web apagado por defecto (antes bastaba con no definir `DEV_LOGIN`).
- [x] Modo «solo por invitación» (`AUTH_SIGNUP_OPEN=false`) y comando de operador para quitar el 2FA a quien perdió el teléfono y los códigos, con constancia en la auditoría (`python -m cloudcost.cli reset-mfa`).
- [x] Publicación de imágenes con escaneo de vulnerabilidades (`release.yml`) y despliegue con aprobación (`deploy.yml`).
- [x] Copias de seguridad automáticas con restauración de prueba y verificación de la cadena de auditoría (`scripts/`, probadas en CI y contra Postgres real, incluidos los casos de copia dañada y auditoría alterada).
- [x] Pruebas de punta a punta con navegador real contra la pila completa y un buzón de correo de prueba (`apps/web/e2e`, job `e2e` de CI).
- [x] Métricas HTTP y de autenticación, y reglas de alerta (`infrastructure/docker/alerts.yml`).
- [x] Borradores de Términos y Política de privacidad en `/terms` y `/privacy`.
- [x] Medición de capacidad de inicio de sesión (docs/deployment.md §7).

## Depende de ti (no puedo hacerlo desde aquí)
- [ ] Servidor con Docker, dominio y DNS apuntando a él; firewall con 22/80/443.
- [ ] `.env.production` con secretos propios; **`AUTH_PEPPER` guardado además en un gestor aparte**.
- [ ] Cuenta SMTP y registros SPF, DKIM y DMARC del dominio remitente ([email.md](email.md)); prueba de entrega a la bandeja principal.
- [ ] `docker login ghcr.io` en el servidor, primera etiqueta `v1.0.0` y primer despliegue.
- [ ] **Ensayar una restauración** de una copia real en otra máquina (docs/deployment.md §5) y configurar `BACKUP_S3_URI` o copiar el volumen fuera del servidor.
- [ ] `BACKUP_PING_URL` en un monitor externo, y `alertmanager.yml` con tu correo o webhook (hoy trae valores de ejemplo).
- [ ] Un monitor externo de disponibilidad sobre `https://tu-dominio/login`.
- [ ] Abogado: revisar y completar `/terms` y `/privacy` (hoy son borradores con [CORCHETES]).
- [ ] Prueba con una cuenta AWS real y el rol de solo lectura (docs/runbook.md §1): hasta ahora el colector se ha probado con datos de demostración y archivos importados, no contra AWS.
- [ ] Crear las cuentas fundadoras, activar 2FA en cada una y poner `AUTH_SIGNUP_OPEN=false`.
- [ ] Proteger `main` en GitHub (revisión obligatoria y checks `api`, `web`, `e2e`, `backups`, `docker`, `security`, `policies`).

## Conviene hacer después del lanzamiento
- [ ] **Rotación del pepper**: hoy no es posible sin invalidar contraseñas y 2FA. Requiere aceptar dos peppers a la vez y reescribir hashes en el siguiente login; hay que construirlo antes de necesitarlo.
- [ ] Pasar la CSP a nonces (quitar `'unsafe-inline'` de los scripts): exige renderizado dinámico.
- [ ] Passkeys (WebAuthn) como segundo factor más fuerte que TOTP.
- [ ] Registrar la aceptación de los términos con fecha y versión (hoy el aviso es informativo al pie de las pantallas de acceso).
- [ ] Prueba de penetración externa del módulo de autenticación y del BFF antes de aceptar clientes de pago.
- [ ] Prueba de carga con tráfico realista (la medición actual es de un solo proceso).
- [ ] Azure, GCP, GitLab y Kubernetes: siguen en las fases posteriores del README.
