"""Runs research jobs in the background and records their progress in the database.

Two backends (JOB_BACKEND):
- inprocess: jobs run as asyncio tasks inside the API process. Simple for local use, but a job
  doesn't survive a restart: on startup, unfinished jobs are marked failed.
- arq: the API puts jobs on a Redis queue and a separate worker (app/worker.py) runs them.
  If the worker stops mid-job, the job goes back to pending and the queue retries it.
"""

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, delete, select, update

from app.agent.graph import stream_research
from app.agent.limits import Depth
from app.agent.nodes import research_stop_reason
from app.agent.state import ResearchState, initial_state
from app.config import get_settings
from app.db.databases import SessionLocal
from app.utils.run_context import research_context, tokens_used
from app.db.models import (
    TERMINAL_STATUSES,
    ResearchEvent,
    ResearchEvidence,
    ResearchJob,
    ResearchReport,
    ResearchSource,
    utcnow,
)

logger = logging.getLogger(__name__)

# How far through a round each step is, for the progress bar.
ROUND_STEPS = {"search_web": 0.25, "read_pages": 0.5, "extract_evidence": 0.75, "evaluate_gaps": 1.0, "plan_next_round": 0.0}


def _parse_time(value: str | None) -> datetime:
    if not value:
        return utcnow()
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def progress_for(node: str, state: ResearchState) -> tuple[str, int]:
    """(stage, percent) after a node finishes. Planning is 0-10%, rounds 10-85%, writing 90-100%."""
    if node == "plan_research":
        return ("failed", 10) if state["status"] == "failed" else ("searching", 10)
    if node == "finalize_report":
        return "completed", 100
    if node == "evaluate_gaps" and research_stop_reason(state):
        return "writing", 90
    rounds = state["limits"]["max_rounds"]
    done = (max(state["round_number"], 1) - 1) + ROUND_STEPS.get(node, 0)
    return state["status"], int(10 + 75 * done / rounds)


def describe(node: str, update: dict[str, Any], state: ResearchState) -> str:
    """A human-readable line for the progress stream."""
    round_number = state["round_number"]
    if node == "plan_research":
        if state["status"] == "failed":
            return "Could not create a research plan"
        plan = state["plan"]
        return f"Planned {len(plan['subquestions'])} subquestions and {len(state['pending_queries'])} searches"
    if node == "search_web":
        return f"Round {round_number}: found {len(update.get('search_results', []))} new search results"
    if node == "read_pages":
        sources = update.get("sources", [])
        read = sum(1 for s in sources if s["fetch_status"] == "success")
        return f"Round {round_number}: read {read} of {len(sources)} pages"
    if node == "extract_evidence":
        return f"Round {round_number}: extracted {len(update.get('evidence', []))} verified evidence items"
    if node == "evaluate_gaps":
        if reason := research_stop_reason(state):
            return f"Research complete ({reason}); writing the report"
        return f"Round {round_number}: {len(state['missing_questions'])} open questions remain"
    if node == "plan_next_round":
        return f"Starting round {round_number} with {len(state['pending_queries'])} new searches"
    if node == "finalize_report":
        report = state.get("report") or {}
        return "Partial report ready" if report.get("partial") else "Report ready"
    return node


def live_stats(state: ResearchState) -> dict[str, Any]:
    """Counters shown while a job runs; the report's stats replace them at the end."""
    sources = state.get("sources", [])
    return {
        "rounds": state["round_number"],
        "searches": state["search_calls"],
        "pages_attempted": len(sources),
        "pages_read": sum(1 for s in sources if s["fetch_status"] == "success"),
        "evidence_items": len(state.get("evidence", [])),
        "llm_calls": state["llm_calls"],
        "tokens": tokens_used() or 0,
    }


CANCEL_POLL_SECONDS = 1.0


