.PHONY: install dev test lint format up down logs db-reset

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

# Apaga os dados do banco e sobe tudo de novo. Os scripts de db/init/ só rodam
# com o volume vazio: use depois de mudar algum deles ou as senhas do .env.
db-reset:
	docker compose down -v
	docker compose up -d --build --wait
