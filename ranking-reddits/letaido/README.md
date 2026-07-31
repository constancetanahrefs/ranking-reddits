# Letaido install

The production source of Ranking Reddits as it runs in a Letaido workspace. It uses **Ahrefs connectors** (OAuth — you never handle a token) instead of the REST API. If you're not on Letaido, use [`../portable/`](../portable/) instead.

## Install

Copy into your workspace's Console scaffold (`/home/console/http/default/`):

```
applications/ranking_reddits.py
applications/_ranking_reddits_engine.py
applications/_ranking_reddits_models.py
templates/ranking_reddits/index.html
```

and the worker into your workspace:

```
~/workspace/scripts/ranking_reddits_monthly.py
```

Console auto-discovers the blueprint and the app appears at `/applications/ranking_reddits/`. Tables (`rr_threads`, `rr_hits`, `rr_scans`, `rr_settings`) are created on import in `console_site_db`.

## Configure — required, nothing is pre-filled

Open the app → **⚙ Settings**:

| Setting | What it is |
|---|---|
| **Target domain** | the domain you're monitoring, e.g. `example.com` |
| **Brand keywords** | your brand terms — threads matching these are annotated as brand mentions |
| **Rank Tracker project** | picked from a live dropdown (`ahrefs_rank_tracker.list_projects`) |
| **Keyword tags** | empty = every tracked keyword |
| **Max SERP position** | `10` = first page only |
| **Countries** | empty = all markets |
| **Sources** | Brand Radar visibility, tracked keywords, or both |

The scan refuses to run until target domain / brand keywords / project are set.

### The secret

`ahrefs_secret` is recorded **per project** by the setup wizard, because a Rank Tracker project is only visible to the token whose Ahrefs workspace owns it. **If you have projects spread across two Ahrefs accounts, list both secret names** so the wizard searches both:

```bash
export RR_AHREFS_SECRETS="ahrefs_oauth,my_other_ahrefs_secret"
```

Without that, projects owned by the second account appear not to exist — the single most common setup failure.

Also needed:
- **Apify secret** (`apify_main`) — optional, for upvote counts only.
- **Firewall**: allow `www.reddit.com` so the Atom-feed enrichment works.

## The monthly worker

```
request_create_script_automation(
    name="Ranking Reddits — monthly scan",
    script_path="scripts/ranking_reddits_monthly.py",
    schedule="0 5 1 * *", timezone="UTC")
```

It exits non-zero when a source returns zero rows, so a silent breakage surfaces as a failed run instead of a quiet month.

## Connector caps used

| Cap | Purpose |
|---|---|
| `ahrefs_brand_radar.reddit_results` | both sources — Reddit threads ranking on SERPs |
| `ahrefs_rank_tracker.list_projects` | the project picker |
| `ahrefs_rank_tracker.overview_keywords_export` | the tracked keyword list + tags |
| `apify.run_actor_sync_get_dataset_items` | rendered page for upvote counts (optional) |

REST equivalents for each are in [../docs/API_MAPPING.md](../docs/API_MAPPING.md).
