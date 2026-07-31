"""Reddit Outpost — a listening post for fresh Reddit threads worth replying to.

Companion to Ranking Reddits. Different axis, deliberately kept apart:

  Ranking Reddits  threads that RANK in SERPs (Ahrefs) — evergreen, monthly,
                   permanent library, read-and-mine.
  Reddit Outpost   posts published in the last day (Reddit RSS) — daily,
                   14-day retention, reply-now.

Adapted from a colleague's "Agent A Reddit Outpost". Kept verbatim: the
never-post rule, promo-friendliness heuristics, the three reply variants, the
brand floor and its deliberate exclusion of solo "Agent A". Changed on purpose:
watch profiles instead of one hardcoded product, real schema instead of
hand-made tables, email instead of Slack, serial fetching because this egress
IP is rate-limited far harder than theirs, and notifications decoupled from
scanning.
"""
from __future__ import annotations

import os
from typing import Optional
from urllib.parse import unquote

from flask import Blueprint, Flask, jsonify, render_template, request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app import engine as E
from app.db import engine as db_engine, init_db

blueprint = Blueprint("reddit_outpost", __name__)


# ---------------------------------------------------------------------------
# Request validation — the standalone stand-in for Letaido's `src.schemas`.
# Same contract: parse-or-422, never trust a visitor's JSON.
# ---------------------------------------------------------------------------

def _abort_422(exc: ValidationError):
    from flask import abort, make_response
    abort(make_response(jsonify({"details": exc.errors()}), 422))


def validate_json(model_cls):
    try:
        return model_cls.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        _abort_422(exc)


def validate_query(model_cls):
    try:
        return model_cls.model_validate(request.args.to_dict())
    except ValidationError as exc:
        _abort_422(exc)


init_db()

# ---------------------------------------------------------------------------
# Migrations. create_all() only adds TABLES, never columns, and these tables are
# owned by `console` (this process) — so late-added columns must be ALTERed here
# rather than from an agent-side psql, which lacks ownership.
# ---------------------------------------------------------------------------
def _migrate() -> None:
    """No-op on a fresh standalone install — create_all() already made the
    current schema. Kept so upgrading an OLD install still picks up new columns."""
    from sqlalchemy import text as _sql
    stmts = [
        # session_email: the scheduled digest has no request context, so it can't
        # read X-Auth-User-Email itself. Captured at opt-in time instead.
        "ALTER TABLE outpost_notify ADD COLUMN IF NOT EXISTS "
        "session_email varchar(320) NOT NULL DEFAULT ''",
    ]
    try:
        with db_engine.begin() as c:
            for stmt in stmts:
                c.execute(_sql(stmt))
    except Exception as exc:  # noqa: BLE001 — never take the app down over this
        print(f"[reddit_outpost] migration skipped: {exc}")


_migrate()

# A restart kills in-flight scan threads; leave no run stuck at "running".
E.recover_stale_runs()
E.seed_default_profile()

# ---------------------------------------------------------------------------
# Identity.
#
# In Letaido, nginx authenticates every request and injects X-Auth-User-*, so the
# app knows who you are for free. Standing alone there is NO authentication: this
# is a single-user tool meant to run on localhost or behind your own auth proxy.
#
# If you put it on the internet, put a reverse proxy with auth in front and have
# it set these same headers — the code below picks them up automatically.
# ---------------------------------------------------------------------------

SINGLE_USER = os.environ.get("OUTPOST_USER", "me")
SINGLE_USER_EMAIL = os.environ.get("OUTPOST_EMAIL", "")


def _can_write() -> bool:
    """No permission tiers standing alone. Set OUTPOST_READ_ONLY=true to lock."""
    return os.environ.get("OUTPOST_READ_ONLY", "false").lower() != "true"


def _user_id() -> str:
    return (request.headers.get("X-Auth-User-Id")
            or request.headers.get("X-Auth-User-Email") or SINGLE_USER)


