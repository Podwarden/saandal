# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Podwarden Inc

"""router — defer bulky core tool schemas behind the built-in tool_search bridge.

Narrows tool_search's never-defer set (normally all of _HERMES_CORE_TOOLS) to a
small keep-list, so everything else is served via the shipped
tool_search/tool_describe/tool_call bridge. No new tools, no new dispatch path:
catalog, search, describe, scoped dispatch, approvals/hooks, and display unwrap
are all the tested in-tree bridge.

Additionally supports DESCRIPTION OVERRIDES from overrides.yaml (next to this
file): tool descriptions and per-parameter descriptions can be replaced from
data, so concise rewrites ship without editing site-packages. Schema:

    tools:
      <tool_name>:
        description: "replacement for the tool description"   # optional
        params:
          <param_name>: "replacement for properties.<param>.description"

Overrides are applied at the single choke point every model-facing schema
flows through — registry.get_definitions (the only hermes call site is
model_tools._compute_tool_definitions, model_tools.py:445) — which covers
(a) the flat schemas in agent.tools, (b) tool_describe output, and (c) the
tool_search BM25 catalog text and search-result snippets (both built from
get_tool_definitions(skip_tool_search_assembly=True)). The three bridge tools
(tool_search/tool_describe/tool_call) never pass through the registry — they
are synthesized in tool_search.bridge_tool_schemas — so that function gets the
same treatment. Overridden defs are DEEP-COPIED before editing: the dicts
registry.get_definitions returns share their nested 'parameters' object with
the registry's own ToolEntry.schema, and in-place edits would leak into
registry state. The file is read once at plugin load; edits take effect on the
next process start (gateway restart), which also keeps model_tools'
_tool_defs_cache memoization (keyed on registry generation + config mtime,
not on this file) consistent for the process lifetime.

Since v1.2.0 the plugin also hardens the tool_call bridge against malformed
argument shapes (incident 2026-07-18, session 20260718_050731_9114f597:
local-27b looped 40x on nested/double-wrapped tool_call arguments):

* resolve_underlying_call is wrapped with a REPAIR layer. The original
  resolver always runs first and its verdict is authoritative for valid
  calls — a call the original resolves is returned untouched, except for one
  provably-mechanical hazard: a payload that is exactly {"arguments": {...}}
  for a tool whose own schema declares no "arguments" parameter (schema
  checked via the registry; unknown schema = hands off). When the original
  FAILS, a mechanical repair is attempted: unwrap up to 3 levels of
  {"arguments": {...}} nesting, hoist an inner "name" to the top level, and
  skip a spurious top-level name == "tool_call". The repaired call is then
  re-run through the ORIGINAL resolver so all upstream validation (bridge
  names, deferrability, JSON arguments) still applies. Every repair is
  logged at INFO with before/after. If repair fails too, the error is
  enriched with the literal expected shape and a pointer to the
  deferred-names index so the model can self-correct instead of looping.

* dispatch_tool_describe is wrapped so describing a BRIDGE tool
  (tool_search/tool_describe/tool_call) returns that bridge tool's own
  schema plus a usage example instead of the upstream refusal — the refusal
  denied the model the one document that teaches the tool_call shape.
  Non-bridge names delegate to the original unchanged.

Since v1.3.0 the plugin also ships two web-behavior fixes (2026-07-18 5-run
voice-lookup test: 24-117 searches/turn, web_extract had no backend at all):

* LOCAL WEB_EXTRACT BACKEND (web_local.py). An extract-only
  WebSearchProvider named "local" is registered through the supported
  plugin API (PluginContext.register_web_search_provider,
  hermes_cli/plugins.py:740 -> agent.web_search_registry.register_provider).
  Select it with ``web.extract_backend: local``. It fetches up to 3 URLs per
  call with httpx (15 s timeout, 2 MB cap, per-hop private-network guard)
  and converts HTML to readable text with the stdlib html.parser; the core
  dispatcher (tools/web_tools.py:web_extract_tool) then applies its normal
  char_limit truncation + cache/web full-text store.

* SEARCH STEERING. A ``transform_tool_result`` lifecycle hook (a supported
  hook — no monkey-patching) counts successful web_search results per turn
  and, once the count exceeds ``tools.tool_search.search_steer_after``
  (default 6, values < 1 disable), injects a short "steering" field into
  each further result telling the model to stop searching and either
  web_extract a URL it already found or answer from what it has. TURN
  BOUNDARY: the dispatcher passes ``turn_id`` to the hook
  (model_tools.py:1319-1333), so the counter is keyed on
  (session_id, turn_id) and a new turn naturally starts a fresh counter —
  no heuristics needed. If a dispatch path ever passes an empty turn_id,
  the fallback key is (session_id, "window") with a 300 s idle reset.
  Purely additive text (~350 bytes); hard blocking stays the guardrails'
  job (tool_loop_guardrails).

Since v1.4.0 the plugin adds CONSTRAINED TOOL-CALL DECODING via an
``llm_request`` middleware (a supported plugin surface —
PluginContext.register_middleware, hermes_cli/plugins.py:1175, applied at
agent/conversation_loop.py:1177 through
hermes_cli/middleware.py:apply_llm_request_middleware):

* Empirical background (2026-07-18 investigation): the vLLM ~0.12.x /
  xgrammar server behind vllm.example.com does NOT constrain tool-call
  arguments under ``tool_choice: auto``, but DOES enforce a
  ``response_format`` of type ``structural_tag`` (legacy shape:
  ``{"type": "structural_tag", "structures": [{"begin": "<tool_call>",
  "schema": {...}, "end": "</tool_call>"}, ...], "triggers":
  ["<tool_call>"]}``), which composes with the model's hermes-format tool
  parser and streaming. Named ``tool_choice`` is broken on that build
  (intermittent 500s) and is deliberately not used.

* The middleware fires only when ALL gates pass: config
  ``tools.tool_search.constrained_decoding`` == "on" (default OFF —
  install.py seeds "on" only-when-absent, so turning it on everywhere is a
  conscious deploy step), ``api_mode == "chat_completions"``, the request's
  base_url host is in ``tools.tool_search.constrained_hosts`` (default
  ["vllm.example.com"]), the request has a non-empty ``tools`` array, and
  no ``response_format`` is already set. Otherwise it returns None and the
  request goes out unchanged.

* One structure per tool: ``{"type": "object", "properties": {"name":
  {"type": "string", "enum": [<tool name>]}, "arguments": <tool's
  parameters schema>}, "required": ["name", "arguments"]}``. Parameters are
  sanitized with the shipped tools/schema_sanitizer.strip_pattern_and_format
  (xgrammar chokes on some pattern/format keywords); if that import fails,
  parameters are embedded as-is with a logged warning. Structures are
  sorted by tool name and the built response_format is cached keyed on a
  hash of the tools array (deterministic output = xgrammar grammar-cache
  friendly).

* Fail-safe: any exception inside the middleware logs one warning and
  returns the request unchanged — the API call itself can never break.
  Known residual hole (until a server upgrade): if the model skips its
  <think> block the grammar gate doesn't activate (rare); the v1.2.0
  repair layer stays as the backstop. Rollback:
  ``tools.tool_search.constrained_decoding: "off"``.

Since v1.5.0 the plugin adds a GRACEFUL FLAT-NAME FALLBACK (config
``tools.tool_search.flat_name_fallback``, default "on"; incident 2026-07-18:
a long telegram session whose history was full of pre-router flat terminal
calls hit a think-skip grammar escape, the model emitted a bare ``terminal``
call, and conversation_loop's valid_tool_names gate — agent/
conversation_loop.py:4431/4442-4463 — rejected it three times and hard-killed
the turn with "Model generated invalid tool call: terminal"):

* WHAT: when the model calls a deferred tool by its BARE name instead of via
  the tool_call bridge, the call now EXECUTES through the normal executor
  path (same hooks/approvals/guardrails — after the gate, bare names take
  byte-for-byte the same path a tool_call unwrap takes) instead of being
  rejected. The tool's schema stays out of the prompt, the names index lists
  each deferred name exactly once, and the v1.4.0 constrained-decoding
  grammar is untouched (grammar-active generations still can't emit bare
  names; this feature only rescues grammar-inactive generations, e.g. the
  think-skip escape).

* HOW: ``agent.valid_tool_names`` — the only gate blocking bare-name calls
  (there is no enabled_toolsets check at dispatch;
  ``registry.dispatch``/``handle_function_call`` execute deferred tools fine)
  — is extended with the session-scoped DEFERRED tool names at both places
  it is computed/published: ``agent.agent_init.init_agent`` (sets it from
  ``agent.tools`` at build, agent_init.py:1166-1168) and
  ``tools.mcp_tool.refresh_agent_mcp_tools`` (republishes it on MCP refresh,
  mcp_tool.py:5342-5343). Both are wrapped (call-time function-local imports
  at every call site make module-attr patching effective), NOT post-hoc
  mutated, so every later refresh re-extends consistently. The added set is
  the SESSION-SCOPED deferrable subset (same classify logic as the keep-list,
  same scope the tool_call unwrap enforces via
  tool_executor._tool_search_scoped_names) — a restricted-toolset session
  (subagent, kanban worker) can NOT reach out-of-scope tools by bare name.
  ``agent.tools`` (the schemas sent to the model) is never touched.

* KNOWN SIDE EFFECTS (all restore pre-router behavior; deferred tools were
  always callable via the bridge, valid_tool_names just didn't say so):
  system-prompt guidance gated on membership re-appears
  (SESSION_SEARCH_GUIDANCE ~190 chars, SKILLS_GUIDANCE ~390 chars, the
  skill-nudge counters), the skills index sees the full toolset list, and
  execute_code sandbox stubs / delegate toolset inheritance are derived from
  the full name set again. ACP sessions (acp_adapter/server.py:844,1799)
  rebuild valid_tool_names inline and are not covered — CLI/gateway/telegram
  paths all go through the two wrapped publish points.

Since v1.5.1 the plugin ships three small mitigations for model-side decoding
pathologies observed in live testing (2026-07-18):

* MAX-TOKENS CLAMP (same llm_request middleware, same host gating as
  constrained decoding): hermes sends max_tokens=65536 on this route — the
  "custom" ProviderProfile's default_max_tokens (providers/, profile path in
  agent/transports/chat_completions.py:508-523; agent.max_tokens is None
  because config.yaml sets no model.max_tokens). 65536 is no effective cap:
  one repetition-looping call streamed 185 KB for 10 minutes. The middleware
  clamps the outgoing max_tokens (or max_completion_tokens when that key is
  the one present) to ``tools.tool_search.max_tokens_cap`` (default 3000;
  explicit 0 disables) whenever it is absent, non-numeric, or above the cap.
  hermes's conversation loop already handles finish_reason=length by
  appending the partial turn + a continuation prompt (up to 4 retries,
  conversation_loop.py:1890-1936), so the cap converts runaway generations
  into fast, recoverable turns.

* ANTI-REPETITION (same middleware, same gating): injects a penalty ONLY
  when the request does not already set it. Both are standard OpenAI params
  (portable — deliberately NOT vLLM's repetition_penalty, whose A/B result
  was the worst: a synonym-avalanche collapse that also suppressed natural
  EOS). ``presence_penalty`` (``tools.tool_search.presence_penalty``,
  default 0.3; explicit 0 disables) is the v1.8.2 primary lever: it is FLAT
  (one-shot per token) so it curbs genuine loops without accumulating, and
  long structured answers complete and stop naturally under it.
  ``frequency_penalty`` (``tools.tool_search.frequency_penalty``, default
  0.0 = OFF as of v1.8.2; any value re-enables) is retained configurable but
  disabled by default: A/B (spec-decode permanently off) proved its per-COUNT
  accumulation was itself the CAUSE of long-output tail collapse — across a
  long answer it progressively forbids legitimately-recurring tokens and the
  EOS, derailing greedy decoding into word-salad exactly at the tail.

* SEARCH-POLLUTION NUDGE (same transform_tool_result hook as search
  steering): web_search results for "hermes"-adjacent queries get polluted
  by the Hermès fashion brand. When >= half of a result set's titles/urls
  match the brand pattern (case-insensitive "hermès", or "hermes" plus any
  of birkin/kelly/bag/handbag/scarf/fashion/luxury) AND the query does not
  contain "nous", a one-line ``brand_note`` field is appended telling the
  model to re-search with "Nous Research Hermes agent <keywords>".
  ``tools.tool_search.brand_nudge`` ("on"/"off", default on).

Since v1.5.2 (round-6 live test, session 20260718_173910_27cb7f: 69
web_search calls in one turn, 63 of them the IDENTICAL query string; the
model read the steering notes and kept searching anyway):

* STEERING/BRAND OBSERVABILITY. Post-incident analysis first concluded the
  v1.5.1 hook rewrite had silently broken steering — the preserved request
  dump proves the opposite (63 of the 71 tool results carry "steering":
  "STEERING: this was web_search #7..#69 ..."), but nothing in agent.log
  said so either way. Injections now log: INFO on every steering-note
  injection, INFO on every brand-note injection, and DEBUG when the brand
  classifier evaluates a result set without triggering (with the reason —
  "query contains 'nous'", hit ratio, etc.), so firings are observable
  without dumping conversations.

* DUPLICATE-QUERY HARD GATE (``tools.tool_search.dup_query_limit``, default
  3; values < 1 disable). A ``pre_tool_call`` hook (supported surface, the
  same block contract security plugins use) tracks normalized (casefold +
  whitespace-collapsed) web_search query strings per (session_id, turn_id).
  When the same query is attempted more than N times in one turn, the hook
  returns {"action": "block", ...} — the dispatcher then SKIPS the network
  call entirely and the model receives an error-style result
  {"error": "duplicate query loop: you already ran this exact search N
  times this turn — the results will not change; STOP searching and either
  web_extract one of the URLs you already have or write your final answer
  now"}. The model demonstrably reacts to tool errors where it ignored the
  additive steering note; the softer note (at search_steer_after=6) stays
  as the first line, this gate backs it. Blocked calls never reach the
  transform hook, so the steering counter counts executed searches only.

Since v1.5.3 (live incident 2026-07-18, session 20260718_050731_9114f597: the
model repeated one BYTE-IDENTICAL terminal curl 40+ times, every call
SUCCEEDING with slightly-varying results — the v1.5.2 gate only watched
web_search, hermes's tool_loop_guardrails key on failing calls or identical
results, and the frequency penalty is per-completion):

* GENERAL DUPLICATE-CALL GATE. The v1.5.2 ``pre_tool_call`` hook now gates
  EVERY tool: attempts are counted per (session_id, turn_id, tool_name,
  normalized_args), where normalized_args is the args dict JSON-serialized
  with sorted keys, casefolded, and whitespace-collapsed (stored as a sha256
  digest, so huge payloads cost nothing). The tool_call bridge is UNWRAPPED
  first — an intercepted ``tool_call {"name": X, "arguments": {...}}`` is
  keyed on the underlying (X, arguments), so bridged and bare invocations of
  the same call share one counter (the agent executor unwraps before the
  hook on the main paths; the gate's own unwrap covers every other path).
  More than ``tools.tool_search.dup_call_limit`` (default 3; explicit 0/<1
  disables) identical attempts in one turn are blocked before execution with
  an error-style result telling the model the result will not change.
  EXEMPT (``tools.tool_search.dup_call_exempt``, default clarify, todo,
  memory, text_to_speech, tool_search, tool_describe): tools with legitimate
  repeats or self-limiting cost. The v1.5.2 web_search query gate keeps its
  own config and counter for backward compat — web_search is blocked at
  whichever of the two limits is hit first (i.e. the lower one). Same memory
  bounds as v1.5.2 (per-turn distinct cap, stale-turn pruning); every block
  logs INFO.

Since v1.5.4 (live incident 2026-07-19, session 20260719_012254_e3b7f659: the
model generated 60+ endlessly VARIED web_search queries in one turn and burned
the whole 90-iteration budget — the v1.5.3 dup gate blocked ~50 exact repeats
but every paraphrase sailed through; hermes's same_tool_failure_halt never
fired because plugin-blocked calls never reach the guardrail's after_call at
all (tool_executor.py:1516 sequential / :851 concurrent both skip
_append_guardrail_observation for blocked calls — deliberate upstream design,
see the comment at tool_executor.py:865) and successful varied searches RESET
the same-tool failure counter anyway; the soft steering notes were ignored):

* HARD PER-TURN SEARCH CEILING (``tools.tool_search.search_hard_cap``,
  default 15; explicit 0 / any value < 1 disables). The same ``pre_tool_call``
  gate counts ALL web_search ATTEMPTS per (session_id, turn_id) — blocked and
  successful alike, bridge-unwrapped exactly like the dup gate, same
  turn-key/idle-window-fallback infrastructure. Once the count exceeds the
  cap, EVERY further web_search this turn is blocked (no exact-match or
  paraphrase escape — the counter is per-tool, not per-query) with an
  error-style result ordering the model to write its final answer now, or
  web_extract a URL it already has. web_extract stays UNCAPPED by this
  ceiling (it is the desired pivot) but remains covered by the v1.5.3
  dup-call gate. Ceiling activation logs INFO once per turn; each further
  block logs DEBUG. The dup gates and the steering notes keep operating
  beneath the ceiling untouched. A plugin-side fix that feeds blocks into
  the guardrail counters was investigated and rejected: the after_call skip
  is structural in the executor (no block-result shape reaches it), the
  pre_tool_call hook receives no agent handle, and feeding the counters
  would not have helped this incident anyway (varied searches SUCCEED, and
  any success resets same_tool_failure_counts) — the ceiling supersedes it.

Since v1.6.0 the plugin ships a SESSION SELF-HEALING layer (selfheal.py;
design: docs/selfheal_design.md; master flag tools.tool_search.selfheal.
enabled, default on). Sensors ride the supported plugin surfaces (the same
llm_request middleware chain, pre_tool_call, pre_llm_call, post_llm_call,
transform_llm_output, pre_gateway_dispatch) and drive a per-turn
HEALTHY->WOBBLING->FAILING->DOOMED state machine fed by the v1.5.x gate
counters (exposed via router_turn_counters — the gates record, the healer
reads). WOBBLING is log-only (the soft corrective-injection rung ships OFF:
experiment E stalled the endpoint 3/3 when nudging with tools enabled).
FAILING triggers FORCED SYNTHESIS (proven live on the poisoned session
20260719_012254_e3b7f659): every remaining API call has tools/tool_choice/
response_format stripped plus one corrective user message appended, thinking
is disabled at the chat-template level on the constrained_hosts allowlist
(selfheal.no_think — without it the model answers inside the reasoning
channel and content stays empty), and a pre_tool_call tripwire blocks
non-exempt tools for the rest of the turn.
DOOMED (final text still degenerate at the transform_llm_output finish
line) replaces the text with an honest diagnostic and schedules a
FRESH-SESSION RETRY through the gateway captured at pre_gateway_dispatch
(reset_session + agent evict + synthetic internal MessageEvent re-dispatch
carrying the verbatim question + diagnosis; max 1 retry per session/
question, then an honest failure report). Every private gateway symbol is
accessed through guard helpers that degrade gracefully — healer failure
never breaks normal operation.

Since v1.6.1 (corpus batch 1, 2026-07-19 — 4 failure classes):

* OUTGOING-ANSWER SECRET SCRUB (``tools.tool_search.secret_scrub``, default
  on; incident run 21: the model read a vllm-local API key
  (``vw_<55 base32 chars>``) from ``~/.config/opencode/opencode.json`` via
  read_file and PRINTED it in its final answer). hermes's own redactor
  (agent/redact.py, security.redact_secrets default-on) did run on the tool
  output but missed it twice over: ``vw_`` is not in its known-prefix list,
  and file reads use ``file_read=True`` -> ``code_file=True``, which
  deliberately skips the ``"apiKey": "..."`` JSON-field pass; and NOTHING
  upstream scrubs model-COMPOSED answers (transform_llm_output is the only
  surface that sees the final text). The scrub runs inside the selfheal
  transform_llm_output callback (hermes applies the FIRST non-empty string
  a hook returns — turn_finalizer.py:353-357 — so finisher and scrubber
  must compose in one callback) and redacts, conservatively: known
  vendor-prefixed key shapes (sk-…, ghp_…, hf_…, vw_…, JWTs, …), generic
  ``prefix_tail`` tokens with a 24+ char high-entropy tail (>= 2 digits),
  secret-keyword assignments (``VLLM_API_KEY=…``, ``api_key: …``,
  ``"apiKey": "…"``) whose VALUE looks secret-like (never pure numbers,
  placeholders, env lookups, or short plain words — ``max_tokens: 3000``
  and ``tokenizer: sentencepiece`` pass untouched), and ``Bearer <token>``
  credentials. Matches inside base64 data: URIs are exempt; bare hex
  blobs (git SHAs) and long prose/code identifiers never match. Each hit
  becomes ``[redacted]``; every scrub logs INFO with the hit count.

* NO_THINK_ALWAYS (``tools.tool_search.no_think_always``; plugin default
  off, install.py seeds the A/B-chosen value): extends the v1.6.0 forced-
  synthesis think-disable to EVERY chat_completions call on the
  constrained_hosts allowlist (same _cd_middleware, same gating as the
  clamps). Corpus batch 1 runs 7/13/18/25 leaked reasoning-channel
  deliberation into normal final answers WITHOUT literal <think> tags (the
  vLLM reasoning parser strips the tags; the leak is the model deliberating
  in the content channel), so a post-hoc tag strip cannot catch it — only
  disabling the channel can. An explicit enable_thinking already present in
  the request is never overridden.

* FORCED-SYNTHESIS honesty rewrite + classify_final intent-announcement
  detection live in selfheal.py (see its docstring).

Built against hermes-agent 0.18.2 tool_search API. If upstream renames the
patched symbols, register() logs a warning and does nothing — sessions fall
back to today's flat tool list (capability never degrades, only the token
bill). The provider registration and the steering hook are guarded the same
way: any missing upstream symbol logs a warning and skips just that feature.
Malformed overrides.yaml entries are warned about and skipped, never fatal,
in the same spirit.
"""
import copy
import json
import logging
import os
import re
import time

