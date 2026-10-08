# Router-Tool Design — hermes-agent v0.18.2, local-27b @ vllm-local

Goal: fixed per-call payload (system prompt + tool schemas) from ~55 KB / ~13.8k tokens
down to ≤6-7k tokens on root/telegram, without losing reach to any tool.

**Chosen route: B — a ~55-line out-of-tree plugin (`~/.hermes/plugins/router/`) that
narrows the shipped Tool Search bridge's never-defer set to a configurable keep-list,
plus two config keys that already exist or are inert extras.** Zero site-packages edits.
Everything else (catalog, BM25 search, describe, scoped dispatch, approval/hook parity,
display unwrap) is the tested in-tree bridge.

**Measured result (offline, real inspection agent):**

| | system prompt | tool schemas | total | ~tokens |
|---|---|---|---|---|
| telegram baseline | 15,177 B | 40,030 B (17 tools) | 55,207 B | ~13.8k |
| telegram router | 14,602 B | 10,097 B (9 tools) | **24,699 B** | **~6.2k** |
| cli router | 17,109 B | 10,260 B (9 tools) | 27,369 B | ~6.8k |

All paths below relative to SP = `/home/user/.hermes/venv/lib/python3.11/site-packages/`.

---

## 1. Verified answers to the open questions

### (a) Can handle_function_call dispatch a core tool whose toolset is not in enabled_toolsets?

**YES — verified empirically.** `handle_function_call` (SP/model_tools.py:1019) performs
NO enabled_toolsets check on direct dispatch; the `enabled_toolsets`/`disabled_toolsets`
kwargs are used ONLY to scope the bridge catalog (:1091-1095). `registry.dispatch`
(SP/tools/registry.py:574) resolves the handler purely by name. `check_fn` filters
*schema visibility* in `registry.get_definitions` (:521-548), never dispatch.
Experiment: `handle_function_call("read_file", {...}, enabled_toolsets=["clarify"])`
executed and returned file content.

What actually blocks, layer by layer:
1. **`agent.valid_tool_names`** (set at build, SP/agent/agent_init.py:1166-1168; enforced
   SP/agent/conversation_loop.py:4431-4500) — the model cannot name-call a tool absent
   from the schemas array; it gets a repairable error + 3 retries.
2. **The bridge's own deferrability predicate** — `resolve_underlying_call`
   (SP/tools/tool_search.py:705) rejects any name failing `is_deferrable_tool_name`
   (:163-186), which excludes `_core_tool_names()` (:150-160 → `toolsets._HERMES_CORE_TOOLS`,
   SP/toolsets.py:31-80). **This predicate, not dispatch, is the only thing keeping core
   tools out of the router.**
3. **Session scope gates** — `scoped_deferrable_names` check in
   SP/model_tools.py:1115-1122 (execute_code-sandbox path) and
   `_tool_search_scoped_names` in SP/agent/tool_executor.py:198-244, enforced at both
   unwrap sites (:375, :1010). Verified: a session with `enabled_toolsets=["clarify","todo"]`
   is blocked from `tool_call(read_file)` with a clean error.

Crucially, **every one of these bridge-side checks resolves `is_deferrable_tool_name` /
`_core_tool_names` through module-global lookup at call time** (classify_tools:205,
dispatch_tool_describe:639, resolve_underlying_call:705, scoped_deferrable_names:676).
One monkeypatch of `tool_search._core_tool_names` changes assembly, catalog, describe,
call-resolution, and both scope gates consistently. Verified end-to-end:
`tool_call(terminal, echo router-ok)` and `tool_call(read_file, ...)` executed real
tools through `model_tools.handle_function_call` with the patch applied.

### (b) Does the tool_call unwrap display properly in gateway mode?

**Yes.** The unwrap lives in the agent loop itself, not the CLI: SP/agent/tool_executor.py
concurrent path :353-386 and sequential path :1001-1019 rewrite
`function_name/function_args` to the underlying tool BEFORE anything downstream —
guardrails (:447/:1053), plugin pre_tool_call hooks, checkpointing, and the display
layer: `tool_progress_callback("tool.started", function_name, ...)` (:1093-1099) and
`tool_start_callback` (:1101-1106) both fire with the **underlying** name, which is what
the telegram gateway renders in its activity feed and approval buttons. The transcript
keeps the model's original `tool_call` entry and tool_call_id (comment at :360-362), so
message pairing on the wire is intact. Tool results flow back unmodified (verified:
real terminal/read_file output returned through the bridge).

### (c) Per-toolset system-prompt guidance & skills index per route

Guidance is keyed on `agent.valid_tool_names` **at build time**
(SP/agent/system_prompt.py:189-207 for memory/session_search/skills/kanban; :260-290 for
the `<available_skills>` index; prompt built once per session, :113-130).

