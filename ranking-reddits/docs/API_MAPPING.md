# Letaido connector → Ahrefs API v3 endpoint mapping

If you're building outside Letaido, you call `https://api.ahrefs.com/v3/...` directly with a Bearer token instead of using connectors. This page maps every call the app makes.

```
Authorization: Bearer <AHREFS_API_KEY>
Accept: application/json
```

Base URL: `https://api.ahrefs.com/v3`. Rate limit: ~60 req/min, HTTP 429 on burst — back off exponentially.

> **Verify before you build.** Ahrefs ships endpoints regularly. The authoritative reference is <https://docs.ahrefs.com/api/reference>. Everything below was checked against the live docs at time of writing; if something 404s, look it up rather than guessing a path.

---

## 1. The tracked-keyword source

**What it does:** get the keywords you track, then find Reddit pages in their top 10.

### 1a. List your Rank Tracker projects

| | |
|---|---|
| Letaido | `ahrefs_rank_tracker.list_projects` |
| API v3 | `GET /v3/management/projects` |
| Cost | **Free** — no API units |

Returns `projects[]` with `project_id`, `name`, `target_url`, `target_mode` (`exact`/`prefix`/`domain`/`subdomains`), `protocol`, `access`. Use it to let the user *pick* a project instead of asking them to paste an id.

The id is also visible in the Ahrefs UI URL: `https://app.ahrefs.com/rank-tracker/overview/<project_id>`.

### 1b. Get the tracked keywords

| | |
|---|---|
| Letaido | `ahrefs_rank_tracker.overview_keywords_export` |
| API v3 | `GET /v3/management/project-keywords?project_id=<id>` |
| Cost | **Free** |

Returns `keywords[]` with `keyword` and `language_code`. This is the cheap, canonical keyword list — use it for the scan.

If you also want each keyword's **position, volume, tags and ranking URL** (for a richer UI), use `GET /v3/rank-tracker/overview` — also **free**, with `select`, `where`, `order_by`, `limit`. Tag filtering happens here: tags come back per keyword, so filter client-side on the tag names your user picked.

### 1c. Find the Reddit pages ranking for each keyword

| | |
|---|---|
| Letaido | `ahrefs_brand_radar.reddit_results` (scoped by a query text rule) |
| API v3 | `GET /v3/serp-overview?keyword=<kw>&country=us` |
| Cost | **Consumes units** — this is the expensive part |

Required: `select` (comma-separated columns) and `keyword`. Useful params: `country` (ISO-3166-1 alpha-2), `top_positions` (cap how many organic rows come back), `date` (a past crawl).

Ask for at least:

```
select=position,result_type,url,title,description,domain_rating,traffic,top_keyword,top_volume
```

Then **keep only rows whose `url` contains `reddit.com`** and whose `position` ≤ 10. One call per keyword, so:

- **Cache aggressively.** SERPs move slowly; a monthly refresh is the whole point of this app.
- **Cap the keyword list.** 214 keywords ≈ 214 calls ≈ 214 × ≥50 units. Let the user filter by tag first.
- Use `top_positions=10` so you're not paying for rows you'll discard.
- Check your budget with `GET /v3/subscription-info/limits-and-usage` (**free**) *before* a big scan and tell the user the estimated cost.

**Rank Tracker's own SERP endpoint** (`GET /v3/rank-tracker/serp-overview`, free) is worth trying first: it returns the stored SERP for a tracked keyword including `Discussions`-type results. In our testing the snapshot timestamps required by the equivalent Letaido cap came back empty for the project we used, so treat it as a bonus path, not the primary one — fall back to `/v3/serp-overview` when it returns nothing.

---

## 2. The brand-visibility source

**What it does:** find Reddit threads ranking on the SERPs where your brand is the entity — including threads that outrank you for your own brand queries.

### ⚠️ The gap you need to know about

| | |
|---|---|
| Letaido | `ahrefs_brand_radar.reddit_results` |
| API v3 | **No public equivalent** |

Brand Radar's public API surface (18 endpoints) covers AI visibility (`/ai-responses`, `/cited-pages`, `/cited-domains`), overview stats (`/mentions-overview`, `/impressions-overview`, `/sov-overview`) and their history variants. The **Reddit-in-SERPs index is not exposed as a REST endpoint** — it's available in the Brand Radar UI and, in Letaido, through the connector.

Two honest options:

**Option A — SERP Overview over a brand keyword set (recommended).**
Build the keyword set yourself, then reuse the exact call from §1c:

1. Seed with your brand terms (`yourbrand`, `yourbrand pricing`, `yourbrand vs …`, `yourbrand alternative`).
2. Expand with `GET /v3/keywords-explorer/matching-terms?keywords=<brand>` and/or the keywords your domain already ranks for: `GET /v3/site-explorer/organic-keywords?target=<yourdomain>&mode=subdomains`.
3. Run SERP Overview on that set and keep the reddit.com rows.

You lose the connector's `matched_brands` annotation (trivial to recompute: does the title/snippet contain a brand term?) and the per-engine **AI citation counts** (`chatgpt`, `gemini`, `perplexity`, `copilot`, `grok`, `google_ai_overviews`, `google_ai_mode`) — there is no per-URL public equivalent for these.

**Option B — approximate AI-side interest with the AI visibility endpoints.**
If what you want is "which Reddit URLs do AI engines cite about my brand", that *is* public:

```
GET /v3/brand-radar/cited-pages
  ?select=<cols>&data_source=chatgpt&brand=<brand>
```

`data_source` ∈ `chatgpt`, `perplexity`, `gemini`, `copilot`, `google_ai_overviews`, `google_ai_mode`. Note the **Google sources can't be mixed with the chatbot sources in one call** — issue separate calls. At least one of `brand`, `competitors`, `market` or `where` must be non-empty, and `select` is required. Filter the results to `cited_domain = reddit.com` and you get Reddit URLs AI engines cite about your brand — a different, complementary signal to SERP position. Requests returning **only custom-prompt data are free**; including Ahrefs prompt data consumes units and requires `report_id` when `prompts=custom`.

Ship Option A as the source, and Option B as an optional "AI citations" column.

---

## 3. Enrichment (not Ahrefs)

Titles, upvotes, dates and bodies come from Reddit, not Ahrefs. See [REDDIT_ACCESS.md](REDDIT_ACCESS.md).

## 4. Reading notes (not Ahrefs)

Any OpenAI-compatible chat completion endpoint. The app asks for strict JSON; **strip ``` fences before parsing** — several providers wrap JSON-mode replies in a markdown fence even when you asked for `response_format: json_object`.

---

## Cost summary

| Call | Units |
|---|---|
| `/v3/management/projects` | Free |
| `/v3/management/project-keywords` | Free |
| `/v3/management/brand-radar-reports` | Free |
| `/v3/rank-tracker/overview` | Free |
| `/v3/rank-tracker/serp-overview` | Free |
| `/v3/subscription-info/limits-and-usage` | Free |
| `/v3/serp-overview` | **≥50 per call**, scales with rows + fields |
| `/v3/keywords-explorer/*` | **Consumes units** |
| `/v3/brand-radar/cited-pages` | Free for custom-prompt-only; otherwise consumes units |

Minimum cost for any billable request is **50 units**, and consumed units are **non-refundable** — so build against [free test queries](https://docs.ahrefs.com/api/docs/free-test-queries) and put a **limit on your API key** while developing.
