"""Ranking Reddits — engine.

Two discovery sources, one card wall.

* ``brand``    — Brand Radar SERP visibility: Reddit threads ranking for keywords
  where the brand/domain is a tracked entity (`ahrefs_brand_radar.reddit_results`
  scoped with inline `brands` tracking groups).
* ``keywords`` — Reddit threads in the top-10 for the keywords tracked in the
  Ahrefs Rank Tracker project (same endpoint, scoped by an `is`-any text rule on
  the SERP query, batched because the keyword list is long).

Both return the same thread shape, so a thread found by both simply carries two
`sources` and a hit row per (keyword, country, source).

Enrichment (title / upvotes / comments / body + top comments) is NOT available
from a plain fetch — reddit.com answers 403 to every server-side UA we can send,
and the .rss endpoint rate-limits immediately. So enrichment goes through the
Apify `apify/website-content-crawler` actor with a residential proxy: it renders
the page and we read the embedded `shreddit-screenview-data` JSON for score /
comment count / upvote ratio and the actor's own markdown for the body.
Enrichment is per-card and lazy (fires when the card is opened) so a 200-card
scan costs nothing until someone actually reads a thread.
"""

from __future__ import annotations

import html as _html
import json
import os
import re
import threading
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import delete, func, select

from src.connectors import invoke as connector_invoke
from src.db_cross import cross_session_scope
from src.llm import console_openai_client

from applications._ranking_reddits_models import (
    RRHit, RRScan, RRSetting, RRThread, RRWorkspace,
)

APP_SLUG = "applications:ranking_reddits"
CHAT_MODEL = "anthropic/claude-sonnet-4.5"

_llm = console_openai_client(app_slug=APP_SLUG)


def _now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

# App-level settings (shared across every workspace) stay in rr_settings.
APP_DEFAULTS: dict[str, Any] = {
    "apify_secret": "apify_main",
    "apify_actor": "apify/website-content-crawler",
    "apify_thread_actor": "trudax/reddit-scraper-lite",
    "max_comments": 15,
    "auto_fetch_on_open": True,
    "auto_notes_on_open": True,
}

# Per-workspace scope lives on the RRWorkspace row.
WS_FIELDS = ("name", "target_domain", "brand_keywords", "rt_project_id",
             "rt_project_name", "rt_tags", "ahrefs_secret", "sources",
             "countries", "max_serp_position", "brand_limit", "manual_keywords")


def app_settings() -> dict:
    out = dict(APP_DEFAULTS)
    with cross_session_scope() as s:
        for row in s.execute(select(RRSetting)).scalars().all():
            if row.key in APP_DEFAULTS and (row.value or {}).get("v") is not None:
                out[row.key] = (row.value or {}).get("v")
    return out


def save_app_settings(patch: dict) -> dict:
    with cross_session_scope() as s:
        for k, v in patch.items():
            if k not in APP_DEFAULTS or v is None:
                continue
            row = s.get(RRSetting, k)
            if row:
                row.value = {"v": v}
            else:
                s.add(RRSetting(key=k, value={"v": v}))
    return app_settings()


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------

def list_workspaces() -> list[dict]:
    with cross_session_scope() as s:
        rows = s.execute(select(RRWorkspace)
                         .order_by(RRWorkspace.is_default.desc(),
                                   RRWorkspace.created_at.asc())).scalars().all()
        out = []
        for w in rows:
            d = w.to_dict()
            d["threads"] = s.execute(
                select(func.count(RRThread.id))
                .where(RRThread.workspace_id == w.id)).scalar() or 0
            d["new"] = s.execute(
                select(func.count(RRThread.id))
                .where(RRThread.workspace_id == w.id,
                       RRThread.is_new.is_(True))).scalar() or 0
            out.append(d)
        return out


def get_workspace(ws_id: str | None = None) -> Optional[dict]:
    """A specific workspace, or the default one when no id is given."""
    with cross_session_scope() as s:
        w = None
        if ws_id:
            w = s.get(RRWorkspace, ws_id)
        if w is None:
            w = s.execute(select(RRWorkspace)
                          .order_by(RRWorkspace.is_default.desc(),
                                    RRWorkspace.created_at.asc())
                          .limit(1)).scalars().first()
        return w.to_dict() if w else None


def create_workspace(data: dict) -> dict:
    with cross_session_scope() as s:
        first = (s.execute(select(func.count(RRWorkspace.id))).scalar() or 0) == 0
        w = RRWorkspace(
            name=(data.get("name") or data.get("rt_project_name")
                  or data.get("target_domain") or "New project")[:200],
            target_domain=(data.get("target_domain") or "")[:253],
            brand_keywords=data.get("brand_keywords") or [],
            rt_project_id=str(data.get("rt_project_id") or ""),
            rt_project_name=data.get("rt_project_name") or "",
            rt_tags=data.get("rt_tags") or [],
            ahrefs_secret=data.get("ahrefs_secret") or "ahrefs_oauth",
            sources=data.get("sources") or ["brand"],
            countries=data.get("countries") or [],
            max_serp_position=int(data.get("max_serp_position") or 10),
            brand_limit=int(data.get("brand_limit") or 400),
            manual_keywords=data.get("manual_keywords") or [],
            is_default=first,
            setup_complete=bool(data.get("setup_complete", True)))
        s.add(w)
        s.flush()
        return w.to_dict()


def update_workspace(ws_id: str, patch: dict) -> dict:
    with cross_session_scope() as s:
        w = s.get(RRWorkspace, ws_id)
        if not w:
            raise RuntimeError("Project not found.")
        for k, v in patch.items():
            if k in WS_FIELDS and v is not None:
                setattr(w, k, v)
        if patch.get("setup_complete") is not None:
            w.setup_complete = bool(patch["setup_complete"])
        s.flush()
        return w.to_dict()


