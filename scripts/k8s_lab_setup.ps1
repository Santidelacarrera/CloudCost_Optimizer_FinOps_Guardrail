# Levanta el laboratorio de Kubernetes en minikube: clúster + Prometheus (kube-state-metrics, cAdvisor) + las 3 cargas de prueba.
# Uso (PowerShell, desde la raíz del repo):   .\scripts\k8s_lab_setup.ps1
# Requisitos: Docker Desktop en marcha, minikube, kubectl y helm en el PATH. Para borrarlo todo:   minikube delete
$ErrorActionPreference = "Stop"
foreach ($tool in "minikube", "kubectl", "helm") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "Falta '$tool' en el PATH. Ver docs/k8s-lab-validation.md" }
}
minikube start --cpus 4 --memory 6144
minikube addons enable metrics-server
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm upgrade --install prometheus prometheus-community/prometheus --namespace monitoring --create-namespace `
    -f infrastructure/k8s-lab/prometheus-values.yaml --wait --timeout 10m
kubectl apply -f infrastructure/k8s-lab/workloads.yaml
kubectl -n lab-dev rollout status deployment/overprovisioned-web deployment/well-sized deployment/hpa-protected --timeout=5m
Write-Host ""
Write-Host "Listo. Deja que las cargas corran al menos 30 minutos (mejor 2 horas o más) y entonces:"
Write-Host "  1) En OTRA terminal:  kubectl -n monitoring port-forward svc/prometheus-server 9090:80"
Write-Host "  2) En esta:           python scripts\k8s_lab.py --min-observation-days 0 --out-dir k8s-lab-out"
