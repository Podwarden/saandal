# Session Self-Healing for hermes-agent 0.18.2 — Design

**Status:** DESIGN (verified against SP code + live-model experiments, 2026-07-19)
**Author:** design agent for the saandal out-of-tree plugin repo (`/home/user/saandal`)
**Model under care:** local-27b via vllm-local (`https://vllm.example.com/v1`, chat_completions)
**SP root (read-only):** `/home/user/.hermes/venv/lib/python3.11/site-packages/` — all `file:line` refs below are relative to it.

## 0. Executive summary / feasibility verdicts

| Question | Verdict |
|---|---|
| (a) Rewrite outgoing conversation history from out-of-tree code? | **YES** — `llm_request` middleware replaces the *entire* provider kwargs dict (messages, tools, response_format, everything). Wire-only; stored history untouched; re-applied on every API call, so durable for as long as the healer keeps applying it. |
| (a) Inject guidance the model sees as a *user* message mid-turn? | **YES (wire-level)** — append a `{"role":"user"}` message to `request["messages"]` in the middleware. Proven live (experiment C). **Caveat from experiment E:** injecting a "stop searching" user message while tools remain enabled stalled the endpoint 3/3 times — user-voice injection is only shipped *together with* tool-stripping (forced synthesis). Turn-start injection additionally available via the supported `pre_llm_call` `{"context": ...}` contract. |
| (b) Force-end a turn with a chosen synthesis ("strip tools")? | **YES — proven live on the real poisoned session.** Removing `tools` alone is NOT enough (model emits a literal `<tool_call>` text block); removing `tools`+`response_format` **plus a trailing corrective user message** produced a real 1,238-char Russian answer from the exact 65k-token failure history that had previously yielded `(empty)` and 7-char answers. The loop's no-tool-call branch then ends the turn with that text (conversation_loop.py:4786-4790). |
| (c) Session reset + re-dispatch from out-of-tree code? | **YES (in-process only).** The `pre_gateway_dispatch` hook hands the plugin the live `GatewayRunner` + `session_store` (gateway/run.py:8764-8770). From a captured reference the plugin can do everything the `/new` command and the CLI→gateway handoff watcher already do: `session_store.reset_session()`, `_evict_cached_agent()`, `_interrupt_and_clear_session()`, forge a synthetic `MessageEvent(internal=True)` and `await gateway._handle_message(...)`, and deliver via `adapter.send()`. **No out-of-process surface can do this** (dashboard 9119 has no per-session dispatch/reset API; TUI RPC serves a different process; state.db writing from outside is lock-risky and there is no generic command row). |
| (d) Post-turn detection without a turn-end hook? | **Partially in-process** (`transform_llm_output` + `post_llm_call` fire once per turn — but only when `final_response` is non-empty, turn_finalizer.py:343/365) **plus** watchdog-grade signals from `agent.log` ("Turn ended: reason=... response_len=..." at INFO, turn_finalizer.py:236) and `state.db` (WAL; read-only connections work; per-message rows with roles/timestamps). |
| Architecture | **Single plugin ("selfheal" module inside the router plugin repo), no separate watchdog process.** The plugin is simultaneously the sensor (hooks/middleware run in-process on every call) and the only possible actuator (gateway object access). An optional cron-style log watcher is relegated to alerting only. |

---

## 1. Feasibility map (verified)

### 1.a Mid-turn interventions

#### 1.a.1 `llm_request` middleware can rewrite conversation history — VERIFIED

* Contract: middleware returns `{"request": {...}}` and the returned dict **replaces the effective provider kwargs** (`hermes_cli/middleware.py:77-117`, `apply_llm_request_middleware`; result consumed at `agent/conversation_loop.py:1177-1191`, `api_kwargs = _llm_request_mw.payload`).
* The payload for this route is plain chat.completions kwargs: `{"model", "messages", "tools", "max_tokens", "response_format", "frequency_penalty", ...}` (transport builder `agent/transports/chat_completions.py:225+`; confirmed byte-level by the preserved request dumps, e.g. `~/.hermes/sessions/request_dump_20260718_173910_27cb7f_*.json` whose body keys are exactly `model, messages, tools, max_tokens, response_format, frequency_penalty`).
* The request is **deep-copied before and after every middleware callback** (`middleware.py:94-109`, `_safe_copy`) — the healer can freely drop/replace/append messages without corrupting hermes state.
* Context available to the middleware on every call: `task_id, turn_id, api_request_id, session_id, platform, model, provider, base_url, api_mode, api_call_count` (conversation_loop.py:1179-1190). So the healer sees the **full message list + turn identity + iteration count on every single API call of the turn** — this is both its sensor and its scalpel.
* Middleware exceptions are swallowed per-callback (`middleware.py:289-300` for execution chain; for request middleware a non-dict / missing `"request"` return is simply skipped, `middleware.py:104-107`) — a healer bug cannot break an API call, matching the router plugin's existing fail-safe posture.