def delete_workspace(ws_id: str) -> int:
    """Removes the scope and every card in it. Cards elsewhere are untouched."""
    with cross_session_scope() as s:
        w = s.get(RRWorkspace, ws_id)
        if not w:
            raise RuntimeError("Project not found.")
        n = s.execute(select(func.count(RRThread.id))
                      .where(RRThread.workspace_id == ws_id)).scalar() or 0
        s.execute(delete(RRHit).where(RRHit.thread_id.in_(
            select(RRThread.id).where(RRThread.workspace_id == ws_id))))
        s.execute(delete(RRThread).where(RRThread.workspace_id == ws_id))
        s.execute(delete(RRScan).where(RRScan.workspace_id == ws_id))
        was_default = w.is_default
        s.delete(w)
        s.flush()
        if was_default:
            nxt = s.execute(select(RRWorkspace)
                            .order_by(RRWorkspace.created_at.asc())
                            .limit(1)).scalars().first()
            if nxt:
                nxt.is_default = True
        return n


def missing_required(ws: dict | None) -> list[str]:
    """Which required values are still empty on this workspace, phrased for a human."""
    if not ws:
        return ["No project yet — run the setup wizard to add one."]
    srcs = ws.get("sources") or []
    out: list[str] = []
    if not ws.get("ahrefs_secret"):
        out.append("Ahrefs secret — the connector secret that owns the project")
    if "keywords" in srcs and not (ws.get("rt_project_id")
                                   or ws.get("manual_keywords")):
        out.append("Rank Tracker project or a manual keyword list")
    if "brand" in srcs and not (ws.get("brand_keywords") or []):
        out.append("Brand keywords — e.g. your brand name and obvious variants")
    if "brand" in srcs and not ws.get("target_domain"):
        out.append("Target domain — e.g. example.com")
    if not srcs:
        out.append("Sources — enable Brand Radar visibility and/or tracked keywords")
    return out


# ---------------------------------------------------------------------------
# Jobs — the 30s rule
# ---------------------------------------------------------------------------

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def job_new(kind: str) -> str:
    jid = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[jid] = {"status": "running", "kind": kind, "progress": "Starting…",
                      "result": None, "error": None}
        for old in list(_jobs)[:-60]:
            _jobs.pop(old, None)
    return jid


def job_progress(jid: str, msg: str) -> None:
    with _jobs_lock:
        if jid in _jobs:
            _jobs[jid]["progress"] = msg


def job_done(jid: str, result: Any = None, progress: str = "Done") -> None:
    with _jobs_lock:
        if jid in _jobs:
            _jobs[jid].update(status="completed", result=result, progress=progress)


def job_fail(jid: str, err: str) -> None:
    with _jobs_lock:
        if jid in _jobs:
            _jobs[jid].update(status="failed", error=err, progress=f"Failed: {err}")


def job_get(jid: str) -> Optional[dict]:
    with _jobs_lock:
        j = _jobs.get(jid)
        return dict(j) if j else None


