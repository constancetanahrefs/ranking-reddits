"""Ranking Reddits — the Reddit threads that rank for you, as a readable card wall.

Two sources, both from Ahrefs (no Reddit API keys):
  * Brand Radar SERP visibility for your brand/domain
  * Reddit pages in the top-10 for the keywords tracked in your Rank Tracker project

FIRST RUN: open Settings and fill in target domain, brand keywords and the Rank
Tracker project. Nothing is pre-configured; the scan refuses to run until it is.

Opening a card marks it read, renders the thread (title, upvotes, comments, body)
and drafts AI reading notes. Cards can be pushed into the Scrapbook.
"""

from __future__ import annotations

NAME = "Ranking Reddits"
OWNER = ""   # set to your workspace handle if you use OWNER badges

from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from flask import Blueprint, jsonify, render_template, request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from src.db_cross import cross_engine, cross_session_scope
from src.schemas import validate_json, validate_query

from applications import _ranking_reddits_engine as E
from applications._ranking_reddits_models import RRBase, RRHit, RRScan, RRSetting, RRThread

# console_site_db has its own engine — Console's init_db_app only covers console_db
RRBase.metadata.create_all(cross_engine)

# ── Migration: single-scope -> multi-workspace ────────────────────────────
# The app originally tracked one hardcoded scope. Existing rows have no
# workspace_id, so adopt them into a default workspace built from the old
# rr_settings values. Idempotent; safe on a fresh install (does nothing).
with cross_engine.begin() as _c:
    from sqlalchemy import text as _sql
    for _t in ("rr_threads", "rr_scans"):
        _c.execute(_sql(f"ALTER TABLE {_t} ADD COLUMN IF NOT EXISTS "
                        f"workspace_id varchar(36) DEFAULT ''"))
    _c.execute(_sql("CREATE INDEX IF NOT EXISTS rr_threads_ws_idx "
                    "ON rr_threads(workspace_id)"))
    # url_key was globally unique; it must now be unique PER WORKSPACE. SQLAlchemy
    # created it as a unique *index* (ix_rr_threads_url_key), not a table constraint,
    # so dropping the constraint alone leaves the old index enforcing global
    # uniqueness — and the second project's first shared thread then explodes.
    _c.execute(_sql("ALTER TABLE rr_threads DROP CONSTRAINT IF EXISTS rr_threads_url_key_key"))
    _c.execute(_sql("DROP INDEX IF EXISTS ix_rr_threads_url_key"))
    _c.execute(_sql("CREATE INDEX IF NOT EXISTS ix_rr_threads_url_key "
                    "ON rr_threads(url_key)"))
    _c.execute(_sql("CREATE UNIQUE INDEX IF NOT EXISTS rr_threads_ws_key_uniq "
                    "ON rr_threads(workspace_id, url_key)"))
    _orphans = _c.execute(_sql(
        "SELECT count(*) FROM rr_threads WHERE workspace_id IS NULL OR workspace_id = ''"
    )).scalar() or 0
    if _orphans:
        _old = {r[0]: (r[1] or {}).get("v")
                for r in _c.execute(_sql("SELECT key, value FROM rr_settings")).all()}
        _wsid = str(__import__("uuid").uuid4())
        _c.execute(_sql(
            "INSERT INTO rr_workspaces (id, name, target_domain, brand_keywords, "
            "rt_project_id, rt_project_name, rt_tags, ahrefs_secret, sources, countries, "
            "max_serp_position, brand_limit, manual_keywords, is_default, setup_complete, "
            "created_at) VALUES (:id, :name, :dom, :bk, :pid, :pname, :tags, :sec, :src, "
            ":cty, :pos, :bl, :mk, true, true, now())"), {
                "id": _wsid,
                "name": _old.get("rt_project_name") or _old.get("target_domain") or "Default",
                "dom": _old.get("target_domain") or "",
                "bk": _old.get("brand_keywords") or [],
                "pid": _old.get("rt_project_id") or "",
                "pname": _old.get("rt_project_name") or "",
                "tags": _old.get("rt_tags") or [],
                "sec": _old.get("ahrefs_secret") or "ahrefs_oauth",
                "src": _old.get("sources") or ["brand", "keywords"],
                "cty": _old.get("countries") or [],
                "pos": int(_old.get("max_serp_position") or 10),
                "bl": int(_old.get("brand_limit") or 400),
                "mk": [],
            })
        for _t in ("rr_threads", "rr_scans"):
            _c.execute(_sql(f"UPDATE {_t} SET workspace_id = :w "
                            f"WHERE workspace_id IS NULL OR workspace_id = ''"), {"w": _wsid})
        print(f"[ranking_reddits] adopted {_orphans} existing threads into workspace {_wsid}")