logger = logging.getLogger("hermes.plugins.router")

DEFAULT_KEEP = {"clarify", "todo", "memory", "vision_analyze",
                "text_to_speech", "skills_list", "web_search"}
# Kanban workers must keep their lifecycle tools flat: KANBAN_GUIDANCE in the
# system prompt is keyed on kanban_show being in valid_tool_names at build time.
KANBAN_LIFECYCLE = {"kanban_show", "kanban_list", "kanban_complete", "kanban_block",
                    "kanban_heartbeat", "kanban_comment", "kanban_create",
                    "kanban_link", "kanban_unblock"}

OVERRIDES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "overrides.yaml")


def _load_overrides():
    """Parse overrides.yaml into {tool: {"description": str|None, "params": {str: str}}}.

    Fail-safe by design: missing or empty file -> {} (no-op). A malformed file
    or entry logs a warning and is skipped; this function never raises.
    """
    try:
        if not os.path.exists(OVERRIDES_FILE):
            return {}
        import yaml
        with open(OVERRIDES_FILE, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except Exception as e:
        logger.warning("router plugin: cannot read/parse %s (%s); "
                       "description overrides disabled", OVERRIDES_FILE, e)
        return {}
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning("router plugin: %s root must be a mapping; "
                       "description overrides disabled", OVERRIDES_FILE)
        return {}
    tools = raw.get("tools")
    if tools is None:
        return {}
    if not isinstance(tools, dict):
        logger.warning("router plugin: %s 'tools' must be a mapping; "
                       "description overrides disabled", OVERRIDES_FILE)
        return {}

    out = {}
    for name, spec in tools.items():
        if not isinstance(spec, dict):
            logger.warning("router plugin: overrides entry %r is not a mapping; "
                           "skipped", name)
            continue
        unknown = set(spec) - {"description", "params"}
        if unknown:
            logger.warning("router plugin: overrides entry %r has unknown keys "
                           "%s; ignored", name, sorted(unknown))
        desc = spec.get("description")
        if desc is not None and not isinstance(desc, str):
            logger.warning("router plugin: overrides %r.description is not a "
                           "string; skipped", name)
            desc = None
        params = {}
        raw_params = spec.get("params")
        if raw_params is not None and not isinstance(raw_params, dict):
            logger.warning("router plugin: overrides %r.params is not a mapping; "
                           "skipped", name)
            raw_params = None
        if raw_params:
            for pname, pdesc in raw_params.items():
                if isinstance(pdesc, str):
                    params[str(pname)] = pdesc
                else:
                    logger.warning("router plugin: overrides %r.params.%r is not "
                                   "a string; skipped", name, pname)
        if desc is None and not params:
            continue
        out[str(name)] = {"description": desc, "params": params}
    return out


# ---------------------------------------------------------------------------
# v1.3.0: local web_extract backend
# ---------------------------------------------------------------------------

def _register_local_extract_provider(ctx):
    """Register the "local" extract-only web provider (web_local.py).

    Uses the supported plugin surface: PluginContext.register_web_search_provider
    -> agent.web_search_registry.register_provider. Re-registration on a force
    rescan is safe (the registry overwrites by name). Every failure mode logs
    a warning and leaves web_extract exactly as it was.
    """
    try:
        if not hasattr(ctx, "register_web_search_provider"):
            logger.warning(
                "router plugin: PluginContext has no register_web_search_provider; "
                "local web_extract backend NOT installed")
            return
        try:
            from . import web_local
        except ImportError:
            # Loaded outside the normal package context — fall back to a
            # path-based import of the sibling file.
            import importlib.util
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "web_local.py")
            spec = importlib.util.spec_from_file_location(
                "hermes_plugins_router_web_local", path)
            web_local = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(web_local)
        provider = web_local.build_provider()
        if provider is None:
            logger.warning(
                "router plugin: agent.web_search_provider.WebSearchProvider "
                "unavailable; local web_extract backend NOT installed")
            return
        ctx.register_web_search_provider(provider)
        logger.info("router plugin: local web_extract backend registered "
                    "(select with web.extract_backend: local)")
    except Exception:
        logger.warning("router plugin: local web_extract backend failed to "
                       "install; web_extract unchanged", exc_info=True)


# ---------------------------------------------------------------------------
# v1.3.0: web_search steering (stop endless re-searching)
# ---------------------------------------------------------------------------

STEER_AFTER_DEFAULT = 6
_STEER_WINDOW_S = 300  # idle reset for the no-turn_id fallback key only
_steer_counts = {}     # {(session_id, key): [count, last_ts]}


def _steer_after():
    """tools.tool_search.search_steer_after from config (default 6, <1 disables)."""
    try:
        from hermes_cli.config import load_config
        v = ((load_config().get("tools") or {}).get("tool_search") or {}).get(
            "search_steer_after")
        if v is not None:
            return int(v)
    except Exception:
        pass
    return STEER_AFTER_DEFAULT


def _steer_note(count):
    return (
        f"STEERING: this was web_search #{count} this turn — stop searching. "
        "The results you already have are enough to act on. Next action: pick "
        "the most relevant URL from your previous results and read it with "
        'web_extract (deferred tool — invoke as tool_call {"name": '
        '"web_extract", "arguments": {"urls": ["<url>"]}}), or answer the '
        "user now from what you have. Rephrased searches keep returning the "
        "same information."
    )


# v1.5.1: brand-pollution nudge — "hermes" searches drown in the fashion brand.
BRAND_NUDGE_DEFAULT = "on"
_BRAND_KEYWORDS = ("birkin", "kelly", "bag", "handbag", "scarf", "fashion",
                   "luxury")
_BRAND_NOTE = (
    "NOTE: these results look like the Hermès fashion brand — if you meant "
    'the AI agent, re-search with "Nous Research Hermes agent <keywords>".'
)


def _brand_nudge_setting():
    """tools.tool_search.brand_nudge ("on"/"off", default on).

    Accepts YAML-1.1 booleans (bare on/off parse as bool under safe_load),
    matching the other flags. Read per call — a config flip needs no restart.
    """
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        v = ((_load().get("tools") or {}).get("tool_search") or {}).get(
            "brand_nudge")
        if v is not None:
            if isinstance(v, bool):
                return "on" if v else "off"
            return str(v).strip().lower()
    except Exception:
        pass
    return BRAND_NUDGE_DEFAULT


def _brand_hit(text):
    """True when *text* (a title+url blob) matches the fashion-brand pattern:
    case-insensitive "hermès", or "hermes" plus any brand keyword."""
    t = str(text).lower()
    if "hermès" in t:
        return True
    return "hermes" in t and any(kw in t for kw in _BRAND_KEYWORDS)


def _brand_verdict(data, args):
    """(polluted, reason) — polluted is True when >= half of the web_search
    results look like the fashion brand and the query does not already
    disambiguate with "nous"; reason is a short human string for the log.

    *data* is the parsed web_search result dict ({"data": {"web": [...]}});
    *args* the tool-call arguments ({"query": ...}). Empty/absent result
    lists never trigger.
    """
    query = ""
    if isinstance(args, dict):
        query = str(args.get("query") or "")
    if "nous" in query.lower():
        return False, "query contains 'nous' (already disambiguated)"
    web = (data.get("data") or {}).get("web")
    if not isinstance(web, list) or not web:
        return False, "no web result list in payload"
    hits = 0
    for r in web:
        if not isinstance(r, dict):
            continue
        if _brand_hit(str(r.get("title") or "") + " " + str(r.get("url") or "")):
            hits += 1
    return hits * 2 >= len(web), f"{hits}/{len(web)} results match brand pattern"


def _steer_transform(tool_name="", result=None, status="", turn_id="",
                     session_id="", args=None, **_kw):
    """transform_tool_result hook: steering note + brand-pollution nudge.

    Counts only successful web_search results (status == "ok" as derived by
    model_tools._tool_result_observer_fields). Never blocks anything — on or
    past search N+1 it injects a "steering" field into the result JSON (or
    appends plain text when the result isn't JSON), and (v1.5.1) when the
    result set looks like the Hermès fashion brand it injects a one-line
    "brand_note" field (JSON results only — the detector needs titles/urls).
    Returns None (= leave result unchanged) in every other case, including
    its own failures.
    """
    try:
        # v1.15.0: web_extract pre-halt failure cap — observe FAILED extracts
        # (status=="error", the same signal hermes's same_tool_failure_halt
        # counts) so the pre_tool_call gate can block further extracts before the
        # halt ships an empty answer. Model-agnostic; done before the web_search
        # early-return below. The gate (_dup_gate) enforces the cap.
        if tool_name == "web_extract" and str(status) == "error":
            _note_extract_fail(session_id, turn_id)
        if tool_name != "web_search" or status != "ok" or not isinstance(result, str):
            return None
        data = None
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            pass
        changed = False

        # v1.5.1 brand-pollution nudge (JSON results only). v1.5.2: every
        # evaluation is observable — INFO on injection, DEBUG on pass-through.
        if data is not None and _brand_nudge_setting() == "on":
            polluted, why = _brand_verdict(data, args)
            if polluted:
                data["brand_note"] = _BRAND_NOTE
                changed = True
                logger.info(
                    "router plugin: brand-pollution nudge injected into "
                    "web_search result (%s; session=%s)", why,
                    session_id or "-")
            else:
                logger.debug(
                    "router plugin: brand nudge evaluated, not triggered "
                    "(%s)", why)

        # v1.3.0 steering counter (semantics unchanged; disabled when n < 1).
        n = _steer_after()
        if n >= 1:
            now = time.time()
            # Turn boundary: turn_id changes on each new user message (the tool
            # dispatcher threads it through, model_tools.py:1327). Fallback for
            # an empty turn_id: single per-session window, 300 s idle reset.
            key = (session_id, turn_id) if turn_id else (session_id, "window")
            entry = _steer_counts.get(key)
            if entry is None or (not turn_id and now - entry[1] > _STEER_WINDOW_S):
                entry = [0, now]
            entry[0] += 1
            entry[1] = now
            if len(_steer_counts) > 64:  # bound memory; stale turns never shrink
                cutoff = now - 3600
                for k in [k for k, v in _steer_counts.items() if v[1] < cutoff]:
                    _steer_counts.pop(k, None)
                if len(_steer_counts) > 64:
                    _steer_counts.clear()
            _steer_counts[key] = entry
            if entry[0] > n:
                note = _steer_note(entry[0])
                # v1.5.2: injections are observable in agent.log (the round-6
                # post-mortem misdiagnosed working steering as broken because
                # nothing logged).
                logger.info(
                    "router plugin: steering note injected (web_search #%d "
                    "this turn > threshold %d; session=%s turn=%s)",
                    entry[0], n, session_id or "-",
                    (turn_id or "-")[-12:])
                if data is not None:
                    data["steering"] = note
                    changed = True
                else:
                    # Non-JSON result: plain-text append (pre-v1.5.1 behavior).
                    return result + "\n\n" + note
        if not changed:
            return None
        return json.dumps(data, indent=2, ensure_ascii=False)
    except Exception:
        logger.debug("router plugin: steering hook failed; result passed "
                     "through unchanged", exc_info=True)
        return None


_steer_transform._router_steer = True  # dedup marker for force rescans