**What a rewrite can do:**
1. **Append a corrective user message** at the tail (`...tool_result, {"role":"user","content": corrective}`) — the model sees it with user authority. Valid message ordering for OpenAI-compatible APIs; hermes's own empty-recovery nudge creates the same `tool → assistant → user` shape in *stored* history (conversation_loop.py:4900-4917), so the wire shape is precedented.
2. **Strip failure placeholders** — drop assistant messages whose content is `""`/`"(empty)"` and carry no `tool_calls`, drop the synthetic recovery nudges (their stored counterparts are marked `_empty_recovery_synthetic`, but that key is stripped before the wire — on the wire, match content patterns), drop guard-error tool results (`"duplicate query loop"`, `"duplicate call loop"`, `"search limit reached"`) **together with their paired assistant tool_calls entries** (pairing must be preserved: drop the assistant tool_call message and its tool results as a unit, or replace the tool result body with a one-line stub).
3. **Truncate poisoned middles** — replace runs of failed search rounds with a single synthetic tool-result stub ("[54 failed/duplicate searches elided]").

**Risks / limits (verified):**
* **Wire-only, per-request durability.** hermes rebuilds `api_messages` from its own `messages` list on every API call (conversation_loop.py:787-830) and persists `messages` to state.db untouched. A rewrite lasts exactly one request; durability comes from the healer **re-applying deterministically** on every subsequent call, keyed on `(session_id, turn_id)` state it owns. Within a turn this is fully reliable (middleware runs on every call, including retries — `_build_api_kwargs` + middleware are both inside the retry loop, conversation_loop.py:1099/1161/1177). Across turns it is equally reliable *as long as the plugin is loaded*, but the **stored** poison persists and re-poisons anything that reads raw history (compression summarizer, session_search, a future process without the plugin). Fresh-session retry is therefore the real fix for heavy poison; wire-stripping is a mid-turn palliative.
* **Prompt cache.** vLLM prefix caching: tail-appends preserve the whole cached prefix (zero cost). Mid-history drops invalidate the cache from the edit point — one-time re-prefill of the suffix (~65k tokens ≈ tens of seconds on this box). Acceptable at the FAILING stage; do not flap edits on and off between calls (keep the edit set monotonic within a turn).
* **hermes bookkeeping** — unaffected. The loop's own counters (`_empty_content_retries`, guardrails, compression) all read the *stored* `messages`, never the wire copy. One interaction to note: the token-estimate that drives compression uses API-reported `prompt_tokens` from the *rewritten* (smaller) request (conversation_loop.py:4747-4767) — stripping big chunks slightly delays auto-compression. Not harmful at 0.15 threshold; documented.
* **Constrained-decoding interplay (own plugin).** The router's `_cd_middleware` adds `response_format: structural_tag` when the request has tools. The healer must strip `tools`, `tool_choice`, **and** `response_format` in the same pass and must be robust to running either before or after `_cd_middleware` (if before: cd skips because tools are gone; if after: healer removes what cd added). Registration order within one plugin `register()` is deterministic — register the healer middleware **after** `_register_constrained_decoding` and strip all three keys anyway.

#### 1.a.2 `pre_tool_call` — block + teach (existing), no user-voice injection by itself

* Verified contract: a block returns `{"error": <message>}` as the tool result (`model_tools.py:1192-1208`) and emits `post_tool_call` with `status="blocked"`. The model sees it as a *tool error*, not a user message. It demonstrably reacts to these (v1.5.2 finding) but can also learn to ignore them (v1.5.4 incident).
* The hook receives ids only (`function_name, function_args, task_id, session_id, tool_call_id, turn_id, api_request_id`, model_tools.py:1179-1188) — no agent handle. **Its role in the healer: sensor + tripwire.** The user-voice escalation rides the next `llm_request` rewrite (1.a.1), which fires immediately after the blocked round returns to the loop.

#### 1.a.3 `pre_llm_call` — supported turn-start injection

* Fires once per turn in the prologue with `conversation_history=list(messages)` (agent/turn_context.py:464-515). A returned `{"context": "..."}` is appended to **the current turn's user message on the wire only** (conversation_loop.py:790-806, never persisted). This is the supported, cache-friendly channel for turn-start corrective context ("this session has a history of failed searches on this topic; answer from knowledge or search at most twice") once the healer's session-poison score is high.

### 1.b Turn-abort / forced synthesis — VERIFIED LIVE