def job_run(kind: str, fn: Callable[..., Any], *args, **kwargs) -> str:
    jid = job_new(kind)

    def _wrap():
        try:
            fn(jid, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            job_fail(jid, str(exc)[:400])
            print(f"[ranking_reddits:{kind}] {jid} failed:\n{traceback.format_exc()}")

    threading.Thread(target=_wrap, daemon=True).start()
    return jid


# ---------------------------------------------------------------------------
# URL normalisation — one card per thread
# ---------------------------------------------------------------------------

_COMMENTS_RE = re.compile(r"/comments/([a-z0-9]+)", re.I)
_SUB_RE = re.compile(r"reddit\.com/r/([A-Za-z0-9_]+)")


def url_key(url: str) -> Optional[str]:
    """`/r/<sub>/comments/<id>` → `<sub>/<id>`; a subreddit landing page → `r/<sub>`.

    Anything that isn't a reddit.com URL returns None and is skipped: the card
    wall is Reddit-only by definition.
    """
    if "reddit.com" not in (url or ""):
        return None
    sub = _SUB_RE.search(url)
    cid = _COMMENTS_RE.search(url)
    if cid:
        return f"{(sub.group(1) if sub else '').lower()}/{cid.group(1).lower()}"
    if sub:
        return f"r/{sub.group(1).lower()}"
    return None


def clean_title(t: str) -> str:
    t = (t or "").strip()
    # Ahrefs SERP titles carry a " : r/SEO - Reddit" / " - Reddit" tail
    t = re.sub(r"\s*[:\-|]\s*r/[A-Za-z0-9_]+\s*(-\s*Reddit)?\s*$", "", t)
    t = re.sub(r"\s*-\s*Reddit\s*$", "", t)
    return t.strip()


# ---------------------------------------------------------------------------
# Ahrefs access
# ---------------------------------------------------------------------------

def ahrefs(cap: str, args: dict, secret: str, timeout: int = 180) -> dict:
    return connector_invoke(cap, args, secret=secret, timeout=timeout)


def tracked_keywords(st: dict) -> list[str]:
    """Every keyword tracked in the Rank Tracker project, optionally tag-filtered.

    A zero-keyword result is a FAILURE, never an empty success — a silent
    transport change must not look like "nothing tracked".
    """
    manual = [k.strip() for k in (st.get("manual_keywords") or []) if k.strip()]
    pid = str(st.get("rt_project_id") or "").strip()
    if not pid:
        if manual:
            return sorted(set(manual))
        raise RuntimeError("No Rank Tracker project and no manual keyword list "
                           "configured for this project.")
    res = ahrefs("ahrefs_rank_tracker.overview_keywords_export",
                 {"project_id": pid, "limit": 10000}, st["ahrefs_secret"])
    recs = res.get("records") or []
    if not recs:
        if manual:
            return sorted(set(manual))
        raise RuntimeError(
            f"Rank Tracker project {pid} tracks 0 keywords. Either add keywords to it "
            f"(the setup wizard can), paste a manual list, or check that secret "
            f"'{st['ahrefs_secret']}' owns the project.")
    tags = [t.lower() for t in (st.get("rt_tags") or [])]
    kws: set[str] = set()
    for r in recs:
        kw = (r.get("keyword") or "").strip()
        if not kw:
            continue
        if tags and not any((t or "").lower() in tags for t in (r.get("tags") or [])):
            continue
        kws.add(kw)
    if tags and not kws:
        raise RuntimeError(f"No tracked keywords carry the tag(s) {st.get('rt_tags')}.")
    kws.update(manual)
    return sorted(kws)


def rt_tag_options(st: dict) -> dict:
    res = ahrefs("ahrefs_rank_tracker.overview_keywords_export",
                 {"project_id": str(st.get("rt_project_id") or ""), "limit": 10000},
                 st["ahrefs_secret"])
    recs = res.get("records") or []
    tags: dict[str, int] = {}
    for r in recs:
        for t in (r.get("tags") or []):
            tags[t] = tags.get(t, 0) + 1
    return {"keywords": len({r.get("keyword") for r in recs if r.get("keyword")}),
            "tags": sorted(({"tag": k, "count": v} for k, v in tags.items()),
                           key=lambda d: -d["count"])}


def rt_projects(st: dict) -> list[dict]:
    res = ahrefs("ahrefs_rank_tracker.list_projects", {"limit": 200}, st["ahrefs_secret"])
    return [{"id": p["id"], "name": p.get("name") or "",
             "target_url": p.get("target_url") or "",
             "keywords": p.get("number_of_keywords") or 0}
            for p in (res.get("records") or [])]


# ---------------------------------------------------------------------------
# Setup wizard — discover projects, suggest keywords, add them to Rank Tracker
# ---------------------------------------------------------------------------

# Every Ahrefs connector secret in this workspace that the wizard should search
# for Rank Tracker projects. A project is only visible to the token whose Ahrefs
# workspace OWNS it, so if you have projects spread across two Ahrefs accounts,
# add the second secret's name here — otherwise those projects appear to not
# exist. This is the single most common setup failure.
AHREFS_SECRETS = [
    s.strip() for s in os.environ.get(
        "RR_AHREFS_SECRETS", "ahrefs_oauth").split(",") if s.strip()
]


def wizard_projects(secret: str = "ahrefs_oauth") -> list[dict]:
    """Every Rank Tracker project the configured secrets can see, with counts.

    The wizard shows this as a picker so nobody has to know a project id.
    """
    seen: dict[str, dict] = {}
    errors: list[str] = []
    for sec in dict.fromkeys([secret, *AHREFS_SECRETS]):
        try:
            res = ahrefs("ahrefs_rank_tracker.list_projects", {"limit": 200}, sec)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{sec}: {str(exc)[:120]}")
            continue
        for p in (res.get("records") or []):
            pid = str(p.get("id"))
            if pid in seen:
                continue
            seen[pid] = {"id": pid, "name": p.get("name") or "",
                         "target_url": p.get("target_url") or "",
                         "target_mode": p.get("target_mode") or "subdomains",
                         "keywords": int(p.get("number_of_keywords") or 0),
                         "secret": sec}
    if not seen and errors:
        raise RuntimeError("Could not list Rank Tracker projects — " + " | ".join(errors))
    return sorted(seen.values(), key=lambda d: (-d["keywords"], d["name"].lower()))


def suggest_keywords(target: str, mode: str = "subdomains", secret: str = "ahrefs_oauth",
                     limit: int = 60, min_volume: int = 50) -> list[dict]:
    """Keyword suggestions from the target's existing organic footprint.

    Used by the wizard when a project tracks 0 keywords: the user picks from
    real ranking data instead of inventing a list.
    """
    res = ahrefs("ahrefs_rank_tracker.keywords_suggestions",
                 {"target": target, "mode": mode, "limit": min(int(limit), 200),
                  "min_volume": int(min_volume)}, secret)
    out = []
    for r in (res.get("records") or []):
        kw = (r.get("keyword") or "").strip()
        if kw:
            out.append({"keyword": kw, "volume": r.get("volume"),
                        "position": r.get("position"), "url": r.get("url") or ""})
    return out


def add_keywords_to_project(project_id: str, keywords: list[str], secret: str,
                            country: str = "us", language: str = "en",
                            tags: list[str] | None = None) -> dict:
    """Add keywords to a Rank Tracker project (a WRITE against the user's Ahrefs).

    Each keyword is tracked in every location passed, so keep it to one location
    unless the user explicitly wants more — 5 keywords x 3 locations = 15 tracked
    rows against their plan's quota.
    """
    kws = [k.strip() for k in keywords if k and k.strip()]
    if not kws:
        raise RuntimeError("No keywords given.")
    if not project_id:
        raise RuntimeError("No Rank Tracker project selected.")
    payload = {
        "project_id": str(project_id),
        "keywords": [{"keyword": k, "tags": list(tags or [])} for k in kws],
        "locations": [{"country": country, "language": language,
                       "location_id": 0, "location_name": "", "location_type": "Country"}],
        "import_history": False,
    }
    res = ahrefs("ahrefs_rank_tracker.add_keywords", payload, secret, timeout=180)
    return {"status": res.get("status"), "requested": res.get("keywords_requested"),
            "import_id": res.get("import_id"),
            "note": ("Ahrefs is collecting ranking data for these; positions appear "
                     "after the next crawl.")}


def _reddit_filters(st: dict, extra: dict | None = None) -> dict:
    f: dict[str, Any] = {"date_latest": True}
    mp = st.get("max_serp_position")
    if mp:
        f["max_serp_position"] = int(mp)
    if extra:
        f.update(extra)
    return f


def pull_brand_source(st: dict) -> list[dict]:
    """Brand Radar SERP visibility — threads on keywords where our brand is tracked."""
    brands = [{"keywords": [k for k in (st.get("brand_keywords") or []) if k],
               "urls": [st.get("target_domain") or ""],
               "url_mode": "subdomains"}]
    args: dict[str, Any] = {"brands": brands, "filters": _reddit_filters(st),
                            "limit": min(int(st.get("brand_limit") or 400), 1000),
                            "sort_by": "volume"}
    if st.get("countries"):
        args["country"] = st["countries"]
    res = ahrefs("ahrefs_brand_radar.reddit_results", args, st["ahrefs_secret"])
    return res.get("records") or []


def pull_keyword_source(st: dict, keywords: list[str],
                        progress: Callable[[str], None] | None = None) -> list[dict]:
    """Reddit pages in the top-N for the tracked keyword list (batched `is`-any rule)."""
    out: list[dict] = []
    batch = 60
    chunks = [keywords[i:i + batch] for i in range(0, len(keywords), batch)]
    for i, ch in enumerate(chunks, 1):
        if progress:
            progress(f"Tracked keywords {i}/{len(chunks)} ({len(ch)} keywords)…")
        args: dict[str, Any] = {
            "filters": _reddit_filters(st, {
                "text_rules": [{"location": "query", "op": "is",
                                "terms": ch, "match": "any"}]}),
            "limit": 1000}
        if st.get("countries"):
            args["country"] = st["countries"]
        res = ahrefs("ahrefs_brand_radar.reddit_results", args, st["ahrefs_secret"])
        out.extend(res.get("records") or [])
    return out


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def _parse_dt(v) -> Optional[datetime]:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def _ingest(s, records: list[dict], source: str, stats: dict, ws_id: str) -> None:
    # A single pull legitimately repeats (keyword, country) across snapshot rows, so
    # the DB-side existence check isn't enough — track what this batch already added.
    added: set[tuple] = set()
    for rec in records:
        kw = (rec.get("keyword") or "").strip()
        country = (rec.get("serp_country") or "").strip()
        vol = rec.get("search_volume")
        upd = _parse_dt(rec.get("updated_at"))
        for th in (rec.get("reddit_threads") or []):
            url = th.get("url") or ""
            key = url_key(url)
            if not key:
                continue
            stats["threads_seen"] += 1
            cites = th.get("citation_counts") or {}
            total_cites = sum(v for k, v in cites.items()
                              if isinstance(v, (int, float)) and k != "other")
            row = s.execute(select(RRThread).where(
                RRThread.workspace_id == ws_id,
                RRThread.url_key == key)).scalar_one_or_none()
            pos = th.get("serp_position")
            if row is None:
                row = RRThread(
                    workspace_id=ws_id, url_key=key, url=url, title=clean_title(th.get("title") or ""),
                    subreddit=(th.get("subreddit") or ""), author=(th.get("username") or ""),
                    posted_at=_parse_dt(th.get("posted_at")),
                    description=(th.get("description") or "")[:6000],
                    best_position=pos, max_volume=vol, ai_citations=int(total_cites or 0),
                    citation_counts=cites, sources=[source], is_new=True,
                    num_comments=th.get("number_of_comments"))
                s.add(row)
                s.flush()
                stats["threads_new"] += 1
            else:
                if pos and (row.best_position is None or pos < row.best_position):
                    row.best_position = pos
                if vol and (row.max_volume is None or vol > row.max_volume):
                    row.max_volume = vol
                if total_cites and total_cites > (row.ai_citations or 0):
                    row.ai_citations = int(total_cites)
                    row.citation_counts = cites
                if source not in (row.sources or []):
                    row.sources = list(row.sources or []) + [source]
                if not row.title:
                    row.title = clean_title(th.get("title") or "")
                if not row.posted_at:
                    row.posted_at = _parse_dt(th.get("posted_at"))
                if not row.subreddit:
                    row.subreddit = th.get("subreddit") or ""
                row.last_seen_at = _now()

            sig = (row.id, kw, country, source)
            if sig in added:
                continue
            exists = s.execute(select(RRHit.id).where(
                RRHit.thread_id == row.id, RRHit.keyword == kw,
                RRHit.country == country, RRHit.source == source)).scalar_one_or_none()
            if exists is None:
                s.add(RRHit(thread_id=row.id, keyword=kw, country=country, source=source,
                            serp_position=pos, search_volume=vol, serp_updated_at=upd,
                            matched_brands=list(th.get("matched_brands") or [])))
                s.flush()
                stats["hits_new"] += 1
            added.add(sig)


def run_scan(job_id: str, ws_id: str | None = None, trigger: str = "manual") -> None:
    """Scan ONE workspace. Every row written carries its workspace_id."""
    st = get_workspace(ws_id)
    if not st:
        raise RuntimeError("No project configured yet — run the setup wizard.")
    ws_id = st["id"]
    gaps = missing_required(st)
    if gaps:
        raise RuntimeError("Not configured yet — open Settings and fill in:\n- "
                           + "\n- ".join(gaps))
    sources = [x for x in (st.get("sources") or []) if x in ("brand", "keywords")] or ["brand"]
    log: list[str] = []
    stats = {"threads_seen": 0, "threads_new": 0, "hits_new": 0}

    with cross_session_scope() as s:
        scan = RRScan(workspace_id=ws_id, trigger=trigger, sources=sources,
                      status="running")
        s.add(scan)
        s.flush()
        scan_id = scan.id

    try:
        kw_count = 0
        if "brand" in sources:
            job_progress(job_id, "Brand Radar SERP visibility…")
            recs = pull_brand_source(st)
            if not recs:
                raise RuntimeError(
                    "Brand Radar returned 0 rows for the brand scope — treat as a failure, "
                    "not an empty result. Check brand keywords / target domain in Settings.")
            log.append(f"brand: {len(recs)} SERP rows")
            with cross_session_scope() as s:
                _ingest(s, recs, "brand", stats, ws_id)

        if "keywords" in sources:
            job_progress(job_id, "Loading tracked keywords…")
            kws = tracked_keywords(st)
            kw_count = len(kws)
            log.append(f"tracked keywords: {kw_count}")
            recs = pull_keyword_source(st, kws, lambda m: job_progress(job_id, m))
            log.append(f"keywords: {len(recs)} SERP rows")
            with cross_session_scope() as s:
                _ingest(s, recs, "keywords", stats, ws_id)

        with cross_session_scope() as s:
            scan = s.get(RRScan, scan_id)
            scan.status = "completed"
            scan.finished_at = _now()
            scan.keywords_used = kw_count
            scan.threads_seen = stats["threads_seen"]
            scan.threads_new = stats["threads_new"]
            scan.hits_new = stats["hits_new"]
            scan.log = log
        job_done(job_id, {"scan_id": scan_id, **stats},
                 f"{stats['threads_new']} new threads, {stats['hits_new']} new keyword hits")
    except Exception as exc:  # noqa: BLE001
        with cross_session_scope() as s:
            scan = s.get(RRScan, scan_id)
            if scan:
                scan.status = "failed"
                scan.finished_at = _now()
                scan.error = str(exc)[:2000]
                scan.log = log
        raise


# ---------------------------------------------------------------------------
# Enrichment — Apify rendered page
# ---------------------------------------------------------------------------

_SCORE_RE = re.compile(r'"score"\s*:\s*(\d+)')
_NCOM_RE = re.compile(r'"number_comments"\s*:\s*(\d+)')
_RATIO_RE = re.compile(r'"upvote_ratio"\s*:\s*([0-9.]+)')
_CREATED_RE = re.compile(r'"created_timestamp"\s*:\s*(\d+)')


def _apify(actor: str, inp: dict, st: dict, limit: int) -> list[dict]:
    res = connector_invoke(
        "apify.run_actor_sync_get_dataset_items",
        {"actor_id": actor, "input": inp, "wait_for_finish": 290,
         "timeout_secs": 270, "limit": limit, "clean": False},
        secret=st.get("apify_secret") or APP_DEFAULTS["apify_secret"], timeout=300)
    return res.get("items") or []


def _fetch_engagement(url: str, st: dict) -> dict:
    """Upvotes / comment count / ratio / post date.

    Reddit answers 403 to every server-side UA available here and the .rss endpoint
    rate-limits instantly, so the page is rendered through the Apify crawler with a
    residential proxy. The numbers are read out of the page's own embedded
    `shreddit-screenview-data` JSON — nothing is estimated.
    """
    items = _apify(st.get("apify_actor") or APP_DEFAULTS["apify_actor"],
                   {"startUrls": [{"url": url}], "maxCrawlPages": 1,
                    "crawlerType": "playwright:firefox", "saveHtml": True,
                    "maxRequestRetries": 2, "readableTextCharThreshold": 50,
                    "proxyConfiguration": {"useApifyProxy": True,
                                           "apifyProxyGroups": ["RESIDENTIAL"]}},
                   st, 1)
    if not items:
        raise RuntimeError("the render returned no page (Reddit likely blocked it)")
    it = items[0]
    raw = _html.unescape(it.get("html") or "")
    title = ((it.get("metadata") or {}).get("title") or "").strip()

    def _first(rx, cast=int):
        m = rx.search(raw)
        return cast(m.group(1)) if m else None

    created = _first(_CREATED_RE)
    score = _first(_SCORE_RE)
    if score is None:
        # A render that came back without the embedded counters is a FAILURE, not
        # "zero upvotes" — Reddit sometimes serves a login wall through the proxy.
        raise RuntimeError("rendered page carried no vote data (login wall?)")
    return {"title": clean_title(title), "upvotes": score,
            "num_comments": _first(_NCOM_RE),
            "upvote_ratio": _first(_RATIO_RE, float),
            "posted_at": (datetime.fromtimestamp(created / 1000, tz=timezone.utc)
                          if created else None)}


def _rss_text(url: str, st: dict) -> dict:
    """Post body, OP, date and comments straight from Reddit's Atom feed.

    Requires `www.reddit.com` on the workspace firewall allowlist. While the HTML and `.json`
    endpoints answer 403 to every server-side UA, `<thread>/.rss` answers 200.
    It carries no vote counts — those still need the rendered page — but it makes
    the text half free and instant instead of a ~1 min Apify actor run.
    It rate-limits (429) on rapid repeats, hence the backoff.
    """
    import time
    import urllib.request

    feed = re.sub(r"/?(\?.*)?$", "", url.split("#")[0]).rstrip("/") + "/.rss"
    raw = ""
    last = ""
    for attempt in range(3):
        try:
            req = urllib.request.Request(feed, headers={
                "User-Agent": "letaido-ranking-reddits/1.0",
                "Accept": "application/atom+xml,application/xml"})
            with urllib.request.urlopen(req, timeout=25) as r:
                raw = r.read().decode("utf-8", "replace")
            break
        except Exception as exc:  # noqa: BLE001
            last = str(exc)[:120]
            time.sleep(3 * (attempt + 1))
    if not raw:
        raise RuntimeError(f"the Atom feed did not answer ({last})")

    entries = re.findall(r"<entry>(.*?)</entry>", raw, re.S)
    if not entries:
        raise RuntimeError("the Atom feed carried no entries")

    def _field(block: str, tag: str) -> str:
        m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.S)
        return _html.unescape(m.group(1)).strip() if m else ""

    def _text(block: str) -> str:
        body = _field(block, "content")
        body = re.sub(r"<br\s*/?>|</p>", "\n", body)
        body = re.sub(r"<[^>]+>", " ", body)
        body = _html.unescape(body)
        # every entry ends with reddit's own "submitted by / [link] [comments]" chrome
        body = re.sub(r"\s*submitted by\s*/u/\S+\s*\[link\]\s*\[comments\]\s*$", "", body)
        return "\n".join(ln.strip() for ln in body.split("\n") if ln.strip())

    maxc = int(st.get("max_comments") or APP_DEFAULTS["max_comments"])
    post, comments = entries[0], entries[1:1 + maxc]
    author = _field(post, "name").lstrip("/").removeprefix("u/")
    parts = []
    body = _text(post)
    if body:
        parts.append(body)
    kept = 0
    if comments:
        lines = []
        for c in comments:
            t = _text(c)
            who = _field(c, "name").lstrip("/").removeprefix("u/") or "?"
            if t and t != "[deleted]":
                lines.append(f"u/{who}: {t}")
                kept += 1
        if lines:
            parts.append(f"\n--- Top {kept} comments ---")
            parts += lines
    return {"title": clean_title(_field(post, "title")),
            "author": author,
            "posted_at": _parse_dt(_field(post, "published")),
            "body_md": "\n\n".join(parts)[:40000],
            "comments_fetched": kept}


