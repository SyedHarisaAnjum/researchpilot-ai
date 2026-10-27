"""Database tables. Change them through an Alembic migration (see migrations/README.md)."""

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.databases import Base

JOB_STATUSES = ("pending", "running", "completed", "failed", "cancelled")
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class User(Base):
    """An account. Accounts are created by an admin; there is no public sign-up."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)  # stored lowercased
    name: Mapped[str] = mapped_column(String(255), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserSession(Base):
    """A signed-in browser. Only a SHA-256 of the session token is stored."""

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    user_agent: Mapped[str] = mapped_column(String(255), default="")


class ResearchJob(Base):
    __tablename__ = "research_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # Null only for jobs created before accounts existed; those are visible to admins only.
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    question: Mapped[str] = mapped_column(Text)
    depth: Mapped[str] = mapped_column(String(16), default="standard")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    stage: Mapped[str] = mapped_column(String(32), default="pending")  # current step, e.g. "reading"
    progress: Mapped[int] = mapped_column(Integer, default=0)  # 0-100
    error: Mapped[str | None] = mapped_column(Text)
    # Why a job failed or stopped: llm_timeout, llm_rate_limited, llm_invalid_output, llm_error,
    # search_*, cancelled, interrupted, internal_error. Null for completed jobs.
    error_code: Mapped[str | None] = mapped_column(String(32))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    limits: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # The final ResearchState, kept for debugging and inspection.
    state: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    sources: Mapped[list["ResearchSource"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True
    )
    evidence: Mapped[list["ResearchEvidence"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True
    )
    report: Mapped["ResearchReport | None"] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True
    )


class ResearchSource(Base):
    __tablename__ = "research_sources"
    __table_args__ = (UniqueConstraint("job_id", "source_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("research_jobs.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[str] = mapped_column(String(8))  # S01, unique within a job
    url: Mapped[str] = mapped_column(Text)
    final_url: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[str | None] = mapped_column(String(32))  # as found on the page
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fetch_status: Mapped[str] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64))  # SHA-256 of the extracted text
    content_length: Mapped[int] = mapped_column(Integer, default=0)
    query: Mapped[str] = mapped_column(Text, default="")
    round: Mapped[int] = mapped_column(Integer, default=1)

    job: Mapped[ResearchJob] = relationship(back_populates="sources")


class ResearchEvidence(Base):
    __tablename__ = "research_evidence"
    __table_args__ = (
        UniqueConstraint("job_id", "evidence_id"),
        # Evidence must point at a source of the same job.
        ForeignKeyConstraint(
            ["job_id", "source_id"],
            ["research_sources.job_id", "research_sources.source_id"],
            ondelete="CASCADE",
        ),
        Index("ix_research_evidence_job_source", "job_id", "source_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("research_jobs.id", ondelete="CASCADE"))
    evidence_id: Mapped[str] = mapped_column(String(8))  # E001
    source_id: Mapped[str] = mapped_column(String(8))
    claim: Mapped[str] = mapped_column(Text)
    excerpt: Mapped[str] = mapped_column(Text)
    subquestion: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[str] = mapped_column(String(8))
    round: Mapped[int] = mapped_column(Integer, default=1)

    job: Mapped[ResearchJob] = relationship(back_populates="evidence")


class ResearchReport(Base):
    __tablename__ = "research_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("research_jobs.id", ondelete="CASCADE"), unique=True)
    title: Mapped[str] = mapped_column(Text)
    report: Mapped[dict[str, Any]] = mapped_column(JSON)
    markdown: Mapped[str] = mapped_column(Text)
    partial: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[ResearchJob] = relationship(back_populates="report")


class ResearchEvent(Base):
    """Progress events, stored so the SSE stream can resume after a disconnect or restart."""

    __tablename__ = "research_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("research_jobs.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(32))  # status, progress, warning, completed, failed, cancelled
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