def _user_email() -> str:
    """Where digests go. From a proxy header if present, else OUTPOST_EMAIL."""
    return ((request.headers.get("X-Auth-User-Email") or "").strip()
            or SINGLE_USER_EMAIL)


def _user_name() -> str:
    return (unquote(request.headers.get("X-Auth-User-Name") or "").strip()
            or SINGLE_USER)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class FeedQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    p: str = Field(default="", max_length=36)
    status: str = Field(default="new", pattern=r"^(new|done|dismissed|any)$")
    window_days: int = Field(default=14, ge=0, le=365)
    subreddit: str = Field(default="", max_length=120)
    topic: str = Field(default="", max_length=60)
    matched_only: bool = True
    q: str = Field(default="", max_length=200)
    sort: str = Field(default="relevance", pattern=r"^(relevance|newest|oldest)$")
    limit: int = Field(default=60, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class ProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(default=None, max_length=200)
    product_name: Optional[str] = Field(default=None, max_length=200)
    product_url: Optional[str] = Field(default=None, max_length=500)
    brief: Optional[str] = Field(default=None, max_length=8000)
    topics: Optional[list[dict]] = None
    brand_regex: Optional[str] = Field(default=None, max_length=400)
    audience: Optional[str] = Field(default=None, max_length=4000)
    relevance_floor: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    max_posts_per_sub: Optional[int] = Field(default=None, ge=5, le=100)
    lookback_hours: Optional[int] = Field(default=None, ge=1, le=336)
    retention_days: Optional[int] = Field(default=None, ge=1, le=3650)
    subreddits: Optional[list[str]] = None

    @field_validator("brand_regex")
    @classmethod
    def _valid_regex(cls, v):
        if v:
            import re as _re
            try:
                _re.compile(v)
            except _re.error as exc:
                raise ValueError(f"not a valid regular expression: {exc}") from exc
        return v


class ScanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    p: str = Field(default="", max_length=36)
    trigger: str = Field(default="manual", pattern=r"^(manual|daily)$")


class SubIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    p: str = Field(default="", max_length=36)
    name: str = Field(min_length=1, max_length=200)


class BulkSubIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    p: str = Field(default="", max_length=36)
    names: list[str] = Field(min_length=1, max_length=60)


class SubPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class DiscoverIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    p: str = Field(default="", max_length=36)
    n: int = Field(default=30, ge=5, le=60)


class StatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(pattern=r"^(new|done|dismissed)$")


class BlockIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    p: str = Field(default="", max_length=36)
    post_ids: list[str] = Field(min_length=1, max_length=500)
    reason: str = Field(default="removed by hand", max_length=400)


class SweepQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    p: str = Field(default="", max_length=36)
    max_relevance: float = Field(default=0.4, ge=0.0, le=1.0)
    older_than_days: int = Field(default=3, ge=0, le=365)


class SweepIn(SweepQuery):
    pass


class RefineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(min_length=1, max_length=500)


class LogActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    p: str = Field(default="", max_length=36)
    draft_id: str = Field(default="", max_length=36)
    comment_url: str = Field(default="", max_length=2000)
    body: str = Field(default="", max_length=8000)


class NotifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: Optional[bool] = None
    email_override: Optional[str] = Field(default=None, max_length=320)
    matched_only: Optional[bool] = None


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

@blueprint.route("/")
def index():
    pid = request.args.get("p") or None
    st = E.stats(pid)
    prof = st.get("profile")
    return render_template(
        "index.html",
        stats=st, profile=prof, profiles=st.get("profiles") or [],
        subreddits=E.list_subreddits(prof["id"]) if prof else [],
        notify=E.get_notify(_user_id(), _user_email()),
        session_email=_user_email(),
        focus=request.args.get("focus", ""),
        autogen=request.args.get("autogen", ""),
    )


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

@blueprint.route("/api/profiles")
def api_profiles():
    return jsonify({"profiles": E.list_profiles()})


@blueprint.route("/api/profiles", methods=["POST"])
def api_profile_create():
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    body = validate_json(ProfileIn)
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        return jsonify(E.create_profile(data))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)[:300]}), 400


