# Build brief: Ranking Reddits

**How to use this file:** give it to an AI coding assistant (Claude, ChatGPT, Cursor, Copilot, Gemini, Windsurf…) as the task description. It is stack-agnostic and self-contained. If the assistant can read the rest of this repo, tell it to also read `docs/API_MAPPING.md`, `docs/REDDIT_ACCESS.md` and `docs/DATA_MODEL.md`.

---

## AI: read this section first

**Do not write any code until you have interviewed the user.** This app talks to *their* Ahrefs account. Every account-specific value must come from them. If you hardcode a project id, a report id, a domain or a brand name, the app is broken for its owner and leaks whoever's account you copied from.

Ask these, one at a time, and wait for answers. Offer the defaults shown but never assume them:

1. **"What domain are we monitoring?"** (e.g. `example.com`) — used to scope brand visibility.
2. **"What brand terms should count as a mention?"** (comma separated; usually the brand name plus obvious variants.)
3. **"Do you have an Ahrefs API key?"** If not: point them to Account settings → API keys, tell them it needs a **paid plan, Lite or above**, and tell them to put a **unit limit** on the key while developing. Never ask them to paste the key into a file you commit — use an environment variable.
4. **"Do you have a Rank Tracker project with keywords in it?"**
   - If yes: call `GET /v3/management/projects` (free) and let them **pick from the list** rather than typing an id.
   - If no: offer to create one — `POST /v3/management/projects`, then `PUT /v3/management/project-keywords`. Ask which keywords, and suggest brand + brand-modifier + support-question terms.
   - Either way, verify with `GET /v3/management/project-keywords?project_id=…` (free) that it returns a non-zero count, and say the number out loud.
5. **"Only certain keyword tags, or all of them?"** (Tags come back per keyword; filter client-side. Default: all.)
6. **"Which markets?"** (ISO country codes; default: all.)
7. **"Do you want AI reading notes?"** If yes, which LLM provider + key. If no, skip that whole feature — the app must work without it.
8. **"Do you want upvote counts?"** They require a managed-browser/proxy service (see `REDDIT_ACCESS.md`) — extra cost. If no, everything else still works; show "not fetched".
9. **Optional:** "Do you have a Brand Radar report?" Only needed for the AI-citations extra. Offer `GET /v3/management/brand-radar-reports` (free) to list, or `POST` to create.

Then: **estimate and state the API cost before the first scan.** `GET /v3/subscription-info/limits-and-usage` is free — call it, and tell the user roughly how many units the scan will consume (one billable SERP call per keyword, minimum 50 units each). Get their go-ahead.

Write every one of these into config/env with **no working default**. On startup, if a required value is missing, refuse to scan and name exactly which ones — do not silently scan a placeholder domain.

---

## What you're building

A card wall of the Reddit threads that rank in search for keywords the user cares about, so a marketing team can read them, judge them, and act.

### Sources (both, into one card wall)

**Source A — tracked keywords.** The keywords in the user's Ahrefs Rank Tracker project. For each, fetch the SERP and keep results whose URL is on `reddit.com` and whose position ≤ 10 (configurable).

**Source B — brand visibility.** The same thing over a *brand* keyword set (brand name + modifiers, optionally expanded from keywords the domain already ranks for). These are the threads competing with the user for their own brand queries.

A thread found by both is **one card with two source badges** — never two cards.

Exact endpoints, costs and the one real gap (the public API has no Reddit-in-SERPs endpoint; you use SERP Overview instead) are in `docs/API_MAPPING.md`. Read it before writing the client.

### The card

Shows: subreddit · thread publish date · title · best SERP position · which keywords surface it (with position and search volume) · upvotes + comment count once fetched · a NEW badge if the last scan discovered it · a read/unread state.

**Clicking a card** opens the thread *inside the app* (not a new tab — an in-app reader), and:

1. marks it **read** and clears its NEW flag,
2. fetches title / OP / publish date / body / top comments, and upvotes + comment count (see `docs/REDDIT_ACCESS.md` — this is the fiddly part, read it),
3. drafts **AI reading notes** in two clearly separated bullet sections:
   - **Brand** — what's said about the brand or its competitors, the sentiment, factual errors worth correcting, and whether a reply from the brand would help.
   - **Content** — the content/keyword angle: which question isn't well answered, whether existing docs cover it, and a concrete article or update idea.