# Restart recovery: the app restarts on every file edit, which kills the daemon
# threads running fetches and scans. Anything left mid-flight is reset so it can
# be retried instead of being stuck forever.
with cross_engine.begin() as _c:
    from sqlalchemy import text as _sql
    _c.execute(_sql("UPDATE rr_threads SET fetch_status='pending' WHERE fetch_status='running'"))
    _c.execute(_sql("UPDATE rr_threads SET notes_status='pending' WHERE notes_status='running'"))
    _c.execute(_sql("UPDATE rr_scans SET status='failed', "
                    "error='interrupted by an app restart — re-run the scan' "
                    "WHERE status='running'"))

blueprint = Blueprint("ranking_reddits", __name__,
                      template_folder="../templates/ranking_reddits")


def _now():
    return datetime.now(timezone.utc)


def _user() -> str:
    return (request.headers.get("X-Auth-User-Email")
            or request.headers.get("X-Auth-User-Id") or "")


def _can_write() -> bool:
    return (request.headers.get("X-Auth-User-Permission") or "editor").lower() != "viewer"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class CardQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ws: str = Field(default="", max_length=36)
    source: str = Field(default="all", pattern=r"^(all|brand|keywords)$")
    subreddit: str = Field(default="", max_length=120)
    state: str = Field(default="all", pattern=r"^(all|new|unread|read|saved)$")
    sort: str = Field(default="newest",
                      pattern=r"^(newest|found|position|upvotes|volume|citations)$")
    q: str = Field(default="", max_length=200)
    limit: int = Field(default=200, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class OpenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thread_id: str = Field(min_length=1, max_length=36)
    fetch: bool = True


class ThreadIdIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thread_id: str = Field(min_length=1, max_length=36)


class NotesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thread_id: str = Field(min_length=1, max_length=36)
    user_notes: str = Field(default="", max_length=20000)


class MarkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thread_id: Optional[str] = Field(default=None, max_length=36)
    all_cards: bool = False
    is_read: Optional[bool] = None
    clear_new: bool = False


class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_domain: Optional[str] = Field(default=None, max_length=253)
    brand_keywords: Optional[list[str]] = None
    rt_project_id: Optional[str] = Field(default=None, max_length=32)
    rt_project_name: Optional[str] = Field(default=None, max_length=200)
    rt_tags: Optional[list[str]] = None
    ahrefs_secret: Optional[str] = Field(default=None, max_length=100)
    max_serp_position: Optional[int] = Field(default=None, ge=1, le=100)
    countries: Optional[list[str]] = None
    sources: Optional[list[str]] = None
    brand_limit: Optional[int] = Field(default=None, ge=10, le=1000)
    apify_secret: Optional[str] = Field(default=None, max_length=100)
    apify_actor: Optional[str] = Field(default=None, max_length=200)
    auto_fetch_on_open: Optional[bool] = None
    auto_notes_on_open: Optional[bool] = None

    @field_validator("countries")
    @classmethod
    def _cc(cls, v):
        if v is None:
            return v
        return [c.strip().lower() for c in v if c and len(c.strip()) == 2]

    @field_validator("sources")
    @classmethod
    def _src(cls, v):
        if v is None:
            return v
        out = [s for s in v if s in ("brand", "keywords")]
        if not out:
            raise ValueError("pick at least one source")
        return out


class EnrichIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=10, ge=1, le=25)
    notes: bool = False


class ScanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ws: str = Field(default="", max_length=36)
    trigger: str = Field(default="manual", pattern=r"^(manual|monthly)$")


class EnrichIn2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ws: str = Field(default="", max_length=36)
    limit: int = Field(default=10, ge=1, le=25)
    notes: bool = False


class WorkspaceIn(BaseModel):
    """Create / update a monitored project. Every field optional on update."""
    model_config = ConfigDict(extra="forbid")
    id: Optional[str] = Field(default=None, max_length=36)
    name: Optional[str] = Field(default=None, max_length=200)
    target_domain: Optional[str] = Field(default=None, max_length=253)
    brand_keywords: Optional[list[str]] = None
    rt_project_id: Optional[str] = Field(default=None, max_length=32)
    rt_project_name: Optional[str] = Field(default=None, max_length=200)
    rt_tags: Optional[list[str]] = None
    ahrefs_secret: Optional[str] = Field(default=None, max_length=100)
    sources: Optional[list[str]] = None
    countries: Optional[list[str]] = None
    max_serp_position: Optional[int] = Field(default=None, ge=1, le=100)
    brand_limit: Optional[int] = Field(default=None, ge=10, le=1000)
    manual_keywords: Optional[list[str]] = None
    setup_complete: Optional[bool] = None

    @field_validator("countries")
    @classmethod
    def _cc2(cls, v):
        return None if v is None else [c.strip().lower() for c in v
                                       if c and len(c.strip()) == 2]

    @field_validator("sources")
    @classmethod
    def _src2(cls, v):
        if v is None:
            return v
        out = [x for x in v if x in ("brand", "keywords")]
        if not out:
            raise ValueError("pick at least one source")
        return out


class SuggestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(min_length=1, max_length=253)
    mode: str = Field(default="subdomains",
                      pattern=r"^(exact|prefix|domain|subdomains)$")
    secret: str = Field(default="ahrefs_oauth", max_length=100)
    limit: int = Field(default=60, ge=1, le=200)
    min_volume: int = Field(default=50, ge=0)


class AddKeywordsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rt_project_id: str = Field(min_length=1, max_length=32)
    keywords: list[str] = Field(min_length=1, max_length=500)
    secret: str = Field(default="ahrefs_oauth", max_length=100)
    country: str = Field(default="us", pattern=r"^[a-z]{2}$")
    language: str = Field(default="en", max_length=8)
    tags: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@blueprint.route("/")
def index():
    ws_id = request.args.get("ws") or None
    st = E.stats(ws_id)
    ws = st.get("workspace")
    cards = E.list_cards(ws_id=ws["id"], limit=60)["cards"] if ws else []
    return render_template("ranking_reddits/index.html",
                           app_settings=E.app_settings(),
                           stats=st, workspace=ws,
                           workspaces=st.get("workspaces") or [],
                           initial=cards)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@blueprint.route("/api/cards")
def api_cards():
    q = validate_query(CardQuery)
    ws = E.get_workspace(q.ws or None)
    if not ws:
        return jsonify({"cards": []})
    return jsonify(E.list_cards(ws_id=ws["id"], source=q.source, subreddit=q.subreddit,
                                state=q.state, sort=q.sort, q=q.q,
                                limit=q.limit, offset=q.offset))


@blueprint.route("/api/stats")
def api_stats():
    return jsonify(E.stats(request.args.get("ws") or None))


