"""Scan, enrichment and AI notes."""
from __future__ import annotations

import json
import re
import threading
import time
import traceback
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy import func, select

from . import ahrefs, reddit
from .config import config
from .models import Hit, Scan, SessionLocal, Thread


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Jobs: anything slow returns a job id and is polled ────────────────────

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def job_new(kind: str) -> str:
    jid = str(uuid.uuid4())
    with _lock:
        _jobs[jid] = {"status": "running", "kind": kind, "progress": "Starting…",
                      "result": None, "error": None}
        for old in list(_jobs)[:-60]:
            _jobs.pop(old, None)
    return jid


def job_progress(jid: str, msg: str) -> None:
    with _lock:
        if jid in _jobs:
            _jobs[jid]["progress"] = msg


def job_done(jid: str, result: Any = None, progress: str = "Done") -> None:
    with _lock:
        if jid in _jobs:
            _jobs[jid].update(status="completed", result=result, progress=progress)


def job_fail(jid: str, err: str) -> None:
    with _lock:
        if jid in _jobs:
            _jobs[jid].update(status="failed", error=err, progress=f"Failed: {err}")


def job_get(jid: str) -> Optional[dict]:
    with _lock:
        j = _jobs.get(jid)
        return dict(j) if j else None


def job_run(kind: str, fn: Callable[..., Any], *args) -> str:
    jid = job_new(kind)

    def wrap():
        try:
            fn(jid, *args)
        except Exception as e:  # noqa: BLE001
            job_fail(jid, str(e)[:400])
            print(f"[{kind}] {jid} failed:\n{traceback.format_exc()}")

    threading.Thread(target=wrap, daemon=True).start()
    return jid


# ── Scan ──────────────────────────────────────────────────────────────────

def _keyword_set(source: str) -> list[str]:
    if source == "keywords":
        kws = ahrefs.project_keywords(config.rt_project_id)
        if config.rt_tags:
            wanted = {t.lower() for t in config.rt_tags}
            tagged: set[str] = set()
            for row in ahrefs.rank_tracker_overview(config.rt_project_id):
                kw = (row.get("keyword") or "").strip()
                tags = [str(t).lower() for t in (row.get("tags") or [])]
                if kw and wanted & set(tags):
                    tagged.add(kw)
            if not tagged:
                raise ahrefs.AhrefsError(
                    f"No tracked keywords carry the tag(s) {config.rt_tags}.")
            kws = sorted(tagged)
        return kws
    return ahrefs.expand_brand_keywords(config.brand_keywords, config.target_domain)


def _ingest(s, rows: list[dict], source: str, stats: dict) -> None:
    """Upsert threads + hits. Two dedupe layers — see docs/DATA_MODEL.md."""
    added: set[tuple] = set()
    for r in rows:
        url = r.get("url") or ""
        key = reddit.url_key(url)
        if not key:
            continue
        stats["threads_seen"] += 1
        kw = (r.get("keyword") or "").strip()
        country = (r.get("country") or "").strip()
        pos = r.get("serp_position")
        vol = r.get("search_volume")

        row = s.execute(select(Thread).where(Thread.url_key == key)).scalar_one_or_none()
        if row is None:
            row = Thread(
                url_key=key, url=url, title=reddit.clean_title(r.get("title") or ""),
                subreddit=reddit.subreddit_of(url),
                description=(r.get("description") or "")[:6000],
                best_position=pos, max_volume=vol, sources=[source], is_new=True)
            s.add(row)
            s.flush()
            stats["threads_new"] += 1
        else:
            if pos and (row.best_position is None or pos < row.best_position):
                row.best_position = pos
            if vol and (row.max_volume is None or vol > row.max_volume):
                row.max_volume = vol
            if source not in (row.sources or []):
                row.sources = list(row.sources or []) + [source]
            if not row.title:
                row.title = reddit.clean_title(r.get("title") or "")
            row.last_seen_at = _now()

        # the unique constraint alone isn't enough: one API response repeats the
        # same (keyword, country) across snapshot rows and would abort the batch
        sig = (row.id, kw, country, source)
        if sig in added:
            continue
        exists = s.execute(select(Hit.id).where(
            Hit.thread_id == row.id, Hit.keyword == kw,
            Hit.country == country, Hit.source == source)).scalar_one_or_none()
        if exists is None:
            s.add(Hit(thread_id=row.id, keyword=kw, country=country, source=source,
                      serp_position=pos, search_volume=vol))
            s.flush()
            stats["hits_new"] += 1
        added.add(sig)


