# Ranking Reddits

**Find and read the Reddit threads that rank for your brand.**

Most "Reddit listening" tools watch subreddits and match keywords. This one starts from **search visibility** instead: it finds the Reddit threads that are *actually ranking* on Google for the keywords you care about — the ones real people (and AI answer engines) hit when they research your product.

Each thread becomes a **card**. Click it and the thread opens in-app: it's marked read, the title / upvotes / comment count / body + top comments are fetched, and an AI drafts short reading notes in two angles — **Brand** (what's said about you, sentiment, is it worth replying) and **Content** (what question isn't answered, and the article idea). A monthly worker re-checks both sources and flags new URLs as NEW.

![Ranking Reddits — the card wall, showing a project with 345 Reddit threads ranking for hubspot.com, split across Brand Radar SERP visibility and tracked keywords](docs/screenshot.png)

## Multiple projects, one app

A **project switcher** at the top; each project is an independent scope with its own
cards, read state and scan history. A **3-step setup wizard** pulls your live Ahrefs
Rank Tracker projects so you never type an id: pick the project, confirm scope, and
— if it tracks no keywords yet — pick from suggestions drawn from what the target
already ranks for, optionally **adding them to Rank Tracker via the API**.

The same thread can rank for two projects and is a separate card in each.

> **Looking for fresh threads to reply to today?** That's the sibling app,
> [Reddit Outpost](../reddit-outpost/) — same repo, opposite end of a thread's life.

## Two discovery sources

| Source | What it finds |
|---|---|
| **Brand SERP visibility** | Reddit threads ranking on SERPs where your brand / domain is the tracked entity — i.e. threads competing with you for your own brand queries |
| **Tracked keywords** | Reddit pages sitting in the **top 10** for the keywords you already track in Ahrefs Rank Tracker |

A thread found by both keeps one card with two source badges.

## Pick your build

This repo ships **two implementations of the same product**:

| | [`portable/`](portable/) | [`letaido/`](letaido/) |
|---|---|---|
| **For** | Anyone, any stack | Letaido workspaces |
| **Ahrefs access** | Ahrefs **API v3** (`api.ahrefs.com`, Bearer token) | Ahrefs **connectors** (OAuth, no token handling) |
| **Runs** | `docker compose up` or plain Flask + Postgres | Drop-in Console app |
| **Status** | Standalone, runnable | Production source, running today |

Building it yourself with an AI assistant? Point your assistant at **[`docs/BUILD_PROMPT.md`](docs/BUILD_PROMPT.md)** — a complete, stack-agnostic build brief. It tells the assistant to interview you for your own account details first, so nothing is hardcoded.

## Documentation

- **[docs/BUILD_PROMPT.md](docs/BUILD_PROMPT.md)** — give this to Claude / ChatGPT / Cursor / Copilot to build the app from scratch in any stack
- **[docs/AHREFS_SETUP.md](docs/AHREFS_SETUP.md)** — what to set up in Ahrefs first (API key, Rank Tracker project, Brand Radar report), including how to create them **via the API**
- **[docs/API_MAPPING.md](docs/API_MAPPING.md)** — Letaido connector → Ahrefs API v3 endpoint, cap by cap, with the gaps called out
- **[docs/REDDIT_ACCESS.md](docs/REDDIT_ACCESS.md)** — how to get titles, upvotes and bodies when Reddit 403s your server
- **[docs/DATA_MODEL.md](docs/DATA_MODEL.md)** — the five tables and why each field exists
- **[portable/README.md](portable/README.md)** — run the standalone app
- **[letaido/README.md](letaido/README.md)** — install into a Letaido workspace

## Before your first scan — check the cost

Scanning calls Ahrefs SERP Overview once per keyword per market, and that consumes API units. The portable build ships a pre-flight that uses **only free endpoints** to verify your setup and estimate the spend first:

```bash
python3 scripts/preflight.py
```

It prints your unit budget, your keyword count, the number of billable calls a scan will make, and the minimum units that costs. Run it before you spend anything.

## Requirements

- **Ahrefs paid plan, Lite or above** — API v3 is not available on Free or Starter. Rank Tracker + Management endpoints are **free** (no API units); SERP Overview and Brand Radar **consume units**.
- **A Rank Tracker project** with keywords in it, or your own keyword list.
- **Postgres** (the portable app) — no SQLite, because concurrent enrichment workers need real locking.
- **An LLM API key** for the reading notes (any OpenAI-compatible endpoint). Optional: the app works without notes.
- **Optional: an Apify token** — only needed for upvote counts. Everything else works without it. See [docs/REDDIT_ACCESS.md](docs/REDDIT_ACCESS.md).

## Nothing here is pre-filled with someone else's account

Every account-specific value — API keys, Rank Tracker project id, Brand Radar report id, target domain, brand terms — is read from config with **no default that points at a real account**. On first run the portable app refuses to scan and tells you exactly which values are missing. The build prompt makes your AI assistant ask you for them.

## Honest limitations

- **The public Ahrefs API v3 has no Reddit-in-SERPs endpoint.** Brand Radar's public surface exposes AI visibility, citations and overview stats — the Reddit index that powers the Letaido version's `reddit_results` cap is not published as a REST endpoint. The portable build gets the same *outcome* by calling **SERP Overview per keyword and keeping the reddit.com results**. Trade-offs (unit cost, per-engine AI-citation counts you lose) are in [docs/API_MAPPING.md](docs/API_MAPPING.md). Check the live docs before assuming — Ahrefs ships endpoints regularly.
- **Reddit blocks server-side fetches.** `.json`, `old.reddit.com` and browser user-agents all return 403 from a datacenter IP. The `.rss` feed works and gives title, author, date, body and comments; **upvote counts** need a rendered page (see [docs/REDDIT_ACCESS.md](docs/REDDIT_ACCESS.md)).
- **Unknown is never zero.** A thread whose vote count couldn't be read shows "not fetched", not `0 upvotes`. Likewise a scan returning zero rows is recorded as a **failure**, not a quiet month — that distinction is the whole reason you can trust a "0 new threads" result.

## License

MIT — see [LICENSE](../LICENSE).
