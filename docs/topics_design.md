# Multi-topic conversation system — design (v0, 2026-07-22)

Designed by a 5-agent brainstorm, grounded in and verified against the hermes/saandal source.
Ships as a new fail-safe sibling module `plugin/topics.py`, entirely behind
`tools.tool_search.topics.enabled` (default **off**) — deploying the code is inert until the flag flips.

## One turn, end to end (every step `try/except → None/no-op` = stock hermes)

1. **`pre_llm_call`** `_tp_pre_llm(...)` — fires once/turn in `build_turn_context`
   (conversation_loop.py:576), on the per-session worker thread (off the gateway loop):
   - flag-off or `_in_background_review` → return None (stock)
   - parse `/topic …` overrides (no LLM)
   - skip-gates (first turn / single-topic continuation) → no LLM
   - else load mtime-cached `<wiki>/topics/_index.json` (≤8 open one-liners) → one
     `ctx.llm.complete_structured` **classify** call → `TopicDecision{id,action,confidence,title,entities}`
   - continuity-biased thresholds (switch ≥0.55, new ≥0.6), cold-match dormant topics, LRU-evict to ≤8
   - build the **budget-capped** block and `return {"context": block}` → hermes appends it to the
     **current-turn user message** (turn_context.py:113/495), **not** the system prompt → cached
     system prefix stays byte-stable (the ~846 tokens we just trimmed are never re-inflated)
2. Agent runs — topics registers **no** `llm_request` middleware, so the existing
   `_cd → selfheal → progress → loadaware` chain is byte-for-byte untouched.
3. **Badge** `t#00042` — composed into selfheal's sole `transform_llm_output` finisher via the
   host dict (like the existing `scrub` bridge; transform hooks don't chain), applied last, stripped
   for `voice_only` sessions.
4. **`post_llm_call`** `_tp_post_llm(...)` — write-back: append `## log`, harvest this turn's
   web_search/web_extract findings (`selfheal.harvest_findings`) into `## findings` with `[src: url]`
   provenance; amortized summary roll-up only when `## log` crosses a threshold; on close/durable
   fact, drop an `inbox/hermes/topic-…md` curation note (merged by `tools/merge_inboxes.py`).

## Storage (llm-wiki, verified merge-safe)

Plugin-owned `<wiki>/topics/` subtree — `merge_inboxes.py` scans only `inbox/*/*.md` and
`wiki/<SECTIONS>/*.md`, never `topics/`.
- `topics/_index.json` — hot manifest, the only file read at classify time.
- `topics/t<NNNNN>.md` — frontmatter + `## summary` (≤200 tok) / `## facts` / `## findings` (with `[src:]`) / `## log`.
- Atomic tmp+`os.replace`; per-topic filenames so concurrent gateways never collide.
- Durable facts reach curated `wiki/` only via the existing inbox→merge protocol (never edits wiki/ directly).

## Budget & cost
- Injected block hard-capped at `block_token_cap` (default 250 tok), fixed section order,
  drop-order findings→summary, memoized per file mtime (byte-identical across a turn's tool loop).
- **One** small classify call/turn (aux-pinned cheap model, temp 0, max_tokens ~64, 3–4s timeout,
  skipped on most turns). Timeout/error → fall back to the active topic, never open new, never block dispatch.

## Phased plan (each shippable + tested behind the flag)
- **P0** — inert wiring + kill-switch + config namespace; zero behavior change.
- **P1** — heuristic routing + injection + badge (no LLM).
- **P2** — LLM classifier + ≤8 LRU eviction + `/topic` overrides.
- **P3** — durable disk store + dormant rehydration.
- **P4** — write-back + curation + verify-before-asserting provenance.
- **P5** — per-gateway hardening (voice badge-strip; adapter capture tests; mattermost deferred).

## Kill-switch
`tools.tool_search.topics.enabled` (default off). Sub-flags: `topics.badge=off`,
`topics.classify=heuristic` (no LLM call), `topics.summarize=off`. Any unhandled error → stock hermes.
