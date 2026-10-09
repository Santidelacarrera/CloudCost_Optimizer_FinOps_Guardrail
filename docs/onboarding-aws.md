# Conectar una cuenta AWS con un clic (CloudFormation)

CloudCost lee tu cuenta asumiendo un **rol IAM de solo lectura** con `sts:AssumeRole` + **ExternalId**. Este flujo crea ese rol con
una pila de CloudFormation, sin copiar políticas a mano. Es equivalente al módulo Terraform
(`infrastructure/terraform/aws-readonly-role`); un test (`tests/unit/test_cloudformation.py`) comprueba que ambos conceden
exactamente las mismas acciones y que ninguna es de escritura.

## Qué concede el rol

| Acciones | Para qué |
|---|---|
| `ec2:DescribeInstances/Volumes/Snapshots/Images/InstanceTypes` | Inventario |
| `cloudwatch:GetMetricData/ListMetrics` | Utilización |
| `cloudtrail:LookupEvents`, `backup:ListProtectedResources`, `dlm:GetLifecyclePolicies/GetLifecyclePolicy` | Contexto de borrado seguro |
| `ce:GetCostAndUsage`, `ce:GetCostAndUsageWithResources` (opcional, `EnableCostExplorer`) | Costo real por recurso |

Nada de escritura, nada de `*` en las acciones. La confianza solo admite **un** principal concreto y exige el ExternalId.

## Flujo para el cliente

1. En CloudCost (rol ADMIN): `POST /api/v1/onboarding/aws/cloudformation`. Devuelve `external_id` (se muestra **una sola vez**,
   no se guarda), `trusted_principal_arn` y `quick_create_url`.
2. Guarda el `external_id` en tu gestor de secretos (`env:CC_SECRET_NOMBRE` o `aws-sm:cloudcost/<org_id>/nombre`). La base de datos solo guarda la referencia.
3. Abre `quick_create_url`: la consola de AWS muestra la pila con todo precargado. Revisa la plantilla y pulsa **Crear pila**
   (si cambias `RoleName`, AWS pedirá aceptar `CAPABILITY_NAMED_IAM`).
4. Cuando la pila termine, copia la salida **RoleArn** y registra la cuenta:
   `POST /api/v1/cloud-accounts` con `provider: "aws"`, `role_arn` y `external_id_ref`.

> El ExternalId viaja en el fragmento (`#`) de la URL de la consola, que el navegador no envía a ningún servidor. Aun así no es una
> credencial por sí solo: protege contra el «delegado confuso»; sin permiso de `AssumeRole` del principal de CloudCost no sirve de nada.

## Configuración del operador de CloudCost

| Variable | Descripción |
|---|---|
| `AWS_PLATFORM_PRINCIPAL_ARN` | ARN **concreto** del rol de CloudCost que asumirá los roles de los clientes (sin comodines). |
| `AWS_ONBOARDING_TEMPLATE_URL` | URL `https://…amazonaws.com/…` de S3 donde publicas `readonly-role.yaml` (CloudFormation solo acepta S3). |
| `AWS_ONBOARDING_STACK_REGION` | Región de la consola a la que apunta el enlace (por defecto `us-east-1`). |

Publicar la plantilla:

```bash
aws s3 cp infrastructure/cloudformation/readonly-role.yaml s3://MI-BUCKET/cloudcost/readonly-role.yaml
# El bucket debe permitir lectura al cliente (o usa una URL prefirmada de larga duración si prefieres no hacerlo público).
```

Sin esas variables el endpoint responde `503` con el motivo, y el cliente puede usar la plantilla directamente:
`aws cloudformation create-stack --stack-name CloudCostOptimizer-ReadOnly --template-body file://readonly-role.yaml --capabilities CAPABILITY_NAMED_IAM --parameters ParameterKey=TrustedPrincipalArn,ParameterValue=… ParameterKey=ExternalId,ParameterValue=…`

## Verificación

- Local: parseo de la plantilla, paridad con Terraform y forma del enlace (`pytest tests/unit/test_cloudformation.py`).
- CI: `cfn-lint` oficial sobre la plantilla (job `cloudformation`).
- **No verificado aquí:** el despliegue real de la pila en una cuenta AWS. Pruébalo una vez en una cuenta de prueba antes de ofrecerlo a clientes.
