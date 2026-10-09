# Conectar Azure y GCP

CloudCost lee el inventario y las métricas de tu suscripción de Azure o proyecto de GCP **solo con permisos de lectura**. No
crea, modifica ni borra nada: los cambios salen como Pull Request de Terraform, con aprobación humana.

| | Azure | GCP |
|---|---|---|
| Se inventaría | Máquinas virtuales, discos administrados, snapshots | Instancias de Compute Engine, discos persistentes, snapshots |
| Métricas | Azure Monitor (CPU y memoria de la plataforma, 14 días) | Cloud Monitoring (CPU; memoria si tienes el Ops Agent) |
| Permisos | Roles integrados `Reader` + `Monitoring Reader` | `roles/compute.viewer` + `roles/monitoring.viewer` |
| Credencial | Service principal (tenant, client id, secreto) | Cuenta de servicio (clave JSON) |
| Terraform que lo crea | `infrastructure/terraform/azure-readonly-role` | `infrastructure/terraform/gcp-readonly-role` |
| IaC que se parchea | `azurerm_linux_virtual_machine`, `azurerm_windows_virtual_machine`, `azurerm_managed_disk`, `azurerm_snapshot` | `google_compute_instance`, `google_compute_disk`, `google_compute_snapshot` |

Las mismas reglas de AWS (reducir instancia, instancia ociosa, volumen huérfano, snapshot antiguo) se aplican a las tres nubes,
con el mismo cálculo de riesgo y las mismas políticas de aprobación.

## Azure

1. **Crea el service principal** (elige una de las dos):
   - Terraform: `cd infrastructure/terraform/azure-readonly-role && terraform apply -var subscription_id=<SUB>`. El secreto queda en el *state*: guárdalo cifrado.
   - CLI (el secreto no pasa por ningún state):
     ```bash
     az ad sp create-for-rbac --name cloudcost-optimizer-readonly --role Reader --scopes /subscriptions/<SUB> --years 1
     az role assignment create --assignee <appId> --role "Monitoring Reader" --scope /subscriptions/<SUB>
     ```
     La salida trae `tenant`, `appId` (client id) y `password` (secreto).
2. **Guarda el secreto fuera de la base**, como variable de entorno de la API y el worker (`AZURE_RO_SECRET=...`) o en AWS Secrets Manager. En la plataforma solo se registra la *referencia*.
3. **Registra la cuenta** (rol ADMIN):
   ```bash
   curl -X POST $API/api/v1/cloud-accounts -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{
     "provider": "azure", "account_ref": "<SUB>", "display_name": "Azure producción",
     "tenant_id": "<TENANT>", "client_id": "<APP_ID>", "credentials_ref": "env:AZURE_RO_SECRET"}'
   ```
   `account_ref` es el id de la suscripción. Sin `regions` se inventarían todas; con `"regions": ["eastus"]` solo esas.

## GCP

1. **Crea la cuenta de servicio**:
   - Terraform: `cd infrastructure/terraform/gcp-readonly-role && terraform apply -var project_id=<PROYECTO>`; o con CLI:
     ```bash
     gcloud iam service-accounts create cloudcost-readonly --project <PROYECTO>
     for r in roles/compute.viewer roles/monitoring.viewer; do
       gcloud projects add-iam-policy-binding <PROYECTO> --member serviceAccount:cloudcost-readonly@<PROYECTO>.iam.gserviceaccount.com --role $r
     done
     ```
2. **Genera la clave JSON** (Terraform no la crea a propósito, para que no acabe en el *state*):
   `gcloud iam service-accounts keys create key.json --iam-account cloudcost-readonly@<PROYECTO>.iam.gserviceaccount.com`.
   Guarda **el contenido** del JSON en una variable de entorno (`GCP_RO_KEY`) o en AWS Secrets Manager y borra el archivo.
   Las claves de larga vida son un riesgo: rótalas (cada 90 días) y no las reutilices.
3. **Registra la cuenta**:
   ```bash
   curl -X POST $API/api/v1/cloud-accounts -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{
     "provider": "gcp", "account_ref": "<ID-DEL-PROYECTO>", "display_name": "GCP datos", "credentials_ref": "env:GCP_RO_KEY"}'
   ```
   `account_ref` es el **id** del proyecto (p. ej. `mi-proyecto-123`), no su número.

## Qué conviene saber

- **Costos estimados.** Azure y GCP no entregan costo por recurso en una llamada barata; el costo mensual sale de una tabla de precios aproximada (lista pública, región principal, sin descuentos por reserva ni compromiso). El ahorro se **verifica** después del despliegue con el costo observado, igual que en AWS. Los snapshots de GCP usan los bytes realmente almacenados.
- **Memoria.** Sin métrica de memoria no se propone reducir una máquina (regla `require_memory_metric`). En Azure se usa `Available Memory Bytes`; en GCP hace falta el [Ops Agent](https://cloud.google.com/stackdriver/docs/solutions/agents/ops-agent) (métrica `agent.googleapis.com/memory/percent_used`).
- **Ante la duda no se borra.** Un snapshot que sirve de base a una imagen (o a una versión de Azure Compute Gallery) no se propone. Si no se pudo listar las imágenes (permiso o error de API), ningún snapshot se propone y el escaneo queda marcado como parcial.
- **Fallos parciales.** Si una API falla (permiso, límite de tasa agotado) el escaneo sigue, lo registra en los avisos y **no** marca como inactivo lo que no vio. Si faltan las credenciales, el escaneo falla enseguida con un mensaje claro y sin reintentos.
- **Discos creados por Kubernetes** (AKS/GKE: `pvc-…`) no están en Terraform: se proponen, pero sin parche automático. Se reconocen por la etiqueta `kubernetes.io-created-for-pv-name` (Azure) y la descripción del disco (GCP).
- **Protección.** Las etiquetas/labels `finops:ignore`, `do-not-delete`, `retain`, `legal-hold`, `keep` excluyen un recurso en cualquier nube (en GCP las *labels* no admiten `:`: usa `finops-ignore`).
- **Máquinas de Azure** declaradas con `size = var.…` o con `count`/`for_each` se proponen sin parche automático (igual que en AWS).
- Solo se hacen peticiones **GET** a `management.azure.com`, `login.microsoftonline.com`, `compute.googleapis.com`, `monitoring.googleapis.com` y `oauth2.googleapis.com`; un enlace de paginación hacia otro host se rechaza.

## Qué se ha probado

Los colectores y el cliente HTTP se prueban con respuestas simuladas (formas de JSON según la documentación de cada API): normalización,
reglas, parches Terraform, paginación, límites de tasa, fallos parciales, firma del JWT de GCP. El alta de cuentas y el escaneo
completo se prueban contra PostgreSQL real en CI. **Todavía no se han ejecutado contra una suscripción o proyecto reales**:
la primera vez, haz un escaneo y revisa que los recursos y los avisos de la pantalla de escaneos coincidan con tu consola.
