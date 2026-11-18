.PHONY: install dev test lint format up down logs

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

# Docker: API e worker (docker-compose.yml). O banco entra depois.
up:
	docker compose up -d --build --wait

down:
	docker compose down

logs:
	docker compose logs -f
