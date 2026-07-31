# How scoring works

Three independent scorers. Each answers a different question, and keeping them
separate is what stops the feed filling with noise.

## 1. Post relevance — "would a reply land here?"

Batched, 20 posts per LLM call, grounded in the profile's product brief.

Returns per post: `relevance` 0–1, 1–3 matched `topics`, a one-line `reason`, and
`suggest_reply`.

The thresholds in the prompt are deliberately conservative:

| Score | Meaning |
|---|---|
| ≥ 0.7 | The OP is asking for help/advice/tool recs **and** the product genuinely fits |
| 0.4–0.69 | Tangentially relevant — a soft mention could fit but isn't perfect |
| < 0.4 | Off-topic, news without a question, hiring, memes, drama, other vendors' pitches, or the OP already chose a tool |

`suggest_reply` is true only when relevance ≥ 0.5 **and** there's a clear question
you'd be welcome to answer. A post is **matched** when it clears the profile's
relevance floor *and* `suggest_reply`.

Why conservative: false positives cost the user's attention, which is the scarce
resource. From a live 30-post scan, 2 matched — and the rejects were right
("Hermes sucks" → 0.3, using AI to stack data-entry jobs → 0.4).

**The brief does the heavy lifting.** It's the only thing telling the model what
"genuinely fits" means. A vague brief produces both false positives and useless
drafts, which is why the app timestamps it and shows the age.

## 2. The brand floor — never miss a direct mention

A whole-word regex match on title or body forces `relevance = 1.0` and
`suggest_reply = true`, whatever the model thought.

**Choose the pattern narrowly.** In the original build the product was two common
words, and matching them caught every unrelated thread using them as
placeholders — so it was narrowed to the company name alone. A generic product
name will flood your feed. Empty disables the floor.

## 3. The subreddit audit — two axes, deliberately separate

Confusing these is the classic mistake. A community full of your buyers that bans
promotion is not the same as a tolerant community full of the wrong people.

### Audience fit (LLM, 0–1)

Judged on the description plus 15 recent titles. The prompt says: judge the
**audience**, not their tolerance for promotion; if recent titles contradict the
description, trust the titles; with almost no evidence return ~0.3 and say so.

### Promo tolerance (rule-based, 0–1)

Starts at 0.5, then:

| Signal | Effect |
|---|---|
| Hostile phrases — *no self-promo*, *no advertising*, *instant ban*, *9:1 rule*, *no AI* | −0.12 each, capped −0.35 |
| Friendly phrases — *share*, *showcase*, *feedback*, *discussion*, *tools*, *build in public* | +0.05 each, capped +0.2 |
| 5k–500k members | +0.1 (active, not a firehose) |
| < 1k members | −0.1 |
| > 2M members | −0.05 (a reply gets buried) |
| ≥30% of recent posts are self-promo | +0.15 (clearly tolerated) |
| No self-promo in recent posts | −0.05 |

Rule-based on purpose: it must be cheap enough to run on every candidate in a
sweep, and the reasons must be **quotable in the UI** so you can disagree with
them. Every subreddit card shows exactly which phrases fired.

Member-count rules **skip themselves** when the count is unknown (see
[REDDIT_ACCESS.md](REDDIT_ACCESS.md)) rather than treating unknown as tiny.

### Combined

```
0.7 × audience_fit + 0.3 × promo_tolerance
```

Audience first: a tolerant subreddit full of the wrong people is worthless, while
the right people in a strict subreddit are still worth knowing about — you just
reply without the link. Bulk-add pre-selects ≥0.7 fit **and** ≥0.5 promo.

Real output for a CRM product:

| | fit | promo | note |
|---|---|---|---|
| r/marketing | 95% | 45% | |
| r/sales | 95% | **38%** | right people, hostile to links |
| r/SaaS | 85% | 55% | |
| r/msp | 85% | 45% | *"MSPs are core buyers needing CRM tools"* |
| r/DataHoarder | **20%** | 50% | correctly rejected — hardware/archival, not marketers |

## Model choice

This runs on every new post, every day. A small fast model is right — the prompts
carry the logic. Reference builds use `gemini-3-flash-preview` (Letaido) and
`gemini-2.5-flash` (portable default).

Every call requests JSON mode. **Parse defensively anyway**: even with
`response_format={"type":"json_object"}`, models sometimes return a bare `[...]`
instead of `{"items":[...]}`, or wrap the whole thing in a ``` fence. Both broke
the first live scan. `_json_loads` + `_items_of` in `engine.py` handle all three
shapes.
