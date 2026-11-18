"""Ponto de entrada da aplicação FastAPI."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from onibus.config import Settings

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Cria uma aplicação com configuração isolada e logging básico."""
    settings = settings if settings is not None else Settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger().setLevel(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("Application started environment=%s", settings.environment)
        yield
        logger.info("Application stopped")

    app = FastAPI(title=settings.name, version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    return app
