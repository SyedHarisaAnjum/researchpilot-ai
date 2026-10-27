"""API: validation, unknown IDs, job lifecycle and SSE, through FastAPI's test client."""

import json
import time

import pytest

from tests.conftest import signed_in_client

TERMINAL = {"completed", "failed", "cancelled"}
QUESTION = "Compare the free tiers of popular LLM APIs"


@pytest.fixture
def client(services):
    with signed_in_client() as test_client:
        yield test_client


def wait_for(client, job_id, statuses=TERMINAL, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/research/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job stuck in {job['status']}")


def sse_messages(client, job_id, headers=None):
    with client.stream("GET", f"/api/research/{job_id}/events", headers=headers or {}) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())
    messages = []
    for block in body.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":"))
        if "event" in fields:
            messages.append({"id": int(fields["id"]) if "id" in fields else None, "event": fields["event"], "data": json.loads(fields["data"])})
    return messages


def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"


@pytest.mark.parametrize(
    "body, field",
    [
        ({"question": "too short"}, "question"),
        ({"question": "          "}, "question"),
        ({"question": QUESTION, "depth": "extreme"}, "depth"),
        ({"question": "x" * 2001}, "question"),
        ({}, "question"),
    ],
)
def test_invalid_requests_are_rejected(client, body, field):
    response = client.post("/api/research", json=body)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert any(d["loc"][-1] == field for d in error["details"])


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/api/research/unknown-id"),
        ("get", "/api/research/unknown-id/report"),
        ("get", "/api/research/unknown-id/sources"),
        ("get", "/api/research/unknown-id/events"),
        ("post", "/api/research/unknown-id/cancel"),
        ("get", "/api/reports/unknown-id/download"),
    ],
)
def test_unknown_ids_return_404(client, method, path):
    response = getattr(client, method)(path)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "http_404"


def test_unknown_route_uses_the_standard_error_shape(client):
    assert client.get("/api/nothing").json() == {"error": {"code": "http_404", "message": "Not Found", "details": None}}


def test_job_lifecycle(client):
    created = client.post("/api/research", json={"question": QUESTION, "depth": "quick"})
    assert created.status_code == 202
    job = created.json()
    assert job["status"] in {"pending", "running"} and job["limits"]["max_rounds"] == 1

    done = wait_for(client, job["id"])
    assert done["status"] == "completed" and done["progress"] == 100
    assert done["source_count"] > 0 and done["evidence_count"] > 0 and done["report_id"]

    report = client.get(f"/api/research/{job['id']}/report").json()
    assert report["report_id"] == done["report_id"] and report["report"]["sources"]

    sources = client.get(f"/api/research/{job['id']}/sources").json()
    assert sources[0]["source_id"] == "S01" and "evidence_count" in sources[0]

    markdown = client.get(f"/api/reports/{done['report_id']}/download")
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert "attachment" in markdown.headers["content-disposition"]
    assert markdown.text.startswith("# ")
    as_json = client.get(f"/api/reports/{done['report_id']}/download", params={"format": "json"})
    assert as_json.json()["title"] == report["report"]["title"]
    pdf = client.get(f"/api/reports/{done['report_id']}/download", params={"format": "pdf"})
    assert pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF")
    assert client.get(f"/api/reports/{done['report_id']}/download", params={"format": "docx"}).status_code == 422

    assert client.post(f"/api/research/{job['id']}/cancel").status_code == 409


def test_report_is_409_while_running_and_cancel_works(client, services):
    services.reader.delay = 2
    job = client.post("/api/research", json={"question": QUESTION}).json()
    wait_for(client, job["id"], {"running"})
    assert client.get(f"/api/research/{job['id']}/report").status_code == 409

    cancelled = client.post(f"/api/research/{job['id']}/cancel")
    assert cancelled.status_code == 202 and cancelled.json()["status"] == "cancelled"
    response = client.get(f"/api/research/{job['id']}/report")
    assert response.status_code == 404 and "cancelled" in response.json()["error"]["message"]


def test_list_and_filter(client):
    job = client.post("/api/research", json={"question": QUESTION, "depth": "quick"}).json()
    wait_for(client, job["id"])
    listed = client.get("/api/research", params={"limit": 5}).json()
    assert listed["jobs"][0]["id"] == job["id"] and listed["total"] >= 1
    assert all(j["status"] == "completed" for j in client.get("/api/research", params={"status": "completed"}).json()["jobs"])
    assert client.get("/api/research", params={"status": "bogus"}).status_code == 422
    assert client.get("/api/research", params={"limit": 0}).status_code == 422


def test_sse_stream_and_resume(client):
    job = client.post("/api/research", json={"question": QUESTION, "depth": "quick"}).json()
    wait_for(client, job["id"])

    messages = sse_messages(client, job["id"])
    events = [m["event"] for m in messages]
    assert events[0] == "research_state" and messages[0]["id"] is None
    assert events[1:3] == ["research_queued", "research_started"]
    assert "research_progress" in events and events[-2:] == ["research_completed", "end"]
    ids = [m["id"] for m in messages if m["id"] is not None]
    assert ids == sorted(ids)
    assert all(m["data"]["research_id"] == job["id"] for m in messages)

    resumed = sse_messages(client, job["id"], headers={"Last-Event-ID": str(ids[2])})
    assert resumed[0]["event"] == "research_state" and resumed[0]["data"]["status"] == "completed"
    assert [m["id"] for m in resumed if m["id"]] == ids[3:]


def test_failed_job_reports_the_cause(client, services):
    from app.services.llm_service import LLMError

    services.llm.handlers["ResearchPlan"] = LLMError("Model request failed (404): model not found")
    job = client.post("/api/research", json={"question": QUESTION}).json()
    done = wait_for(client, job["id"])
    assert done["status"] == "failed" and "model not found" in done["error"]
    assert sse_messages(client, job["id"])[-2]["event"] == "research_failed"
