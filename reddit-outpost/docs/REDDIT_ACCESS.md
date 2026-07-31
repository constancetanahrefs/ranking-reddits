# Reading Reddit from a server in 2026

Everything here was **measured**, not assumed — from a datacenter IP on
2026-07-31. Re-run the checks yourself before trusting them; Reddit changes.

## What works and what doesn't

| Endpoint | Result | Gives you |
|---|---|---|
| `/r/<sub>/new.rss` | **200** (browser UA required) | post id, title, author, published, body |
| `/r/<sub>/about.json` | **403** | — |
| `/r/<sub>/new.json` | **403** | — |
| `/comments/<id>.json` | **403** | — |
| Rendered HTML page | 200 via a residential proxy | upvotes, comments, ratio |
| OAuth API (PRAW) | works with credentials | everything, properly |

Reddit blocked anonymous `.json` from datacenter IPs in 2023. The Atom feeds were
never closed.

## The four constraints that shape the design

**1. A browser User-Agent is mandatory.** A library default or a custom
`MyApp/0.1` UA gets 403 on the feeds too. This app sends a Safari UA.

**2. An empty 200 is a real response.** Roughly 1 request in 4 under load returns
status 200 with a zero-length body — no feed, no error. Accepting it reads as
"this subreddit has no new posts", which silently loses data. Treat an empty or
`<feed>`-less 200 as retryable.

**3. The 429 budget is global to your IP and cumulative.** This is the big one,
and it's easy to get wrong because a single subreddit tested in isolation always
works. Measured:

| Strategy | Outcome |
|---|---|
| 8 subreddits in parallel | fails almost entirely |
| Serial, 5s apart | ~1 in 5 succeed |
| Serial, 30s apart | ~50% succeed |
| **4 attempts, 20s apart, serial** | **7/8 subreddits, 26 requests** |

Patience beats concurrency, decisively. Fetching in parallel to finish sooner
just means half your subreddits silently return nothing — and if you're scanning
once a day, you have all the time in the world.

**4. A 404 is final.** A subreddit that doesn't exist (or is private) will never
succeed, so retrying it burns budget the real subreddits need. Short-circuit it.

## Consequences for your data model

The feeds carry **no vote data and no subscriber counts**. So:

- Store them **NULL and display "unavailable"** — never `0`. A `0` is
  indistinguishable from a real zero and quietly poisons any sort or heuristic
  built on it. (The app this was adapted from stored `0` and its own audit
  admitted the engagement column was "meaningless".)
- Any subscriber-based heuristic must **disable itself** when the number is
  unknown, rather than treating unknown as tiny.
- Subreddit rules text is unavailable too, so promo-friendliness scoring falls
  back to the public description in the feed's `<subtitle>`.

Relevance scoring and reply drafting only need title + body, so **the app is
fully functional on feeds alone** — you just lose engagement numbers.

## Getting the missing numbers

Two options, both optional:

- **Reddit OAuth (PRAW)** — the correct answer. Free, generous limits, gives you
  everything. `app/reddit.py` is the only file you'd change.
- **Render the page** — a headless browser through a *residential* proxy, parsing
  the `shreddit-screenview-data` JSON blob for `score`, `number_comments` and
  `upvote_ratio`. Works, but Reddit serves a login wall roughly half the time, so
  retry ~3×. Slower and costs money.

## Verifying it yourself

```bash
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 \
(KHTML, like Gecko) Version/17.0 Safari/605.1.15"

# should be 200 with <entry> elements
curl -s -o /tmp/f.xml -w "%{http_code}\n" -H "User-Agent: $UA" \
  "https://www.reddit.com/r/SEO/new.rss?limit=5"
grep -o '<entry>' /tmp/f.xml | wc -l     # note: grep -c counts LINES, and the
                                          # feed is one line — this bit me

# should be 403
curl -s -o /dev/null -w "%{http_code}\n" -H "User-Agent: $UA" \
  "https://www.reddit.com/r/SEO/about.json"
```