def _register_search_steering(ctx):
    """Register the steering hook, once per process (idempotent on rescans)."""
    try:
        if not hasattr(ctx, "register_hook"):
            logger.warning("router plugin: PluginContext has no register_hook; "
                           "web_search steering NOT installed")
            return
        try:  # dedup: a force rescan re-runs register() on the same manager
            hooks = ctx._manager._hooks.get("transform_tool_result", [])
            if any(getattr(cb, "_router_steer", False) for cb in hooks):
                return
        except Exception:
            pass  # private layout changed — worst case the hook double-counts
        ctx.register_hook("transform_tool_result", _steer_transform)
        logger.info("router plugin: web_search steering active "
                    "(after %d searches/turn; tools.tool_search."
                    "search_steer_after) + brand nudge (%s; tools."
                    "tool_search.brand_nudge)",
                    _steer_after(), _brand_nudge_setting())
    except Exception:
        logger.warning("router plugin: web_search steering failed to install",
                       exc_info=True)


# ---------------------------------------------------------------------------
# v1.5.2: duplicate-query hard gate (block identical web_search loops)
# v1.5.3: generalized duplicate-CALL gate (every tool, bridge-unwrapped)
# ---------------------------------------------------------------------------

DUP_QUERY_LIMIT_DEFAULT = 3
DUP_CALL_LIMIT_DEFAULT = 3
SEARCH_HARD_CAP_DEFAULT = 15
# Tools with legitimate identical repeats (clarify/todo/memory/text_to_speech)
# or cheap self-limiting reads (tool_search/tool_describe). tool_call is NOT
# here — it is unwrapped to the underlying tool, which decides.
DUP_CALL_EXEMPT_DEFAULT = ["clarify", "todo", "memory", "text_to_speech",
                           "tool_search", "tool_describe"]
_DUP_MAX_DISTINCT = 512   # bound per-turn memory for pathological query streams
_dup_state = {}           # {(session_id, key): [{norm_query: count}, last_ts]}
_dupc_state = {}          # {(session_id, key): [{call_key: count}, last_ts]}
# v1.5.4 hard search ceiling: one attempt counter per turn (no per-query dict)
# plus a "ceiling INFO already logged this turn" flag.
_cap_state = {}           # {(session_id, key): [attempts, last_ts, info_logged]}
# v1.6.0: shared per-turn observations for the selfheal module — the dup/cap
# gates RECORD here (searches, blocks, last-8 normalized queries) so the
# healer reads instead of re-counting. Same key scheme and memory bounds as
# the gates. No behavior change to the gates themselves.
_turn_obs = {}            # {(session_id, key): {"searches","blocks","queries","ts"}}
# v1.15.0: per-turn web_extract FAILURE counter (model-agnostic pre-halt cap).
EXTRACT_FAIL_CAP_DEFAULT = 3
_extract_fail_state = {}  # {(session_id, key): [fail_count, last_ts, block_logged]}


def _note_extract_fail(session_id, turn_id):
    """Record a FAILED web_extract for this turn (observed in the
    transform_tool_result hook via status=="error"). Never raises."""
    try:
        now = time.time()
        key = (session_id, turn_id) if turn_id else (session_id, "window")
        entry = _extract_fail_state.get(key)
        if entry is None or (not turn_id and now - entry[1] > _STEER_WINDOW_S):
            entry = [0, now, False]
        entry[0] += 1
        entry[1] = now
        if len(_extract_fail_state) > 64:  # bound memory; stale turns pruned
            cutoff = now - 3600
            for k in [k for k, v in _extract_fail_state.items()
                      if v[1] < cutoff]:
                _extract_fail_state.pop(k, None)
            if len(_extract_fail_state) > 64:
                _extract_fail_state.clear()
        _extract_fail_state[key] = entry
    except Exception:
        pass


def _extract_fail_count(key):
    """Failed-extract count for this turn key (0 on miss). Never raises."""
    try:
        entry = _extract_fail_state.get(key)
        return int(entry[0]) if entry else 0
    except Exception:
        return 0


def _obs_note(key, now, search_query=None, block=False):
    """Record a per-turn observation for the healer. Never raises."""
    try:
        entry = _turn_obs.get(key)
        if entry is None:
            entry = {"searches": 0, "blocks": 0, "queries": [], "ts": now}
        if search_query is not None:
            entry["searches"] += 1
            entry["queries"].append(search_query)
            if len(entry["queries"]) > 8:
                del entry["queries"][:-8]
        if block:
            entry["blocks"] += 1
        entry["ts"] = now
        if len(_turn_obs) > 64:  # bound memory; stale turns never shrink
            cutoff = now - 3600
            for k in [k for k, v in list(_turn_obs.items()) if v["ts"] < cutoff]:
                _turn_obs.pop(k, None)
            if len(_turn_obs) > 64:
                _turn_obs.clear()
        _turn_obs[key] = entry
    except Exception:
        pass


def router_turn_counters(session_id, turn_id):
    """v1.6.0 accessor: this turn's (searches, blocks, queries) snapshot.

    searches = ALL web_search attempts (blocked and successful alike, bridge
    unwrapped — the same count the v1.5.4 ceiling uses); blocks = calls the
    dup/cap gates blocked this turn; queries = the last 8 normalized
    web_search query strings (for the healer's similarity-collapse sensor).
    """
    key = (session_id, turn_id) if turn_id else (session_id, "window")
    entry = _turn_obs.get(key)
    if not entry:
        return {"searches": 0, "blocks": 0, "queries": []}
    return {"searches": entry["searches"], "blocks": entry["blocks"],
            "queries": list(entry["queries"])}


def _dup_limit():
    """tools.tool_search.dup_query_limit from config (default 3, <1 disables).

    Read per call — a config flip needs no restart. Invalid values fall back
    to the default.
    """
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        v = ((_load().get("tools") or {}).get("tool_search") or {}).get(
            "dup_query_limit")
        if v is not None and not isinstance(v, bool):
            return int(v)
    except Exception:
        pass
    return DUP_QUERY_LIMIT_DEFAULT


def _dup_call_limit():
    """tools.tool_search.dup_call_limit from config (default 3, <1 disables).

    Read per call — a config flip needs no restart. Invalid values fall back
    to the default; an explicit 0 (or any value < 1) disables the GENERAL
    gate only (the web_search query gate has its own dup_query_limit).
    """
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        v = ((_load().get("tools") or {}).get("tool_search") or {}).get(
            "dup_call_limit")
        if v is not None and not isinstance(v, bool):
            return int(v)
    except Exception:
        pass
    return DUP_CALL_LIMIT_DEFAULT


def _dup_call_exempt():
    """tools.tool_search.dup_call_exempt as a set (default: legit-repeat and
    self-limiting tools). A non-list config value falls back to the default;
    an explicit empty list means 'exempt nothing'."""
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        v = ((_load().get("tools") or {}).get("tool_search") or {}).get(
            "dup_call_exempt")
        if isinstance(v, list):
            return {str(x).strip() for x in v if str(x).strip()}
    except Exception:
        pass
    return set(DUP_CALL_EXEMPT_DEFAULT)


def _search_hard_cap():
    """tools.tool_search.search_hard_cap (default 15; explicit 0/<1 disables).

    Read per call — a config flip needs no restart. Invalid values fall back
    to the default.
    """
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        v = ((_load().get("tools") or {}).get("tool_search") or {}).get(
            "search_hard_cap")
        if v is not None and not isinstance(v, bool):
            return int(v)
    except Exception:
        pass
    return SEARCH_HARD_CAP_DEFAULT


def _extract_fail_cap():
    """tools.tool_search.extract_fail_cap (default 3; explicit 0/<1 disables).

    Read per call — a config flip needs no restart. After this many FAILED
    web_extract calls in a turn, further web_extract is blocked before the
    network so hermes's same_tool_failure_halt (8 consecutive failures) never
    ships an empty answer. Invalid values fall back to the default.
    """
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        v = ((_load().get("tools") or {}).get("tool_search") or {}).get(
            "extract_fail_cap")
        if v is not None and not isinstance(v, bool):
            return int(v)
    except Exception:
        pass
    return EXTRACT_FAIL_CAP_DEFAULT


def _cap_error(prior):
    return (
        f"search limit reached: you have run {prior} searches this turn — no "
        "more searches are allowed. Write your final answer NOW using the "
        "results you already have; if you truly need a page's content, use "
        "web_extract on a URL from your existing results."
    )


def _extract_cap_error(fails):
    return (
        f"web_extract limit reached: {fails} extract attempts this turn have "
        "failed (the pages are inaccessible/blocked and will not become "
        "readable by retrying). STOP calling web_extract. Write your FINAL "
        "ANSWER NOW, synthesizing from the web_search result snippets (titles, "
        "URLs, and summaries) you already have; cite those. If you genuinely "
        "have nothing usable, say so honestly — do not keep extracting."
    )


def _dup_error(prior):
    return (
        f"duplicate query loop: you already ran this exact search {prior} "
        "times this turn — the results will not change; STOP searching and "
        "either web_extract one of the URLs you already have or write your "
        "final answer now"
    )


def _dup_call_error(tool, prior):
    return (
        f"duplicate call loop: you already ran {tool} with these exact "
        f"arguments {prior} times this turn and got a result each time — "
        "the result will not change; use the results you already have, try "
        "DIFFERENT arguments, or write your final answer now."
    )


_EMPTY_QUERY_MSG = (
    "web_search was called with an empty query. Provide a specific, non-empty "
    "search query — the actual words you want to look up — and try again. If "
    "you do not need to search, write your answer directly instead."
)


def _dup_unwrap(tool_name, args):
    """(underlying_name, underlying_args) with the tool_call bridge peeled off.

    The agent executor unwraps tool_call BEFORE the pre_tool_call hook fires
    on the main dispatch paths (tool_executor.py:372/1007), so the gate
    usually sees the real tool already. This covers every other path (e.g.
    a failed executor-side resolve, direct handle_function_call callers):
    when the intercepted call is ``tool_call {"name": X, "arguments": {...}}``
    the counter is keyed on the UNDERLYING (X, arguments), so bridged and
    bare invocations of the same call share one counter. Up to 3 nesting
    levels; a JSON-string "arguments" is parsed like the resolver does; an
    unparseable string stays distinguishable instead of colliding on {}.
    """
    try:
        from tools import tool_search as ts
        bridge = ts.TOOL_CALL_NAME
    except Exception:
        bridge = "tool_call"
    name = str(tool_name or "")
    for _ in range(3):
        if name != bridge or not isinstance(args, dict):
            break
        inner = args.get("name")
        if not isinstance(inner, str) or not inner.strip():
            break
        payload = args.get("arguments")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {"_unparsed_arguments": payload}
        if not isinstance(payload, dict):
            payload = {}
        name = inner.strip()
        args = payload
    return name, (args if isinstance(args, dict) else {})