def run_scan(job_id: str, trigger: str = "manual") -> None:
    missing = config.missing_required()
    if missing:
        raise RuntimeError("Configuration incomplete:\n- " + "\n- ".join(missing))

    sources = [x for x in config.sources if x in ("brand", "keywords")]
    log: list[str] = []
    stats = {"threads_seen": 0, "threads_new": 0, "hits_new": 0}

    with SessionLocal() as s:
        scan = Scan(trigger=trigger, sources=sources, status="running")
        s.add(scan)
        s.commit()
        scan_id = scan.id

    try:
        total_kw = 0
        for source in sources:
            job_progress(job_id, f"Loading keywords for the {source} source…")
            kws = _keyword_set(source)
            if len(kws) > config.max_keywords_per_scan:
                log.append(f"{source}: capped {len(kws)} -> "
                           f"{config.max_keywords_per_scan} keywords (MAX_KEYWORDS_PER_SCAN)")
                kws = kws[:config.max_keywords_per_scan]
            total_kw += len(kws)
            log.append(f"{source}: {len(kws)} keywords")

            rows: list[dict] = []
            for i, kw in enumerate(kws, 1):
                if i % 5 == 0 or i == 1:
                    job_progress(job_id, f"{source}: SERP {i}/{len(kws)} — “{kw}”")
                for country in config.countries:
                    try:
                        rows += ahrefs.reddit_rows_for_keyword(
                            kw, country, config.max_serp_position)
                    except ahrefs.AhrefsError as e:
                        log.append(f"{source}/{kw}/{country}: {str(e)[:160]}")
                time.sleep(0.4)          # stay well inside ~60 req/min

            log.append(f"{source}: {len(rows)} reddit SERP rows")
            with SessionLocal() as s:
                _ingest(s, rows, source, stats)
                s.commit()

        # A zero-row scan is a FAILURE, not a quiet month.
        if stats["threads_seen"] == 0:
            raise RuntimeError(
                "0 Reddit rows across every keyword and market. Treated as a transport "
                "failure, not an empty result — check the API key, the keyword list and "
                "MAX_SERP_POSITION before believing 'nothing ranks'.")

        with SessionLocal() as s:
            scan = s.get(Scan, scan_id)
            scan.status = "completed"
            scan.finished_at = _now()
            scan.keywords_used = total_kw
            scan.threads_seen = stats["threads_seen"]
            scan.threads_new = stats["threads_new"]
            scan.hits_new = stats["hits_new"]
            scan.log = log
            s.commit()
        job_done(job_id, {"scan_id": scan_id, **stats},
                 f"{stats['threads_new']} new threads, {stats['hits_new']} new keyword hits")
    except Exception as e:  # noqa: BLE001
        with SessionLocal() as s:
            scan = s.get(Scan, scan_id)
            if scan:
                scan.status = "failed"
                scan.finished_at = _now()
                scan.error = str(e)[:2000]
                scan.log = log
                s.commit()
        raise


# ── Enrichment ────────────────────────────────────────────────────────────

def enrich_one(thread_id: str) -> dict:
    with SessionLocal() as s:
        row = s.get(Thread, thread_id)
        if not row:
            raise RuntimeError("Card not found.")
        url = row.url
        row.fetch_status = "running"
        row.fetch_error = ""
        s.commit()
    try:
        data = reddit.enrich(url)
    except Exception as e:  # noqa: BLE001
        with SessionLocal() as s:
            row = s.get(Thread, thread_id)
            row.fetch_status = "failed"
            row.fetch_error = str(e)[:1000]
            s.commit()
        raise
    with SessionLocal() as s:
        row = s.get(Thread, thread_id)
        for k in ("title", "author"):
            if data.get(k) and not getattr(row, k):
                setattr(row, k, data[k])
        # only set when present — absent stays NULL, never 0
        for k in ("upvotes", "num_comments", "upvote_ratio"):
            if data.get(k) is not None:
                setattr(row, k, data[k])
        if data.get("posted_at") and not row.posted_at:
            row.posted_at = data["posted_at"]
        if data.get("body_md"):
            row.body_md = data["body_md"]
        row.fetch_status = "done"
        row.fetch_error = data.get("partial_error") or ""
        row.fetched_at = _now()
        s.commit()
    return data


