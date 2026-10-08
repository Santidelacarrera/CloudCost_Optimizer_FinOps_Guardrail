# Políticas (OPA / Rego) dentro de la API

La plataforma evalúa las políticas de `packages/policies` **ella misma, de forma síncrona, antes de crear el Pull Request**. Ya no depende de que el CI del cliente las ejecute: si una política lo niega, el PR no se crea y el repositorio no se toca.

```
aprobado → parche IaC → plan sintético + contexto → opa eval → ¿deny? ──sí (enforce)──> 422, nada se crea
                                                                └─no/audit──> rama + commit + PR
```

## Qué se evalúa
1. La API construye un **plan sintético** con el formato de `terraform show -json` (`resource_changes[].change.{actions,before,after}`) a partir del parche aprobado: `update` para un cambio de tamaño, `delete` para eliminar un volumen, instancia o snapshot. No es un `terraform plan` real (la API no tiene credenciales ni estado de tu nube); para eso sigue valiendo el mismo Rego en tu CI.
2. Añade el contexto `input.cloudcost` que **solo la plataforma conoce**:
   - `environments`: entorno normalizado del recurso (`production`, `staging`, `development`, `test`, `unknown`).
   - `reinforced_approval`: `true` si la aprobación reforzada se completó (mínimo 2 aprobadores y al menos un ADMIN o SRE).
   - `change_ticket`: el primer ticket con forma `ABC-123` citado en el motivo de alguna aprobación (p. ej. «autorizado por CAB, CHG-1234»).
3. Ejecuta `opa eval` (binario oficial, subproceso sin shell, límite de tiempo y de tamaño, entorno sin los secretos del proceso) sobre `data.cloudcost.guardrail.deny`.

## Reglas incluidas (`packages/policies/guardrail.rego`)
| Regla | Se permite si… |
|---|---|
| Eliminar un recurso de **producción o de entorno desconocido** | el recurso tiene la etiqueta `finops:reinforced-approval` (texto no vacío) **o** la plataforma registró la aprobación reforzada |
| **Redimensionar** una instancia de producción | tiene `finops:change-ticket` **o** se citó un ticket en la aprobación |

Un entorno ausente o no reconocido (`Prod-EU`) cuenta como producción: es la opción prudente. `null` y `""` nunca cuentan como etiqueta o ticket.

**Cómo desbloquear un PR denegado:** añade la etiqueta `finops:change-ticket` (o `finops:reinforced-approval`) al recurso en tu nube y vuelve a escanear, o cita el ticket al aprobar. La recomendación sigue en `APPROVED`; reintentar «Crear Pull Request» reevalúa.

## Modos (`OPA_MODE`)
| Modo | Violación | Motor caído o política inválida |
|---|---|---|
| `enforce` (por defecto en producción) | **bloquea** (422 `policy_denied`) | **bloquea** (503 `policy_engine_unavailable`): falla cerrado |
| `audit` (por defecto en desarrollo) | crea el PR y deja la advertencia en su cuerpo | crea el PR y lo registra |
| `off` | no evalúa | — |

Cada evaluación queda en la auditoría (`POLICY_EVALUATED`: resultado, violaciones, hash de las políticas y duración). La métrica `policy_evaluations_total{result}` alimenta la alerta `MotorDePoliticasConErrores`.

## Políticas propias
Coloca archivos `.rego` con `package cloudcost.guardrail` y reglas `deny contains msg if { … }` en un directorio y apúntalo con `OPA_EXTRA_POLICY_DIR`; se suman a las incluidas. Los `*_test.rego` se ignoran. Valida con `opa check --strict` y `opa test` antes de desplegar: una política inválida es un error del motor (en `enforce`, bloquea).

## Evaluar un plan real desde tu pipeline
`POST /api/v1/policies/evaluate-plan` con `{"plan": <salida de terraform show -json>}` (roles ADMIN, FINOPS o SRE; máx. 2 MB) devuelve `{allowed, violations, policy_digest}` sin que instales OPA. Solo lectura; el campo `cloudcost` del plan se descarta.

## Imagen y CI
El `Dockerfile` de la API copia el binario de la imagen oficial `openpolicyagent/opa:1.9.0-static` y `guardrail.rego`. El job `api` de CI instala la misma versión para que las pruebas usen el motor real; el job `policies` ejecuta `opa check --strict`, `opa fmt --fail` y `opa test`.
