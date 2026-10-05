.PHONY: install test lint up down migrate-local demo
install:        ## dependencias Python
	pip install -r requirements-dev.txt
test:           ## pruebas unitarias (las de integración requieren DATABASE_URL y DATABASE_ADMIN_URL)
	pytest -q
lint:
	ruff check apps tests
up:
	docker compose up --build
down:
	docker compose down -v
migrate-local:
	SEED_DEV=true sh scripts/migrate.sh
demo:           ## flujo completo contra la API local
	python scripts/demo_flow.py