@blueprint.route("/api/profiles/<pid>", methods=["POST"])
def api_profile_update(pid: str):
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    body = validate_json(ProfileIn)
    patch = {k: v for k, v in body.model_dump().items()
             if v is not None and k != "subreddits"}
    try:
        return jsonify(E.update_profile(pid, patch))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)[:300]}), 400


class RenameTextIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    old_name: str = Field(min_length=1, max_length=200)
    new_name: str = Field(min_length=1, max_length=200)


@blueprint.route("/api/profiles/<pid>/stale-name")
def api_stale_name(pid: str):
    """How many stored reasoning lines / drafts still name the old product."""
    return jsonify(E.count_stale_product_name(pid, request.args.get("old_name", "")))


@blueprint.route("/api/profiles/<pid>/rename-text", methods=["POST"])
def api_rename_text(pid: str):
    """Substitute the old product name for the new one in stored text.

    Scores are NOT recomputed — the user asked to fix a name, not to re-judge
    their queue.
    """
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    body = validate_json(RenameTextIn)
    try:
        return jsonify(E.rename_product_in_text(pid, body.old_name, body.new_name))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)[:300]}), 400


@blueprint.route("/api/profiles/<pid>/delete", methods=["POST"])
def api_profile_delete(pid: str):
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    try:
        return jsonify({"ok": True, "removed_posts": E.delete_profile(pid)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)[:300]}), 400


class SuggestTopicsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_name: str = Field(min_length=1, max_length=200)
    brief: str = Field(min_length=20, max_length=8000)
    audience: str = Field(default="", max_length=4000)
    n: int = Field(default=8, ge=3, le=12)


@blueprint.route("/api/suggest/topics", methods=["POST"])
def api_suggest_topics():
    """Topic filters + pitch lines for THIS product, not inherited from another."""
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    body = validate_json(SuggestTopicsIn)
    try:
        return jsonify({"topics": E.suggest_topics(body.product_name, body.brief,
                                                   body.audience, body.n)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)[:300]}), 400


# ---------------------------------------------------------------------------
# Feed
# ---------------------------------------------------------------------------

@blueprint.route("/api/posts")
def api_posts():
    q = validate_query(FeedQuery)
    prof = E.get_profile(q.p or None)
    if not prof:
        return jsonify({"posts": [], "total": 0})
    return jsonify(E.list_posts(
        pid=prof["id"], status=q.status, window_days=q.window_days,
        subreddit=q.subreddit, topic=q.topic, matched_only=q.matched_only,
        q=q.q, sort=q.sort, limit=q.limit, offset=q.offset))


@blueprint.route("/api/stats")
def api_stats():
    return jsonify(E.stats(request.args.get("p") or None))


@blueprint.route("/api/posts/<post_id>/status", methods=["POST"])
def api_post_status(post_id: str):
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    body = validate_json(StatusIn)
    try:
        return jsonify(E.set_post_status(post_id, body.status))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)[:300]}), 400


@blueprint.route("/api/posts/block", methods=["POST"])
def api_posts_block():
    """Delete posts and blocklist them so no later scan can bring them back."""
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    body = validate_json(BlockIn)
    prof = E.get_profile(body.p or None)
    if not prof:
        return jsonify({"error": "no_profile"}), 400
    return jsonify({"removed": E.block_posts(prof["id"], body.post_ids, body.reason)})


@blueprint.route("/api/sweep/preview")
def api_sweep_preview():
    q = validate_query(SweepQuery)
    prof = E.get_profile(q.p or None)
    if not prof:
        return jsonify({"count": 0})
    return jsonify({"count": E.sweep_preview(prof["id"], q.max_relevance,
                                             q.older_than_days)})


