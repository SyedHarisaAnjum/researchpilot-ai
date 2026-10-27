"""Production setup: config checks, the durable queue's retry behaviour, shared rate limits."""

import asyncio

import fakeredis
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.db.databases import SessionLocal
from app.db.models import ResearchJob
from app.services import rate_limit
from app.services.job_service import JobManager
from app.services.rate_limit import RateLimitExceeded, RedisLimiter

BASE = {"GEMINI_API_KEY": "k" * 10, "GEMINI_MODEL": "m", "TAVILY_API_KEY": "t" * 10}
GOOD = {**BASE, "ENVIRONMENT": "production", "DATABASE_URL": "postgresql+asyncpg://u:p@db/research",
        "JOB_BACKEND": "arq", "REDIS_URL": "redis://redis:6379/0"}


def settings_from(monkeypatch, env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def test_production_settings(monkeypatch):
    settings = settings_from(monkeypatch, GOOD)
    assert settings.secure_cookies is True and settings.docs_enabled is False


@pytest.mark.parametrize(
    "override, problem",
    [
        ({"DATABASE_URL": "sqlite+aiosqlite:///x.db"}, "PostgreSQL"),
        ({"JOB_BACKEND": "inprocess"}, "JOB_BACKEND"),
        ({"REDIS_URL": ""}, "REDIS_URL"),
        ({"COOKIE_SECURE": "false"}, "COOKIE_SECURE"),
    ],
)
def test_unsafe_production_settings_are_refused(monkeypatch, override, problem):
    with pytest.raises(ValidationError, match=problem):
        settings_from(monkeypatch, {**GOOD, **override})


async def make_job(question="Compare free LLM APIs") -> str:
    manager = JobManager()
    job = await manager.create_job(question, "quick")
    await manager.shutdown()  # discard the in-process task; the test drives run_job itself
    async with SessionLocal() as session:
        row = await session.get(ResearchJob, job.id)
        row.status, row.error, row.error_code, row.completed_at = "pending", None, None, None
        await session.commit()
    return job.id


async def status_of(job_id: str) -> ResearchJob:
    async with SessionLocal() as session:
        return await session.get(ResearchJob, job_id)


async def test_worker_shutdown_puts_the_job_back_in_the_queue(services):
    services.reader.delay = 5
    job_id = await make_job()
    manager = JobManager()
    task = asyncio.create_task(manager.run_job(job_id, requeue_on_interrupt=True))
    await asyncio.sleep(0.5)
    task.cancel()  # what Arq does when the worker stops
    await asyncio.gather(task, return_exceptions=True)
    job = await status_of(job_id)
    assert job.status == "pending" and job.error is None

    services.reader.delay = 0
    await manager.run_job(job_id, requeue_on_interrupt=True)  # the queue's retry
    assert (await status_of(job_id)).status == "completed"


async def test_cancel_reaches_a_job_running_in_another_process(services):
    services.reader.delay = 5
    job_id = await make_job()
    worker = JobManager()
    run = asyncio.create_task(worker.run_job(job_id, requeue_on_interrupt=True))
    await asyncio.sleep(0.5)
    async with SessionLocal() as session:  # the API process only writes the flag
        (await session.get(ResearchJob, job_id)).cancel_requested = True
        await session.commit()
    await asyncio.wait_for(run, timeout=5)  # the worker's watcher notices within a second
    job = await status_of(job_id)
    assert job.status == "cancelled" and job.error_code == "cancelled"


async def test_redis_rate_limits_are_shared():
    server = fakeredis.FakeServer()
    first, second = RedisLimiter("redis://fake"), RedisLimiter("redis://fake")
    for limiter in (first, second):  # two API processes, one Redis
        limiter.redis = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    windows = [(3, 60)]
    await first.hit("jobs:u1", windows)
    await second.hit("jobs:u1", windows)
    await first.hit("jobs:u1", windows)
    with pytest.raises(RateLimitExceeded) as error:
        await second.hit("jobs:u1", windows, "research jobs started")
    assert error.value.retry_after >= 1 and "3 per minute" in str(error.value)
    await second.hit("jobs:u2", windows)  # other keys are unaffected


def test_memory_limiter_is_the_default_without_redis():
    rate_limit._limiter = None
    assert isinstance(rate_limit.get_limiter(), rate_limit.MemoryLimiter)