class JobManager:
    """Creates jobs and runs them, either in this process or through the Arq worker queue."""

    def __init__(self) -> None:
        self.tasks: dict[str, asyncio.Task] = {}
        self._semaphore: asyncio.Semaphore | None = None
        self._queue = None  # arq.ArqRedis when JOB_BACKEND=arq

    @property
    def semaphore(self) -> asyncio.Semaphore:
        # Created lazily so it belongs to the running event loop.
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(get_settings().max_concurrent_jobs)
        return self._semaphore

    @property
    def uses_queue(self) -> bool:
        return get_settings().job_backend == "arq"

    async def startup(self) -> None:
        if self.uses_queue:
            from arq import create_pool
            from arq.connections import RedisSettings

            self._queue = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))

    async def shutdown(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._semaphore = None  # belongs to this event loop
        if self._queue is not None:
            await self._queue.aclose()
            self._queue = None

    async def ping_queue(self) -> bool:
        return self._queue is None or bool(await self._queue.ping())

    # --- Public API ---

    async def create_job(self, question: str, depth: Depth, user_id: str | None = None) -> ResearchJob:
        state = initial_state(question, depth)
        async with SessionLocal() as session:
            job = ResearchJob(
                id=state["research_id"], user_id=user_id, question=state["question"], depth=depth,
                limits=dict(state["limits"]),
            )
            session.add(job)
            await session.flush()  # the job row must exist before an event can point at it
            session.add(_event(job.id, "queued", stage="pending", progress=0, message="Research job created"))
            await session.commit()
        if self.uses_queue:
            # The job ID doubles as the queue ID, so the same job is never queued twice.
            await self._queue.enqueue_job("run_research_job", job.id, _job_id=job.id)
        else:
            self.start(job.id)
        return job

    def start(self, job_id: str) -> None:
        """In-process backend: run the job as a task in this event loop."""

        async def limited() -> None:
            async with self.semaphore:
                await self.run_job(job_id)

        task = asyncio.create_task(limited(), name=f"research-{job_id}")
        self.tasks[job_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(job_id, None))

    async def cancel(self, job_id: str) -> ResearchJob | None:
        """Ask a job to stop. Returns None if the job doesn't exist."""
        async with SessionLocal() as session:
            job = await session.get(ResearchJob, job_id)
            if job is None or job.status in TERMINAL_STATUSES:
                return job
            job.cancel_requested = True
            status = job.status
            await session.commit()
        if task := self.tasks.get(job_id):
            task.cancel()
            await asyncio.wait({task}, timeout=5)
            # Cancelled while still waiting for a free slot: the job never started, so record it here.
            await self._finish_stopped(job_id, None, "cancelled", "Cancelled by request", "cancelled")
        elif status == "pending":
            # Still waiting in the queue: stop it now; the worker will skip it.
            await self._finish_stopped(job_id, None, "cancelled", "Cancelled by request", "cancelled")
        else:
            # Running in a worker, which checks for cancellation every second.
            await self._wait_until_finished(job_id, timeout=5)
        async with SessionLocal() as session:
            return await session.get(ResearchJob, job_id)

    async def recover_interrupted_jobs(self) -> int:
        """In-process backend only: jobs can't resume after a restart, so mark unfinished ones failed.
        (With the queue, the worker runs them again instead.)"""
        if self.uses_queue:
            return 0
        async with SessionLocal() as session:
            jobs = (await session.scalars(select(ResearchJob).where(ResearchJob.status.in_(["pending", "running"])))).all()
            for job in jobs:
                message = "Interrupted: the server stopped before this job finished"
                job.status, job.stage, job.error, job.completed_at = "failed", "failed", message, utcnow()
                job.error_code = "interrupted"
                session.add(_event(job.id, "failed", stage="failed", progress=job.progress, message=message))
            await session.commit()
        return len(jobs)

    # --- Running a job (both backends) ---

    async def run_job(self, job_id: str, requeue_on_interrupt: bool = False) -> None:
        """Run one job to the end. A watcher stops it within a second of a cancel request, even when
        the request arrives at another process. With `requeue_on_interrupt` (the worker), a shutdown
        puts the job back to pending so the queue runs it again, instead of marking it failed."""
        work = asyncio.create_task(self._execute(job_id, requeue_on_interrupt))
        watcher = asyncio.create_task(self._watch_for_cancel(job_id, work))
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError:
            work.cancel()  # we are shutting down: let the job record how it ended
            await asyncio.gather(work, return_exceptions=True)
            raise
        finally:
            watcher.cancel()

    async def _watch_for_cancel(self, job_id: str, work: asyncio.Task) -> None:
        while not work.done():
            await asyncio.sleep(CANCEL_POLL_SECONDS)
            if await self._cancel_requested(job_id):
                work.cancel()
                return

    async def _execute(self, job_id: str, requeue_on_interrupt: bool) -> None:
        state: ResearchState | None = None
        try:
            async with SessionLocal() as session:
                job = await session.get(ResearchJob, job_id)
                if job is None or job.status in TERMINAL_STATUSES:
                    return
                if job.cancel_requested:
                    raise asyncio.CancelledError
                state = initial_state(job.question, job.depth, job.id)
                state["limits"] = {**state["limits"], **(job.limits or {})}  # the limits chosen at creation
                job.status, job.stage, job.progress, job.started_at = "running", "planning", 1, utcnow()
                job.error, job.error_code = None, None
                session.add(_event(job_id, "started", stage="planning", progress=1, message="Research started"))
                await session.commit()

            with research_context(job_id):
                logger.info("Research job %s started (%s)", job_id, state["depth"])
                async for node, node_update, state in stream_research(state):
                    await self._record_progress(job_id, node, node_update, state)
                    if await self._cancel_requested(job_id):
                        raise asyncio.CancelledError
                await self._finish(job_id, state)
        except asyncio.CancelledError:
            if await self._cancel_requested(job_id):
                await self._finish_stopped(job_id, state, "cancelled", "Cancelled by request", "cancelled")
            elif requeue_on_interrupt:
                await self._requeue(job_id)
            else:
                await self._finish_stopped(
                    job_id, state, "failed", "Interrupted: the server shut down while this job was running", "interrupted"
                )
        except Exception as exc:
            logger.exception("Research job %s failed", job_id)
            await self._finish_stopped(
                job_id, state, "failed", f"Unexpected error: {type(exc).__name__}: {exc}", "internal_error"
            )

    async def _requeue(self, job_id: str) -> None:
        async with SessionLocal() as session:
            job = await session.get(ResearchJob, job_id)
            if job is None or job.status in TERMINAL_STATUSES:
                return
            job.status, job.stage, job.progress = "pending", "pending", 0
            session.add(_event(job_id, "queued", stage="pending", progress=0,
                               message="The worker restarted; the research will start again"))
            await session.commit()
        logger.warning("Research job %s interrupted; left for the queue to retry", job_id)

    async def _wait_until_finished(self, job_id: str, timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            async with SessionLocal() as session:
                if await session.scalar(select(ResearchJob.status).where(ResearchJob.id == job_id)) in TERMINAL_STATUSES:
                    return
            await asyncio.sleep(0.25)

    async def _record_progress(self, job_id: str, node: str, node_update: dict[str, Any], state: ResearchState) -> None:
        stage, progress = progress_for(node, state)
        events = []
        if node != "finalize_report":  # the "completed" event follows, with the same news
            events.append(_event(job_id, "progress", stage=stage, progress=progress, node=node,
                                 round=state["round_number"], message=describe(node, node_update, state),
                                 stats=live_stats(state)))
        events += [
            _event(job_id, "warning", stage=stage, progress=progress, node=node, round=e.get("round"), message=e["message"])
            for e in node_update.get("errors", [])
        ]
        async with SessionLocal() as session:
            await session.execute(
                update(ResearchJob)
                .where(ResearchJob.id == job_id)
                # Never move the bar backwards.
                .values(stage=stage, progress=_max_progress(progress), stats=live_stats(state), updated_at=utcnow())
            )
            session.add_all(events)
            await session.commit()

    async def _cancel_requested(self, job_id: str) -> bool:
        async with SessionLocal() as session:
            return bool(await session.scalar(select(ResearchJob.cancel_requested).where(ResearchJob.id == job_id)))

    async def _finish(self, job_id: str, state: ResearchState) -> None:
        async with SessionLocal() as session:
            job = await session.get(ResearchJob, job_id)
            report_id = await _save_results(session, job, state)
            report = state.get("report") or {}
            if state["status"] == "completed" and report:
                job.status, job.stage, job.progress = "completed", "completed", 100
                session.add(_event(job_id, "completed", stage="completed", progress=100,
                                   message="Partial report ready" if report["partial"] else "Report ready",
                                   partial=report["partial"], report_id=report_id))
            else:
                errors = state.get("errors", [])
                job.status, job.stage = "failed", "failed"
                job.error = errors[-1]["message"] if errors else state.get("stop_reason") or "Research failed"
                job.error_code = next((e["code"] for e in reversed(errors) if e.get("code")), "research_failed")
                session.add(_event(job_id, "failed", stage="failed", progress=job.progress, message=job.error))
            job.completed_at = utcnow()
            await session.commit()

    async def _finish_stopped(
        self, job_id: str, state: ResearchState | None, status: str, message: str, code: str
    ) -> None:
        """Mark a job cancelled or failed, keeping whatever sources and evidence it had collected."""
        try:
            async with SessionLocal() as session:
                job = await session.get(ResearchJob, job_id)
                if job is None or job.status in TERMINAL_STATUSES:
                    return
                if state is not None:
                    await _save_results(session, job, state)
                job.status, job.stage, job.error, job.completed_at = status, status, message, utcnow()
                job.error_code = code
                session.add(_event(job_id, status, stage=status, progress=job.progress, message=message, error_code=code))
                await session.commit()
        except Exception:
            logger.exception("Could not record the end of research job %s", job_id)


def _max_progress(progress: int):
    return case((ResearchJob.progress > progress, ResearchJob.progress), else_=progress)


def _event(job_id: str, event_type: str, **data: Any) -> ResearchEvent:
    return ResearchEvent(job_id=job_id, type=event_type, data=data)


async def _save_results(session, job: ResearchJob, state: ResearchState) -> str | None:
    """Write sources, evidence and the report from the state; returns the report ID. Safe to call more than once."""
    for model in (ResearchEvidence, ResearchSource, ResearchReport):
        await session.execute(delete(model).where(model.job_id == job.id))

    content = {p["source_id"]: p["content"] for p in state.get("pages", [])}
    for s in state.get("sources", []):
        text = content.get(s["source_id"])
        session.add(
            ResearchSource(
                job_id=job.id, source_id=s["source_id"], url=s["url"], final_url=s["final_url"], domain=s["domain"],
                title=s["title"], published_at=s["published_at"], retrieved_at=_parse_time(s["retrieved_at"]),
                fetch_status=s["fetch_status"], error=s["error"],
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None,
                content_length=len(text or ""), query=s["query"], round=s["round"],
            )
        )
    await session.flush()  # sources must exist before evidence points at them
    for e in state.get("evidence", []):
        session.add(
            ResearchEvidence(
                job_id=job.id, evidence_id=e["evidence_id"], source_id=e["source_id"], claim=e["claim"],
                excerpt=e["supporting_excerpt"], subquestion=e["subquestion"], confidence=e["confidence"], round=e["round"],
            )
        )
    report_id = None
    if report := state.get("report"):
        # Added directly rather than via job.report, which would lazy-load (not allowed in async).
        record = ResearchReport(
            job_id=job.id, title=report["title"], report=report, markdown=report["markdown"], partial=report["partial"],
            completed_at=_parse_time(report.get("generated_at")),
        )
        session.add(record)
        await session.flush()
        report_id = record.id
        job.stats = report.get("stats")
    job.plan = state.get("plan") or None
    job.state = dict(state)
    return report_id


job_manager = JobManager()
