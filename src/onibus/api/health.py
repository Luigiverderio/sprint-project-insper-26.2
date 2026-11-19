"""Verificação operacional da API e da coleta."""

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from onibus.api.errors import ErrorResponse
from onibus.db.connection import get_last_collection_at

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    status: str = "ok"
    last_collection_at: datetime | None


@router.get(
    "/health",
    responses={503: {"model": ErrorResponse, "description": "Database unavailable"}},
)
async def health(request: Request) -> HealthResponse:
    engine: AsyncEngine | None = request.app.state.read_engine
    if engine is None:
        raise HTTPException(503, "Database unavailable.")

    try:
        last_collection_at = await get_last_collection_at(engine)
    except (SQLAlchemyError, OSError) as exc:
        logger.warning("Database health check failed: %s", type(exc).__name__)
        raise HTTPException(503, "Database unavailable.") from exc

    return HealthResponse(last_collection_at=last_collection_at)
