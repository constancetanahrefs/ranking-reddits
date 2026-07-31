# Ahrefs setup — do this before your first scan

This app reads from your own Ahrefs account. Nothing works until three things exist: an **API key**, a **Rank Tracker project** with keywords, and (optionally) a **Brand Radar report**.

> **If an AI assistant is setting this up for you:** don't guess these values. Ask the user, one question at a time, and offer to create the missing pieces via the API (§2 and §3 below are write endpoints). Never invent a `project_id` or `report_id`.

---

## 1. API key — required

1. You need a **paid plan, Lite or above**. API v3 is not available on Ahrefs Free or Starter. (A limited set of [free test queries](https://docs.ahrefs.com/api/docs/free-test-queries) exists for development.)
2. Only **workspace owners and admins** can create keys.
3. Go to **Account settings → API keys** (<https://app.ahrefs.com/account/api-keys>).
4. Create a key and **put a unit limit on it** while you're developing — consumed units are non-refundable.
5. Store it as `AHREFS_API_KEY`. Never commit it.

Verify it and see your budget (this call is **free**):

```bash
curl -s "https://api.ahrefs.com/v3/subscription-info/limits-and-usage" \
  -H "Authorization: Bearer $AHREFS_API_KEY" | jq .
```

If this 401s, the key is wrong. If it 403s, your plan lacks API access.

---

## 2. Rank Tracker project — required for the tracked-keyword source

### Already have one?

List your projects (**free**) and pick the id:

```bash
curl -s "https://api.ahrefs.com/v3/management/projects" \
  -H "Authorization: Bearer $AHREFS_API_KEY" | jq '.projects[] | {project_id, name, target_url}'
```

Confirm it has keywords (**free**):

```bash
curl -s "https://api.ahrefs.com/v3/management/project-keywords?project_id=$PROJECT_ID" \
  -H "Authorization: Bearer $AHREFS_API_KEY" | jq '.keywords | length'
```

If that returns `0`, the project exists but tracks nothing — add keywords (§2b) or the scan will correctly fail.

### 2a. Create a project via the API

```bash
curl -s -X POST "https://api.ahrefs.com/v3/management/projects" \
  -H "Authorization: Bearer $AHREFS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "name": "My Site",
        "target_url": "example.com/",
        "target_mode": "subdomains",
        "protocol": "both"
      }'
```

`target_mode` ∈ `exact` | `prefix` | `domain` | `subdomains`. Use `prefix` to scope to a section (`example.com/blog`), `subdomains` for a whole site including `help.` / `docs.`.

### 2b. Add keywords via the API

```bash
curl -s -X PUT "https://api.ahrefs.com/v3/management/project-keywords" \
  -H "Authorization: Bearer $AHREFS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "project_id": '"$PROJECT_ID"',
        "keywords": ["your brand", "your brand pricing", "your brand vs competitor"]
      }'
```

Keywords are ingested asynchronously and positions appear after the next crawl. **Tags** (used to narrow a scan to e.g. only "Pricing" keywords) are managed in the Rank Tracker UI; the API returns them per keyword so the app can filter client-side.

Available locations/languages (**free**): `GET /v3/management/locations`.

### 2c. Which keywords to track

The app is only as good as this list. Good candidates:

- your brand and brand+modifier terms (`<brand> pricing`, `<brand> alternative`, `<brand> vs <competitor>`)
- the questions your support/help content answers
- category terms where a Reddit thread outranking you actually costs you traffic

Skip terms with no commercial or support relevance — every keyword costs API units at scan time.

---

## 3. Brand Radar report — optional

The **portable** build does not require a Brand Radar report: it derives brand SERP visibility from SERP Overview over a brand keyword set (see [API_MAPPING.md](API_MAPPING.md) §2). Set one up if you also want the AI-visibility signal (which Reddit URLs ChatGPT/Perplexity/Gemini cite about you).

List existing reports (**free**):

```bash
curl -s "https://api.ahrefs.com/v3/management/brand-radar-reports" \
  -H "Authorization: Bearer $AHREFS_API_KEY" | jq '.reports[] | {report_id, report_name}'
```

Create one:

```bash
curl -s -X POST "https://api.ahrefs.com/v3/management/brand-radar-reports" \
  -H "Authorization: Bearer $AHREFS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{ "report_name": "My Brand", "brand": "yourbrand.com" }'
```

Then add the prompts you want tracked:

```bash
curl -s -X POST "https://api.ahrefs.com/v3/management/brand-radar-prompts" \
  -H "Authorization: Bearer $AHREFS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{ "report_id": "<REPORT_ID>",
        "prompts": ["What does yourbrand do?", "Best alternatives to yourbrand"] }'
```

Check the live schema for exact field names — <https://docs.ahrefs.com/api/reference/management> — the report/prompt bodies have evolved. A freshly created report has no snapshots yet; give it a while before reading overview or citation data.

Reading citations, once it exists:

```bash
curl -s "https://api.ahrefs.com/v3/brand-radar/cited-pages?select=<cols>&data_source=chatgpt&brand=yourbrand" \
  -H "Authorization: Bearer $AHREFS_API_KEY"
```

Filter to `reddit.com` to get the Reddit URLs AI engines cite about you. Requests returning only **custom-prompt** data are free; including Ahrefs prompt data consumes units.

---

## 4. What to put in your config

| Value | Where it comes from | Required |
|---|---|---|
| `AHREFS_API_KEY` | §1 | yes |
| `RT_PROJECT_ID` | §2 | yes (tracked-keyword source) |
| `TARGET_DOMAIN` | your site, e.g. `example.com` | yes |
| `BRAND_KEYWORDS` | your brand terms, comma separated | yes (brand source) |
| `BRAND_RADAR_REPORT_ID` | §3 | no |
| `MAX_SERP_POSITION` | `10` = first page only | default 10 |
| `COUNTRIES` | empty = all markets, or `us,gb` | default empty |
| `LLM_API_KEY` / `LLM_BASE_URL` | any OpenAI-compatible provider | only for AI notes |
| `APIFY_API_TOKEN` | <https://console.apify.com/account/integrations> | only for upvote counts |

None of these have a working default. The app refuses to scan and names the missing ones.

---

## 5. Sanity check before your first real scan

```bash
# 1. key + budget          (free)
curl -s ".../v3/subscription-info/limits-and-usage" -H "Authorization: Bearer $AHREFS_API_KEY"
# 2. project exists        (free)
curl -s ".../v3/management/projects" -H "Authorization: Bearer $AHREFS_API_KEY"
# 3. it has keywords       (free)
curl -s ".../v3/management/project-keywords?project_id=$RT_PROJECT_ID" -H "Authorization: Bearer $AHREFS_API_KEY"
# 4. ONE billable SERP call to prove the path works
curl -s ".../v3/serp-overview?keyword=your+brand&country=us&top_positions=10&select=position,result_type,url,title" \
  -H "Authorization: Bearer $AHREFS_API_KEY"
```

Steps 1–3 are free, so run them first every time. Only step 4 costs units.
