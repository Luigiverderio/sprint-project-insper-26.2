"""Ponto de entrada da aplicação FastAPI."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException
from starlette.middleware.errors import ServerErrorMiddleware

from onibus.api.errors import (
    ErrorResponse,
    handle_http_error,
    handle_unexpected_error,
    handle_validation_error,
)
from onibus.api.health import router as health_router
from onibus.config import Settings
from onibus.db.connection import create_read_engine

logger = logging.getLogger(__name__)
v1_router = APIRouter(
    prefix="/v1",
    responses={422: {"model": ErrorResponse, "description": "Invalid request"}},
)


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
        engine = create_read_engine(settings)
        app.state.read_engine = engine
        logger.info("Application started environment=%s", settings.environment)
        try:
            yield
        finally:
            if engine is not None:
                await engine.dispose()
            logger.info("Application stopped")

    app = FastAPI(title=settings.name, version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(health_router)
    app.include_router(v1_router)
    app.add_exception_handler(HTTPException, handle_http_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    # CORS needs to wrap the error handler so it also adds headers to HTTP 500.
    app.add_middleware(ServerErrorMiddleware, handler=handle_unexpected_error)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET"],
    )
    return app
