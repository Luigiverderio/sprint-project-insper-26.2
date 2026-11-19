.PHONY: install dev test lint format up down logs psql db-reset

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

# APAGA os dados do banco e o recria do zero com o esquema atual.
db-reset:
	docker compose down -v
	docker compose up -d --build --wait
