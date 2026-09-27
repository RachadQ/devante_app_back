import pytest

from app.config import Settings, get_settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    values = {
        "MONGODB_URI": "mongodb://localhost/test",
        "JWT_SECRET": "j" * 32,
        "SESSION_SECRET": "s" * 32,
        "FRONTEND_URL": "https://frontend.example.com",
        "CORS_ALLOWED_ORIGINS": "https://frontend.example.com",
        "TRUSTED_HOSTS": "backend.example.com",
        "MICROSOFT_CLIENT_ID": "test-client",
        "MICROSOFT_CLIENT_SECRET": "test-only",
        "MICROSOFT_TENANT_ID": "test-tenant",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_blank_optional_cookie_settings_keep_secure_defaults(monkeypatch):
    for name in ("AUTH_COOKIE_SECURE", "SESSION_COOKIE_SECURE", "AUTH_COOKIE_SAMESITE"):
        monkeypatch.setenv(name, "")
    settings = get_settings()
    assert settings.auth_cookie_secure is True
    assert settings.session_cookie_secure is True
    assert settings.auth_cookie_samesite == "lax"


@pytest.mark.parametrize("name", ["MONGODB_URI", "JWT_SECRET", "SESSION_SECRET", "FRONTEND_URL"])
def test_blank_required_settings_fail_closed(monkeypatch, name):
    monkeypatch.setenv(name, "")
    with pytest.raises(RuntimeError, match=name):
        get_settings()


def test_invalid_values_are_not_exposed(monkeypatch):
    monkeypatch.setenv("MONGODB_URI", "private-invalid-value")
    with pytest.raises(RuntimeError) as error:
        get_settings()
    assert "MONGODB_URI" in str(error.value)
    assert "private-invalid-value" not in str(error.value)
    assert error.value.__suppress_context__ is True


@pytest.mark.parametrize("name", ["MICROSOFT_TENANT_ID", "CORS_ALLOWED_ORIGINS", "TRUSTED_HOSTS"])
def test_blank_production_security_settings_fail_closed(monkeypatch, name):
    monkeypatch.setenv(name, "")
    with pytest.raises(RuntimeError):
        get_settings()


def test_insecure_cookie_override_still_rejected(monkeypatch):
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    with pytest.raises(RuntimeError, match="Secure cookies"):
        get_settings()