- **Route A/B (deferral at assembly):** deferred tools are absent from
  `valid_tool_names`, so their guidance auto-drops — *consistent, never stale*. With the
  chosen keep-list: MEMORY_GUIDANCE kept (memory flat), skills index kept (1,051 B —
  `skills_list` flat satisfies the any-of-three check at :260), SESSION_SEARCH_GUIDANCE
  and SKILLS_GUIDANCE (keyed on `skill_manage`, :193) drop. Net −575 B. The dropped
  blocks are usage-nudges, not schemas; describe returns full schemas on demand.
  Kanban worker edge: KANBAN_GUIDANCE is keyed on `kanban_show` (agent_init.py:1186-1189)
  — the plugin env-widens the keep-list when `HERMES_KANBAN_TASK` is set; verified in a
  fresh process that workers keep all 8 kanban tools flat AND the guidance block.
- **Route C (own router tools + narrowed platform_toolsets):** same guidance drops, but
  the deferred universe is *outside* the session's toolset scope, so the existing scope
  gates ((a).3) would correctly block dispatch — route C must bypass or re-implement the
  security gates. Rejected.
- **Route D (live tool-list swap):** the system prompt is cached per session, so tools
  loaded mid-session get NO guidance and a stale skills index; each swap also invalidates
  the serialized prompt prefix. Rejected.

### (d) Memoization / generation stamping interactions

- `_tool_defs_cache` (SP/model_tools.py:261-353) key =
  `(enabled, disabled, registry._generation, config-mtime+size, kanban-env, skip_tool_search_assembly)`.
  The patch is NOT part of the key — safe anyway, because `discover_plugins()` runs at
  `model_tools` import time (:204-208), before any cache fill, and the patch is stable
  for process lifetime. Keep-list edits go through config.yaml → mtime bump → cache
  invalidation for free. The plugin registers no tools → no generation churn.
- Bridge handlers read the catalog via `get_tool_definitions(skip_tool_search_assembly=True)`
  — separately cached (skip flag in key). No interaction.
- `refresh_agent_mcp_tools` (SP/tools/mcp_tool.py:5231) re-runs `get_tool_definitions`
  with the agent's own toolsets → assembly (and the patch) re-applies; atomic publish and
  generation stamping unaffected. Runs only between turns (SP/agent/turn_context.py:186-201).
- One real pitfall found (test artifact, not production): the check_fn TTL cache
  (SP/tools/registry.py:139-198, 30 s) masks env flips within a live process for ≤30 s.
  Kanban workers are fresh subprocesses, so unaffected.

### (e) Prompt-cache implications on the vllm backend

- `cache_control` markers are applied **only** when `agent._use_prompt_caching`
  (SP/agent/conversation_loop.py:883-894), set by `_anthropic_prompt_cache_policy` —
  Anthropic/Claude-compatible paths only. The vllm-local custom OpenAI-compatible
  provider sends **no cache_control**; caching there is vLLM's server-side automatic
  prefix caching (APC), transparent to the client.
- Routes A/B keep the tools array **byte-stable for the whole session** (snapshot at
  agent_init:1159, re-read but never recomputed per call at
  SP/agent/chat_completion_helpers.py:666-668). tool_search/describe/call results are
  ordinary appended tool messages — the prefix (system + tools rendered by the chat
  template) is untouched. Route D would invalidate the whole APC prefix on every swap.
- Late MCP connect changes `deferred_count`/names in the bridge description → one
  turn-boundary prefix invalidation, identical to today's flat behavior.
- Bonus: cutting the fixed payload ~7.6k tokens directly cuts per-request KV/prefill on
  the 27B, which is the point of the exercise.

---

## 2. Architecture & exact change plan

### New files (out-of-tree only)

**`~/.hermes/plugins/router/plugin.yaml`**
```yaml
name: router
version: 1.0.0
description: "Defers bulky core tool schemas behind the built-in tool_search router. Keep-list: tools.tool_search.keep_flat."
kind: standalone
```