@blueprint.route("/api/card/<thread_id>")
def api_card(thread_id: str):
    with cross_session_scope() as s:
        row = s.get(RRThread, thread_id)
        if not row:
            return jsonify({"error": "not_found"}), 404
        hits = [{"keyword": h.keyword, "country": h.country, "source": h.source,
                 "position": h.serp_position, "volume": h.search_volume}
                for h in s.execute(select(RRHit).where(RRHit.thread_id == thread_id)).scalars().all()]
        hits.sort(key=lambda d: (d["position"] or 99, -(d["volume"] or 0)))
        return jsonify(row.to_dict(hits))


@blueprint.route("/api/open", methods=["POST"])
def api_open():
    """Opening a card: marks it read + clears NEW, and kicks off fetch + notes."""
    body = validate_json(OpenIn)
    st = E.app_settings()   # auto-fetch / auto-notes are app-wide, not per project
    with cross_session_scope() as s:
        row = s.get(RRThread, body.thread_id)
        if not row:
            return jsonify({"error": "not_found"}), 404
        if _can_write():
            if not row.is_read:
                row.is_read = True
                row.read_at = _now()
                row.read_by = _user()
            row.is_new = False
        needs_fetch = row.fetch_status in ("pending", "failed")
    job_id = None
    if body.fetch and needs_fetch and st.get("auto_fetch_on_open") and _can_write():
        job_id = E.job_run("fetch", E.fetch_thread, body.thread_id, True)
    return jsonify({"ok": True, "job_id": job_id})


@blueprint.route("/api/fetch", methods=["POST"])
def api_fetch():
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    body = validate_json(ThreadIdIn)
    return jsonify({"job_id": E.job_run("fetch", E.fetch_thread, body.thread_id, True)})


@blueprint.route("/api/enrich", methods=["POST"])
def api_enrich():
    """Fill in details for the next N un-fetched cards, without opening each one."""
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    body = validate_json(EnrichIn2)
    ws = E.get_workspace(body.ws or None)
    if not ws:
        return jsonify({"error": "no_project"}), 400
    return jsonify({"job_id": E.job_run("enrich", E.enrich_batch, ws["id"],
                                        body.limit, body.notes)})


@blueprint.route("/api/notes/draft", methods=["POST"])
def api_notes_draft():
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    body = validate_json(ThreadIdIn)
    return jsonify({"job_id": E.job_run("notes", E.draft_notes_job, body.thread_id)})


@blueprint.route("/api/notes/save", methods=["POST"])
def api_notes_save():
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    body = validate_json(NotesIn)
    with cross_session_scope() as s:
        row = s.get(RRThread, body.thread_id)
        if not row:
            return jsonify({"error": "not_found"}), 404
        row.user_notes = body.user_notes
    return jsonify({"ok": True})


@blueprint.route("/api/mark", methods=["POST"])
def api_mark():
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    body = validate_json(MarkIn)
    with cross_session_scope() as s:
        if body.all_cards:
            rows = s.execute(select(RRThread)).scalars().all()
        elif body.thread_id:
            r = s.get(RRThread, body.thread_id)
            rows = [r] if r else []
        else:
            rows = []
        for row in rows:
            if body.is_read is not None:
                row.is_read = body.is_read
                row.read_at = _now() if body.is_read else None
                row.read_by = _user() if body.is_read else ""
            if body.clear_new:
                row.is_new = False
        n = len(rows)
    return jsonify({"ok": True, "updated": n})


@blueprint.route("/api/scrapbook", methods=["POST"])
def api_scrapbook():
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    body = validate_json(ThreadIdIn)
    try:
        item_id = E.push_to_scrapbook(body.thread_id, saved_by=_user())
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)[:300]}), 400
    return jsonify({"ok": True, "item_id": item_id,
                    "url": "/applications/scrapbook/"})


@blueprint.route("/api/scan", methods=["POST"])
def api_scan():
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    body = validate_json(ScanIn)
    ws = E.get_workspace(body.ws or None)
    if not ws:
        return jsonify({"error": "no_project",
                        "missing": ["No project yet — add one with the setup wizard."]}), 400
    gaps = E.missing_required(ws)
    if gaps:
        return jsonify({"error": "config_incomplete", "missing": gaps}), 400
    return jsonify({"job_id": E.job_run("scan", E.run_scan, ws["id"], body.trigger)})