@blueprint.route("/api/sweep", methods=["POST"])
def api_sweep():
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    body = validate_json(SweepIn)
    prof = E.get_profile(body.p or None)
    if not prof:
        return jsonify({"error": "no_profile"}), 400
    ids = E.sweep_ids(prof["id"], body.max_relevance, body.older_than_days)
    if not ids:
        return jsonify({"removed": 0})
    reason = (f"swept: relevance <= {body.max_relevance} and older than "
              f"{body.older_than_days} days")
    return jsonify({"removed": E.block_posts(prof["id"], ids, reason)})


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------

@blueprint.route("/api/posts/<post_id>/drafts", methods=["POST"])
def api_generate_drafts(post_id: str):
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    return jsonify({"job_id": E.job_run("drafts", E.generate_drafts, post_id)})


@blueprint.route("/api/drafts/<draft_id>/refine", methods=["POST"])
def api_refine_draft(draft_id: str):
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    body = validate_json(RefineIn)
    return jsonify({"job_id": E.job_run("refine", E.refine_draft, draft_id, body.note)})


@blueprint.route("/api/posts/<post_id>/log-action", methods=["POST"])
def api_log_action(post_id: str):
    """Record a reply a human already posted by hand. The app never posts."""
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    body = validate_json(LogActionIn)
    prof = E.get_profile(body.p or None)
    if not prof:
        return jsonify({"error": "no_profile"}), 400
    try:
        return jsonify(E.log_action(prof["id"], post_id, draft_id=body.draft_id,
                                    comment_url=body.comment_url, body=body.body,
                                    actor=_user_name() or _user_id()))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)[:300]}), 400


@blueprint.route("/api/actions")
def api_actions():
    prof = E.get_profile(request.args.get("p") or None)
    return jsonify({"actions": E.list_actions(prof["id"]) if prof else []})


# ---------------------------------------------------------------------------
# Subreddits
# ---------------------------------------------------------------------------

@blueprint.route("/api/subreddits")
def api_subreddits():
    prof = E.get_profile(request.args.get("p") or None)
    return jsonify({"subreddits": E.list_subreddits(prof["id"]) if prof else []})


@blueprint.route("/api/subreddits", methods=["POST"])
def api_subreddit_add():
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    body = validate_json(SubIn)
    prof = E.get_profile(body.p or None)
    if not prof:
        return jsonify({"error": "no_profile"}), 400
    try:
        return jsonify(E.add_subreddit(prof["id"], body.name))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)[:300]}), 400


@blueprint.route("/api/subreddits/bulk", methods=["POST"])
def api_subreddits_bulk():
    """Add several at once (the Discover tab's bulk-add). Audits run in background."""
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    body = validate_json(BulkSubIn)
    prof = E.get_profile(body.p or None)
    if not prof:
        return jsonify({"error": "no_profile"}), 400
    added, failed = [], []
    for nm in body.names:
        try:
            added.append(E.add_subreddit(prof["id"], nm, added_from="discover",
                                         audit_now=False))
        except Exception as exc:  # noqa: BLE001
            failed.append({"name": nm, "error": str(exc)[:160]})
    return jsonify({"added": added, "failed": failed})


@blueprint.route("/api/subreddits/<sid>/audit", methods=["POST"])
def api_subreddit_audit(sid: str):
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    return jsonify({"job_id": E.job_run("audit", _audit_job, sid)})


def _audit_job(job_id: str, sid: str) -> None:
    E.job_progress(job_id, "Checking the subreddit…")
    E.job_done(job_id, E.audit_subreddit(sid), "Audited")


@blueprint.route("/api/subreddits/<sid>", methods=["POST"])
def api_subreddit_patch(sid: str):
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    body = validate_json(SubPatch)
    try:
        return jsonify(E.set_subreddit(sid, enabled=body.enabled))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)[:300]}), 400