def _fetch_thread_text(url: str, st: dict) -> dict:
    """Post body, OP username, post date and the top comments, via the Reddit actor."""
    maxc = int(st.get("max_comments") or APP_DEFAULTS["max_comments"])
    items = _apify(st.get("apify_thread_actor") or APP_DEFAULTS["apify_thread_actor"],
                   {"startUrls": [{"url": url}], "maxItems": maxc + 1,
                    "maxComments": maxc, "maxPostCount": 1, "skipComments": False,
                    "proxy": {"useApifyProxy": True}},
                   st, maxc + 3)
    post, comments = None, []
    for it in items:
        if it.get("dataType") == "post" and post is None:
            post = it
        elif it.get("dataType") == "comment":
            comments.append(it)
    if post is None and not comments:
        raise RuntimeError("the Reddit actor returned no post or comments")

    def _clean(t: str) -> str:
        return _html.unescape(t or "").strip()

    parts = []
    if post:
        body = _clean(post.get("body"))
        # a link post's "body" is just the reddit chrome line
        if body and "submitted by" not in body[:40]:
            parts.append(body)
    if comments:
        parts.append(f"\n--- Top {len(comments)} comments ---")
        for c in comments:
            b = _clean(c.get("body"))
            if b and b != "[deleted]":
                parts.append(f"u/{c.get('username') or '?'}: {b}")
    out = {"body_md": "\n\n".join(parts)[:40000],
           "comments_fetched": len(comments)}
    if post:
        out["title"] = clean_title(post.get("title") or "")
        out["author"] = post.get("username") or ""
        out["posted_at"] = _parse_dt(post.get("createdAt"))
    return out


