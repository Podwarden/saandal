#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Podwarden Inc

"""Install/reapply the router plugin to the root profile and all named profiles.

Idempotent: copies plugin/ from this repo into each profile's plugins/router/
and ensures the config keys exist (without clobbering user-tuned values).
Run after cloning, after editing plugin source, or to repair a deleted install.
`hermes update` does NOT remove the plugin (it lives under ~/.hermes), so this
script is for repair/rollout, not a required post-upgrade step — that's
verify.py's job.
"""
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "plugin"


def hermes_home() -> Path:
    """The hermes home directory: $HERMES_HOME if set, else ~/.hermes."""
    env = os.environ.get("HERMES_HOME")
    return Path(env).expanduser() if env else (Path.home() / ".hermes")


def homes(root: Path) -> "list[Path]":
    """The root home plus any named profiles under <root>/profiles/. Robust to a
    missing profiles/ directory — a single-profile install simply has none."""
    result = [root]
    pdir = root / "profiles"
    if pdir.is_dir():
        result += sorted(p for p in pdir.iterdir() if p.is_dir())
    return result


ROOT = hermes_home()
PROFILES = homes(ROOT)

DEFAULT_KEEP_FLAT = ["clarify", "todo", "memory", "vision_analyze",
                     "text_to_speech", "skills_list", "web_search"]


