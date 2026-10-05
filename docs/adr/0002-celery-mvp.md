# ADR-0002: Celery + Redis para los escaneos en el MVP
**Estado**: aceptada. **Contexto**: la tabla de stack propone workers en Go. **Decisión**: Celery para reutilizar el dominio en Python. **Consecuencias**: reintentos acotados, `acks_late` y límites de tiempo por escaneo; migrar a Go o Temporal queda como opción de Fase 2 si el volumen lo exige.
