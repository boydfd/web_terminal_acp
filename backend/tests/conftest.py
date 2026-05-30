import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.main import app


@pytest.fixture(autouse=True)
def disable_http_auth_for_legacy_tests():
    settings = get_settings()
    previous = settings.web_terminal_disable_auth_for_tests
    settings.web_terminal_disable_auth_for_tests = True
    try:
        yield
    finally:
        settings.web_terminal_disable_auth_for_tests = previous


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client