def ensure_config(cfg_path: Path) -> list[str]:
    from ruamel.yaml import YAML
    yaml = YAML()
    yaml.preserve_quotes = True
    # Tolerate a home without a config.yaml (fresh/minimal install): start from
    # empty and seed the defaults, which creates the file.
    data = (yaml.load(cfg_path.read_text()) if cfg_path.exists() else None) or {}
    changed = []

    tools = data.setdefault("tools", {})
    ts = tools.setdefault("tool_search", {})
    # Respect an existing value (e.g. an incident rollback to "off") — only
    # seed the key when it is absent entirely.
    if "enabled" not in ts:
        ts["enabled"] = "on"
        changed.append("tools.tool_search.enabled=on")
    if "keep_flat" not in ts:
        ts["keep_flat"] = list(DEFAULT_KEEP_FLAT)
        changed.append("tools.tool_search.keep_flat=<default>")

    # Loop hard-stop guardrail (incident 2026-07-18): blocks identical failing
    # tool calls after 5 repeats and halts the turn after 8 same-tool failures.
    # Only seeded when absent so a user-tuned value is never clobbered.
    guard = data.setdefault("tool_loop_guardrails", {})
    if "hard_stop_enabled" not in guard:
        guard["hard_stop_enabled"] = True
        changed.append("tool_loop_guardrails.hard_stop_enabled=true")

    # v1.3.0: local extract backend (plugin/web_local.py) — without this key
    # web_extract has no backend on this install (SearXNG is search-only).
    web = data.setdefault("web", {})
    if "extract_backend" not in web:
        web["extract_backend"] = "local"
        changed.append("web.extract_backend=local")

    # v1.3.0: search steering threshold. Default lives in the plugin code
    # too; seeding it here makes the knob discoverable. <1 disables.
    if "search_steer_after" not in ts:
        ts["search_steer_after"] = 6
        changed.append("tools.tool_search.search_steer_after=6")

    # v1.4.0: constrained tool-call decoding. The PLUGIN default is "off";
    # this deploy-time seed is what consciously turns it on everywhere —
    # only when the key is absent, so an incident rollback to "off" (or a
    # tuned host list) is never overwritten by a re-install.
    # Quoted on purpose: hermes loads config with YAML-1.1 safe_load, where a
    # bare `on` parses as boolean True (the plugin accepts both, but the file
    # should say what it means).
    from ruamel.yaml.scalarstring import SingleQuotedScalarString
    if "constrained_decoding" not in ts:
        ts["constrained_decoding"] = SingleQuotedScalarString("on")
        changed.append("tools.tool_search.constrained_decoding=on")
    if "constrained_hosts" not in ts:
        ts["constrained_hosts"] = ["vllm.example.com"]
        changed.append("tools.tool_search.constrained_hosts=[vllm.example.com]")

    # v1.5.0: graceful flat-name fallback — bare-name calls to deferred tools
    # execute instead of hard-killing the turn after 3 invalid-call retries.
    # Plugin default is already "on"; seeding makes the knob discoverable and,
    # as always, only when absent so a rollback to "off" survives reinstalls.
    for _k in ("force_verify", "calc_route"):
        if _k not in ts:
            ts[_k] = SingleQuotedScalarString("off")
            changed.append(f"tools.tool_search.{_k}=off")
    if "flat_name_fallback" not in ts:
        ts["flat_name_fallback"] = SingleQuotedScalarString("on")
        changed.append("tools.tool_search.flat_name_fallback=on")

    # v1.17.0: anti-fabrication (claim-grounding). B (antifab) = post-draft
    # numeric claim-grounding annotate — append-only "unverified figures" caveat
    # on research turns; A (ground_directive) = a ~60-token pre-draft grounding
    # directive; C (verify_pass) = an optional strong-tier escalation LLM pass.
    # Plugin defaults match these seeds (B/A on, C off); only-when-absent so an
    # incident rollback ("off") or a tuned min survives reinstalls.
    for _k, _v in (("antifab", "on"), ("ground_directive", "on"),
                   ("verify_pass", "off")):
        if _k not in ts:
            ts[_k] = SingleQuotedScalarString(_v)
            changed.append(f"tools.tool_search.{_k}={_v}")
    if "antifab_min" not in ts:
        ts["antifab_min"] = 1
        changed.append("tools.tool_search.antifab_min=1")

    # v1.18.0: clarify guard — an ADD-ONLY pre-turn directive that nudges the
    # model to ask ONE clarifying question (instead of guessing / spelunking
    # session history and over-executing) on a genuinely ambiguous, no-referent
    # prompt ("Fix it.", "finish the thing we discussed"). Tier-agnostic
    # (un-host-gated); conservative detector biased to silence so a CLEAR short
    # prompt never trips it. Plugin default is also "on"; "off" is the one-key
    # rollback. Only-when-absent so a rollback survives reinstalls.
    if "clarify_guard" not in ts:
        ts["clarify_guard"] = SingleQuotedScalarString("on")
        changed.append("tools.tool_search.clarify_guard=on")
    if "verify_pass_min" not in ts:
        ts["verify_pass_min"] = 2
        changed.append("tools.tool_search.verify_pass_min=2")

    # v1.19.0: clarify-finalize + tier-independent runaway wall — two ADD-ONLY,
    # fail-safe pre_tool_call gates. clarify_finalize: on a clarify_guard-flagged
    # turn, once the model invokes `clarify`, end the turn with the clarifying
    # question and block further tool calls (never touches a clear prompt / normal
    # turn — keyed off the guard's own firing). runaway_wall: a tier-independent
    # backstop that ends a hung tool loop as an honest failure past a
    # conservatively HIGH call-count / wall-clock (set above any legit long-research
    # turn). Plugin defaults match these; only-when-absent so a rollback survives
    # reinstalls. "off" is the one-key rollback on each flag.
    if "clarify_finalize" not in ts:
        ts["clarify_finalize"] = SingleQuotedScalarString("on")
        changed.append("tools.tool_search.clarify_finalize=on")
    if "runaway_wall" not in ts:
        ts["runaway_wall"] = SingleQuotedScalarString("on")
        changed.append("tools.tool_search.runaway_wall=on")
    if "runaway_call_cap" not in ts:
        ts["runaway_call_cap"] = 50
        changed.append("tools.tool_search.runaway_call_cap=50")
    if "runaway_wall_secs" not in ts:
        ts["runaway_wall_secs"] = 480
        changed.append("tools.tool_search.runaway_wall_secs=480")

    # v1.5.1: model-side decoding mitigations. All three only-when-absent so
    # a tuned/rolled-back value survives reinstalls. max_tokens_cap: explicit
    # 0 disables clamping (the plugin default when the key is absent is also
    # 3000); frequency_penalty: explicit 0 disables injection; brand_nudge:
    # "off" disables the Hermès-fashion-brand re-search note.
    # v1.11.1: raised 3000 -> 8000 on the vLLM engine owner's guidance — an
    # explicit max_tokens >= 8000 plus enable_thinking=false (no_think_always)
    # is the client-side bound that stops over-reasoning before it starts (the
    # server detector is defense-in-depth). 8000 also avoids finish_reason=length
    # truncations -> fewer continuation retries -> less shared-engine load.
    if "max_tokens_cap" not in ts:
        ts["max_tokens_cap"] = 8000
        changed.append("tools.tool_search.max_tokens_cap=8000")
    # v1.8.2: frequency_penalty is now seeded to 0 (OFF). A/B with spec-decode
    # permanently off proved the v1.5.1 seed of 0.3 was the CAUSE of long-output
    # TAIL COLLAPSE (per-count accumulation forbids recurring tokens + the EOS,
    # derailing greedy decoding into word-salad at the tail). This release also
    # MIGRATES the exact old seed (0.3) to 0 in place on already-deployed
    # configs — the same precedent as v1.7.3 lowering fail_wall_secs 480->240 —
    # while leaving any other user-tuned value untouched.
    if "frequency_penalty" not in ts:
        ts["frequency_penalty"] = 0
        changed.append("tools.tool_search.frequency_penalty=0")
    elif ts.get("frequency_penalty") == 0.3:
        ts["frequency_penalty"] = 0
        changed.append("tools.tool_search.frequency_penalty:0.3->0 (v1.8.2)")
    # v1.8.2: presence_penalty is the replacement anti-loop insurance — FLAT
    # (one-shot per token, does not accumulate) so it curbs genuine loops
    # without derailing long structured output. Winner of the A/B (every probe
    # completed all items and stopped naturally). Only-when-absent; explicit 0
    # disables injection.
    if "presence_penalty" not in ts:
        ts["presence_penalty"] = 0.3
        changed.append("tools.tool_search.presence_penalty=0.3")
    if "brand_nudge" not in ts:
        ts["brand_nudge"] = SingleQuotedScalarString("on")
        changed.append("tools.tool_search.brand_nudge=on")

    # v1.9.0: TASK-AWARE coding / large-output accommodation. On a detected
    # coding turn (low-false-positive classifier) the max_tokens cap is raised
    # to coding_max_tokens and the selfheal S7 wall to coding_wall_secs so a
    # real code file isn't truncated to an empty write_file and the turn isn't
    # forced-synthesised before the file is written. EVERY non-coding turn keeps
    # the tight 3000/150 bounds exactly (proven research/chat batch). Master
    # switch coding_mode: "auto" (on) | "off" (behave exactly as before). All
    # three only-when-absent so a rollback ("off") or a tuned value survives
    # reinstalls; the plugin's built-in defaults match these seeds.
    if "coding_mode" not in ts:
        ts["coding_mode"] = SingleQuotedScalarString("auto")
        changed.append("tools.tool_search.coding_mode=auto")
    if "coding_max_tokens" not in ts:
        ts["coding_max_tokens"] = 8000
        changed.append("tools.tool_search.coding_max_tokens=8000")
    if "coding_wall_secs" not in ts:
        ts["coding_wall_secs"] = 360
        changed.append("tools.tool_search.coding_wall_secs=360")

    # v1.5.2: duplicate-query hard gate — the same normalized web_search query
    # attempted more than N times in one turn is blocked before the network
    # call with an error-style result. Plugin default is also 3 when the key
    # is absent; <1 disables. Only-when-absent as always.
    if "dup_query_limit" not in ts:
        ts["dup_query_limit"] = 3
        changed.append("tools.tool_search.dup_query_limit=3")

    # v1.5.3: general duplicate-call gate — the same tool invoked with
    # byte-identical (normalized) arguments more than N times in one turn is
    # blocked before execution (tool_call bridge unwrapped, so bridged and
    # bare calls share a counter). Plugin default is also 3 when the key is
    # absent; <1 disables the general gate (the web_search query gate above
    # stays independent). dup_call_exempt lists tools never gated (legit
    # repeats / self-limiting). Only-when-absent as always.
    if "dup_call_limit" not in ts:
        ts["dup_call_limit"] = 3
        changed.append("tools.tool_search.dup_call_limit=3")
    if "dup_call_exempt" not in ts:
        ts["dup_call_exempt"] = ["clarify", "todo", "memory",
                                 "text_to_speech", "tool_search",
                                 "tool_describe"]
        changed.append("tools.tool_search.dup_call_exempt=<default>")

    # v1.5.4: hard per-turn search ceiling — ALL web_search attempts (blocked
    # and successful alike) past N in one turn are blocked regardless of query
    # wording; the paraphrase escape the dup gates can't catch. Plugin default
    # is also 15 when the key is absent; explicit 0 (or <1) disables.
    # Only-when-absent as always.
    if "search_hard_cap" not in ts:
        ts["search_hard_cap"] = 15
        changed.append("tools.tool_search.search_hard_cap=15")

    # v1.6.0: session self-healing layer (plugin/selfheal.py). Every key is
    # seeded ONLY when absent (per-key, so a tuned threshold or an incident
    # rollback of one flag survives reinstalls). soft_nudge ships OFF —
    # design experiment E showed 3/3 endpoint stalls when injecting a
    # corrective user message while tools stay enabled.
    sh = ts.setdefault("selfheal", {})
    selfheal_defaults = [
        ("enabled", SingleQuotedScalarString("on")),
        ("soft_nudge", SingleQuotedScalarString("off")),
        # v1.6.2 (corpus batch 2 T2): append a short anti-fabrication system
        # note once the turn reaches WOBBLING+ (r00/r04/r18 invented version
        # numbers / release names / a hermes version while WOBBLING). Advisory
        # only — HEALTHY turns untouched. Plugin default is also "on".
        ("wobble_honesty", SingleQuotedScalarString("on")),
        ("forced_synthesis", SingleQuotedScalarString("on")),
        # no_think: during forced synthesis, disable thinking at the chat
        # template level (vLLM chat_template_kwargs, constrained_hosts only)
        # — without it the model often writes the answer inside the
        # reasoning channel and content stays empty (0/3 in the 2026-07-19
        # implementation-phase reruns; deterministic cure when on).
        ("no_think", SingleQuotedScalarString("on")),
        ("fresh_retry", SingleQuotedScalarString("on")),
        ("wobble_searches", 8),
        ("wobble_blocks", 2),
        ("wobble_blank", 2),
        ("wobble_sim", 0.6),
        ("wobble_sim_n", 6),
        ("fail_blocks", 5),
        ("fail_blank", 4),
        ("fail_budget_frac", 0.6),
        # v1.8.3 (2026-07-19 12-min-turn incident): 150 (was 240, was 480).
        # Forced synthesis (S7) now arms one logical-call boundary sooner so a
        # stalled stream is escalated to a tool-stripped synthesis faster.
        # Only-when-absent — a deployed config already carrying 240/480 is NOT
        # lowered by re-install; the deploy step lowers it to 150 in place.
        ("fail_wall_secs", 150),
        ("doomed_min_answer", 40),
        ("poison_hi", 0.25),
        # v1.7.1 (FIX 1): proactive poison reset at message arrival. When the
        # existing session's LAST COMPLETED turn is degenerate above
        # poison_reset_hi (last-turn degeneracy fraction; measured 0.62 on the
        # real poisoned Silverton session vs a 0.33 max across 58 clean/churn
        # controls, so 0.35 is conservative — higher than poison_hi), the
        # session is reset BEFORE the incoming question is dispatched so it runs
        # clean. Plugin defaults match these; only-when-absent so a rollback
        # (proactive_reset: "off") survives reinstalls.
        ("proactive_reset", SingleQuotedScalarString("on")),
        ("poison_reset_hi", 0.35),
        ("max_retries_per_question", 1),
        # v1.11.2: bounded corruption auto-retry. On a detected token-corruption
        # glitch, re-dispatch the verbatim question in a fresh session up to N
        # times (with a "retrying (k/N)" note) before falling back to the honest
        # withhold. 0 disables (restores the immediate-withhold behavior).
        ("corrupt_retries", 2),
        # v1.11.3: convert markdown tables to Telegram-safe bullet groups before
        # the platform adapter's own converter (which mangles **bold** header
        # cells into malformed MarkdownV2, making long table answers render
        # blank). "off" restores raw markdown tables.
        ("telegram_tablesafe", SingleQuotedScalarString("on")),
        ("platforms", ["telegram"]),
    ]
    for k, v in selfheal_defaults:
        if k not in sh:
            sh[k] = list(v) if isinstance(v, list) else v
            changed.append(f"tools.tool_search.selfheal.{k}={v}")

    # v1.12.0: multi-topic conversation system (plugin/topics.py). Seeded OFF —
    # turning it on is a conscious deploy step (like constrained_decoding). The
    # code ships INERT until tools.tool_search.topics.enabled flips to "on";
    # sub-flags allow graduated rollback (badge/classify/summarize).
    tp = ts.setdefault("topics", {})
    topics_defaults = [
        ("enabled", SingleQuotedScalarString("off")),
        ("badge", SingleQuotedScalarString("on")),
        # v1.20.0: badge on a strong-tier host too. The continuity injection
        # stays blanket-inert there; this is only the t#NNNNN label, which is
        # what makes a thread referenceable from a phone. 'off' restores the
        # v1.15.0 suppression.
        ("badge_strong", SingleQuotedScalarString("on")),
        ("summarize", SingleQuotedScalarString("on")),
        ("classify", SingleQuotedScalarString("llm")),
        ("max_open", 8),
        ("block_token_cap", 250),
        ("summarize_every", 6),
        ("rehydrate_overlap", 0.30),
        # v1.14.0: active long-term memory + research scratchpads (R0-R4). All
        # seeded OFF — inert until each is a conscious flip. research: /research
        # plan checklist + injection + reconcile; research_auto: heuristic
        # promotion of a fresh topic; research_decompose: llm|off plan seeding;
        # scratchpad: capture url-less research notes; resume_surface: name the
        # t#NNNNN tag on a dormant resume; active_recall: proactively surface a
        # known fact; self_knowledge: inject memory self-knowledge; autostore:
        # amortized durable-fact curation into inbox (human merge gate).
        ("research", SingleQuotedScalarString("off")),
        ("research_auto", SingleQuotedScalarString("off")),
        ("research_decompose", SingleQuotedScalarString("off")),
        ("scratchpad", SingleQuotedScalarString("off")),
        ("resume_surface", SingleQuotedScalarString("off")),
        ("active_recall", SingleQuotedScalarString("off")),
        ("self_knowledge", SingleQuotedScalarString("off")),
        ("autostore", SingleQuotedScalarString("off")),
        ("plan_max_items", 6),
        ("plan_token_cap", 180),
        ("recall_facts_max", 5),
        ("autostore_every", 4),
        ("plan_overlap", 0.34),
    ]
    for k, v in topics_defaults:
        if k not in tp:
            tp[k] = v
            changed.append(f"tools.tool_search.topics.{k}={v}")

    # v1.6.1: disable the model's reasoning channel on EVERY call to the
    # constrained hosts (not just forced synthesis). Corpus batch 1 leaked
    # <think>-style deliberation into 4/26 final answers WITHOUT literal
    # tags (post-hoc stripping can't catch it). A/B 2026-07-19 (5 corpus
    # prompts per arm): off = 2/5 leaks incl. one pure thinking-dump
    # non-answer; on = 0/5 leaks, all answered, hard-question quality
    # unchanged (see README). The PLUGIN default is "off"; this seed is the
    # conscious deploy decision, and an incident rollback survives
    # re-installs (only-when-absent, as always).
    if "no_think_always" not in ts:
        ts["no_think_always"] = SingleQuotedScalarString("on")
        changed.append("tools.tool_search.no_think_always=on")

    # v1.6.1: outgoing-answer secret scrub (corpus batch 1 run 21: a vw_ API
    # key read from a config file was printed verbatim in the final answer;
    # hermes's own redactor covers tool output/logs, never model-composed
    # answers, and does not know the vw_ prefix). Plugin default is also
    # "on" when the key is absent; "off" disables. Only-when-absent.
    if "secret_scrub" not in ts:
        ts["secret_scrub"] = SingleQuotedScalarString("on")
        changed.append("tools.tool_search.secret_scrub=on")

    # v1.6.2 (corpus batch 2 T1/T3): unconditional final-text degeneracy guard
    # in the selfheal transform_llm_output callback — truncates long-form
    # repetition collapse and replaces bare intent announcements on HEALTHY
    # turns too (batch-2 r08/r21/r26/r39 + mt0). Plugin default is also "on";
    # "off" disables. Only-when-absent so a rollback survives reinstalls.
    if "repetition_guard" not in ts:
        ts["repetition_guard"] = SingleQuotedScalarString("on")
        changed.append("tools.tool_search.repetition_guard=on")

    # v1.7.3 (corpus batch 3): token-corruption / mutating-fragment guard in
    # the selfheal final-guard + classify_final. Flags coherent-prose finals
    # with dropped/doubled/truncated tokens, unbalanced emphasis, or a bare
    # trailing stub (cl05/cl06/ms01 shipped corrupted past the v1.7.2 guards)
    # and converts them to an honest failure. DEFENSIVE only — the root cause
    # is server-side spec-decode/fp8; a server rollback restores correctness.
    # Plugin default is also "on"; "off" disables. Only-when-absent so a
    # rollback survives reinstalls.
    if "corruption_guard" not in ts:
        ts["corruption_guard"] = SingleQuotedScalarString("on")
        changed.append("tools.tool_search.corruption_guard=on")

    # v1.15.0: WEAK-MODEL-HOST gating of the 27B-specific correctness guards
    # (output-corruption withhold, selfheal escalation ladder, topics
    # continuity/auto-resume injection). "on" reuses tools.tool_search.
    # constrained_hosts as the weak-host allowlist — so vLLM profiles keep every
    # guard while a frontier-model host (deepseek) sheds the harmful ones, with
    # NO per-profile config divergence. "off" restores the pre-1.15 behaviour
    # (guards active on every host). Plugin default is also "on"; only-when-absent
    # so a rollback survives reinstalls.
    if "weak_host_guards" not in ts:
        ts["weak_host_guards"] = SingleQuotedScalarString("on")
        changed.append("tools.tool_search.weak_host_guards=on")

    # v1.16.0: MODEL-TIER map (self-calibrating replacement for the v1.15.0
    # weak-host nameplate; weak_host_guards above stays as the rollback alias).
    # Resolved per request most-specific-wins: exact model id -> model-id glob
    # -> base_url host -> provider id -> default. The seed is equivalence-
    # preserving on the current fleet: the frontier root model is STRONG (sheds
    # the 27B-only correctness guards), every unlisted model (the vLLM 27B on
    # the profile homes, and the root's 27B fallback_model) falls to the WEAK
    # default (full protection — the fail-safe floor). ONBOARDING A MODEL is one
    # line: add `<model-id>: strong|weak` under this key. Only-when-absent, so a
    # tuned map / rollback survives reinstalls. When this key is ABSENT the
    # plugin derives the tier from weak_host_guards/constrained_hosts (the exact
    # v1.15.0 behaviour), so an operator can delete it to fall back cleanly.
    if "model_tier" not in ts:
        from ruamel.yaml.comments import CommentedMap
        mt = CommentedMap()
        mt["deepseek-v4-pro"] = SingleQuotedScalarString("strong")
        mt["default"] = SingleQuotedScalarString("weak")
        ts["model_tier"] = mt
        changed.append("tools.tool_search.model_tier={deepseek-v4-pro:strong,"
                       "default:weak}")

    # v1.15.0: web_extract pre-halt failure cap (MODEL-AGNOSTIC — helps both).
    # After N failed extracts in a turn, the pre_tool_call gate blocks further
    # web_extract before hermes's same_tool_failure_halt (8 consecutive
    # failures) ships an empty answer; the block tells the model to synthesize
    # from the search snippets it already has (batch4 r03/r24/r40). 0 disables.
    # Only-when-absent so a tuned value / rollback survives reinstalls.
    if "extract_fail_cap" not in ts:
        ts["extract_fail_cap"] = 3
        changed.append("tools.tool_search.extract_fail_cap=3")
    # v1.8.1 extends corruption_guard with three MORE class-B signals
    # (doubled-phrase / dropped-letter proper noun / markdown table collapse) —
    # same flag, no new key to seed. v1.8.3 adds the emoji/symbol-spew signal
    # under the SAME corruption_guard flag — also NO new key to seed. The v1.8.1
    # OPT-IN
    # tools.tool_search.selfheal.math_execute_nudge is deliberately NOT seeded:
    # the math-shape regex isn't reliably math-only, so it stays off until an
    # operator consciously enables it (plugin default is "off").

    # v1.7.0: intermediate progress messages (plugin/progress.py). Every key
    # seeded ONLY when absent (per-key, so a tuned threshold or an incident
    # rollback of the master flag survives reinstalls). Content-bearing interim
    # Telegram updates during long turns ("So far I've found X and Y; still
    # checking…") synthesized from actual tool results. Plugin defaults match
    # these values when the keys are absent.
    pg = ts.setdefault("progress", {})
    progress_defaults = [
        ("enabled", SingleQuotedScalarString("on")),
        ("platforms", ["telegram"]),
        ("first_delay_secs", 25),
        ("first_steps", 3),
        ("throttle_secs", 30),
        ("throttle_steps", 2),
        ("min_new_findings", 1),
        ("max_per_turn", 4),
        ("max_findings_per_msg", 3),
    ]
    for k, v in progress_defaults:
        if k not in pg:
            pg[k] = list(v) if isinstance(v, list) else v
            changed.append(f"tools.tool_search.progress.{k}={v}")

    # v1.8.0: latency-inferred server-load awareness (plugin/loadaware.py).
    # Every key seeded ONLY when absent (per-key). Infers HEALTHY/SLOW/STALLED
    # from observed per-call latency + timeout events on the vLLM host and, on
    # SLOW/STALLED, delivers ONE honest, throttled Telegram note ("server under
    # heavy load, still working" / "server unresponsive, try again shortly").
    # It NEVER amplifies (issues no LLM calls). Plugin defaults match these.
    la = ts.setdefault("loadaware", {})
    loadaware_defaults = [
        ("enabled", SingleQuotedScalarString("on")),
        ("platforms", ["telegram"]),
        ("window", 12),
        ("window_secs", 600),
        ("min_samples", 3),
        ("slow_latency_secs", 20),
        ("slow_frac", 0.5),
        ("climb_ratio", 1.5),
        ("stalled_streak", 2),
        ("note_throttle_secs", 45),
        ("max_notes_per_turn", 2),
        ("coord_secs", 12),
    ]
    for k, v in loadaware_defaults:
        if k not in la:
            la[k] = list(v) if isinstance(v, list) else v
            changed.append(f"tools.tool_search.loadaware.{k}={v}")

    # v1.8.0: the BIG anti-congestion-collapse levers are CONFIG-ONLY on this
    # hermes build (a plugin/middleware cannot raise the streaming timeout or
    # suppress retries — see plugin/loadaware.py + README). Both seeded ONLY
    # when absent so operator tuning / an incident value is never clobbered.
    #
    # 1. providers.<id>.request_timeout_seconds: sets hermes's streaming socket
    #    READ timeout (chat_completion_helpers.py:2039-2048) AND the non-stream
    #    read timeout. Kept at 300 (v1.8.0) — it preserves tolerance for a
    #    slow-but-WORKING server that goes briefly silent during a big-context
    #    prefill. It is NOT a turn budget: with SSE keep-alive pings the socket
    #    stays readable, and the stale detector (1b) at 75 s fires first anyway,
    #    so it is only an outer no-byte-wedge backstop. Scoped to this provider.
    #
    # 1b. providers.<id>.stale_timeout_seconds (v1.8.4, 2026-07-19 12-min-turn
    #    incident): the stream/non-stream STALE detector base
    #    (get_provider_stale_timeout). 75 s of ZERO real tokens on the now-healthy
    #    server is a genuine stall, tolerant of a brief prefill/thinking gap on a
    #    light turn. This is THE per-attempt stall cap for light/compacted turns.
    #    NOTE: for heavy contexts the SP scales it UP via max(base, 150/240)
    #    (>50k) / max(base, 240/300) (>100k) — a hardcoded FLOOR config can only
    #    RAISE, never lower (chat_completion_helpers.py:2883-2889). So on heavy
    #    (>50k-token) sessions each attempt still floors at 150-300 s; the only
    #    lever left there is cutting the stream-retry count (env
    #    HERMES_STREAM_RETRIES=1, gateway units) + this fail_wall escalation.
    provider_id = str(((data.get("model") or {}).get("provider")) or "").strip()
    if provider_id:
        providers = data.setdefault("providers", {})
        prov = providers.setdefault(provider_id, {})
        if isinstance(prov, dict) and "request_timeout_seconds" not in prov:
            prov["request_timeout_seconds"] = 300
            changed.append(
                f"providers.{provider_id}.request_timeout_seconds=300")
        if isinstance(prov, dict) and "stale_timeout_seconds" not in prov:
            prov["stale_timeout_seconds"] = 75
            changed.append(
                f"providers.{provider_id}.stale_timeout_seconds=75")

    # 2. agent.api_max_retries: hermes's outer retry loop defaults to 3. Each
    #    client-side timeout leaves its request RUNNING server-side, so retries
    #    DEEPEN a saturated queue (congestion collapse). Reduce to 2 (one
    #    retry). Aggressive anti-collapse operators can set 1 (no retry); the
    #    nested per-stream retries are env-only (HERMES_STREAM_RETRIES, default
    #    2) — set that env to 0 in the gateway environment to disable them.
    agent = data.setdefault("agent", {})
    if isinstance(agent, dict) and "api_max_retries" not in agent:
        agent["api_max_retries"] = 2
        changed.append("agent.api_max_retries=2")

    plugins = data.setdefault("plugins", {})
    enabled = plugins.setdefault("enabled", [])
    if "router" not in enabled:
        enabled.append("router")
        changed.append("plugins.enabled+=router")

    if changed:
        yaml.dump(data, cfg_path.open("w"))
    return changed


def main() -> int:
    if not (SRC / "__init__.py").exists():
        print(f"ERROR: plugin source missing at {SRC}", file=sys.stderr)
        return 1
    if not ROOT.is_dir():
        print(f"ERROR: hermes home not found at {ROOT} — is hermes-agent "
              f"installed? Set HERMES_HOME to override.", file=sys.stderr)
        return 1
    print(f"hermes home: {ROOT}  ({len(PROFILES)} profile(s))")
    for home in PROFILES:
        dest = home / "plugins" / "router"
        dest.mkdir(parents=True, exist_ok=True)
        for f in SRC.iterdir():
            if f.name == "__pycache__":
                continue
            shutil.copy2(f, dest / f.name)
        changed = ensure_config(home / "config.yaml")
        note = f" (config: {', '.join(changed)})" if changed else ""
        print(f"applied -> {dest}{note}")
    print("\nRestart gateways to activate: hermes gateway restart"
          " (+ per profile: hermes --profile <name> gateway restart)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
