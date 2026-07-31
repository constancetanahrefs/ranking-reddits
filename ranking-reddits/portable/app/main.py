"""Flask app — routes only. Every input goes through a Pydantic model."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from flask import Flask, jsonify, render_template, request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from . import ahrefs, engine
from .config import config
from .models import Hit, SessionLocal, Thread, init_db

app = Flask(__name__, template_folder="../templates")
init_db()


def _now():
    return datetime.now(timezone.utc)


def _validate(model):
    try:
        return model(**(request.get_json(silent=True) or {}))
    except ValidationError as e:
        raise _Invalid(e.errors()) from None


class _Invalid(Exception):
    def __init__(self, details):
        self.details = details


@app.errorhandler(_Invalid)
def _on_invalid(e):
    return jsonify({"error": "validation_error", "details": str(e.details)[:800]}), 422


# ── Schemas ───────────────────────────────────────────────────────────────

class ThreadIdIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thread_id: str = Field(min_length=1, max_length=36)


class OpenIn(ThreadIdIn):
    fetch: bool = True


class NotesIn(ThreadIdIn):
    user_notes: str = Field(default="", max_length=20000)


class MarkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thread_id: Optional[str] = Field(default=None, max_length=36)
    all_cards: bool = False
    is_read: Optional[bool] = None
    clear_new: bool = False


class EnrichIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=10, ge=1, le=25)


class ScanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trigger: str = Field(default="manual", pattern=r"^(manual|monthly)$")


class SaveIn(ThreadIdIn):
    ref: str = Field(default="", max_length=200)


# ── Pages ─────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return render_template("index.html", stats=engine.stats(),
                           initial=engine.list_cards(limit=60)["cards"],
                           cfg={"target_domain": config.target_domain,
                                "max_serp_position": config.max_serp_position,
                                "notes_enabled": config.notes_enabled,
                                "votes_enabled": config.votes_enabled})


# ── API ───────────────────────────────────────────────────────────────────

@app.get("/api/cards")
def api_cards():
    a = request.args
    return jsonify(engine.list_cards(
        source=a.get("source", "all"), subreddit=a.get("subreddit", ""),
        state=a.get("state", "all"), sort=a.get("sort", "newest"),
        q=a.get("q", "")[:200], limit=min(int(a.get("limit", 200)), 500)))


@app.get("/api/stats")
def api_stats():
    return jsonify(engine.stats())


@app.get("/api/card/<thread_id>")
def api_card(thread_id):
    with SessionLocal() as s:
        row = s.get(Thread, thread_id)
        if not row:
            return jsonify({"error": "not_found"}), 404
        hits = [{"keyword": h.keyword, "country": h.country, "source": h.source,
                 "position": h.serp_position, "volume": h.search_volume}
                for h in s.execute(select(Hit).where(Hit.thread_id == thread_id)).scalars().all()]
        hits.sort(key=lambda d: (d["position"] or 99, -(d["volume"] or 0)))
        return jsonify(row.to_dict(hits))


@app.post("/api/open")
def api_open():
    body = _validate(OpenIn)
    with SessionLocal() as s:
        row = s.get(Thread, body.thread_id)
        if not row:
            return jsonify({"error": "not_found"}), 404
        if not row.is_read:
            row.is_read = True
            row.read_at = _now()
        row.is_new = False
        needs = row.fetch_status in ("pending", "failed")
        s.commit()
    job = None
    if body.fetch and needs and config.auto_fetch_on_open:
        job = engine.job_run("fetch", engine.fetch_job, body.thread_id, True)
    return jsonify({"ok": True, "job_id": job})


@app.post("/api/fetch")
def api_fetch():
    body = _validate(ThreadIdIn)
    return jsonify({"job_id": engine.job_run("fetch", engine.fetch_job, body.thread_id, True)})


@app.post("/api/enrich")
def api_enrich():
    body = _validate(EnrichIn)
    return jsonify({"job_id": engine.job_run("enrich", engine.enrich_batch, body.limit)})


@app.post("/api/notes/draft")
def api_notes_draft():
    if not config.notes_enabled:
        return jsonify({"error": "AI notes are disabled — set LLM_API_KEY."}), 400
    body = _validate(ThreadIdIn)
    return jsonify({"job_id": engine.job_run("notes", engine.notes_job, body.thread_id)})


@app.post("/api/notes/save")
def api_notes_save():
    body = _validate(NotesIn)
    with SessionLocal() as s:
        row = s.get(Thread, body.thread_id)
        if not row:
            return jsonify({"error": "not_found"}), 404
        row.user_notes = body.user_notes
        s.commit()
    return jsonify({"ok": True})


@app.post("/api/mark")
def api_mark():
    body = _validate(MarkIn)
    with SessionLocal() as s:
        if body.all_cards:
            rows = s.execute(select(Thread)).scalars().all()
        elif body.thread_id:
            r = s.get(Thread, body.thread_id)
            rows = [r] if r else []
        else:
            rows = []
        for row in rows:
            if body.is_read is not None:
                row.is_read = body.is_read
                row.read_at = _now() if body.is_read else None
            if body.clear_new:
                row.is_new = False
        s.commit()
        return jsonify({"ok": True, "updated": len(rows)})


@app.post("/api/save")
def api_save():
    """Mark a card as exported to your system of record.

    Replace the body of this route to push into Notion / Airtable / your CMS —
    it exists as a seam, not a finished integration.
    """
    body = _validate(SaveIn)
    with SessionLocal() as s:
        row = s.get(Thread, body.thread_id)
        if not row:
            return jsonify({"error": "not_found"}), 404
        row.saved_ref = body.ref or f"saved:{_now().date().isoformat()}"
        s.commit()
        return jsonify({"ok": True, "ref": row.saved_ref})


@app.post("/api/scan")
def api_scan():
    body = _validate(ScanIn)
    missing = config.missing_required()
    if missing:
        return jsonify({"error": "config_incomplete", "missing": missing}), 400
    return jsonify({"job_id": engine.job_run("scan", engine.run_scan, body.trigger)})


@app.get("/api/scans")
def api_scans():
    return jsonify({"scans": engine.scan_history()})


@app.get("/api/job/<job_id>")
def api_job(job_id):
    j = engine.job_get(job_id)
    return (jsonify(j), 200) if j else (jsonify({"error": "not_found"}), 404)


@app.get("/api/ahrefs/projects")
def api_projects():
    """Free endpoint — lets the UI offer a picker instead of asking for an id."""
    try:
        return jsonify({"projects": ahrefs.list_projects()})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)[:300]}), 400


@app.get("/api/ahrefs/usage")
def api_usage():
    """Free endpoint — show the unit budget before a billable scan."""
    try:
        return jsonify(ahrefs.limits_and_usage())
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)[:300]}), 400


if __name__ == "__main__":
    # Dev only. In production use gunicorn — see portable/README.md.
    app.run(host="127.0.0.1", port=8000, debug=False, threaded=True)