def _dup_norm_args(args):
    """Spec normalization: JSON with sorted keys, casefolded, ws-collapsed."""
    try:
        s = json.dumps(args, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"), default=str)
    except Exception:
        s = repr(args)
    return " ".join(s.casefold().split())


def _dup_count(state, key, ckey, now, turn_id):
    """Shared attempt counter: bump state[key][0][ckey], prune stale turns.

    Returns the new count for ckey, or None when the per-turn distinct cap
    would be exceeded (stay bounded, never block on a flood of DIFFERENT
    calls). Same memory bounds as the v1.5.2 gate: per-turn distinct cap
    _DUP_MAX_DISTINCT, global prune of turn keys idle > 1 h once the table
    exceeds 64 entries, full clear as the last resort.
    """
    entry = state.get(key)
    if entry is None or (not turn_id and now - entry[1] > _STEER_WINDOW_S):
        entry = [{}, now]
    counts = entry[0]
    if ckey not in counts and len(counts) >= _DUP_MAX_DISTINCT:
        return None
    counts[ckey] = counts.get(ckey, 0) + 1
    entry[1] = now
    if len(state) > 64:  # bound memory; stale turns never shrink
        cutoff = now - 3600
        for k in [k for k, v in state.items() if v[1] < cutoff]:
            state.pop(k, None)
        if len(state) > 64:
            state.clear()
    state[key] = entry
    return counts[ckey]


def _dup_gate(tool_name="", args=None, turn_id="", session_id="", **_kw):
    """pre_tool_call hook: hard anti-loop gate for byte-identical tool calls.

    v1.5.2 semantics kept as-is for web_search (normalized QUERY counted
    against ``tools.tool_search.dup_query_limit``); v1.5.3 adds the general
    gate: every non-exempt tool's normalized FULL ARGS are counted against
    ``tools.tool_search.dup_call_limit``; v1.5.4 adds the hard per-turn
    search ceiling: ALL web_search attempts (blocked and successful alike)
    are counted against ``tools.tool_search.search_hard_cap`` and past the
    cap every further web_search this turn is blocked regardless of query
    wording (web_extract stays uncapped). All counters key on
    (session_id, turn_id) — same turn keying as the steering counter, same
    idle-window fallback for an empty turn_id — and both count ATTEMPTS.
    web_search feeds both counters, so it is blocked at whichever limit is
    hit first (the lower one). The tool_call bridge is unwrapped first, so
    bridged and bare invocations of the same underlying call share a counter.
    On breach the hook returns the supported block directive: the dispatcher
    skips execution entirely (model_tools.py:1193 turns the message into the
    error-style result {"error": ...} the model sees, and emits
    post_tool_call with status="blocked"). Returns None (= allow) in every
    other case, including its own failures.
    """
    try:
        name, call_args = _dup_unwrap(tool_name, args)
        if not name:
            return None
        now = time.time()
        key = (session_id, turn_id) if turn_id else (session_id, "window")

        # v1.6.2 (corpus batch 2 T6): block empty/whitespace-only web_search
        # queries before the network. A q="" hit SearXNG and returned 400
        # (mt2). Return an error-style result telling the model to supply a
        # real query. Checked first so empty attempts never touch the counters.
        if name == "web_search":
            raw_q = call_args.get("query")
            if not isinstance(raw_q, str) or not raw_q.strip():
                logger.info("router plugin: blocked empty web_search query "
                            "(session=%s turn=%s)", session_id or "-",
                            (turn_id or "-")[-12:])
                return {"action": "block", "message": _EMPTY_QUERY_MSG}

        # v1.15.0: web_extract pre-halt cap. After extract_fail_cap FAILED
        # extracts this turn (recorded by _steer_transform), block further
        # web_extract BEFORE the network so hermes's same_tool_failure_halt
        # (8 consecutive failures) can't ship an empty answer; the block result
        # tells the model to synthesize from the search snippets it already has.
        # Model-agnostic (helps both the 27B and the frontier model). Kept
        # independent of the selfheal loop counters (no _obs_note here).
        if name == "web_extract":
            fcap = _extract_fail_cap()
            if fcap >= 1:
                fails = _extract_fail_count(key)
                if fails >= fcap:
                    entry = _extract_fail_state.get(key)
                    logged = bool(entry[2]) if entry else False
                    if not logged:
                        if entry:
                            entry[2] = True
                        logger.info(
                            "router plugin: web_extract pre-halt cap ACTIVATED "
                            "— further web_extract blocked this turn (%d failed "
                            "extracts >= cap %d; session=%s turn=%s)",
                            fails, fcap, session_id or "-",
                            (turn_id or "-")[-12:])
                    else:
                        logger.debug(
                            "router plugin: web_extract pre-halt cap blocked "
                            "web_extract (%d failures, cap %d; session=%s)",
                            fails, fcap, session_id or "-")
                    return {"action": "block",
                            "message": _extract_cap_error(fails)}

        # v1.6.0: record the attempt for the selfheal healer (reads, never
        # re-counts). Every web_search attempt + its normalized query.
        if name == "web_search":
            _obs_note(key, now, search_query=" ".join(
                str(call_args.get("query") or "").casefold().split()))

        # v1.5.4 hard per-turn search ceiling. Counts EVERY web_search
        # ATTEMPT — blocked and successful alike (this increment happens
        # before any dup verdict below, and dup-blocked attempts return
        # through this same hook, so they were already counted here).
        # Paraphrases can't escape: the counter is per-tool, not per-query.
        # web_extract is deliberately NOT capped by this ceiling (it is the
        # pivot the block message demands); it stays under the dup-call gate.
        if name == "web_search":
            cap = _search_hard_cap()
            if cap >= 1:
                entry = _cap_state.get(key)
                if entry is None or (not turn_id
                                     and now - entry[1] > _STEER_WINDOW_S):
                    entry = [0, now, False]
                entry[0] += 1
                entry[1] = now
                if len(_cap_state) > 64:  # bound memory; stale turns never shrink
                    cutoff = now - 3600
                    for k in [k for k, v in _cap_state.items()
                              if v[1] < cutoff]:
                        _cap_state.pop(k, None)
                    if len(_cap_state) > 64:
                        _cap_state.clear()
                _cap_state[key] = entry
                if entry[0] > cap:
                    prior = entry[0] - 1
                    if not entry[2]:
                        entry[2] = True
                        logger.info(
                            "router plugin: search hard cap ACTIVATED — "
                            "web_search blocked for the rest of the turn "
                            "(attempt %d > cap %d; session=%s turn=%s)",
                            entry[0], cap, session_id or "-",
                            (turn_id or "-")[-12:])
                    else:
                        logger.debug(
                            "router plugin: search hard cap blocked "
                            "web_search (attempt %d, cap %d; session=%s "
                            "turn=%s)", entry[0], cap, session_id or "-",
                            (turn_id or "-")[-12:])
                    _obs_note(key, now, block=True)
                    return {"action": "block", "message": _cap_error(prior)}

        # v1.5.2 web_search query gate (backward compat: own config+counter).
        q_breach = None
        if name == "web_search":
            q_limit = _dup_limit()
            if q_limit >= 1:
                norm_q = " ".join(
                    str(call_args.get("query") or "").casefold().split())
                if norm_q:
                    n = _dup_count(_dup_state, key, norm_q, now, turn_id)
                    if n is not None and n > q_limit:
                        q_breach = (n - 1, q_limit, norm_q)
                    elif n == q_limit:
                        logger.debug(
                            "router plugin: duplicate-query gate at limit "
                            "(%d/%d) for %r — next identical attempt will "
                            "be blocked", n, q_limit, norm_q[:120])

        # v1.5.3 general duplicate-call gate (all tools, exemptions apply).
        c_breach = None
        c_limit = _dup_call_limit()
        if c_limit >= 1 and name not in _dup_call_exempt():
            norm = _dup_norm_args(call_args)
            import hashlib
            ckey = name + "\x00" + hashlib.sha256(
                norm.encode("utf-8")).hexdigest()
            n = _dup_count(_dupc_state, key, ckey, now, turn_id)
            if n is not None and n > c_limit:
                c_breach = (n - 1, c_limit, norm)
            elif n == c_limit:
                logger.debug(
                    "router plugin: duplicate-call gate at limit (%d/%d) "
                    "for %s(%s) — next identical attempt will be blocked",
                    n, c_limit, name, norm[:120])

        if q_breach is not None:
            prior, q_limit, norm_q = q_breach
            logger.info(
                "router plugin: duplicate-query gate BLOCKED web_search "
                "(query already ran %d times this turn, limit %d; "
                "session=%s turn=%s): %r",
                prior, q_limit, session_id or "-", (turn_id or "-")[-12:],
                norm_q[:120])
            _obs_note(key, now, block=True)
            return {"action": "block", "message": _dup_error(prior)}
        if c_breach is not None:
            prior, c_limit, norm = c_breach
            logger.info(
                "router plugin: duplicate-call gate BLOCKED %s (identical "
                "args already ran %d times this turn, limit %d; "
                "session=%s turn=%s): %s",
                name, prior, c_limit, session_id or "-",
                (turn_id or "-")[-12:], norm[:160])
            _obs_note(key, now, block=True)
            return {"action": "block", "message": _dup_call_error(name, prior)}
        return None
    except Exception:
        logger.debug("router plugin: duplicate-call gate failed; call "
                     "allowed through", exc_info=True)
        return None


_dup_gate._router_dup_gate = True  # dedup marker for force rescans


def _register_dup_query_gate(ctx):
    """Register the pre_tool_call dup gate, once per process (rescans-safe)."""
    try:
        if not hasattr(ctx, "register_hook"):
            logger.warning("router plugin: PluginContext has no register_hook; "
                           "duplicate-call gate NOT installed")
            return
        try:  # dedup: a force rescan re-runs register() on the same manager
            hooks = ctx._manager._hooks.get("pre_tool_call", [])
            if any(getattr(cb, "_router_dup_gate", False) for cb in hooks):
                return
        except Exception:
            pass  # private layout changed — worst case the gate double-counts
        ctx.register_hook("pre_tool_call", _dup_gate)
        # v1.19.0: the clarify-finalize / runaway-wall pre_tool_call gate rides
        # the same surface (own dedup marker). Registered here so it installs
        # alongside the other pre_tool_call gates.
        _register_clarify_finalize(ctx)
        logger.info("router plugin: duplicate-call gate active "
                    "(identical call > %d times/turn is blocked, "
                    "tools.tool_search.dup_call_limit, %d exempt tool(s); "
                    "web_search query gate at %d, "
                    "tools.tool_search.dup_query_limit; search hard cap at "
                    "%d attempts/turn, tools.tool_search.search_hard_cap)",
                    _dup_call_limit(), len(_dup_call_exempt()), _dup_limit(),
                    _search_hard_cap())
    except Exception:
        logger.warning("router plugin: duplicate-query gate failed to install",
                       exc_info=True)


# ---------------------------------------------------------------------------
# v1.19.0: clarify-finalize + tier-independent runaway wall
#
# Batch-13 head-to-head found saandal v1.18.0 ties vanilla (43/44 each); the one
# saandal-side loss is idx43 ("finish the thing we discussed"): clarify_guard
# fired but the turn did NOT stop — the model ran ~11 session_search calls,
# inferred an unrelated prior task, called the `clarify` tool (which blocks in a
# headless -Q run), then kept firing terminal/todo calls to a 560s / 30-turn hard
# timeout. Two ADD-ONLY, fail-safe pre_tool_call gates close that class:
#
# * CLARIFY-FINALIZE (tools.tool_search.clarify_finalize, default on). ONLY on a
#   turn that clarify_guard flagged (recorded by _antifab_middleware when
#   apply_clarify_guard fires — so it can NEVER touch a clear prompt or a normal
#   multi-tool turn; the guard's 0-false-positive property is preserved) AND ONLY
#   once the model actually INVOKES the `clarify` tool: the turn is finalized. The
#   (blocking/interactive) clarify call is intercepted and the model is told to
#   emit its clarifying question as the final answer, and every further tool call
#   that turn is blocked. The model already asked — the turn ends there instead of
#   spelunking history. Tier-agnostic (the ambiguity failure is tier-independent).
#
# * RUNAWAY WALL (tools.tool_search.runaway_wall, default on). A tier-INDEPENDENT
#   backstop for a hung tool loop that no other guard caught (the strong-tier
#   escalation actuators are inert by design): once a turn's tool-call attempts
#   OR wall-clock cross a CONSERVATIVELY HIGH threshold, further tool calls are
#   blocked and the model is told to stop and report honestly — so a runaway ends
#   as an honest failure, not a hard external timeout. Thresholds are set well
#   above any legitimate long-research turn (runaway_call_cap default 50,
#   runaway_wall_secs default 480) so normal work is never cut.
#
# Both are fail-safe (any internal error -> allow the call) and roll back with one
# key. Neither can withhold or rewrite a real answer — they only block further
# TOOL calls and steer the model to finalize.
# ---------------------------------------------------------------------------

CLARIFY_FINALIZE_DEFAULT = "on"
RUNAWAY_WALL_DEFAULT = "on"
RUNAWAY_CALL_CAP_DEFAULT = 50
RUNAWAY_WALL_SECS_DEFAULT = 480
_CF_TABLE_MAX = 64
_CF_IDLE_S = 3600

# (session_id, turn_key) -> ts : this turn was flagged by clarify_guard.
_clarify_flagged = {}
# (session_id, turn_key) -> {"ts", "question", "logged"} : clarify was invoked on
# a flagged turn; the turn is now finalized (further tool calls blocked).
_clarify_armed = {}
# (session_id, turn_key) -> [first_ts, attempts, logged] : runaway-wall counters.
_runaway_state = {}


def _cf_reset_state():
    """Test helper: wipe the clarify-finalize / runaway-wall state."""
    _clarify_flagged.clear()
    _clarify_armed.clear()
    _runaway_state.clear()


def _cf_prune(state, now):
    """Bound a turn-keyed state table (idle > 1 h pruned once it exceeds the
    cap; full clear as the last resort). Never raises."""
    try:
        if len(state) <= _CF_TABLE_MAX:
            return
        cutoff = now - _CF_IDLE_S

        def _ts(v):
            if isinstance(v, dict):
                return float(v.get("ts") or 0)
            if isinstance(v, (list, tuple)) and v:
                return float(v[0] or 0)
            try:
                return float(v)
            except Exception:
                return 0.0
        for k in [k for k, v in list(state.items()) if _ts(v) < cutoff]:
            state.pop(k, None)
        if len(state) > _CF_TABLE_MAX:
            state.clear()
    except Exception:
        pass


def _cf_key(session_id, turn_id):
    return (session_id, turn_id) if turn_id else (session_id, "window")


def _clarify_finalize_setting():
    """tools.tool_search.clarify_finalize ("on"/"off", default on)."""
    return _ts_flag("clarify_finalize", CLARIFY_FINALIZE_DEFAULT)


def _runaway_wall_setting():
    """tools.tool_search.runaway_wall ("on"/"off", default on)."""
    return _ts_flag("runaway_wall", RUNAWAY_WALL_DEFAULT)


def _cf_int(key, default):
    """A tools.tool_search.<key> integer (invalid/absent -> default). <=0 keeps
    the caller's meaning (the caller treats <1 as 'disabled')."""
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        v = ((_load().get("tools") or {}).get("tool_search") or {}).get(key)
        if v is not None and not isinstance(v, bool):
            return int(v)
    except Exception:
        pass
    return default


def _note_clarify_flagged(session_id, turn_id):
    """Record that clarify_guard fired on this turn (called from the middleware
    only when apply_clarify_guard actually applied). Never raises."""
    try:
        now = time.time()
        _clarify_flagged[_cf_key(session_id, turn_id)] = now
        _cf_prune(_clarify_flagged, now)
    except Exception:
        pass


def _clarify_finalize_msg(question):
    """The block message that finalizes a flagged turn once clarify is invoked:
    the model already asked, so emit the question as the final answer and stop."""
    base = ("Your clarifying question has been recorded — this turn is complete. "
            "Do NOT call any more tools and do NOT search past sessions or history "
            "for what the request might mean. Write your clarifying question to the "
            "user now as your final answer")
    try:
        q = str(question or "").strip()
    except Exception:
        q = ""
    if q:
        return base + ', asking for the specific information you need: "' \
            + q[:400] + '"'
    return base + ", asking for the specific information you need to proceed."


_RUNAWAY_MSG = (
    "This turn has made too many tool calls / run too long without resolving. "
    "STOP calling tools now. Write your final answer to the user honestly: give "
    "the best result you can from what you already have, or state plainly that "
    "you could not complete the request and (if it was ambiguous) ask what they "
    "meant. Do not make any further tool calls.")


def _clarify_finalize_gate(tool_name="", args=None, turn_id="", session_id="",
                           **_kw):
    """pre_tool_call hook (v1.19.0): clarify-finalize + tier-independent runaway
    wall. ADD-only and fail-safe — returns None (allow) in every case except the
    two deliberate blocks below, and on ANY internal error.

    1. CLARIFY-FINALIZE — only on a clarify_guard-flagged turn: when the model
       invokes `clarify`, capture its question, arm the turn, and block the
       (blocking) clarify call with a "emit your question as the final answer"
       directive; block every further tool call on the armed turn. Never fires on
       an unflagged turn, so a normal multi-tool turn and a clear prompt are
       untouched (0-FP preserved).
    2. RUNAWAY WALL — tier-independent: past a conservatively high per-turn
       tool-call count or wall-clock, block further tools and steer the model to
       finalize honestly. Set high enough that legitimate long research never
       trips it.
    """
    try:
        name, call_args = _dup_unwrap(tool_name, args)
        if not name:
            return None
        now = time.time()
        key = _cf_key(session_id, turn_id)

        # ---- (1) clarify-finalize (only on a flagged turn) ------------------
        if _clarify_finalize_setting() == "on":
            armed = _clarify_armed.get(key)
            if armed is not None:
                # Turn already finalized this turn — block every further tool.
                logger.debug("router plugin: clarify-finalize blocked %s after "
                             "clarify (session=%s)", name, session_id or "-")
                return {"action": "block",
                        "message": _clarify_finalize_msg(armed.get("question"))}
            if name == "clarify" and key in _clarify_flagged:
                q = ""
                if isinstance(call_args, dict):
                    q = str(call_args.get("question") or "").strip()
                _clarify_armed[key] = {"ts": now, "question": q, "logged": True}
                _cf_prune(_clarify_armed, now)
                logger.info(
                    "router plugin: clarify-finalize ARMED — clarify invoked on "
                    "a clarify_guard-flagged turn; finalizing with the question "
                    "and blocking further tool calls (session=%s turn=%s)",
                    session_id or "-", (turn_id or "-")[-12:])
                return {"action": "block", "message": _clarify_finalize_msg(q)}

        # ---- (2) tier-independent runaway wall ------------------------------
        if _runaway_wall_setting() == "on":
            call_cap = _cf_int("runaway_call_cap", RUNAWAY_CALL_CAP_DEFAULT)
            wall_secs = _cf_int("runaway_wall_secs", RUNAWAY_WALL_SECS_DEFAULT)
            entry = _runaway_state.get(key)
            if entry is None or (not turn_id and now - entry[0] > _CF_IDLE_S):
                entry = [now, 0, False]
            entry[1] += 1
            _runaway_state[key] = entry
            _cf_prune(_runaway_state, now)
            over_calls = call_cap >= 1 and entry[1] > call_cap
            over_wall = wall_secs >= 1 and (now - entry[0]) > wall_secs
            if over_calls or over_wall:
                if not entry[2]:
                    entry[2] = True
                    logger.info(
                        "router plugin: runaway wall ACTIVATED — blocking "
                        "further tool calls this turn (attempts=%d cap=%d, "
                        "wall=%ds cap=%ds; session=%s turn=%s)",
                        entry[1], call_cap, int(now - entry[0]), wall_secs,
                        session_id or "-", (turn_id or "-")[-12:])
                else:
                    logger.debug("router plugin: runaway wall blocked %s "
                                 "(session=%s)", name, session_id or "-")
                return {"action": "block", "message": _RUNAWAY_MSG}
        return None
    except Exception:
        logger.debug("router plugin: clarify-finalize/runaway gate failed; "
                     "call allowed through", exc_info=True)
        return None


_clarify_finalize_gate._router_clarify_finalize = True  # dedup marker


def _register_clarify_finalize(ctx):
    """Register the clarify-finalize / runaway-wall pre_tool_call hook, once per
    process (rescans-safe). Fail-safe: any error skips just this feature."""
    try:
        if not hasattr(ctx, "register_hook"):
            logger.warning("router plugin: PluginContext has no register_hook; "
                           "clarify-finalize NOT installed")
            return
        try:  # dedup: a force rescan re-runs register() on the same manager
            hooks = ctx._manager._hooks.get("pre_tool_call", [])
            if any(getattr(cb, "_router_clarify_finalize", False)
                   for cb in hooks):
                return
        except Exception:
            pass
        ctx.register_hook("pre_tool_call", _clarify_finalize_gate)
        logger.info(
            "router plugin: clarify-finalize active (clarify on a "
            "clarify_guard-flagged turn ends the turn, "
            "tools.tool_search.clarify_finalize=%s) + runaway wall "
            "(tools.tool_search.runaway_wall=%s, call_cap=%d, wall=%ds)",
            _clarify_finalize_setting(), _runaway_wall_setting(),
            _cf_int("runaway_call_cap", RUNAWAY_CALL_CAP_DEFAULT),
            _cf_int("runaway_wall_secs", RUNAWAY_WALL_SECS_DEFAULT))
    except Exception:
        logger.warning("router plugin: clarify-finalize failed to install",
                       exc_info=True)


# ---------------------------------------------------------------------------
# v1.4.0: constrained tool-call decoding (vLLM structural_tag response_format)
# ---------------------------------------------------------------------------

CONSTRAINED_HOSTS_DEFAULT = ["vllm.example.com"]
_CD_CACHE_MAX = 32
_cd_cache = {}                       # {sha256(tools json): response_format}
_cd_stats = {"hits": 0, "misses": 0}  # introspectable for tests
_cd_warned = set()                    # one-shot warning keys
_cd_applied_logged = [False]          # first application logs INFO, rest DEBUG


def _cd_warn_once(key, msg, *args, **kw):
    if key in _cd_warned:
        return
    _cd_warned.add(key)
    logger.warning(msg, *args, **kw)


def _cd_settings():
    """(flag, hosts) from config: tools.tool_search.constrained_decoding /
    constrained_hosts. Flag defaults to "off"; hosts to CONSTRAINED_HOSTS_DEFAULT.
    """
    flag, hosts = "off", list(CONSTRAINED_HOSTS_DEFAULT)
    try:
        # Read-only fast path (no defensive deepcopy) — this runs once per
        # API call. We never mutate the returned dict.
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        ts = ((_load().get("tools") or {}).get("tool_search") or {})
        raw_flag = ts.get("constrained_decoding")
        if raw_flag is not None:
            # YAML 1.1 (hermes uses safe_load) parses bare on/off as
            # booleans — accept those alongside the documented strings.
            if isinstance(raw_flag, bool):
                flag = "on" if raw_flag else "off"
            else:
                flag = str(raw_flag).strip().lower()
        raw_hosts = ts.get("constrained_hosts")
        if isinstance(raw_hosts, list) and raw_hosts:
            hosts = [str(h).strip().lower() for h in raw_hosts if str(h).strip()]
    except Exception:
        pass  # unreadable config = feature stays off / defaults
    return flag, hosts


def _cd_sanitize_params(tools):
    """Deep-copied tools with pattern/format keywords stripped for xgrammar.

    Uses the shipped tools/schema_sanitizer.strip_pattern_and_format (which
    mutates in place — hence the copy). If the import fails, parameters are
    embedded as-is and a warning is logged once.
    """
    tools = copy.deepcopy(tools)
    try:
        from tools.schema_sanitizer import strip_pattern_and_format
    except Exception:
        _cd_warn_once(
            "sanitizer-import",
            "router plugin: tools.schema_sanitizer.strip_pattern_and_format "
            "unavailable; embedding tool parameters unsanitized in "
            "structural_tag (xgrammar may reject some pattern/format keywords)",
            exc_info=True)
        return tools
    strip_pattern_and_format(tools)
    return tools


def _cd_build_response_format(tools):
    """Build the legacy-shape structural_tag response_format for *tools*.

    Deterministic (structures sorted by tool name) for xgrammar cache
    friendliness; results cached on a hash of the tools array. Returns None
    when no usable structure can be built.
    """
    import hashlib
    try:
        key = hashlib.sha256(
            json.dumps(tools, sort_keys=True, separators=(",", ":"),
                       default=str).encode("utf-8")).hexdigest()
    except Exception:
        key = None
    if key is not None and key in _cd_cache:
        _cd_stats["hits"] += 1
        return _cd_cache[key]
    _cd_stats["misses"] += 1

    structures = []
    for tool in _cd_sanitize_params(tools):
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        params = fn.get("parameters")
        if not isinstance(params, dict) or not params:
            params = {"type": "object"}
        structures.append({
            "begin": "<tool_call>",
            "schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": [name]},
                    "arguments": params,
                },
                "required": ["name", "arguments"],
            },
            "end": "</tool_call>",
        })
    if not structures:
        return None
    structures.sort(key=lambda s: s["schema"]["properties"]["name"]["enum"][0])
    response_format = {
        "type": "structural_tag",
        "structures": structures,
        "triggers": ["<tool_call>"],
    }
    if key is not None:
        if len(_cd_cache) >= _CD_CACHE_MAX:
            _cd_cache.clear()
        _cd_cache[key] = response_format
    return response_format


