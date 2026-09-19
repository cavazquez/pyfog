from pathlib import Path

import pytest

from pyfog.config import (
    DEVELOPMENT_ALLOWED_HOSTS,
    Settings,
    environment_flag,
    environment_int,
    environment_list,
    session_secret,
)

SECRET = "explicit-secret-with-at-least-32-characters"  # pragma: allowlist secret


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_environment_flag_accepts_truthy_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PYFOG_FLAG", value)
    assert environment_flag("PYFOG_FLAG") is True


@pytest.mark.parametrize("value", ["0", "false", "NO", " off "])
def test_environment_flag_accepts_falsy_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("PYFOG_FLAG", value)
    assert environment_flag("PYFOG_FLAG", default=True) is False


def test_environment_helpers_handle_defaults_and_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PYFOG_FLAG", raising=False)
    monkeypatch.delenv("PYFOG_LIST", raising=False)
    monkeypatch.delenv("PYFOG_NUMBER", raising=False)
    assert environment_flag("PYFOG_FLAG") is False
    assert environment_flag("PYFOG_FLAG", default=True) is True
    assert environment_list("PYFOG_LIST") == []
    assert environment_int("PYFOG_NUMBER", default=15) == 15

    monkeypatch.setenv("PYFOG_LIST", " first, ,second ")
    monkeypatch.setenv("PYFOG_NUMBER", " 42 ")
    assert environment_list("PYFOG_LIST") == ["first", "second"]
    assert environment_int("PYFOG_NUMBER", default=15) == 42

    monkeypatch.setenv("PYFOG_FLAG", "sometimes")
    with pytest.raises(ValueError, match="true o false"):
        environment_flag("PYFOG_FLAG")
    monkeypatch.setenv("PYFOG_NUMBER", "not-a-number")
    with pytest.raises(ValueError, match="entero"):
        environment_int("PYFOG_NUMBER", default=15)
    monkeypatch.setenv("PYFOG_NUMBER", " ")
    assert environment_int("PYFOG_NUMBER", default=15) == 15


def test_session_secret_supports_environment_and_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYFOG_SECRET_KEY", SECRET)
    monkeypatch.delenv("PYFOG_SECRET_KEY_FILE", raising=False)
    assert session_secret() == SECRET

    secret_file = tmp_path / "secret"
    secret_file.write_text(f"{SECRET}\n", encoding="utf-8")
    monkeypatch.delenv("PYFOG_SECRET_KEY", raising=False)
    monkeypatch.setenv("PYFOG_SECRET_KEY_FILE", str(secret_file))
    assert session_secret() == SECRET

    monkeypatch.setenv("PYFOG_SECRET_KEY", SECRET)
    with pytest.raises(ValueError, match="no ambos"):
        session_secret()
    monkeypatch.delenv("PYFOG_SECRET_KEY", raising=False)
    monkeypatch.setenv("PYFOG_SECRET_KEY_FILE", str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="No se pudo leer"):
        session_secret()


def test_settings_normalizes_development_hosts_and_generates_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PYFOG_SECRET_KEY", raising=False)
    monkeypatch.delenv("PYFOG_SECRET_KEY_FILE", raising=False)
    settings = Settings(allowed_hosts=[" app.test ", ""], trusted_proxy_ips=[" 192.0.2.1 "])
    assert settings.allowed_hosts == ["app.test"]
    assert settings.trusted_proxy_ips == ["192.0.2.1"]
    assert len(settings.secret_key) >= 32

    development = Settings(allowed_hosts=[], trusted_proxy_ips=[])
    assert development.allowed_hosts == DEVELOPMENT_ALLOWED_HOSTS


def production_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "production": True,
        "secret_key": SECRET,
        "allowed_hosts": ["pyfog.test"],
        "trusted_proxy_ips": ["192.0.2.1"],
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"secret_key": "short"}, "PYFOG_SECRET_KEY"),  # pragma: allowlist secret
        ({"allowed_hosts": []}, "PYFOG_ALLOWED_HOSTS"),
        ({"debug": True}, "PYFOG_DEBUG"),
        ({"allowed_hosts": ["*"]}, "no puede incluir"),
        ({"trusted_proxy_ips": []}, "PYFOG_TRUSTED_PROXY_IPS"),
        ({"trusted_proxy_ips": ["*"]}, "no puede incluir"),
    ],
)
def test_settings_rejects_insecure_production_configuration(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        production_settings(**overrides)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("max_body_bytes", 0, "max_body_bytes"),
        ("token_seconds", 0, "token_seconds"),
        ("token_rotation_grace_seconds", 0, "token_rotation_grace_seconds"),
        ("max_chunk_bytes", 0, "max_chunk_bytes"),
        ("max_image_bytes", 0, "max_image_bytes"),
        ("task_lease_seconds", 0, "tiempos de tareas"),
        ("task_heartbeat_seconds", 0, "tiempos de tareas"),
        ("coordinator_lease_seconds", 0, "coordinator_lease_seconds"),
        ("coordinator_lease_seconds", 301, "coordinator_lease_seconds"),
        ("min_storage_free_bytes", -1, "min_storage_free_bytes"),
    ],
)
def test_settings_rejects_invalid_limits(field: str, value: int, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Settings(**{field: value})


def test_settings_rejects_chunk_larger_than_body() -> None:
    with pytest.raises(ValueError, match="max_chunk_bytes"):
        Settings(max_body_bytes=10, max_chunk_bytes=11)
