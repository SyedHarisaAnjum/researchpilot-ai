"""Test setup. Runs before any app module is imported.

Tests use a throwaway SQLite database and fake LLM/search/reader services, so they
never spend API quota. Live tests (marked `live`) use the real keys from .env and only
run with RUN_LIVE_TESTS=1.
"""

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

LIVE = os.environ.get("RUN_LIVE_TESTS") == "1"
_db_dir = Path(tempfile.mkdtemp(prefix="research-tests-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(_db_dir / 'test.db').as_posix()}"
os.environ["ENVIRONMENT"] = "test"
# Tests start many jobs quickly; rate limiting has its own test.
os.environ["RESEARCH_JOBS_PER_MINUTE"] = "1000"
os.environ["RESEARCH_JOBS_PER_HOUR"] = "10000"
if not LIVE:
    # Real keys in .env are never used by mocked tests.
    os.environ["GEMINI_API_KEY"] = "test-gemini-key"
    os.environ["TAVILY_API_KEY"] = "test-tavily-key"
    os.environ["GEMINI_MODEL"] = "test-model"

import asyncio  # noqa: E402
from contextlib import contextmanager  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.agent import nodes, planner  # noqa: E402
from app.auth.users import create_user  # noqa: E402
from app.db.databases import SessionLocal, run_migrations  # noqa: E402
from app.services import rate_limit, report_service  # noqa: E402
from tests.fakes import FakeLLM, FakeReader, FakeSearch  # noqa: E402

PASSWORD = "correct horse battery"
USER_EMAIL, OTHER_EMAIL, ADMIN_EMAIL = "tester@example.com", "other@example.com", "admin@example.com"


def login(client: TestClient, email: str = USER_EMAIL, password: str = PASSWORD) -> TestClient:
    """Sign the test client in (session cookie) and add the CSRF header the API requires."""
    client.headers["X-Requested-With"] = "ResearchLab"
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return client


@contextmanager
def signed_in_client(email: str = USER_EMAIL):
    from app.main import app

    with TestClient(app) as client:
        yield login(client, email)


def pytest_collection_modifyitems(config, items):
    if LIVE:
        return
    skip = pytest.mark.skip(reason="live test: set RUN_LIVE_TESTS=1 to run against real APIs")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session", autouse=True)
def database():
    run_migrations()

    async def accounts():
        async with SessionLocal() as session:
            await create_user(session, USER_EMAIL, PASSWORD, "Tester")
            await create_user(session, OTHER_EMAIL, PASSWORD, "Other")
            await create_user(session, ADMIN_EMAIL, PASSWORD, "Admin", is_admin=True)

    asyncio.run(accounts())
    yield


@pytest.fixture(autouse=True)
def fresh_rate_limits():
    rate_limit._limiter = None  # every test starts with empty counters
    yield
    rate_limit._limiter = None


@pytest.fixture
def services(monkeypatch):
    """Fake LLM, search and reader wired into the agent. Tests can change their behaviour."""
    fakes = SimpleNamespace(llm=FakeLLM(), search=FakeSearch(), reader=FakeReader())
    monkeypatch.setattr(nodes, "get_llm_service", lambda: fakes.llm)
    monkeypatch.setattr(planner, "get_llm_service", lambda: fakes.llm)
    monkeypatch.setattr(report_service, "get_llm_service", lambda: fakes.llm)
    monkeypatch.setattr(nodes, "get_search_service", lambda: fakes.search)
    monkeypatch.setattr(nodes, "get_web_reader", lambda: fakes.reader)
    return fakes
