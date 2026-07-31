"""Reddit access for the Outpost.

Reddit's 2023 clamp-down means anonymous `.json` returns 403 from datacenter
IPs (verified again 2026-07-31 from this sandbox). The Atom feeds still answer
200 — but only with a browser User-Agent, and they intermittently return an
EMPTY 200 body under load, which is why every fetch retries with backoff.

What each source can and cannot give us:

  new.rss        post id, title, author, published, body HTML.  NO vote counts.
  about.json     403. Dead. Kept only to document why we don't try.
  <sub>/new.rss  <subtitle> carries the public description — the only
                 machine-readable sub description available without OAuth.

Vote counts therefore need a rendered page, which is what the Ranking Reddits
engine already does via Apify. We reuse that rather than displaying a hard 0
for every post the way the source app did.

Nothing here ever writes to Reddit. There is no code path that posts.
"""
from __future__ import annotations

import html as _html
import re
import time
from datetime import datetime, timezone

import requests

# A browser UA is mandatory — the feeds 403 a generic one.
RSS_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")

# Rate limiting, measured from this sandbox on 2026-07-31.
#
# The 429 budget is GLOBAL to our egress IP and cumulative — not per-subreddit.
# Measured: 8 parallel threads (what the source app used) fails almost
# entirely; serial at 30s spacing still 429s ~50% of the time. What DOES work
# is patient retry: 4 attempts, 20s apart, got 7/8 subreddits in 26 requests.
#
# So the scan is deliberately SERIAL and slow. A daily scan has all the time in
# the world; burning the budget in parallel just to finish 3 minutes sooner
# means half the subreddits silently return nothing.
RSS_ATTEMPTS = 4
RSS_BASE_DELAY = 20.0   # flat, not exponential — the budget refills on wall time
RSS_SPACING = 8.0       # courtesy gap between two different subreddits

_ENTRY_RE = re.compile(r"<entry>(.*?)</entry>", re.S)
_ID_RE = re.compile(r"<id>t3_([a-z0-9]+)</id>")
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
_AUTHOR_RE = re.compile(r"<author>\s*<name>/u/([^<]+)</name>")
_LINK_RE = re.compile(r'<link href="([^"]+)"\s*/>')
_PUBLISHED_RE = re.compile(r"<published>([^<]+)</published>")
_CONTENT_RE = re.compile(r'<content type="html">(.*?)</content>', re.S)
_SUBTITLE_RE = re.compile(r"<subtitle>(.*?)</subtitle>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(s: str) -> str:
    """Feed bodies are escaped HTML; we want readable text for the LLM."""
    s = _html.unescape(s or "")
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</p>", "\n\n", s, flags=re.I)
    s = _TAG_RE.sub("", s)
    return _html.unescape(s).strip()


def _get(url: str, *, timeout: int = 20) -> str | None:
    """GET with backoff. Treats an EMPTY 200 as a retryable failure.

    The empty-200 is real and frequent (~1 in 4 in testing): Reddit answers with
    the right status and no feed at all. Accepting it would silently look like
    "this subreddit has no new posts".
    """
    for attempt in range(RSS_ATTEMPTS):
        try:
            r = requests.get(url, headers={"User-Agent": RSS_UA}, timeout=timeout)
            if r.status_code == 200 and r.text and "<feed" in r.text:
                return r.text
            # 404 is final — the subreddit does not exist. Retrying wastes the
            # rate-limit budget that the *other* subreddits need.
            if r.status_code == 404:
                return None
            if attempt < RSS_ATTEMPTS - 1:
                time.sleep(RSS_BASE_DELAY)
                continue
        except Exception:  # noqa: BLE001
            if attempt < RSS_ATTEMPTS - 1:
                time.sleep(RSS_BASE_DELAY)
                continue
    return None


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def fetch_new(subreddit: str, limit: int = 30,
              after_utc: float | None = None) -> tuple[list[dict], str]:
    """Newest posts in a subreddit. Returns (posts, error_message).

    An empty list with an empty error means "the feed worked and the sub is
    quiet"; an empty list WITH an error means the fetch failed. The caller
    must be able to tell those apart — conflating them is how a broken
    transport starts looking like a slow news day.
    """
    url = f"https://www.reddit.com/r/{subreddit}/new.rss?limit={min(int(limit), 100)}"
    text = _get(url)
    if text is None:
        return [], (f"r/{subreddit}: rate-limited or unreachable after "
                    f"{RSS_ATTEMPTS} attempts — not the same as 'no new posts'")
    if "page not found" in text.lower() and "<entry>" not in text:
        return [], f"r/{subreddit}: no such subreddit (or it is private)"

    out: list[dict] = []
    for chunk in _ENTRY_RE.findall(text):
        m_id = _ID_RE.search(chunk)
        if not m_id:
            continue
        rid = m_id.group(1)

        m_t = _TITLE_RE.search(chunk)
        title = _html.unescape(_TAG_RE.sub("", m_t.group(1))).strip() if m_t else ""

        m_a = _AUTHOR_RE.search(chunk)
        author = m_a.group(1).strip() if m_a else ""

        m_l = _LINK_RE.search(chunk)
        link = m_l.group(1) if m_l else f"https://www.reddit.com/comments/{rid}"

        m_p = _PUBLISHED_RE.search(chunk)
        created = _parse_ts(m_p.group(1)) if m_p else None

        m_c = _CONTENT_RE.search(chunk)
        body = strip_html(m_c.group(1)) if m_c else ""
        # The feed appends a "submitted by /u/x [link] [comments]" footer.
        body = re.sub(r"submitted by\s*/u/\S+.*$", "", body, flags=re.S).strip()

        if after_utc and created and created.timestamp() < after_utc:
            continue

        out.append({
            "reddit_id": rid, "subreddit": subreddit, "title": title,
            "selftext": body, "author": author, "url": link, "permalink": link,
            "created_utc": created,
        })
    return out, ""


def about(subreddit: str) -> dict | None:
    """What we can learn about a subreddit without OAuth.

    `about.json` is 403 from here, so this is the Atom <subtitle> only:
    a public description, no subscriber count, no rules text. Subscribers
    come back as None (not 0) so the audit scorer can tell "unknown" from
    "tiny" — the source app stored 0 and then had to guard against it.
    """
    text = _get(f"https://www.reddit.com/r/{subreddit}/new.rss?limit=1")
    if text is None:
        return None
    if "page not found" in text.lower() and "<entry>" not in text:
        return None
    m = _SUBTITLE_RE.search(text)
    desc = strip_html(m.group(1)) if m else ""
    titles = [_html.unescape(_TAG_RE.sub("", t)).strip()
              for t in _TITLE_RE.findall(text)]
    return {
        "subscribers": None,          # unknowable over RSS — never fake a 0
        "public_description": desc[:1000],
        "rules_text": "",             # needs OAuth; absent, not empty
        "_source": "rss",
    }


def sample_titles(subreddit: str, n: int = 15) -> list[str]:
    """Recent post titles — the audit's evidence for what a sub is actually about."""
    posts, _ = fetch_new(subreddit, limit=n)
    return [p["title"] for p in posts if p.get("title")][:n]


def normalise_name(raw: str) -> str:
    """`r/SEO`, `/r/SEO`, a full URL, or `SEO` → `SEO`."""
    s = (raw or "").strip()
    s = re.sub(r"^https?://(www\.)?reddit\.com", "", s, flags=re.I)
    s = s.strip("/")
    s = re.sub(r"^/?r/", "", s, flags=re.I)
    return re.sub(r"[^A-Za-z0-9_]", "", s.split("/")[0])[:120]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
