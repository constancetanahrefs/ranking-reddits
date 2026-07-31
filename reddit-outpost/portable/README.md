# Reddit Outpost — standalone build

No Letaido, no Ahrefs, no Reddit API key. Flask + PostgreSQL + any
OpenAI-compatible LLM endpoint.

## Run it

```bash
cp .env.example .env       # then set LLM_API_KEY
docker compose up -d
open http://localhost:8000
```

Or without Docker:

```bash
pip install -r requirements.txt
createdb outpost
cp .env.example .env       # set DATABASE_URL + LLM_API_KEY
python -m app.main         # dev server on :8000
```

Tables are created on first boot. A placeholder watch profile is seeded — open
**⚙ Edit**, replace the brief with your own product, and click
**✨ Generate from the brief** to derive topics.

## What you must configure

| Setting | Why it matters |
|---|---|
| `LLM_API_KEY` | Every relevance score and reply draft. Nothing works without it. |
| **The product brief** (in the UI) | The single most important string in the app. It grounds scoring *and* drafting — a vague brief produces false positives and useless drafts. |
| `OUTPOST_EMAIL` | Where digests go. There is no login standing alone. |
| Subreddits | Seeded with a marketing/SaaS set. Replace with wherever *your* buyers are, or use the **Discover** tab. |

## The daily scan

```bash
python scripts/daily.py          # scan every profile, email digests, sweep old posts
```

Cron it once a day — `0 7 * * *`. The compose file includes a `cron` service that
does this for you; drop it if you'd rather use your host's crontab.

**Expect it to be slow: roughly one minute per subreddit.** That is deliberate,
see below.

## Reddit access, and why the scan is slow

There is no Reddit API key here. The app reads public Atom feeds
(`/r/<sub>/new.rss`), which needs no auth but comes with real constraints,
measured rather than assumed:

- A **browser User-Agent is mandatory** — a generic one gets 403.
- `about.json` is **403 from datacenter IPs** and has been since 2023. So
  subscriber counts are unavailable; they're stored `NULL` and shown as
  "unavailable", never as `0`.
- The feeds sometimes return an **empty 200** (~1 in 4 under load). Treating that
  as "no new posts" would silently lose data, so it's retried.
- **The 429 budget is global to your IP and cumulative**, not per subreddit.
  Fetching 8 subreddits in parallel fails almost entirely; serial at 30s spacing
  still 429s about half the time. What works is patient retry: **4 attempts, 20s
  apart** got 7/8 subreddits in 26 requests.

Hence: serial fetching, ~1 min per subreddit, with a 404 short-circuiting
immediately so a dead subreddit doesn't burn the budget the others need. From a
residential IP you may get away with more — raise `RSS_ATTEMPTS` / lower
`RSS_BASE_DELAY` and `RSS_SPACING` in `app/reddit.py` if so.

If you have Reddit OAuth credentials, `app/reddit.py` is the only file to change
to get upvotes, comment counts and rules text.

## It never posts to Reddit

There is no write path. Reddit shadowbans accounts that post automated
promotion, so every reply is copied by a human and pasted by a human. The app
records what you posted afterwards, as an audit trail. Keep it that way.

## No authentication

Standing alone there is none — it's a single-user tool for localhost or behind
your own auth proxy. **Don't expose it to the internet as-is.** If you put a
proxy in front, have it set `X-Auth-User-Id` / `X-Auth-User-Email` and the app
will pick them up per-user automatically. `OUTPOST_READ_ONLY=true` makes the UI
read-only.

## Layout

```
app/db.py       engine + session + create_all
app/models.py   the 8 tables
app/reddit.py   Atom-feed fetch + the rate-limit strategy
app/llm.py      OpenAI-compatible client (OpenRouter by default)
app/notify.py   email: console | smtp | resend
app/engine.py   profiles, scan, scoring, drafting, Discover
app/main.py     Flask routes + Pydantic validation + app factory
templates/      the single-page UI (Tailwind via CDN)
scripts/daily.py  the cron entrypoint
```

## Gunicorn: one worker

Scans and Discover run in background threads and keep job state **in memory**.
More than one worker sends the status poll to a process that never heard of the
job. Scale with threads, not workers — and note a restart loses in-flight jobs
(the UI says so rather than spinning).
