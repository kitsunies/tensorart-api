import pytest


@pytest.fixture(autouse=True)
def sign_key(monkeypatch):
    monkeypatch.setenv("TENSORART_SIGN_KEY", "test-key")
    monkeypatch.delenv("TENSORHUB_SIGN_KEY", raising=False)
