"""Verificação operacional da API e da coleta."""

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from onibus.api.errors import ErrorResponse
from onibus.db.connection import get_collection_health

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    status: str = "ok"
    # Último ciclo de posições concluído, com sucesso ou falha.
    last_collection_at: datetime | None
    # Último ciclo de posições com sucesso: se ficar para trás de
    # last_collection_at, o coletor está rodando mas falhando.
    last_successful_collection_at: datetime | None


@router.get(
    "/health",
    responses={503: {"model": ErrorResponse, "description": "Database unavailable"}},
)
async def health(request: Request) -> HealthResponse:
    engine: AsyncEngine | None = request.app.state.read_engine
    if engine is None:
        raise HTTPException(503, "Database unavailable.")

    try:
        collection = await get_collection_health(engine)
    except (SQLAlchemyError, OSError) as exc:
        logger.warning("Database health check failed: %s", type(exc).__name__)
        raise HTTPException(503, "Database unavailable.") from exc

    return HealthResponse(
        last_collection_at=collection.last_collection_at,
        last_successful_collection_at=collection.last_successful_collection_at,
    )
