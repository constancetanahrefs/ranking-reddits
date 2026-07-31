# Portable Ranking Reddits

Standalone Flask + Postgres app talking to the **Ahrefs API v3** directly. No Letaido, no connectors — just an API key.

## Quick start

```bash
cp .env.example .env    # then fill it in — see docs/AHREFS_SETUP.md
docker compose up --build
# → http://localhost:8000
```

Without Docker:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
createdb ranking_reddits
export $(grep -v '^#' .env | xargs)
python3 scripts/preflight.py          # free API calls only — verifies setup + estimates cost
gunicorn -w 1 -k gthread --threads 8 -t 120 -b 127.0.0.1:8000 app.main:app
```

`python3 -m app.main` also works for local dev (Werkzeug, single process).

## Run the pre-flight first

`scripts/preflight.py` uses **only free Ahrefs endpoints**. It checks your key, your project, your keyword count and your unit budget, then tells you how many billable SERP calls a scan will make and roughly what it costs. Run it before spending anything.

```
$ python3 scripts/preflight.py
config: all required values present
api key: OK — usage/limits: {...}
rank tracker project 1234567: 214 keywords
  sample: acme pricing, acme review, ...
brand keyword set: 82 keywords
estimated scan cost
  keywords: 250 (cap 250) x 1 market(s) = 250 billable SERP calls
  minimum units: ~12500
```

## What's where

```
app/config.py    env-driven config; missing_required() is why a bad setup fails loudly
app/models.py    the four tables + restart recovery
app/ahrefs.py    Ahrefs API v3 client — free vs billable endpoints marked
app/reddit.py    enrichment: Atom feed (text) + rendered page (votes)
app/engine.py    scan, ingest, enrichment batching, AI notes, reads
app/main.py      Flask routes, Pydantic-validated
templates/       single-page UI (no build step, no framework, dark-mode aware)
scripts/preflight.py      free pre-flight + cost estimate
scripts/monthly_scan.py   the monthly worker
```

## The monthly worker

```cron
0 5 1 * *  cd /path/to/portable && python3 scripts/monthly_scan.py >> scan.log 2>&1
```

Idempotent — re-running the same month adds zero duplicates. Exits non-zero when a source returns zero rows, so your cron mailer tells you the integration broke instead of letting it look like a quiet month.

## Cost control

Every billable call is a SERP Overview (one per keyword × market, ≥50 units each). Guards:

- `MAX_KEYWORDS_PER_SCAN` — hard ceiling per scan (default 250).
- `RT_TAGS` — scan only the keyword tags you care about.
- `COUNTRIES` — each extra market multiplies the call count.
- `MAX_SERP_POSITION` — passed to the API as `top_positions`, so you don't pay for rows you'd discard.
- Enrichment is **lazy** — nothing is fetched from Reddit until you open a card or press "Fetch next 10".

## Optional pieces

| Feature | Needs | Without it |
|---|---|---|
| AI reading notes | `LLM_API_KEY` | Cards work, no notes |
| Upvote / ratio counts | `APIFY_API_TOKEN` | Everything else works; upvotes show "not fetched" |
| Brand AI-citations | `BRAND_RADAR_REPORT_ID` | Not shown |

## Saving cards elsewhere

`POST /api/save` marks a card as exported and stores a reference. It's a **seam, not an integration** — replace the route body in `app/main.py` to push into Notion, Airtable, Linear, your CMS, or wherever your team keeps research.

## Notes

- Postgres, not SQLite: concurrent enrichment needs real locking, and `ARRAY`/`JSONB` columns are used.
- One gunicorn worker with threads — jobs are tracked in memory, so multiple workers would each see only their own. Move jobs to a table (or Redis) before scaling out.
- Restart recovery runs on startup: any card stuck `running` resets to `pending` and any `running` scan is marked failed. Background threads die on redeploy; without this, cards would hang forever.
