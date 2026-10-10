# Levanta el laboratorio de Kubernetes en minikube: clúster + Prometheus (kube-state-metrics, cAdvisor) + las 3 cargas de prueba.
# Uso (PowerShell, desde la raíz del repo):   .\scripts\k8s_lab_setup.ps1
# Requisitos: Docker Desktop EN MARCHA, minikube, kubectl y helm en el PATH. Para borrarlo todo:   minikube delete
$ErrorActionPreference = "Stop"

function Step([string]$what, [scriptblock]$cmd) {
    Write-Host "`n==> $what" -ForegroundColor Cyan
    & $cmd
    if ($LASTEXITCODE -ne 0) { throw "Falló: $what (código $LASTEXITCODE). Corrige el error de arriba y vuelve a ejecutar el script." }
}

foreach ($tool in "minikube", "kubectl", "helm", "docker") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "Falta '$tool' en el PATH. Ver docs/k8s-lab-validation.md" }
}
docker version --format "{{.Server.Version}}" 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Docker no responde. Abre Docker Desktop y espera a que diga 'Engine running'; luego vuelve a ejecutar el script." }

Step "Crear el clúster (minikube)" { minikube start --driver=docker --cpus 4 --memory 6144 }
Step "Activar metrics-server" { minikube addons enable metrics-server }
Step "Añadir el repositorio de charts de Prometheus" { helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update }
Step "Actualizar repositorios" { helm repo update }
Step "Instalar Prometheus (puede tardar varios minutos)" {
    helm upgrade --install prometheus prometheus-community/prometheus --namespace monitoring --create-namespace `
        -f infrastructure/k8s-lab/prometheus-values.yaml --wait --timeout 10m
}
Step "Desplegar las cargas de prueba" { kubectl apply -f infrastructure/k8s-lab/workloads.yaml }
Step "Esperar a que las cargas estén listas" {
    kubectl -n lab-dev rollout status deployment/overprovisioned-web --timeout=5m
    if ($LASTEXITCODE -eq 0) { kubectl -n lab-dev rollout status deployment/well-sized --timeout=5m }
    if ($LASTEXITCODE -eq 0) { kubectl -n lab-dev rollout status deployment/hpa-protected --timeout=5m }
}
Write-Host ""
Write-Host "Listo. Deja que las cargas corran al menos 30 minutos (mejor 2 horas o más) y entonces:" -ForegroundColor Green
Write-Host "  1) En OTRA terminal:  kubectl -n monitoring port-forward svc/prometheus-server 9090:80"
Write-Host "  2) En esta:           python scripts\k8s_lab.py --min-observation-days 0 --out-dir k8s-lab-out"
