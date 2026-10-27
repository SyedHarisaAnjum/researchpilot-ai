import asyncio
import json
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.databases import get_session
from app.auth.deps import current_user
from app.db.models import ResearchJob, ResearchReport, User
from app.services.pdf_service import PDFError, markdown_to_pdf

router = APIRouter(prefix="/api/reports", tags=["reports"])


def _filename(title: str, extension: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80] or "research-report"
    return f"{slug}.{extension}"


@router.get("/{report_id}/download")
async def download_report(
    report_id: str,
    format: Literal["markdown", "pdf", "json"] = Query("markdown"),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Download a report as Markdown, PDF or JSON."""
    report = await session.get(ResearchReport, report_id)
    owner = await session.scalar(select(ResearchJob.user_id).where(ResearchJob.id == report.job_id)) if report else None
    if report is None or not (user.is_admin or owner == user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Report {report_id} not found")
    if format == "pdf":
        try:
            # Rendering is CPU-bound; keep it off the event loop.
            content = await asyncio.to_thread(markdown_to_pdf, report.markdown, report.title)
        except PDFError as exc:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc
        media_type, extension = "application/pdf", "pdf"
    elif format == "json":
        content = json.dumps(report.report, ensure_ascii=False, indent=2).encode("utf-8")
        media_type, extension = "application/json; charset=utf-8", "json"
    else:
        content, media_type, extension = report.markdown.encode("utf-8"), "text/markdown; charset=utf-8", "md"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{_filename(report.title, extension)}"'},
    )