def _apify_fetch(url: str, st: dict) -> dict:
    """Both halves. Either can fail without losing the other."""
    data: dict = {}
    errors: list[str] = []
    if (url_key(url) or "").startswith("r/"):
        # A subreddit landing page ranked, not a thread. Its feed/render describes
        # whatever is newest in the sub, which would be a misleading "title" and
        # "upvotes" for this card — so leave it as-is rather than fake it.
        return {"body_md": "", "partial_error":
                "This card is a subreddit landing page, not a single thread, "
                "so there is no post body or upvote count to fetch."}
    # Text first from the free Atom feed, falling back to the Apify actor; then the
    # rendered page for vote counters (the residential proxy serves Reddit's login
    # wall maybe half the time, and the wall carries no vote data — hence 3 tries).
    def _text_half(u: str, s: dict) -> dict:
        try:
            return _rss_text(u, s)
        except Exception as rss_exc:  # noqa: BLE001
            try:
                return _fetch_thread_text(u, s)
            except Exception as apify_exc:  # noqa: BLE001
                raise RuntimeError(f"feed: {rss_exc} / actor: {apify_exc}") from None

    for fn, label, tries in ((_text_half, "thread text", 1),
                             (_fetch_engagement, "engagement counters", 3)):
        last = ""
        for attempt in range(tries):
            try:
                for k, v in (fn(url, st) or {}).items():
                    # first non-empty value wins; the text actor runs first and is richer
                    if data.get(k) in (None, "", 0) and v not in (None, ""):
                        data[k] = v
                last = ""
                break
            except Exception as exc:  # noqa: BLE001
                last = f"{label}: {str(exc)[:200]}"
        if last:
            errors.append(last)
    if not data:
        raise RuntimeError("Could not read the thread. " + " | ".join(errors))
    data["partial_error"] = " | ".join(errors)
    return data


