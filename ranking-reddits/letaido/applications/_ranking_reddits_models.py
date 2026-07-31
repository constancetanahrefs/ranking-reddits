"""Ranking Reddits — tables.

Lives in `console_site_db` (same DB as the Scrapbook) so a card can be pushed
into `scrapbook_items` in one transaction.

Shapes
------
RRWorkspace one monitored scope (an Ahrefs project / domain). Everything else hangs
           off it, so several projects live side by side in one app.
RRThread   one Reddit URL = one card, WITHIN a workspace. Deduped on
           (workspace_id, url_key) — the same thread can legitimately rank for two
           different projects and is a separate card in each.
RRHit      one (thread, keyword, source, country) SERP occurrence. A thread can be
           surfaced by many keywords and by both sources.
RRScan     one scan run — what it found, so "new since last scan" is auditable.
RRSetting  per-workspace config (project id, secret, filters).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db_cross import CrossBase as RRBase


def _now():
    return datetime.now(timezone.utc)


def _uid():
    return str(uuid.uuid4())


class RRWorkspace(RRBase):
    """One monitored scope. The switcher at the top of the app picks between these."""
    __tablename__ = "rr_workspaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    name: Mapped[str] = mapped_column(String(200), default="")           # display label
    target_domain: Mapped[str] = mapped_column(String(253), default="")
    brand_keywords: Mapped[list] = mapped_column(ARRAY(String), default=list)
    rt_project_id: Mapped[str] = mapped_column(String(32), default="")
    rt_project_name: Mapped[str] = mapped_column(String(200), default="")
    rt_tags: Mapped[list] = mapped_column(ARRAY(String), default=list)
    ahrefs_secret: Mapped[str] = mapped_column(String(100), default="ahrefs_oauth")
    sources: Mapped[list] = mapped_column(ARRAY(String), default=list)
    countries: Mapped[list] = mapped_column(ARRAY(String), default=list)
    max_serp_position: Mapped[int] = mapped_column(Integer, default=10)
    brand_limit: Mapped[int] = mapped_column(Integer, default=400)
    # manual keyword list, used when the RT project has none of its own
    manual_keywords: Mapped[list] = mapped_column(ARRAY(String), default=list)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    setup_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "target_domain": self.target_domain,
            "brand_keywords": list(self.brand_keywords or []),
            "rt_project_id": self.rt_project_id, "rt_project_name": self.rt_project_name,
            "rt_tags": list(self.rt_tags or []), "ahrefs_secret": self.ahrefs_secret,
            "sources": list(self.sources or []), "countries": list(self.countries or []),
            "max_serp_position": self.max_serp_position, "brand_limit": self.brand_limit,
            "manual_keywords": list(self.manual_keywords or []),
            "is_default": self.is_default, "setup_complete": self.setup_complete,
        }


class RRThread(RRBase):
    __tablename__ = "rr_threads"
    __table_args__ = (
        UniqueConstraint("workspace_id", "url_key", name="rr_threads_ws_key_uniq"),
    )

    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("rr_workspaces.id", ondelete="CASCADE"),
        index=True, default="")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    url_key: Mapped[str] = mapped_column(String(200), index=True)
    url: Mapped[str] = mapped_column(String(2000))
    title: Mapped[str] = mapped_column(String(600), default="")
    subreddit: Mapped[str] = mapped_column(String(120), default="", index=True)
    author: Mapped[str] = mapped_column(String(200), default="")
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")

    # best SERP position across all hits (1 = top)
    best_position: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    max_volume: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # citation counts summed across AI engines, from Brand Radar
    ai_citations: Mapped[int] = mapped_column(Integer, default=0)
    citation_counts: Mapped[dict] = mapped_column(JSONB, default=dict)
    sources: Mapped[list] = mapped_column(ARRAY(String), default=list)   # brand | keywords

    # card state
    is_new: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    read_by: Mapped[str] = mapped_column(String(200), default="")

    # enrichment (fetched when the card is opened)
    upvotes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    num_comments: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    upvote_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    body_md: Mapped[str] = mapped_column(Text, default="")
    fetch_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|running|done|failed
    fetch_error: Mapped[str] = mapped_column(Text, default="")
    fetched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # AI notes
    ai_notes: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    notes_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    notes_error: Mapped[str] = mapped_column(Text, default="")
    user_notes: Mapped[str] = mapped_column(Text, default="")

    scrapbook_item_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    def to_dict(self, hits: list | None = None) -> dict:
        return {
            "id": self.id, "url": self.url, "title": self.title,
            "subreddit": self.subreddit, "author": self.author,
            "posted_at": self.posted_at.isoformat() if self.posted_at else None,
            "description": self.description,
            "best_position": self.best_position, "max_volume": self.max_volume,
            "ai_citations": self.ai_citations, "citation_counts": self.citation_counts or {},
            "sources": list(self.sources or []),
            "is_new": self.is_new, "is_read": self.is_read,
            "read_at": self.read_at.isoformat() if self.read_at else None,
            "read_by": self.read_by,
            "upvotes": self.upvotes, "num_comments": self.num_comments,
            "upvote_ratio": self.upvote_ratio,
            "fetch_status": self.fetch_status, "fetch_error": self.fetch_error,
            "body_md": self.body_md,
            "ai_notes": self.ai_notes, "notes_status": self.notes_status,
            "notes_error": self.notes_error, "user_notes": self.user_notes,
            "scrapbook_item_id": self.scrapbook_item_id,
            "first_seen_at": self.first_seen_at.isoformat() if self.first_seen_at else None,
            "keywords": hits if hits is not None else None,
        }


class RRHit(RRBase):
    __tablename__ = "rr_hits"
    __table_args__ = (
        UniqueConstraint("thread_id", "keyword", "country", "source", name="rr_hits_uniq"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    thread_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("rr_threads.id", ondelete="CASCADE"), index=True)
    keyword: Mapped[str] = mapped_column(String(300), default="")
    country: Mapped[str] = mapped_column(String(8), default="")
    source: Mapped[str] = mapped_column(String(16), default="", index=True)  # brand | keywords
    serp_position: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    search_volume: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    serp_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    matched_brands: Mapped[list] = mapped_column(ARRAY(String), default=list)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class RRScan(RRBase):
    __tablename__ = "rr_scans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    workspace_id: Mapped[str] = mapped_column(String(36), index=True, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    trigger: Mapped[str] = mapped_column(String(24), default="manual")   # manual | monthly
    sources: Mapped[list] = mapped_column(ARRAY(String), default=list)
    keywords_used: Mapped[int] = mapped_column(Integer, default=0)
    threads_seen: Mapped[int] = mapped_column(Integer, default=0)
    threads_new: Mapped[int] = mapped_column(Integer, default=0)
    hits_new: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    log: Mapped[list] = mapped_column(JSONB, default=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "workspace_id": self.workspace_id,
            "status": self.status, "trigger": self.trigger,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "sources": list(self.sources or []), "keywords_used": self.keywords_used,
            "threads_seen": self.threads_seen, "threads_new": self.threads_new,
            "hits_new": self.hits_new, "error": self.error, "log": self.log or [],
        }


class RRSetting(RRBase):
    __tablename__ = "rr_settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now)
