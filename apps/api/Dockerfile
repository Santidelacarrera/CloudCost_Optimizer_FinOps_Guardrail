FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/api/cloudcost ./cloudcost
# Motor de políticas: binario oficial de OPA (imagen fijada por versión) + las políticas Rego de la plataforma.
COPY --from=openpolicyagent/opa:1.9.0-static /opa /usr/local/bin/opa
COPY packages/policies/guardrail.rego ./policies/guardrail.rego
ENV OPA_POLICY_DIR=/app/policies
COPY infrastructure/terraform/example-iac ./example-iac
RUN useradd -r -u 10001 app && chown -R app /app
USER app
EXPOSE 8000
CMD ["uvicorn", "cloudcost.main:app", "--host", "0.0.0.0", "--port", "8000"]
