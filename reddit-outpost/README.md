# Reddit Outpost

**Find the Reddit threads where replying would genuinely help — and where
mentioning your product won't get you banned.**

Most "Reddit monitoring" tools alert on keywords. That gets you a firehose of
threads that merely *contain* your word. This scores each new post on a harder
question: does this person have a problem you actually solve, are they asking for
help, and would a reply be welcome here?

Then it drafts three replies — and **never posts them**.

![Reddit Outpost — the Feed tab: a matched thread at 75% relevance with its topic tags and the scorer's reasoning, above a generated "Helpful" reply variant that answers the question with no product mention](docs/screenshot.png)

> **Looking for threads that rank in Google instead?** That's the sibling app,
> [Ranking Reddits](../ranking-reddits/) — same repo, opposite end of a thread's life.

## What makes it different

**It scores the subreddit, not just the post.** The Discover tab proposes
communities where your buyers actually are, then rates each on two independent
axes:

- **Audience fit** — is this your buyer? (LLM, judged on the description and 15 recent posts)
- **Promo tolerance** — would a tool mention get you banned? (rule-based: hostile
  rules like *"no self-promo"*, *"9:1 rule"*, *"instant ban"* vs. friendly signals
  like *"share"*, *"feedback"*, *"tools"*)

Ranked `0.7 × fit + 0.3 × promo` — audience first, tolerance second. The two axes
matter separately: r/sales scored **95% fit but 38% promo** in testing. Right
people, wrong place to drop a link.

**It never posts.** There is no write path to Reddit, deliberately — Reddit
shadowbans automated promotion. You copy a draft, paste it yourself, then log it
for the audit trail.

**Unknown is never zero.** Upvotes and subscriber counts stay `NULL` and display
as "unavailable" when they can't be fetched, rather than rendering a confident
`0`.

**A zero-result scan is a failure, not a quiet day.** If no subreddit could be
read, the run is recorded as failed — a broken transport must never look like
"nothing happened on Reddit".

## The five tabs

| Tab | What it does |
|---|---|
| 📥 **Feed** | Scored posts with matched topics and the scorer's reasoning. Generate 3 reply variants, refine any one with free-text steering ("shorter", "less salesy"), log what you posted, bulk-delete, sweep by relevance + age. |
| 🔎 **Discover** | Propose + score candidate subreddits. Bulk-add the ones that clear both thresholds. |
| 📂 **Subreddits** | The monitored list with fit/promo pills and the audit's reasons quoted. Re-audit, enable, remove. |
| 🕒 **Runs** | Scan history, with per-subreddit failures expandable. |
| 🔔 **Notifications** | Daily email digest. Each thread carries a deep link that opens the app on that card and starts drafting. |

## Watch profiles

One product per profile: its own brief, topics, subreddits, feed and blocklist.
Topics are **generated from that profile's brief**, and carry the capability line
the drafter leads with — so a second profile can't inherit the first one's pitch.
(That bug was real: a HubSpot profile once pitched a competitor's product because
it copied its topics.)

## The three reply variants

- 🤝 **Helpful** — answers the question, no product mention at all
- 💬 **Soft** — answers first, one casual closing line plus the link
- 🎯 **Pitch** — a direct recommendation, still framed around the OP's problem

All three answer the actual question before doing anything else. That's the whole
trick to not being downvoted.

## Pick your build

| | [`portable/`](portable/) | [`letaido/`](letaido/) |
|---|---|---|
| **For** | Anyone, any stack | Letaido workspaces |
| **Needs** | PostgreSQL + an OpenAI-compatible LLM key | Nothing — the workspace provides it |
| **Runs** | `docker compose up` | Drop-in Console app |
| **Auth** | None (localhost / your own proxy) | Workspace SSO via nginx headers |
| **Email** | SMTP / Resend / console | Platform-managed |
| **Status** | Standalone, tested | Production source |

Building it yourself with an AI assistant? Point it at
**[`docs/BUILD_PROMPT.md`](docs/BUILD_PROMPT.md)** — a stack-agnostic brief that
tells the assistant to interview you first, so nothing is hardcoded.

## Documentation

- **[docs/BUILD_PROMPT.md](docs/BUILD_PROMPT.md)** — hand to Claude / ChatGPT / Cursor to rebuild from scratch
- **[docs/REDDIT_ACCESS.md](docs/REDDIT_ACCESS.md)** — how to read Reddit from a server in 2026, and the rate limits that shape the design
- **[docs/SCORING.md](docs/SCORING.md)** — the relevance pipeline, the brand floor, and the two-axis subreddit audit
- **[docs/DATA_MODEL.md](docs/DATA_MODEL.md)** — the eight tables and why each field exists
- **[portable/README.md](portable/README.md)** — run the standalone build
- **[letaido/README.md](letaido/README.md)** — install into a Letaido workspace

## Credit

Adapted from a colleague's internal "Agent A Reddit Outpost". The subreddit
audit, the three-variant drafter, the brand floor and the never-post rule are
their design. Changed here: per-product watch profiles instead of one hardcoded
product, a real schema, email instead of Slack, serial fetching for
harder-rate-limited hosts, and notifications decoupled from scanning.

## License

MIT — see [LICENSE](../LICENSE).