def fetch_thread(job_id: str, thread_id: str, then_notes: bool = True) -> None:
    st = app_settings()
    job_progress(job_id, "Rendering the thread (this takes ~1 min)…")
    _apify_fetch_into(thread_id, st)

    if then_notes and st.get("auto_notes_on_open"):
        job_progress(job_id, "Drafting notes…")
        _draft_notes(thread_id, st)
    job_done(job_id, {"thread_id": thread_id}, "Fetched")


def _json_loads(raw: str) -> dict:
    """Parse a model reply that may arrive wrapped in a ```json fence."""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"```\s*$", "", s).strip()
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        m = re.search(r"\{.*\}", s, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


NOTES_PROMPT = """You read a Reddit thread on behalf of the marketing team that owns \
the brand named in the input. Write short DRAFT reading notes.

Return JSON with exactly these keys:
  "summary": one sentence on what the thread is actually about.
  "brand": array of 2-4 short bullet strings — what is said about Ahrefs (or its
     competitors), the sentiment, factual errors worth correcting, and whether a
     reply from Ahrefs would help. If Ahrefs isn't discussed, say so in one bullet.
  "content": array of 2-4 short bullet strings — the content/keyword angle: the
     question that isn't well answered, whether a help article covers it, and a
     concrete article or update idea.
  "sentiment": one of "positive", "mixed", "negative", "neutral".
  "reply_worthy": true or false.

Rules: never invent a quote or a metric. If the thread body is missing, say so
rather than guessing. Keep each bullet under 25 words."""


def _draft_notes(thread_id: str, st: dict | None = None) -> dict:
    st = st or app_settings()
    with cross_session_scope() as s:
        row = s.get(RRThread, thread_id)
        if not row:
            raise RuntimeError("Card not found.")
        row.notes_status = "running"
        row.notes_error = ""
        ctx = {
            "brand": st.get("brand_keywords") if st else None,
            "brand_domain": (st or {}).get("target_domain"),
            "url": row.url, "title": row.title, "subreddit": row.subreddit,
            "posted_at": row.posted_at.isoformat() if row.posted_at else None,
            "upvotes": row.upvotes, "num_comments": row.num_comments,
            "serp_snippet": row.description,
            "keywords_it_ranks_for": [h.keyword for h in s.execute(
                select(RRHit).where(RRHit.thread_id == thread_id).limit(25)).scalars().all()],
            "thread_text": (row.body_md or "")[:18000],
        }
    try:
        r = _llm.chat.completions.create(
            model=CHAT_MODEL, temperature=0.2, max_tokens=1200,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": NOTES_PROMPT},
                      {"role": "user", "content": json.dumps(ctx)}])
        notes = _json_loads(r.choices[0].message.content or "{}")
    except Exception as exc:  # noqa: BLE001
        with cross_session_scope() as s:
            row = s.get(RRThread, thread_id)
            row.notes_status = "failed"
            row.notes_error = str(exc)[:800]
        raise
    with cross_session_scope() as s:
        row = s.get(RRThread, thread_id)
        row.ai_notes = notes
        row.notes_status = "done"
    return notes


