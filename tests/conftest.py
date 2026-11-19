import pytest

from onibus.config import MigrationSettings, Settings


@pytest.fixture(autouse=True)
def isolate_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Impede que os testes carreguem configurações locais ou segredos."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setitem(MigrationSettings.model_config, "env_file", None)
    for name in (
        "APP_NAME",
        "APP_ENVIRONMENT",
        "APP_LOG_LEVEL",
        "APP_OLHO_VIVO_TOKEN",
        "POSTGRES_HOST",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "DB_PORT",
    ):
        monkeypatch.delenv(name, raising=False)