**`~/.hermes/plugins/router/__init__.py`** (~55 lines):
```python
DEFAULT_KEEP = {"clarify", "todo", "memory", "vision_analyze",
                "text_to_speech", "skills_list"}
KANBAN_LIFECYCLE = {"kanban_show", "kanban_list", "kanban_complete", "kanban_block",
                    "kanban_heartbeat", "kanban_comment", "kanban_create",
                    "kanban_link", "kanban_unblock"}

def register(ctx):
    import os
    from tools import tool_search as ts
    import model_tools
    if getattr(ts, "_router_plugin_patched", False):      # idempotent (force rescans)
        return
    # sanity guard: bail loudly (fallback = flat tools) if internals moved
    for sym in ("_core_tool_names", "assemble_tool_defs", "classify_tools",
                "BRIDGE_TOOL_NAMES", "TOOL_SEARCH_NAME"):
        if not hasattr(ts, sym):
            ctx.log.warning("router: tool_search API changed; not patching"); return

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
            keep |= KANBAN_LIFECYCLE            # workers keep lifecycle tools flat
        return frozenset(keep)

    ts._core_tool_names = _keep                 # single patch point — see §1(a)

    _orig = ts.assemble_tool_defs               # names index → tool_search description
    def assemble_with_index(tool_defs, **kw):
        res = _orig(tool_defs, **kw)
        if res.activated:
            _, deferrable = ts.classify_tools(
                [td for td in tool_defs
                 if (td.get("function") or {}).get("name") not in ts.BRIDGE_TOOL_NAMES])
            names = sorted((td.get("function") or {}).get("name", "") for td in deferrable)
            for td in res.tool_defs:
                fn = td.get("function") or {}
                if fn.get("name") == ts.TOOL_SEARCH_NAME:
                    fn["description"] += (" Deferred tools available via this router: "
                                          + ", ".join(names) + ".")
                    break
        return res
    ts.assemble_tool_defs = assemble_with_index
    ts._router_plugin_patched = True
    model_tools._tool_defs_cache.clear()        # defensive; normally empty at load
```
Safe to mutate the bridge description: `bridge_tool_schemas` builds fresh dicts per
assembly (tool_search.py:426-510), unlike registry-shared schemas.

### Config changes (per profile `config.yaml`)
```yaml
tools:
  tool_search:
    enabled: on                        # existing key — bypasses the 10%-of-224k auto
                                       # threshold (deferred ~8.7k tok < 22.5k tok gate)
    keep_flat: [clarify, todo, memory, vision_analyze, text_to_speech, skills_list]
                                       # read only by the plugin; core ToolSearchConfig
                                       # .from_raw ignores unknown keys (verified :92-114)
plugins:
  enabled: [router]                    # user plugins are opt-in (plugins.py:241-267)
```
No `platform_toolsets` changes — every toolset stays granted; only schema *presentation*
changes. `hermes update` reinstalls site-packages but never touches `~/.hermes` → the
whole mechanism survives updates.

### What the model sees (telegram)
Flat (9 schemas, 10,097 B): clarify 1938, memory 2831, todo 1372, vision_analyze 924,
text_to_speech 949, skills_list 305, tool_search ~880 (incl names index),
tool_describe 373, tool_call 522.
Deferred (11, reachable via router): terminal, process, read_file, write_file, patch,
search_files, session_search, delegate_task, execute_code, skill_view, skill_manage.
(cronjob/image_gen/kanban contribute 0 tools on this box — unmet deps at build.)

### Rejected routes
- **A (in-tree patch):** identical semantics, but clobbered by `hermes update`; needs a
  re-apply script + version pin. Keep as fallback if upstream refactors tool_search.
- **C (plugin-owned router tools):** duplicates the bridge, and §1(a) shows it must
  either bypass or re-implement the session scope gates — a security regression risk.
- **D (set_agent_toolsets swap):** new machinery, stale system-prompt guidance for
  late-loaded tools, APC prefix invalidation per swap.

---

## 3. Failure-mode analysis (27B model)

**Discovery — three redundant cues:**
1. The names index appended to `tool_search`'s description (exact tool names; the model
   can skip search and go straight to `tool_describe`). Measured cost ~200-380 B.