def enrich_batch(job_id: str, ws_id: str, limit: int = 10, notes: bool = False) -> None:
    """Fetch details for the next N never-fetched cards.

    Deliberately bounded: each card is two Apify actor runs (~1 min), so an
    unbounded 'enrich everything' would run for hours and spend real money.
    Ordered newest-thread-first so the most relevant cards fill in first.
    """
    with cross_session_scope() as s:
        ids = [r.id for r in s.execute(
            select(RRThread).where(RRThread.workspace_id == ws_id,
                                   RRThread.fetch_status == "pending")
            .order_by(RRThread.posted_at.desc().nullslast())
            .limit(max(1, min(int(limit), 25)))).scalars().all()]
    if not ids:
        job_done(job_id, {"done": 0}, "Every card already has its details.")
        return
    ok, failed = 0, []
    st = app_settings()
    for i, tid in enumerate(ids, 1):
        job_progress(job_id, f"Fetching card {i}/{len(ids)}…")
        if i > 1:
            import time
            time.sleep(6)   # Reddit's Atom feed 429s on rapid repeats
        try:
            data = _apify_fetch_into(tid, st)
            ok += 1
            if notes and data:
                try:
                    _draft_notes(tid, st)
                except Exception as exc:  # noqa: BLE001
                    failed.append(f"notes: {str(exc)[:120]}")
        except Exception as exc:  # noqa: BLE001
            failed.append(str(exc)[:160])
    job_done(job_id, {"done": ok, "failed": failed},
             f"{ok}/{len(ids)} cards enriched" + (f"; {len(failed)} failed" if failed else ""))


def _apify_fetch_into(thread_id: str, st: dict) -> dict:
    """Fetch one card's details and persist them. Shared by open-card and batch."""
    with cross_session_scope() as s:
        row = s.get(RRThread, thread_id)
        if not row:
            raise RuntimeError("Card not found.")
        url = row.url
        row.fetch_status = "running"
        row.fetch_error = ""
    try:
        data = _apify_fetch(url, st)
    except Exception as exc:  # noqa: BLE001
        with cross_session_scope() as s:
            row = s.get(RRThread, thread_id)
            row.fetch_status = "failed"
            row.fetch_error = str(exc)[:1000]
        raise
    with cross_session_scope() as s:
        row = s.get(RRThread, thread_id)
        if data.get("title"):
            row.title = data["title"]
        if data.get("upvotes") is not None:
            row.upvotes = data["upvotes"]
        if data.get("num_comments") is not None:
            row.num_comments = data["num_comments"]
        if data.get("upvote_ratio") is not None:
            row.upvote_ratio = data["upvote_ratio"]
        if data.get("posted_at") and not row.posted_at:
            row.posted_at = data["posted_at"]
        if data.get("author") and not row.author:
            row.author = data["author"]
        if data.get("body_md"):
            row.body_md = data["body_md"]
        row.fetch_status = "done"
        row.fetch_error = data.get("partial_error") or ""
        row.fetched_at = _now()
    return data


def draft_notes_job(job_id: str, thread_id: str) -> None:
    job_progress(job_id, "Drafting notes…")
    notes = _draft_notes(thread_id)
    job_done(job_id, {"thread_id": thread_id, "notes": notes}, "Notes drafted")


# ---------------------------------------------------------------------------
# Scrapbook hand-off
# ---------------------------------------------------------------------------

