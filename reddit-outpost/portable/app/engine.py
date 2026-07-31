"""Reddit Outpost — profiles, scan pipeline, scoring, drafting, discovery.

Design notes worth keeping in view:

* **It never posts to Reddit.** There is no write path. Every reply is copied by
  a human and pasted by a human, because Reddit shadowbans automated promotion.
* **Scanning is serial and patient**, because the RSS rate limit is global to our
  IP (see `_reddit_outpost_reddit`). Parallel fetching loses posts.
* **Unknown is never zero.** Upvotes stay NULL unless actually fetched.
* **A scan that reaches zero subreddits is a FAILURE**, not a quiet day.
"""
from __future__ import annotations

import json
import os
import re
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import delete, func, or_, select

from app import reddit as R
from app.db import session_scope as cross_session_scope
from app.llm import chat_json
from app.models import (OutpostAction, OutpostBlocked, OutpostDraft,
                        OutpostNotify, OutpostPost, OutpostProfile, OutpostRun,
                        OutpostSubreddit)

SCORE_BATCH = 20          # posts per scoring call
DISCOVER_WORKERS = 4      # LLM audit fan-out (Reddit fetches stay serial)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Seed content — the example profile created on a fresh install.
#
# THIS IS A PLACEHOLDER. Replace it with your own product, or just edit the
# profile in the UI after first run (Edit → fill in the brief → ✨ Generate
# topics from the brief). Everything below is illustrative only.
#
# The brief is the single most important string in the app: it grounds every
# relevance score and every drafted reply. A vague or stale brief quietly
# degrades everything downstream.
# ---------------------------------------------------------------------------

SEED_PRODUCT = os.environ.get("OUTPOST_SEED_PRODUCT", "Example Product")
SEED_URL = os.environ.get("OUTPOST_SEED_URL", "https://example.com")

SEED_BRIEF = os.environ.get("OUTPOST_SEED_BRIEF", "").strip() or (
    "REPLACE ME. Describe your product in 4-8 sentences: what it does, who it is "
    "for, the concrete problems it solves, what makes it different from the "
    "obvious alternatives, and roughly what it costs. Be specific — the model uses "
    "this to judge whether a Reddit thread is genuinely a fit, and vague briefs "
    "produce both false positives and useless reply drafts."
)

# Topics do double duty: they are the themes posts get scored against, AND each
# carries the capability line the reply drafter leads with. Leave this empty and
# the app generates topics from your brief on first save — which is almost always
# better than hand-writing them, and avoids the classic mistake of inheriting
# another product's pitch lines.
SEED_TOPICS: list[dict] = []

# A post matching this regex is forced to maximum relevance regardless of what
# the scorer thought — you never want to miss someone naming you directly.
#
# Choose a term nobody else uses. A generic product name floods the feed: in the
# original build, matching a two-word product name caught every unrelated thread
# that used those words as placeholders, so it was narrowed to the company name
# only. Empty disables the brand floor entirely.
SEED_BRAND_RE = os.environ.get("OUTPOST_SEED_BRAND_REGEX", "")

SEED_AUDIENCE = os.environ.get("OUTPOST_SEED_AUDIENCE", "").strip() or (
    "REPLACE ME. Who has to be in a subreddit for it to be worth monitoring? "
    "Job titles, company stage, what they are trying to do."
)

# Starting subreddits. These are marketing/SaaS-flavoured because that was the
# original use case — swap them for wherever YOUR buyers actually are, or use the
# Discover tab, which proposes and scores candidates for you.
DEFAULT_SUBS = [
    "SEO", "bigseo", "juststart", "content_marketing",
    "ArtificialInteligence", "OpenAI", "ChatGPTPro", "AI_Agents", "LangChain",
    "marketing", "DigitalMarketing", "AskMarketing", "marketingautomation",
    "agency", "agencylife",
    "SaaS", "SideProject", "Entrepreneur", "indiehackers",
]

VARIANTS = [
    ("helpful", "ZERO product mention. Pure advice that answers the question. "
                "~80-140 words."),
    ("soft", "Answer helpfully first, then ONE casual closing line mentioning the "
             "product with its link, in the spirit of 'fwiw I've been using X for "
             "this'. ~100-160 words."),
    ("pitch", "A direct recommendation of the product, still framed around the OP's "
              "actual problem, never spammy. ~100-160 words."),
]
VARIANT_KEYS = [v for v, _ in VARIANTS]


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
            print(f"[reddit_outpost:{kind}] {jid} failed:\n{traceback.format_exc()}")

    threading.Thread(target=_wrap, daemon=True).start()
    return jid


