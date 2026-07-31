"""Reddit Outpost — tables.

Lives in `console_site_db` (same DB as the Scrapbook and Ranking Reddits) so a
promising thread can be pushed into `scrapbook_items` in one transaction.

Shape, and why:

  OutpostProfile   one *watch profile* = one product being listened for. Owns
                   its brief, topics, subreddits, feed and blocklist. The
                   colleague's app hardcoded a single product (Agent A) with a
                   15-line brief scraped once; making it a row means a second
                   product costs a form fill, not a fork.
  OutpostSubreddit a monitored subreddit within a profile, plus its audit
                   scores (topical fit + promo friendliness).
  OutpostPost      one scanned Reddit post. Scored, not necessarily replied to.
  OutpostDraft     a generated reply variant (helpful / soft / pitch).
  OutpostAction    audit trail: "I posted this", with the comment URL.
  OutpostBlocked   reddit_ids that must never be re-ingested (deleted/swept).
  OutpostRun       scan history, per profile.
  OutpostNotify    per-user notification prefs. Email address is NOT stored —
                   it comes from the authenticated Console session.

Everything except the profile itself is profile-scoped; posts and subreddits are
additionally unique per profile so two profiles can watch r/SEO independently.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Index,
                        Integer, String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from src.db_cross import CrossBase as OutpostBase


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OutpostProfile(OutpostBase):
    """A product/audience being listened for."""

    __tablename__ = "outpost_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), default="")
    product_name: Mapped[str] = mapped_column(String(200), default="")
    product_url: Mapped[str] = mapped_column(String(500), default="")

    # The LLM grounding. Stale briefs poison every downstream call, so the UI
    # surfaces `brief_updated_at` rather than letting it rot invisibly.
    brief: Mapped[str] = mapped_column(Text, default="")
    brief_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # [{id, label, emoji, pitch}] — topics double as scoring targets and as the
    # capability line the drafter pitches for that topic.
    topics: Mapped[list] = mapped_column(JSON, default=list)

    # Whole-word regex that forces relevance 1.0. Deliberately NOT the product
    # name by default: "Agent A" alone matches multi-agent architecture posts.
    brand_regex: Mapped[str] = mapped_column(String(400), default="")

    audience: Mapped[str] = mapped_column(Text, default="")
    relevance_floor: Mapped[float] = mapped_column(Float, default=0.5)
    max_posts_per_sub: Mapped[int] = mapped_column(Integer, default=30)
    lookback_hours: Mapped[int] = mapped_column(Integer, default=26)
    retention_days: Mapped[int] = mapped_column(Integer, default=14)
    fetch_engagement: Mapped[bool] = mapped_column(Boolean, default=False)

    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    setup_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    def to_dict(self, extra: dict | None = None) -> dict:
        d = {
            "id": self.id, "name": self.name, "product_name": self.product_name,
            "product_url": self.product_url, "brief": self.brief,
            "brief_updated_at": self.brief_updated_at.isoformat()
            if self.brief_updated_at else None,
            "topics": self.topics or [], "brand_regex": self.brand_regex,
            "audience": self.audience, "relevance_floor": self.relevance_floor,
            "max_posts_per_sub": self.max_posts_per_sub,
            "lookback_hours": self.lookback_hours,
            "retention_days": self.retention_days,
            "fetch_engagement": self.fetch_engagement,
            "is_default": self.is_default, "setup_complete": self.setup_complete,
        }
        d.update(extra or {})
        return d


class OutpostSubreddit(OutpostBase):
    """A monitored subreddit + its audit scores."""

    __tablename__ = "outpost_subreddits"
    __table_args__ = (
        UniqueConstraint("profile_id", "name", name="outpost_subs_profile_name_uniq"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("outpost_profiles.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    topical_fit: Mapped[float | None] = mapped_column(Float)
    promo_friendly: Mapped[float | None] = mapped_column(Float)
    # NULL, never 0 — RSS can't see subscriber counts and "0 subscribers" is a lie.
    subscribers: Mapped[int | None] = mapped_column(Integer)
    public_description: Mapped[str] = mapped_column(Text, default="")
    audit: Mapped[dict] = mapped_column(JSON, default=dict)
    audit_error: Mapped[str] = mapped_column(Text, default="")
    last_audited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    added_from: Mapped[str] = mapped_column(String(20), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    @property
    def combined(self) -> float | None:
        if self.topical_fit is None and self.promo_friendly is None:
            return None
        return round(0.7 * (self.topical_fit or 0.0)
                     + 0.3 * (self.promo_friendly or 0.0), 3)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "enabled": self.enabled,
            "topical_fit": self.topical_fit, "promo_friendly": self.promo_friendly,
            "combined": self.combined, "subscribers": self.subscribers,
            "public_description": self.public_description,
            "audit": self.audit or {}, "audit_error": self.audit_error,
            "last_audited_at": self.last_audited_at.isoformat()
            if self.last_audited_at else None,
            "added_from": self.added_from,
        }


class OutpostPost(OutpostBase):
    """One scanned Reddit post."""

    __tablename__ = "outpost_posts"
    __table_args__ = (
        UniqueConstraint("profile_id", "reddit_id", name="outpost_posts_profile_rid_uniq"),
        Index("outpost_posts_profile_fetched_idx", "profile_id", "fetched_at"),
        Index("outpost_posts_profile_matched_idx", "profile_id", "matched", "fetched_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("outpost_profiles.id", ondelete="CASCADE"), index=True)

    reddit_id: Mapped[str] = mapped_column(String(32))
    subreddit: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    selftext: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(200), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    permalink: Mapped[str] = mapped_column(Text, default="")

    # NULL = not fetched. Never 0-as-unknown: the source app displayed a hard 0
    # for every post because it ran RSS-only, which made the column meaningless.
    upvotes: Mapped[int | None] = mapped_column(Integer)
    num_comments: Mapped[int | None] = mapped_column(Integer)
    engagement_status: Mapped[str] = mapped_column(String(20), default="none")

    created_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    relevance: Mapped[float | None] = mapped_column(Float, index=True)
    topics: Mapped[list] = mapped_column(JSON, default=list)
    matched: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    brand_mention: Mapped[bool] = mapped_column(Boolean, default=False)
    reasoning: Mapped[str] = mapped_column(Text, default="")
    suggest_reply: Mapped[bool] = mapped_column(Boolean, default=False)

    # new | done (posted) | dismissed
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    drafts_status: Mapped[str] = mapped_column(String(20), default="none")
    drafts_error: Mapped[str] = mapped_column(Text, default="")
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def to_dict(self, drafts: list | None = None) -> dict:
        return {
            "id": self.id, "reddit_id": self.reddit_id, "subreddit": self.subreddit,
            "title": self.title, "selftext": self.selftext, "author": self.author,
            "url": self.url, "permalink": self.permalink,
            "upvotes": self.upvotes, "num_comments": self.num_comments,
            "engagement_status": self.engagement_status,
            "created_utc": self.created_utc.isoformat() if self.created_utc else None,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "relevance": self.relevance, "topics": self.topics or [],
            "matched": self.matched, "brand_mention": self.brand_mention,
            "reasoning": self.reasoning, "suggest_reply": self.suggest_reply,
            "status": self.status, "drafts_status": self.drafts_status,
            "drafts_error": self.drafts_error,
            "drafts": [d.to_dict() for d in (drafts or [])],
        }


class OutpostDraft(OutpostBase):
    """A generated reply variant. Never auto-posted — a human copies it."""

    __tablename__ = "outpost_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    post_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("outpost_posts.id", ondelete="CASCADE"), index=True)
    variant: Mapped[str] = mapped_column(String(20))
    body: Mapped[str] = mapped_column(Text, default="")
    features: Mapped[list] = mapped_column(JSON, default=list)
    refine_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    def to_dict(self) -> dict:
        return {"id": self.id, "variant": self.variant, "body": self.body,
                "features": self.features or [], "refine_note": self.refine_note,
                "created_at": self.created_at.isoformat() if self.created_at else None}


class OutpostAction(OutpostBase):
    """Audit trail — what a human actually did with a draft."""

    __tablename__ = "outpost_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(String(36), index=True)
    post_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("outpost_posts.id", ondelete="CASCADE"), index=True)
    draft_id: Mapped[str | None] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(30))
    comment_url: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    def to_dict(self) -> dict:
        return {"id": self.id, "post_id": self.post_id, "draft_id": self.draft_id,
                "action": self.action, "comment_url": self.comment_url,
                "body": self.body, "actor": self.actor,
                "created_at": self.created_at.isoformat() if self.created_at else None}


class OutpostBlocked(OutpostBase):
    """reddit_ids that must never come back. Deleting a post blocklists it."""

    __tablename__ = "outpost_blocked"
    __table_args__ = (
        UniqueConstraint("profile_id", "reddit_id", name="outpost_blocked_profile_rid_uniq"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("outpost_profiles.id", ondelete="CASCADE"), index=True)
    reddit_id: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(Text, default="")
    permalink: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class OutpostRun(OutpostBase):
    """Scan history. A zero-post scan is a FAILURE, not an empty day."""

    __tablename__ = "outpost_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("outpost_profiles.id", ondelete="CASCADE"), index=True)
    trigger: Mapped[str] = mapped_column(String(20), default="manual")
    status: Mapped[str] = mapped_column(String(20), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    subs_scanned: Mapped[int] = mapped_column(Integer, default=0)
    posts_seen: Mapped[int] = mapped_column(Integer, default=0)
    posts_new: Mapped[int] = mapped_column(Integer, default=0)
    posts_matched: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    log: Mapped[list] = mapped_column(JSON, default=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "trigger": self.trigger, "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "subs_scanned": self.subs_scanned, "posts_seen": self.posts_seen,
            "posts_new": self.posts_new, "posts_matched": self.posts_matched,
            "error": self.error, "log": self.log or [],
        }


class OutpostNotify(OutpostBase):
    """Per-user notification prefs.

    The user never types an address: `session_email` is captured from the
    authenticated Console session (`X-Auth-User-Email`) when they opt in, and
    refreshed on every visit. The scheduled digest runs with NO request context,
    so it cannot read that header itself — hence the column. `email_override`
    exists only for the rare "send it somewhere else" case.

    `enabled` controls NOTIFICATIONS ONLY — it never disables scanning. The
    source app coupled the two, which silently emptied people's feeds.
    """

    __tablename__ = "outpost_notify"

    user_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Captured from the session header, never typed by the user. The cron job
    # has no request context, so without this it has no address to send to.
    session_email: Mapped[str] = mapped_column(String(320), default="")
    email_override: Mapped[str] = mapped_column(String(320), default="")
    matched_only: Mapped[bool] = mapped_column(Boolean, default=True)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notify_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    def to_dict(self) -> dict:
        return {"user_id": self.user_id, "enabled": self.enabled,
                "session_email": self.session_email,
                "email_override": self.email_override,
                "matched_only": self.matched_only,
                "last_notified_at": self.last_notified_at.isoformat()
                if self.last_notified_at else None,
                "notify_count": self.notify_count, "last_error": self.last_error}
