"""Formato de erro compartilhado pelas rotas da API."""

from collections.abc import Mapping
from http import HTTPStatus

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


def error_response(
    status: int, code: str, message: str, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    body = ErrorResponse(error=ErrorDetail(code=code, message=message))
    return JSONResponse(status_code=status, content=body.model_dump(), headers=headers)


async def handle_http_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, HTTPException):
        return await handle_unexpected_error(request, exc)
    try:
        code = HTTPStatus(exc.status_code).name.lower()
    except ValueError:
        code = "http_error"
    message = exc.detail if isinstance(exc.detail, str) else "Request failed."
    return error_response(exc.status_code, code, message, exc.headers)


async def handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    return error_response(422, "validation_error", "Invalid request.")


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    return error_response(500, "internal_server_error", "Internal server error.")
