"""Database: jobs, sources, evidence and reports survive an application restart."""

import sqlite3
import time

from sqlalchemy.engine import make_url

from app.db.databases import database_url
from tests.conftest import signed_in_client

QUESTION = "Compare the free tiers of popular LLM APIs"


def wait_for(client, job_id, statuses, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/research/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.05)
    raise AssertionError(job)


def raw_db():
    return sqlite3.connect(make_url(database_url()).database)


def test_completed_job_and_report_survive_a_restart(services):
    with signed_in_client() as client:  # first "run" of the server
        job = client.post("/api/research", json={"question": QUESTION, "depth": "quick"}).json()
        before = wait_for(client, job["id"], {"completed"})
        report_before = client.get(f"/api/research/{job['id']}/report").json()

    with signed_in_client() as client:  # restarted server: same database, fresh process state
        after = client.get(f"/api/research/{job['id']}").json()
        assert after["status"] == "completed"
        assert (after["source_count"], after["evidence_count"], after["report_id"]) == (
            before["source_count"], before["evidence_count"], before["report_id"],
        )
        assert client.get(f"/api/research/{job['id']}/report").json() == report_before
        assert client.get(f"/api/reports/{before['report_id']}/download").status_code == 200

    with raw_db() as db:
        hashes = db.execute("select content_hash from research_sources where job_id=? and fetch_status='success'", (job["id"],)).fetchall()
        assert hashes and all(len(h[0]) == 64 for h in hashes)
        orphans = db.execute(
            "select count(*) from research_evidence e left join research_sources s "
            "on s.job_id = e.job_id and s.source_id = e.source_id where e.job_id=? and s.id is null", (job["id"],)
        ).fetchone()[0]
        assert orphans == 0


def test_job_running_at_shutdown_is_marked_interrupted(services):
    services.reader.delay = 5
    with signed_in_client() as client:
        job = client.post("/api/research", json={"question": QUESTION}).json()
        wait_for(client, job["id"], {"running"})
    # Shutdown happened mid-run.
    with signed_in_client() as client:
        after = client.get(f"/api/research/{job['id']}").json()
        assert after["status"] == "failed" and "Interrupted" in after["error"]


def test_unfinished_jobs_are_recovered_on_startup(services):
    with signed_in_client() as client:
        job = client.post("/api/research", json={"question": QUESTION, "depth": "quick"}).json()
        wait_for(client, job["id"], {"completed"})
    with raw_db() as db:  # pretend the process died while it was running
        db.execute("update research_jobs set status='running', completed_at=null where id=?", (job["id"],))
    with signed_in_client() as client:
        after = client.get(f"/api/research/{job['id']}").json()
        assert after["status"] == "failed" and "server stopped" in after["error"]


def test_schema_is_at_the_latest_migration():
    with raw_db() as db:
        version = db.execute("select version_num from alembic_version").fetchone()[0]
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from app.config import BACKEND_DIR

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    assert version == ScriptDirectory.from_config(config).get_current_head()