def push_to_scrapbook(thread_id: str, saved_by: str = "") -> str:
    """Save the card into the Scrapbook library as a URL item with its AI notes."""
    from applications.scrapbook import SBItem  # local import: avoid a cycle at load

    with cross_session_scope() as s:
        row = s.get(RRThread, thread_id)
        if not row:
            raise RuntimeError("Card not found.")
        if row.scrapbook_item_id:
            return row.scrapbook_item_id
        kws = [h.keyword for h in s.execute(
            select(RRHit).where(RRHit.thread_id == thread_id).limit(30)).scalars().all()]
        notes = row.ai_notes or {}
        note_lines = [notes.get("summary") or ""]
        for k in ("brand", "content"):
            for b in (notes.get(k) or []):
                note_lines.append(f"- {b}")
        if row.user_notes:
            note_lines += ["", row.user_notes]
        item = SBItem(
            type="url", title=row.title or row.url, url=row.url,
            note="\n".join(x for x in note_lines if x).strip(),
            content_md=(row.body_md or "")[:20000],
            meta={"source": "ranking_reddits", "subreddit": row.subreddit,
                  "upvotes": row.upvotes, "num_comments": row.num_comments,
                  "best_serp_position": row.best_position,
                  "ai_citations": row.ai_citations,
                  "ranking_keywords": kws[:30],
                  "sources": list(row.sources or [])},
            tags=["reddit", "ranking-reddits"] + ([f"r/{row.subreddit}"] if row.subreddit else []),
            saved_by=saved_by, scrape_status="na",
            ai_note=(row.ai_notes or None),
            ai_note_status="done" if row.ai_notes else "pending")
        s.add(item)
        s.flush()
        row.scrapbook_item_id = item.id
        return item.id


# ---------------------------------------------------------------------------
# Reads for the UI
# ---------------------------------------------------------------------------

def list_cards(*, ws_id: str, source: str = "all", subreddit: str = "", state: str = "all",
               sort: str = "newest", q: str = "", limit: int = 200,
               offset: int = 0) -> dict:
    with cross_session_scope() as s:
        stmt = select(RRThread).where(RRThread.workspace_id == ws_id)
        if source in ("brand", "keywords"):
            stmt = stmt.where(RRThread.sources.any(source))
        if subreddit:
            stmt = stmt.where(func.lower(RRThread.subreddit) == subreddit.lower())
        if state == "new":
            stmt = stmt.where(RRThread.is_new.is_(True))
        elif state == "unread":
            stmt = stmt.where(RRThread.is_read.is_(False))
        elif state == "read":
            stmt = stmt.where(RRThread.is_read.is_(True))
        elif state == "saved":
            stmt = stmt.where(RRThread.scrapbook_item_id.isnot(None))
        if q:
            like = f"%{q.lower()}%"
            stmt = stmt.where(func.lower(RRThread.title).like(like)
                              | func.lower(RRThread.description).like(like)
                              | func.lower(RRThread.subreddit).like(like))
        order = {
            "newest": RRThread.posted_at.desc().nullslast(),
            "found": RRThread.first_seen_at.desc(),
            "position": RRThread.best_position.asc().nullslast(),
            "upvotes": RRThread.upvotes.desc().nullslast(),
            "volume": RRThread.max_volume.desc().nullslast(),
            "citations": RRThread.ai_citations.desc(),
        }.get(sort, RRThread.posted_at.desc().nullslast())
        rows = s.execute(stmt.order_by(order).limit(limit).offset(offset)).scalars().all()
        ids = [r.id for r in rows]
        hits: dict[str, list] = {i: [] for i in ids}
        if ids:
            for h in s.execute(select(RRHit).where(RRHit.thread_id.in_(ids))).scalars().all():
                hits[h.thread_id].append(
                    {"keyword": h.keyword, "country": h.country, "source": h.source,
                     "position": h.serp_position, "volume": h.search_volume})
        for v in hits.values():
            v.sort(key=lambda d: (d["position"] or 99, -(d["volume"] or 0)))
        return {"cards": [r.to_dict(hits.get(r.id, [])) for r in rows]}


def stats(ws_id: str | None = None) -> dict:
    ws = get_workspace(ws_id)
    if not ws:
        return {"config_missing": missing_required(None), "workspace": None,
                "total": 0, "new": 0, "unread": 0, "saved": 0,
                "subreddits": [], "by_source": {"brand": 0, "keywords": 0},
                "last_scan": None, "workspaces": []}
    wid = ws["id"]
    with cross_session_scope() as s:
        def n(*where):
            stmt = select(func.count(RRThread.id)).where(RRThread.workspace_id == wid)
            for w in where:
                stmt = stmt.where(w)
            return s.execute(stmt).scalar() or 0

        subs = s.execute(
            select(RRThread.subreddit, func.count(RRThread.id))
            .where(RRThread.workspace_id == wid, RRThread.subreddit != "")
            .group_by(RRThread.subreddit)
            .order_by(func.count(RRThread.id).desc())).all()
        last = s.execute(select(RRScan).where(RRScan.workspace_id == wid)
                         .order_by(RRScan.started_at.desc()).limit(1)).scalars().first()
        return {"config_missing": missing_required(ws),
                "workspace": ws,
                "workspaces": list_workspaces(),
                "total": n(), "new": n(RRThread.is_new.is_(True)),
                "unread": n(RRThread.is_read.is_(False)),
                "saved": n(RRThread.scrapbook_item_id.isnot(None)),
                "subreddits": [{"name": a, "count": b} for a, b in subs],
                "by_source": {src: n(RRThread.sources.any(src))
                              for src in ("brand", "keywords")},
                "last_scan": last.to_dict() if last else None}


def scan_history(ws_id: str | None = None, limit: int = 12) -> list[dict]:
    with cross_session_scope() as s:
        stmt = select(RRScan)
        if ws_id:
            stmt = stmt.where(RRScan.workspace_id == ws_id)
        rows = s.execute(stmt.order_by(RRScan.started_at.desc())
                         .limit(limit)).scalars().all()
        return [r.to_dict() for r in rows]
