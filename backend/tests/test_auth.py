"""Accounts: sign-in, sessions, access to other users' research, admin-only actions, CSRF."""

import time

import pytest
from fastapi.testclient import TestClient

from app.auth.security import hash_password, password_problem, verify_password
from app.config import get_settings
from app.main import app
from tests.conftest import ADMIN_EMAIL, OTHER_EMAIL, PASSWORD, USER_EMAIL, login, signed_in_client

QUESTION = "Compare the free tiers of popular LLM APIs"


@pytest.fixture
def anonymous(services):
    with TestClient(app) as client:
        client.headers["X-Requested-With"] = "ResearchLab"
        yield client


def wait_done(client, job_id):
    deadline = time.time() + 15
    while client.get(f"/api/research/{job_id}").json()["status"] not in {"completed", "failed", "cancelled"}:
        assert time.time() < deadline
        time.sleep(0.05)


def test_password_hashing():
    stored = hash_password("a long passphrase")
    assert stored.startswith("scrypt$") and "a long passphrase" not in stored
    assert verify_password("a long passphrase", stored)
    assert not verify_password("a long passphrasE", stored)
    assert hash_password("same") != hash_password("same")  # salted
    assert not verify_password("x", "not-a-hash")
    assert password_problem("short") and password_problem(" padded password ") and not password_problem("long enough pw")


def test_sign_in_sets_a_secure_session_cookie(anonymous):
    response = anonymous.post("/api/auth/login", json={"email": " Tester@Example.com ", "password": PASSWORD})
    assert response.status_code == 200
    assert response.json()["email"] == USER_EMAIL and "password_hash" not in response.json()
    cookie = response.headers["set-cookie"].lower()
    assert "research_session=" in cookie and "httponly" in cookie and "samesite=strict" in cookie
    assert anonymous.get("/api/auth/me").json()["email"] == USER_EMAIL


@pytest.mark.parametrize("email, password", [(USER_EMAIL, "wrong password"), ("nobody@example.com", PASSWORD)])
def test_failed_sign_in_gives_one_generic_message(anonymous, email, password):
    response = anonymous.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 401
    assert response.json()["error"] == {"code": "not_authenticated", "message": "Incorrect email or password", "details": None}


def test_sign_in_attempts_are_rate_limited(anonymous, monkeypatch):
    monkeypatch.setattr(get_settings(), "login_attempts_per_15_minutes", 3)
    for _ in range(3):
        anonymous.post("/api/auth/login", json={"email": USER_EMAIL, "password": "wrong password"})
    blocked = anonymous.post("/api/auth/login", json={"email": USER_EMAIL, "password": PASSWORD})
    assert blocked.status_code == 429 and "Retry-After" in blocked.headers


def test_sign_out_ends_the_session(services):
    with signed_in_client() as client:
        assert client.post("/api/auth/logout").status_code == 204
        assert client.get("/api/auth/me").status_code == 401


def test_bearer_token_works_for_scripts(anonymous):
    response = anonymous.post("/api/auth/login", json={"email": USER_EMAIL, "password": PASSWORD})
    token = response.cookies["research_session"]
    anonymous.cookies.clear()
    assert anonymous.get("/api/auth/me").status_code == 401
    assert anonymous.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).json()["email"] == USER_EMAIL


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/api/research"), ("post", "/api/research"), ("get", "/api/research/x"),
        ("get", "/api/research/x/events"), ("get", "/api/research/x/report"), ("get", "/api/research/x/sources"),
        ("post", "/api/research/x/cancel"), ("get", "/api/reports/x/download"), ("get", "/api/admin/users"),
        ("get", "/api/auth/me"),
    ],
)
def test_everything_except_health_needs_sign_in(anonymous, method, path):
    response = getattr(anonymous, method)(path, **({"json": {"question": QUESTION}} if method == "post" else {}))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"
    assert anonymous.get("/api/health").status_code == 200


def test_state_changing_requests_need_the_csrf_header(services):
    with signed_in_client() as client:
        del client.headers["X-Requested-With"]
        refused = client.post("/api/research", json={"question": QUESTION})
        assert refused.status_code == 403 and refused.json()["error"]["code"] == "csrf_check_failed"
        assert client.get("/api/research").status_code == 200  # reads are fine


def test_users_cannot_see_each_others_research(services):
    with signed_in_client(USER_EMAIL) as owner:
        job = owner.post("/api/research", json={"question": QUESTION, "depth": "quick"}).json()
        wait_done(owner, job["id"])
        report_id = owner.get(f"/api/research/{job['id']}").json()["report_id"]

    with signed_in_client(OTHER_EMAIL) as other:
        for path in (f"/api/research/{job['id']}", f"/api/research/{job['id']}/report",
                     f"/api/research/{job['id']}/sources", f"/api/research/{job['id']}/events",
                     f"/api/reports/{report_id}/download"):
            assert other.get(path).status_code == 404, path  # reported as missing, not forbidden
        assert other.post(f"/api/research/{job['id']}/cancel").status_code == 404
        assert job["id"] not in {j["id"] for j in other.get("/api/research").json()["jobs"]}

    with signed_in_client(ADMIN_EMAIL) as admin:
        assert admin.get(f"/api/research/{job['id']}").status_code == 200


def test_per_user_active_job_limit(services, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_active_jobs_per_user", 1)
    services.reader.delay = 2
    with signed_in_client() as client:
        first = client.post("/api/research", json={"question": QUESTION}).json()
        refused = client.post("/api/research", json={"question": QUESTION})
        assert refused.status_code == 429 and "You already have 1" in refused.json()["error"]["message"]
        client.post(f"/api/research/{first['id']}/cancel")


def test_admin_manages_accounts(services):
    with signed_in_client(USER_EMAIL) as user:
        assert user.get("/api/admin/users").status_code == 403
        assert user.post("/api/admin/users", json={"email": "x@example.com", "password": "long enough pw"}).status_code == 403

    with signed_in_client(ADMIN_EMAIL) as admin:
        created = admin.post("/api/admin/users", json={"email": "New.Person@Example.com", "password": "long enough pw", "name": "New"})
        assert created.status_code == 201 and created.json()["email"] == "new.person@example.com"
        assert admin.post("/api/admin/users", json={"email": "new.person@example.com", "password": "long enough pw"}).status_code == 400
        assert admin.post("/api/admin/users", json={"email": "weak@example.com", "password": "short"}).status_code == 400
        user_id = created.json()["id"]

        with TestClient(app) as person:
            login(person, "new.person@example.com", "long enough pw")
            assert admin.patch(f"/api/admin/users/{user_id}", json={"is_active": False}).json()["is_active"] is False
            assert person.get("/api/auth/me").status_code == 401  # deactivation ends sessions at once
            refused = person.post("/api/auth/login", json={"email": "new.person@example.com", "password": "long enough pw"})
            assert refused.status_code == 401

        me = admin.get("/api/auth/me").json()
        assert admin.patch(f"/api/admin/users/{me['id']}", json={"is_active": False}).status_code == 400


def test_api_sends_security_headers(services):
    with signed_in_client() as client:
        response = client.get("/api/research")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["cache-control"] == "no-store"