def _json_loads(raw: str):
    """Parse a model reply that may arrive fenced, or as a bare array.

    Returns whatever JSON type the model produced — callers must not assume a
    dict. Even with response_format=json_object, asking for {"items":[...]}
    sometimes yields the bare [...] instead, which used to crash the scan.
    """
    s = (raw or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"```\s*$", "", s).strip()
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        m = re.search(r"[\{\[].*[\}\]]", s, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


def _items_of(data, *keys: str) -> list:
    """Pull the list out of a model reply that may be {"items":[…]} or just […]."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return v
        # single unnamed list value, e.g. {"results": [...]}
        lists = [v for v in data.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
    return []


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

def seed_default_profile() -> str | None:
    """Create the placeholder profile + its subreddits on an empty install."""
    with cross_session_scope() as s:
        if s.execute(select(func.count(OutpostProfile.id))).scalar() or 0:
            return None
        p = OutpostProfile(
            name=SEED_PRODUCT, product_name=SEED_PRODUCT, product_url=SEED_URL,
            brief=SEED_BRIEF, brief_updated_at=_now(), topics=SEED_TOPICS,
            brand_regex=SEED_BRAND_RE, audience=SEED_AUDIENCE,
            is_default=True, setup_complete=True,
        )
        s.add(p)
        s.flush()
        for name in DEFAULT_SUBS:
            s.add(OutpostSubreddit(profile_id=p.id, name=name, added_from="seed"))
        return p.id


def suggest_topics(product_name: str, brief: str, audience: str = "",
                   n: int = 8) -> list[dict]:
    """Generate topic filters WITH pitch lines for a specific product.

    Topics are product-specific: they are both the scoring targets and the
    capability line the drafter pitches. Copying another product's topics is
    actively harmful — a HubSpot profile inheriting "AI marketing agent on
    Ahrefs data" produces drafts that pitch the wrong product entirely.
    """
    sysmsg = (
        "You define topic filters for a Reddit listening tool.\n\n"
        "Each topic is a theme where this product is genuinely the right answer. "
        f"Produce {n} distinct topics covering different buying situations.\n"
        "For each: a snake_case id, a short human label, one relevant emoji, and a "
        "'pitch' — a single clause (no sentence, no product name) naming the concrete "
        "capability to lead with for that theme. The pitch is dropped into a Reddit "
        "reply after the product name, so it must read naturally there and must "
        "describe THIS product only.\n"
        'Return JSON: {"topics":[{"id":"...","label":"...","emoji":"...",'
        '"pitch":"..."}]}'
    )
    ctx = {"product": product_name, "brief": brief, "target_audience": audience}
    r = chat_json([{"role": "system", "content": sysmsg},
                  {"role": "user", "content": json.dumps(ctx, ensure_ascii=False)}], temperature=0.4, max_tokens=1600)
    out, seen = [], set()
    for t in _items_of(_json_loads(r),
                       "topics", "items"):
        if not isinstance(t, dict):
            continue
        tid = re.sub(r"[^a-z0-9_]", "", (t.get("id") or "").lower().replace(" ", "_"))[:40]
        if not tid or tid in seen:
            continue
        seen.add(tid)
        out.append({"id": tid, "label": (t.get("label") or tid)[:80],
                    "emoji": (t.get("emoji") or "•")[:4],
                    "pitch": (t.get("pitch") or "")[:300]})
    return out[:n]


def list_profiles() -> list[dict]:
    with cross_session_scope() as s:
        out = []
        for p in s.execute(select(OutpostProfile)
                           .order_by(OutpostProfile.is_default.desc(),
                                     OutpostProfile.name)).scalars().all():
            posts = s.execute(select(func.count(OutpostPost.id))
                              .where(OutpostPost.profile_id == p.id)).scalar() or 0
            matched = s.execute(
                select(func.count(OutpostPost.id))
                .where(OutpostPost.profile_id == p.id,
                       OutpostPost.matched.is_(True),
                       OutpostPost.status == "new")).scalar() or 0
            subs = s.execute(select(func.count(OutpostSubreddit.id))
                             .where(OutpostSubreddit.profile_id == p.id,
                                    OutpostSubreddit.enabled.is_(True))).scalar() or 0
            out.append(p.to_dict({"posts": posts, "matched": matched, "subs": subs}))
        return out


def get_profile(pid: str | None = None) -> dict | None:
    with cross_session_scope() as s:
        p = s.get(OutpostProfile, pid) if pid else None
        if p is None:
            p = s.execute(select(OutpostProfile)
                          .order_by(OutpostProfile.is_default.desc(),
                                    OutpostProfile.created_at)).scalars().first()
        return p.to_dict() if p else None


def create_profile(data: dict) -> dict:
    with cross_session_scope() as s:
        first = not (s.execute(select(func.count(OutpostProfile.id))).scalar() or 0)
        p = OutpostProfile(is_default=first, setup_complete=True)
        for k, v in data.items():
            if hasattr(p, k) and k not in ("id", "is_default", "created_at"):
                setattr(p, k, v)
        if data.get("brief"):
            p.brief_updated_at = _now()
        # No topics given? Derive them from THIS product's brief rather than
        # leaving the profile unscoreable or inheriting another product's pitches.
        if not (p.topics or []) and (p.brief or "").strip():
            try:
                p.topics = suggest_topics(p.product_name or p.name, p.brief,
                                          p.audience or "")
            except Exception as exc:  # noqa: BLE001
                print(f"[reddit_outpost] topic generation failed: {exc}")
        s.add(p)
        s.flush()
        for name in (data.get("subreddits") or []):
            n = R.normalise_name(name)
            if n:
                s.add(OutpostSubreddit(profile_id=p.id, name=n, added_from="manual"))
        return p.to_dict()


def update_profile(pid: str, patch: dict) -> dict:
    with cross_session_scope() as s:
        p = s.get(OutpostProfile, pid)
        if not p:
            raise RuntimeError("Profile not found.")
        for k, v in patch.items():
            if hasattr(p, k) and k not in ("id", "created_at"):
                setattr(p, k, v)
        if "brief" in patch:
            p.brief_updated_at = _now()
        return p.to_dict()


def count_stale_product_name(pid: str, old_name: str) -> dict:
    """How much stored text still names the OLD product?

    Scores, reasoning lines and drafts are PERSISTED, not recomputed — which is
    correct (you don't want yesterday's queue silently rewriting itself), but it
    means renaming a product leaves its old name scattered through text the model
    already wrote.
    """
    old = (old_name or "").strip()
    if not old:
        return {"posts": 0, "drafts": 0}
    # Whole-word, same rule the rewrite uses. A plain LIKE over-counts badly:
    # "multi-agent architecture" contains "agent a" but is not the product name,
    # so a substring count would offer to fix text that is already correct.
    like = f"%{old}%"
    pat = re.compile(rf"\b{re.escape(old)}\b", re.I)
    with cross_session_scope() as s:
        posts = sum(
            1 for (txt,) in s.execute(select(OutpostPost.reasoning).where(
                OutpostPost.profile_id == pid,
                OutpostPost.reasoning.ilike(like))).all()
            if pat.search(txt or ""))
        drafts = sum(
            1 for (txt,) in s.execute(
                select(OutpostDraft.body)
                .join(OutpostPost, OutpostPost.id == OutpostDraft.post_id)
                .where(OutpostPost.profile_id == pid,
                       OutpostDraft.body.ilike(like))).all()
            if pat.search(txt or ""))
    return {"posts": int(posts), "drafts": int(drafts)}


def rename_product_in_text(pid: str, old_name: str, new_name: str) -> dict:
    """Rewrite the old product name to the new one in stored reasoning + drafts.

    A plain whole-word, case-sensitive-ish substitution — deliberately NOT an LLM
    rewrite: re-running the scorer would change the scores themselves, and the
    user asked to fix a name, not to re-judge their queue.
    """
    old, new = (old_name or "").strip(), (new_name or "").strip()
    if not old or not new or old == new:
        return {"posts": 0, "drafts": 0}

    pat = re.compile(rf"\b{re.escape(old)}\b", re.I)
    n_posts = n_drafts = 0
    with cross_session_scope() as s:
        rows = s.execute(select(OutpostPost).where(
            OutpostPost.profile_id == pid,
            OutpostPost.reasoning.ilike(f"%{old}%"))).scalars().all()
        for r in rows:
            fixed = pat.sub(new, r.reasoning or "")
            if fixed != r.reasoning:
                r.reasoning = fixed
                n_posts += 1

        drafts = s.execute(
            select(OutpostDraft)
            .join(OutpostPost, OutpostPost.id == OutpostDraft.post_id)
            .where(OutpostPost.profile_id == pid,
                   OutpostDraft.body.ilike(f"%{old}%"))).scalars().all()
        for d in drafts:
            fixed = pat.sub(new, d.body or "")
            if fixed != d.body:
                d.body = fixed
                n_drafts += 1
    return {"posts": n_posts, "drafts": n_drafts}


def delete_profile(pid: str) -> int:
    with cross_session_scope() as s:
        p = s.get(OutpostProfile, pid)
        if not p:
            raise RuntimeError("Profile not found.")
        n = s.execute(select(func.count(OutpostPost.id))
                      .where(OutpostPost.profile_id == pid)).scalar() or 0
        was_default = p.is_default
        s.delete(p)
        s.flush()
        if was_default:
            nxt = s.execute(select(OutpostProfile)
                            .order_by(OutpostProfile.created_at)).scalars().first()
            if nxt:
                nxt.is_default = True
        return n


def missing_required(p: dict | None) -> list[str]:
    if not p:
        return ["No profile yet — add one to start listening."]
    out = []
    if not (p.get("product_name") or "").strip():
        out.append("Product name — what you're listening for")
    if not (p.get("brief") or "").strip():
        out.append("Product brief — grounds every relevance score and draft")
    if not (p.get("topics") or []):
        out.append("At least one topic filter")
    return out


# ---------------------------------------------------------------------------
# Subreddits
# ---------------------------------------------------------------------------

def list_subreddits(pid: str, only_enabled: bool = False) -> list[dict]:
    with cross_session_scope() as s:
        q = select(OutpostSubreddit).where(OutpostSubreddit.profile_id == pid)
        if only_enabled:
            q = q.where(OutpostSubreddit.enabled.is_(True))
        rows = s.execute(q.order_by(OutpostSubreddit.name)).scalars().all()
        return [r.to_dict() for r in rows]


def add_subreddit(pid: str, raw_name: str, *, added_from: str = "manual",
                  audit_now: bool = True) -> dict:
    name = R.normalise_name(raw_name)
    if not name:
        raise RuntimeError(f"'{raw_name}' is not a usable subreddit name.")
    with cross_session_scope() as s:
        exists = s.execute(select(OutpostSubreddit).where(
            OutpostSubreddit.profile_id == pid,
            func.lower(OutpostSubreddit.name) == name.lower())).scalars().first()
        if exists:
            return exists.to_dict()
        row = OutpostSubreddit(profile_id=pid, name=name, added_from=added_from)
        s.add(row)
        s.flush()
        sid = row.id
    if audit_now:
        try:
            audit_subreddit(sid)
        except Exception as exc:  # noqa: BLE001
            print(f"[reddit_outpost] audit on add failed for r/{name}: {exc}")
    with cross_session_scope() as s:
        return s.get(OutpostSubreddit, sid).to_dict()


def set_subreddit(sid: str, *, enabled: bool | None = None) -> dict:
    with cross_session_scope() as s:
        row = s.get(OutpostSubreddit, sid)
        if not row:
            raise RuntimeError("Subreddit not found.")
        if enabled is not None:
            row.enabled = enabled
        return row.to_dict()


def delete_subreddit(sid: str) -> None:
    with cross_session_scope() as s:
        row = s.get(OutpostSubreddit, sid)
        if row:
            s.delete(row)


# ---------------------------------------------------------------------------
# Subreddit audit — promo friendliness (rules) + topical fit (LLM)
# ---------------------------------------------------------------------------

_HOSTILE = [
    "no self-promo", "no self promotion", "no promotion", "no advertising",
    "no ads", "no shilling", "no plugs", "no marketing",
    "will be banned", "instant ban", "permanent ban",
    "no ai", "no ai-generated", "no chatgpt", "no llm",
    "9:1 rule", "9-1 rule", "90/10", "reddiquette",
]
_FRIENDLY = [
    "share", "showcase", "feedback", "discussion", "recommend",
    "introduce yourself", "tools", "resources", "build in public",
]


def _promo_friendly(about: dict | None, titles: list[str]) -> tuple[float, list[str]]:
    """0-1: how welcome is a tool recommendation here? Rule-based, explainable.

    Deliberately not an LLM call — it must be cheap enough to re-run on every
    subreddit in a discovery sweep, and the reasons must be quotable in the UI.
    """
    score, reasons = 0.5, []
    text = " ".join([
        (about or {}).get("public_description") or "",
        (about or {}).get("rules_text") or "",
    ]).lower()

    if text:
        hostile = [p for p in _HOSTILE if p in text]
        if hostile:
            score -= min(0.35, 0.12 * len(hostile))
            reasons.append("rules/description mention: " + ", ".join(hostile[:3]))
        friendly = [p for p in _FRIENDLY if p in text]
        if friendly:
            score += min(0.2, 0.05 * len(friendly))
            reasons.append("welcoming language: " + ", ".join(friendly[:3]))
    else:
        reasons.append("no description available — scored on post mix alone")

    # Subscribers are unknowable over RSS. Only apply the size heuristic when we
    # genuinely have the number, rather than treating unknown as tiny.
    subs = (about or {}).get("subscribers")
    if subs:
        if 5_000 <= subs <= 500_000:
            score += 0.1
            reasons.append(f"{subs:,} members — active but not firehose-sized")
        elif subs < 1_000:
            score -= 0.1
            reasons.append(f"only {subs:,} members")
        elif subs > 2_000_000:
            score -= 0.05
            reasons.append(f"{subs:,} members — a reply gets buried fast")

    if titles:
        promoish = sum(1 for t in titles
                       if re.search(r"\b(i built|i made|launch|my (app|tool|saas)|"
                                    r"feedback on|check out|introducing)\b", t, re.I))
        share = promoish / len(titles)
        if share >= 0.3:
            score += 0.15
            reasons.append(f"{promoish}/{len(titles)} recent posts are self-promo — "
                           "tolerated here")
        elif share == 0:
            score -= 0.05
            reasons.append("no self-promo in recent posts")
    return max(0.0, min(1.0, round(score, 3))), reasons


def _topical_fit(profile: dict, name: str, about: dict | None,
                 titles: list[str]) -> tuple[float, str]:
    """0-1 LLM judgement: is this sub's audience the product's buyer?"""
    ctx = {
        "subreddit": name,
        "description": (about or {}).get("public_description") or "",
        "recent_titles": titles[:15],
        "product": profile.get("product_name"),
        "product_brief": profile.get("brief"),
        "target_audience": profile.get("audience"),
    }
    sysmsg = (
        "Rate how well a subreddit's audience matches a product's target buyer.\n"
        "  >=0.8  the core buyer (e.g. in-house marketers, agencies, indie SaaS "
        "founders for a marketing tool)\n"
        "  0.5-0.79 adjacent — entrepreneurship, general AI, tangential trades\n"
        "  <0.4  off target\n"
        "Judge the AUDIENCE, not whether they'd tolerate promotion. If the recent "
        "titles contradict the description, trust the titles. If you have almost no "
        "evidence, return a score near 0.3 and say so.\n"
        'Return JSON: {"fit": 0.0-1.0, "why": "<=25 words"}'
    )
    try:
        r = chat_json([{"role": "system", "content": sysmsg},
                      {"role": "user", "content": json.dumps(ctx, ensure_ascii=False)}], temperature=0.1, max_tokens=200)
        d = _json_loads(r)
        if isinstance(d, list):
            d = d[0] if d and isinstance(d[0], dict) else {}
        return max(0.0, min(1.0, float(d.get("fit") or 0.0))), (d.get("why") or "")[:240]
    except Exception as exc:  # noqa: BLE001
        return 0.0, f"scoring failed: {str(exc)[:120]}"


def audit_subreddit(sid: str) -> dict:
    """Fetch + score one subreddit. Records the failure rather than faking scores."""
    with cross_session_scope() as s:
        row = s.get(OutpostSubreddit, sid)
        if not row:
            raise RuntimeError("Subreddit not found.")
        name, pid = row.name, row.profile_id
    profile = get_profile(pid) or {}

    about = R.about(name)
    titles = R.sample_titles(name, 15) if about is not None else []

    if about is None and not titles:
        with cross_session_scope() as s:
            row = s.get(OutpostSubreddit, sid)
            row.audit_error = ("Could not reach r/%s — it may not exist, be private, "
                               "or we hit Reddit's rate limit. Scores left unset "
                               "rather than guessed." % name)
            row.last_audited_at = _now()
            return row.to_dict()

    promo, reasons = _promo_friendly(about, titles)
    fit, why = _topical_fit(profile, name, about, titles)

    with cross_session_scope() as s:
        row = s.get(OutpostSubreddit, sid)
        row.topical_fit = fit
        row.promo_friendly = promo
        row.subscribers = (about or {}).get("subscribers")
        row.public_description = ((about or {}).get("public_description") or "")[:1000]
        row.audit = {"promo_reasons": reasons, "fit_why": why,
                     "sampled_titles": titles[:8],
                     "source": (about or {}).get("_source") or "rss"}
        row.audit_error = ""
        row.last_audited_at = _now()
        return row.to_dict()


# ---------------------------------------------------------------------------
# Discovery — propose subreddits, then audit them
# ---------------------------------------------------------------------------

def _propose_subs(profile: dict, existing: list[str], n: int = 30) -> list[dict]:
    sysmsg = (
        "You suggest Reddit communities where a product's target buyers actually "
        "spend time.\n"
        f"Suggest up to {n} DIVERSE subreddits — vary the angle (practitioners, "
        "adjacent trades, tooling, business-stage communities). Real subreddits "
        "only; do not invent names. Exclude any already listed.\n"
        'Return JSON: {"subs":[{"name":"SEO","why":"<=15 words",'
        '"audience_tag":"seo|content|marketing|ai|agency|saas|indie_founder|other"}]}'
    )
    ctx = {"product": profile.get("product_name"), "brief": profile.get("brief"),
           "target_audience": profile.get("audience"), "already_monitoring": existing}
    r = chat_json([{"role": "system", "content": sysmsg},
                  {"role": "user", "content": json.dumps(ctx, ensure_ascii=False)}], temperature=0.7, max_tokens=2000)
    d = _json_loads(r)
    have = {e.lower() for e in existing}
    out, seen = [], set()
    for item in _items_of(d, "subs", "subreddits", "items"):
        if not isinstance(item, dict):
            continue
        nm = R.normalise_name(item.get("name") or "")
        if not nm or nm.lower() in have or nm.lower() in seen:
            continue
        seen.add(nm.lower())
        out.append({"name": nm, "why": (item.get("why") or "")[:200],
                    "audience_tag": (item.get("audience_tag") or "other")[:30]})
    return out


def run_discover(job_id: str, pid: str, n: int = 30) -> None:
    """Propose candidate subreddits and audit each one.

    Reddit fetches are serialised (rate limit); only the LLM scoring fans out.
    """
    profile = get_profile(pid)
    if not profile:
        raise RuntimeError("Profile not found.")
    existing = [s["name"] for s in list_subreddits(pid)]

    job_progress(job_id, "Asking the model for candidate communities…")
    cands = _propose_subs(profile, existing, n)
    if not cands:
        job_done(job_id, {"candidates": []}, "No new candidates suggested")
        return

    job_progress(job_id, f"{len(cands)} candidates — checking each on Reddit "
                         "(serial, to stay inside the rate limit)…")

    # Serial Reddit reads; the throttle is global to our IP.
    fetched: list[dict] = []
    for i, c in enumerate(cands, 1):
        job_progress(job_id, f"Checking r/{c['name']} ({i}/{len(cands)})…")
        about = R.about(c["name"])
        titles = R.sample_titles(c["name"], 12) if about is not None else []
        if about is None and not titles:
            c["unreachable"] = True
        c["_about"], c["_titles"] = about, titles
        fetched.append(c)

    job_progress(job_id, "Scoring audience fit…")
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=DISCOVER_WORKERS) as pool:
        futs = {}
        for c in fetched:
            if c.get("unreachable"):
                c.update(topical_fit=None, promo_friendly=None, combined=None,
                         fit_why="Could not reach this subreddit — it may not exist, "
                                 "be private, or we hit the rate limit.",
                         promo_reasons=[], subscribers=None, description="")
                results.append(c)
                continue
            futs[pool.submit(_topical_fit, profile, c["name"],
                             c["_about"], c["_titles"])] = c
        for fut in as_completed(futs):
            c = futs[fut]
            try:
                fit, why = fut.result()
            except Exception as exc:  # noqa: BLE001
                fit, why = 0.0, f"scoring failed: {str(exc)[:120]}"
            promo, reasons = _promo_friendly(c["_about"], c["_titles"])
            c.update(topical_fit=fit, promo_friendly=promo,
                     combined=round(0.7 * fit + 0.3 * promo, 3),
                     fit_why=why, promo_reasons=reasons,
                     subscribers=(c["_about"] or {}).get("subscribers"),
                     description=((c["_about"] or {}).get("public_description") or "")[:300])
            results.append(c)

    for c in results:
        c.pop("_about", None)
        c.pop("_titles", None)
    results.sort(key=lambda d: (d.get("combined") is None, -(d.get("combined") or 0)))
    reachable = [c for c in results if not c.get("unreachable")]
    job_done(job_id, {"candidates": results},
             f"{len(reachable)} of {len(results)} candidates checked")


# ---------------------------------------------------------------------------
# Relevance scoring
# ---------------------------------------------------------------------------

def _score_batch(profile: dict, posts: list[dict]) -> list[dict]:
    """Batch-score posts for whether a reply mentioning the product would land."""
    if not posts:
        return []
    topics = profile.get("topics") or []
    topic_doc = "\n".join(f"  - {t['id']}: {t.get('label') or t['id']}" for t in topics)
    topic_ids = {t["id"] for t in topics}
    items = [{"idx": i, "subreddit": p["subreddit"], "title": p["title"],
              "body": (p.get("selftext") or "")[:1200]} for i, p in enumerate(posts)]

    sysmsg = (
        f"You screen Reddit posts for places where a helpful reply mentioning "
        f"{profile.get('product_name')} would genuinely land.\n\nProduct brief:\n"
        f"{profile.get('brief')}\n\nMatch against these topics:\n{topic_doc}\n\n"
        "Strict rules:\n"
        "  - Score >=0.7 ONLY when (a) the OP is asking for help, advice or tool "
        "recommendations, AND (b) the product genuinely fits — not merely adjacent "
        "chatter.\n"
        "  - Score 0.4-0.69 for tangentially relevant posts where a soft mention "
        "could fit.\n"
        "  - Score <0.4 for off-topic, news without a question, hiring, memes, "
        "drama, other vendors' pitches, or an OP who already chose a tool.\n"
        "  - suggest_reply: true ONLY if relevance>=0.5 AND there is a clear "
        "question we would be welcome to answer.\n"
        "  - topics: 1-3 ids from the list, or [] if none fit.\n"
        "  - Be conservative. False positives waste the user's time.\n"
        'Return JSON: {"items":[{"idx":0,"relevance":0.0,"topics":[],'
        '"suggest_reply":false,"reason":"<=20 words"}]}'
    )
    try:
        r = chat_json([{"role": "system", "content": sysmsg},
                      {"role": "user", "content": json.dumps({"posts": items},
                                                             ensure_ascii=False)}], temperature=0.1, max_tokens=4000)
        data = _json_loads(r)
    except Exception as exc:  # noqa: BLE001
        print(f"[reddit_outpost] scoring failed: {exc}")
        return []

    out = []
    for it in _items_of(data, "items", "posts", "results"):
        if not isinstance(it, dict):
            continue
        try:
            idx = int(it.get("idx"))
            if not 0 <= idx < len(posts):
                continue
            out.append({
                "idx": idx,
                "relevance": max(0.0, min(1.0, float(it.get("relevance") or 0.0))),
                "topics": [t for t in (it.get("topics") or []) if t in topic_ids],
                "suggest_reply": bool(it.get("suggest_reply")),
                "reason": (it.get("reason") or "")[:240],
            })
        except Exception:  # noqa: BLE001
            continue
    return out


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------

def run_scan(job_id: str, pid: str | None = None, trigger: str = "manual") -> None:
    """Fetch /new from every enabled subreddit, dedupe, score, persist.

    Failure semantics: if NO subreddit could be fetched, the run is recorded as
    failed. A transport break must never look like a quiet day on Reddit.
    """
    profile = get_profile(pid)
    if not profile:
        raise RuntimeError("No profile configured.")
    pid = profile["id"]
    gaps = missing_required(profile)
    if gaps:
        raise RuntimeError("Profile incomplete: " + "; ".join(gaps))

    subs = list_subreddits(pid, only_enabled=True)
    if not subs:
        raise RuntimeError("No enabled subreddits — add some on the Subreddits tab.")

    with cross_session_scope() as s:
        run = OutpostRun(profile_id=pid, trigger=trigger, status="running")
        s.add(run)
        s.flush()
        run_id = run.id

    log: list[dict] = []
    cutoff = _now() - timedelta(hours=int(profile.get("lookback_hours") or 26))
    after_utc = cutoff.timestamp()
    per_sub = int(profile.get("max_posts_per_sub") or 30)

    try:
        with cross_session_scope() as s:
            blocked = {b for (b,) in s.execute(
                select(OutpostBlocked.reddit_id)
                .where(OutpostBlocked.profile_id == pid)).all()}
            known = {r for (r,) in s.execute(
                select(OutpostPost.reddit_id)
                .where(OutpostPost.profile_id == pid)).all()}

        # ---- fetch (serial + patient; see the rate-limit note in the reddit module)
        fresh: list[dict] = []
        ok_subs = 0
        for i, sub in enumerate(subs, 1):
            job_progress(job_id, f"Reading r/{sub['name']} ({i}/{len(subs)})…")
            posts, err = R.fetch_new(sub["name"], per_sub, after_utc)
            if err:
                log.append({"step": "fetch", "subreddit": sub["name"], "error": err})
                continue
            ok_subs += 1
            new_here = 0
            for p in posts:
                rid = p["reddit_id"]
                if rid in blocked or rid in known:
                    continue
                known.add(rid)
                fresh.append(p)
                new_here += 1
            log.append({"step": "fetch", "subreddit": sub["name"],
                        "seen": len(posts), "new": new_here})
            if i < len(subs):
                import time as _t
                _t.sleep(R.RSS_SPACING)

        if ok_subs == 0:
            raise RuntimeError(
                f"All {len(subs)} subreddits failed to fetch — treating this as a "
                "transport failure, not an empty scan. Reddit is most likely "
                "rate-limiting us; the next run should recover.")

        seen_total = sum(e.get("seen", 0) for e in log if e["step"] == "fetch")

        # ---- score
        matched_n = 0
        brand_re = None
        if (profile.get("brand_regex") or "").strip():
            try:
                brand_re = re.compile(profile["brand_regex"], re.I)
            except re.error as exc:
                log.append({"step": "score",
                            "error": f"brand_regex invalid, ignored: {exc}"})

        floor = float(profile.get("relevance_floor") or 0.5)
        for start in range(0, len(fresh), SCORE_BATCH):
            batch = fresh[start:start + SCORE_BATCH]
            job_progress(job_id, f"Scoring posts {start + 1}-{start + len(batch)} "
                                 f"of {len(fresh)}…")
            scored = {d["idx"]: d for d in _score_batch(profile, batch)}
            with cross_session_scope() as s:
                for i, p in enumerate(batch):
                    d = scored.get(i) or {}
                    rel = d.get("relevance")
                    reason = d.get("reason") or ""
                    topics = d.get("topics") or []
                    suggest = bool(d.get("suggest_reply"))

                    # Brand floor: an explicit brand mention always surfaces,
                    # whatever the model thought.
                    hit_brand = False
                    if brand_re:
                        blob = f"{p['title']}\n{p.get('selftext') or ''}"
                        if brand_re.search(blob):
                            hit_brand = True
                            rel = 1.0
                            suggest = True
                            reason = ("✨ names the brand directly — "
                                      + reason).strip()[:240]

                    is_match = bool(rel is not None and rel >= floor
                                    and (suggest or hit_brand))
                    if is_match:
                        matched_n += 1
                    s.add(OutpostPost(
                        profile_id=pid, reddit_id=p["reddit_id"],
                        subreddit=p["subreddit"], title=p["title"],
                        selftext=p.get("selftext") or "", author=p.get("author") or "",
                        url=p.get("url") or "", permalink=p.get("permalink") or "",
                        created_utc=p.get("created_utc"),
                        relevance=rel, topics=topics, matched=is_match,
                        brand_mention=hit_brand, reasoning=reason,
                        suggest_reply=suggest,
                    ))
            log.append({"step": "score", "batch": start // SCORE_BATCH + 1,
                        "posts": len(batch), "scored": len(scored)})

        with cross_session_scope() as s:
            run = s.get(OutpostRun, run_id)
            run.status = "completed"
            run.finished_at = _now()
            run.subs_scanned = ok_subs
            run.posts_seen = seen_total
            run.posts_new = len(fresh)
            run.posts_matched = matched_n
            run.log = log
        job_done(job_id, {"run_id": run_id, "subs_ok": ok_subs,
                          "posts_seen": seen_total, "posts_new": len(fresh),
                          "matched": matched_n},
                 f"{len(fresh)} new posts, {matched_n} worth a look")
    except Exception as exc:  # noqa: BLE001
        with cross_session_scope() as s:
            run = s.get(OutpostRun, run_id)
            if run:
                run.status = "failed"
                run.finished_at = _now()
                run.error = str(exc)[:1000]
                run.log = log
        raise


def scan_history(pid: str, limit: int = 12) -> list[dict]:
    with cross_session_scope() as s:
        rows = s.execute(select(OutpostRun).where(OutpostRun.profile_id == pid)
                         .order_by(OutpostRun.started_at.desc())
                         .limit(limit)).scalars().all()
        return [r.to_dict() for r in rows]


def recover_stale_runs() -> int:
    """Console restarts kill daemon threads; a 'running' row would hang forever."""
    try:
        with cross_session_scope() as s:
            rows = s.execute(select(OutpostRun)
                             .where(OutpostRun.status == "running")).scalars().all()
            for r in rows:
                r.status = "failed"
                r.finished_at = _now()
                r.error = ("Interrupted — the Console restarted mid-scan. "
                           "Nothing was lost; run it again.")
            return len(rows)
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
# Feed
# ---------------------------------------------------------------------------

def list_posts(*, pid: str, status: str = "new", window_days: int = 14,
               subreddit: str = "", topic: str = "", matched_only: bool = True,
               q: str = "", sort: str = "relevance",
               limit: int = 60, offset: int = 0) -> dict:
    with cross_session_scope() as s:
        cond = [OutpostPost.profile_id == pid]
        if status and status != "any":
            cond.append(OutpostPost.status == status)
        if window_days:
            cond.append(OutpostPost.fetched_at >= _now() - timedelta(days=window_days))
        if subreddit:
            cond.append(OutpostPost.subreddit == subreddit)
        if matched_only:
            cond.append(OutpostPost.matched.is_(True))
        if q:
            like = f"%{q.lower()}%"
            cond.append(or_(func.lower(OutpostPost.title).like(like),
                            func.lower(OutpostPost.selftext).like(like)))

        order = {
            "relevance": (OutpostPost.relevance.desc().nullslast(),
                          OutpostPost.created_utc.desc().nullslast()),
            "newest": (OutpostPost.created_utc.desc().nullslast(),),
            "oldest": (OutpostPost.created_utc.asc().nullsfirst(),),
        }.get(sort, (OutpostPost.relevance.desc().nullslast(),))

        rows = s.execute(select(OutpostPost).where(*cond).order_by(*order)
                         .limit(min(int(limit), 200)).offset(max(int(offset), 0))
                         ).scalars().all()
        total = s.execute(select(func.count(OutpostPost.id))
                          .where(*cond)).scalar() or 0

        # Topic filtering is done in Python: `topics` is JSON and the operators
        # differ across backends. The page size is small, so this is cheap.
        out = []
        for r in rows:
            if topic and topic not in (r.topics or []):
                continue
            drafts = s.execute(select(OutpostDraft)
                               .where(OutpostDraft.post_id == r.id)
                               .order_by(OutpostDraft.created_at)).scalars().all()
            out.append(r.to_dict(drafts))
        return {"posts": out, "total": total}


def stats(pid: str | None = None) -> dict:
    profile = get_profile(pid)
    profiles = list_profiles()
    if not profile:
        return {"profile": None, "profiles": profiles, "total": 0, "matched": 0,
                "posted": 0, "subs": 0, "config_missing": missing_required(None),
                "last_run": None}
    pid = profile["id"]
    with cross_session_scope() as s:
        total = s.execute(select(func.count(OutpostPost.id))
                          .where(OutpostPost.profile_id == pid)).scalar() or 0
        matched = s.execute(select(func.count(OutpostPost.id)).where(
            OutpostPost.profile_id == pid, OutpostPost.matched.is_(True),
            OutpostPost.status == "new")).scalar() or 0
        posted = s.execute(select(func.count(OutpostPost.id)).where(
            OutpostPost.profile_id == pid, OutpostPost.status == "done")).scalar() or 0
        subs = s.execute(select(func.count(OutpostSubreddit.id)).where(
            OutpostSubreddit.profile_id == pid,
            OutpostSubreddit.enabled.is_(True))).scalar() or 0
        last = s.execute(select(OutpostRun).where(OutpostRun.profile_id == pid)
                         .order_by(OutpostRun.started_at.desc())).scalars().first()
    return {"profile": profile, "profiles": profiles, "total": total,
            "matched": matched, "posted": posted, "subs": subs,
            "config_missing": missing_required(profile),
            "last_run": last.to_dict() if last else None}


def set_post_status(post_id: str, status: str) -> dict:
    with cross_session_scope() as s:
        row = s.get(OutpostPost, post_id)
        if not row:
            raise RuntimeError("Post not found.")
        row.status = status
        return row.to_dict()


def block_posts(pid: str, post_ids: list[str], reason: str) -> int:
    """Delete posts AND blocklist them so a later scan can't resurrect them."""
    n = 0
    with cross_session_scope() as s:
        for pidx in post_ids[:500]:
            row = s.get(OutpostPost, pidx)
            if not row or row.profile_id != pid:
                continue
            exists = s.execute(select(OutpostBlocked).where(
                OutpostBlocked.profile_id == pid,
                OutpostBlocked.reddit_id == row.reddit_id)).scalars().first()
            if not exists:
                s.add(OutpostBlocked(profile_id=pid, reddit_id=row.reddit_id,
                                     title=row.title, permalink=row.permalink,
                                     reason=reason[:400]))
            s.delete(row)
            n += 1
    return n


def sweep_preview(pid: str, max_relevance: float, older_than_days: int) -> int:
    with cross_session_scope() as s:
        return s.execute(select(func.count(OutpostPost.id)).where(
            OutpostPost.profile_id == pid,
            OutpostPost.status != "done",
            OutpostPost.brand_mention.is_(False),
            or_(OutpostPost.relevance <= max_relevance,
                OutpostPost.relevance.is_(None)),
            OutpostPost.fetched_at < _now() - timedelta(days=older_than_days),
        )).scalar() or 0


def sweep_ids(pid: str, max_relevance: float, older_than_days: int) -> list[str]:
    with cross_session_scope() as s:
        return [r for (r,) in s.execute(select(OutpostPost.id).where(
            OutpostPost.profile_id == pid,
            OutpostPost.status != "done",
            OutpostPost.brand_mention.is_(False),
            or_(OutpostPost.relevance <= max_relevance,
                OutpostPost.relevance.is_(None)),
            OutpostPost.fetched_at < _now() - timedelta(days=older_than_days),
        ).limit(500)).all()]


def retention_sweep(pid: str) -> int:
    """Age out old posts. Keeps posted threads and brand mentions forever."""
    profile = get_profile(pid)
    if not profile:
        return 0
    days = int(profile.get("retention_days") or 14)
    ids = []
    with cross_session_scope() as s:
        ids = [r for (r,) in s.execute(select(OutpostPost.id).where(
            OutpostPost.profile_id == pid,
            OutpostPost.status != "done",
            OutpostPost.brand_mention.is_(False),
            OutpostPost.fetched_at < _now() - timedelta(days=days),
        )).all()]
    return block_posts(pid, ids, f"auto-swept: older than {days} days") if ids else 0


# ---------------------------------------------------------------------------
# Reply drafting — generated on request, never posted
# ---------------------------------------------------------------------------

def _draft_prompt(profile: dict, post: dict, topics: list[str],
                  spec: str, refine: str = "", previous: str = "") -> list[dict]:
    tmap = {t["id"]: t for t in (profile.get("topics") or [])}
    pitches = [tmap[t]["pitch"] for t in topics if t in tmap and tmap[t].get("pitch")]
    sysmsg = (
        "You write Reddit replies for a marketer who will read, edit and post them "
        "BY HAND. Never claim to be the OP's peer if you aren't; never fabricate "
        "results, metrics or personal anecdotes.\n\n"
        f"Product: {profile.get('product_name')} — {profile.get('product_url')}\n"
        f"Brief: {profile.get('brief')}\n"
        + (f"Angle for this thread: {'; '.join(pitches)}\n" if pitches else "")
        + f"\nThis variant: {spec}\n\n"
        "Rules:\n"
        "  - Answer the OP's ACTUAL question first. Reddit punishes drive-by promo.\n"
        "  - Plain conversational Reddit prose. No headers, no bullet lists unless "
        "the question really calls for one, no emoji, no marketing voice.\n"
        "  - Mention only this one product. Never pitch sibling products.\n"
        "  - If the thread doesn't warrant a product mention, say less rather than "
        "forcing it.\n"
        'Return JSON: {"body": "the reply text"}'
    )
    ctx = {"subreddit": post.get("subreddit"), "title": post.get("title"),
           "body": (post.get("selftext") or "")[:4000]}
    if refine:
        ctx["previous_draft"] = previous
        ctx["revision_request"] = refine
        sysmsg += ("\n\nYou are REVISING your previous draft. Apply the revision "
                   "request and change nothing else.")
    return [{"role": "system", "content": sysmsg},
            {"role": "user", "content": json.dumps(ctx, ensure_ascii=False)}]


def generate_drafts(job_id: str, post_id: str) -> None:
    with cross_session_scope() as s:
        row = s.get(OutpostPost, post_id)
        if not row:
            raise RuntimeError("Post not found.")
        row.drafts_status = "running"
        row.drafts_error = ""
        post, pid, topics = row.to_dict(), row.profile_id, list(row.topics or [])
    profile = get_profile(pid) or {}

    try:
        made = []
        for i, (variant, spec) in enumerate(VARIANTS, 1):
            job_progress(job_id, f"Writing the {variant} variant ({i}/{len(VARIANTS)})…")
            raw = chat_json(_draft_prompt(profile, post, topics, spec),
                            temperature=0.7, max_tokens=900)
            d = _json_loads(raw)
            if isinstance(d, list):
                d = d[0] if d and isinstance(d[0], dict) else {}
            body = (d.get("body") or "").strip()
            made.append((variant, body))

        with cross_session_scope() as s:
            s.execute(delete(OutpostDraft).where(OutpostDraft.post_id == post_id))
            for variant, body in made:
                s.add(OutpostDraft(post_id=post_id, variant=variant, body=body))
            row = s.get(OutpostPost, post_id)
            row.drafts_status = "done"
        job_done(job_id, {"post_id": post_id}, "Three variants ready")
    except Exception as exc:  # noqa: BLE001
        with cross_session_scope() as s:
            row = s.get(OutpostPost, post_id)
            if row:
                row.drafts_status = "failed"
                row.drafts_error = str(exc)[:600]
        raise


def refine_draft(job_id: str, draft_id: str, note: str) -> None:
    """Regenerate ONE variant from free-text steering, leaving the others alone."""
    with cross_session_scope() as s:
        d = s.get(OutpostDraft, draft_id)
        if not d:
            raise RuntimeError("Draft not found.")
        variant, previous, post_id = d.variant, d.body, d.post_id
        row = s.get(OutpostPost, post_id)
        post, pid, topics = row.to_dict(), row.profile_id, list(row.topics or [])
    profile = get_profile(pid) or {}
    spec = dict(VARIANTS).get(variant, "")

    job_progress(job_id, f"Rewriting the {variant} variant…")
    raw = chat_json(_draft_prompt(profile, post, topics, spec, refine=note,
                                  previous=previous),
                    temperature=0.7, max_tokens=900)
    _d = _json_loads(raw)
    if isinstance(_d, list):
        _d = _d[0] if _d and isinstance(_d[0], dict) else {}
    body = (_d.get("body") or "").strip()
    with cross_session_scope() as s:
        d = s.get(OutpostDraft, draft_id)
        d.body = body
        d.refine_note = note[:500]
    job_done(job_id, {"draft_id": draft_id}, "Rewritten")


def log_action(pid: str, post_id: str, *, draft_id: str = "", comment_url: str = "",
               body: str = "", actor: str = "") -> dict:
    """Record that a human posted a reply. This is the audit trail, not a post action."""
    with cross_session_scope() as s:
        row = s.get(OutpostPost, post_id)
        if not row:
            raise RuntimeError("Post not found.")
        row.status = "done"
        a = OutpostAction(profile_id=pid, post_id=post_id, draft_id=draft_id or None,
                          action="posted", comment_url=comment_url[:2000],
                          body=body[:8000], actor=actor[:200])
        s.add(a)
        s.flush()
        return a.to_dict()


def list_actions(pid: str, limit: int = 50) -> list[dict]:
    with cross_session_scope() as s:
        rows = s.execute(select(OutpostAction)
                         .where(OutpostAction.profile_id == pid)
                         .order_by(OutpostAction.created_at.desc())
                         .limit(limit)).scalars().all()
        return [r.to_dict() for r in rows]


# ---------------------------------------------------------------------------
# Notifications — email, addressed from the authenticated session
# ---------------------------------------------------------------------------

def get_notify(user_id: str, session_email: str = "") -> dict:
    """Read prefs, refreshing the remembered session address if we have one.

    The daily digest runs without a request context and therefore cannot read
    X-Auth-User-Email — so every visit quietly re-records it here, keeping the
    cron's recipient correct even if the user's address changes.
    """
    with cross_session_scope() as s:
        row = s.get(OutpostNotify, user_id)
        if not row:
            return {"user_id": user_id, "enabled": False, "session_email": session_email,
                    "email_override": "", "matched_only": True,
                    "last_notified_at": None, "notify_count": 0, "last_error": ""}
        if session_email and row.session_email != session_email:
            row.session_email = session_email
        return row.to_dict()


def save_notify(user_id: str, patch: dict) -> dict:
    with cross_session_scope() as s:
        row = s.get(OutpostNotify, user_id)
        if not row:
            row = OutpostNotify(user_id=user_id)
            s.add(row)
        for k in ("enabled", "email_override", "matched_only", "session_email"):
            if k in patch and patch[k] is not None:
                setattr(row, k, patch[k])
        row.updated_at = _now()
        s.flush()
        return row.to_dict()


def digest_items(pid: str, since_hours: int = 26, matched_only: bool = True,
                 limit: int = 25) -> list[dict]:
    with cross_session_scope() as s:
        cond = [OutpostPost.profile_id == pid,
                OutpostPost.fetched_at >= _now() - timedelta(hours=since_hours),
                OutpostPost.status == "new"]
        if matched_only:
            cond.append(OutpostPost.matched.is_(True))
        rows = s.execute(select(OutpostPost).where(*cond)
                         .order_by(OutpostPost.relevance.desc().nullslast())
                         .limit(limit)).scalars().all()
        return [r.to_dict() for r in rows]
