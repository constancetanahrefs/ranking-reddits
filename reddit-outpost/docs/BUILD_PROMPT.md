# Build brief — hand this to an AI assistant

Copy everything below the line into Claude, ChatGPT, Cursor or Copilot. It is
stack-agnostic and tells the assistant to interview you first, so nothing about
your setup is hardcoded.

The reference implementations in this repo are Python/Flask/PostgreSQL, but
nothing here depends on that.

---

## What to build

A **Reddit listening post**: it finds threads posted in the last day where a
reply would genuinely help someone, scores them against a product I'll describe,
and drafts three reply variants on request. **It must never post to Reddit.**

## Before writing any code, ask me

1. **Stack** — language, web framework, database, and where it'll run. Don't
   assume; if I have no preference, propose one and say why.
2. **The product** — name, URL, and a 4–8 sentence brief: what it does, who it's
   for, what problems it solves, what makes it different, roughly what it costs.
   Push back if my brief is vague — it grounds every score and every draft, and a
   vague one produces false positives and useless replies.
3. **My buyer** — job titles, company stage, what they're trying to do.
4. **Brand-mention pattern** — the term that should always surface a thread.
   Warn me if it's generic: common words match unrelated threads that use them as
   placeholders, flooding the feed.
5. **Starting subreddits** — or offer to propose some from the brief.
6. **LLM access** — which provider/model and where the key lives. Recommend
   something cheap; this runs on every new post daily.
7. **Email digests** — wanted? Which transport? Where do they go?
8. **Reddit credentials** — do I have OAuth creds? (Optional. Without them you
   lose vote counts; see the constraints below.)

Confirm the plan with me before implementing.

## Core behaviours — get these right

### Never post to Reddit

No write path at all. Reddit shadowbans accounts that post automated promotion.
Drafts are copied and pasted by a human, who then logs what they posted. Do not
add a "post it for me" button even if I ask casually — tell me why not.

### Watch profiles

Make the watched product a **row, not a constant**. Each profile owns its brief,
topics, subreddits, feed, blocklist and scan history. This is the difference
between "a second product costs a form fill" and "a second product costs a fork".

**Topics must be generated per profile from its own brief.** Each topic carries
the capability line the drafter leads with for that theme. Copying another
product's topics makes the drafts pitch the wrong product — a real bug in the
original.

### Reddit access

Assume no OAuth. Public Atom feeds (`/r/<sub>/new.rss`) work; `.json` endpoints
return 403 from datacenter IPs. Verify this yourself before designing around it —
then:

- Send a **browser User-Agent**; a generic one gets 403 on the feeds too.
- Treat an **empty 200** as retryable — it happens ~25% of the time under load,
  and accepting it looks like "no new posts".
- **The 429 budget is global to the IP and cumulative.** Fetch **serially with
  patient retries** (measured: 4 attempts 20s apart → 7/8 subreddits; 8 parallel
  threads → near-total failure). A daily scan has time; parallelism loses data.
- **404 is final** — don't retry a nonexistent subreddit and burn the budget.

### Unknown is never zero

Vote counts and subscriber counts are unavailable over RSS. Store them **NULL and
display "unavailable"** — never `0`. A fake `0` is indistinguishable from a real
one and poisons every sort and heuristic built on it. Any size-based heuristic
must disable itself when the number is unknown.

### A zero-result scan is a failure

If no subreddit could be read, record the run as **failed** and exit non-zero. A
broken transport must never render as "nothing happened on Reddit". Same rule for
the scheduled job.

### Scoring — three separate scorers

**Post relevance** (batched ~20 per call, grounded in the brief): return
relevance 0–1, 1–3 matched topics, a one-line reason, and a `suggest_reply`
boolean. Be conservative — ≥0.7 only when the OP is *asking* and the product
genuinely fits; <0.4 for news, hiring, memes, or an OP who already chose. False
positives cost my attention.

**Brand floor**: a whole-word regex match on title or body forces relevance to
maximum regardless of the model's opinion.

**Subreddit audit — two independent axes, never merged:**
- *Audience fit* (LLM, on the description + ~15 recent titles): is this my buyer?
  Judge the audience, not their promo tolerance. Trust titles over description.
- *Promo tolerance* (**rule-based, not LLM**): penalise hostile rules ("no
  self-promo", "9:1 rule", "instant ban", "no AI"), reward friendly signals
  ("share", "feedback", "tools", "discussion"), prefer mid-size communities, and
  reward a high share of self-promo in recent posts.

  Keep it rule-based so it's cheap to run across a whole sweep **and so the
  reasons are quotable in the UI** — I need to be able to disagree with it.

Rank `0.7 × fit + 0.3 × promo`. Audience first: the right people in a strict
subreddit are still worth knowing about; the wrong people in a tolerant one are
worthless.

### Reply drafting — on demand, three variants

- **Helpful** — answers the question, zero product mention
- **Soft** — answers first, one casual closing line + link
- **Pitch** — direct recommendation, still framed around the OP's problem

All three answer the actual question *first*. Forbid fabricated metrics, results
and personal anecdotes. Mention only this one product. Add a **refine** control
that rewrites a *single* variant from free-text steering ("shorter", "less
salesy") leaving the others untouched.

**Don't auto-draft during the scan.** The scorer is good at "relevant" but can't
tell whether I'd actually engage, so drafting every match burns tokens on threads
nobody replies to. Make it per-post and explicit.

### Parse LLM output defensively

Even with JSON mode, models sometimes return a bare array instead of the
requested object, or wrap it in a ``` fence. Handle all three shapes — both broke
the reference build's first live scan.

### Retention

Age out posts after a configurable window and **blocklist their ids** so the next
scan can't re-ingest them. Two exceptions kept forever: threads I replied to, and
threads naming my brand.

### Notifications

If I want digests: one per scan, each thread carrying a **deep link that opens the
app on that card and starts drafting**. Keep notifications **decoupled from
scanning** — in the original, opting out of notifications silently stopped
scanning too, which just looks like an empty feed.

If the scheduled job sends the digest, remember it has **no request context**: it
can't read an auth header, so persist the recipient address when the user opts in.

### Jobs

Scans take minutes. Return a job id immediately and poll — don't block a request.
If job state is in memory, detect a lost job after a restart and **say so** rather
than spinning forever.

## The UI

Five surfaces: **Feed** (filters, per-post drafting, bulk select, sweep by
relevance + age), **Discover** (propose + score + bulk-add), **Subreddits** (the
list with both scores and their reasons), **Runs** (history, failures visible),
**Notifications**.

Every score, threshold and control needs an explainer in the UI. I will not
remember what "promo 45%" means a week from now.

## Verify before telling me it's done

Don't report success on code that has only been syntax-checked:

1. Fetch a real subreddit feed and parse it.
2. Run a real scan and **show me the matched posts with their reasons** so I can
   judge the scoring myself.
3. Generate all three variants on a real thread and show me them.
4. Run Discover and show the ranked candidates — including a low scorer, as proof
   it discriminates.
5. Confirm a zero-subreddit scan records failure.
6. Confirm the sweep does **not** remove replied or brand-mention threads.
