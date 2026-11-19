import pytest
from pydantic import ValidationError

from onibus.config import MigrationSettings, Settings


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


def test_migration_settings_read_compose_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POSTGRES_DB", "onibus_teste")
    monkeypatch.setenv("POSTGRES_PASSWORD", "s3nh@:/com#simbolos")
    monkeypatch.setenv("DB_PORT", "5433")
    # O compose ignora POSTGRES_USER e sempre cria o postgres; a migração também.
    monkeypatch.setenv("POSTGRES_USER", "outro_usuario")

    url = MigrationSettings().database_url()

    assert url.drivername == "postgresql+asyncpg"
    assert (url.username, url.host, url.port) == ("postgres", "127.0.0.1", 5433)
    assert url.database == "onibus_teste"
    assert url.password == "s3nh@:/com#simbolos"
    assert "s3nh" not in repr(url)


def test_migration_settings_require_password() -> None:
    with pytest.raises(ValueError, match="POSTGRES_PASSWORD"):
        MigrationSettings().database_url()
