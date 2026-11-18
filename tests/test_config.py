import pytest
from pydantic import ValidationError

from onibus.config import Settings


def test_settings_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_NAME", "Test backend")
    monkeypatch.setenv("APP_ENVIRONMENT", "test")
    monkeypatch.setenv("APP_LOG_LEVEL", "DEBUG")

    settings = Settings()

    assert settings.name == "Test backend"
    assert settings.environment == "test"
    assert settings.log_level == "DEBUG"


def test_settings_reject_invalid_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_LOG_LEVEL", "INVALID")
    with pytest.raises(ValidationError):
        Settings()
