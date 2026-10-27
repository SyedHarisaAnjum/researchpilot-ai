"""Server-Sent Events: live progress for a research job.

Every connection starts with a `research_state` event holding the job's current state,
then replays the stored events after the client's Last-Event-ID (the browser sends it
automatically when it reconnects) and streams new ones until the job ends. Events live in
the database, so a reconnect, or a server restart, never needs the job to start over.

    event: research_state       current status, stage, progress and counts (sent on connect, no id)
    event: research_queued      the job was created and is waiting to run
    event: research_started     the job began
    event: research_progress    a step finished: stage, progress %, message, counts
    event: research_warning     something went wrong but research continues (e.g. a page was blocked)
    event: research_completed   the report is ready (includes report_id)
    event: research_failed      the job stopped with an error
    event: research_cancelled   the job was cancelled
    event: end                  no more events will come; the server closes the stream
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.research import get_job_or_404
from app.auth.deps import current_user
from app.db.databases import SessionLocal, get_session
from app.db.models import TERMINAL_STATUSES, ResearchEvent, ResearchJob, User
from app.schemas import _as_utc

router = APIRouter(prefix="/api/research", tags=["research"])

POLL_SECONDS = 1.0
KEEPALIVE_SECONDS = 15
RETRY_MS = 3000


def event_name(event: ResearchEvent) -> str:
    kind = event.type
    if kind == "status":  # older rows: "created" and "started" were both "status"
        kind = "queued" if event.data.get("stage") == "pending" else "started"
    return f"research_{kind}"


def sse(event: str, data: dict[str, Any], event_id: int | None = None) -> str:
    """One SSE message. JSON is a single line, so it fits in one `data:` field."""
    lines = [f"id: {event_id}"] if event_id is not None else []
    lines += [f"event: {event}", f"data: {json.dumps(data, ensure_ascii=False, default=str)}"]
    return "\n".join(lines) + "\n\n"


def job_snapshot(job: ResearchJob, last_event_id: int | None) -> dict[str, Any]:
    return {
        "research_id": job.id,
        "status": job.status,
        "stage": job.stage,
        "progress": job.progress,
        "stats": job.stats or {},
        "error": job.error,
        "cancel_requested": job.cancel_requested,
        "updated_at": _as_utc(job.updated_at).isoformat(),
        "completed_at": _as_utc(job.completed_at).isoformat() if job.completed_at else None,
        # The newest stored event; the client has everything up to here once replay finishes.
        "last_event_id": last_event_id,
    }


async def event_stream(request: Request, job_id: str, after: int) -> AsyncIterator[str]:
    yield f"retry: {RETRY_MS}\n\n"
    async with SessionLocal() as session:
        job = await session.get(ResearchJob, job_id)
        latest = await session.scalar(
            select(ResearchEvent.id).where(ResearchEvent.job_id == job_id).order_by(ResearchEvent.id.desc()).limit(1)
        )
        yield sse("research_state", job_snapshot(job, latest))

    last_id = after
    idle = 0.0
    while not await request.is_disconnected():
        async with SessionLocal() as session:
            # Status first: once it's terminal, every event has already been written.
            job_status = await session.scalar(select(ResearchJob.status).where(ResearchJob.id == job_id))
            events = (
                await session.scalars(
                    select(ResearchEvent)
                    .where(ResearchEvent.job_id == job_id, ResearchEvent.id > last_id)
                    .order_by(ResearchEvent.id)
                    .limit(200)
                )
            ).all()
        for event in events:
            last_id = event.id
            name = event_name(event)
            payload = {
                "id": event.id,
                "research_id": job_id,
                "type": name,
                "created_at": _as_utc(event.created_at).isoformat(),
                **event.data,
            }
            yield sse(name, payload, event.id)
        if not events and job_status in TERMINAL_STATUSES:
            yield sse("end", {"research_id": job_id, "status": job_status})
            return
        idle = 0.0 if events else idle + POLL_SECONDS
        if idle >= KEEPALIVE_SECONDS:
            idle = 0.0
            yield ": keep-alive\n\n"
        if not events:
            await asyncio.sleep(POLL_SECONDS)


@router.get("/{job_id}/events")
async def research_events(
    job_id: str,
    request: Request,
    after: int = Query(0, ge=0, description="Only replay events with an ID greater than this"),
    last_event_id: str | None = Header(None),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """Stream progress as Server-Sent Events (see the module docstring for event types).
    Reconnecting with Last-Event-ID (or ?after=) resumes where the client left off."""
    await get_job_or_404(session, job_id, user)
    start = int(last_event_id) if last_event_id and last_event_id.isdigit() else after
    return StreamingResponse(
        event_stream(request, job_id, start),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