def fetch_job(job_id: str, thread_id: str, then_notes: bool = True) -> None:
    job_progress(job_id, "Reading the thread…")
    enrich_one(thread_id)
    if then_notes and config.notes_enabled and config.auto_notes_on_open:
        job_progress(job_id, "Drafting notes…")
        try:
            draft_notes(thread_id)
        except Exception as e:  # noqa: BLE001
            job_done(job_id, {"thread_id": thread_id},
                     f"Fetched; notes failed: {str(e)[:120]}")
            return
    job_done(job_id, {"thread_id": thread_id}, "Fetched")


def enrich_batch(job_id: str, limit: int = 10) -> None:
    """Bounded on purpose — each card is a real fetch with real cost."""
    limit = max(1, min(int(limit), 25))
    with SessionLocal() as s:
        ids = [r.id for r in s.execute(
            select(Thread).where(Thread.fetch_status == "pending")
            .order_by(Thread.posted_at.desc().nullslast()).limit(limit)).scalars().all()]
    if not ids:
        job_done(job_id, {"done": 0}, "Every card already has its details.")
        return
    ok, failed = 0, []
    for i, tid in enumerate(ids, 1):
        job_progress(job_id, f"Fetching card {i}/{len(ids)}…")
        if i > 1:
            time.sleep(6)               # Reddit's feed 429s on rapid repeats
        try:
            enrich_one(tid)
            ok += 1
            if config.notes_enabled:
                try:
                    draft_notes(tid)
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            failed.append(str(e)[:160])
    job_done(job_id, {"done": ok, "failed": failed},
             f"{ok}/{len(ids)} cards enriched" + (f"; {len(failed)} failed" if failed else ""))


# ── AI notes ──────────────────────────────────────────────────────────────

NOTES_PROMPT = """You read a Reddit thread on behalf of a marketing team that owns \
the brand described in the input. Write short DRAFT reading notes.

Return JSON with exactly these keys:
  "summary": one sentence on what the thread is actually about.
  "brand": array of 2-4 short bullet strings — what is said about the brand (or its
     competitors), the sentiment, factual errors worth correcting, and whether a
     reply from the brand would help. If the brand isn't discussed, say so in one bullet.
  "content": array of 2-4 short bullet strings — the content/keyword angle: the
     question that isn't well answered, whether existing docs cover it, and a
     concrete article or update idea.
  "sentiment": one of "positive", "mixed", "negative", "neutral".
  "reply_worthy": true or false.

Rules: never invent a quote or a metric. If the thread body is missing, say so
rather than guessing from the title. Keep each bullet under 25 words."""


def _json_loads(raw: str) -> dict:
    """Providers wrap JSON-mode replies in ``` fences even when told not to."""
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