@blueprint.route("/api/scans")
def api_scans():
    ws = E.get_workspace(request.args.get("ws") or None)
    return jsonify({"scans": E.scan_history(ws["id"] if ws else None)})


@blueprint.route("/api/job/<job_id>")
def api_job(job_id: str):
    j = E.job_get(job_id)
    if not j:
        return jsonify({"error": "not_found"}), 404
    return jsonify(j)


@blueprint.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    """App-level settings only (enrichment + auto behaviours). Scope lives per project."""
    if request.method == "GET":
        return jsonify(E.app_settings())
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    body = validate_json(SettingsIn)
    patch = {k: v for k, v in body.model_dump().items()
             if v is not None and k in E.APP_DEFAULTS}
    return jsonify(E.save_app_settings(patch))


# ── Projects (workspaces) ────────────────────────────────────────────────

@blueprint.route("/api/workspaces")
def api_workspaces():
    return jsonify({"workspaces": E.list_workspaces()})


@blueprint.route("/api/workspaces", methods=["POST"])
def api_workspace_create():
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    body = validate_json(WorkspaceIn)
    data = {k: v for k, v in body.model_dump().items() if v is not None and k != "id"}
    try:
        return jsonify(E.create_workspace(data))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)[:300]}), 400


@blueprint.route("/api/workspaces/<ws_id>", methods=["POST"])
def api_workspace_update(ws_id: str):
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    body = validate_json(WorkspaceIn)
    patch = {k: v for k, v in body.model_dump().items() if v is not None and k != "id"}
    try:
        return jsonify(E.update_workspace(ws_id, patch))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)[:300]}), 400


@blueprint.route("/api/workspaces/<ws_id>/delete", methods=["POST"])
def api_workspace_delete(ws_id: str):
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    try:
        return jsonify({"ok": True, "removed_threads": E.delete_workspace(ws_id)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)[:300]}), 400


# ── Setup wizard ─────────────────────────────────────────────────────────

@blueprint.route("/api/wizard/projects")
def api_wizard_projects():
    """Every Rank Tracker project both Ahrefs secrets can see, with keyword counts."""
    try:
        return jsonify({"projects": E.wizard_projects()})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)[:300]}), 400


@blueprint.route("/api/wizard/suggest", methods=["POST"])
def api_wizard_suggest():
    body = validate_json(SuggestIn)
    try:
        return jsonify({"suggestions": E.suggest_keywords(
            body.target, body.mode, body.secret, body.limit, body.min_volume)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)[:300]}), 400


@blueprint.route("/api/wizard/add-keywords", methods=["POST"])
def api_wizard_add_keywords():
    """WRITE against the user's Ahrefs account — adds tracked keywords."""
    if not _can_write():
        return jsonify({"error": "read_only"}), 403
    body = validate_json(AddKeywordsIn)
    try:
        return jsonify(E.add_keywords_to_project(
            body.rt_project_id, body.keywords, body.secret,
            body.country, body.language, body.tags))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)[:300]}), 400


@blueprint.route("/api/rt/projects")
def api_rt_projects():
    """Legacy single-secret project list. The wizard uses /api/wizard/projects,
    which spans every connected Ahrefs secret."""
    ws = E.get_workspace(request.args.get("ws") or None)
    try:
        return jsonify({"projects": E.rt_projects(ws or {})})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)[:300]}), 400


@blueprint.route("/api/rt/tags")
def api_rt_tags():
    """Tag options for the given project's Rank Tracker project."""
    ws = E.get_workspace(request.args.get("ws") or None)
    if not ws:
        return jsonify({"tags": []})
    try:
        return jsonify(E.rt_tag_options(ws))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)[:300]}), 400
