# syntax=docker/dockerfile:1
# Imagem única da API e do worker. O comando de cada um fica no docker-compose.yml.
FROM python:3.12-slim-trixie

COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /uvx /bin/

# O ambiente fica em /opt/venv, separado do código copiado para /app.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependências primeiro: a camada só é refeita quando o uv.lock muda.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

EXPOSE 8000
CMD ["uvicorn", "onibus.api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
