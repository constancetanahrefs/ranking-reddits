"""Reddit enrichment — two independent halves.

Reddit answers 403 to server-side .json / HTML requests from datacenter IPs, but
the Atom feed answers 200. So:

  half 1 (free, ~2s)  — .rss feed: title, OP, real date, body, top comments
  half 2 (optional)   — rendered page: upvotes, comment count, upvote ratio

See docs/REDDIT_ACCESS.md for the full test matrix and rationale.

THE RULE: a render that comes back without vote data raises. It never stores 0.
"""
from __future__ import annotations

import html as _html
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from .config import config

_COMMENTS_RE = re.compile(r"/comments/([a-z0-9]+)", re.I)
_SUB_RE = re.compile(r"reddit\.com/r/([A-Za-z0-9_]+)")

_SCORE_RE = re.compile(r'"score"\s*:\s*(\d+)')
_NCOM_RE = re.compile(r'"number_comments"\s*:\s*(\d+)')
_RATIO_RE = re.compile(r'"upvote_ratio"\s*:\s*([0-9.]+)')
_CREATED_RE = re.compile(r'"created_timestamp"\s*:\s*(\d+)')


class RedditError(RuntimeError):
    pass


# ── URL handling ──────────────────────────────────────────────────────────

def url_key(url: str) -> Optional[str]:
    """Canonical dedupe key: '<subreddit>/<thread_id>', or 'r/<sub>' for a landing page.

    Dedupe on THIS, never the raw URL — trailing slashes, utm_* params, www and
    comment permalinks otherwise turn one thread into four cards.
    Non-reddit URLs return None and are skipped.
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


def subreddit_of(url: str) -> str:
    """Subreddit name without the r/ prefix, or '' if the URL has none."""
    m = _SUB_RE.search(url or "")
    return m.group(1) if m else ""


def is_landing_page(url: str) -> bool:
    """True for reddit.com/r/<sub>/ — a subreddit, not a thread."""
    return (url_key(url) or "").startswith("r/")


def clean_title(t: str) -> str:
    """SERP titles carry a ' : r/SEO - Reddit' tail. Strip it."""
    t = (t or "").strip()
    t = re.sub(r"\s*[:\-|]\s*r/[A-Za-z0-9_]+\s*(-\s*Reddit)?\s*$", "", t)
    t = re.sub(r"\s*-\s*Reddit\s*$", "", t)
    return t.strip()


def _parse_dt(v) -> Optional[datetime]:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


# ── Half 1: the Atom feed (free) ──────────────────────────────────────────

def fetch_text(url: str) -> dict:
    """Title, OP, publish date, body and top comments from <thread>/.rss.

    429s on rapid repeats — hence the backoff. Space batch jobs out too.
    """
    feed = re.sub(r"/?(\?.*)?$", "", url.split("#")[0]).rstrip("/") + "/.rss"
    raw, last = "", ""
    for attempt in range(3):
        try:
            req = urllib.request.Request(feed, headers={
                "User-Agent": config.reddit_user_agent,
                "Accept": "application/atom+xml,application/xml",
            })
            with urllib.request.urlopen(req, timeout=25) as r:
                raw = r.read().decode("utf-8", "replace")
            break
        except Exception as e:  # noqa: BLE001
            last = str(e)[:140]
            time.sleep(3 * (attempt + 1))
    if not raw:
        raise RedditError(f"the Atom feed did not answer ({last})")

    entries = re.findall(r"<entry>(.*?)</entry>", raw, re.S)
    if not entries:
        raise RedditError("the Atom feed carried no entries")

    def field(block: str, tag: str) -> str:
        m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.S)
        return _html.unescape(m.group(1)).strip() if m else ""

    def text_of(block: str) -> str:
        body = field(block, "content")
        body = re.sub(r"<br\s*/?>|</p>", "\n", body)
        body = re.sub(r"<[^>]+>", " ", body)
        body = _html.unescape(body)
        # every entry ends with reddit's own "submitted by /u/x [link] [comments]"
        body = re.sub(r"\s*submitted by\s*/u/\S+\s*\[link\]\s*\[comments\]\s*$", "", body)
        return "\n".join(ln.strip() for ln in body.split("\n") if ln.strip())

    post, comments = entries[0], entries[1:1 + config.max_comments]
    parts, kept = [], 0
    body = text_of(post)
    if body:
        parts.append(body)
    lines = []
    for c in comments:
        t = text_of(c)
        who = field(c, "name").lstrip("/").removeprefix("u/") or "?"
        if t and t != "[deleted]":
            lines.append(f"u/{who}: {t}")
            kept += 1
    if lines:
        parts.append(f"\n--- Top {kept} comments ---")
        parts += lines

    return {
        "title": clean_title(field(post, "title")),
        "author": field(post, "name").lstrip("/").removeprefix("u/"),
        "posted_at": _parse_dt(field(post, "published")),
        "body_md": "\n\n".join(parts)[:40000],
        "comments_fetched": kept,
    }


# ── Half 2: vote counts from a rendered page (optional) ───────────────────

def fetch_votes(url: str) -> dict:
    """Upvotes / comment count / ratio, read from the page's embedded JSON.

    Requires a managed browser + residential proxy; Apify is used here but any
    equivalent service works. Raises if the page carried no vote data (Reddit's
    login wall) — the caller must NOT treat that as zero.
    """
    if not config.apify_api_token:
        raise RedditError("no APIFY_API_TOKEN set — vote counts are disabled")

    endpoint = (f"https://api.apify.com/v2/acts/{config.apify_render_actor}"
                f"/run-sync-get-dataset-items?token={config.apify_api_token}"
                f"&timeout=280&limit=1")
    payload = json.dumps({
        "startUrls": [{"url": url}],
        "maxCrawlPages": 1,
        "crawlerType": "playwright:firefox",
        "saveHtml": True,
        "maxRequestRetries": 2,
        "readableTextCharThreshold": 50,
        "proxyConfiguration": {"useApifyProxy": True,
                               "apifyProxyGroups": ["RESIDENTIAL"]},
    }).encode()
    req = urllib.request.Request(endpoint, data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        items = json.loads(r.read().decode("utf-8", "replace"))
    if not items:
        raise RedditError("the render returned no page (Reddit likely blocked it)")

    it = items[0]
    raw = _html.unescape(it.get("html") or "")

    def first(rx, cast=int):
        m = rx.search(raw)
        return cast(m.group(1)) if m else None

    score = first(_SCORE_RE)
    if score is None:
        # A login wall renders fine but has no vote data. That is a FAILURE.
        raise RedditError("rendered page carried no vote data (login wall?)")
    created = first(_CREATED_RE)
    return {
        "upvotes": score,
        "num_comments": first(_NCOM_RE),
        "upvote_ratio": first(_RATIO_RE, float),
        "posted_at": (datetime.fromtimestamp(created / 1000, tz=timezone.utc)
                      if created else None),
        "title": clean_title(((it.get("metadata") or {}).get("title") or "")),
    }


# ── Both halves ───────────────────────────────────────────────────────────

def enrich(url: str) -> dict:
    """Run both halves. Either can fail without losing the other.

    Returns the merged data plus `partial_error` describing what didn't work, so
    the UI can be honest about which numbers are missing.
    """
    if is_landing_page(url):
        return {"body_md": "", "partial_error":
                "This is a subreddit landing page, not a single thread, so there is "
                "no post body or upvote count to fetch."}

    data: dict = {}
    errors: list[str] = []
    halves = [(fetch_text, "thread text", 2)]
    if config.apify_api_token:
        # the residential proxy hits Reddit's login wall ~half the time
        halves.append((fetch_votes, "vote counts", 3))

    for fn, label, tries in halves:
        last = ""
        for attempt in range(tries):
            try:
                for k, v in (fn(url) or {}).items():
                    if data.get(k) in (None, "", 0) and v not in (None, ""):
                        data[k] = v
                last = ""
                break
            except Exception as e:  # noqa: BLE001
                last = f"{label}: {str(e)[:180]}"
                if attempt + 1 < tries:
                    time.sleep(2)
        if last:
            errors.append(last)

    if not data:
        raise RedditError("Could not read the thread. " + " | ".join(errors))
    data["partial_error"] = " | ".join(errors)
    return data
