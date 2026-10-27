from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import current_user
from app.config import get_settings
from app.db.databases import get_session
from app.db.models import (
    JOB_STATUSES,
    TERMINAL_STATUSES,
    ResearchEvidence,
    ResearchJob,
    ResearchReport,
    ResearchSource,
    User,
)
from app.schemas import JobDetail, JobList, JobSummary, ReportResponse, ResearchRequest, SourceOut
from app.services.job_service import job_manager
from app.services.rate_limit import RateLimitExceeded, get_limiter

router = APIRouter(prefix="/api/research", tags=["research"])

ACTIVE = ["pending", "running"]


def visible_to(user: User):
    """Users see their own jobs; admins see every job."""
    return True if user.is_admin else ResearchJob.user_id == user.id


async def get_job_or_404(session: AsyncSession, job_id: str, user: User) -> ResearchJob:
    job = await session.get(ResearchJob, job_id)
    # Someone else's job is reported as missing, so job IDs can't be probed.
    if job is None or not (user.is_admin or job.user_id == user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Research job {job_id} not found")
    return job


async def job_detail(session: AsyncSession, job: ResearchJob) -> JobDetail:
    source_count = await session.scalar(select(func.count()).where(ResearchSource.job_id == job.id))
    evidence_count = await session.scalar(select(func.count()).where(ResearchEvidence.job_id == job.id))
    report_id = await session.scalar(select(ResearchReport.id).where(ResearchReport.job_id == job.id))
    return JobDetail.model_validate(job).model_copy(
        update={"source_count": source_count or 0, "evidence_count": evidence_count or 0, "report_id": report_id}
    )


@router.post("", response_model=JobDetail, status_code=status.HTTP_202_ACCEPTED)
async def create_research(
    request: ResearchRequest, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> JobDetail:
    """Start a research job. Follow its progress at /api/research/{id}/events.
    Rate limited per user, and refused while too many jobs are already queued or running."""
    settings = get_settings()
    active = await session.scalar(select(func.count()).where(ResearchJob.status.in_(ACTIVE))) or 0
    if active >= settings.max_active_jobs:
        raise RateLimitExceeded(
            f"{active} research jobs are already queued or running. Try again when one finishes.",
            retry_after=30, code="too_many_active_jobs",
        )
    mine = await session.scalar(
        select(func.count()).where(ResearchJob.status.in_(ACTIVE), ResearchJob.user_id == user.id)
    ) or 0
    if mine >= settings.max_active_jobs_per_user:
        raise RateLimitExceeded(
            f"You already have {mine} research jobs running. Wait for one to finish or cancel it.",
            retry_after=30, code="too_many_active_jobs",
        )
    await get_limiter().hit(
        f"jobs:{user.id}",
        [(settings.research_jobs_per_minute, 60), (settings.research_jobs_per_hour, 3600)],
        "research jobs started",
    )
    job = await job_manager.create_job(request.question, request.depth, user_id=user.id)
    return await job_detail(session, await get_job_or_404(session, job.id, user))


@router.get("", response_model=JobList)
async def list_research(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status_filter: str | None = Query(None, alias="status", pattern=f"^({'|'.join(JOB_STATUSES)})$"),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> JobList:
    """Your recent research jobs, newest first (all jobs, for admins)."""
    query = select(ResearchJob).where(visible_to(user))
    if status_filter:
        query = query.where(ResearchJob.status == status_filter)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    jobs = await session.scalars(query.order_by(ResearchJob.created_at.desc()).limit(limit).offset(offset))
    return JobList(jobs=[JobSummary.model_validate(j) for j in jobs], total=total or 0, limit=limit, offset=offset)


@router.get("/{job_id}", response_model=JobDetail)
async def get_research(job_id: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> JobDetail:
    return await job_detail(session, await get_job_or_404(session, job_id, user))


@router.get("/{job_id}/report", response_model=ReportResponse)
async def get_report(job_id: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> ReportResponse:
    job = await get_job_or_404(session, job_id, user)
    report = await session.scalar(select(ResearchReport).where(ResearchReport.job_id == job_id))
    if report is None:
        if job.status not in TERMINAL_STATUSES:
            raise HTTPException(status.HTTP_409_CONFLICT, f"The report is not ready yet (job is {job.status}, {job.progress}%)")
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"This job has no report (job {job.status}: {job.error or 'no details'})")
    return ReportResponse(report_id=report.id, job_id=job_id, completed_at=report.completed_at, report=report.report)


@router.post("/{job_id}/cancel", response_model=JobDetail, status_code=status.HTTP_202_ACCEPTED)
async def cancel_research(job_id: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> JobDetail:
    job = await get_job_or_404(session, job_id, user)
    if job.status in TERMINAL_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Job is already {job.status}")
    await job_manager.cancel(job_id)
    await session.refresh(job)  # the cancel was recorded in another session
    return await job_detail(session, job)


@router.get("/{job_id}/sources", response_model=list[SourceOut])
async def get_sources(job_id: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> list[SourceOut]:
    """Every page the job tried to read, including ones that failed. Saved when the job finishes."""
    await get_job_or_404(session, job_id, user)
    counts = dict(
        (await session.execute(
            select(ResearchEvidence.source_id, func.count())
            .where(ResearchEvidence.job_id == job_id)
            .group_by(ResearchEvidence.source_id)
        )).all()
    )
    sources = await session.scalars(
        select(ResearchSource).where(ResearchSource.job_id == job_id).order_by(ResearchSource.id)
    )
    return [SourceOut.model_validate(s).model_copy(update={"evidence_count": counts.get(s.source_id, 0)}) for s in sources]