@blueprint.route("/api/subreddits/<sid>/delete", methods=["POST"])
def api_subreddit_delete(sid: str):
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    E.delete_subreddit(sid)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Discover
# ---------------------------------------------------------------------------

@blueprint.route("/api/discover", methods=["POST"])
def api_discover():
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    body = validate_json(DiscoverIn)
    prof = E.get_profile(body.p or None)
    if not prof:
        return jsonify({"error": "no_profile"}), 400
    return jsonify({"job_id": E.job_run("discover", E.run_discover, prof["id"], body.n)})


# ---------------------------------------------------------------------------
# Scan + runs
# ---------------------------------------------------------------------------

@blueprint.route("/api/scan", methods=["POST"])
def api_scan():
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    body = validate_json(ScanIn)
    prof = E.get_profile(body.p or None)
    if not prof:
        return jsonify({"error": "no_profile",
                        "missing": ["No profile yet — add one first."]}), 400
    gaps = E.missing_required(prof)
    if gaps:
        return jsonify({"error": "config_incomplete", "missing": gaps}), 400
    if not E.list_subreddits(prof["id"], only_enabled=True):
        return jsonify({"error": "no_subreddits",
                        "missing": ["No enabled subreddits to read."]}), 400
    return jsonify({"job_id": E.job_run("scan", E.run_scan, prof["id"], body.trigger)})


@blueprint.route("/api/runs")
def api_runs():
    prof = E.get_profile(request.args.get("p") or None)
    return jsonify({"runs": E.scan_history(prof["id"]) if prof else []})


@blueprint.route("/api/job/<job_id>")
def api_job(job_id: str):
    j = E.job_get(job_id)
    if not j:
        return jsonify({"error": "not_found"}), 404
    return jsonify(j)


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

@blueprint.route("/api/notify")
def api_notify_get():
    cfg = E.get_notify(_user_id(), _user_email())
    cfg["session_email"] = _user_email() or cfg.get("session_email") or ""
    return jsonify(cfg)


@blueprint.route("/api/notify", methods=["POST"])
def api_notify_set():
    body = validate_json(NotifyIn)
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    # Remember the session address so the scheduled digest has somewhere to send.
    if _user_email():
        patch["session_email"] = _user_email()
    cfg = E.save_notify(_user_id(), patch)
    cfg["session_email"] = _user_email() or cfg.get("session_email") or ""
    return jsonify(cfg)


@blueprint.route("/api/notify/test", methods=["POST"])
def api_notify_test():
    """Send a real email to prove delivery before anyone relies on the digest."""
    cfg = E.get_notify(_user_id(), _user_email())
    to = ((cfg.get("email_override") or "").strip() or _user_email()
          or (cfg.get("session_email") or "").strip())
    if not to:
        return jsonify({"error": "No address on your Console session — sign in "
                                 "again, or set an override."}), 400
    try:
        from app import notify as letaido_email
        letaido_email.send_email(
            to=to, subject="Reddit Outpost — test email",
            body=("This is a test from Reddit Outpost.\n\nIf you're reading this, "
                  "daily digests will reach you at this address."))
        return jsonify({"ok": True, "to": to})
    except Exception as exc:  # noqa: BLE001
        # RecipientNotInOrg is a policy rejection, not a typo — say so plainly so
        # nobody retries the same address expecting a different answer.
        return jsonify({"error": f"{type(exc).__name__}: {str(exc)[:300]}"}), 400


# ---------------------------------------------------------------------------
# Standalone app factory
# ---------------------------------------------------------------------------

def create_app() -> Flask:
    app = Flask(__name__, template_folder="../templates",
                static_folder="../static")
    app.register_blueprint(blueprint)

    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True, "profiles": len(E.list_profiles())})

    return app


app = create_app()

if __name__ == "__main__":
    # Dev only. In production use gunicorn (see the Dockerfile) — the scan runs
    # in background threads, so keep it to ONE worker with several threads.
    app.run(host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8000")), threaded=True)