def _cd_host(base_url):
    try:
        from urllib.parse import urlsplit
        return (urlsplit(base_url or "").hostname or "").lower()
    except Exception:
        return ""


# v1.5.1: model-side decoding mitigations (same middleware, same host gating).
MAX_TOKENS_CAP_DEFAULT = 3000
# v1.8.2: frequency_penalty is now OFF by default. A/B (2026-07-19, spec-decode
# permanently off) proved it was the CAUSE of long-output TAIL COLLAPSE, not a
# cure: frequency_penalty scales with a token's running COUNT, so across a long
# structured answer it progressively forbids the legitimately-recurring tokens
# (list markers, table separators, "Linux", the EOS token itself) and derails
# greedy decoding into word-salad / token-fusion / synonym-avalanche exactly at
# the tail (freq 0.3 ran BOTH multi-item probes to the 2200 cap without ever
# stopping; freq 0.5 melted into off-topic narrative). presence_penalty is FLAT
# (one-shot per token regardless of count) so it never accumulates and never
# derails — the winning combo is frequency_penalty=0 + presence_penalty=0.3,
# under which every probe completed ALL items and stopped naturally well under
# the cap with legit structural repeats preserved. Both remain configurable.
FREQUENCY_PENALTY_DEFAULT = 0.0
PRESENCE_PENALTY_DEFAULT = 0.3


def _mt_cap():
    """tools.tool_search.max_tokens_cap (default 3000; <= 0 disables).

    Absent key = plugin default 3000 (install.py seeds 3000 only-when-absent,
    so the file usually says it explicitly); explicit 0 disables clamping.
    Invalid values fall back to the default.
    """
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        v = ((_load().get("tools") or {}).get("tool_search") or {}).get(
            "max_tokens_cap")
        if v is not None and not isinstance(v, bool):
            return int(v)
    except Exception:
        pass
    return MAX_TOKENS_CAP_DEFAULT


def _fp_value():
    """tools.tool_search.frequency_penalty (default 0.0 = OFF as of v1.8.2;
    any value re-enables; explicit 0 disables injection).

    Invalid values fall back to the default (FREQUENCY_PENALTY_DEFAULT = 0.0);
    0 (or 0.0) returns 0.0, which the middleware treats as "do not inject".
    """
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        v = ((_load().get("tools") or {}).get("tool_search") or {}).get(
            "frequency_penalty")
        if v is not None and not isinstance(v, bool):
            return float(v)
    except Exception:
        pass
    return FREQUENCY_PENALTY_DEFAULT


def _pp_value():
    """tools.tool_search.presence_penalty (default 0.3; explicit 0 disables).

    v1.8.2 anti-tail-collapse. presence_penalty is FLAT (does not accumulate
    with token count the way frequency_penalty does) so it curbs genuine loops
    without derailing long structured output. Invalid values fall back to the
    default; 0 (or 0.0) returns 0.0, which the middleware treats as "do not
    inject".
    """
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        v = ((_load().get("tools") or {}).get("tool_search") or {}).get(
            "presence_penalty")
        if v is not None and not isinstance(v, bool):
            return float(v)
    except Exception:
        pass
    return PRESENCE_PENALTY_DEFAULT


# ---------------------------------------------------------------------------
# v1.9.0: TASK-AWARE coding / large-output accommodation.
#
# The tight bounds max_tokens_cap=3000 + selfheal.fail_wall_secs=150 are
# load-bearing for the proven research/chat batch (they convert runaway
# repetition loops into fast, recoverable turns) and MUST NOT be loosened
# globally. But those same bounds actively break a legitimate CODING /
# large-output turn: a real telegram turn ("write a c code for quake like
# game", session 20260720_031846) hit out=3000 on the batch that carried the
# write_file call, so the file's content was truncated to "" (0 bytes written)
# and the model never emitted code; the 150s wall then forced-synthesised a
# "here's my plan" text before any file could be written; and every follow-up
# ("progress?") re-planned from scratch (re-clarify scope, re-run dependency
# recon) instead of continuing.
#
# This accommodation detects a coding turn with LOW-FALSE-POSITIVE signals and,
# ONLY for such turns, raises the max_tokens cap (coding_max_tokens, default
# 8000) and the selfheal S7 wall (coding_wall_secs, default 360, applied in
# selfheal.py). Every non-coding turn keeps 3000 / 150 EXACTLY. Master switch
# tools.tool_search.coding_mode ("auto" = on, default; "off" = behave exactly
# as before). Detection is a pure function so it is unit-testable and shared
# (selfheal reads it via the host bridge).
# ---------------------------------------------------------------------------

CODING_MODE_DEFAULT = "auto"          # "auto" | "off"
CODING_MAX_TOKENS_DEFAULT = 8000      # coding-turn max_tokens cap
CODING_WALL_SECS_DEFAULT = 360        # coding-turn selfheal S7 wall (read in selfheal)

# Tools whose presence in the recent history means the model is actively
# BUILDING code (the "still working on the same code" continuation signal).
_CODE_BUILD_TOOLS = ("write_file", "execute_code")

# Strong producing verbs — the user is asking us to EMIT an artifact.
_CV = (r"write|create|implement|build|generate|develop|refactor|rewrite|"
       r"code|program|make|design|port|add|write me|code up|whip up|"
       r"put together|draft")
# Concrete coding-artifact nouns (things the model must actually output).
_CN = (r"code|program|programme|script|function|subroutine|routine|class|"
       r"module|package|library|app|application|game|engine|website|web ?app|"
       r"web ?page|webpage|api|endpoint|micro-?service|server|daemon|bot|cli|"
       r"command[- ]?line tool|parser|lexer|tokenizer|compiler|interpreter|"
       r"algorithm|data ?structure|makefile|dockerfile|shader|driver|plugin|"
       r"extension|snippet|one[- ]?liner|regex|stored procedure|"
       r"snake|tetris|pong|breakout|pac-?man|minesweeper|fizzbuzz|"
       r"calculator|scraper|crawler|chatbot|unit tests?|test suite|benchmark")
# Programming languages (full-name set kept separate from the short/ambiguous
# abbreviations so ".py files" / "in Go for a walk" never trip the noun path).
_CL_ANY = (r"c\+\+|cpp|c#|python|rust|golang|go|java|javascript|typescript|"
           r"bash|shell|zsh|powershell|sql|html|css|php|ruby|kotlin|swift|"
           r"scala|haskell|lua|perl|assembly|fortran|cobol|objective-c|dart|"
           r"elixir|erlang|clojure|julia|nim|zig|solidity|verilog|vhdl|c")
_CL_FULL = (r"c\+\+|cpp|python|rust|golang|java|javascript|typescript|bash|"
            r"powershell|php|ruby|kotlin|swift|scala|haskell|perl|assembly|"
            r"fortran|objective-c|dart|elixir|erlang|clojure|julia|solidity")
# Comparison / explanation / research FRAMING — dampens the classic false
# positive ("Compare Python, Rust, Go, TypeScript for writing a small CLI
# tool, in a table"): that is a research/comparison turn, not a produce-code
# request, and must keep the tight bounds.
_CODING_NEG = re.compile(
    r"\bcompare\b|\bcomparison\b|\bvs\.?\b|\bversus\b|\bdifference between\b|"
    r"\bpros and cons\b|\btrade[- ]?offs?\b|\bin a table\b|\bas a table\b|"
    r"\bexplain\b|\bwhich (?:is|one|language|tool|framework)\b|"
    r"\bbest (?:language|tool|framework|way|approach)\b|"
    # creative-writing framing: "write a story/poem about a video game" is prose,
    # not a produce-code request (kills the _RE_VN 'game'/'engine' noun FP). Note
    # "script" is deliberately excluded — "python script" is real code.
    r"\b(?:short )?story\b|\bnovel\b|\bpoem\b|\bhaiku\b|\bessay\b|\blyrics\b|"
    r"\bblog post\b|\bscreenplay\b")

_RE_VN = re.compile(
    r"\b(?:%s)\b[^.?!\n]{0,40}?\b(?:%s)\b" % (_CV, _CN), re.I)   # verb -> noun
_RE_NV = re.compile(
    r"\b(?:%s)\b[^.?!\n]{0,25}?\b(?:%s)\s+(?:it|this|that|them)\b"
    % (_CN, _CV), re.I)                                         # noun -> verb
_RE_VLANG = re.compile(
    r"\b(?:%s)\b[^.?!\n]{0,50}?\bin\s+(?:%s)\b" % (_CV, _CL_ANY), re.I)
_RE_LANGN = re.compile(
    r"\b(?:%s)\b[ -]?(?:program|programme|script|function|module|class|"
    r"app|library|code|snippet|one[- ]?liner)\b" % _CL_FULL, re.I)
# "write a c program/code" — allow bare "c"/"go" ONLY tight before an artifact.
_RE_CPROG = re.compile(
    r"\bc\s+(?:program|programme|code|function)\b", re.I)
# Game/graphics BUILD signals — tokens that essentially only appear when the
# user wants a program produced (a raycaster, a pygame sprite loop, ...). These
# rescue descriptive game prompts whose verb->noun distance blows past the tight
# _RE_VN window ("Write a minimal Quake/Doom-style first-person raycaster game
# in a single Python file using pygame" — "write"..."game" is ~51 chars). Paired
# with a producing verb (order-independent) so a bare "what is pygame?" question
# stays OUT, and still gated behind _CODING_NEG so "compare/explain pygame" too.
_RE_PRODVERB = re.compile(r"\b(?:%s)\b" % _CV, re.I)
_RE_GAMEDEV = re.compile(
    r"\b(?:pygame|pyglet|raylib|raycast(?:er|ing)?|opengl|webgl|sdl2?|"
    r"tilemap|sprite ?sheet|game ?loop|platformer|roguelike|"
    r"side[- ]?scroll(?:er|ing)?|game engine|voxel|ray[- ]?trac(?:er|ing))\b",
    re.I)
# Short-continuation cue ("progress?", "continue", "go on", "да, продолжай").
_RE_CONT = re.compile(
    r"\b(continue|proceed|go on|go ahead|keep going|carry on|next|more|"
    r"finish|progress|status|update|resume|да|продолж|дальше|ещё|еще|"
    r"давай)\b|^\s*[?？]+\s*$", re.I)


def _coding_mode():
    """tools.tool_search.coding_mode ("auto" default | "off"). Any value other
    than a case-insensitive "off" is treated as "auto"; never raises."""
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        v = ((_load().get("tools") or {}).get("tool_search") or {}).get(
            "coding_mode")
        if v is not None:
            return "off" if str(v).strip().lower() == "off" else "auto"
    except Exception:
        pass
    return CODING_MODE_DEFAULT


def _coding_max_tokens():
    """tools.tool_search.coding_max_tokens (default 8000). Invalid -> default."""
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        v = ((_load().get("tools") or {}).get("tool_search") or {}).get(
            "coding_max_tokens")
        if v is not None and not isinstance(v, bool):
            return int(v)
    except Exception:
        pass
    return CODING_MAX_TOKENS_DEFAULT


def _coding_wall_secs():
    """tools.tool_search.coding_wall_secs (default 360). Invalid -> default."""
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        v = ((_load().get("tools") or {}).get("tool_search") or {}).get(
            "coding_wall_secs")
        if v is not None and not isinstance(v, bool):
            return int(v)
    except Exception:
        pass
    return CODING_WALL_SECS_DEFAULT