def _llm(messages: list[dict]) -> str:
    body = json.dumps({"model": config.llm_model, "messages": messages,
                       "temperature": 0.2, "max_tokens": 1200,
                       "response_format": {"type": "json_object"}}).encode()
    req = urllib.request.Request(
        f"{config.llm_base_url.rstrip('/')}/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {config.llm_api_key}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    return (d["choices"][0]["message"].get("content") or "").strip()


def draft_notes(thread_id: str) -> dict:
    if not config.notes_enabled:
        raise RuntimeError("No LLM_API_KEY set — AI notes are disabled.")
    with SessionLocal() as s:
        row = s.get(Thread, thread_id)
        if not row:
            raise RuntimeError("Card not found.")
        row.notes_status = "running"
        row.notes_error = ""
        ctx = {
            "brand": config.brand_keywords, "brand_domain": config.target_domain,
            "url": row.url, "title": row.title, "subreddit": row.subreddit,
            "posted_at": row.posted_at.isoformat() if row.posted_at else None,
            "upvotes": row.upvotes, "num_comments": row.num_comments,
            "serp_snippet": row.description,
            "keywords_it_ranks_for": [h.keyword for h in s.execute(
                select(Hit).where(Hit.thread_id == thread_id).limit(25)).scalars().all()],
            "thread_text": (row.body_md or "")[:18000],
        }
        s.commit()
    try:
        notes = _json_loads(_llm([{"role": "system", "content": NOTES_PROMPT},
                                  {"role": "user", "content": json.dumps(ctx)}]))
    except Exception as e:  # noqa: BLE001
        with SessionLocal() as s:
            row = s.get(Thread, thread_id)
            row.notes_status = "failed"
            row.notes_error = str(e)[:800]
            s.commit()
        raise
    with SessionLocal() as s:
        row = s.get(Thread, thread_id)
        row.ai_notes = notes
        row.notes_status = "done"
        s.commit()
    return notes


def notes_job(job_id: str, thread_id: str) -> None:
    job_progress(job_id, "Drafting notes…")
    job_done(job_id, {"notes": draft_notes(thread_id)}, "Notes drafted")


# ── Reads ─────────────────────────────────────────────────────────────────

def list_cards(*, source="all", subreddit="", state="all", sort="newest",
               q="", limit=200, offset=0) -> dict:
    with SessionLocal() as s:
        stmt = select(Thread)
        if source in ("brand", "keywords"):
            stmt = stmt.where(Thread.sources.any(source))
        if subreddit:
            stmt = stmt.where(func.lower(Thread.subreddit) == subreddit.lower())
        if state == "new":
            stmt = stmt.where(Thread.is_new.is_(True))
        elif state == "unread":
            stmt = stmt.where(Thread.is_read.is_(False))
        elif state == "read":
            stmt = stmt.where(Thread.is_read.is_(True))
        elif state == "saved":
            stmt = stmt.where(Thread.saved_ref.isnot(None))
        if q:
            like = f"%{q.lower()}%"
            stmt = stmt.where(func.lower(Thread.title).like(like)
                              | func.lower(Thread.description).like(like)
                              | func.lower(Thread.subreddit).like(like))
        order = {
            "newest": Thread.posted_at.desc().nullslast(),
            "found": Thread.first_seen_at.desc(),
            "position": Thread.best_position.asc().nullslast(),
            "upvotes": Thread.upvotes.desc().nullslast(),
            "volume": Thread.max_volume.desc().nullslast(),
        }.get(sort, Thread.posted_at.desc().nullslast())
        rows = s.execute(stmt.order_by(order).limit(limit).offset(offset)).scalars().all()
        ids = [r.id for r in rows]
        hits: dict[str, list] = {i: [] for i in ids}
        if ids:
            for h in s.execute(select(Hit).where(Hit.thread_id.in_(ids))).scalars().all():
                hits[h.thread_id].append({"keyword": h.keyword, "country": h.country,
                                          "source": h.source, "position": h.serp_position,
                                          "volume": h.search_volume})
        for v in hits.values():
            v.sort(key=lambda d: (d["position"] or 99, -(d["volume"] or 0)))
        return {"cards": [r.to_dict(hits.get(r.id, [])) for r in rows]}


def stats() -> dict:
    with SessionLocal() as s:
        def count(*where):
            st = select(func.count(Thread.id))
            for w in where:
                st = st.where(w)
            return s.execute(st).scalar() or 0

        subs = s.execute(
            select(Thread.subreddit, func.count(Thread.id))
            .where(Thread.subreddit != "")
            .group_by(Thread.subreddit).order_by(func.count(Thread.id).desc())).all()
        last = s.execute(select(Scan).order_by(Scan.started_at.desc()).limit(1)).scalars().first()
        return {
            "total": count(), "new": count(Thread.is_new.is_(True)),
            "unread": count(Thread.is_read.is_(False)),
            "saved": count(Thread.saved_ref.isnot(None)),
            "by_source": {src: count(Thread.sources.any(src)) for src in ("brand", "keywords")},
            "subreddits": [{"name": a, "count": b} for a, b in subs],
            "last_scan": last.to_dict() if last else None,
            "config_missing": config.missing_required(),
            "notes_enabled": config.notes_enabled,
            "votes_enabled": config.votes_enabled,
        }


def scan_history(limit: int = 12) -> list[dict]:
    with SessionLocal() as s:
        return [r.to_dict() for r in s.execute(
            select(Scan).order_by(Scan.started_at.desc()).limit(limit)).scalars().all()]
