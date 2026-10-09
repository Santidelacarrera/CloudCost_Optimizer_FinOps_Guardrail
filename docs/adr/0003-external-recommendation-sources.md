# ADR-0003: Fuentes externas de recomendaciones (AWS Trusted Advisor y similares)

**Estado**: aceptada (decisión: **no integrar todavía**; criterios de reconsideración abajo). **Fecha**: 2026-10.

## Contexto
La plataforma genera recomendaciones con reglas propias, deterministas y explicables, y cada una acaba en un parche de IaC revisable. Se pidió evaluar si una fuente de AWS
(p. ej. Trusted Advisor) encaja. Se compararon tres. Lo único comprobado en el entorno de trabajo es que el SDK instalado (`boto3`) expone los servicios `support`, `trustedadvisor`, `cost-optimization-hub` y
`compute-optimizer`. **Los requisitos de plan y de activación de la tabla provienen de la documentación de AWS recordada, no consultada** (el entorno no tenía acceso a docs.aws.amazon.com):
verifícalos antes de reabrir esta decisión.

| Fuente | Qué aporta | Requisitos | Encaje |
|---|---|---|---|
| **Trusted Advisor** (`support` / `trustedadvisor`) | Comprobaciones de optimización de costes: instancias EC2 de bajo uso, volúmenes EBS sin adjuntar, RDS inactivas, balanceadores sin uso, IP elásticas sin asociar… | Las comprobaciones de **costes** exigen un plan **Business, Enterprise On-Ramp o Enterprise**. La API `support` solo responde en `us-east-1` | Parcial: solapa con nuestras reglas (EC2 ocioso, EBS huérfano) y no entrega el dato que necesitamos (métricas y coste por recurso) |
| **Cost Optimization Hub** (`cost-optimization-hub`) | Recomendaciones de rightsizing, recursos inactivos, Savings Plans/RI con ahorro estimado por recurso y «esfuerzo de implementación» | Gratuito; hay que activarlo (optar) en la cuenta de gestión; permisos `cost-optimization-hub:ListRecommendations`/`GetRecommendation` | Bueno como **contraste**, pero sus cifras incluyen descuentos y compromisos de la cuenta, y su metodología no es la nuestra |
| **Compute Optimizer** (`compute-optimizer`) | Rightsizing de EC2/EBS/Lambda/ECS con ML sobre CloudWatch | Opt-in; mejora con la métrica de memoria | Bueno como contraste del rightsizing; mismo problema de metodología |

## Decisión
**No integrar ninguna en esta iteración.** Razones:

1. **Contradice el principio rector del producto** (las cifras se explican y se pueden reproducir). Una recomendación de Trusted Advisor llega sin la serie de utilización ni la fórmula: no se
   podría guardar «evidencia verificable» propia (fórmula, coste de referencia, supuestos), solo la cifra de AWS.
2. **Dos cifras de ahorro para el mismo recurso** (la nuestra y la de AWS) obligarían a decidir cuál se aprueba, cuál se mide y cuál se muestra: justo la mezcla que la plataforma evita
   separando estimado, aprobado y observado.
3. **Cobertura y requisitos desiguales**: Trusted Advisor de costes exige un plan de soporte de pago que muchos clientes no tienen; Cost Optimization Hub solo ve lo que se haya activado en la
   cuenta de gestión. Una función que funciona en un tercio de las cuentas es difícil de explicar y de probar.
4. **El permiso nuevo ampliaría la superficie de lectura** (cada acción IAM añadida al rol del cliente es una conversación de seguridad) a cambio de un beneficio sin medir.
5. **No hay forma de validarlo aquí**: las respuestas reales dependen de la cuenta; sin una cuenta con datos, se escribiría código sin contrastar.

## Cuándo reconsiderarlo
Integrar **Cost Optimization Hub como contraste** (no como fuente de recomendaciones) si, tras validar el flujo en una cuenta real, se cumple lo siguiente:
- hay un criterio medible: «de las recomendaciones propias, qué fracción coincide con la de AWS sobre el mismo recurso» (acuerdo) y «qué hallazgos de AWS no detectamos» (huecos);
- se guarda en `evidence.external_signals` **sin alterar** el ahorro, la confianza ni el estado (solo informa a quien aprueba y alimenta el análisis de huecos);
- la acción IAM adicional va en una sentencia **opcional** de la plantilla (como `EnableCostExplorer`), apagada por defecto.

Trusted Advisor queda descartado como fuente de datos mientras exija un plan de soporte de pago.

## Consecuencias
- La lista de operaciones permitidas (`collectors/aws_guard.py`) y el rol IAM siguen siendo exactamente los mismos: 10 operaciones de lectura.
- Las recomendaciones de AWS no aparecen en la plataforma; quien las use puede compararlas a mano con las nuestras.
