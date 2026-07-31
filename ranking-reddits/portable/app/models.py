"""Tables. See docs/DATA_MODEL.md for why each field exists."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
    create_engine,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import config


class Base(DeclarativeBase):
    pass


engine = create_engine(config.database_url, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uid() -> str:
    return str(uuid.uuid4())


class Thread(Base):
    """One unique Reddit thread = one card."""
    __tablename__ = "threads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    # canonical "<subreddit>/<thread_id>" — the dedupe key, NOT the raw URL
    url_key: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    url: Mapped[str] = mapped_column(String(2000))
    title: Mapped[str] = mapped_column(String(600), default="")
    subreddit: Mapped[str] = mapped_column(String(120), default="", index=True)
    author: Mapped[str] = mapped_column(String(200), default="")
    # nullable on purpose: null renders "date unknown", never today
    posted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")

    best_position: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    max_volume: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ai_citations: Mapped[int] = mapped_column(Integer, default=0)
    citation_counts: Mapped[dict] = mapped_column(JSONB, default=dict)
    sources: Mapped[list] = mapped_column(ARRAY(String), default=list)

    is_new: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    read_by: Mapped[str] = mapped_column(String(200), default="")

    # ALL nullable on purpose — null means "not fetched". NEVER write 0 for unknown.
    upvotes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    num_comments: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    upvote_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    body_md: Mapped[str] = mapped_column(Text, default="")
    fetch_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    fetch_error: Mapped[str] = mapped_column(Text, default="")
    fetched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    ai_notes: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    notes_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    notes_error: Mapped[str] = mapped_column(Text, default="")
    user_notes: Mapped[str] = mapped_column(Text, default="")

    saved_ref: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    def to_dict(self, hits: list | None = None) -> dict:
        return {
            "id": self.id, "url": self.url, "title": self.title,
            "subreddit": self.subreddit, "author": self.author,
            "posted_at": self.posted_at.isoformat() if self.posted_at else None,
            "description": self.description,
            "best_position": self.best_position, "max_volume": self.max_volume,
            "ai_citations": self.ai_citations,
            "citation_counts": self.citation_counts or {},
            "sources": list(self.sources or []),
            "is_new": self.is_new, "is_read": self.is_read,
            "read_at": self.read_at.isoformat() if self.read_at else None,
            "read_by": self.read_by,
            "upvotes": self.upvotes, "num_comments": self.num_comments,
            "upvote_ratio": self.upvote_ratio,
            "body_md": self.body_md,
            "fetch_status": self.fetch_status, "fetch_error": self.fetch_error,
            "ai_notes": self.ai_notes, "notes_status": self.notes_status,
            "notes_error": self.notes_error, "user_notes": self.user_notes,
            "saved_ref": self.saved_ref,
            "first_seen_at": self.first_seen_at.isoformat() if self.first_seen_at else None,
            "keywords": hits if hits is not None else None,
        }


class Hit(Base):
    """One (thread × keyword × country × source) SERP occurrence."""
    __tablename__ = "hits"
    __table_args__ = (
        UniqueConstraint("thread_id", "keyword", "country", "source", name="hits_uniq"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    thread_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("threads.id", ondelete="CASCADE"), index=True)
    keyword: Mapped[str] = mapped_column(String(300), default="")
    country: Mapped[str] = mapped_column(String(8), default="")
    source: Mapped[str] = mapped_column(String(16), default="", index=True)
    serp_position: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    search_volume: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    serp_updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)
    matched_brands: Mapped[list] = mapped_column(ARRAY(String), default=list)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Scan(Base):
    """Audit trail. Without this you can't tell 'nothing new' from 'it broke'."""
    __tablename__ = "scans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    trigger: Mapped[str] = mapped_column(String(24), default="manual")
    sources: Mapped[list] = mapped_column(ARRAY(String), default=list)
    keywords_used: Mapped[int] = mapped_column(Integer, default=0)
    threads_seen: Mapped[int] = mapped_column(Integer, default=0)
    threads_new: Mapped[int] = mapped_column(Integer, default=0)
    hits_new: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    log: Mapped[list] = mapped_column(JSONB, default=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "status": self.status, "trigger": self.trigger,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "sources": list(self.sources or []), "keywords_used": self.keywords_used,
            "threads_seen": self.threads_seen, "threads_new": self.threads_new,
            "hits_new": self.hits_new, "error": self.error, "log": self.log or [],
        }


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now)


def init_db() -> None:
    """Create tables, then recover anything a restart left mid-flight.

    The recovery step matters: background threads die on redeploy, so without it a
    card whose fetch was in flight stays `running` forever and can never be retried.
    Table names are read off the models so this keeps working if you rename them.
    """
    from sqlalchemy import text
    Base.metadata.create_all(engine)
    t, sc = Thread.__table__.name, Scan.__table__.name
    with engine.begin() as c:
        c.execute(text(f"UPDATE {t} SET fetch_status='pending' WHERE fetch_status='running'"))
        c.execute(text(f"UPDATE {t} SET notes_status='pending' WHERE notes_status='running'"))
        c.execute(text(f"UPDATE {sc} SET status='failed', "
                       "error='interrupted by a restart \u2014 re-run the scan' "
                       "WHERE status='running'"))
