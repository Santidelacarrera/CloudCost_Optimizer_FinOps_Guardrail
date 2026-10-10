#!/usr/bin/env bash
# Levanta el laboratorio de Kubernetes en minikube (macOS/Linux). Ver docs/k8s-lab-validation.md. Para borrarlo todo: minikube delete
set -euo pipefail
for tool in minikube kubectl helm; do command -v "$tool" >/dev/null || { echo "Falta '$tool' en el PATH" >&2; exit 1; }; done
minikube start --cpus 4 --memory 6144
minikube addons enable metrics-server
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm upgrade --install prometheus prometheus-community/prometheus --namespace monitoring --create-namespace \
  -f infrastructure/k8s-lab/prometheus-values.yaml --wait --timeout 10m
kubectl apply -f infrastructure/k8s-lab/workloads.yaml
kubectl -n lab-dev rollout status deployment/overprovisioned-web deployment/well-sized deployment/hpa-protected --timeout=5m
echo
echo "Listo. Deja que las cargas corran al menos 30 minutos (mejor 2 horas o más) y entonces:"
echo "  1) En OTRA terminal:  kubectl -n monitoring port-forward svc/prometheus-server 9090:80"
echo "  2) En esta:           python scripts/k8s_lab.py --min-observation-days 0 --out-dir k8s-lab-out"
