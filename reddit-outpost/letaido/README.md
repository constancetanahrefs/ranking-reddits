# Reddit Outpost — Letaido build

The production source, running today. Drop-in Console app: no build step, no
server to start — `console-http` discovers it and restarts itself.

## Install

```bash
# 1. app + helpers  →  applications/ (helpers MUST keep their leading underscore;
#    the loader skips those, and the source viewer groups them with the app)
cp applications/reddit_outpost.py            /home/console/http/default/applications/
cp applications/_reddit_outpost_engine.py    /home/console/http/default/applications/
cp applications/_reddit_outpost_models.py    /home/console/http/default/applications/
cp applications/_reddit_outpost_reddit.py    /home/console/http/default/applications/

# 2. template  →  templates/<slug>/
mkdir -p /home/console/http/default/templates/reddit_outpost
cp templates/reddit_outpost/index.html /home/console/http/default/templates/reddit_outpost/

# 3. the daily worker
cp scripts/reddit_outpost_daily.py ~/workspace/scripts/
```

Open `/applications/reddit_outpost/`. The eight `outpost_*` tables are created on
import via `create_all`, and a placeholder watch profile is seeded.

## Configure

1. **⚙ Edit** the seeded profile → replace the brief with your product →
   **✨ Generate from the brief** for topics.
2. Set a **brand-mention pattern** — a whole-word regex forced to maximum
   relevance. Use a term nobody else uses; a generic product name floods the
   feed. Leave empty to disable.
3. **📂 Subreddits** — replace the seeded marketing/SaaS list with your own, or
   run **🔎 Discover**.
4. **🔔 Notifications** — tick the digest and send a test. The address comes from
   your authenticated Console session; nothing to type.
5. Schedule `scripts/reddit_outpost_daily.py` daily (`0 7 * * *`) via
   `request_create_script_automation`.

Set `OUTPOST_APP_URL` for the digest's deep links, or edit `APP_URL` at the top
of the daily script — it defaults to a `CHANGE-ME` sentinel.

## What it depends on

| Uses | For |
|---|---|
| `src.db_cross` | `console_site_db`, shared with Scrapbook so a thread can be pushed there in one transaction |
| `src.llm` | the workspace LLM proxy, with per-app spend attribution |
| `src.schemas` | `validate_json` / `validate_query` → 422 on bad input |
| `letaido_email` | digests; delivers only to members of the workspace's org |
| `X-Auth-User-*` | identity, injected by nginx |
| `templates/base.html` | workspace chrome + design tokens |

Swapping those four modules is exactly what [`../portable/`](../portable/) does.

## Notes

- **Late columns are ALTERed inside the blueprint** (`_migrate()`), not from an
  agent-side `psql`: these tables are owned by the `console` role, so `agent`
  gets *"must be owner"*. `create_all()` adds tables only, never columns.
- **Jobs live in memory**, so any file edit — including another session's —
  restarts `console-http` and drops an in-flight scan. The UI reports the
  interruption instead of spinning; persisted work is kept.
- There's an optional header link to a companion app, **Ranking Reddits**
  (threads that *rank* in search, rather than fresh ones). Delete that `{% if %}`
  block if you don't run it.