**Loop behavior when a response has no tool calls:** the `else` branch at conversation_loop.py:4786-4790 takes `assistant_message.content` as `final_response` and (after empty-content recovery paths at 4798-4930 that don't trigger on real text) breaks; `finalize_turn` persists, logs "Turn ended", and returns the text as the turn's answer. **So a tools-less request whose response is real text cleanly ends the turn with that text.** No code after the middleware re-adds tools; the transport sends whatever kwargs it gets.

**Live experiment** (script: `exp_forced_synthesis.py` in this scratchpad; replayed the actual poisoned session `20260719_012254_e3b7f659` — 281 rows, 130 blank/`(empty)` assistant rows, cat-femur-fracture question, ~65-68k prompt tokens — against the production endpoint):

| Variant | Request | Result |
|---|---|---|
| A (control) | tools present | `finish=tool_calls` — model immediately fires yet another paraphrased `web_search`. **Pathology reproduces deterministically; A doubles as the test fixture.** |
| B | `tools` key removed, nothing else | `finish=stop` but content is a **literal `<tool_call>{"name":"web_search"...}</tool_call>` text block** — the turn would end delivering that garbage to telegram. Naive tool-strip is insufficient. |
| C | no tools **+ trailing user message** ("tool access disabled — write your final answer in Russian now") | **Real answer, 1,238 chars, structured Russian veterinary summary.** Forced synthesis works. |
| D | C + poison-strip of blank assistants/nudges | Real answer, 839 chars. (The heavy poison in this session sits in assistant rows that carry tool_calls, so the strip removed little; C alone sufficed.) |
| E | tools present + corrective user message only (WOBBLING-stage soft move) | **3/3 attempts stalled the endpoint past 560-600s** (read timeout, zero bytes even with streaming) while A/C completed in 100-210s on the same history and the gateway was idle. Interpretation: with tools still available, the "stop searching" conflict sends the model into pathological deliberation. **Do not ship a mid-turn soft nudge that keeps tools enabled.** |

**Forced-synthesis recipe (the healer's FAILING action), per API call while the turn is in state FAILING:**
1. `request.pop("tools", None)`, `request.pop("tool_choice", None)`, `request.pop("response_format", None)`.
2. Append one corrective user message (constant per turn, so the wire history stays stable across iterations): explicit "tool access disabled, answer now, user's language, no `<tool_call>`, no '(empty)'".
3. Belt-and-braces for the B-failure mode: register a `transform_llm_output` hook that, when the healer forced synthesis this turn and the final text still contains `<tool_call>`/is empty, replaces it with an honest diagnostic ("I got stuck in a search loop and could not produce the answer — retrying in a fresh session") — which also triggers the DOOMED path.
4. In parallel, `pre_tool_call` returns block for **every** tool this turn (protects against the think-skip/textual tool-call being parsed by some path; cheap and already the established mechanism). Exemption: `text_to_speech` (and the `dup_call_exempt` set) stays allowed so a voice_only chat can still voice its forced answer — those tools cannot loop the turn (they end in an answer) and are already self-limiting.

### 1.c Session-level actions — the gateway control surface

**How sessions work (verified):** the telegram gateway keys sessions on `session_key` (`agent:main:telegram:dm:<user_id>`; builder `gateway/session.py:870`), maps them to `session_id` rows in `~/.hermes/state.db` (tables `sessions`, `messages`; messages are soft-deletable via `active` column — loader uses `WHERE active = 1`, hermes_state.py:3695). `/new` → `_interrupt_and_clear_session()` then `session_store.reset_session(session_key)` (gateway/session.py:2089-2150: ends the old DB session with reason `session_reset`, mints `YYYYmmdd_HHMMSS_<hex8>`, preserves binding) + `_evict_cached_agent()`.

**Upstream precedents that ARE the healer's playbook:**
* **Auto-reset on compression exhaustion** (gateway/run.py:11631-11671): reset_session + evict + user-visible "🔄 Session auto-reset" note appended to the response. Exactly the DOOMED action, upstream-sanctioned.
* **CLI→gateway handoff watcher** (gateway/run.py:7284-7500): a background task inside the gateway that forges `MessageEvent(text=..., source=..., internal=True)` and `await self._handle_message(synthetic_event)`, then delivers the returned text via `adapter.send(chat_id, ...)` (streaming may already have delivered it). **Synthetic re-dispatch with normal delivery is a shipped pattern.**
* **Hard interrupt:** `await gateway._interrupt_and_clear_session(session_key, source, interrupt_reason=..., invalidation_reason=...)` (gateway/run.py:15829) — interrupts the running agent (`running_agent.interrupt(...)` via `_running_agents`), clears queued messages, releases the run lock.

**How the plugin gets the gateway object:** the `pre_gateway_dispatch` hook receives `event: MessageEvent, gateway: GatewayRunner, session_store` on every user-originated message (gateway/run.py:8762-8770; hook declared in `hermes_cli/plugins.py:135+` VALID_HOOKS). The healer:
* stores `weakref.ref(gateway)` + `asyncio.get_running_loop()` (the hook runs synchronously on the gateway's event loop) + the latest `event.source` per `session_key` (needed to rebuild `SessionSource` for interrupts/re-dispatch/adapter sends);
* thereafter can act **from any thread** via `asyncio.run_coroutine_threadsafe(coro, loop)`. (Agent turns run in an executor thread; hooks/middleware fire in that thread — actions must hop to the loop; verified the handoff/reset helpers are coroutines or loop-affine.)

**Fresh-session retry flow (DOOMED action), all in-process:**
1. (turn already ended — forced synthesis returned degenerate text, or `Turn ended` with guardrail_halt) — if a turn is somehow still running, `_interrupt_and_clear_session(...)` first.
2. `new_entry = session_store.reset_session(session_key)`; `gateway._evict_cached_agent(session_key)`; clear per-session overrides exactly as the compression-exhausted path does (model overrides map, `_pending_model_notes`, `_last_resolved_model`) — copy that block's sequence (gateway/run.py:11637-11648) behind `getattr` guards.
3. Forge `MessageEvent(text=<rephrased prompt>, source=<captured SessionSource>, internal=True)`; `await gateway._handle_message(evt)`; if it returns text (streaming didn't deliver), `adapter = gateway._adapter_for_source(source)`; `await adapter.send(chat_id, text, metadata=...)` — mirroring `_process_handoff` (gateway/run.py:7475-7500).
4. Mark `(session_key, question_hash)` as retried (max 1). On a second failure: deliver the honest failure report via `adapter.send` (or the turn's own transform_llm_output replacement text) — never loop.

**Out-of-process surfaces investigated and rejected as actuators:**
* **Dashboard (port 9119, FastAPI `hermes_cli/web_server.py`):** read surfaces (`/api/sessions`, `/api/status`) plus heavy hammers (`/api/gateway/restart`, `/api/gateway/drain`, web_server.py:3216/3233). **No per-chat reset, no message dispatch.** Useful for a human, not for the healer.
* **TUI RPC (`tui_gateway/server.py`):** newline-delimited JSON-RPC over stdio or WebSocket (`tui_gateway/ws.py` mounts the same dispatcher at the dashboard's `/api/ws`, behind dashboard auth). Rich surface (slash commands, approvals, agent events) — but it drives the **TUI's own hermes session**, not the telegram gateway's session store; no cross-process control of telegram chats.
* **`hermes send` (`hermes_cli/send_cmd.py`):** delivery-only (direct platform REST, no agent loop). Good for watchdog *alerts*, cannot run a turn.
* **state.db manipulation from outside:** the only state.db-driven command channel is the handoff table (`handoff_state='pending'` polled by the gateway, run.py:7292+), which is purpose-built for CLI→gateway moves, not generic dispatch. Soft-deleting poison rows (`UPDATE messages SET active=0`) from another process while the gateway holds the write connection is possible (WAL) but races the gateway's own writes and heals nothing the in-process healer can't do better. **Rejected for v1**; noted as a manual ops tool.

### 1.d Post-turn detection

* **No general turn-end hook** — re-verified: `pre_verify` is consulted only when `agent._turn_file_mutation_paths` is non-empty (`_edited and has_hook("pre_verify")`, conversation_loop.py:5181-5187). Unchanged from the v1.5.2 investigation.
* **In-process turn-end observers that DO exist:** `transform_llm_output` (turn_finalizer.py:340-359; can *replace* the final text — doubles as an intervention) and `post_llm_call` (turn_finalizer.py:361-380; observer with `conversation_history`). **Gate:** both fire only `if final_response and not interrupted` — an empty-final turn is invisible to them. The turn-completion explainer (turn_finalizer.py:~410+) usually replaces empty finals with an explanation *before* these hooks, so in practice most degenerate turns are visible; the truly-empty residue is covered by the next-turn-start check (`pre_llm_call` sees full history) and by the log watcher.
* **agent.log is watchdog-grade:** session-tagged INFO lines with timestamps. Verified present: `Turn ended: reason=... model=... api_calls=N/M budget=... tool_turns=... last_msg_role=... response_len=N session=<id>` (turn_finalizer.py:236-256; e.g. the live degenerate turns logged `response_len=16 / 113 / 7`); router-plugin INFO lines for every dup-block, cap activation, steering injection. A cron/daemon watcher (hermes-skills-sync pattern) can tail it for **alerting and metrics**, and `state.db` is readable via `sqlite3 file:...?mode=ro` (verified) for message-level forensics. But since it cannot act (see 1.c), the watchdog is optional observability, not part of the control loop.

---

## 2. Heuristics — signals, scores, state machine

All state is per `(session_id, turn_id)` with a session-level overlay, held in the plugin process (the gateway); same memory-bounding pattern as the v1.5.x gates (bounded dicts, 1h idle pruning).

### 2.1 Signals (scored)

| # | Signal | Source (in-process) | Cost | Discriminative power (observed incidents) | Score |
|---|---|---|---|---|---|
| S1 | web_search attempts this turn | pre_tool_call counter (exists, v1.5.4) | free | 69/turn vs ≤6 healthy — the single cleanest churn signal | ★★★★★ |
| S2 | guard blocks this turn (dup-query, dup-call, hard-cap) | pre_tool_call verdicts (exists) | free | 63 blocked identical, ~50 blocked dups — fires only when something is already wrong | ★★★★★ |
| S3 | query-similarity collapse | pre_tool_call: keep last 8 normalized queries; mean pairwise Jaccard (word-set) > 0.6 across ≥6 searches | trivial | catches the paraphrase fountain *below* the hard cap; edit distance unnecessary — token overlap suffices | ★★★★ |
| S4 | blank/`(empty)` assistant messages this turn | llm_request middleware: scan `request["messages"]` tail each call | trivial | 130 in the poisoned session; healthy sessions ≈ 0 | ★★★★★ |
| S5 | history poison score (session) | pre_llm_call once/turn over `conversation_history`: fraction of assistant msgs blank/`(empty)` + count of guard-error tool results + count of `STEERING` markers | cheap | separates "sick session" (retry-in-fresh candidates) from "sick turn" | ★★★★ |
| S6 | api_calls vs budget without answer progress | middleware `api_call_count` + "no non-empty assistant text yet this turn" | free | the 90/90 max_iterations burn | ★★★ |
| S7 | turn wall-time | first-seen timestamp per turn_id | free | 10-min runaway streams; blunt but a good backstop | ★★★ |
| S8 | thinking-only / inline `<tool_call>` in final text | transform_llm_output | free | catches the B-failure mode and 7-char answers at the finish line | ★★★★ |
| S9 | turn-end reason/response_len | agent.log (watchdog) or transform_llm_output | free | ground truth but post-hoc | ★★ (alerting) |

Dropped: embedding-based similarity (overkill), per-token entropy (no access), tool_turns velocity (S6 subsumes).

### 2.2 State machine (per turn, with session overlay)

```
HEALTHY --(T1)--> WOBBLING --(T2)--> FAILING --(T3)--> DOOMED
   ^                                                     |
   +---------- new turn / fresh session -----------------+
```

* **T1 (→WOBBLING)** any of: S1 ≥ 8 · S2 ≥ 2 · S3 collapse · S4 ≥ 2 · (session overlay: S5 high at turn start).
  **Action: log-only by default** (`soft_nudge: "off"`). Experiment E (3/3 endpoint stalls >560s with tools + corrective message, vs 100-210s for control/forced-synthesis on the same history) shows a mid-turn user-voice nudge **with tools still enabled** makes this model deliberate pathologically — the soft rung is *worse* than escalating. The existing steering notes (`search_steer_after`) remain the soft mechanism; WOBBLING's real job is arming T2 earlier (once WOBBLING, T2 thresholds apply at their configured values; T2 could optionally be lowered for wobbling turns in a later tuning pass). `soft_nudge: "on"` re-enables the injection for experimentation on future models/servers.
* **T2 (→FAILING)** any of: S1 > search_hard_cap · S2 ≥ 5 · S4 ≥ 4 · S6: api_calls ≥ 60% of max_iterations with no substantive assistant text · S7 > 8 min.
  **Action: FORCED SYNTHESIS** (recipe §1.b) for every remaining API call this turn. Logged INFO `selfheal: FORCED_SYNTHESIS session=… turn=… trigger=…`.
* **T3 (→DOOMED)** forced synthesis still degenerate: S8 at turn end (final text < 40 chars after think-strip, `(empty)`, or contains `<tool_call>`), OR turn ended `guardrail_halt`/`max_iterations` with response_len < 40, OR (session overlay) S5 ≥ threshold AND the previous turn in this session also ended degenerate.
  **Action: FRESH-SESSION RETRY** (flow §1.c), max **1** per (session_key, question-hash); then honest failure report with diagnosis (which signals fired, counts) to the chat.

Session overlay: S5 is computed each turn start; `S5 ≥ high` alone (without a bad current turn) arms "WOBBLING at birth" — corrective context injected via the supported `pre_llm_call` channel rather than mid-turn rewrite.

### 2.3 Rephrasing: template-based (recommended) with optional aux-LLM garnish

**Recommendation: template.** Rationale: at DOOMED time the model on this box is the *sick component* — an aux-LLM call through the same vLLM (plugin_llm routes to the user's active model by default) inherits the pathology and adds a failure mode exactly when reliability matters most. The template is deterministic, language-preserving, and testable:

```
[Fresh session — previous attempt hit a failure loop and was reset]
The user asked (verbatim): «<original user text>»
Context: a previous attempt failed because <diagnosis: e.g. "it repeated 60+ web searches without reading results">.
Do NOT repeat that: run at most 2 web searches, web_extract at most 2 URLs, then answer.
<optional: Key findings already retrieved: – <up to 3 one-line strings harvested from the last successful tool results>>
Answer in the user's language.
```

* Verbatim original text preserved (no pronoun-stripping needed — the fresh session has no antecedents, and the bracketed frame supplies the missing context). Topic-specific disambiguators (the "Nous Research" trick) are already handled by the shipped brand nudge + system prompt; the template only adds them when the healer's diagnosis includes brand pollution (`brand_note` seen this turn).
* The "key findings" bullets are harvested mechanically (first line of the last ≤3 successful `web_search`/`web_extract` results). If harvesting is noisy, ship without it — C proved the model can synthesize from its own knowledge.
* `ctx.llm.complete()` (`agent/plugin_llm.py` — supported plugin LLM facade) is available if a smarter rephrase is ever wanted; keep behind `selfheal.rephrase: template|llm` with `template` the default.

---

## 3. Architecture decision

**Chosen: in-plugin only** — a new `selfheal.py` module in `/home/user/saandal/plugin/`, registered from the existing `register(ctx)`. No separate daemon.

| Criterion | in-plugin | plugin + watchdog daemon | watchdog only |
|---|---|---|---|
| Intervention power | full (middleware rewrite, forced synthesis, gateway actions) | same + nothing extra (daemon can't act) | delivery + gateway restart only — **cannot** reset/redispatch |
| Detection latency | per-API-call / per-tool-call (seconds) | plugin part same; daemon adds none | log-tail seconds, but blind to wire detail |
| Blast radius | one process, fail-safe hook pattern already proven 8× in this repo | + a process to babysit | low, but toothless |
| Testability | offline request-replay + hook-level unit calls | worse (IPC) | log fixtures only |
| Update survival | same story as the rest of the router plugin (symbol guards, flat-tools fallback) | two artifacts to keep compatible | best, but irrelevant given power |

The only genuine watchdog advantage — catching a *hung/crashed* gateway — is out of scope (that's process supervision, and `/api/gateway/restart` + systemd already exist). A tiny **optional** `bin/selfheal_alert.py` (cron, tails agent.log for `selfheal:` decisions and `Turn ended … response_len<40`, delivers a daily digest via `hermes send`) is listed as a stretch item for observability only.

### 3.1 Components (exact wiring)

All in `plugin/selfheal.py`, registered from `plugin/__init__.py::register(ctx)` after the existing features (order matters only for the llm_request chain — healer registers after `_cd_middleware`):

| Component | Surface | Role |
|---|---|---|
| `_sh_pre_tool` | `ctx.register_hook("pre_tool_call", ...)` | sensor S1-S3 (reuses/extends the existing `_dup_gate` counters — refactor: the dup gate exposes its per-turn counters to the healer instead of duplicating them); tripwire: in FAILING state returns block for every tool |
| `_sh_middleware` | `ctx.register_middleware("llm_request", ...)` | sensor S4, S6, S7; actuator: corrective user-message append (WOBBLING), forced synthesis strip+append (FAILING) |
| `_sh_pre_llm` | `ctx.register_hook("pre_llm_call", ...)` | sensor S5 at turn start; actuator: supported turn-start context injection |
| `_sh_transform_out` | `ctx.register_hook("transform_llm_output", ...)` | sensor S8; actuator: replace degenerate final text with honest diagnostic; flags DOOMED |
| `_sh_post_llm` | `ctx.register_hook("post_llm_call", ...)` | closes the turn record, feeds the session overlay |
| `_sh_gateway_capture` | `ctx.register_hook("pre_gateway_dispatch", ...)` | captures `weakref(gateway)`, event loop, per-session_key `SessionSource` + last user text; also the place where a queued DOOMED retry for *this* session_key is checked (belt-and-braces ordering) |
| `_sh_actions` | plain module code | DOOMED executor: schedules `run_coroutine_threadsafe(_do_fresh_retry(...), loop)`; implements interrupt→reset→evict→redispatch→deliver with `getattr` guards on every private symbol |
| `_sh_state` | plain module code | bounded state tables, decision log, intervention caps, config readers |

**Private-API usage & guards:** `_evict_cached_agent`, `_interrupt_and_clear_session`, `_adapter_for_source`, `_handle_message`, `MessageEvent(internal=True)`, `_running_agents` are private. Every access goes through a single `_gw_call(name, *a, **kw)` helper that `getattr`-probes and, on any missing symbol, logs one warning and **degrades to the next-weaker action** (fresh-retry unavailable → honest failure report via `adapter.send`; that too unavailable → transform_llm_output text only). This is the same survival contract the rest of the router plugin ships.

### 3.2 Config (all under `tools.tool_search.selfheal`, read per call like every other router knob; `bin/install.py` seeds only-when-absent)

```yaml
tools:
  tool_search:
    selfheal:
      enabled: "on"            # master switch; "off" = pure no-op
      soft_nudge: "off"        # WOBBLING corrective injection — OFF: experiment E showed 3/3
                               # endpoint stalls when nudging with tools still enabled
      forced_synthesis: "on"   # FAILING tool-strip
      fresh_retry: "on"        # DOOMED session reset + re-dispatch (gateway only)
      # thresholds (defaults from §2.2)
      wobble_searches: 8
      wobble_blocks: 2
      wobble_blank: 2
      wobble_sim: 0.6          # S3: mean pairwise Jaccard over the last wobble_sim_n queries
      wobble_sim_n: 6
      fail_blocks: 5
      fail_blank: 4
      fail_budget_frac: 0.6
      fail_wall_secs: 480
      doomed_min_answer: 40
      poison_hi: 0.25          # S5: fraction of blank assistant rows
      max_retries_per_question: 1
      platforms: [telegram]    # fresh_retry allowlist; empty = all gateway platforms
```

### 3.3 Observability & failure-safety story

* Every state transition and every action logs one INFO line with a fixed grep-able prefix: `selfheal: <STATE>-><STATE> session=… turn=… trigger=S2=6 …` / `selfheal: action=forced_synthesis …` / `selfheal: action=fresh_retry attempt=1 …` / `selfheal: action=skipped reason=disabled|cap|missing-symbol`. (Lesson from the v1.5.2 "invisible steering" post-mortem: silent firing is a bug class.)
* Caps: ≤1 soft nudge per turn; forced synthesis latches for the turn (no flapping); ≤1 fresh retry per (session_key, question sha256), tracked in a bounded table AND double-checked against a marker embedded in the rephrased prompt (`[Fresh session — previous attempt…]` prefix in the *stored* user message of the fresh session) so the cap survives a gateway restart.
* The healer never raises: every hook body is `try/except → None/passthrough` (house style). Worst-case failure = today's behavior.
* Kill switch: `selfheal.enabled: "off"` — read per call, no restart. Each action has its own flag.
* The fresh-retry action refuses to run when: platform not allowlisted, chat is a group with a running unrelated turn, question hash already retried, or gateway/loop refs are older than 24h (stale weakref).

---

## 4. Implementation work-list (for the implement agent)

1. **Refactor (small):** expose the v1.5.2-4 per-turn counters (`_dup_state/_dupc_state/_cap_state`) through a tiny accessor so selfheal reads instead of re-counting. No behavior change; keep the public log lines identical.
2. **`plugin/selfheal.py`** with the components of §3.1:
   a. state tables + config readers + decision logger;
   b. sensors (S1-S8) and the state machine (§2.2) — pure functions over the tables, unit-testable without hermes;
   c. `llm_request` actuator: WOBBLING append / FAILING strip+append (deterministic per turn — build the injected message once and cache it on the turn record);
   d. `pre_tool_call` FAILING tripwire (block-all with the forced-synthesis error text);
   e. `transform_llm_output` finisher (S8 + degenerate-text replacement + DOOMED flag);
   f. gateway capture + `_do_fresh_retry` coroutine (interrupt → reset → evict → overrides-clear → synthetic dispatch → deliver → mark retried), all through the `_gw_call` guard helper;
   g. rephrase template builder (+ optional findings harvest).
3. **`plugin/__init__.py`:** import guard + `_register_selfheal(ctx)` at the end of `register()`; keep every failure non-fatal.
4. **`bin/install.py`:** seed the `selfheal` config block only-when-absent.
5. **`bin/verify.py`:** add a check that the selfheal hooks/middleware are registered (mirror existing checks).
6. **Tests** (see §5): offline replay harness (`exp_forced_synthesis.py` promoted into `docs/`/`tests/` as a fixture runner), hook-level unit tests, one gated live E2E script.
7. **README:** v1.6.0 section documenting behavior, config, logs, rollback; note the upstream asks (turn-end hook; fire pre_verify without `_edited`).
8. *(stretch)* `bin/selfheal_alert.py` cron digest via `hermes send`.

## 5. E2E test plan

**Inducing each pathology deterministically:**

* **Fixture P (poisoned session):** session `20260719_012254_e3b7f659` is preserved in state.db (281 rows). Copy its rows under a fresh session_id into state.db (read rows read-only, INSERT under a test session_id while the gateway is stopped, or into a scratch profile's state.db), then bind a test chat to it via `session_store.switch_session` (or gateway `/resume`-style binding) — every request from that chat now replays the 65k-token failure context. **Experiment A proved the churn reproduces from this context on the first call.**
* **Fixture Q (churn inducer, fresh session):** the cat-fracture question verbatim (`Поищи в интернете как лечат перелом шейки бедра у котов`) in a voice_only-prompted session reproduced 60+ varied searches live; keep it as the organic inducer (less deterministic than P, still reliable per 2026-07-18/19 history).
* **Fixture R (synthetic failure history):** unit-level — build a message list with N blank assistants + M guard-error tool results; feed it to the sensor functions directly. Deterministic by construction.

**Per-intervention pass criteria:**

| Intervention | Test | Pass |
|---|---|---|
| Sensors/state machine | unit: replay fixture R sequences into the pure functions | transitions exactly at configured thresholds; no transition on healthy traces (take 3 transcripts from passing sessions as negative controls) |
| WOBBLING soft nudge | **already decided by experiment E (design phase): 3/3 stalls → ships OFF.** If ever re-enabled: offline fixture P + variant-E request, pass = ≥2/3 runs return <300s AND abandon churn | n/a for v1 |
| FORCED SYNTHESIS | offline: fixture P, healer-built request (strip+append via the real `_sh_middleware` code path, not a hand copy) | final content non-empty, ≥`doomed_min_answer` chars after think-strip, no `<tool_call>`, in Russian — 3/3 runs. (Variant C passed 1/1 in design; implement agent re-runs ×3.) |
| Forced synthesis in-loop | live gateway on a test chat bound to fixture P; ask the cat question; watch agent.log | `selfheal: action=forced_synthesis` INFO present; `Turn ended: reason=text_response` with `response_len ≥ 200`; telegram receives a real answer; total turn ≤ ~15 API calls |
| Fresh-session retry | live: fixture P + `fail_*` thresholds temporarily set low so the turn dooms fast | `selfheal: action=fresh_retry attempt=1`; state.db shows old session `end_reason=session_reset` + new session whose first user message carries the `[Fresh session…]` template; telegram receives the fresh answer; a SECOND induced doom on the same question produces the honest failure report, not a third session |
| Fail-safety | live: `selfheal.enabled: "off"` mid-test; kill a symbol (monkeypatch `_handle_message` away in a scratch venv copy) | zero healer log lines when off; on missing symbol exactly one warning + degraded action, turn completes |
| Regression | run `bin/verify.py` + the 12-scenario router E2E suite from `docs/router_design.md` | unchanged results; healthy sessions show zero `selfheal:` action lines |

**Metrics to record in the report:** interventions per state, answer length distribution, retry count, and — the user's own bar — a poisoned-session retest going from 0/5 to ≥4/5 delivered answers (each either a real answer or one honest failure report).

## 6. Known limitations / upstream asks

* Turns that end with an *empty* final response bypass `transform_llm_output`/`post_llm_call` (turn_finalizer.py:343 gate) — DOOMED detection for those rides the next turn start (S5) or the explainer's replacement text. Upstream ask (existing list in README): a `pre_turn_end` hook, or `pre_verify` without the `_edited` gate.
* Wire-stripping does not clean *stored* poison; fresh-session retry is the durable cure. A manual ops recipe (`UPDATE messages SET active=0 WHERE …` with the gateway stopped) is documented but not automated.
* ACP/Zed sessions bypass the gateway capture (no `pre_gateway_dispatch`); mid-turn interventions still work there, fresh-retry does not (acceptable: incidents are all telegram).
* `pre_gateway_dispatch` fires only on user-originated messages — after a gateway restart the healer has no gateway ref until the next inbound message; fresh-retry before that degrades to the failure report. Acceptable (a restart empties the failing turn anyway).
* The experiment-E stall (corrective user message + tools ⇒ >9 min of silence from vLLM, 3/3) is model/server-specific and worth flagging to the vllm-local operator: whatever the mechanism (endless think-block, scheduler interaction), hermes's in-loop defenses (90s stale-stream detection, max_tokens 3000 clamp) convert it into a retriable failure, but the healer deliberately never creates that request shape.

## 7. Experiment artifacts (this scratchpad)

* `exp_forced_synthesis.py` — fixture builder + variants A-D runner (rebuilds the poisoned session from state.db read-only; promote into the repo as the offline test harness).
* `exp_results.json` — A-D outcomes (A: churn reproduced; B: literal `<tool_call>` text; C: 1,238-char real answer; D: 839-char answer).
* `exp_e.py` / `exp_e2.py` / `exp_e3.py`, `exp_e*.out` — the three E attempts (non-streaming, streaming, non-streaming under `timeout 560`), all read-timeouts with zero bytes received.
* `tools_flat.json` — the exact 10 flat tool schemas from the live request dump (control-variant fidelity).
