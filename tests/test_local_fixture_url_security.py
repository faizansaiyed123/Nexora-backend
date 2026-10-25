from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.core.config import Settings
from backend.services import url_security
from backend.services.url_security import SecurityValidationError, UrlSecurityService


FIXTURE_URL = "http://127.0.0.1:8877/fixtures/retail-price-js.html"


def settings(*, enabled: bool = False, environment: str = "development"):
    return SimpleNamespace(
        allow_local_test_fixtures=enabled,
        environment=environment,
    )


def test_local_fixture_urls_are_blocked_by_default(monkeypatch) -> None:
    monkeypatch.setattr(url_security, "get_settings", lambda: settings())
    with pytest.raises(SecurityValidationError):
        UrlSecurityService.validate_url(FIXTURE_URL, allow_empty=False)
    with pytest.raises(SecurityValidationError):
        UrlSecurityService.resolve_and_validate_host("127.0.0.1", 8877)


def test_only_allowlisted_loopback_fixture_urls_are_allowed_in_test_mode(monkeypatch) -> None:
    monkeypatch.setattr(url_security, "get_settings", lambda: settings(enabled=True))
    assert UrlSecurityService.validate_url(FIXTURE_URL, allow_empty=False) == FIXTURE_URL
    assert (
        UrlSecurityService.validate_url(
            "http://127.0.0.1:8877/fixtures/retail-price-high.html",
            allow_empty=False,
        )
        == "http://127.0.0.1:8877/fixtures/retail-price-high.html"
    )
    assert UrlSecurityService.resolve_and_validate_host("127.0.0.1", 8877) == "127.0.0.1"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8877/v1/health",
        "http://127.0.0.1:8877/fixtures/../v1/health",
        "http://127.0.0.1:8877/fixtures/retail-price-js.html?url=http://127.0.0.1:8000",
        "http://127.0.0.1:8878/fixtures/retail-price-js.html",
        "http://localhost:8877/fixtures/retail-price-js.html",
        "http://127.0.0.1:8877/fixtures/unknown.html",
    ],
)
def test_local_fixture_opt_in_does_not_allow_other_local_targets(monkeypatch, url: str) -> None:
    monkeypatch.setattr(url_security, "get_settings", lambda: settings(enabled=True))
    with pytest.raises(SecurityValidationError):
        UrlSecurityService.validate_url(url, allow_empty=False)


def test_production_configuration_rejects_local_fixture_opt_in() -> None:
    with pytest.raises(ValidationError, match="ALLOW_LOCAL_TEST_FIXTURES"):
        Settings(
            jwt_secret_key="test-runtime-secret-that-is-long-enough-12345",
            environment="production",
            allowed_hosts=["nexora.example.com"],
            database_url="postgresql+asyncpg://user:password@db.example.com/nexora",
            allow_local_test_fixtures=True,
        )
