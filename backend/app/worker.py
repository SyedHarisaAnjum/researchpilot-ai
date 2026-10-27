"""Background worker that runs research jobs from the Redis queue (JOB_BACKEND=arq).

    arq app.worker.WorkerSettings

Delivery is at-least-once: if the worker stops mid-job, the job returns to pending and the
queue runs it again from the start, up to JOB_MAX_TRIES times.
"""

import logging
from datetime import timedelta

from arq.connections import RedisSettings
from sqlalchemy import select

from app.agent.limits import limits_for_depth
from app.config import get_settings
from app.db.databases import SessionLocal, engine
from app.db.models import ResearchJob, utcnow
from app.logging_setup import configure_logging
from app.services.job_service import _event, job_manager
from app.services.llm_service import get_llm_service
from app.services.search_service import get_search_service
from app.services.web_reader import get_web_reader
from app.utils.run_context import current_job_id

logger = logging.getLogger("app.worker")
settings = get_settings()
# Longer than the deepest job's own time limit, so the research limit always fires first.
JOB_TIMEOUT_SECONDS = limits_for_depth("deep")["max_research_seconds"] + 600


async def run_research_job(ctx: dict, job_id: str) -> None:
    current_job_id.set(job_id)  # tag this job's log lines from the start
    logger.info("Picked up research job %s (attempt %s)", job_id, ctx.get("job_try"))
    await job_manager.run_job(job_id, requeue_on_interrupt=True)


async def fail_abandoned_jobs() -> None:
    """Jobs the queue gave up on (all tries used) would otherwise stay "running" forever."""
    cutoff = utcnow() - timedelta(seconds=JOB_TIMEOUT_SECONDS * settings.job_max_tries)
    async with SessionLocal() as session:
        jobs = (await session.scalars(
            select(ResearchJob).where(ResearchJob.status.in_(["pending", "running"]), ResearchJob.updated_at < cutoff)
        )).all()
        for job in jobs:
            message = "Abandoned: the job did not finish after repeated attempts"
            job.status, job.stage, job.error, job.error_code, job.completed_at = "failed", "failed", message, "interrupted", utcnow()
            session.add(_event(job.id, "failed", stage="failed", progress=job.progress, message=message, error_code="interrupted"))
        await session.commit()
    if jobs:
        logger.warning("Marked %d abandoned research job(s) as failed", len(jobs))


async def startup(ctx: dict) -> None:
    configure_logging()
    # arq installs its own plain-text handler; send its records through ours instead.
    arq_logger = logging.getLogger("arq")
    arq_logger.handlers, arq_logger.propagate = [], True
    await fail_abandoned_jobs()
    logger.info("Worker ready: up to %d jobs at once", settings.max_concurrent_jobs)


async def shutdown(ctx: dict) -> None:
    for get_service in (get_llm_service, get_search_service, get_web_reader):
        if get_service.cache_info().currsize:
            await get_service().aclose()
    await engine.dispose()


class WorkerSettings:
    functions = [run_research_job]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url or "redis://localhost:6379")
    max_jobs = settings.max_concurrent_jobs
    job_timeout = JOB_TIMEOUT_SECONDS
    max_tries = settings.job_max_tries
    keep_result = 3600
    health_check_interval = 30
