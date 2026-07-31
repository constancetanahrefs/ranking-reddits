# Data model

Five tables. Names below are the portable build's; the Letaido build prefixes them `rr_`.

## `workspaces` — one monitored project

Added when the app went multi-project. Each row is an independent scope: its own
target domain, brand keywords, Rank Tracker project, countries, SERP-position cap
and sources — plus its own cards, read state and scan history.

| Column | Why |
|---|---|
| `name` | display label in the switcher |
| `target_domain` | scopes the Brand Radar source |
| `brand_keywords` | threads matching these are annotated as brand mentions |
| `rt_project_id`, `rt_project_name` | the Rank Tracker project driving the keyword source |
| `rt_tags` | optional tag filter within that project |
| `ahrefs_secret` | **which connector secret owns this project.** A project is only visible to the token whose Ahrefs workspace owns it, so this is per-project, not global — the wizard records it automatically |
| `countries`, `max_serp_position`, `brand_limit`, `sources` | scan scope |
| `manual_keywords` | fallback when there's no Rank Tracker project |
| `is_default` | which project loads with no `?ws=` param |

**The migration lesson:** `url_key` used to be globally unique, and SQLAlchemy had
created that as a unique *index*, not a table constraint. Dropping the constraint
left the index still enforcing global uniqueness, so the second project exploded
on the first thread it shared with the first project. Drop the index too, then
create a composite unique on `(workspace_id, url_key)`.

## `threads` — one row per card

The unit the user interacts with. One unique Reddit thread = one card, no matter how many keywords surface it.

| Column | Type | Why it exists |
|---|---|---|
| `id` | uuid pk | |
| `workspace_id` | uuid fk | Which project this card belongs to. Cascades on project delete. |
| `url_key` | text | Canonical `<subreddit>/<thread_id>` from `/r/<sub>/comments/<id>`. **The dedupe key.** Raw URLs vary by trailing slash, `utm_*`, `www`, comment permalinks — dedupe on the raw URL and one thread becomes four cards. Subreddit landing pages (no `/comments/`) key as `r/<sub>`. **Unique per `(workspace_id, url_key)`, not globally** — the same thread can rank for two projects and is a separate card in each, so marking it read in one leaves the other untouched. |
| `url` | text | The first URL seen for it; what you link out to. |
| `title` | text | From the SERP initially (SERP titles carry a ` : r/SEO - Reddit` tail — strip it), overwritten with the real title on enrichment. |
| `subreddit` | text | Filter + display. |
| `author` | text | Only known after enrichment; SERP data rarely has it. |
| `posted_at` | timestamptz **null** | Nullable **on purpose**. Null renders "date unknown" — never today's date. |
| `description` | text | SERP snippet. Useful before enrichment, and as LLM fallback context. |
| `best_position` | int null | Best position across all hits. A thread ranking #2 for one keyword and #9 for another is a #2 problem. |
| `max_volume` | int null | Highest search volume among its keywords — a proxy for how much traffic the thread intercepts. |
| `ai_citations` | int | Sum of per-engine citation counts, where available. Optional signal. |
| `citation_counts` | jsonb | Per-engine breakdown, kept raw so a new engine doesn't need a migration. |
| `sources` | text[] | `brand`, `keywords`, or both. Drives the badges. |
| `is_new` | bool | Set on discovery, cleared when opened. The "what changed this month" signal. |
| `is_read`, `read_at`, `read_by` | bool, ts, text | Reading state, and who read it — this is a team tool. |
| `upvotes`, `num_comments`, `upvote_ratio` | int/int/float, all **null** | Nullable **on purpose**. Null = not fetched. **Never write 0 for unknown** — it silently corrupts every "sort by upvotes" view. |
| `body_md` | text | Post body + top comments. The LLM's real context. |
| `fetch_status` | text | `pending` / `running` / `done` / `failed`. `running` is reset to `pending` at startup so a restart mid-fetch doesn't strand a card. |
| `fetch_error` | text | Shown on the card. Also holds a *partial* error when one enrichment half worked and the other didn't. |
| `ai_notes` | jsonb | `{summary, brand[], content[], sentiment, reply_worthy}`. JSON, not text, so the UI can render sections and you can query on sentiment later. |
| `notes_status`, `notes_error` | text | Same lifecycle as fetch. |
| `user_notes` | text | Human notes. Never overwritten by AI. |
| `saved_ref` | text null | Reference in the user's system of record once exported. Non-null = "saved" filter. |
| `first_seen_at`, `last_seen_at` | timestamptz | First tells you when it entered the index (sort "recently found"); last proves it still ranks. |

## `hits` — one row per (thread × keyword × country × source)

Why a separate table: a single thread can rank for a dozen keywords, in several countries, found by both sources. Flattening that into `threads` would either lose data or duplicate cards.

| Column | Notes |
|---|---|
| `thread_id` | fk → threads, cascade delete |
| `keyword` | the SERP query that surfaced the thread |
| `country` | ISO-3166-1 alpha-2 |
| `source` | `brand` \| `keywords` |
| `serp_position`, `search_volume` | as of this hit |
| `serp_updated_at` | the SERP snapshot date — lets you tell stale hits from fresh ones |
| `matched_brands` | which brand terms matched, when the source annotates it |
| **unique** | `(thread_id, keyword, country, source)` |

**Two dedupe layers are required.** The unique constraint is not enough: one API response legitimately repeats the same `(keyword, country)` across multiple snapshot rows, so a single batch will violate the constraint mid-transaction. Track an in-memory set of tuples already added *in this batch* and skip repeats before inserting. (This is a real bug we hit — the first scan died on `duplicate key value violates unique constraint`.)

## `scans` — the audit trail

| Column | Notes |
|---|---|
| `id`, `started_at`, `finished_at` | |
| `status` | `running` / `completed` / `failed`. Reset stale `running` rows at startup. |
| `trigger` | `manual` \| `monthly` — tells you whether a human or the cron found something |
| `sources`, `keywords_used` | what was actually scanned |
| `threads_seen`, `threads_new`, `hits_new` | seen vs new is the whole point: 820 seen / 0 new is a healthy idempotent re-run; 0 seen is a broken transport |
| `error`, `log` | why it failed, and per-source row counts |

Without this table you cannot tell "nothing new happened" from "the integration broke three weeks ago". That distinction is the reason to build it.

## `settings` — key/value

The user's config (target domain, brand terms, project id, tag filter, max position, countries, active sources, auto-fetch/auto-notes toggles) stored as rows so it's editable in-app, not only at deploy time. Read with defaults merged underneath, and make sure a stored `null` can't erase a default.

---

## Indexes worth having

`threads`: unique on `url_key`; plus `is_new`, `is_read`, `subreddit`, `posted_at`, `fetch_status`.
`hits`: `thread_id`, `source`, and the unique tuple.
`scans`: `started_at desc`.
