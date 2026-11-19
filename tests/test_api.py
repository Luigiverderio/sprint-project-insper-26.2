import json
import logging
import socket

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import SecretStr

from onibus.api.errors import handle_http_error
from onibus.api.main import create_app, v1_router
from onibus.config import Settings


def test_app_exposes_documentation_and_logs_lifecycle(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(name="Test backend", environment="test")

    with caplog.at_level(logging.INFO):
        logging.getLogger().setLevel(logging.WARNING)
        app = create_app(settings)
        with TestClient(app) as client:
            assert client.get("/docs").status_code == 200
            schema = client.get("/openapi.json").json()
            assert schema["info"]["title"] == "Test backend"
            assert "/health" in schema["paths"]
            assert app.state.settings is settings
    assert "Application started environment=test" in caplog.text
    assert "Application stopped" in caplog.text


@pytest.mark.parametrize(
    ("level", "expected_messages"),
    [("DEBUG", ["debug message", "error message"]), ("ERROR", ["error message"])],
)
def test_app_applies_log_level_with_existing_handlers(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    level: str,
    expected_messages: list[str],
) -> None:
    monkeypatch.setenv("APP_LOG_LEVEL", level)
    root = logging.getLogger()
    app_logger = logging.getLogger("onibus.api.main")

    with caplog.at_level(logging.DEBUG):
        root.setLevel(logging.WARNING)
        handlers = tuple(root.handlers)

        create_app(Settings())
        app_logger.debug("debug message")
        app_logger.error("error message")

        assert tuple(root.handlers) == handlers
        assert caplog.messages == expected_messages


def test_health_returns_503_without_database_configuration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING), TestClient(create_app()) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "service_unavailable", "message": "Database unavailable."}
    }
    assert "APP_API_DATABASE_PASSWORD is not set" in caplog.text


def test_api_database_login_uses_its_own_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_API_DATABASE_USER", "api_login")
    monkeypatch.setenv("APP_API_DATABASE_PASSWORD", "api-secret")
    monkeypatch.setenv("APP_DATABASE_USER", "other_login")
    monkeypatch.setenv("APP_DATABASE_PASSWORD", "other-secret")

    settings = Settings()

    assert settings.api_database_user == "api_login"
    assert settings.api_database_password is not None
    assert settings.api_database_password.get_secret_value() == "api-secret"


async def test_http_error_handler_falls_back_for_other_exceptions() -> None:
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})

    response = await handle_http_error(request, RuntimeError("private detail"))

    assert response.status_code == 500
    assert json.loads(bytes(response.body)) == {
        "error": {"code": "internal_server_error", "message": "Internal server error."}
    }


def test_health_returns_503_when_database_is_down() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        settings = Settings(
            database_port=sock.getsockname()[1],
            api_database_password=SecretStr("test-password"),
        )
        with TestClient(create_app(settings)) as client:
            response = client.get("/health")
            assert client.get("/docs").status_code == 200

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


def test_cors_only_allows_the_configured_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = "https://frontend.example"
    monkeypatch.setenv("APP_CORS_ORIGINS", f'["{origin}"]')

    with TestClient(create_app()) as client:
        allowed = client.options(
            "/health",
            headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
        )
        blocked = client.get("/health", headers={"Origin": "https://other.example"})

    assert allowed.status_code == 200
    assert allowed.headers["Access-Control-Allow-Origin"] == origin
    assert "Access-Control-Allow-Origin" not in blocked.headers


@pytest.mark.parametrize(
    ("path", "status", "code"),
    [
        ("/missing", 404, "not_found"),
        ("/test?value=invalid", 422, "validation_error"),
        ("/test?value=5", 500, "internal_server_error"),
    ],
)
def test_errors_use_the_same_format(path: str, status: int, code: str) -> None:
    origin = "https://frontend.example"
    app: FastAPI = create_app(Settings(cors_origins=[origin]))

    @app.get("/test")
    async def sample(value: int) -> None:
        raise RuntimeError("private detail")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(path, headers={"Origin": origin})

    assert response.status_code == status
    assert response.headers.get("Access-Control-Allow-Origin") == origin
    assert set(response.json()) == {"error"}
    assert set(response.json()["error"]) == {"code", "message"}
    assert response.json()["error"]["code"] == code
    assert "private detail" not in response.text


def test_public_routes_are_mounted_under_v1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(v1_router, "routes", [])

    @v1_router.get("/sample")
    async def sample(value: int) -> dict[str, int]:
        return {"value": value}

    with TestClient(create_app()) as client:
        assert client.get("/v1/sample?value=1").json() == {"value": 1}
        assert client.get("/sample").status_code == 404
        validation_error = client.get("/v1/sample?value=invalid")
        openapi = client.get("/openapi.json").json()

    assert validation_error.status_code == 422
    assert validation_error.json() == {
        "error": {"code": "validation_error", "message": "Invalid request."}
    }
    response = openapi["paths"]["/v1/sample"]["get"]["responses"]["422"]
    schema = response["content"]["application/json"]["schema"]
    assert schema["$ref"] == "#/components/schemas/ErrorResponse"
