.PHONY: install dev test lint format

install:
	uv sync --locked

dev:
	uv run --locked uvicorn onibus.api.main:create_app --factory --reload

test:
	uv run --locked pytest

lint:
	uv run --locked ruff check .
	uv run --locked ruff format --check .
	uv run --locked mypy

format:
	uv run --locked ruff format .