4. offers a free-text "my notes" box that persists,
5. offers an **export/save** action (to whatever the user's system of record is — a bookmarks table, Notion, Airtable, a `saved` flag).

The reader also links out to the real thread, and offers "re-fetch" and "re-draft notes".

### Filters and sorting

Filter by: source (all / brand / keywords), state (all / new / unread / read / saved), subreddit, free-text search on title+snippet. Sort by: thread date, recently found, best SERP position, upvotes, keyword volume.

### The monthly worker

A scheduled job (cron, Celery beat, GitHub Action, whatever your stack uses) that re-runs both sources once a month, upserts everything, and flags only genuinely-new URLs as NEW. It must be **idempotent** — re-running the same month adds zero duplicates. Verify that by running it twice and asserting the second run inserts nothing.

---

## Non-negotiable engineering rules

These come from things that actually broke. Ignore them and you'll ship data the user can't trust.

1. **Dedupe on the canonical thread key, not the URL.** Extract `<subreddit>/<thread_id>` from `/r/<sub>/comments/<id>`. Raw URLs vary by trailing slash, `utm_*` params, `www`, and comment permalinks — dedupe on the URL and one thread becomes four cards.

2. **A zero-row scan is a FAILURE, not an empty result.** If a source returns nothing, record the run as failed with the reason. Otherwise a silent transport/auth change looks exactly like "quiet month" and nobody notices for weeks. Keep a scan-history view so this is auditable.

3. **Unknown is never zero.** Missing upvotes render as "not fetched", not `0`. Missing dates render as "date unknown", not today. Never substitute a plausible number for an absent one.

4. **Enrichment is lazy and bounded.** Never enrich a whole scan automatically — hundreds of threads × ~20s each is hours of runtime and real money. Fetch on card-open, plus a "fetch next N" button with a hard cap.

5. **Long work runs in the background with a polling UI.** Most HTTP layers time out around 30–60s; a scan takes minutes and a fetch ~20s. Return a job id immediately and poll. Also: **recover on restart** — reset any job stuck in `running` at startup, or a redeploy mid-fetch leaves cards permanently stuck.

6. **Persist in a real database.** Postgres or equivalent. Not SQLite (concurrent enrichment workers), not JSON files, not an in-memory dict (evaporates on restart).

7. **Validate every request boundary** with a schema library (Pydantic / zod / etc.) that rejects unknown fields.

8. **Parse LLM JSON defensively.** Providers wrap JSON-mode replies in ``` fences even when you asked for strict JSON. Strip fences, then fall back to a `{...}` regex extract before giving up.

9. **Never fabricate in the notes.** The prompt must instruct: no invented quotes, no invented metrics, and if the thread body is missing, say so rather than guessing from the title.

10. **Every feature ships with its explainer.** Each stat, filter, threshold and toggle gets a tooltip saying what it means and where the number came from. A metric a user can't interpret is a metric they'll mistrust.

11. **Rate-limit politely.** Ahrefs ~60 req/min; Reddit's feed 429s on rapid repeats. Back off exponentially and space batch jobs.

12. **Secrets in env vars only.** Never in code, never in the repo, never echoed into logs or error messages shown to the user.

---

## Suggested data model

Four tables. Full field-by-field rationale in `docs/DATA_MODEL.md`.

- **`threads`** — one row per unique Reddit thread (the card). Canonical key, url, title, subreddit, author, posted_at, snippet, best_position, max_volume, sources[], is_new, is_read, read_at/by, upvotes, num_comments, upvote_ratio, body, fetch_status + fetch_error, ai_notes, notes_status, user_notes, saved reference, first_seen_at, last_seen_at.
- **`hits`** — one row per (thread × keyword × country × source), with serp_position, search_volume, snapshot date. Unique constraint on that tuple. This is what lets one card show "ranks for 12 keywords".
- **`scans`** — one row per run: trigger, sources, keywords_used, threads_seen, threads_new, hits_new, status, error, log. The audit trail behind rule 2.
- **`settings`** — the user's config, so it's editable in-app rather than only at deploy time.

Two dedupe layers are needed on `hits`: the unique constraint **and** an in-memory set for the current batch, because one API response legitimately repeats the same (keyword, country) across snapshot rows.

---

## Definition of done

- [ ] Startup with missing config refuses to scan and names the missing values.
- [ ] A scan populates cards from both sources; a thread found by both shows one card, two badges.
- [ ] Running the scan twice adds **zero** duplicate threads and zero duplicate hits.
- [ ] Forcing a source to return nothing produces a **failed** scan with a readable reason, not a silent success.
- [ ] Opening a card marks it read, clears NEW, fetches details, and drafts notes with both sections.
- [ ] A thread whose votes couldn't be read shows "not fetched" — grep the codebase to confirm no path writes `0` for unknown.
- [ ] A subreddit landing page (no `/comments/`) is detected and skipped with an explanation.
- [ ] Restarting mid-fetch leaves no card stuck in `running`.
- [ ] The scheduled monthly job runs, is idempotent, and exits non-zero on a zero-row scan.
- [ ] Every stat and control has a tooltip.
- [ ] No secret, account id, domain or brand term from anyone else's account appears anywhere in the repo.