def _ct_text(content):
    """Plain text of a message content (str or OpenAI parts list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                out.append(p["text"])
            elif isinstance(p, str):
                out.append(p)
        return "\n".join(out)
    return "" if content is None else str(content)


def coding_intent(text):
    """True iff *text* is an imperative request to PRODUCE code/a program/a
    script/etc. Pure, low-false-positive: a comparison/explanation framing
    ("compare X and Y", "in a table", "explain") suppresses the match so
    research/chat turns keep the tight bounds. Never raises."""
    try:
        if not text:
            return False
        t = text.lower()
        if len(t) > 4000:
            t = t[:4000]
        if _CODING_NEG.search(t):
            return False
        return bool(_RE_VN.search(t) or _RE_NV.search(t) or _RE_VLANG.search(t)
                    or _RE_LANGN.search(t) or _RE_CPROG.search(t)
                    or (_RE_PRODVERB.search(t) and _RE_GAMEDEV.search(t)))
    except Exception:
        return False


def _msg_builds_code(m):
    """True if assistant message *m* calls write_file/execute_code (directly or
    through the tool_call bridge), or is a tool result from one of them."""
    try:
        if not isinstance(m, dict):
            return False
        if m.get("role") == "tool" and m.get("name") in _CODE_BUILD_TOOLS:
            return True
        tcs = m.get("tool_calls")
        if not isinstance(tcs, list):
            return False
        for tc in tcs:
            fn = tc.get("function") if isinstance(tc, dict) else None
            name = (fn or {}).get("name") if isinstance(fn, dict) else None
            if name in _CODE_BUILD_TOOLS:
                return True
            if name == "tool_call":
                # bridge: {"name":"tool_call","arguments":{"name":"write_file"}}
                args = (fn or {}).get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = None
                if isinstance(args, dict) and args.get("name") in _CODE_BUILD_TOOLS:
                    return True
        return False
    except Exception:
        return False


def _in_background_review(messages=None):
    """True when running inside hermes's background memory/skill review
    (agent/background_review.py) — a forked, never-delivered session that runs
    our middleware in a daemon thread under a hard tool-whitelist. User-facing
    accommodations (the coding cap/wall bump) must NOT apply there. Detected via
    the review thread name, the thread-local tool whitelist, or the replayed
    review harness prompt. Fail-safe (False on error)."""
    try:
        import threading
        if str(getattr(threading.current_thread(), "name", "")
               ).startswith("bg-review"):
            return True
    except Exception:
        pass
    try:
        from hermes_cli import plugins as _hp
        if getattr(getattr(_hp, "_thread_tool_whitelist", None),
                   "allowed", None) is not None:
            return True
    except Exception:
        pass
    try:
        for m in (messages or []):
            if isinstance(m, dict) and m.get("role") in ("user", "system"):
                c = m.get("content")
                if isinstance(c, str) and c.lstrip().startswith(
                        "Review the conversation above"):
                    return True
    except Exception:
        pass
    return False


def is_coding_turn(messages):
    """True iff the turn is a coding / large-output turn deserving the raised
    cap+wall. Pure, fail-safe (returns False on any error).

    Two signals:
      1. The CURRENT user ask is itself a coding request (coding_intent).
      2. CONTINUATION: the current user message is a short "keep going"/
         "progress?" cue AND the recent history shows the model actively
         building code (write_file/execute_code) for a request that WAS a
         coding ask — so "progress?" after "write a C game" stays in coding
         mode instead of re-planning under the tight bounds. A vague follow-up
         to a pure computation turn (execute_code but no coding-intent user
         message) is deliberately NOT promoted, keeping computation turns tight.
    """
    try:
        if not isinstance(messages, list) or not messages:
            return False
        last_user = ""
        for m in reversed(messages):
            if isinstance(m, dict) and m.get("role") == "user":
                last_user = _ct_text(m.get("content"))
                break
        if coding_intent(last_user):
            return True
        lu = (last_user or "").strip()
        if len(lu) <= 40 and _RE_CONT.search(lu):
            window = messages[-50:]
            building = any(_msg_builds_code(m) for m in window)
            if building:
                for m in window:
                    if (isinstance(m, dict) and m.get("role") == "user"
                            and coding_intent(_ct_text(m.get("content")))):
                        return True
        return False
    except Exception:
        return False


CODING_NOTE = (
    "Coding task detected. Write the actual code NOW — call write_file to save "
    "the complete file (or emit the full code directly in your reply). Do not "
    "re-plan, do not re-ask scope you already have, and do not run more "
    "environment/dependency reconnaissance before writing. You write code "
    "YOURSELF using write_file, execute_code, and terminal — you cannot and "
    "must not hand the task off to \"Claude Code\", Codex, or any other agent; "
    "there is no such delegation configured. Deliver one complete file at a "
    "time; if it is large, write it fully in the file rather than stopping to "
    "ask permission to continue.")


def _inject_coding_note(request):
    """Append CODING_NOTE once as a cache-stable system message. Idempotent."""
    try:
        msgs = request.get("messages")
        if not isinstance(msgs, list):
            return False
        for mm in msgs:
            if (isinstance(mm, dict) and mm.get("role") == "system"
                    and _ct_text(mm.get("content")) == CODING_NOTE):
                return False
        msgs.append({"role": "system", "content": CODING_NOTE})
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# v1.6.1: outgoing-answer secret scrub (tools.tool_search.secret_scrub)
# ---------------------------------------------------------------------------

SECRET_SCRUB_DEFAULT = "on"
NO_THINK_ALWAYS_DEFAULT = "off"
_SCRUB_REPLACEMENT = "[redacted]"

# Known vendor key prefixes (subset of agent/redact.py's list that can appear
# in answers, plus vw_ — the vllm-local shape that actually leaked in corpus
# batch 1 run 21 and is NOT in the upstream list).
_SCRUB_PREFIX_PATTERNS = [
    r"sk-[A-Za-z0-9_-]{10,}",                 # OpenAI / Anthropic / OpenRouter
    r"sk_(?:live|test)_[A-Za-z0-9]{10,}",     # Stripe
    r"ghp_[A-Za-z0-9]{10,}",                  # GitHub PAT
    r"github_pat_[A-Za-z0-9_]{10,}",          # GitHub fine-grained PAT
    r"gh[ousr]_[A-Za-z0-9]{10,}",             # GitHub OAuth/user/server/refresh
    r"xox[baprs]-[A-Za-z0-9-]{10,}",          # Slack
    r"xapp-\d+-[A-Za-z0-9-]{10,}",            # Slack app-level
    r"AIza[A-Za-z0-9_-]{30,}",                # Google
    r"AKIA[A-Z0-9]{16}",                      # AWS access key id
    r"hf_[A-Za-z0-9]{10,}",                   # HuggingFace
    r"gsk_[A-Za-z0-9]{10,}",                  # Groq
    r"pplx-[A-Za-z0-9]{10,}",                 # Perplexity
    r"xai-[A-Za-z0-9]{30,}",                  # xAI
    r"npm_[A-Za-z0-9]{10,}",                  # npm
    r"pypi-[A-Za-z0-9_-]{10,}",               # PyPI
    r"tvly-[A-Za-z0-9]{10,}",                 # Tavily
    r"vw_[A-Za-z0-9]{20,}",                   # vllm-local (run-21 leak)
    r"eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_=-]{4,}){0,2}",  # JWT
]
_SCRUB_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:" + "|".join(_SCRUB_PREFIX_PATTERNS)
    + r")(?![A-Za-z0-9_-])")

# Generic prefixed token: short letter prefix + "_" + long high-entropy tail.
# The tail must be one contiguous 24+ char alnum run (no underscores — snake_
# case identifiers can't chain into it) AND contain >= 2 digits (checked in
# code, so `some_verylongidentifiername` never trips). The lookbehind also
# excludes . + / — a match can never start mid-URL-path, mid-word, or inside
# a base64 blob (base64's only non-alnum chars are + / = and _ - in the
# url-safe alphabet, all of which block the boundary).
_SCRUB_GENERIC_RE = re.compile(
    r"(?<![A-Za-z0-9_.+/-])([A-Za-z]{2,10}_[A-Za-z0-9]{24,})(?![A-Za-z0-9_-])")

# Secret-keyword assignment: `VLLM_API_KEY=...`, `api_key: ...`,
# `"apiKey": "..."`. The keyword must be part of the KEY immediately before
# = or :. Value verdicts are decided by _scrub_secretish below.
_SCRUB_KEYWORDS = (r"(?:api[_-]?key|apikey|api[_-]?token|access[_-]?token|"
                   r"auth[_-]?token|refresh[_-]?token|token|secret|passwd|"
                   r"password|credentials?)")
_SCRUB_ASSIGN_RE = re.compile(
    r"([A-Za-z0-9_.\-]*" + _SCRUB_KEYWORDS + r"[A-Za-z0-9_.\-]*[\"'`]?"
    r"\s*[=:]\s*)([\"'`]?)([^\s\"'`;,(){}\[\]]{8,})(\2)",
    re.IGNORECASE)

_SCRUB_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=-]{16,})")

# Values that are provably NOT leaked secrets: booleans/None, pure numbers
# (max_tokens: 65536), mask/placeholder text, env lookups, templates.
_SCRUB_SAFE_VALUE_RE = re.compile(
    r"(?i)^(?:true|false|on|off|none|null|yes|no|\d[\d._-]*|\*+.*|<.*|\{.*|"
    r"\$.*|os\.(?:getenv|environ).*|process\.env.*|your[-_].*|xxx+.*|"
    r"example.*|placeholder.*|redacted.*|\[redacted\]|«redacted.*)$")

# base64 data: URIs are exempt from every pass (an inline image in an answer
# must not be corrupted).
_SCRUB_DATA_URI_RE = re.compile(
    r"data:[A-Za-z0-9/.+-]+;base64,[A-Za-z0-9+/=_-]+")


def _scrub_secretish(val):
    """True when an assignment/bearer VALUE looks like leaked secret material.

    Conservative: pure numbers, placeholders, env lookups, and short plain
    words (no digit, < 16 chars — `tokenizer: sentencepiece`) all pass
    through untouched.
    """
    if _SCRUB_SAFE_VALUE_RE.match(val):
        return False
    if not any(c.isdigit() for c in val) and len(val) < 16:
        return False
    return True


def scrub_secrets(text):
    """(scrubbed_text, n_hits) — redact secret-shaped tokens from an answer.

    Four passes: known vendor prefixes, generic high-entropy prefixed
    tokens, secret-keyword assignments, Bearer credentials. Matches inside
    base64 data: URIs are exempt. Never raises — any internal failure
    returns the text unchanged with 0 hits.
    """
    try:
        s = str(text or "")
        if not s:
            return s, 0
        hits = [0]

        def run(pattern, repl_fn):
            spans = [m.span() for m in _SCRUB_DATA_URI_RE.finditer(s)]

            def inner(m):
                if any(m.start() >= a and m.end() <= b for a, b in spans):
                    return m.group(0)
                r = repl_fn(m)
                if r != m.group(0):
                    hits[0] += 1
                return r
            return pattern.sub(inner, s)

        s = run(_SCRUB_PREFIX_RE, lambda m: _SCRUB_REPLACEMENT)
        s = run(_SCRUB_GENERIC_RE,
                lambda m: _SCRUB_REPLACEMENT
                if sum(c.isdigit() for c in m.group(1).split("_", 1)[1]) >= 2
                else m.group(0))
        s = run(_SCRUB_ASSIGN_RE,
                lambda m: (m.group(1) + m.group(2) + _SCRUB_REPLACEMENT
                           + m.group(4))
                if _scrub_secretish(m.group(3)) else m.group(0))
        s = run(_SCRUB_BEARER_RE,
                lambda m: m.group(1) + _SCRUB_REPLACEMENT
                if _scrub_secretish(m.group(2)) else m.group(0))
        return s, hits[0]
    except Exception:
        logger.debug("router plugin: secret scrub failed; text unchanged",
                     exc_info=True)
        return text, 0


def _secret_scrub_setting():
    """tools.tool_search.secret_scrub ("on"/"off", default on). Per call."""
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        v = ((_load().get("tools") or {}).get("tool_search") or {}).get(
            "secret_scrub")
        if v is not None:
            if isinstance(v, bool):
                return "on" if v else "off"
            return str(v).strip().lower()
    except Exception:
        pass
    return SECRET_SCRUB_DEFAULT


def _secret_scrub(text):
    """Host bridge for selfheal's transform_llm_output composition:
    (text, n_hits), honoring the config flag. Never raises."""
    try:
        if _secret_scrub_setting() != "on":
            return text, 0
        return scrub_secrets(text)
    except Exception:
        return text, 0


def _nt_always():
    """tools.tool_search.no_think_always ("on"/"off", default off). Per call."""
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        v = ((_load().get("tools") or {}).get("tool_search") or {}).get(
            "no_think_always")
        if v is not None:
            if isinstance(v, bool):
                return "on" if v else "off"
            return str(v).strip().lower()
    except Exception:
        pass
    return NO_THINK_ALWAYS_DEFAULT


def _ts_flag(key, default="off"):
    """Read a tools.tool_search.<key> on/off flag per call. Never raises."""
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        v = ((_load().get("tools") or {}).get("tool_search") or {}).get(key)
        if v is not None:
            if isinstance(v, bool):
                return "on" if v else "off"
            s = str(v).strip().lower()
            return s if s in ("on", "off") else default
    except Exception:
        pass
    return default


_vr_mod = None
_vr_loaded = [False]


def _verify_route_mod():
    """Lazy guarded import of the sibling verify_route module (once). None on
    failure — the caller no-ops."""
    global _vr_mod
    if _vr_loaded[0]:
        return _vr_mod
    _vr_loaded[0] = True
    try:
        try:
            from . import verify_route as _vr
        except ImportError:
            import importlib.util
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "verify_route.py")
            spec = importlib.util.spec_from_file_location(
                "hermes_plugins_router_verify_route", path)
            _vr = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(_vr)
        _vr_mod = _vr
    except Exception:
        _vr_mod = None
    return _vr_mod


def _cd_middleware(request=None, api_mode="", base_url="", **_kw):
    """llm_request middleware for the constrained-decoding host allowlist.

    Applies up to four independent rewrites to outgoing chat.completions
    requests whose base_url host is in tools.tool_search.constrained_hosts:

    1. structural_tag response_format (v1.4.0, constrained_decoding flag) —
       constrains every emitted <tool_call> block to one of the request's own
       tool schemas (vLLM/xgrammar enforces it server-side).
    2. max_tokens clamp (v1.5.1, max_tokens_cap) — absent/oversized output
       caps are clamped so repetition loops die fast and recoverably.
    3. frequency_penalty injection (v1.5.1, frequency_penalty) — only when
       the request does not already set it.
    4. no_think_always (v1.6.1, no_think_always flag, default off) — sets
       extra_body.chat_template_kwargs.enable_thinking=false on EVERY call,
       not just forced synthesis, unless the request already pins it.

    Returns None (request unchanged) when no rewrite applies; any internal
    failure also returns None — this can never break an API call.
    """
    try:
        if api_mode != "chat_completions":
            return None
        flag, hosts = _cd_settings()
        if _cd_host(base_url) not in hosts:
            return None
        if not isinstance(request, dict):
            return None
        applied = []

        # 1) v1.4.0 constrained decoding — gates unchanged.
        if flag == "on":
            tools = request.get("tools")
            if (isinstance(tools, list) and tools
                    and request.get("response_format") is None):
                response_format = _cd_build_response_format(tools)
                if response_format is not None:
                    request["response_format"] = response_format
                    applied.append("constrained_decoding")
                    n = len(response_format["structures"])
                    if not _cd_applied_logged[0]:
                        _cd_applied_logged[0] = True
                        logger.info(
                            "router plugin: constrained tool-call decoding "
                            "applied (structural_tag, %d tool structure(s), "
                            "host=%s)", n, _cd_host(base_url))
                    else:
                        logger.debug(
                            "router plugin: constrained decoding applied "
                            "(%d structures)", n)

        # v1.9.0 task-aware coding accommodation — detect ONCE (used for both
        # the raised cap below and the coding nudge). Only when coding_mode is
        # "auto"; any error in detection leaves it False (tight bounds hold).
        coding = False
        if _coding_mode() == "auto" \
                and not _in_background_review(request.get("messages")):
            coding = is_coding_turn(request.get("messages"))
            if coding:
                logger.info(
                    "router plugin: coding turn detected -> cap %d, wall %ds",
                    _coding_max_tokens(), _coding_wall_secs())

        # 2) v1.5.1 max_tokens clamp. hermes sends max_tokens=65536 on this
        # route (the "custom" ProviderProfile default) — no effective cap for
        # a repetition loop. Clamp whichever output-cap key the request uses
        # (this route uses max_tokens; max_completion_tokens is respected if
        # ever present so we never send both).
        #
        # v1.9.0: on a detected CODING turn RAISE the cap to coding_max_tokens
        # (default 8000) so a real code file isn't truncated to an empty
        # write_file — but never LOWER it below the base cap, and still clamp
        # the 65536 default down, so a coding runaway still dies (just with
        # room to emit code). Non-coding turns keep the base 3000 exactly.
        cap = _mt_cap()
        if coding:
            try:
                ccap = _coding_max_tokens()
                if ccap > cap:
                    cap = ccap
            except Exception:
                pass
        if cap > 0:
            key = ("max_completion_tokens"
                   if "max_completion_tokens" in request else "max_tokens")
            cur = request.get(key)
            valid = isinstance(cur, (int, float)) and not isinstance(cur, bool)
            if not valid or cur > cap:
                request[key] = cap
                applied.append("max_tokens_clamp")
                logger.debug(
                    "router plugin: clamped %s %s -> %d", key,
                    cur if valid else "<absent>", cap)

        # 3) v1.5.1 anti-repetition: inject frequency_penalty only when the
        # request doesn't already set it (key absent or None). A caller's
        # explicit value — including an explicit 0 — is never overridden.
        fp = _fp_value()
        if fp != 0 and request.get("frequency_penalty") is None:
            request["frequency_penalty"] = fp
            applied.append("frequency_penalty")

        # 3b) v1.8.2 anti-tail-collapse: inject presence_penalty (flat, does
        # NOT accumulate with token count) only when the request doesn't
        # already set it. This replaces frequency_penalty as the anti-loop
        # insurance — A/B proved frequency_penalty's per-count accumulation
        # was itself causing the long-output tail collapse, while a flat
        # presence_penalty=0.3 lets long structured answers complete and stop
        # naturally. A caller's explicit value (including 0) is never
        # overridden.
        pp = _pp_value()
        if pp != 0 and request.get("presence_penalty") is None:
            request["presence_penalty"] = pp
            applied.append("presence_penalty")

        # 3c) v1.9.0 coding nudge: on a detected coding turn append one
        # cache-stable system note telling the model to WRITE the code now
        # (write_file / emit directly) instead of re-planning or re-clarifying,
        # and that it must not offer to delegate to "Claude Code"/Codex (no
        # such delegation is configured — the confusion that stalled the real
        # quake turn). Appended once; HEALTHY non-coding turns never see it.
        if coding and _inject_coding_note(request):
            applied.append("coding_note")

        # 4) v1.6.1 no_think_always: disable the reasoning channel on every
        # call to this host. Corpus batch 1 (runs 7/13/18/25): reasoning-
        # channel deliberation leaked into normal final answers WITHOUT
        # literal <think> tags (the vLLM reasoning parser strips them; the
        # model deliberates in the content channel) — a post-hoc tag strip
        # cannot catch that, only disabling the channel can. vLLM-only knob
        # (chat_template_kwargs passthrough); an explicit enable_thinking
        # already present in the request is never overridden. The selfheal
        # forced-synthesis no_think (selfheal.py) stays independent.
        if _nt_always() == "on":
            eb = request.get("extra_body")
            eb = dict(eb) if isinstance(eb, dict) else {}
            ctk = eb.get("chat_template_kwargs")
            ctk = dict(ctk) if isinstance(ctk, dict) else {}
            if "enable_thinking" not in ctk:
                ctk["enable_thinking"] = False
                eb["chat_template_kwargs"] = ctk
                request["extra_body"] = eb
                applied.append("no_think_always")

        # v1.13.0 F1: forced retrieval for attribution/recency turns (research-
        # driven — the model will NOT self-trigger; 0/4 unprompted). On the FIRST
        # call of such a turn, inject a "you MUST search" directive + tool_choice
        # =required, only when web_search is available. Fail-safe + flag-gated.
        try:
            if _ts_flag("force_verify") == "on":
                vr = _verify_route_mod()
                if vr is not None and vr.apply_force_verify(request):
                    applied.append("force_verify")
        except Exception:
            logger.debug("router plugin: force_verify skipped", exc_info=True)

        # v1.13.0 F2: calc-tool routing for exact multi-digit arithmetic (light —
        # the model self-routes to execute_code, this just nudges + forbids
        # mental multi-digit results). Fail-safe + flag-gated.
        try:
            if _ts_flag("calc_route") == "on":
                vr = _verify_route_mod()
                if vr is not None and vr.apply_calc_route(request):
                    applied.append("calc_route")
        except Exception:
            logger.debug("router plugin: calc_route skipped", exc_info=True)

        if not applied:
            return None
        return {"request": request, "source": "router",
                "name": "+".join(applied)}
    except Exception:
        _cd_warn_once(
            "middleware-failed",
            "router plugin: request middleware failed; request "
            "passed through unchanged", exc_info=True)
        return None


_cd_middleware._router_constrained = True  # dedup marker for force rescans


def _antifab_middleware(request=None, api_mode="", base_url="", session_id="",
                        turn_id="", **_kw):
    """UN-host-gated llm_request middleware (fires on deepseek AND vllm alike —
    both residuals it addresses are tier-agnostic). Applies, independently and
    each behind its own flag, two ADD-ONLY pre-turn directives:

    * ground_directive (v1.17.0): on a first-call research/attribution turn with
      a web_search tool present, append one cache-stable system note steering
      the model off self-computed roll-up totals and rumored/upcoming items
      presented as sourced (~60 tokens).

    * clarify_guard (v1.18.0): on a first-call turn with NO prior in-conversation
      context whose user message is genuinely ambiguous with no concrete
      referent ("Fix it.", "finish the thing we discussed"), append one
      cache-stable note nudging the model to ask ONE clarifying question instead
      of guessing or spelunking session history (~35 tokens). Conservative
      detector (biased to silence — a clear short prompt never fires). It NEVER
      withholds or rewrites an answer; it does not name a specific tool.

    No extra API call; each note is appended at most once (cache-stable). Any
    internal failure returns the request unchanged — this can never break a
    call. Flag-gated per directive; each defaults ON but rolls back with one key
    (tools.tool_search.ground_directive / .clarify_guard)."""
    try:
        if api_mode != "chat_completions":
            return None
        if not isinstance(request, dict):
            return None
        vr = _verify_route_mod()
        if vr is None:
            return None
        applied = []
        try:
            if _ts_flag("ground_directive", "on") == "on" \
                    and vr.apply_grounding_directive(request):
                applied.append("ground_directive")
                if not _antifab_logged[0]:
                    _antifab_logged[0] = True
                    logger.info("router plugin: grounding directive applied "
                                "(ground_directive on)")
        except Exception:
            logger.debug("router plugin: ground_directive skipped",
                         exc_info=True)
        try:
            if _ts_flag("clarify_guard", "on") == "on" \
                    and vr.apply_clarify_guard(request):
                applied.append("clarify_guard")
                # v1.19.0: record the flagged turn so clarify-finalize can end it
                # if the model invokes `clarify` (the pre_tool_call gate reads
                # this). Only recorded when the guard actually fired, so a clear
                # prompt / normal turn is never flagged.
                _note_clarify_flagged(session_id, turn_id)
                logger.info("router plugin: clarify guard applied "
                            "(clarify_guard on) — ambiguous no-referent prompt, "
                            "nudging a clarifying question")
        except Exception:
            logger.debug("router plugin: clarify_guard skipped", exc_info=True)
        if not applied:
            return None
        return {"request": request, "source": "router",
                "name": "+".join(applied)}
    except Exception:
        logger.debug("router plugin: antifab directive skipped", exc_info=True)
        return None


_antifab_middleware._router_antifab = True   # dedup marker for force rescans
_antifab_logged = [False]


def _register_monkeypatches(ctx):
    """v1.11.0: apply any hermes-core monkeypatches (guarded, fail-safe helper
    in plugin/monkeypatch.py). INERT by default — no patches are applied. This
    is the single place to add a future function-boundary override; each is a
    one-liner that SKIPS itself (one warning, stock hermes unchanged) if the
    target moved or its signature changed.

    Example (do NOT enable without a reviewed reason):
        from . import monkeypatch as mp
        mp.replace("agent.conversation_loop._get_continuation_prompt",
                   _my_continuation_prompt,
                   verify=mp.expects_params("is_partial_stub"),
                   tag="continuation-prompt")

    Rule of thumb: only patch a WHOLE function / wrapper here; for surgical mid-
    function edits use a patch-overlay so drift fails loudly, not silently.
    """
    try:
        from . import monkeypatch  # noqa: F401  (import-safe; applies nothing)
        # (no patches registered — capability is ready when a concrete,
        #  reviewed core change calls for it.)
    except Exception:
        logger.debug("router plugin: monkeypatch harness unavailable",
                     exc_info=True)


def _register_constrained_decoding(ctx):
    """Register the llm_request middleware, once per process (rescans-safe)."""
    try:
        if not hasattr(ctx, "register_middleware"):
            logger.warning(
                "router plugin: PluginContext has no register_middleware; "
                "constrained tool-call decoding NOT installed")
            return
        try:  # dedup: a force rescan re-runs register() on the same manager
            mws = ctx._manager._middleware.get("llm_request", [])
            if any(getattr(cb, "_router_constrained", False) for cb in mws):
                return
        except Exception:
            pass  # private layout changed — worst case a redundant no-op copy
        ctx.register_middleware("llm_request", _cd_middleware)
        # v1.17.0: the un-host-gated grounding-directive middleware (A). Kept
        # separate from the host-gated _cd_middleware so it fires on the strong
        # (deepseek) host too. Own dedup marker.
        ctx.register_middleware("llm_request", _antifab_middleware)
        flag, hosts = _cd_settings()
        logger.info(
            "router plugin: llm_request middleware registered "
            "(constrained_decoding=%s, max_tokens_cap=%s, "
            "frequency_penalty=%s, presence_penalty=%s, hosts=%s, "
            "ground_directive=%s, clarify_guard=%s)",
            flag, _mt_cap(), _fp_value(), _pp_value(), ",".join(hosts),
            _ts_flag("ground_directive", "on"),
            _ts_flag("clarify_guard", "on"))
    except Exception:
        logger.warning("router plugin: constrained tool-call decoding failed "
                       "to install", exc_info=True)


# ---------------------------------------------------------------------------
# v1.5.0: graceful flat-name fallback (bare calls to deferred tools execute)
# ---------------------------------------------------------------------------

FLAT_FALLBACK_DEFAULT = "on"
_ff_info_logged = [False]  # first extension logs INFO, the rest DEBUG


def _ff_setting():
    """tools.tool_search.flat_name_fallback from config ("on"/"off", default on).

    Accepts YAML-1.1 booleans (bare on/off parse as bool under safe_load),
    matching the constrained-decoding flag. Read at extension time (agent
    build / MCP refresh), so a config flip applies to new agents and to the
    next refresh of existing ones without a reinstall.
    """
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        v = ((_load().get("tools") or {}).get("tool_search") or {}).get(
            "flat_name_fallback")
        if v is not None:
            if isinstance(v, bool):
                return "on" if v else "off"
            return str(v).strip().lower()
    except Exception:
        pass
    return FLAT_FALLBACK_DEFAULT


def _ff_scoped_deferred_names(agent):
    """The session-scoped DEFERRED tool names for *agent* (frozenset).

    Same classify logic the keep-list patch defers by, and the same scope the
    tool_call unwrap enforces (tool_executor._tool_search_scoped_names): the
    deferrable subset of the agent's own enabled/disabled-toolset tool defs.
    Bare-name calls bypass the unwrap's scope gate entirely, so scoping the
    extension itself is what keeps a restricted-toolset session (subagent,
    kanban worker, curated gateway session) unable to reach out-of-scope
    tools by bare name. Prefers the shipped helper for its per-agent cache
    (keyed on registry generation + toolsets); falls back to computing the
    identical set directly. Empty frozenset on any failure = no extension.
    """
    try:
        from agent.tool_executor import _tool_search_scoped_names
        return frozenset(_tool_search_scoped_names(agent) or ())
    except Exception:
        pass
    try:
        import model_tools
        from tools import tool_search as ts
        defs = model_tools.get_tool_definitions(
            enabled_toolsets=getattr(agent, "enabled_toolsets", None),
            disabled_toolsets=getattr(agent, "disabled_toolsets", None),
            quiet_mode=True, skip_tool_search_assembly=True) or []
        return frozenset(ts.scoped_deferrable_names(defs) or ())
    except Exception:
        logger.warning("router plugin: flat-name fallback could not compute "
                       "the deferred-name set; valid_tool_names left as-is",
                       exc_info=True)
        return frozenset()


def _ff_extend_valid_names(agent, origin):
    """Union agent.valid_tool_names with the scoped deferred names.

    Runs right after each publish point (init_agent / refresh_agent_mcp_tools)
    wrote a flat-only set. Publishes a NEW set object (atomic attribute swap,
    same publication style the SP code uses) instead of mutating in place, so
    a concurrent reader never sees a half-extended set. No-op when the flag
    is off, the agent has no tools, or nothing is missing (e.g. tool_search
    assembly inactive -> deferred names are already flat). Never raises.
    """
    try:
        if _ff_setting() != "on":
            return
        vtn = getattr(agent, "valid_tool_names", None)
        if not isinstance(vtn, set) or not vtn:
            return  # no tools loaded — nothing to rescue
        missing = _ff_scoped_deferred_names(agent) - vtn
        if not missing:
            return
        agent.valid_tool_names = vtn | missing
        if not _ff_info_logged[0]:
            _ff_info_logged[0] = True
            logger.info(
                "router plugin: flat-name fallback active (%s): +%d deferred "
                "names accepted by the tool-call gate (schemas stay deferred)",
                origin, len(missing))
        else:
            logger.debug(
                "router plugin: flat-name fallback re-extended "
                "valid_tool_names (%s, +%d)", origin, len(missing))
    except Exception:
        logger.warning("router plugin: flat-name fallback extension failed; "
                       "agent keeps flat-only valid_tool_names", exc_info=True)


def _register_flat_name_fallback():
    """Wrap the two valid_tool_names publish points (agent build, MCP refresh).

    Every call site resolves both functions with a call-time function-local
    import (run_agent.py:490 for init_agent; cli.py:10701, gateway/run.py:13803,
    tui_gateway/server.py:4240, agent/turn_context.py:198 for
    refresh_agent_mcp_tools), so patching the module attributes covers them
    all. Each wrap is independently idempotent for force rescans.
    """
    try:
        try:
            from agent import agent_init as _ai
            from tools import mcp_tool as _mt
            from tools import tool_search as _ts
        except Exception:
            logger.warning(
                "router plugin: flat-name fallback NOT installed "
                "(agent_init/mcp_tool/tool_search import failed)", exc_info=True)
            return
        # Symbol guard: this feature needs both publish points and the scoped
        # classify helper; any missing symbol skips just this feature.
        for _mod, _sym in ((_ai, "init_agent"),
                           (_mt, "refresh_agent_mcp_tools"),
                           (_ts, "scoped_deferrable_names")):
            if not callable(getattr(_mod, _sym, None)):
                logger.warning(
                    "router plugin: flat-name fallback NOT installed "
                    "(missing %s.%s)", _mod.__name__, _sym)
                return

        import functools

        if not getattr(_ai.init_agent, "_router_flat_fallback", False):
            _orig_init = _ai.init_agent

            @functools.wraps(_orig_init)
            def init_agent_with_fallback(agent, *args, **kwargs):
                res = _orig_init(agent, *args, **kwargs)
                _ff_extend_valid_names(agent, "agent init")
                return res

            init_agent_with_fallback._router_flat_fallback = True
            _ai.init_agent = init_agent_with_fallback

        if not getattr(_mt.refresh_agent_mcp_tools, "_router_flat_fallback", False):
            _orig_refresh = _mt.refresh_agent_mcp_tools

            @functools.wraps(_orig_refresh)
            def refresh_with_fallback(agent, *args, **kwargs):
                res = _orig_refresh(agent, *args, **kwargs)
                _ff_extend_valid_names(agent, "mcp refresh")
                return res

            refresh_with_fallback._router_flat_fallback = True
            _mt.refresh_agent_mcp_tools = refresh_with_fallback

        logger.info(
            "router plugin: flat-name fallback registered "
            "(tools.tool_search.flat_name_fallback=%s)", _ff_setting())
    except Exception:
        logger.warning("router plugin: flat-name fallback failed to install",
                       exc_info=True)


# ---------------------------------------------------------------------------
# v1.6.0: session self-healing layer (selfheal.py)
# ---------------------------------------------------------------------------

def _register_selfheal(ctx):
    """Register the selfheal module (sibling file), independently guarded.

    Must run AFTER _register_constrained_decoding so the healer's llm_request
    middleware sits after _cd_middleware in the chain (the forced-synthesis
    strip then removes anything constrained decoding added). Every failure
    logs a warning and leaves the rest of the plugin untouched.
    """
    try:
        try:
            from . import selfheal
        except ImportError:
            # Loaded outside the normal package context — path-based import
            # of the sibling file (same fallback as web_local).
            import importlib.util
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "selfheal.py")
            spec = importlib.util.spec_from_file_location(
                "hermes_plugins_router_selfheal", path)
            selfheal = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(selfheal)
        selfheal.register(ctx, host={
            "counters": router_turn_counters,
            "search_hard_cap": _search_hard_cap,
            "unwrap": _dup_unwrap,
            "exempt": _dup_call_exempt,
            # constrained_hosts allowlist — gates the vLLM-only
            # chat_template_kwargs think-disable during forced synthesis.
            "hosts": lambda: _cd_settings()[1],
            # v1.6.1 outgoing-answer secret scrub — composed into the
            # selfheal transform_llm_output callback (hooks don't chain:
            # the first non-empty string wins, turn_finalizer.py:353-357).
            "scrub": _secret_scrub,
            # v1.9.0 task-aware coding accommodation: the healer raises its S7
            # wall to coding_wall_secs ONLY on a detected coding turn (mode
            # "auto"), so a legit code-writing turn isn't forced-synthesised at
            # 150s before the file is written. Non-coding turns keep 150.
            "is_coding_turn": is_coding_turn,
            "coding_mode": _coding_mode,
            "coding_wall_secs": _coding_wall_secs,
            # v1.12.0 multi-topic reply badge — composed into the selfheal
            # finisher (transform hooks don't chain; selfheal owns the sole
            # one). Returns "" when topics is disabled/inert, so it's a no-op.
            "badge": _topics_badge,
        })
    except Exception:
        logger.warning("router plugin: selfheal failed to install; healer "
                       "inactive (normal operation unaffected)", exc_info=True)


# ---------------------------------------------------------------------------
# v1.7.0: intermediate progress messages (progress.py)
# ---------------------------------------------------------------------------

def _register_progress(ctx):
    """Register the progress module (sibling file), independently guarded.

    Must run AFTER _register_selfheal so its READ-ONLY llm_request middleware
    sits LAST in the chain — it observes the request after any forced-synthesis
    mutation, never before, and never perturbs the healer. Every failure logs
    a warning and leaves the rest of the plugin untouched.
    """
    try:
        try:
            from . import progress
        except ImportError:
            # Loaded outside the normal package context — path-based import
            # of the sibling file (same fallback as web_local/selfheal).
            import importlib.util
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "progress.py")
            spec = importlib.util.spec_from_file_location(
                "hermes_plugins_router_progress", path)
            progress = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(progress)
        progress.register(ctx)
    except Exception:
        logger.warning("router plugin: progress failed to install; interim "
                       "messages inactive (normal operation unaffected)",
                       exc_info=True)


# ---------------------------------------------------------------------------
# v1.8.0: latency-inferred server-load awareness (loadaware.py)
# ---------------------------------------------------------------------------

def _register_loadaware(ctx):
    """Register the loadaware module (sibling file), independently guarded.

    Must run AFTER _register_progress so loadaware can reuse progress's
    captured-adapter path for its honest load notes. Every failure logs a
    warning and leaves the rest of the plugin untouched.
    """
    try:
        try:
            from . import loadaware
        except ImportError:
            # Loaded outside the normal package context — path-based import
            # of the sibling file (same fallback as web_local/selfheal/progress).
            import importlib.util
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "loadaware.py")
            spec = importlib.util.spec_from_file_location(
                "hermes_plugins_router_loadaware", path)
            loadaware = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loadaware)
        loadaware.register(ctx)
    except Exception:
        logger.warning("router plugin: loadaware failed to install; "
                       "server-load awareness inactive (normal operation "
                       "unaffected)", exc_info=True)


# v1.12.0: multi-topic conversation system (topics.py). Registered BEFORE
# selfheal so topics.badge_for is a bound reference when selfheal's host dict is
# built. Kept in a module global so the badge bridge can reach it.
_topics_mod = None


def _register_topics(ctx):
    """Register the topics module (sibling file), independently guarded.

    Must run BEFORE _register_selfheal so ``topics.badge_for`` is available to
    fold into the healer's host dict (the reply badge composes into selfheal's
    sole transform_llm_output finisher, since transform hooks don't chain).
    topics adds NO llm_request middleware, so the healer chain is unaffected.
    Every failure logs a warning and leaves the rest of the plugin untouched.
    """
    global _topics_mod
    try:
        try:
            from . import topics
        except ImportError:
            # Loaded outside the normal package context — path-based import
            # of the sibling file (same fallback as web_local/selfheal/progress).
            import importlib.util
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "topics.py")
            spec = importlib.util.spec_from_file_location(
                "hermes_plugins_router_topics", path)
            topics = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(topics)
        topics.register(ctx)
        _topics_mod = topics
    except Exception:
        logger.warning("router plugin: topics failed to install; multi-topic "
                       "conversation inactive (normal operation unaffected)",
                       exc_info=True)


def _topics_badge(session_id=""):
    """Host bridge: the active-topic reply badge, composed into selfheal's
    finisher. Returns "" when topics is uninstalled/disabled/inert. Never
    raises."""
    try:
        return _topics_mod.badge_for(session_id) if _topics_mod else ""
    except Exception:
        return ""


def register(ctx):
    # Independent features, each with its own symbol guards: a failure in one
    # never blocks the others.
    _register_local_extract_provider(ctx)
    _register_search_steering(ctx)
    _register_dup_query_gate(ctx)
    _register_constrained_decoding(ctx)
    _register_flat_name_fallback()
    _patch_tool_search()
    # v1.12.0: multi-topic system registers BEFORE selfheal so its badge_for is
    # bound when the healer's host dict (which composes the reply badge) is built.
    _register_topics(ctx)
    # v1.6.0: selfheal registers LAST-but-one so its llm_request middleware
    # runs after _cd_middleware.
    _register_selfheal(ctx)
    # v1.7.0: progress registers LAST so its READ-ONLY llm_request middleware
    # observes the request after any selfheal forced-synthesis mutation.
    _register_progress(ctx)
    # v1.8.0: latency-inferred server-load awareness. Read-only middleware +
    # two post-call observer hooks; reuses progress's captured adapter for its
    # honest notes (registered after progress so that path exists).
    _register_loadaware(ctx)
    # v1.11.0: guarded monkeypatch harness — inert (no patches applied), ready
    # for a future function-boundary core override. Fail-safe; never blocks the
    # others.
    _register_monkeypatches(ctx)


def _patch_tool_search():
    from tools import tool_search as ts

    if getattr(ts, "_router_plugin_patched", False):  # idempotent (force rescans)
        return

    # Symbol guard: bail loudly (fallback = flat tools) if upstream internals moved.
    for sym in ("_core_tool_names", "assemble_tool_defs", "classify_tools",
                "BRIDGE_TOOL_NAMES", "TOOL_SEARCH_NAME", "bridge_tool_schemas",
                "resolve_underlying_call", "dispatch_tool_describe",
                "TOOL_CALL_NAME", "TOOL_DESCRIBE_NAME"):
        if not hasattr(ts, sym):
            logger.warning(
                "router plugin: tool_search API changed (missing %r); NOT patching — "
                "falling back to flat tool schemas", sym)
            return

    def _keep():
        keep = set(DEFAULT_KEEP)
        try:
            from hermes_cli.config import load_config
            kf = ((load_config().get("tools") or {}).get("tool_search") or {}).get("keep_flat")
            if isinstance(kf, list) and kf:
                keep = set(map(str, kf))
        except Exception:
            pass
        if os.environ.get("HERMES_KANBAN_TASK"):
            keep |= KANBAN_LIFECYCLE
        return frozenset(keep)

    # Single patch point: every bridge-side deferrability check (assembly,
    # catalog, describe, call resolution, both scope gates) resolves
    # _core_tool_names via module-global lookup at call time.
    ts._core_tool_names = _keep

    # Append a names index of deferred tools to the tool_search description so
    # the model can skip search and go straight to tool_describe/tool_call.
    _orig = ts.assemble_tool_defs

    def assemble_with_index(tool_defs, **kw):
        res = _orig(tool_defs, **kw)
        # v1.8.6: the names-index append is the one monkeypatch that runs inside
        # hermes's core tool-assembly path, so it must honor the same fail-safe
        # covenant as the registered hooks — a raise here would break assembly.
        # On any failure, degrade to the un-indexed (but fully functional) res.
        try:
            if res.activated:
                _, deferrable = ts.classify_tools(
                    [td for td in tool_defs
                     if (td.get("function") or {}).get("name") not in ts.BRIDGE_TOOL_NAMES])
                names = sorted((td.get("function") or {}).get("name", "") for td in deferrable)
                for td in res.tool_defs:
                    fn = td.get("function") or {}
                    if fn.get("name") == ts.TOOL_SEARCH_NAME:
                        # Safe to mutate: bridge_tool_schemas builds fresh dicts per
                        # assembly, unlike registry-shared schemas. (With overrides
                        # active, the index lands on top of the overridden text —
                        # bridge_tool_schemas is patched below and resolved via
                        # module-global lookup inside assemble_tool_defs.)
                        fn["description"] += (" Deferred tools available via this router: "
                                              + ", ".join(names) + ".")
                        break
        except Exception:
            logger.debug("router plugin: names-index append skipped (non-fatal)")
        return res

    ts.assemble_tool_defs = assemble_with_index

    # ------------------------------------------------------------------
    # tool_call argument repair (incident 2026-07-18). model_tools resolves
    # _ts_mod.resolve_underlying_call at CALL time (model_tools.py:1105), so
    # patching the module attribute covers dispatch, display, and trajectory.
    # ------------------------------------------------------------------
    _orig_resolve = ts.resolve_underlying_call

    _SHAPE_HINT = (
        ' Expected shape: {"name": "terminal", "arguments": {"command": "echo hi"}}'
        ' — the top-level "name" is the tool to invoke and the tool\'s own'
        ' parameters go INSIDE the "arguments" object (never nest another'
        ' "name"/"arguments" wrapper inside it). Available deferred tool names'
        ' are listed in the tool_search tool description. Deferred tools must'
        ' be invoked via tool_call — never by their bare name.'
    )

    def _short(obj, limit=400):
        try:
            s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            s = repr(obj)
        return s if len(s) <= limit else s[:limit] + "...(truncated)"

    def _declared_params(tool_name):
        """The tool's declared parameter-name set, or None when unknowable.

        None means 'do not touch anything' — every repair that depends on
        schema knowledge is skipped when the registry can't answer.
        """
        try:
            from tools.registry import registry
            schema = registry.get_schema(tool_name)
            props = ((schema or {}).get("parameters") or {}).get("properties")
            if isinstance(props, dict):
                return set(props)
        except Exception:
            pass
        return None

    def _unwrap_payload(tool_name, payload):
        """Strip mechanical wrappers from a tool's argument payload.

        Alternates two steps to a fixpoint (models emit alternating layers,
        e.g. name echoed inside a wrapper inside another wrapper): unwrap a
        spurious single-key {"arguments": {...}} level, and drop a payload
        key "name" that merely echoes the tool's own name — each ONLY when
        the tool's schema provably does not declare a parameter of that name
        (so skill_view's legitimate "name" param, or any tool with an
        "arguments" param, is never touched). Returns (payload, changed).
        """
        changed = False
        for _ in range(8):
            if (isinstance(payload, dict) and set(payload) == {"arguments"}
                    and isinstance(payload["arguments"], dict)):
                declared = _declared_params(tool_name)
                if declared is not None and "arguments" not in declared:
                    payload = payload["arguments"]
                    changed = True
                    continue
            if isinstance(payload, dict) and payload.get("name") == tool_name:
                declared = _declared_params(tool_name)
                if declared is not None and "name" not in declared:
                    payload = {k: v for k, v in payload.items() if k != "name"}
                    changed = True
                    continue
            break
        return payload, changed

    def _extract_call(raw):
        """Mechanically extract (tool_name, payload) from malformed tool_call args.

        Descends through up to 3 levels of "arguments" nesting looking for a
        usable tool name; bridge names (a spurious name == "tool_call"
        wrapper) are skipped, not accepted. Returns None when no candidate
        exists — the caller then emits the enriched error.
        """
        cur = raw
        for _ in range(4):  # level 0 + up to 3 descents
            if not isinstance(cur, dict):
                return None
            nm = cur.get("name")
            if isinstance(nm, str) and nm.strip() and nm.strip() not in ts.BRIDGE_TOOL_NAMES:
                nm = nm.strip()
                rest = {k: v for k, v in cur.items() if k != "name"}
                if set(rest) == {"arguments"} and isinstance(rest["arguments"], (dict, str)):
                    payload = rest["arguments"]  # canonical {"name": X, "arguments": {...}}
                else:
                    payload = rest  # name inlined alongside the tool's own params
                return nm, payload
            nxt = cur.get("arguments")
            if isinstance(nxt, str):
                try:
                    nxt = json.loads(nxt)
                except Exception:
                    return None
            if not isinstance(nxt, dict):
                return None
            cur = nxt
        return None

    def resolve_with_repair(args):
        name, rargs, err = _orig_resolve(args)
        if err is None and name:
            # Original resolved: authoritative. The only post-success repair
            # is the schema-guarded double-wrap unwrap (family ii); anything
            # the schema can't rule out is returned untouched.
            repaired, changed = _unwrap_payload(name, rargs)
            if changed:
                n2, a2, e2 = _orig_resolve({"name": name, "arguments": repaired})
                if e2 is None and n2:
                    logger.info(
                        "router plugin: repaired double-wrapped tool_call args "
                        "for %r: %s -> %s", name, _short(rargs), _short(a2))
                    return n2, a2, None
            return name, rargs, None
        # Original failed: attempt mechanical repair (families i-iii), then
        # re-run the ORIGINAL resolver so all its validation still applies.
        try:
            extracted = _extract_call(args if isinstance(args, dict) else None)
            if extracted:
                nm, payload = extracted
                if isinstance(payload, dict):
                    payload, _ = _unwrap_payload(nm, payload)
                n2, a2, e2 = _orig_resolve({"name": nm, "arguments": payload})
                if e2 is None and n2:
                    logger.info(
                        "router plugin: repaired malformed tool_call %s -> "
                        "name=%r arguments=%s", _short(args), n2, _short(a2))
                    return n2, a2, None
        except Exception:
            logger.warning("router plugin: tool_call repair itself failed; "
                           "returning original error", exc_info=True)
        base = err or "tool_call could not be resolved"
        return None, {}, base + _SHAPE_HINT

    ts.resolve_underlying_call = resolve_with_repair

    # ------------------------------------------------------------------
    # tool_describe for bridge tools: serve the bridge tool's own schema and
    # a usage example instead of the upstream refusal (the refusal denied the
    # model the one document that teaches the tool_call shape).
    # ------------------------------------------------------------------
    _orig_describe = ts.dispatch_tool_describe

    _BRIDGE_EXAMPLES = {
        ts.TOOL_SEARCH_NAME: 'tool_search {"query": "run shell command"}',
        ts.TOOL_DESCRIBE_NAME: 'tool_describe {"name": "terminal"}',
        ts.TOOL_CALL_NAME: ('tool_call {"name": "terminal", "arguments": '
                            '{"command": "echo hi"}} — the tool\'s own '
                            'parameters go inside "arguments". Deferred tools '
                            'must be invoked via tool_call — never by their '
                            'bare name.'),
    }

    def describe_with_bridge_schemas(args, **kw):
        try:
            name = str((args or {}).get("name") or "").strip()
        except Exception:
            name = ""
        if name in ts.BRIDGE_TOOL_NAMES:
            try:
                _, deferrable = ts.classify_tools(kw.get("current_tool_defs") or [])
                # ts.bridge_tool_schemas resolved at call time -> picks up the
                # overrides wrapper below, so overridden bridge text is served.
                for td in ts.bridge_tool_schemas(len(deferrable)):
                    fn = td.get("function") or {}
                    if fn.get("name") == name:
                        out = {
                            "name": name,
                            "description": fn.get("description", ""),
                            "parameters": fn.get("parameters", {}),
                        }
                        example = _BRIDGE_EXAMPLES.get(name)
                        if example:
                            out["example"] = example
                        return json.dumps(out, ensure_ascii=False)
            except Exception:
                logger.warning("router plugin: bridge tool_describe(%r) failed; "
                               "delegating to original", name, exc_info=True)
        return _orig_describe(args, **kw)

    ts.dispatch_tool_describe = describe_with_bridge_schemas

    # ------------------------------------------------------------------
    # Description overrides (overrides.yaml). Read once per process.
    # ------------------------------------------------------------------
    overrides = _load_overrides()
    if overrides:
        _warned = set()  # (tool, param) pairs already reported — avoid per-call spam

        def _apply_overrides(tool_defs):
            out = []
            for td in tool_defs:
                fn = td.get("function") if isinstance(td, dict) else None
                name = (fn or {}).get("name", "")
                ov = overrides.get(name)
                if not ov:
                    out.append(td)
                    continue
                # Deep-copy before editing: registry.get_definitions returns
                # {"type": "function", "function": {**entry.schema, ...}} — the
                # nested 'parameters' dict IS the registry entry's. In-place
                # edits would leak into registry state (and anything that
                # serializes it). Only overridden tools pay the copy.
                td = copy.deepcopy(td)
                fn = td.get("function") or {}
                if ov["description"] is not None:
                    fn["description"] = ov["description"]
                props = ((fn.get("parameters") or {}).get("properties") or {})
                for pname, pdesc in ov["params"].items():
                    target = props.get(pname)
                    if isinstance(target, dict):
                        target["description"] = pdesc
                    elif (name, pname) not in _warned:
                        _warned.add((name, pname))
                        logger.warning(
                            "router plugin: override %s.params.%s: tool has no "
                            "such parameter; skipped", name, pname)
                out.append(td)
            return out

        # Choke point for (a) flat agent.tools, (b) tool_describe, (c) the
        # tool_search catalog/BM25 index and result snippets: all three read
        # schemas produced by registry.get_definitions (sole hermes call site:
        # model_tools.py:445). Patching the singleton's bound attribute is
        # read-time, so it survives MCP nuke-and-repave refreshes and composes
        # with dynamic_schema_overrides (which run inside get_definitions —
        # a static override here intentionally wins over dynamic text).
        try:
            from tools.registry import registry as _registry
        except Exception:
            _registry = None
        _orig_get_defs = getattr(_registry, "get_definitions", None)
        if callable(_orig_get_defs):
            def get_definitions_with_overrides(*a, **kw):
                return _apply_overrides(_orig_get_defs(*a, **kw))
            _registry.get_definitions = get_definitions_with_overrides
        else:
            logger.warning("router plugin: tools.registry.get_definitions missing; "
                           "tool description overrides NOT applied")

        # The three bridge tools are synthesized here, not registered — cover
        # them too. assemble_tool_defs resolves this name at call time, so the
        # names-index append above still lands after the override.
        _orig_bridge = ts.bridge_tool_schemas

        def bridge_schemas_with_overrides(*a, **kw):
            return _apply_overrides(_orig_bridge(*a, **kw))

        ts.bridge_tool_schemas = bridge_schemas_with_overrides
        logger.info("router plugin: description overrides active for %d tool(s): %s",
                    len(overrides), ", ".join(sorted(overrides)))

    ts._router_plugin_patched = True
    # Defensive cache clear for force rescans. Normal load happens while
    # model_tools is still mid-import (discover_plugins() runs at its module
    # top level, before _tool_defs_cache is defined), so importing it here
    # would see a partially-initialized module — look it up via sys.modules
    # and skip when the attribute doesn't exist yet (cache is empty then).
    import sys
    mt = sys.modules.get("model_tools")
    cache = getattr(mt, "_tool_defs_cache", None)
    if cache is not None:
        cache.clear()
    logger.info("router plugin: tool_search keep-list patch applied")
