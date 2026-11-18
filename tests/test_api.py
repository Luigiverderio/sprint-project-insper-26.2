import logging

import pytest
from fastapi.testclient import TestClient

from onibus.api.main import create_app
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