2. The stock bridge descriptions already encode the flow
   (search → describe → call, "Policy, hooks, and approvals run exactly as for any
   directly-listed tool") — keep them verbatim; they are already concise (1,578 B / 3 tools).
3. The invalid-tool error path (below) names the bridge tools.

**Model calls a deferred tool by its bare name** (`terminal(...)`): fails the
`valid_tool_names` gate → `repair_tool_call` (SP/agent/agent_runtime_helpers.py:2297)
runs first. Verified with difflib cutoff 0.7 that NO deferred name (terminal, read_file,
write_file, search_files, session_search, execute_code, delegate_task, patch, skill_view,
skill_manage, process, cronjob) fuzzy-matches any kept/bridge name — no silent
mis-repair. The model then receives
`Tool 'terminal' does not exist. Available tools: clarify, ..., tool_call, tool_describe, tool_search`
with 3 retries (conversation_loop.py:4440-4500). Deterministic, cheap recovery.

**Wrong arguments through the router:** `tool_call` recursion re-enters
`handle_function_call(underlying, ...)` → `coerce_tool_args` runs against the real
schema (string→int etc., parity with direct calls), and the tool's own error text
returns (verified: read_file returned its not-found + similar-files hint). The
`tool_describe`-before-`tool_call` instruction in the bridge descriptions keeps blind
calls rare.

**Agent-loop tools deferred (session_search, delegate_task):** work in real sessions
because the unwrap happens in tool_executor *before* agent-loop routing (§1(b));
the `"must be handled by the agent loop"` stub in model_tools:1161 is only reachable
from the execute_code sandbox proxy — covered by test T7.

**Plugin fails to load / upstream renames internals:** register() bails without
patching → sessions silently fall back to today's flat 17-tool payload. Fail-safe:
capability never degrades, only the token bill.

**Router tool descriptions (final, exact):** stock tool_search/tool_describe/tool_call
descriptions from tool_search.py:433-448 unchanged, plus the appended sentence:
`Deferred tools available via this router: delegate_task, execute_code, patch, process, read_file, search_files, session_search, skill_manage, skill_view, terminal, write_file.`

---

## 4. Update survival, rollback, rollout

- **Survival:** plugin + config live under `~/.hermes` — untouched by `hermes update`.
  Residual risk is *semantic* drift of tool_search internals on upgrade; the symbol
  guard turns that into a loud no-op fallback. Add a one-line note in the plugin README
  pinning "built against hermes-agent 0.18.2 tool_search API".
- **Rollback (single flag):** `tools.tool_search.enabled: off` → assembly skipped
  entirely (model_tools.py:550) → flat 17 tools next session. Alternative levers:
  remove `router` from `plugins.enabled`, or `hermes tools`-level changes. No code undo.
- **Per-profile rollout order:** plugins resolve via `get_hermes_home()/plugins`
  (plugins.py:1348) — profile-aware, so install once at `~/.hermes/plugins/router/` and
  symlink into each profile: `ln -s ~/.hermes/plugins/router ~/.hermes/profiles/<n>/plugins/router`.
  Order: roll out to the lowest-traffic profile first (24 h soak), then the
  root/default bot, then the remaining named profiles in ascending traffic
  order. Each step: add config keys, restart that profile's gateway, run smoke
  tests T1-T3 below.

---

## 5. E2E test plan (real vllm-local model)

**Harness:** one-shot non-interactive turns via `hermes chat -q "<task>" -Q`
(and `hermes -z "<task>"`); toolset override with `-t` where needed; per-profile via the
profile's own shell/HOME. Session JSONs land in `~/.hermes/sessions/` (assert on
`api_calls`, tool names in messages). Payload measurement: `hermes prompt-size --platform telegram --json`
(uses the same offline inspection agent — numbers here were produced with it).

| # | Scenario | Pass criteria |
|---|----------|---------------|
| T1 | `hermes prompt-size` telegram+cli, before/after | telegram tools JSON ≤ 11 KB; system+tools ≤ 26 KB (~6.5k tok); 9 tools listed incl. 3 bridges |
| T2 | Core-only task: "Remember that my favorite color is blue" | `memory` called directly, zero bridge calls, ≤2 API calls |
| T3 | Router task: "How much disk space is free on this machine?" | reaches `terminal` via describe/call (search optional given names index); correct df output relayed; ≤6 API calls; activity feed shows `terminal`, not `tool_call` |
| T4 | File via router: "Show the first 5 lines of ~/.hermes/config.yaml" | `tool_call(read_file)` succeeds |
| T5 | Approval parity: "Delete /tmp/router-test-dir with rm -rf" (CLI, no --yolo) | dangerous-command approval fires naming `terminal`; deny returns block to model; telegram variant shows approval buttons |
| T6 | Multi-turn persistence: T3 then "now show memory usage too" in same session (`--resume`) | turn 2 goes straight to `tool_call(terminal)` — no re-search; vllm-local logs show prefix reuse (APC hit / stable prefill) |
| T7 | execute_code sandbox: task forcing `execute_code` that internally calls a deferred tool | sandbox proxy path returns real result, not the agent-loop stub |
| T8 | Restricted subagent: delegate_task subtask needing terminal when child toolsets exclude it | bridge scope block message, no execution (offline variant already verified) |
| T9 | Kanban worker smoke: dispatch trivial board task | worker completes via `kanban_complete` (env-widened keep-list), guidance present in its prompt |
| T10 | Robustness: run T3 five times | 5/5 task success; mean invalid-tool retries ≤1; no `tool_call` argument-shape failures |

**Quantitative before/after to record in the rollout note:** fixed payload bytes/tokens
(T1), mean API calls for T2/T3, retry counts (T10), and first-token latency on turn 2 of
T6 (prefix-cache proxy).

---

## Appendix: experiment scripts
`exp1_baseline.py`, `exp2_router_sim.py`, `exp3_variants.py` in this scratchpad reproduce
every number above offline (no network, no config writes).
