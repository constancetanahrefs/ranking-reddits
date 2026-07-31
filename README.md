# Reddit tooling for marketers

Two apps for finding Reddit threads worth your attention — from **opposite ends of
a thread's life**. They share a philosophy and nothing else: no shared code, no
shared database, each runs standalone.

| | [**Ranking Reddits**](ranking-reddits/) | [**Reddit Outpost**](reddit-outpost/) |
|---|---|---|
| Finds | Threads **ranking in Google** for your keywords | Threads **posted in the last day** |
| Source | Ahrefs (Brand Radar + Rank Tracker) | Reddit's public Atom feeds |
| Thread age | Evergreen — often months or years old | Hours |
| Cadence | Monthly | Daily |
| You do | Read, take notes, mine for content ideas | **Reply, today** |
| Lifecycle | A permanent library | 14-day retention |
| Needs | An Ahrefs subscription (Lite+) | An LLM key. No Ahrefs, no Reddit API key |

A thread ranking #4 for *"best CRM for small teams"* is a **content** signal — it
intercepts your buyers' searches indefinitely, and you probably want to write
something better. A thread posted 40 minutes ago asking *"which CRM should I
buy?"* is an **engagement** signal with a short fuse. Same platform, completely
different jobs.

## Ranking Reddits

Finds the Reddit threads that actually rank on Google for your brand and
keywords — the ones real people (and AI answer engines) hit when they research
you. Each becomes a card; opening it fetches the thread and drafts reading notes
in two angles, Brand and Content. Multi-project, with a setup wizard that pulls
your live Ahrefs Rank Tracker projects.

→ **[ranking-reddits/](ranking-reddits/)**

## Reddit Outpost

Scores every new post in your chosen subreddits on a harder question than keyword
matching: does this person have a problem you solve, are they asking for help, and
would a reply be welcome *here*? Audits subreddits on **audience fit** and **promo
tolerance** separately. Drafts three reply variants — and **never posts them**.

→ **[reddit-outpost/](reddit-outpost/)**

## Shared principles

Both were built the same way, and these are the parts worth stealing even if you
use neither:

- **Unknown is never zero.** Upvotes, comment counts and subscriber counts stay
  `NULL` and display as "unavailable" when they can't be fetched. A confident `0`
  is indistinguishable from a real zero and quietly corrupts every sort built on
  it.
- **A zero-result scan is a failure, not a quiet day.** If no source could be
  read, the run is recorded as failed and the scheduled job exits non-zero. A
  broken transport must never render as "nothing happened".
- **Never post automatically.** Reddit shadowbans automated promotion. Drafts are
  copied and pasted by a human, who then logs what they posted.
- **Patience beats concurrency on Reddit.** The rate-limit budget is global to
  your IP and cumulative — parallel fetching silently loses half your data. See
  [reddit-outpost/docs/REDDIT_ACCESS.md](reddit-outpost/docs/REDDIT_ACCESS.md) for
  the measurements.

## Each app ships two builds

| | `portable/` | `letaido/` |
|---|---|---|
| For | Anyone, any stack | [Letaido](https://letaido.com) workspaces |
| Runs | `docker compose up` | Drop-in Console app |
| Status | Standalone, tested | Production source |

And a **`docs/BUILD_PROMPT.md`** you can hand to Claude, ChatGPT or Cursor to
rebuild either app from scratch in your own stack — it interviews you for your own
account details first, so nothing is hardcoded.

## License

MIT — see [LICENSE](LICENSE).
