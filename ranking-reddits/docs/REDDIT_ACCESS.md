# Getting thread details out of Reddit

Ahrefs tells you *which* Reddit URLs rank. It doesn't give you the thread's upvotes or full body. That part you fetch from Reddit — and Reddit does not make it easy from a server.

## What we actually tested (from a datacenter IP)

| Approach | Result |
|---|---|
| `GET /r/<sub>/comments/<id>/.json` | **403** |
| Same with a real Chrome / Safari user-agent | **403** |
| Same with a descriptive bot UA + contact | **403** |
| `old.reddit.com/.../.json` | **403** |
| Rendered HTML via plain `curl` | **403** |
| **`GET /r/<sub>/comments/<id>/.rss`** | **200** ✅ |
| Headless browser through a **residential** proxy | **200**, ~50% of the time (login wall otherwise) |

So: **two halves, from two sources.**

---

## Half 1 — text, from the Atom feed (free, ~2 seconds)

```
GET https://www.reddit.com/r/<sub>/comments/<id>/.rss
User-Agent: your-app/1.0
Accept: application/atom+xml
```

The feed's first `<entry>` is the post; the rest are comments. From it you get:

| Field | Where |
|---|---|
| Title | first `<entry><title>` |
| OP username | first `<entry><author><name>` → `/u/name` |
| Real publish date | first `<entry><published>` (ISO 8601) |
| Post body | first `<entry><content type="html">` |
| Top comments | remaining `<entry>` blocks — `<author><name>` + `<content>` |

Two things to handle:

1. **Strip the chrome.** Every entry's content ends with Reddit's own `submitted by /u/x [link] [comments]` boilerplate. Remove it or it pollutes your LLM context and your UI.
2. **It rate-limits.** Rapid repeats return **429**. Back off (we retry 3× with increasing sleeps) and space out batch jobs (~6s between threads). This is the single biggest cause of "it worked once then stopped".

The feed carries **no vote data at all**. That's half 2.

## Half 2 — upvotes, from a rendered page

Reddit's server-rendered HTML embeds a JSON blob in a `<shreddit-screenview-data>` element containing, HTML-entity-escaped:

```json
{"...":"...","number_comments":55,"score":17,"upvote_ratio":0.869,"created_timestamp":1686693518335}
```

So: render the page (headless browser) through a **residential** proxy, HTML-unescape the response, and regex those four keys out. We used Apify's `apify/website-content-crawler` actor with `crawlerType: playwright:firefox` and `apifyProxyGroups: ["RESIDENTIAL"]`, but any managed-browser service works — this is not Apify-specific.

**Retry it.** The residential proxy hits Reddit's login wall roughly half the time, and the wall page has no vote data. We retry up to 3× and a card that failed first pass typically succeeds.

### The rule that matters

**A render with no vote data is a FAILURE, not zero upvotes.** If you write `0` you've silently corrupted your data and every "sort by upvotes" view afterwards lies. Raise, record the error on the card, and show "not fetched" in the UI. Same for `posted_at`: show "date unknown", never today's date.

## Cost shape

- Text half: free, ~2s.
- Vote half: one managed-browser run per thread, ~20s, a fraction of a cent.

So **make enrichment lazy** — fetch when a user opens a card, plus a bounded "fetch next N" batch button. Never auto-enrich a whole scan: 275 cards × 20s is 90 minutes and real money for data nobody asked for.

## Special case: subreddit landing pages

Sometimes the ranking URL is a **subreddit**, not a thread — `reddit.com/r/yourbrand/` ranking for your brand name. Its feed describes whatever is newest in the sub, so enriching it would put a random post's title and upvote count on that card.

Detect it (a thread URL contains `/comments/<id>`; a landing page doesn't) and **skip enrichment with an explicit message**. Don't fake it.

## Alternatives worth knowing

- **Reddit's official OAuth API** (`oauth.reddit.com`) is the sanctioned path: register an app, get a client id/secret, and you get `ups`, `upvote_ratio`, `num_comments` and full comment trees cleanly, with documented rate limits. If you're happy to register an app, this replaces *both* halves and is the better long-term choice. It's not used here because it needs per-user credential setup.
- **Pushshift**-style third-party archives: historical, incomplete, and their availability has changed repeatedly. Not recommended as a primary source.

## URL normalisation — do this before anything else

The same thread arrives with many URL spellings (trailing slash, `?utm_*`, `www` vs not, comment-permalink suffixes). Dedupe on the canonical pair `<subreddit>/<thread_id>` extracted from `/r/<sub>/comments/<id>`, not on the raw URL — otherwise one thread becomes four cards.
