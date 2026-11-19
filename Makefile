.PHONY: install dev test lint format up down logs psql db-reset migrate migrate-down seed

# Comandos simples, sem sintaxe de shell, para funcionar no PowerShell e no Git Bash.

install:
	uv sync --locked

dev:
	uv run --locked uvicorn onibus.api.main:create_app --factory --reload

test:
	uv run --locked pytest

# mypy via python -m: o Controle de Aplicativo do Windows bloqueia o mypy.exe.
lint:
	uv run --locked ruff check .
	uv run --locked ruff format --check .
	uv run --locked python -m mypy

format:
	uv run --locked ruff format .

# Docker: banco, API e worker (docker-compose.yml).
up:
	docker compose up -d --build --wait

down:
	docker compose down

logs:
	docker compose logs -f

# Abre o psql no banco do Docker (dono do banco; só para desenvolvimento).
psql:
	docker compose exec db sh -c 'psql -U postgres -d "$$POSTGRES_DB"'

# APAGA os dados do banco e o recria do zero, com todas as migrações.
db-reset:
	docker compose down -v
	docker compose up -d --build --wait
	uv run --locked alembic upgrade head

# Migrações (db/migrations/): conecta como o dono do banco, com os dados do .env.
# Aplica as que faltam; num banco limpo, cria o esquema inteiro.
migrate:
	uv run --locked alembic upgrade head

# Desfaz a última migração aplicada.
migrate-down:
	uv run --locked alembic downgrade -1

# Dados de teste no banco local: os .sql de db/seeds/, em ordem (veja o README de lá).
# Rode depois do make migrate.
seed:
	uv run --locked python -m onibus.db.seeds
