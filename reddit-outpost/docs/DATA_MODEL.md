# Data model — eight tables

PostgreSQL. JSON columns for topics/audit/log, so no SQLite.

## `outpost_profiles` — one product being watched

The unit of isolation: each profile has its own subreddits, feed, blocklist and
scan history.

| Column | Why |
|---|---|
| `product_name`, `product_url` | what the drafter recommends and links |
| `brief` | **the most important string in the app** — grounds every score and draft |
| `brief_updated_at` | surfaced in the UI so a stale brief can't rot invisibly |
| `topics` (JSON) | `[{id, label, emoji, pitch}]` — scoring targets **and** the capability line the drafter leads with. Generated per profile; copying another product's topics makes drafts pitch the wrong thing |
| `brand_regex` | whole-word pattern forced to max relevance; empty disables |
| `audience` | who must be in a subreddit for it to be worth monitoring (feeds the audit) |
| `relevance_floor` | match threshold, default 0.5 |
| `lookback_hours` | default 26 — deliberately > 24 so a daily scan overlaps and nothing slips between runs |
| `retention_days` | how long non-protected posts survive |

## `outpost_subreddits` — the monitored list

Unique on `(profile_id, name)`, so two profiles watch the same subreddit
independently.

| Column | Why |
|---|---|
| `topical_fit`, `promo_friendly` | the two audit axes, NULL until audited |
| `subscribers` | **NULL, never 0** — unknowable over RSS |
| `audit` (JSON) | the reasons behind both scores + sampled titles, so the UI can quote them |
| `audit_error` | a failed audit records why instead of writing fake scores |
| `enabled` | skipped by scans but kept in the list |

## `outpost_posts` — one scanned post

Unique on `(profile_id, reddit_id)`.

| Column | Why |
|---|---|
| `upvotes`, `num_comments` | **NULL = not fetched.** Never 0-as-unknown |
| `engagement_status` | distinguishes "never tried" from "tried and failed" |
| `relevance`, `topics`, `reasoning`, `suggest_reply` | the scorer's output, kept for auditing its judgement |
| `matched` | cleared the floor **and** suggest_reply — the queue flag |
| `brand_mention` | hit the brand floor; also **protects the row from sweeps** |
| `status` | `new` / `done` (replied) / `dismissed`; `done` is protected from sweeps |
| `created_utc` | NULL renders "date unknown" rather than a fake date |

## `outpost_drafts` — reply variants

`variant` is `helpful` / `soft` / `pitch`. `refine_note` records the steering
instruction, so you can see why a draft reads the way it does. Cascades on post
delete.

## `outpost_actions` — the audit trail

What a human actually posted: `comment_url`, `body`, `actor`, timestamp. The app
never posts, so this is the *only* record that engagement happened.

## `outpost_blocked` — never show this again

Unique on `(profile_id, reddit_id)`. Deleting or sweeping a post adds its id here,
and the scan's dedupe checks it — otherwise the next scan happily re-ingests
everything you just cleared.

## `outpost_runs` — scan history

`subs_scanned`, `posts_seen`, `posts_new`, `posts_matched`, plus a JSON `log` with
a per-subreddit entry. **`status='failed'` when zero subreddits could be read** —
a broken transport must never look like a quiet day.

## `outpost_notify` — per-user prefs

| Column | Why |
|---|---|
| `enabled` | notifications **only**; it never disables scanning (the original coupled them, silently emptying feeds) |
| `session_email` | captured from the authenticated session at opt-in. A cron run has **no request context** and can't read an auth header — without this column the digest has no recipient. This was a real bug: falling back to `user_id` meant a UUID, and every send was skipped |
| `email_override` | send elsewhere |
| `matched_only` | include rejects too (usually far too noisy) |
| `last_error` | last delivery failure, shown in the UI |

## Protected from retention sweeps

Two exceptions survive forever:

1. `status = 'done'` — you replied; it's the audit trail
2. `brand_mention = true` — someone named you

Everything else ages out past `retention_days` and lands on the blocklist.
