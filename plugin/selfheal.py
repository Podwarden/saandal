# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Podwarden Inc

"""selfheal — session self-healing layer for the router plugin (v1.6.0;
v1.6.1 adds the corpus-batch-1 fixes: honest-uncertainty forced-synthesis
corrective, intent-announcement detection in classify_final/S8, and the
outgoing-answer secret scrub composed into the transform_llm_output
callback — hermes applies the FIRST non-empty string a hook returns, so
finisher and scrubber cannot be separate hooks).

Watches every turn through supported plugin surfaces and rescues the two
failure shapes observed live on local-27b (design doc:
docs/selfheal_design.md; incidents 2026-07-18/19): the paraphrase search
fountain that burns the whole iteration budget, and the poisoned session
whose 65k-token failure history yields only "(empty)" / 7-char answers.

State machine per (session_id, turn_id), with a session-level overlay:

    HEALTHY --(T1)--> WOBBLING --(T2)--> FAILING --(T3)--> DOOMED

* WOBBLING: log-only by default. The soft corrective-injection rung
  (``selfheal.soft_nudge``) ships OFF — experiment E (design §1.b) stalled
  the endpoint 3/3 times when a "stop searching" user message was injected
  while tools stayed enabled.
* FAILING: FORCED SYNTHESIS (proven live, experiment C) — every remaining
  API call of the turn has ``tools``/``tool_choice``/``response_format``
  stripped and one constant corrective user message appended; a
  ``pre_tool_call`` tripwire blocks every non-exempt tool for the rest of
  the turn.
* DOOMED: the forced final text is still degenerate (S8) — replace it with
  an honest diagnostic via ``transform_llm_output`` and, on gateway
  platforms, schedule a FRESH-SESSION RETRY: reset_session + agent-cache
  evict + synthetic ``MessageEvent(internal=True)`` re-dispatch carrying the
  verbatim question wrapped in a template rephrase. Max 1 retry per
  (session_key, question); a second doom delivers the honest failure report.

Every private gateway symbol is accessed through guard helpers that log one
warning and degrade to the next-weaker action — a healer failure can NEVER
break normal operation (the same fail-safe covenant as the other router
features). Sensors and the state machine are pure functions over plain
dicts, unit-testable without hermes.

Config (all under ``tools.tool_search.selfheal``, read per call):
    enabled: "on"          master switch ("off" = pure no-op)
    soft_nudge: "off"      WOBBLING corrective injection (harmful on this
                           model/server — experiment E; OFF)
    forced_synthesis: "on" FAILING tool-strip + corrective message + tripwire
    no_think: "on"         disable thinking during forced synthesis (vLLM
                           chat_template_kwargs, constrained_hosts only) —
                           cures the reasoning-channel escape (see DEFAULTS)
    fresh_retry: "on"      DOOMED session reset + re-dispatch (gateway only)
    proactive_reset: "on"  v1.7.1 FIX 1: reset a poisoned session at message
                           arrival (pre_gateway_dispatch), before the incoming
                           question is dispatched, so it runs clean
    poison_reset_hi: 0.35  FIX 1 threshold: last-completed-turn degeneracy
                           fraction that triggers a proactive reset
    wobble_searches: 8, wobble_blocks: 2, wobble_blank: 2,
    wobble_sim: 0.6, wobble_sim_n: 6,
    fail_blocks: 5, fail_blank: 4, fail_budget_frac: 0.6, fail_wall_secs: 240,
    doomed_min_answer: 40, poison_hi: 0.25, max_retries_per_question: 1,
    platforms: [telegram]  fresh_retry allowlist (empty = all)

Registered from plugin/__init__.py::register() AFTER _cd_middleware, so the
forced-synthesis strip removes anything constrained decoding added.
"""
import asyncio
import hashlib
import json
import logging
import re
import time
import weakref

logger = logging.getLogger("hermes.plugins.router.selfheal")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_BUDGET = 90  # agent.max_iterations default (agent_init.py:271)

DEFAULTS = {
    "enabled": "on",
    "soft_nudge": "off",       # experiment E: 3/3 endpoint stalls — ships OFF
    # v1.6.2 (corpus batch 2 T2): append a short anti-fabrication system note
    # once the turn reaches WOBBLING+. Batch-2 r00/r04/r18 invented version
    # numbers / release names / a hermes version while WOBBLING — the honesty
    # guidance previously only reached the model in the FAILING corrective.
    # Advisory system message (not a "stop searching" user turn — that was the
    # experiment-E stall), appended once, cache-stable, HEALTHY turns untouched.
    "wobble_honesty": "on",
    "forced_synthesis": "on",
    # Implementation-phase finding (2026-07-19, 0/3 offline reruns of design
    # variant C): the model often writes its "final answer" INSIDE the
    # reasoning channel (vLLM reasoning parser -> content=None -> the
    # "(empty)" pathology persists even with tools stripped). Disabling
    # thinking at the chat-template level (extra_body.chat_template_kwargs.
    # enable_thinking=false, vLLM passthrough) cures it deterministically.
    # Applied only on the constrained_hosts allowlist (vLLM-specific knob).
    "no_think": "on",
    "fresh_retry": "on",
    "wobble_searches": 8,
    "wobble_blocks": 2,
    "wobble_blank": 2,
    "wobble_sim": 0.6,
    "wobble_sim_n": 6,
    "fail_blocks": 5,
    "fail_blank": 4,
    "fail_budget_frac": 0.6,
    # v1.7.3 (corpus batch 3): lowered 480 -> 240 so a long search/tool loop
    # arms forced-synthesis (S7) and bails well under the corpus 5-min ceiling
    # instead of exceeding it (batch-3 ms03 ran 513s, cl03 508s before S7 at
    # 490s+). Operators with the key already at 480 in a deployed config should
    # lower it to 240; install.py seeds 240 when the key is absent.
    "fail_wall_secs": 240,
    "doomed_min_answer": 40,
    "poison_hi": 0.25,
    # v1.7.1 (FIX 1 — proactive poison reset at message arrival). Session
    # 20260719_012254 was POISONED: 265 messages of prior cat-fracture +
    # Silverton churn that the model imitated, producing ~350-char garbage
    # finals that each individually passed the degeneracy checks so the turn
    # never reached DOOMED. `proactive_reset` resets a poisoned session at
    # pre_gateway_dispatch BEFORE the incoming question is dispatched, so it
    # runs in a clean session. `poison_reset_hi` (0.35, conservative — higher
    # than poison_hi=0.25) is the last-completed-turn degeneracy fraction
    # required to fire (measured: the real poisoned session's last turn = 0.62;
    # 58 clean/churn sessions maxed at 0.33). Gateway-only, platform-gated,
    # at most one reset per inbound message, fail-safe (any error = normal
    # dispatch).
    "proactive_reset": "on",
    "poison_reset_hi": 0.35,
    "max_retries_per_question": 1,
    # v1.11.2: bounded corruption AUTO-RETRY. When the corruption guard fires on
    # a final answer, re-dispatch the verbatim question in a fresh session (a
    # token-corruption glitch is transient, so a re-answer is usually clean) up
    # to this many times — each with a "retrying (k/N)" message — before falling
    # back to the honest withhold. 0 = disabled (immediate withhold, the prior
    # behavior). Bounded on purpose: unbounded retry would loop forever on a
    # detector false positive and burn the shared engine.
    "corrupt_retries": 2,
    # v1.11.3: convert markdown tables to clean Telegram-safe bullet groups in
    # the outgoing text (telegram only) so the platform adapter's own table
    # converter — which mangles **bold** header cells into malformed MarkdownV2
    # — never runs on them. "off" restores the raw markdown tables.
    "telegram_tablesafe": "on",
    "platforms": ["telegram"],
    # v1.8.1 (batch-recovery): OPT-IN math->execute_code nudge. The 27B
    # degenerates on in-head multi-digit arithmetic (batch-recovery: the Japan
    # division digit-looped, 20! stayed in the reasoning channel, decimals
    # dropped). When a user message is CLEARLY math-shaped (an arithmetic cue
    # word + a multi-digit number) this appends one short cache-stable system
    # note telling the model to use execute_code for exact arithmetic. Ships
    # OFF and is deliberately NOT seeded by install.py: the math-shape regex
    # cannot be made reliably math-only (a year or ratio in narrative — "in
    # 2024 the firm divided…" — is a false trigger), so per the task guidance it
    # is a conscious opt-in, not an always-on nudge. The durable defensive win
    # is that the extended corruption/degeneracy guard now catches looped /
    # dropped-digit math output as an HONEST FAILURE. Gated to constrained_hosts.
    "math_execute_nudge": "off",
}

_FLAG_KEYS = ("enabled", "soft_nudge", "wobble_honesty", "forced_synthesis",
              "fresh_retry", "no_think", "proactive_reset", "math_execute_nudge")
_INT_KEYS = ("wobble_searches", "wobble_blocks", "wobble_blank",
             "wobble_sim_n", "fail_blocks", "fail_blank", "fail_wall_secs",
             "doomed_min_answer", "max_retries_per_question", "corrupt_retries")
_FLOAT_KEYS = ("wobble_sim", "fail_budget_frac", "poison_hi", "poison_reset_hi")


def _norm_flag(v, default):
    if isinstance(v, bool):
        return "on" if v else "off"
    s = str(v).strip().lower()
    return s if s in ("on", "off") else default


def _cfg():
    """Effective selfheal config (DEFAULTS overlaid with the config file).

    Read per call like every other router knob — a config flip needs no
    restart. Invalid values fall back to the default; never raises.
    Also carries the host plugin's search_hard_cap for the T2/S1 trigger.
    """
    out = dict(DEFAULTS)
    out["platforms"] = list(DEFAULTS["platforms"])
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        sh = (((_load().get("tools") or {}).get("tool_search") or {})
              .get("selfheal") or {})
        if isinstance(sh, dict):
            for k in _FLAG_KEYS:
                if k in sh and sh[k] is not None:
                    out[k] = _norm_flag(sh[k], out[k])
            for k in _INT_KEYS:
                if k in sh and sh[k] is not None and not isinstance(sh[k], bool):
                    try:
                        out[k] = int(sh[k])
                    except Exception:
                        pass
            for k in _FLOAT_KEYS:
                if k in sh and sh[k] is not None and not isinstance(sh[k], bool):
                    try:
                        out[k] = float(sh[k])
                    except Exception:
                        pass
            if isinstance(sh.get("platforms"), list):
                out["platforms"] = [str(p).strip().lower()
                                    for p in sh["platforms"] if str(p).strip()]
    except Exception:
        pass
    cap = 15
    try:
        if _host.get("search_hard_cap"):
            cap = int(_host["search_hard_cap"]())
    except Exception:
        cap = 15
    out["search_hard_cap"] = cap
    return out


# ---------------------------------------------------------------------------
# Host plugin bridge (per-turn counters from the v1.5.x dup/cap gates)
# ---------------------------------------------------------------------------

_host = {}  # {"counters": fn(sid, turn_id)->dict, "search_hard_cap": fn,
            #  "unwrap": fn(name, args)->(name, args), "exempt": fn()->set,
            #  "hosts": fn()->list (constrained_hosts allowlist)}


def _no_think_host_ok(base_url):
    """True when *base_url*'s host is on the constrained_hosts allowlist —
    the chat_template_kwargs think-disable is a vLLM-specific knob and must
    never be sent to other providers."""
    try:
        fn = _host.get("hosts")
        hosts = fn() if callable(fn) else []
        if not hosts:
            return False
        from urllib.parse import urlsplit
        return (urlsplit(base_url or "").hostname or "").lower() in hosts
    except Exception:
        return False


# ---------------------------------------------------------------------------
# v1.15.0: WEAK-MODEL-HOST gating. The reliability layer's 27B-specific
# CORRECTNESS guards (output-corruption withhold, the selfheal escalation
# LADDER's interventions, and topics' continuity/auto-resume context injection)
# are calibrated for a small self-hosted model under load. On a clean hosted
# frontier model (e.g. deepseek) those same guards MISFIRE (false-positive
# corruption withholds, needless forced-synthesis overhead, topic-bleed
# derails). We gate them on the MODEL HOST, reusing the existing
# constrained_hosts allowlist as the "weak-model-host" signal — exactly the
# host-gating pattern constrained_decoding / no_think already use. A guard is
# ACTIVE when the request's host is in the weak-host allowlist, INERT otherwise.
#
# tools.tool_search.weak_host_guards controls the allowlist:
#   absent / "on" / true / "hosts"  -> reuse tools.tool_search.constrained_hosts
#   "off" / false                    -> gating DISABLED (guards active on EVERY
#                                       host = the pre-1.15 behaviour; full
#                                       rollback for a mono-weak-model deploy)
#   [list of hosts]                  -> explicit weak-host allowlist
#
# Fail-safe covenant: on ANY uncertainty (unreadable config, unparseable host)
# we treat the host as WEAK (guards active). This can never DISABLE a guard on
# the 27B (the critical invariant — the vLLM profiles must keep full protection);
# at worst it leaves a guard active on the frontier model for one turn (today's
# behaviour), never the reverse.
CONSTRAINED_HOSTS_DEFAULT = ["vllm.example.com"]
_weak_host = {}   # session_id -> {"weak": bool, "ts": float} (v1.15.0 legacy
                  # recorder; retained as an unused compat handle for tests)

# ---------------------------------------------------------------------------
# v1.16.0: MODEL-TIER abstraction (generalizes the v1.15.0 weak-host gate).
#
# "Is this a weak model?" becomes "what TIER is this model?" — an ordered scale
# (weak < mid < strong) resolved per request from the model id (available at
# BOTH the llm_request middleware and pre_llm_call), then base_url host, then
# provider id, then a configurable default. One config line onboards a model:
#   tools.tool_search.model_tier: {deepseek-v4-pro: strong, default: weak}
#
# EQUIVALENCE DISCIPLINE (v1.15.0 -> v1.16.0 pure refactor): when the
# ``model_tier`` key is ABSENT, resolution derives EXACTLY from the v1.15.0
# host signal — weak iff _host_is_weak(base_url) — so behaviour is byte-
# equivalent (same guards active on the same hosts). ``weak_host_guards`` is
# retained as the documented rollback alias (its "off" still forces weak
# everywhere via _host_is_weak).
#
# Fail-safe: any uncertainty / unknown key -> the default tier (weak) -> guards
# active -> the 27B is never exposed. This preserves the critical v1.15.0
# invariant ("can never DISABLE a guard on the 27B") for any unlisted model.
_TIER_RANK = {"weak": 0, "mid": 1, "strong": 2}   # ordered; extensible
DEFAULT_TIER = "weak"                              # fail-safe floor
_session_tier = {}  # session_id -> {"tier": str, "ts": float} (recorded by the
                    # middleware from the ACTUAL request model/base_url, so a
                    # mid-session fallback_model swap re-resolves the tier)


def _model_tier_map():
    """(mapping{key_lower: tier}, default_tier) parsed from
    tools.tool_search.model_tier, or **None** when the key is ABSENT/empty
    (=> derive from the v1.15.0 weak-host signal — the byte-equivalent
    back-compat path). Keys may be a resolved model id, a base_url host, or a
    provider id; the reserved key ``default`` sets the fallback tier. Unknown
    tier values are ignored. Read per call; never raises."""
    try:
        raw = _wh_ts().get("model_tier")
        if not isinstance(raw, dict) or not raw:
            return None
        mapping, default = {}, DEFAULT_TIER
        for k, v in raw.items():
            tier = str(v).strip().lower()
            if tier not in _TIER_RANK:
                continue
            key = str(k).strip().lower()
            if key == "default":
                default = tier
            elif key:
                mapping[key] = tier
        return (mapping, default)
    except Exception:
        return None


def resolve_tier(model="", base_url="", provider=""):
    """Resolve the model tier, most-specific-wins: exact model id -> model-id
    glob -> base_url host -> provider id -> configured default. Fail-safe: any
    uncertainty -> DEFAULT_TIER (weak).

    When ``model_tier`` is ABSENT, derives from the v1.15.0 host signal
    (weak iff _host_is_weak(base_url)) — byte-equivalent to v1.15.0."""
    try:
        # weak_host_guards: off is the documented emergency ROLLBACK ALIAS —
        # gating disabled => force WEAK everywhere (all guards active on every
        # model, the pre-1.15 behaviour), OVERRIDING any model_tier map.
        if _weak_host_allowlist() is None:
            return "weak"
        m = _model_tier_map()
        if m is None:
            # v1.15.0 back-compat: pure host derivation. model/provider unused.
            return "weak" if _host_is_weak(base_url) else "strong"
        mapping, default = m
        model_l = str(model or "").strip().lower()
        host_l = _host_of(base_url)
        prov_l = str(provider or "").strip().lower()
        if model_l and model_l in mapping:
            return mapping[model_l]
        if model_l and ("*" in "".join(mapping) or "?" in "".join(mapping)):
            import fnmatch
            for k, tier in mapping.items():
                if ("*" in k or "?" in k) and fnmatch.fnmatchcase(model_l, k):
                    return tier
        if host_l and host_l in mapping:
            return mapping[host_l]
        if prov_l and prov_l in mapping:
            return mapping[prov_l]
        return default if default in _TIER_RANK else DEFAULT_TIER
    except Exception:
        return DEFAULT_TIER


# ---------------------------------------------------------------------------
# v1.16.0: per-guard tier registry. GUARD_MAX_TIER declares the HIGHEST tier at
# which each WITHHOLD/REPLACE-class guard still FIRES. ADD-only guards are not
# listed -> ceiling "strong" -> they fire at every tier. This is the single
# readable place answering "which guards are tier-scoped and at what ceiling".
# With today's 2-tier fleet, a weak-ceiling guard fires iff the session is weak,
# so guard_active() for a weak-ceiling guard delegates to session_on_weak_host()
# — which keeps the v1.15.0 behaviour AND lets the existing unit-test stubs of
# session_on_weak_host keep controlling these gates (equivalence discipline).
GUARD_MAX_TIER = {
    "corruption":       "weak",   # 27B token corruption; frontier doesn't emit it
    "forced_synthesis": "weak",   # FAILING tool-strip + corrective + tripwire
    "failing_tripwire": "weak",   # the FAILING pre_tool_call tool block
    "doomed_replace":   "weak",   # DOOMED honest-diagnostic replace + fresh retry
    "wobble_honesty":   "weak",   # WOBBLING+ anti-fabrication system note
    "topic_injection":  "weak",   # topics resumption context + reply badge
    # v1.17.0: post-draft claim-grounding annotate is ADD-only (appends a
    # caveat, never deletes/rewrites) => ceiling "strong" => fires on BOTH
    # tiers (the confident-fabrication residual is on deepseek AND the 27B).
    # Listed explicitly for discoverability though "strong" is also the default.
    "claim_grounding":  "strong",
}


def guard_active(guard, session_id=""):
    """True when *guard* still FIRES for this session's tier. ADD-only guards
    (ceiling 'strong', the default) fire everywhere. Weak-ceiling guards
    delegate to session_on_weak_host() (2-tier equivalence). Fail-safe: any
    error -> True (guard active; protects the weak model)."""
    try:
        ceiling = GUARD_MAX_TIER.get(guard, "strong")
        if _TIER_RANK.get(ceiling, 2) >= _TIER_RANK["strong"]:
            return True
        if ceiling == "weak":
            return session_on_weak_host(session_id)
        return _TIER_RANK.get(session_tier(session_id), 0) <= _TIER_RANK[ceiling]
    except Exception:
        return True


# ---------------------------------------------------------------------------
# v1.17.0 (B): post-draft claim-grounding annotate — config, corpus cache,
# and the sibling antifab engine loader.
# ---------------------------------------------------------------------------
_af_mod = None
_af_loaded = [False]
_turn_corpus_cache = {}   # session_id -> {"corpus": str, "ts": float}


def _antifab_mod():
    """Lazy guarded import of the sibling antifab module (once). None on failure
    (=> the claim-grounding step no-ops)."""
    global _af_mod
    if _af_loaded[0]:
        return _af_mod
    _af_loaded[0] = True
    try:
        try:
            from . import antifab as _af
        except ImportError:
            import importlib.util
            import os as _os
            path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                 "antifab.py")
            spec = importlib.util.spec_from_file_location(
                "hermes_plugins_router_antifab", path)
            _af = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(_af)
        _af_mod = _af
    except Exception:
        _af_mod = None
    return _af_mod


def _claim_grounding_on():
    """B: tools.tool_search.antifab == on (default ON). Never raises."""
    try:
        return _norm_flag(_wh_ts().get("antifab"), "on") == "on"
    except Exception:
        return True


def _claim_grounding_min():
    """Minimum ungrounded specifics required to append a caveat
    (tools.tool_search.antifab_min, default 1)."""
    try:
        v = _wh_ts().get("antifab_min")
        if v is not None and not isinstance(v, bool):
            return max(1, int(v))
    except Exception:
        pass
    return 1


def _cache_turn_corpus(session_id, request):
    """Stash the CURRENT turn's retrieved corpus (tool-result text since the last
    real user message) keyed by session_id, so the transform_llm_output claim-
    grounding step can reach it (that hook receives no messages). Called on every
    llm_request while claim_grounding is on; the corpus grows within a turn and
    resets to "" on a new turn's first call (no tool results yet). Fail-safe."""
    try:
        af = _antifab_mod()
        if af is None or not isinstance(request, dict):
            return
        msgs = request.get("messages")
        if not isinstance(msgs, list):
            return
        now = time.time()
        _turn_corpus_cache[session_id] = {"corpus": af.turn_corpus(msgs),
                                          "user": af.turn_user_text(msgs),
                                          "ts": now}
        _prune(_turn_corpus_cache, now)
    except Exception:
        pass


def _claim_verifier_on():
    """C: tools.tool_search.verify_pass == on (default OFF). Never raises."""
    try:
        v = _wh_ts().get("verify_pass")
        return v is not None and _norm_flag(v, "off") == "on"
    except Exception:
        return False


def _claim_verifier_min():
    """Min B-flagged specifics before C escalates (default 2)."""
    try:
        v = _wh_ts().get("verify_pass_min")
        if v is not None and not isinstance(v, bool):
            return max(1, int(v))
    except Exception:
        pass
    return 2


# Injectable verification callable for tests / bespoke clients:
#   fn(draft:str, corpus:str) -> str|None   (a caveat note, or None/"" = clean)
# When None (default), C resolves an OpenAI-compatible client from the configured
# strong provider. Either path is ADD-only and fully guarded.
_claim_verifier_call = None

VERIFY_SYS = (
    "You are a strict fact-checker. You are given an ANSWER and the SOURCES that "
    "were retrieved to write it. List ONLY specific figures/claims in the ANSWER "
    "that are NOT supported by the SOURCES or are attributed to the WRONG entity. "
    "Reply with a single line: either 'OK' (everything is supported) or "
    "'UNSUPPORTED: <comma-separated short list>'. Do not add anything else.")


def _run_claim_verifier(draft, corpus):
    """C engine: a skeptic second-pass LLM check. Returns a short caveat note
    (str) when it finds unsupported/misattributed claims, else None. Fully
    guarded — any failure (no client, no key, network, parse) returns None so
    the answer ships unchanged. Never called on weak tier (caller gate)."""
    try:
        fn = _claim_verifier_call
        if callable(fn):
            note = fn(draft, corpus)
            return note if isinstance(note, str) and note.strip() else None
        base_url = _configured_model_base_url()
        model = _configured_model_id()
        if not base_url or not model:
            return None
        # resolve a key defensively (config, then common env vars)
        key = None
        try:
            try:
                from hermes_cli.config import load_config_readonly as _load
            except ImportError:
                from hermes_cli.config import load_config as _load
            cfg = _load()
            mdl = cfg.get("model") or {}
            key = mdl.get("api_key")
            if not key:
                pid = mdl.get("provider")
                prov = (cfg.get("providers") or {}).get(str(pid)) or {}
                key = prov.get("api_key") if isinstance(prov, dict) else None
        except Exception:
            key = None
        if not key:
            import os as _os
            for env in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "HERMES_API_KEY"):
                if _os.environ.get(env):
                    key = _os.environ[env]
                    break
        if not key:
            _warn_once("verify-no-key",
                       "selfheal: verify_pass on but no API key resolvable — "
                       "verifier inert")
            return None
        try:
            from openai import OpenAI
        except Exception:
            _warn_once("verify-no-openai",
                       "selfheal: verify_pass on but openai SDK unavailable")
            return None
        client = OpenAI(base_url=base_url, api_key=key, timeout=30)
        src = str(corpus or "")[:20000]
        resp = client.chat.completions.create(
            model=model, temperature=0, max_tokens=200,
            messages=[{"role": "system", "content": VERIFY_SYS},
                      {"role": "user",
                       "content": "SOURCES:\n%s\n\nANSWER:\n%s" % (src, draft)}])
        out = (resp.choices[0].message.content or "").strip()
        if out and out.upper() != "OK" and "unsupported" in out.lower():
            body = out.split(":", 1)[-1].strip() if ":" in out else out
            return ("Verifier flagged possibly unsupported/misattributed "
                    "claims: " + body)
        return None
    except Exception:
        logger.debug("selfheal: claim verifier failed; ignored", exc_info=True)
        return None


def _apply_claim_grounding(response_text, session_id):
    """B (+ optional C): append an unverified-figures caveat to *response_text*
    when this turn's retrieved corpus does not ground >= claim_grounding_min
    numeric specifics. APPEND-ONLY — returns None (no change) unless it actually
    appends. Self-gates to research/web turns (empty corpus => None). When
    verify_pass is on AND the tier is strong AND B flagged >= verify_pass_min,
    additionally runs the C skeptic pass and appends its note. Fail-safe."""
    try:
        if not _claim_grounding_on():
            return None
        if not guard_active("claim_grounding", session_id):
            return None
        af = _antifab_mod()
        if af is None:
            return None
        rec = _turn_corpus_cache.get(session_id)
        corpus = (rec or {}).get("corpus") or ""
        user_q = (rec or {}).get("user") or ""
        if not corpus.strip():
            return None  # no retrieved WEB sources this turn => not a research turn
        # A computation/conversion request DERIVES numbers by design — a derived
        # result legitimately absent from the web sources is expected, not a
        # fabrication. Skip B on those turns to avoid caveating a correct answer.
        if af.is_computation_request(user_q):
            return None
        draft = str(response_text or "")
        if not draft.strip():
            return None
        # figures the USER supplied in the question are grounded (not fabricated).
        ungrounded = af.ungrounded_specifics(draft, corpus, given=user_q)
        out = draft
        if len(ungrounded) >= _claim_grounding_min():
            annotated = af.annotate(draft, ungrounded)
            if isinstance(annotated, str) and annotated and annotated != draft:
                out = annotated
                logger.info("selfheal: antifab flagged %d unverified "
                            "specific(s) session=%s: %s", len(ungrounded),
                            session_id or "-", "; ".join(ungrounded))
        # C: gated escalation — strong tier only, never weak (single-slot GPU).
        if (_claim_verifier_on()
                and session_tier(session_id) == "strong"
                and len(ungrounded) >= _claim_verifier_min()):
            note = _run_claim_verifier(draft, corpus)
            if note and note not in out:
                logger.info("selfheal: verify_pass appended a note session=%s",
                            session_id or "-")
                out = out + "\n\n⚠️ " + note
        return out if out != draft else None
    except Exception:
        logger.debug("selfheal: claim grounding failed; text unchanged",
                     exc_info=True)
        return None


def _wh_ts():
    """tools.tool_search dict (read-only, per call). {} on any error."""
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        return ((_load().get("tools") or {}).get("tool_search") or {})
    except Exception:
        return {}


def _host_of(base_url):
    try:
        from urllib.parse import urlsplit
        return (urlsplit(base_url or "").hostname or "").lower()
    except Exception:
        return ""


def _weak_host_allowlist():
    """The weak-model-host allowlist, or None when host-gating is DISABLED
    (weak_host_guards: off -> every host treated weak = guards everywhere).
    Reads weak_host_guards, defaulting to constrained_hosts. Never raises."""
    try:
        ts = _wh_ts()
        raw = ts.get("weak_host_guards")
        if isinstance(raw, list):
            hosts = [str(h).strip().lower() for h in raw if str(h).strip()]
            return hosts  # explicit allowlist (empty list => nothing weak)
        if raw is not None:
            if raw is False:
                return None
            if raw is True:
                pass  # -> constrained_hosts below
            else:
                v = str(raw).strip().lower()
                if v in ("off", "false", "none", "disabled", "0", "no"):
                    return None  # gating disabled
                # "on"/"hosts"/"constrained"/anything else -> constrained_hosts
        # default: reuse constrained_hosts (bridge first, config fallback)
        hosts = None
        try:
            fn = _host.get("hosts")
            if callable(fn):
                hosts = fn()
        except Exception:
            hosts = None
        if not hosts:
            ch = ts.get("constrained_hosts")
            if isinstance(ch, list) and ch:
                hosts = ch
        if not hosts:
            hosts = CONSTRAINED_HOSTS_DEFAULT
        return [str(h).strip().lower() for h in hosts if str(h).strip()]
    except Exception:
        # Fail toward gating-ON with the default allowlist — never disables the
        # 27B guards; a frontier host simply isn't in the list so guards there
        # stay inert unless the host is genuinely unknown (handled by callers).
        return list(CONSTRAINED_HOSTS_DEFAULT)


def _host_is_weak(base_url):
    """True when *base_url*'s host is a weak-model host (guards ACTIVE).
    Fail-safe: any uncertainty -> True (guards active; protects the 27B)."""
    try:
        allow = _weak_host_allowlist()
        if allow is None:
            return True  # gating disabled -> guards active everywhere
        host = _host_of(base_url)
        if not host:
            return True  # unknown host -> treat as weak (protect the 27B)
        return host in allow
    except Exception:
        return True


def _configured_model_base_url():
    """The CONFIGURED active model's base_url (a stable per-home signal, usable
    at pre_llm_call time before the middleware has recorded the request host).
    model.base_url, else providers[model.provider].base_url. "" on error."""
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        cfg = _load()
        model = cfg.get("model") or {}
        bu = model.get("base_url")
        if isinstance(bu, str) and bu.strip():
            return bu
        pid = model.get("provider")
        if pid:
            prov = (cfg.get("providers") or {}).get(str(pid)) or {}
            if isinstance(prov, dict):
                bu = prov.get("base_url")
                if isinstance(bu, str) and bu.strip():
                    return bu
    except Exception:
        pass
    return ""


def _configured_model_id():
    """The CONFIGURED active model id (model.default / model.model / model.name).
    Lets first-turn tier resolution key on the model id before the middleware
    has recorded a request. "" on error."""
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        model = (_load().get("model") or {})
        for k in ("default", "model", "name", "id"):
            v = model.get(k)
            if isinstance(v, str) and v.strip():
                return v
    except Exception:
        pass
    return ""


def _configured_provider():
    """The CONFIGURED active model's provider id. "" on error."""
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        v = (_load().get("model") or {}).get("provider")
        return v if isinstance(v, str) else ""
    except Exception:
        return ""


def _record_tier(session_id, model="", base_url="", provider=""):
    """Record *session_id*'s tier, resolved from the ACTUAL request
    model/base_url/provider. Called from the llm_request middleware on every
    call (before the enabled check), so a mid-session fallback_model swap
    re-resolves the tier the next call."""
    try:
        now = time.time()
        _session_tier[session_id] = {
            "tier": resolve_tier(model, base_url, provider), "ts": now}
        _prune(_session_tier, now)
    except Exception:
        pass


def _record_weak_host(session_id, base_url):
    """v1.15.0 back-compat wrapper (retained for callers/tests): record the
    session tier from the request base_url alone (model/provider unknown)."""
    _record_tier(session_id, "", base_url, "")


def session_tier(session_id=""):
    """This session's resolved model tier (weak|mid|strong).

    Prefers the per-session value recorded by the middleware from the ACTUAL
    request model/base_url (so a fallback_model swap is honoured). Falls back
    to resolving from the CONFIGURED model when nothing is recorded yet — e.g.
    pre_llm_call on a brand-new session's first turn, which fires BEFORE the
    first llm_request. Fail-safe: any uncertainty -> DEFAULT_TIER (weak)."""
    try:
        rec = _session_tier.get(session_id)
        if rec is not None:
            t = rec.get("tier")
            if t in _TIER_RANK:
                return t
        return resolve_tier(_configured_model_id(),
                            _configured_model_base_url(),
                            _configured_provider())
    except Exception:
        return DEFAULT_TIER


def session_on_weak_host(session_id=""):
    """True when this session's model is WEAK-tier (guards ACTIVE).

    v1.16.0: a thin alias of ``session_tier(session_id) == 'weak'`` — every
    v1.15.0 call site keeps working unchanged, and with ``model_tier`` absent
    it is byte-equivalent to the old host-gate. Fail-safe: uncertainty ->
    weak (guards active; protects the weak model)."""
    try:
        return session_tier(session_id) == "weak"
    except Exception:
        return True


def _counters(session_id, turn_id):
    """Per-turn (searches, blocks, queries) snapshot from the host gates."""
    try:
        fn = _host.get("counters")
        if callable(fn):
            d = fn(session_id, turn_id)
            if isinstance(d, dict):
                return {"searches": int(d.get("searches") or 0),
                        "blocks": int(d.get("blocks") or 0),
                        "queries": list(d.get("queries") or [])}
    except Exception:
        logger.debug("selfheal: host counters unavailable", exc_info=True)
    return {"searches": 0, "blocks": 0, "queries": []}


# ---------------------------------------------------------------------------
# State tables (bounded, 1h idle pruning — house pattern)
# ---------------------------------------------------------------------------

_TABLE_MAX = 64
_IDLE_S = 3600

_turns = {}          # session_id -> turn record (latest turn only)
_overlay = {}        # session_id -> session overlay dict
_sessions = {}       # session_key -> {"source","text","ts","session_id"}
_retried = {}        # (session_key, qhash) -> {"count","ts"}
_corrupt_retried = {}  # (session_key, qhash) -> {"count","ts"}  — corruption auto-retry cap
_pending_retry = {}  # session_key -> {"ts","fut"}
_proactive_reset_done = {}  # session_key -> ts (v1.7.1 anti-reflood window)
_gw = {"ref": None, "loop": None, "ts": 0.0}
_warned = set()


def _warn_once(key, msg, *args):
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(msg, *args)


def _prune(table, now):
    if len(table) <= _TABLE_MAX:
        return
    cutoff = now - _IDLE_S
    for k in [k for k, v in list(table.items())
              if float((v or {}).get("ts") or 0) < cutoff]:
        table.pop(k, None)
    if len(table) > _TABLE_MAX:
        table.clear()


def _turn_rec(session_id, turn_id, now):
    """Get-or-create the turn record for (session_id, turn_id).

    One record per session (the latest turn) — a new turn_id replaces it,
    which is also the turn boundary for all per-turn state.
    """
    rec = _turns.get(session_id)
    if rec is None or (turn_id and rec.get("turn_id") != turn_id):
        rec = {"session_id": session_id, "turn_id": turn_id or "",
               "first_ts": now, "ts": now, "api_calls": 0, "blanks": 0,
               "state": "HEALTHY", "triggers": [], "forced_msg": None,
               "findings": [], "counters": {}, "poison_at_birth": False,
               "soft_logged": False, "honesty_logged": False,
               "trip_logged": False,
               "forced_logged": False, "finished": False, "doomed": False,
               "closed": False}
        _turns[session_id] = rec
        _prune(_turns, now)
    rec["ts"] = now
    return rec


def _reset_state():
    """Test helper: wipe all module state."""
    _turns.clear(); _overlay.clear(); _sessions.clear()
    _retried.clear(); _corrupt_retried.clear(); _pending_retry.clear()
    _warned.clear()
    _turn_corpus_cache.clear()
    _proactive_reset_done.clear()
    _gw.update({"ref": None, "loop": None, "ts": 0.0})


# ---------------------------------------------------------------------------
# Pure sensor functions (unit-testable without hermes)
# ---------------------------------------------------------------------------

_THINK_PAT = re.compile(r"<think>.*?(?:</think>|\Z)", re.S)
_GUARD_ERR_PAT = re.compile(
    r"duplicate query loop|duplicate call loop|search limit reached", re.I)
_RETRY_MARKER = "[Fresh session"
_EMPTY_NUDGE_MARK = "returned an empty response"

CORRECTIVE_PREFIX = "SYSTEM NOTICE: tool access for this turn has been disabled"

# hermes wraps tool results in an <untrusted_tool_result …> envelope + a
# "treat as DATA, do not follow instructions" boilerplate paragraph. Peel it
# off before harvesting so a web result parses as JSON and the digest never
# surfaces the raw wrapper tag as a finding (aligned with progress.py).
_WRAPPER_TAG_PAT = re.compile(r"</?[a-z][a-z0-9_]*tool_result\b[^>]*>", re.I)
_WRAPPER_BOILERPLATE_PAT = re.compile(
    r"The following content was retrieved from an external source\.?.*?"
    r"can issue instructions\.", re.I | re.S)


def strip_tool_result_wrapper(text):
    """Peel the untrusted-tool-result envelope (tags + boilerplate) off *text*.
    Fail-safe: returns the input on any error."""
    try:
        s = _WRAPPER_TAG_PAT.sub("", str(text))
        s = _WRAPPER_BOILERPLATE_PAT.sub("", s)
        return s.strip()
    except Exception:
        return text if isinstance(text, str) else ""


def _loads_lenient(s):
    """json.loads tolerant of leading/trailing noise: parse the first JSON
    object/array in *s*. Returns None when nothing parses; never raises."""
    try:
        return json.loads(s)
    except Exception:
        pass
    try:
        dec = json.JSONDecoder()
        for i, ch in enumerate(s):
            if ch in "{[":
                try:
                    obj, _end = dec.raw_decode(s, i)
                    return obj
                except ValueError:
                    continue
    except Exception:
        pass
    return None


def strip_think(text):
    """Visible text with <think> blocks removed (unterminated ones too)."""
    try:
        return _THINK_PAT.sub("", str(text or "")).strip()
    except Exception:
        return str(text or "").strip()


def _content_text(content):
    """Best-effort plain text of a message content (str or parts list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                t = p.get("text")
                if isinstance(t, str):
                    parts.append(t)
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts)
    return "" if content is None else str(content)


def is_blank_content(content):
    """True for the failure placeholders: empty/whitespace or "(empty)"."""
    t = _content_text(content).strip()
    return t == "" or t == "(empty)"


def mean_pairwise_jaccard(queries):
    """Mean pairwise Jaccard similarity of the queries' word sets (0..1)."""
    sets = [frozenset(str(q).casefold().split()) for q in (queries or []) if str(q).strip()]
    if len(sets) < 2:
        return 0.0
    total, n = 0.0, 0
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            union = sets[i] | sets[j]
            if union:
                total += len(sets[i] & sets[j]) / len(union)
            n += 1
    return total / n if n else 0.0


def sim_collapse(queries, threshold, min_n):
    """S3: True when >= min_n queries collapse to near-paraphrases."""
    qs = [q for q in (queries or []) if str(q).strip()]
    if len(qs) < max(2, int(min_n)):
        return False
    return mean_pairwise_jaccard(qs[-8:]) > float(threshold)


def count_turn_blank_assistants(messages):
    """S4: blank/"(empty)" assistant rows since the current turn's real user
    message. hermes's empty-recovery nudges (synthetic user rows containing
    "returned an empty response") and the healer's own corrective message do
    NOT end the scan — they are mid-turn artifacts, not turn boundaries."""
    blanks = 0
    for m in reversed(messages or []):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "user":
            c = _content_text(m.get("content"))
            if _EMPTY_NUDGE_MARK in c or c.startswith(CORRECTIVE_PREFIX):
                continue
            break
        if (role == "assistant" and not m.get("tool_calls")
                and is_blank_content(m.get("content"))):
            blanks += 1
    return blanks


def poison_metrics(history):
    """S5 over a full conversation history: blank-assistant fraction plus
    guard-error / steering counts and the fresh-retry marker."""
    assistants = blanks = guard_errors = steering = 0
    retry_marker = False
    for m in history or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        c = _content_text(m.get("content"))
        if role == "assistant":
            assistants += 1
            if not m.get("tool_calls") and is_blank_content(c):
                blanks += 1
        elif role == "tool":
            if _GUARD_ERR_PAT.search(c):
                guard_errors += 1
            if "STEERING: this was web_search" in c:
                steering += 1
        elif role == "user" and c.lstrip().startswith(_RETRY_MARKER):
            retry_marker = True
    return {"assistants": assistants, "blanks": blanks,
            "blank_frac": (blanks / assistants) if assistants else 0.0,
            "guard_errors": guard_errors, "steering": steering,
            "retry_marker": retry_marker}


# v1.7.1 (FIX 1 — proactive poison reset). The proactive score is the
# degeneracy fraction over the session's LAST COMPLETED turn (messages since the
# last real user message). A poisoned session's last turn is a degenerate churn;
# a healthy one's is a normal answer. Measured on the real data: the poisoned
# Silverton session's last turn scored 0.62, while 58 clean/churn control
# sessions from state.db maxed at 0.33 — poison_reset_hi=0.35 separates them
# with margin. Requires >= PROACTIVE_MIN_TURN_ASST assistant messages in the
# turn (a genuine multi-message loop), so a single bad final never resets a
# session. INTERIM assistant messages (with tool_calls) count only HARD signals
# (inline <tool_call>, phrase-repetition, stub table) — a bare intent
# announcement before a tool call is NORMAL narration, not poison; only FINAL
# (no-tool_call) messages additionally count blanks and the trailing-intent stub.
PROACTIVE_MIN_TURN_ASST = 4
PROACTIVE_MIN_HISTORY = 8


def _deg_assistant(msg, min_chars):
    """Whether an assistant message counts as degenerate for the proactive
    poison score (see the note above). Never raises."""
    try:
        vis = strip_think(_content_text(msg.get("content")))
        has_tc = bool(msg.get("tool_calls"))
        if "<tool_call>" in vis:
            return True
        if detect_phrase_repetition(vis)[0]:
            return True
        if _has_stub_table(vis):
            return True
        if not has_tc:
            t = vis.strip()
            if t == "" or t == "(empty)":
                return True
            if trailing_intent_stub(vis, min_chars):
                return True
            if detect_degenerate_repetition(vis)[0]:
                return True
        return False
    except Exception:
        return False


def proactive_poison_score(history, min_chars=40):
    """(score, detail): degeneracy fraction over the last completed turn. Pure;
    never raises. score is 0.0 (with reason) when the last turn has fewer than
    PROACTIVE_MIN_TURN_ASST assistant messages."""
    try:
        hist = history or []
        idx = -1
        for i in range(len(hist) - 1, -1, -1):
            m = hist[i]
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            if _content_text(m.get("content")).lstrip().startswith(_RETRY_MARKER):
                continue  # a fresh-retry re-dispatch is not a real turn boundary
            idx = i
            break
        turn = hist[idx + 1:] if idx >= 0 else hist
        asst = [m for m in turn if isinstance(m, dict)
                and m.get("role") == "assistant"]
        tools = [m for m in turn if isinstance(m, dict)
                 and m.get("role") == "tool"]
        guard = sum(1 for m in tools
                    if _GUARD_ERR_PAT.search(_content_text(m.get("content"))))
        if len(asst) < PROACTIVE_MIN_TURN_ASST:
            return 0.0, {"asst": len(asst), "deg": 0, "guard": guard,
                         "reason": "too-few-assistants"}
        deg = sum(1 for m in asst if _deg_assistant(m, min_chars))
        return deg / len(asst), {"asst": len(asst), "deg": deg, "guard": guard}
    except Exception:
        return 0.0, {"error": True}


# v1.6.1 (corpus batch 1 run 2): a 220-char intent announcement ("I'll
# research X... Let me start by searching...") passed doomed_min_answer=40
# as a "real" forced-synthesis answer. Detect first-person future-tense
# work announcements; "let me know ..." is explicitly excluded so real
# answers offering follow-up never match.
INTENT_MAX_CHARS = 400  # only short finals can be pure announcements

_INTENT_RE = re.compile(
    r"(?i)(?:\blet\s+me\b(?!\s+know)|\bi\s*['’]?\s*ll\b|\bi\s+will\b|"
    r"\bi\s*['’]?\s*m\s+going\s+to\b|\bi\s+am\s+going\s+to\b)"
    r"[^.!?\n]{0,80}?"
    r"\b(?:search\w*|look\w*|research\w*|check\w*|find\w*|investigat\w*|"
    r"brows\w*|dig\w*|start\w*|begin\w*|proceed\w*|gather\w*|fetch\w*|"
    r"run\w*)\b")


def _intent_only(vis, min_chars):
    """True when a short final is an intent announcement with no content:
    after dropping the announcement sentences, less than *min_chars* of
    actual answer remains. A real answer that merely CONTAINS an intent
    phrase keeps its content sentences and passes."""
    if not vis or len(vis) > INTENT_MAX_CHARS:
        return False
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+|\n+", vis)
             if p.strip()]
    if not parts:
        return False
    flags = [bool(_INTENT_RE.search(p)) for p in parts]
    if not any(flags):
        return False
    residual = " ".join(p for p, f in zip(parts, flags) if not f)
    return len(residual) < int(min_chars)


def classify_final(text, min_chars, weak_host=True):
    """S8: (degenerate, reason) for a turn's final text.

    v1.15.0: the token-corruption rung is a 27B-specific correctness guard and
    is only consulted when *weak_host* (default True keeps every caller and the
    unit tests unchanged; the selfheal call sites pass the resolved per-session
    value so a clean frontier host never routes a dense-numeric answer to
    DOOMED on a corruption false positive)."""
    vis = strip_think(text)
    if "<tool_call>" in vis:
        return True, "inline-tool-call"
    if not vis or vis == "(empty)":
        return True, "empty"
    if len(vis) < int(min_chars):
        return True, "short(%d<%d)" % (len(vis), int(min_chars))
    if _intent_only(vis, min_chars):
        return True, "intent-announcement"
    if detect_degenerate_repetition(vis)[0]:
        return True, "repetition-collapse"
    if trailing_intent_stub(vis, min_chars):
        return True, "intent-stub"
    # v1.7.3 (corpus batch 3): token-corruption / mutating-fragment final —
    # armed turns route it to the DOOMED honest-diagnostic / fresh-retry path.
    # v1.15.0: weak-model-host only (frontier models don't token-corrupt; the
    # guard's false positives were a net loss there).
    if weak_host and _corruption_guard_on():
        corrupt, creason = detect_output_corruption(vis)
        if corrupt:
            return True, "corruption:" + creason
    return False, ""


# ---------------------------------------------------------------------------
# v1.6.2 (corpus batch 2): degenerate long-form repetition detector (T1)
#
# The #1 remaining pathology: the model starts coherent, then collapses into
# a repeated shingle / line / char-run that runs on until natural EOS
# (finish_reason=stop, so the max_tokens clamp never trips — verified: batch-2
# r08/r21/r26/r39 all stopped at 277..2672 out-tokens, well under the 3000
# cap). The turn stays HEALTHY and the output is LONG, so no empty/short check
# flags it. This pure detector finds the earliest long CONSECUTIVE tandem
# repeat and the guard below truncates the answer there.
#
# Detection is a maximal-tandem-run scan: for each period p (1..MAX_PERIOD
# chars) walk the string once counting positions where s[i]==s[i+p]; a run of
# length L means text[i:i+p] repeats L/p+1 times spanning L+p chars. A block is
# degenerate when it spans >= MIN_SPAN chars with >= MIN_COPIES copies AND its
# repeated unit is not pure formatting punctuation (markdown table borders
# `----`/`| --- |`, rule lines `====`, dot leaders are legitimately repetitive
# and must pass). A second word-shingle pass catches multi-word phrase loops
# whose period exceeds MAX_PERIOD chars ("Now I have all ... Now I have all").
# Tuned against the real batch-2 garbage AND clean long answers (the 4-way
# comparison tables, the tri-lingual answer) — see tests/data/ fixtures.
# ---------------------------------------------------------------------------

REPETITION_MIN_SPAN = 80      # chars a char-tandem block must cover to flag
REPETITION_MIN_COPIES = 4     # min consecutive copies of a char-tandem unit
REPETITION_MAX_PERIOD = 48    # max char period considered by the tandem scan
REPETITION_SHINGLE_MAX = 8    # max words in a phrase-loop shingle
REPETITION_SHINGLE_COPIES = 4 # min consecutive copies of a word shingle
REPETITION_SHINGLE_SPAN = 60  # chars a phrase loop must cover to flag
REPETITION_DISTINCT_WIN = 200 # sliding window (chars) for the low-diversity scan
REPETITION_DISTINCT_MAX = 14  # <= this many distinct alnum chars in a mostly-
                              # alnum window is char-garbage (clean min was 20)
REPETITION_RUNON_WORDS = 150  # a single unpunctuated segment this long is a
                              # run-on collapse (clean max observed was ~100)
REPETITION_SALVAGE_MIN = 200  # min clean-prefix chars to truncate vs replace

# v1.7.1 (FIX 2 — PHRASE/SENTENCE-level repetition, even in SHORT finals). The
# real Silverton garbage (session 20260719_012254, ids 5827/5829/5831) was a
# whole ~108-char line repeated exactly TWICE inside a 363-365 char final:
#   "**Silverton Hot Spring — a stunning natural hot spring at ~36°C/97°F) —
#    a lovely natural hot spring at 36°C/97°F)." x2 (period ~180 chars).
# The v1.6.2 detector missed it entirely: char-tandem caps at 48-char periods
# and phrase-loop needs >=4 CONSECUTIVE word-shingle copies — a 2x line repeat
# trips neither. This detector fires on two SIGNALS, both tuned against the real
# garbage AND 146 real clean finals from state.db (0 false positives):
#   adjacent-repeat  two consecutive normalized segments (sentence/line, >=6
#                    words, >=25 chars) that are IDENTICAL and >=80 chars long
#                    — a duplicated full line is an unambiguous collapse marker,
#                    while a short adjacent stutter (<80 chars) is tolerated;
#   dominant-repeat  any normalized segment repeated so its occurrences cover
#                    >= 40% of the whole reply (a non-adjacent dominant loop).
# The 80-char floor cleanly separates the Silverton line (108) from a benign
# short stutter (a clean finance answer duplicated a 59-char sentence once).
PHRASE_SEG_MIN_WORDS = 6
PHRASE_SEG_MIN_CHARS = 25
PHRASE_ADJ_MIN_CHARS = 80     # adjacent identical segment must be >= this long
PHRASE_DOMINANT_FRAC = 0.40   # repeated-segment coverage that flags on its own

_PHRASE_STRIP = re.compile(r"^[\s>#*\-|`•]+|[\s>#*\-|`•]+$")


def _phrase_segments(text):
    """Split *text* into non-empty sentence/line segments as (offset, raw)."""
    parts = []
    pos = 0
    s = str(text or "")
    for m in re.finditer(r"(?<=[.!?…])\s+|\n+", s + "\n"):
        seg = s[pos:m.start()]
        if seg.strip():
            parts.append((pos, seg))
        pos = m.end()
    return parts


def _phrase_norm(seg):
    """Normalize a segment for repetition comparison: strip leading/trailing
    markdown, casefold, collapse whitespace."""
    x = _PHRASE_STRIP.sub("", str(seg or "").strip())
    x = _PHRASE_STRIP.sub("", x)
    return re.sub(r"\s+", " ", x.casefold()).strip()


def detect_phrase_repetition(text):
    """(degenerate, offset, reason) — pure, never raises. Catches a normalized
    sentence/line repeated >= 2x even in short (200-500 char) finals: adjacent
    identical long segments, or a segment whose repeats dominate the reply. See
    the PHRASE_* tuning notes above. ``offset`` is where the repeat begins (the
    truncation point); -1 when clean."""
    try:
        s = str(text or "")
        if len(s) < 40:
            return False, -1, ""
        qual = []  # (offset, normalized) for segments long enough to matter
        for off, raw in _phrase_segments(s):
            n = _phrase_norm(raw)
            if len(n.split()) >= PHRASE_SEG_MIN_WORDS and len(n) >= PHRASE_SEG_MIN_CHARS:
                qual.append((off, n))
        # adjacent-repeat: two consecutive qualifying segments identical + long
        for a in range(len(qual) - 1):
            if qual[a][1] == qual[a + 1][1] and len(qual[a][1]) >= PHRASE_ADJ_MIN_CHARS:
                return True, qual[a + 1][0], "phrase-adjacent"
        # dominant-repeat: repeated coverage >= PHRASE_DOMINANT_FRAC of the text
        seen = {}
        rep_chars = 0
        first_rep_off = -1
        for off, n in qual:
            if n in seen:
                rep_chars += len(n)
                if first_rep_off < 0:
                    first_rep_off = off
            seen[n] = off
        if rep_chars / max(1, len(s)) >= PHRASE_DOMINANT_FRAC:
            return True, first_rep_off, "phrase-dominant"
        return False, -1, ""
    except Exception:
        return False, -1, ""


# v1.7.1 (FIX 3 — trailing/embedded intent-announcement over a stub). The
# Silverton garbage finals (ids 5823/5825/5827/5829/5831/5839) all ENDED with a
# bare intent announcement ("Let me do a focused searches to give you real
# data." / "Let me try again with a more careful approach…") sitting above or
# beside a stub: a malformed markdown table (header + separator, no well-formed
# data row) and/or a repeated fabricated fragment. v1.6.1's _intent_only passed
# them because the stub table markup counted as > doomed_min_answer chars of
# "content". This check treats table scaffolding + a repetition-degenerate body
# as non-answer: a short final that ends with (or is dominated by) a
# mostly-intent announcement AND whose real remainder is thin, a stub table, or
# itself degenerate is flagged. A complete answer that merely offers follow-up
# ("Let me know if you want more") passes — _INTENT_RE excludes "let me know",
# and a long substantive body keeps its content (validated against 146 clean
# finals: the only hit was itself a genuine intent-only stub).
TRAILING_INTENT_MAX_CHARS = 700   # only short finals can be intent-dominated stubs
TRAILING_INTENT_SEG_MAX = 120     # a "mostly-intent" segment is this short
TRAILING_INTENT_ANSWER_MIN = 60   # real prose below this = intent-dominated stub

_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}")


def _is_table_sep(line):
    return bool(_TABLE_SEP_RE.match(line)) and set(line.strip()) <= set("|:- ")


def _is_well_formed_row(line):
    l = line.strip()
    return (l.startswith("|") and l.endswith("|") and l.count("|") >= 3
            and not _is_table_sep(l))


def _has_stub_table(vis):
    """True when *vis* contains a markdown table separator but no well-formed
    DATA row after it (a table header + separator with no real rows — the model
    opened a table and abandoned it). The header row precedes the separator, so
    only rows AFTER a separator count as data."""
    try:
        lines = [l for l in str(vis or "").splitlines() if l.strip()]
        seps = [i for i, l in enumerate(lines) if _is_table_sep(l)]
        if not seps:
            return False
        data = 0
        for si in seps:
            for l in lines[si + 1:]:
                if _is_table_sep(l):
                    break
                if l.strip().startswith("|"):
                    if _is_well_formed_row(l):
                        data += 1
                else:
                    break
        return data < 1
    except Exception:
        return False


def _strip_scaffolding(seg):
    """A segment's prose with table markup / heading punctuation removed."""
    s = seg.strip()
    if s.startswith("|") or _is_table_sep(s):
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[*#`|>]+", " ", s)).strip()


def _seg_is_mostly_intent(seg):
    return bool(_INTENT_RE.search(seg)) and len(seg) <= TRAILING_INTENT_SEG_MAX


def trailing_intent_stub(vis, min_chars):
    """FIX 3: True when a short final ends with (or is dominated by) a bare
    intent announcement and the non-announcement remainder is not a real answer
    (thin prose, a stub table, or itself repetition-degenerate). Pure; never
    raises."""
    try:
        s = str(vis or "")
        if not s or len(s) > TRAILING_INTENT_MAX_CHARS:
            return False
        segs = [raw.strip() for _off, raw in _phrase_segments(s)]
        if not segs:
            return False
        flags = [_seg_is_mostly_intent(seg) for seg in segs]
        if not any(flags):
            return False
        # require the intent to be TRAILING or REPEATED (>=2 announcements) —
        # a single intent phrase mid-answer is fine.
        if not (flags[-1] or sum(flags) >= 2):
            return False
        residual = [seg for seg, f in zip(segs, flags) if not f]
        prose = re.sub(r"\s+", " ",
                       " ".join(_strip_scaffolding(seg) for seg in residual)).strip()
        if len(prose) < TRAILING_INTENT_ANSWER_MIN:
            return True
        if _has_stub_table(s):
            return True
        if detect_phrase_repetition(" ".join(residual))[0]:
            return True
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# v1.7.3 (corpus batch 3): TOKEN-CORRUPTION / MUTATING-FRAGMENT detector.
#
# Batch 3 surfaced a NEW degeneracy class the v1.7.2 guards miss: the model's
# output is neither empty, short, repeated, nor an intent-announcement — it is
# COHERENT PROSE with individual tokens dropped/doubled/truncated mid-stream
# (suspected vLLM ngram spec-decode / fp8 on the upgraded build). Real shipped
# fails: cl05 restated a hash truncated ("8cfde6efdfc4" then "8cfde6efdfc"),
# cl06 collapsed to a bare "2!" stub, ms01 mutated the same number three ways
# ("207,879" -> "207,89" -> "207,87") amid unbalanced "**". These slip every
# repetition/intent signal (no long tandem, no announcement).
#
# This is a DEFENSIVE guard: the ROOT cause is server-side spec-decode/fp8 and
# only a server-config rollback restores correctness. The guard converts
# corrupted output into an honest failure instead of shipping garbage.
#
# FOUR signals, each tuned to ZERO false positives on the real clean controls
# (12 real clean batch-3 finals + 12 adversarial: legit factorial, code with
# `2**10`, hash+12-char-prefix, bold tables, big numbers, prices). See
# tests/data/corruption_fixtures.json. Any signal that can't hit zero-FP is
# dropped; legitimate math, code, tables and prose with ** must never trip.
#   trailing_stub  final line is a bare "N!" / lone operator fragment (cl06)
#   prefix_trunc   a long token restated as a near-prefix of itself — small
#                  length delta, so a hash's legit 12-of-64 prefix is safe (cl05, ms01)
#   embedded_emph  "**" wedged BETWEEN two alnum chars outside code — a bold
#                  toggle splitting a token, never legit markdown (cl04)
#   line_odd_emph  a single line (outside code) has an ODD "**" count — an
#                  unclosed emphasis, i.e. a dropped/mutated delimiter (ms01/ms02/ms06/ae02)
#
# v1.8.1 (batch-recovery on vLLM 0.25.1) adds THREE more for the Silverton
# "class B" flavor that slipped even these — all still zero-FP on the 24
# authoritative controls:
#   doubled_phrase a 2-6 word shingle repeated back-to-back on one line with no
#                  sentence punctuation between the copies ("Provincial Park
#                  Provincial Park") — same-line + word-length guards keep
#                  heading->body repeats and "New York, New York" safe
#   dropped_letter a capitalized token appearing BOTH correctly and as a
#                  one-INTERIOR-letter-dropped near-miss ("Silverton"/"Silveron")
#   table_collapse a table that opens well-formed then a data row merges to a
#                  single cell, or (after a good row) drops its leading pipe
# ---------------------------------------------------------------------------

CORRUPTION_PREFIX_NUM_MINDIGITS = 5   # a numeric token needs >= this many digits
CORRUPTION_PREFIX_NUM_MAXDELTA = 2    # near-prefix: shorter within this many chars
CORRUPTION_PREFIX_HEX_MINLEN = 8      # a hex/id token needs >= this many chars
CORRUPTION_PREFIX_HEX_MAXDELTA = 3

_CORRUPT_CODE_FENCE = re.compile(r"```.*?```", re.S)
_CORRUPT_CODE_SPAN = re.compile(r"`[^`]*`")
_CORRUPT_TRAILSTUB_RE = re.compile(r"^(?:\d{1,4}!|[=+*/])$")
_CORRUPT_TRAILFRAG_RE = re.compile(r"^[-=]\s*[=\-]?\s*$")
_CORRUPT_EMBED_EMPH_RE = re.compile(r"[A-Za-z0-9]\*\*[A-Za-z0-9]")
_CORRUPT_NUM_RE = re.compile(r"\d[\d,]{3,}\d")
_CORRUPT_HEX_RE = re.compile(r"[0-9a-fA-F]{8,}")
_CORRUPT_STRIP_RE = re.compile(r"^[\s>#*\-`|.]+|[\s`*]+$")


def _corrupt_strip_code(s):
    """Remove fenced blocks and inline code spans (their content — power
    operators `2**2`, hashes, paths — must never feed the markdown/emphasis
    signals)."""
    return _CORRUPT_CODE_SPAN.sub(" ", _CORRUPT_CODE_FENCE.sub(" ", s))


def _corrupt_trailing_stub(s):
    """Final non-empty line is a bare degenerate fragment (cl06's '2!')."""
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    if not lines:
        return False
    last = lines[-1]
    core = _CORRUPT_STRIP_RE.sub("", last).strip()
    return bool(_CORRUPT_TRAILSTUB_RE.match(core)
                or _CORRUPT_TRAILFRAG_RE.match(last))


def _corrupt_prefix_trunc(s):
    """A significant token restated as a near-prefix of itself (a dropped
    trailing char) — the mutating-fragment signature. Numbers keep their
    commas; the small max-delta keeps a hash's legitimate 12-of-64 prefix
    (delta ~52) from tripping while catching '207,87' inside '207,879'."""
    for rx, minlen, maxdelta, digits_only in (
            (_CORRUPT_NUM_RE, CORRUPTION_PREFIX_NUM_MINDIGITS,
             CORRUPTION_PREFIX_NUM_MAXDELTA, True),
            (_CORRUPT_HEX_RE, CORRUPTION_PREFIX_HEX_MINLEN,
             CORRUPTION_PREFIX_HEX_MAXDELTA, False)):
        toks = []
        for m in rx.finditer(s):
            tok = m.group(0)
            sig = sum(c.isdigit() for c in tok) if digits_only else len(tok)
            if sig >= minlen:
                toks.append(tok)
        uniq = list(dict.fromkeys(toks))
        for a in uniq:
            for b in uniq:
                if a != b and len(a) < len(b) and b.startswith(a) \
                        and (len(b) - len(a)) <= maxdelta:
                    return True
    return False


def _corrupt_embedded_emph(s):
    """A '**' wedged between two alphanumerics (outside code) — a bold toggle
    splitting a token ('v0.18.2**18.2', 'build**2'). Never legit markdown."""
    return bool(_CORRUPT_EMBED_EMPH_RE.search(_corrupt_strip_code(s)))


def _corrupt_line_odd_emph(s):
    """A single content line (outside code) carries an ODD number of '**' —
    an unclosed emphasis, i.e. a dropped/mutated delimiter. Markdown bold
    never spans lines, so a per-line odd count is malformed."""
    for line in _corrupt_strip_code(s).splitlines():
        if line.count("**") % 2 == 1 and any(c.isalnum() for c in line):
            return True
    return False


# ---------------------------------------------------------------------------
# v1.8.1 (batch-recovery, vLLM 0.25.1): three MORE zero-false-positive signals
# for the "class B" corruption that shipped past the v1.7.3 guard (real sample:
# the Silverton itinerary gateway final — dropped-letter proper nouns, doubled
# adjacent phrases, a mid-table pipe/column collapse, all inside otherwise-fine
# prose). Tuned against that real garbage + the silverton/repetition captures
# AND the 24 authoritative clean controls (0 FP). Still DEFENSIVE ONLY.
# ---------------------------------------------------------------------------

CORRUPT_DBL_L2_MINWORD = 5   # a 2-word doubling needs one word >= this long
CORRUPT_NOUN_MINLEN = 8      # a proper-noun near-miss needs the longer token this long

_CORRUPT_WORD_RE = re.compile(r"\S+")
_CORRUPT_SENT_PUNCT = re.compile(r"[.,!?;:]")
_CORRUPT_DBL_EDGE = "*_.,;:()\"'|~#>—–-… "
_CORRUPT_CAP_RE = re.compile(r"\b[A-Z][A-Za-z]{5,}\b")


def _corrupt_doubled_phrase(s):
    """A 2-6 word shingle repeated immediately back-to-back on one line with no
    sentence punctuation between the copies ('Provincial Park Provincial Park',
    'important changes important changes') — a doubled-token corruption. The
    same-line + no-sentence-punctuation guards, and requiring a 2-word shingle
    to carry a >=5-char word (a 3-6 word shingle a content word), keep legit
    heading->body repeats ('...Hermes Agent\\n\\nHermes Agent...'), 'New York,
    New York', and function-word stutters ('I had had enough') from tripping."""
    src = _corrupt_strip_code(s)
    toks = [(m.start(), m.end()) for m in _CORRUPT_WORD_RE.finditer(src)]
    words = [src[a:b].strip(_CORRUPT_DBL_EDGE).casefold() for a, b in toks]
    n = len(words)
    for L in range(2, 7):
        i = 0
        while i + 2 * L <= n:
            a = words[i:i + L]
            if a == words[i + L:i + 2 * L] and all(a):
                sig = (any(len(w) >= CORRUPT_DBL_L2_MINWORD for w in a) if L == 2
                       else any(sum(c.isalnum() for c in w) >= 3 for w in a))
                span = src[toks[i][0]:toks[i + 2 * L - 1][1]]
                if sig and "\n" not in span and not _CORRUPT_SENT_PUNCT.search(span):
                    return True
            i += 1
    return False


def _corrupt_edit1_interior(short, lng):
    """True when *lng* with exactly ONE interior character removed equals
    *short* (a single mid-word dropped letter). Interior-only (never the first
    or last char) excludes plurals/inflections (State/States, Silver/Silvery)
    and trailing truncations — those are a separate, less-separable class."""
    if len(lng) - len(short) != 1:
        return False
    for k in range(1, len(lng) - 1):
        if lng[:k] + lng[k + 1:] == short:
            return True
    return False


# US/UK (and other) spelling variants that differ by exactly one interior
# letter — BOTH are real words, so their co-occurrence is legitimate, NOT
# corruption. Fixed a live false positive where 'Specialty'/'Speciality'
# replaced a correct 3766-char answer with a corruption stub. Lowercased pairs.
_DROPPED_LETTER_VARIANTS = frozenset({
    frozenset(("specialty", "speciality")),
    frozenset(("judgment", "judgement")),
    frozenset(("acknowledgment", "acknowledgement")),
    frozenset(("abridgment", "abridgement")),
    frozenset(("lodgment", "lodgement")),
})


def _dropped_is_dedouble(short, lng):
    """True if the single interior char *lng* has and *short* lacks is a
    de-doubled consonant (traveller->traveler, enrollment->enrolment,
    fulfillment->fulfilment) — the large US/UK double-consonant variant class,
    legitimate spelling, not corruption."""
    if len(lng) - len(short) != 1:
        return False
    for k in range(1, len(lng) - 1):
        if lng[:k] + lng[k + 1:] == short:
            ch = lng[k]
            return ch == lng[k - 1] or (k + 1 < len(lng) and ch == lng[k + 1])
    return False


def _corrupt_dropped_letter_noun(s):
    """A capitalized proper-noun-ish token that appears BOTH correctly and as a
    one-interior-letter-dropped near-miss of itself in the same text
    ('Silverton' and 'Silveron' co-occur) — a strong dropped-token corruption
    signal. The longer token must be >= CORRUPT_NOUN_MINLEN so short-name
    coincidences and morphological pairs can't trip it. Legitimate US/UK
    spelling variants (de-doubled consonants + a curated non-doubling set) are
    excluded — both forms are real words, so their co-occurrence is not
    corruption."""
    src = _corrupt_strip_code(s)
    caps = list(dict.fromkeys(_CORRUPT_CAP_RE.findall(src)))
    for a in caps:
        for b in caps:
            if a == b or len(b) < CORRUPT_NOUN_MINLEN:
                continue
            if len(a) < len(b) and _corrupt_edit1_interior(a, b):
                if frozenset((a.lower(), b.lower())) in _DROPPED_LETTER_VARIANTS:
                    continue
                if _dropped_is_dedouble(a, b):
                    continue
                return True
    return False


def _corrupt_table_cells(line):
    """Content cells of a markdown row (outer pipes stripped)."""
    l = line.strip()
    if l.startswith("|"):
        l = l[1:]
    if l.endswith("|"):
        l = l[:-1]
    return l.split("|")


def _corrupt_table_collapse(s):
    """A markdown table that opens well-formed (a header row starting with '|'
    plus a separator) then a data row COLLAPSES: it merges into a single cell
    while the header had >= 2 columns (the stub-table signature — the Silverton
    '| **Drive from Mission: ...' rows), or, AFTER at least one well-formed row,
    a later row drops its leading pipe with >= 2 pipes still present (an
    inconsistent structural break). Uniform leading-pipe omission (a valid GFM
    style) and a mismatched/legit wide header never trip this."""
    lines = s.splitlines()
    n = len(lines)
    i = 0
    while i < n:
        l = lines[i].strip()
        if (i > 0 and _is_table_sep(l)
                and lines[i - 1].strip().startswith("|")):
            ncol = len(_corrupt_table_cells(lines[i - 1].strip()))
            saw_good = False
            j = i + 1
            while j < n:
                row = lines[j].strip()
                if not row:
                    break
                if _is_table_sep(row):
                    j += 1
                    continue
                if "|" not in row:
                    break   # table ended cleanly into prose
                lead = row.startswith("|")
                nc = len(_corrupt_table_cells(row))
                if lead and ncol >= 2 and nc == 1:
                    return True
                if saw_good and not lead and row.count("|") >= 2:
                    return True
                if lead and row.endswith("|") and nc == ncol:
                    saw_good = True
                j += 1
            i = j
        else:
            i += 1
    return False


# ---------------------------------------------------------------------------
# v1.8.3: EMOJI / SYMBOL-SPEW collapse detector.
#
# A real Silverton run collapsed into an emoji spew ("🔋🔋💤💥…✅✅💯💯⚡😍") that
# every prior signal missed: it is short, has no dropped-token / doubled-phrase
# / repetition signature, and finish_reason=stop. This pure detector flags a
# DENSE run of pictographic emoji (or an emoji-dominated tail / line) while
# staying ZERO-false-positive on answers that use LEGITIMATE occasional emoji:
# a single ✅ in a list, a lone 🎉, a per-row checkmark comparison table, plus
# math symbols (× ÷ √ π), currency ($ € £ ¥), and diagram arrows (→ ← ↑ ↓) —
# NONE of which are classified as emoji here (arrow / math / currency Unicode
# blocks are deliberately excluded), so they can never contribute to a spew.
#
# Signals (any one flags):
#   emoji-cluster  a maximal cluster of >= EMOJI_CLUSTER_MIN emoji where
#                  consecutive emoji are separated by <= EMOJI_CLUSTER_GAP
#                  non-emoji chars (spaces, VS16, a stray '…' all stay in the
#                  cluster) — the real sample is one 10-emoji cluster.
#   emoji-tail     the trailing EMOJI_TAIL_WINDOW chars are >= EMOJI_TAIL_FRAC
#                  emoji by non-space char with >= EMOJI_TAIL_MIN emoji (an
#                  answer that decays into emoji at the very end).
#   emoji-line     a single line carries >= EMOJI_LINE_MIN emoji at >=
#                  EMOJI_LINE_FRAC of its non-space chars, and is not a markdown
#                  table row (>= 3 pipes) — a mid-answer emoji-salad line.
# ---------------------------------------------------------------------------

EMOJI_CLUSTER_MIN = 6     # >= this many emoji in one dense cluster flags
EMOJI_CLUSTER_GAP = 2     # max non-emoji chars between consecutive cluster emoji
EMOJI_TAIL_WINDOW = 24    # trailing chars considered for the emoji-dominated tail
EMOJI_TAIL_MIN = 5        # >= this many emoji in the tail window ...
EMOJI_TAIL_FRAC = 0.5     # ... covering >= this fraction of its non-space chars
EMOJI_LINE_MIN = 8        # >= this many emoji on one non-table line ...
EMOJI_LINE_FRAC = 0.5     # ... at >= this fraction of its non-space chars

# Pictographic-emoji codepoint ranges. Deliberately EXCLUDES the Arrows block
# (U+2190–U+21FF), Misc-Symbols-and-Arrows (U+2B00–U+2BFF), and all math /
# currency / Latin punctuation so diagram arrows, ×÷√π, and $€£¥ are never
# emoji. Covers the common pictographic planes the model actually spews.
_EMOJI_RANGES = (
    (0x1F300, 0x1F5FF),   # Misc Symbols and Pictographs (🔋 💤 💥 💯 …)
    (0x1F600, 0x1F64F),   # Emoticons (😍 …)
    (0x1F680, 0x1F6FF),   # Transport and Map
    (0x1F900, 0x1F9FF),   # Supplemental Symbols and Pictographs
    (0x1FA70, 0x1FAFF),   # Symbols and Pictographs Extended-A
    (0x1F1E6, 0x1F1FF),   # Regional indicator (flags)
    (0x2600, 0x26FF),     # Misc symbols (⚡ ⚠ ☀ ♻ …)
    (0x2700, 0x27BF),     # Dingbats (✅ ✨ ✔ ❌ …)
)
# Emoji continuation chars: ZWJ, variation selectors, skin-tone modifiers.
# They never count as a base emoji but must not break a cluster/ratio.
_EMOJI_MOD = frozenset(
    {0x200D, 0xFE0E, 0xFE0F} | set(range(0x1F3FB, 0x1F400)))


def _is_emoji_cp(cp):
    for lo, hi in _EMOJI_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def _emoji_positions(s):
    """Indices of base (non-modifier) pictographic-emoji chars in *s*."""
    return [i for i, ch in enumerate(s) if _is_emoji_cp(ord(ch))]


def _emoji_cluster_max(positions):
    """Largest count of emoji in a run where consecutive emoji are within
    EMOJI_CLUSTER_GAP non-emoji chars of each other."""
    if not positions:
        return 0
    best = run = 1
    for k in range(1, len(positions)):
        if positions[k] - positions[k - 1] - 1 <= EMOJI_CLUSTER_GAP:
            run += 1
        else:
            run = 1
        if run > best:
            best = run
    return best


def _emoji_tail(s, positions):
    """The trailing window is emoji-dominated (a decay-into-emoji tail)."""
    t = s.rstrip()
    if not t:
        return False
    start = max(0, len(t) - EMOJI_TAIL_WINDOW)
    emo = sum(1 for i in positions if start <= i < len(t))
    if emo < EMOJI_TAIL_MIN:
        return False
    nonspace = sum(1 for ch in t[start:]
                   if not ch.isspace() and ord(ch) not in _EMOJI_MOD)
    return nonspace > 0 and emo / nonspace >= EMOJI_TAIL_FRAC


def _emoji_line_spew(s):
    """A single non-table line is an emoji salad."""
    for line in s.splitlines():
        pos = _emoji_positions(line)
        if len(pos) < EMOJI_LINE_MIN or line.count("|") >= 3:
            continue
        nonspace = sum(1 for ch in line
                       if not ch.isspace() and ord(ch) not in _EMOJI_MOD)
        if nonspace > 0 and len(pos) / nonspace >= EMOJI_LINE_FRAC:
            return True
    return False


def _corrupt_emoji_spew(s):
    """True when *s* contains a degenerate emoji/symbol spew. Pure; the emoji
    thresholds are tuned so a single ✅/🎉 or a per-row checkmark table never
    trips, while a >=6-emoji cluster / emoji-dominated tail / emoji-salad line
    does. Runs even on short finals (the real sample was 11 chars)."""
    positions = _emoji_positions(s)
    if len(positions) < EMOJI_TAIL_MIN:
        return False
    return (_emoji_cluster_max(positions) >= EMOJI_CLUSTER_MIN
            or _emoji_tail(s, positions)
            or _emoji_line_spew(s))


def detect_output_corruption(text):
    """(corrupt, reason) — pure, never raises. Flags token-corruption /
    mutating-fragment finals (dropped/doubled/truncated tokens, unbalanced
    emphasis, bare trailing stubs, emoji/symbol spew) that the repetition +
    intent guards miss.

    Conservative by design: every signal is tuned to ZERO false positives on
    the real clean controls (tests/data/corruption_fixtures.json). Returns the
    first-firing signal's name as the reason; '' when clean."""
    try:
        s = str(text or "")
        # Emoji-spew runs FIRST and ignores the length floor below: the real
        # sample was 11 chars, and its own >=5-emoji gate keeps short clean
        # strings ("Done ✅") from ever reaching a flag.
        if _corrupt_emoji_spew(s):
            return True, "emoji-spew"
        if len(s) < 12:
            return False, ""
        for name, fn in (("trailing-stub", _corrupt_trailing_stub),
                         ("prefix-truncation", _corrupt_prefix_trunc),
                         ("embedded-emphasis", _corrupt_embedded_emph),
                         ("unclosed-emphasis", _corrupt_line_odd_emph),
                         # v1.8.1 class-B signals
                         ("doubled-phrase", _corrupt_doubled_phrase),
                         ("dropped-letter", _corrupt_dropped_letter_noun),
                         ("table-collapse", _corrupt_table_collapse)):
            if fn(s):
                return True, name
        return False, ""
    except Exception:
        return False, ""


_CORRUPT_SIGNALS = (
    ("trailing-stub", _corrupt_trailing_stub),
    ("prefix-truncation", _corrupt_prefix_trunc),
    ("embedded-emphasis", _corrupt_embedded_emph),
    ("unclosed-emphasis", _corrupt_line_odd_emph),
    ("doubled-phrase", _corrupt_doubled_phrase),
    ("dropped-letter", _corrupt_dropped_letter_noun),
    ("table-collapse", _corrupt_table_collapse),
)


def detect_output_corruption_signals(text):
    """ALL firing corruption sub-signal names as a list — pure, never raises.

    v1.16.0 (W1 corroboration): the WITHHOLD/REPLACE decision requires the
    signal SET, not just the first hit. A full replace is authorized only when
    >= 2 sub-signals fire (or 1 sub-signal + a repetition/degeneracy signal);
    a lone signal delivers the answer with an inline caveat rather than
    withholding — retiring the batch4-r09 single-suspicion false-withhold on
    ALL tiers. Emoji-spew is included and (like the detector) ignores the
    12-char length floor."""
    out = []
    try:
        s = str(text or "")
        if _corrupt_emoji_spew(s):
            out.append("emoji-spew")
        if len(s) >= 12:
            for name, fn in _CORRUPT_SIGNALS:
                try:
                    if fn(s):
                        out.append(name)
                except Exception:
                    continue
    except Exception:
        return []
    return out


REPETITION_TRUNCATE_NOTE = (
    "\n\n_[The rest of this reply was removed: the model started repeating "
    "itself and collapsed into degenerate text. Ask me to continue if the "
    "answer above is cut off.]_")

REPETITION_REPLACEMENT = (
    "⚠️ I started answering but the response collapsed into repeated/garbled "
    "text, so I withheld it instead of sending noise. Please re-ask — a "
    "narrower question or a request for a shorter answer usually avoids this.")

INTENT_GUARD_REPLACEMENT = (
    "I got ready to work on this but didn't actually produce the answer yet. "
    "Could you re-send the request? I'll answer it directly this time.")

CORRUPTION_REPLACEMENT = (
    "⚠️ My answer came out with corrupted/garbled tokens (dropped or mutated "
    "digits and characters), so I withheld it rather than send something that "
    "might be subtly wrong. Please re-ask — this is a transient generation "
    "glitch and retrying usually produces a clean answer.")

# v1.16.0 (W1): a lone (uncorroborated) corruption signal is delivered WITH
# this inline caveat instead of withheld — ADD-only, so a correct answer is
# never removed on a single suspicion (the batch4-r09 lesson).
CORRUPTION_CAVEAT = (
    "\n\n_[Note: a token or two above may have come out garbled (a transient "
    "generation glitch) — if a number, name, or code looks off, ask me to "
    "repeat that part.]_")

# v1.11.2: shown when a corruption glitch is auto-retried (bounded). %d = this
# attempt, %d = the cap.
CORRUPT_RETRY_MSG = (
    "⚠️ That answer came out garbled — retrying automatically (attempt %d of "
    "%d). The clean answer will arrive in a separate message shortly. (Send a "
    "new message anytime to interrupt.)")


def _is_formatting_unit(unit):
    """A tandem unit with no alphanumeric char is benign layout (table rules
    `----`/`|---|`, `====`, dot leaders, blank runs) — never flagged."""
    return not any(c.isalnum() for c in unit)


def _char_tandem_offset(s, min_span, min_copies, max_period):
    """Earliest start offset of a qualifying char-level tandem repeat, or -1.

    O(n * max_period): for each period walk once, skipping past each matched
    run. Returns the smallest start index across all periods so truncation
    happens at the point collapse begins."""
    n = len(s)
    best = -1
    hi = min(int(max_period), n // 2)
    for p in range(1, hi + 1):
        i = 0
        limit = n - p
        while i < limit:
            if s[i] != s[i + p]:
                i += 1
                continue
            j = i
            while j < limit and s[j] == s[j + p]:
                j += 1
            span = (j - i) + p          # chars covered by the tandem block
            if span >= min_span and span >= min_copies * p:
                if not _is_formatting_unit(s[i:i + p]):
                    if best == -1 or i < best:
                        best = i
                    break               # earliest for this period found
            i = j + 1
    return best


_WORD_RE = re.compile(r"\S+")
_RUNON_SPLIT = re.compile(r"[.!?\n:;]|…")


def _word_shingle_offset(s, min_span, min_copies, max_shingle):
    """Earliest char offset of a consecutive multi-word phrase loop, or -1.

    Catches phrase repeats whose char period exceeds the tandem scan window
    (e.g. a whole sentence repeated). A qualifying loop repeats an identical
    (casefolded) L-word shingle >= min_copies times back-to-back, spanning
    >= min_span chars, and the shingle must contain a real content token (a
    3+ alnum-char word) so repeated table punctuation never trips it."""
    toks = [(m.start(), m.end()) for m in _WORD_RE.finditer(s)]
    words = [s[a:b].casefold() for a, b in toks]
    n = len(words)
    best = -1
    for L in range(1, int(max_shingle) + 1):
        i = 0
        while i + L * min_copies <= n:
            shingle = words[i:i + L]
            copies = 1
            j = i + L
            while j + L <= n and words[j:j + L] == shingle:
                copies += 1
                j += L
            if copies >= min_copies:
                span = toks[j - 1][1] - toks[i][0]
                has_content = any(sum(c.isalnum() for c in w) >= 3
                                  for w in shingle)
                if span >= min_span and has_content:
                    if best == -1 or toks[i][0] < best:
                        best = toks[i][0]
                    break
                i = j          # skip the whole run we just measured
            else:
                i += 1
    return best


def _low_distinct_offset(s, win=REPETITION_DISTINCT_WIN,
                         max_distinct=REPETITION_DISTINCT_MAX, step=40):
    """Earliest start of a mostly-alnum window with <= max_distinct distinct
    alnum chars, or -1. Catches fragmented character garbage ("aaaa bbbb
    dada", "tetesese", "<ctrl46>/docs</ctrl47>" walls) whose period is
    irregular. The >=50%-alnum gate excludes pure layout runs (dash rules,
    `| --- |`) which have few distinct chars but are legitimate."""
    n = len(s)
    if n < win:
        win = n
    best = -1
    for i in range(0, max(1, n - win + 1), step):
        w = s[i:i + win]
        alnum = [c.lower() for c in w if c.isalnum()]
        if len(alnum) < 0.5 * len(w):
            continue
        if len(set(alnum)) <= max_distinct:
            best = i
            break
    return best


def _runon_offset(s, min_words=REPETITION_RUNON_WORDS):
    """Earliest char offset of an unpunctuated run-on segment of >= min_words
    words, or -1. Catches the 'coherent-looking jargon that never stops'
    collapse (batch-2 r39: a 599-word sentence) that repetition-of-units
    detectors miss because the words are individually distinct."""
    pos = 0
    for m in _RUNON_SPLIT.finditer(s + "."):
        seg = s[pos:m.start()]
        if len(seg.split()) >= min_words:
            return pos + (len(seg) - len(seg.lstrip()))
        pos = m.end()
    return -1


def detect_degenerate_repetition(text, min_span=REPETITION_MIN_SPAN,
                                 min_copies=REPETITION_MIN_COPIES,
                                 max_period=REPETITION_MAX_PERIOD,
                                 max_shingle=REPETITION_SHINGLE_MAX):
    """(degenerate, offset, reason) — pure, never raises.

    Fires when ANY of four independent signals trip; ``offset`` is the
    EARLIEST char index a degenerate region begins (the truncation point),
    -1 when clean. Signals (see module docstring + tuning fixtures):
      char-tandem   exact short-period run ("Allen Newell Herbert Simon"…)
      phrase-loop   repeated multi-word shingle
      char-garbage  low-diversity window (fragmented token soup)
      run-on        an unpunctuated >= 150-word wall of jargon
    """
    try:
        s = str(text or "")
        if len(s) < min_span:
            return False, -1, ""
        # v1.7.1 (FIX 2): phrase/sentence-level 2x repetition (adjacent long
        # line or dominant repeat) — catches the Silverton 363-char garbage the
        # >=4-copy / <=48-char-period signals miss.
        _pdeg, _poff, _preason = detect_phrase_repetition(s)
        signals = [
            ("char-tandem",
             _char_tandem_offset(s, min_span, min_copies, max_period)),
            ("phrase-loop",
             _word_shingle_offset(s, REPETITION_SHINGLE_SPAN,
                                  REPETITION_SHINGLE_COPIES, max_shingle)),
            ("char-garbage", _low_distinct_offset(s)),
            ("run-on", _runon_offset(s)),
            (_preason or "phrase-repeat", _poff if _pdeg else -1),
        ]
        fired = [(name, off) for name, off in signals if off >= 0]
        if not fired:
            return False, -1, ""
        name, off = min(fired, key=lambda t: t[1])
        return True, off, name
    except Exception:
        return False, -1, ""


def salvage_repetition(text, offset):
    """Truncate *text* at the last clean boundary before *offset* and append a
    short honest note. Returns None when the salvageable prefix is too short
    to be worth keeping (caller substitutes the full replacement)."""
    try:
        s = str(text or "")
        cut = max(0, int(offset))
        window = s[:cut]
        for sep in ("\n\n", "\n", ". ", "! ", "? ", "… ", "; "):
            idx = window.rfind(sep)
            if idx >= REPETITION_SALVAGE_MIN:
                cut = idx + len(sep)
                break
        prefix = s[:cut].rstrip()
        if len(prefix) < REPETITION_SALVAGE_MIN:
            return None
        return prefix + REPETITION_TRUNCATE_NOTE
    except Exception:
        return None


def _repetition_signal_count(text):
    """How many INDEPENDENT repetition sub-signals fire (0..5). Used for W9
    corroboration of a FULL replace. Pure; never raises."""
    try:
        s = str(text or "")
        if len(s) < REPETITION_MIN_SPAN:
            return 0
        _pdeg, _, _ = detect_phrase_repetition(s)
        offs = [
            _char_tandem_offset(s, REPETITION_MIN_SPAN, REPETITION_MIN_COPIES,
                                REPETITION_MAX_PERIOD),
            _word_shingle_offset(s, REPETITION_SHINGLE_SPAN,
                                 REPETITION_SHINGLE_COPIES,
                                 REPETITION_SHINGLE_MAX),
            _low_distinct_offset(s),
            _runon_offset(s),
            (0 if _pdeg else -1),
        ]
        return sum(1 for o in offs if o >= 0)
    except Exception:
        return 0


def _short_prefix_salvage(text, offset, floor=60):
    """A clean prefix before *offset* at a lower floor than salvage_repetition,
    for the W9 deliver-with-caveat path (a lone repetition signal delivers the
    short clean prefix instead of withholding the whole answer). None when
    there is no prefix worth delivering (< floor chars). Pure; never raises."""
    try:
        s = str(text or "")
        cut = max(0, int(offset))
        window = s[:cut]
        for sep in ("\n\n", "\n", ". ", "! ", "? ", "… ", "; "):
            idx = window.rfind(sep)
            if idx >= floor:
                cut = idx + len(sep)
                break
        prefix = s[:cut].rstrip()
        if len(prefix) < floor:
            return None
        return prefix + REPETITION_TRUNCATE_NOTE
    except Exception:
        return None


def _repetition_guard_on():
    """tools.tool_search.repetition_guard ("on"/"off", default on). Per call.
    Gates the whole unconditional final-text degeneracy guard (repetition
    collapse + state-independent intent-announcement)."""
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        v = ((_load().get("tools") or {}).get("tool_search") or {}).get(
            "repetition_guard")
        if v is not None:
            if isinstance(v, bool):
                return v
            return str(v).strip().lower() == "on"
    except Exception:
        pass
    return True


def _corruption_guard_on():
    """tools.tool_search.corruption_guard ("on"/"off", default on). Per call.
    Gates the v1.7.3 token-corruption / mutating-fragment detector in both the
    unconditional final guard and classify_final."""
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        v = ((_load().get("tools") or {}).get("tool_search") or {}).get(
            "corruption_guard")
        if v is not None:
            if isinstance(v, bool):
                return v
            return str(v).strip().lower() == "on"
    except Exception:
        pass
    return True


def apply_final_guards(response_text, min_chars=40, session_id=""):
    """UNCONDITIONAL (state-independent) final-text degeneracy guard.

    Runs on HEALTHY turns too — the batch-2 finding was that repetition
    collapse (T1) and bare intent announcements (T3) both ship on turns that
    never arm the self-healer. Returns replacement text, or None to leave the
    answer unchanged. The repetition/intent rungs are gated by
    tools.tool_search.repetition_guard; the v1.7.3 corruption rung by
    tools.tool_search.corruption_guard (independent switches). Never raises
    (fail-safe covenant: any failure leaves the answer untouched)."""
    try:
        vis = strip_think(response_text)
        if not vis:
            return None
        if _repetition_guard_on():
            deg, offset, reason = detect_degenerate_repetition(vis)
            if deg:
                # Truncation/salvage is REVERSIBLE (keeps the clean prefix +
                # note) — always safe, keep it (W9).
                salvaged = salvage_repetition(vis, offset)
                if salvaged is not None:
                    logger.info("selfheal: repetition-collapse guard fired (%s "
                                "offset=%d/%d) session=%s action=truncate",
                                reason, offset, len(vis), session_id or "-")
                    return salvaged
                # FULL-REPLACE branch (prefix too small to salvage): v1.16.0 W9
                # corroboration — withhold the whole answer only when the
                # collapse is CORROBORATED (>= 2 repetition sub-signals, or a
                # repetition signal + a corruption signal). Otherwise deliver
                # the short clean prefix with a caveat rather than removing the
                # answer on a lone signal (the batch4-r09 discipline; the
                # detectors are 0-FP on 138 controls, so this is belt-and-braces).
                corroborated = (_repetition_signal_count(vis) >= 2
                                or len(detect_output_corruption_signals(vis)) >= 1)
                if corroborated:
                    logger.info("selfheal: repetition-collapse WITHHOLD "
                                "(corroborated: %s) session=%s action=replace",
                                reason, session_id or "-")
                    return REPETITION_REPLACEMENT
                short = _short_prefix_salvage(vis, offset)
                logger.info("selfheal: repetition-collapse lone signal (%s) — "
                            "%s session=%s", reason,
                            "deliver short prefix with caveat" if short
                            else "no usable prefix -> withhold", session_id or "-")
                return short if short is not None else REPETITION_REPLACEMENT
            if _intent_only(vis, min_chars):
                logger.info("selfheal: intent-announcement guard fired on a "
                            "non-armed turn session=%s", session_id or "-")
                return INTENT_GUARD_REPLACEMENT
            # v1.7.1 (FIX 3): trailing/embedded intent announcement over a stub.
            if trailing_intent_stub(vis, min_chars):
                logger.info("selfheal: trailing-intent-stub guard fired on a "
                            "non-armed turn session=%s", session_id or "-")
                return INTENT_GUARD_REPLACEMENT
        # v1.7.3 (corpus batch 3): token-corruption / mutating-fragment. This
        # is the CLI / no-gateway / retry-exhausted terminal path (on a gateway
        # the reversible auto-retry in _sh_transform runs first). Weak-tier only
        # (guard_active): frontier models don't token-corrupt and the guard's
        # false positives were a net loss there (batch4 r09).
        #
        # v1.16.0 (W1 corroboration): a full WITHHOLD/REPLACE is authorized only
        # when the corruption is CORROBORATED — >= 2 independent corruption
        # sub-signals, OR 1 sub-signal + a repetition/degeneracy signal. A LONE
        # signal is delivered WITH AN INLINE CAVEAT (ADD-only) instead of
        # withheld: a guard that deletes a correct answer on one suspicion is
        # worse than the failure it prevents (the batch4-r09 lesson), so the
        # single-signal false-withhold class is retired on ALL tiers.
        if _corruption_guard_on() and guard_active("corruption", session_id):
            sigs = detect_output_corruption_signals(vis)
            if sigs:
                corroborated = (len(sigs) >= 2
                                or detect_degenerate_repetition(vis)[0])
                if corroborated:
                    logger.info("selfheal: corruption WITHHOLD (corroborated: "
                                "%s) session=%s action=replace",
                                ",".join(sigs), session_id or "-")
                    return CORRUPTION_REPLACEMENT
                logger.info("selfheal: corruption single-signal (%s) — "
                            "DELIVER WITH CAVEAT (no withhold) session=%s",
                            sigs[0], session_id or "-")
                if CORRUPTION_CAVEAT.strip() in vis:
                    return None
                return vis + CORRUPTION_CAVEAT
        return None
    except Exception:
        logger.debug("selfheal: final guard failed; text unchanged",
                     exc_info=True)
        return None


_TIER_SCALE = {"weak": 1, "mid": 2, "strong": 3}


def tier_scale_cfg(cfg, tier):
    """v1.16.0 (W2): return a copy of *cfg* with the FAILING/WOBBLING sensor
    thresholds scaled for *tier*, plus ``t2_min_sensors`` (how many T2 sensors
    must fire to arm FAILING). weak -> x1, single sensor (byte-equivalent to
    v1.15.0); strong -> x3, needs >= 2 sensors (or one extreme). So the ladder
    is NOT hard-off on a strong model — a genuinely looping one is still caught,
    a well-behaved one never trips. Never raises."""
    try:
        scale = _TIER_SCALE.get(tier, 1)
        c = dict(cfg)
        if scale <= 1:
            c["t2_min_sensors"] = 1
            return c
        for k in ("fail_blocks", "fail_blank", "fail_wall_secs",
                  "search_hard_cap", "wobble_searches", "wobble_blocks",
                  "wobble_blank"):
            try:
                c[k] = int(cfg[k]) * scale
            except Exception:
                pass
        # budget fraction can't scale past 1.0 — raise it toward the ceiling so
        # only near-total budget exhaustion counts on a strong model.
        try:
            c["fail_budget_frac"] = min(0.95, float(cfg["fail_budget_frac"])
                                        + 0.15 * (scale - 1))
        except Exception:
            pass
        c["t2_min_sensors"] = 2
        return c
    except Exception:
        return dict(cfg)


def evaluate_state(state, sig, cfg):
    """§2.2 state machine: (new_state, trigger). Monotonic within a turn —
    never downgrades. *sig* keys: searches, blocks, blanks, sim (bool),
    api_calls, budget, wall, poison_at_birth (bool)."""
    rank = {"HEALTHY": 0, "WOBBLING": 1, "FAILING": 2}
    cur = rank.get(state, 0)
    if cur < 2:
        # v1.16.0 (W2 tier-scaling): the thresholds in *cfg* are already
        # tier-scaled by the caller (weak x1 = v1.15.0 values; strong x3), and
        # ``t2_min_sensors`` (default 1 = weak/v1.15.0) is how many sensors must
        # fire to enter FAILING. A single sensor at an EXTREME (>= 2x its scaled
        # threshold) always qualifies, so a genuinely-looping strong model is
        # still caught while a well-behaved one never trips.
        t2, extreme = [], False
        cap = int(cfg.get("search_hard_cap") or 0)
        if cap >= 1 and sig["searches"] > cap:
            t2.append("S1=%d>cap%d" % (sig["searches"], cap))
            if sig["searches"] > 2 * cap:
                extreme = True
        if sig["blocks"] >= cfg["fail_blocks"]:
            t2.append("S2=%d" % sig["blocks"])
            if sig["blocks"] >= 2 * cfg["fail_blocks"]:
                extreme = True
        if sig["blanks"] >= cfg["fail_blank"]:
            t2.append("S4=%d" % sig["blanks"])
            if sig["blanks"] >= 2 * cfg["fail_blank"]:
                extreme = True
        budget = sig.get("budget") or 0
        if budget > 0 and sig["api_calls"] >= cfg["fail_budget_frac"] * budget:
            t2.append("S6=%d/%d" % (sig["api_calls"], budget))
            if sig["api_calls"] >= budget:
                extreme = True
        if sig["wall"] > cfg["fail_wall_secs"]:
            t2.append("S7=%ds" % int(sig["wall"]))
            if sig["wall"] > 2 * cfg["fail_wall_secs"]:
                extreme = True
        t2_min = int(cfg.get("t2_min_sensors") or 1)
        if t2 and (len(t2) >= t2_min or extreme):
            return "FAILING", "+".join(t2)
    if cur < 1:
        t1 = []
        if sig["searches"] >= cfg["wobble_searches"]:
            t1.append("S1=%d" % sig["searches"])
        if sig["blocks"] >= cfg["wobble_blocks"]:
            t1.append("S2=%d" % sig["blocks"])
        if sig.get("sim"):
            t1.append("S3")
        if sig["blanks"] >= cfg["wobble_blank"]:
            t1.append("S4=%d" % sig["blanks"])
        if sig.get("poison_at_birth"):
            t1.append("S5")
        if t1:
            return "WOBBLING", "+".join(t1)
    return state, ""


# ---------------------------------------------------------------------------
# Message builders (pure)
# ---------------------------------------------------------------------------

def build_corrective(searches, blocks):
    """The FAILING corrective user message (constant per turn — built once
    and cached so the wire history stays stable across iterations).
    Variant C of the design experiments, generalized.

    v1.6.1 (corpus batch 1 runs 0/4): the old wording ("answer from your
    own knowledge") pushed the model into confident fabrication after
    failed/empty searches — invented vLLM changelogs, nonexistent Nous
    releases. The rewrite explicitly permits and prefers honest
    uncertainty and forbids inventing specifics."""
    return (
        CORRECTIVE_PREFIX + " — you have already made enough attempts "
        "(%d web searches, %d blocked calls this turn). Write your FINAL "
        "ANSWER to the user's question now, as plain text, in the user's "
        "language. Base it on what the tool results above ACTUALLY "
        "contained. If they were empty, failed, or irrelevant, say plainly "
        "that you could not find reliable information on this, then give "
        "your best answer from general knowledge and clearly label it as "
        "unverified. Honest uncertainty is a GOOD final answer here. Do NOT "
        "invent specifics the results did not show — no fabricated version "
        "numbers, release names, dates, statistics, quotes, or URLs. "
        "Do NOT emit any <tool_call> block and do NOT reply with '(empty)'."
        % (int(searches), int(blocks)))


SOFT_NUDGE_TEXT = (
    "NOTE from the session supervisor: you are repeating yourself. Stop "
    "searching — use the results you already have (web_extract one URL at "
    "most) and answer the user now.")

# v1.6.2 (corpus batch 2 T2): appended at WOBBLING+ as a system message. Short
# and advisory — it does NOT tell the model to stop using tools (that was the
# experiment-E stall), only to stop inventing specifics.
WOBBLE_HONESTY_NOTE = (
    "SYSTEM NOTE — accuracy check: do not invent specifics you are not sure "
    "of. No fabricated version numbers, release names, dates, statistics, "
    "news headlines, quotes, or URLs. If the tools returned nothing usable or "
    "you don't actually know, say so plainly and label uncertain claims as "
    "unverified. A short honest answer is better than a confident wrong one.")

TURN_START_NOTE = (
    "[session-health note] Earlier turns in this session looped on failed/"
    "duplicate web searches. Do not repeat that: answer from knowledge or "
    "run at most 2 web searches, then answer.")

# v1.8.1 (batch-recovery): appended (opt-in) when the user's message is clearly
# math-shaped. Short + advisory + cache-stable; does NOT force a tool call.
MATH_NUDGE_NOTE = (
    "SYSTEM NOTE — exact arithmetic: this request involves a multi-digit "
    "calculation. Do NOT compute it in your head — call the execute_code tool "
    "and run the arithmetic in Python (e.g. print(125000000/378000)), then "
    "report the exact value it returns. In-head multi-digit math on this model "
    "drops and repeats digits; the code tool is exact.")

# arithmetic cue words, and a "multi-digit number" surface (>=2 contiguous
# digits, a factorial N!, a percentage, or a decimal). BOTH must be present.
_MATH_CUE_RE = re.compile(
    r"(\b(divide[ds]?|dividing|division|multipl(?:y|ies|ied|ication)|times|"
    r"product\s+of|factorial|percent(?:age)?|per\s?cent|convert(?:ed|ing|"
    r"sion)?|square\s+root|sqrt|cube[ds]?|to\s+the\s+power|exponent|"
    r"calculate|compute|how\s+much\s+is|what\s+is|add|plus|added|subtract|"
    r"minus|sum\s+of)\b|%)", re.I)
_MATH_NUM_RE = re.compile(r"\d{2,}|\d+\s*!|\d+\s*%|\d+\.\d+")


def _is_math_shaped(text):
    """True when a user message clearly asks for a multi-digit calculation:
    an arithmetic CUE word AND a multi-digit-number surface. Conservative by
    design, but not perfectly math-only (a year/ratio in narrative can trip a
    cue) — which is exactly why the nudge ships OFF / opt-in. Pure; never
    raises."""
    try:
        s = str(text or "")
        if len(s) > 2000:            # long prose: not a bare calc request
            return False
        return bool(_MATH_CUE_RE.search(s) and _MATH_NUM_RE.search(s))
    except Exception:
        return False


def _last_user_text(msgs):
    """Text of the last role=user message in *msgs* ('' if none)."""
    if not isinstance(msgs, list):
        return ""
    for mm in reversed(msgs):
        if isinstance(mm, dict) and mm.get("role") == "user":
            return _content_text(mm.get("content"))
    return ""

TRIPWIRE_MSG = (
    "tool access disabled: this turn is finishing in forced-synthesis mode "
    "— no more tool calls will be executed this turn. Write your final "
    "answer now as plain text.")

RETRY_PREFIX = "[Fresh session — previous attempt hit a failure loop and was reset]"


def build_retry_prompt(question, diagnosis, findings=None):
    """§2.3 template rephrase: deterministic, language-preserving, carries
    the verbatim question + diagnosis. No aux-LLM call — at DOOMED time the
    model is the sick component."""
    lines = [
        RETRY_PREFIX,
        "The user asked (verbatim): «%s»" % str(question).strip(),
        "Context: a previous attempt failed because %s." % diagnosis,
        "Do NOT repeat that: run at most 2 web searches, web_extract at "
        "most 2 URLs, then answer.",
    ]
    fs = [f for f in (findings or []) if str(f).strip()][:3]
    if fs:
        lines.append("Key findings already retrieved:")
        lines.extend("– %s" % f for f in fs)
    lines.append("Answer in the user's language.")
    return "\n".join(lines)


def build_diagnosis(rec, s8_reason=""):
    """Human-readable diagnosis of which signals fired, with counts."""
    c = rec.get("counters") or {}
    parts = []
    if c.get("searches"):
        parts.append("it ran %d web searches" % c["searches"])
    if c.get("blocks"):
        parts.append("%d calls were blocked as loops" % c["blocks"])
    if rec.get("blanks"):
        parts.append("%d empty model responses" % rec["blanks"])
    base = ", ".join(parts) or "it looped without producing an answer"
    sigs = "; ".join(rec.get("triggers") or [])
    if s8_reason:
        sigs = (sigs + "; " if sigs else "") + "S8:" + s8_reason
    return base + ((" (signals: %s)" % sigs) if sigs else "")


def failure_report(diagnosis, retrying):
    if retrying:
        return ("⚠️ I got stuck in a failure loop and could not produce a "
                "reliable answer (%s). Retrying once in a fresh session — "
                "the answer will follow in a separate message." % diagnosis)
    return ("⚠️ I got stuck in a failure loop and could not produce a "
            "reliable answer (%s). A fresh-session retry is not available "
            "for this question (already used or unsupported here), so I'm "
            "stopping instead of looping. Please rephrase the question or "
            "try again later." % diagnosis)


def harvest_findings(messages, limit=3):
    """Best-effort: one line each from the last <=limit successful tool
    results (web result title+url when parseable, else the first line)."""
    out = []
    try:
        for m in reversed(messages or []):
            if not isinstance(m, dict) or m.get("role") != "tool":
                continue
            c = _content_text(m.get("content")).strip()
            if not c or _GUARD_ERR_PAT.search(c):
                continue
            c = strip_tool_result_wrapper(c)
            if not c:
                continue
            line = ""
            try:
                data = _loads_lenient(c)
                if isinstance(data, dict):
                    if data.get("error"):
                        continue
                    web = (data.get("data") or {}).get("web")
                    if isinstance(web, list) and web and isinstance(web[0], dict):
                        r = web[0]
                        line = "%s — %s" % (str(r.get("title") or "").strip(),
                                            str(r.get("url") or "").strip())
            except Exception:
                pass
            if not line:
                first = next((ln.strip() for ln in c.splitlines() if ln.strip()), "")
                if len(first) < 20 or first in ("{", "["):
                    continue
                line = first
            line = line.strip(" —-")
            if line:
                out.append(line[:200])
            if len(out) >= limit:
                break
    except Exception:
        return []
    return list(reversed(out))


# ---------------------------------------------------------------------------
# Sensors + actuators (hook/middleware callbacks — never raise)
# ---------------------------------------------------------------------------

def _sh_middleware(request=None, api_mode="", base_url="", session_id="",
                   turn_id="", api_call_count=0, platform="", model="",
                   provider="", **_kw):
    """llm_request: sensors S4/S6/S7, state machine step, and the FAILING
    actuator (forced-synthesis strip + corrective append). Runs AFTER
    _cd_middleware, so the strip also removes what constrained decoding
    added this call."""
    try:
        # v1.16.0: record this session's model TIER from the ACTUAL request
        # model/base_url/provider on every call — BEFORE the enabled check, so
        # the tier signal is populated for the transform/finisher/topics hooks
        # regardless of selfheal.enabled, and a mid-session fallback_model swap
        # re-resolves it. (model/provider come from the call kwargs; with
        # model_tier absent this is byte-equivalent to the v1.15.0 host gate.)
        if session_id and (base_url or model):
            _record_tier(session_id, model, base_url, provider)
        # v1.17.0 (B): stash this turn's retrieved corpus for the post-draft
        # claim-grounding annotate (transform_llm_output gets no messages). Runs
        # BEFORE the enabled check (claim_grounding is its own feature) and only
        # when the flag is on (no overhead otherwise). Fail-safe.
        if session_id and _claim_grounding_on():
            _cache_turn_corpus(session_id, request)
        cfg = _cfg()
        if cfg["enabled"] != "on":
            return None
        if not isinstance(request, dict) or not session_id:
            return None
        now = time.time()
        # gate escalation ACTIONS on the per-call resolved tier (== the value
        # just recorded; == _host_is_weak(base_url) when model_tier is absent).
        tier = resolve_tier(model, base_url, provider)
        weak = tier == "weak"
        # v1.16.0 (W2): scale the FAILING/WOBBLING sensor thresholds by tier
        # (weak x1 = v1.15.0; strong x3 + needs >= 2 sensors). NOT hard-off on a
        # strong model — a genuine loop is still caught; a well-behaved one
        # never trips.
        cfg = tier_scale_cfg(cfg, tier)
        rec = _turn_rec(session_id, turn_id, now)
        try:
            rec["api_calls"] = max(rec["api_calls"], int(api_call_count or 0))
        except Exception:
            pass
        msgs = request.get("messages")
        if isinstance(msgs, list):
            rec["blanks"] = max(rec["blanks"], count_turn_blank_assistants(msgs))
        ctr = _counters(session_id, turn_id)
        rec["counters"] = ctr
        sig = {"searches": ctr["searches"], "blocks": ctr["blocks"],
               "blanks": rec["blanks"],
               "sim": sim_collapse(ctr["queries"], cfg["wobble_sim"],
                                   cfg["wobble_sim_n"]),
               "api_calls": rec["api_calls"], "budget": DEFAULT_BUDGET,
               "wall": now - rec["first_ts"],
               "poison_at_birth": rec.get("poison_at_birth", False)}
        # v1.9.0 task-aware coding accommodation: raise ONLY the S7 wall-clock
        # trigger to coding_wall_secs on a detected coding turn (mode "auto"),
        # so a legit code-writing turn isn't forced-synthesised at 150s before
        # the file is written. Never lowers the wall; the other sensors
        # (S1 searches / S2 blocks / S4 blanks / S6 budget) are unchanged, so a
        # coding turn that instead loops on searches still bails. Fully
        # fail-safe: any error leaves the base fail_wall_secs (150) in force.
        try:
            _ict = _host.get("is_coding_turn")
            _cwall = _host.get("coding_wall_secs")
            _cmode = _host.get("coding_mode")
            if (callable(_ict) and callable(_cwall)
                    and (not callable(_cmode) or _cmode() == "auto")
                    and isinstance(msgs, list) and _ict(msgs)):
                _eff = int(_cwall())
                if _eff > cfg["fail_wall_secs"]:
                    cfg = dict(cfg)
                    cfg["fail_wall_secs"] = _eff
        except Exception:
            pass
        new_state, trigger = evaluate_state(rec["state"], sig, cfg)
        if new_state != rec["state"]:
            logger.info("selfheal: %s->%s session=%s turn=%s trigger=%s",
                        rec["state"], new_state, session_id or "-",
                        (turn_id or "-")[-12:], trigger)
            rec["state"] = new_state
            rec["triggers"].append(trigger)

        if rec["state"] == "FAILING":
            # v1.16.0 (W2): forced synthesis is NO LONGER hard-off on a strong
            # model — reaching FAILING already required crossing the x3 /
            # >=2-sensor thresholds (tier_scale_cfg above), so a well-behaved
            # strong model never gets here while a genuinely looping one is
            # caught and recovered in-turn (tool-strip + corrective). The only
            # gate now is the config flag.
            if cfg["forced_synthesis"] != "on":
                if not rec["forced_logged"]:
                    rec["forced_logged"] = True
                    logger.info("selfheal: action=skipped reason=disabled "
                                "(forced_synthesis=off) session=%s", session_id)
                return None
            if rec["forced_msg"] is None:
                rec["forced_msg"] = build_corrective(sig["searches"], sig["blocks"])
                rec["findings"] = harvest_findings(msgs if isinstance(msgs, list) else [])
            if not rec["forced_logged"]:
                rec["forced_logged"] = True
                logger.info("selfheal: action=forced_synthesis session=%s "
                            "turn=%s trigger=%s", session_id or "-",
                            (turn_id or "-")[-12:],
                            "; ".join(rec["triggers"]) or "-")
            request.pop("tools", None)
            request.pop("tool_choice", None)
            request.pop("response_format", None)
            if isinstance(msgs, list):
                last = msgs[-1] if msgs else None
                already = (isinstance(last, dict) and last.get("role") == "user"
                           and _content_text(last.get("content")).startswith(
                               CORRECTIVE_PREFIX))
                if not already:
                    msgs.append({"role": "user", "content": rec["forced_msg"]})
            # Reasoning-channel escape (see DEFAULTS["no_think"]): without
            # this the model frequently emits the whole answer inside
            # <think>/reasoning and content stays empty. vLLM-only; the
            # OpenAI client merges extra_body into the JSON body.
            if cfg["no_think"] == "on" and _no_think_host_ok(base_url):
                eb = request.get("extra_body")
                eb = dict(eb) if isinstance(eb, dict) else {}
                ctk = eb.get("chat_template_kwargs")
                ctk = dict(ctk) if isinstance(ctk, dict) else {}
                ctk["enable_thinking"] = False
                eb["chat_template_kwargs"] = ctk
                request["extra_body"] = eb
            logger.debug("selfheal: forced synthesis applied (api call %s)",
                         api_call_count)
            return {"request": request, "source": "router",
                    "name": "selfheal_forced_synthesis"}

        if rec["state"] == "WOBBLING" and cfg["wobble_honesty"] == "on" and weak:
            # v1.6.2 (T2): append the anti-fabrication note ONCE as a system
            # message. Constant + append-only => prefix cache stays warm.
            if isinstance(msgs, list):
                present = any(
                    isinstance(mm, dict) and mm.get("role") == "system"
                    and _content_text(mm.get("content")) == WOBBLE_HONESTY_NOTE
                    for mm in msgs)
                if not present:
                    msgs.append({"role": "system",
                                 "content": WOBBLE_HONESTY_NOTE})
                    if not rec.get("honesty_logged"):
                        rec["honesty_logged"] = True
                        logger.info("selfheal: action=wobble_honesty session=%s"
                                    " turn=%s", session_id or "-",
                                    (turn_id or "-")[-12:])
                    return {"request": request, "source": "router",
                            "name": "selfheal_wobble_honesty"}

        if rec["state"] == "WOBBLING" and cfg["soft_nudge"] == "on" and weak:
            # Ships OFF (experiment E: tools + corrective user message
            # stalled the endpoint 3/3). Constant message appended on every
            # call while WOBBLING so the wire prefix stays cache-stable.
            if isinstance(msgs, list):
                last = msgs[-1] if msgs else None
                already = (isinstance(last, dict) and last.get("role") == "user"
                           and _content_text(last.get("content")) == SOFT_NUDGE_TEXT)
                if not already:
                    msgs.append({"role": "user", "content": SOFT_NUDGE_TEXT})
                if not rec["soft_logged"]:
                    rec["soft_logged"] = True
                    logger.info("selfheal: action=soft_nudge session=%s turn=%s",
                                session_id or "-", (turn_id or "-")[-12:])
                return {"request": request, "source": "router",
                        "name": "selfheal_soft_nudge"}

        # v1.8.1 (batch-recovery): OPT-IN math->execute_code nudge on a clearly
        # math-shaped user message. State-independent (fires on HEALTHY math
        # turns), host-gated, appended ONCE per turn as a cache-stable system
        # note. Additive only — never strips tools or forces a call.
        if (cfg["math_execute_nudge"] == "on" and _no_think_host_ok(base_url)
                and isinstance(msgs, list)
                and _is_math_shaped(_last_user_text(msgs))):
            present = any(
                isinstance(mm, dict) and mm.get("role") == "system"
                and _content_text(mm.get("content")) == MATH_NUDGE_NOTE
                for mm in msgs)
            if not present:
                msgs.append({"role": "system", "content": MATH_NUDGE_NOTE})
                if not rec.get("math_nudge_logged"):
                    rec["math_nudge_logged"] = True
                    logger.info("selfheal: action=math_execute_nudge session=%s"
                                " turn=%s", session_id or "-",
                                (turn_id or "-")[-12:])
                return {"request": request, "source": "router",
                        "name": "selfheal_math_execute_nudge"}
        return None
    except Exception:
        logger.debug("selfheal: middleware failed; request unchanged",
                     exc_info=True)
        return None


def _sh_pre_tool(tool_name="", args=None, turn_id="", session_id="", **_kw):
    """pre_tool_call tripwire: during FAILING every non-exempt tool is
    blocked (belt-and-braces for textual tool-call parse paths — the
    request no longer carries tools). text_to_speech + the dup_call_exempt
    set stay allowed (self-limiting; voice_only chats can still voice the
    forced answer)."""
    try:
        cfg = _cfg()
        if cfg["enabled"] != "on" or cfg["forced_synthesis"] != "on":
            return None
        rec = _turns.get(session_id)
        if not rec or rec.get("state") != "FAILING":
            return None
        if turn_id and rec.get("turn_id") and rec["turn_id"] != turn_id:
            return None
        # v1.16.0: the tripwire is the FAILING escalation's tool block — gated
        # by the per-guard registry (weak-tier only; INERT on a strong model,
        # where forced synthesis never fired).
        if not guard_active("failing_tripwire", session_id):
            return None
        name = str(tool_name or "")
        try:
            if callable(_host.get("unwrap")):
                name, _ = _host["unwrap"](tool_name, args)
        except Exception:
            name = str(tool_name or "")
        exempt = {"text_to_speech"}
        try:
            if callable(_host.get("exempt")):
                exempt |= set(_host["exempt"]())
        except Exception:
            pass
        if not name or name in exempt:
            return None
        if not rec["trip_logged"]:
            rec["trip_logged"] = True
            logger.info("selfheal: tripwire blocking tools during forced "
                        "synthesis (first blocked: %s) session=%s turn=%s",
                        name, session_id or "-", (turn_id or "-")[-12:])
        else:
            logger.debug("selfheal: tripwire blocked %s", name)
        return {"action": "block", "message": TRIPWIRE_MSG}
    except Exception:
        logger.debug("selfheal: pre_tool_call tripwire failed; call allowed",
                     exc_info=True)
        return None


def _sh_pre_llm(session_id="", turn_id="", conversation_history=None,
                platform="", model="", provider="", **_kw):
    """pre_llm_call: S5 session-poison score at turn start (arms WOBBLING at
    birth) + retry-marker detection (fresh-retry cap survives restarts). The
    supported turn-start context injection rides the soft_nudge flag —
    ships OFF."""
    try:
        # v1.16.0: record the tier from the model id on the FIRST turn (this
        # hook fires before the first llm_request). base_url isn't passed here,
        # so we only do this when a model_tier MAP is configured (keyed on the
        # model id). With the key ABSENT we skip it — resolution then falls back
        # to the configured-model host path in session_tier(), byte-equivalent
        # to v1.15.0's pre_llm host fallback. Fail-safe.
        if (session_id and model and session_id not in _session_tier
                and _model_tier_map() is not None):
            _record_tier(session_id, model, "", provider)
        cfg = _cfg()
        if cfg["enabled"] != "on" or not session_id:
            return None
        now = time.time()
        m = poison_metrics(conversation_history or [])
        prev = (_overlay.get(session_id) or {}).get("prev_degenerate", False)
        _overlay[session_id] = {"poison": m["blank_frac"],
                                "guard_errors": m["guard_errors"],
                                "retry_marker": m["retry_marker"],
                                "prev_degenerate": prev, "ts": now}
        _prune(_overlay, now)
        rec = _turn_rec(session_id, turn_id, now)
        poisoned = m["blank_frac"] >= cfg["poison_hi"] and m["assistants"] >= 4
        rec["poison_at_birth"] = poisoned
        if poisoned:
            logger.info("selfheal: session poison high at turn start "
                        "(S5=%.2f blanks=%d/%d guard_errors=%d) session=%s "
                        "turn=%s", m["blank_frac"], m["blanks"],
                        m["assistants"], m["guard_errors"],
                        session_id or "-", (turn_id or "-")[-12:])
            if cfg["soft_nudge"] == "on":
                logger.info("selfheal: action=turn_start_context session=%s",
                            session_id)
                return {"context": TURN_START_NOTE}
    except Exception:
        logger.debug("selfheal: pre_llm_call sensor failed", exc_info=True)
    return None


# v1.10.0: chat-template special tokens the model occasionally LEAKS into its
# visible output (a known quirk — e.g. a code answer ending "...1e3<|im_end|>").
# These markers are NEVER legitimate answer content, so stripping them is pure
# and safe. An optional role word right after <|im_start|> is consumed too.
_CHAT_SPECIAL_RE = re.compile(
    r"<\|(?:im_start|im_end|im_sep|endoftext)\|>(?:assistant|user|system)?")


def _strip_chat_special_tokens(text):
    """Remove leaked chat-template special tokens. Returns (clean_text, count).
    Fail-safe: on any error returns the input unchanged with count 0."""
    try:
        s = str(text or "")
        new, n = _CHAT_SPECIAL_RE.subn("", s)
        if n:
            new = new.rstrip()
        return new, n
    except Exception:
        return text, 0


_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _strip_md_bold(s):
    """Remove standard-markdown **bold** markers, keeping the inner text."""
    try:
        return _MD_BOLD_RE.sub(r"\1", str(s or "")).strip()
    except Exception:
        return str(s or "").strip()


def _is_table_row(line):
    s = line.strip()
    return s.startswith("|") and s.count("|") >= 2


def _is_table_sep(line):
    s = line.strip()
    return (_is_table_row(line) and "-" in s
            and set(s) <= set("|-: "))


def _table_cells(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _telegram_tablesafe(text):
    """Convert GitHub-flavoured markdown tables to clean bullet groups so the
    Telegram platform adapter's own table->row converter (which mangles **bold**
    header cells into malformed MarkdownV2, e.g. ``*\\*\\*Header*\\*\\*``) never
    runs on them. Each data row becomes:

        **<first-column value>**
        • <col 2 header>: <value>
        • <col 3 header>: <value>

    The first column is treated as the row label. Cells are de-bolded (we add
    our own bold) and empty cells are skipped. Non-table lines pass through
    unchanged. Returns (new_text, tables_converted). Fully fail-safe: on any
    error returns the input unchanged with count 0."""
    try:
        s = str(text or "")
        if "|" not in s:
            return s, 0
        lines = s.split("\n")
        out = []
        i, n, converted = 0, len(lines), 0
        while i < n:
            if (_is_table_row(lines[i]) and i + 1 < n
                    and _is_table_sep(lines[i + 1])):
                header = _table_cells(lines[i])
                i += 2
                first = True
                while (i < n and _is_table_row(lines[i])
                       and not _is_table_sep(lines[i])):
                    row = _table_cells(lines[i])
                    label = _strip_md_bold(row[0]) if row else ""
                    if not first:
                        out.append("")
                    first = False
                    out.append("**%s**" % (label or "—"))
                    for j in range(1, len(header)):
                        col = _strip_md_bold(header[j])
                        val = _strip_md_bold(row[j]) if j < len(row) else ""
                        if val:
                            out.append("• %s: %s" % (col, val) if col
                                       else "• %s" % val)
                    i += 1
                converted += 1
            else:
                out.append(lines[i])
                i += 1
        if not converted:
            return s, 0
        return "\n".join(out), converted
    except Exception:
        return text, 0


def _sh_transform_out(response_text="", session_id="", platform="", **_kw):
    """transform_llm_output: S8 finisher + v1.6.1 outgoing secret scrub.

    hermes applies the FIRST non-empty string a transform_llm_output hook
    returns (agent/turn_finalizer.py:353-357 — hooks do NOT chain), so the
    finisher and the scrubber must compose inside this single callback:
    the finisher may replace a degenerate final with the honest diagnostic,
    then the scrubber (host bridge "scrub" — plugin/__init__.py, config
    tools.tool_search.secret_scrub, default on) redacts secret-shaped
    tokens from whatever text is actually going out. The scrub runs even
    when selfheal itself is disabled; its own failure leaves the text
    unchanged (fail-safe covenant)."""
    replaced = _sh_finisher(response_text=response_text,
                            session_id=session_id, platform=platform, **_kw)
    # v1.6.2 (corpus batch 2): UNCONDITIONAL degeneracy guard — repetition
    # collapse (T1) and intent-announcement (T3) both ship on HEALTHY turns
    # that never arm the finisher, so this runs state-independently. Only when
    # the finisher did not already replace the text (armed path wins).
    if not (isinstance(replaced, str) and replaced):
        # v1.11.2: bounded corruption AUTO-RETRY — runs BEFORE the general guards
        # so a detected token-corruption glitch routes to a fresh re-dispatch
        # (up to corrupt_retries) with a "retrying (k/N)" message, instead of an
        # immediate withhold. When retries are exhausted/unavailable it withholds
        # (the prior behavior); when corrupt_retries=0 it doesn't fire at all and
        # apply_final_guards handles corruption as before. Fully fail-safe: the
        # whole block (incl. the _cfg() read) is guarded, so a config error just
        # falls through to the general guards below.
        try:
            cfg = _cfg()
            if (cfg["enabled"] == "on" and _corruption_guard_on()
                    and guard_active("corruption", session_id)  # weak-tier only
                    and int(cfg.get("corrupt_retries") or 0) > 0):
                vis = strip_think(response_text)
                corrupt, _cr = detect_output_corruption(vis)
                if corrupt:
                    # PREFER the REVERSIBLE auto-retry (a fresh re-dispatch of
                    # the verbatim question — if it comes back clean the glitch
                    # was real, if it comes back the same the "corruption" was
                    # likely a legit token). Reversibility, not corroboration,
                    # is the gateway mechanism (W1(b)).
                    sched, k, n = _schedule_corrupt_retry(session_id, cfg)
                    if sched:
                        logger.info("selfheal: corruption auto-retry %d/%d "
                                    "session=%s", k, n, session_id or "-")
                        replaced = CORRUPT_RETRY_MSG % (k, n)
                    else:
                        # No reversibility available (no gateway / exhausted /
                        # not allowlisted): DON'T hard-withhold here. Fall
                        # through to apply_final_guards, which applies W1
                        # corroboration (>= 2 signals -> withhold; a lone signal
                        # -> deliver with an inline caveat).
                        logger.info("selfheal: corruption auto-retry "
                                    "unavailable/exhausted — deferring to "
                                    "corroboration guard session=%s",
                                    session_id or "-")
        except Exception:
            logger.debug("selfheal: corruption auto-retry failed; falling "
                         "through to guards", exc_info=True)
        # general unconditional guards (repetition/intent collapse, and the
        # corruption withhold when auto-retry is off) — only when nothing above
        # already replaced the text.
        if not (isinstance(replaced, str) and replaced):
            try:
                guarded = apply_final_guards(
                    response_text, _cfg()["doomed_min_answer"], session_id)
            except Exception:
                guarded = None
            if isinstance(guarded, str) and guarded:
                replaced = guarded
    # v1.17.0 (B): post-draft claim-grounding annotate. APPEND-ONLY caveat naming
    # numeric specifics not matched in this turn's retrieved corpus. Runs ONLY on
    # the healthy-answer path (the finisher/corruption/degeneracy guards did NOT
    # replace the text — armed path wins), so it never annotates a withhold stub.
    # Self-gates to research/web turns (no retrieved corpus => no-op). ADD-only,
    # tier-agnostic (guard ceiling "strong" => fires on both tiers). Fail-safe:
    # any error leaves the answer unchanged.
    if not (isinstance(replaced, str) and replaced):
        try:
            annotated = _apply_claim_grounding(response_text, session_id)
            if (isinstance(annotated, str) and annotated
                    and annotated != str(response_text or "")):
                replaced = annotated
        except Exception:
            logger.debug("selfheal: claim grounding step failed; text "
                         "unchanged", exc_info=True)
    # v1.10.0: strip any leaked chat-template special tokens from the outgoing
    # text (runs on whatever is going out — the finisher/guard replacement or
    # the original — and BEFORE the scrub so the scrub still sees the clean
    # text). Only produces a return value when it actually removed something,
    # so non-leaking turns keep the exact prior behaviour.
    base = replaced if (isinstance(replaced, str) and replaced) \
        else str(response_text or "")
    stripped, sn = _strip_chat_special_tokens(base)
    if sn:
        logger.info("selfheal: stripped %d leaked chat-template special "
                    "token(s) from the outgoing answer session=%s",
                    sn, session_id or "-")
    # value to return (None => the transform made no change, use the original)
    result = stripped if sn else replaced
    try:
        scrub = _host.get("scrub")
        if callable(scrub):
            out, n = scrub(stripped)
            if n:
                logger.info("selfheal: secret scrub redacted %d secret-"
                            "shaped token(s) from the outgoing answer "
                            "session=%s", n, session_id or "-")
                result = out
    except Exception:
        logger.debug("selfheal: secret scrub failed; text unchanged",
                     exc_info=True)
    # v1.11.3: Telegram-safe table normalization — the platform adapter's own
    # markdown-table->row-group converter mangles **bold** header cells into
    # malformed MarkdownV2 (``*\*\*Header*\*\*``), which is the likely cause of
    # long, table-heavy answers being accepted by Telegram but rendering
    # wrong/blank. Pre-convert tables to clean bullet groups here (telegram
    # only) so the adapter passes them through intact. Reversible via
    # telegram_tablesafe:off. Fail-safe.
    try:
        if (str(platform or "").lower() == "telegram"
                and _cfg().get("telegram_tablesafe", "on") == "on"):
            current = result if (isinstance(result, str) and result) \
                else str(response_text or "")
            conv, tn = _telegram_tablesafe(current)
            if tn:
                logger.info("selfheal: normalized %d markdown table(s) to "
                            "Telegram-safe bullet groups session=%s",
                            tn, session_id or "-")
                result = conv
    except Exception:
        logger.debug("selfheal: telegram tablesafe failed; text unchanged",
                     exc_info=True)
    # v1.12.0: append the multi-topic reply badge (host bridge, composed here
    # because transform hooks don't chain and this is the sole finisher). Rides
    # on whatever text ships — the healed/scrubbed/tablesafe result or the
    # original response. Returns "" (no-op) when topics is disabled/inert, so
    # non-topic turns are byte-identical. Idempotent; fail-safe.
    try:
        badge = _host.get("badge")
        suffix = badge(session_id) if callable(badge) else ""
        if suffix:
            current = result if (isinstance(result, str) and result) \
                else str(response_text or "")
            if current and suffix not in current:
                result = current + "\n\n" + suffix
    except Exception:
        logger.debug("selfheal: topic badge append failed; text unchanged",
                     exc_info=True)
    return result


def _sh_finisher(response_text="", session_id="", platform="", **_kw):
    """S8 finisher: when forced synthesis was active (or the session
    overlay is poisoned with a degenerate previous turn) and the final text
    is still degenerate, replace it with an honest diagnostic and flag
    DOOMED — scheduling the fresh-session retry when caps/flags allow."""
    try:
        cfg = _cfg()
        if cfg["enabled"] != "on" or not session_id:
            return None
        rec = _turns.get(session_id)
        if not rec or rec.get("finished") or rec.get("closed"):
            return None
        ov = _overlay.get(session_id) or {}
        armed = (rec.get("state") == "FAILING"
                 or (rec.get("poison_at_birth") and ov.get("prev_degenerate")))
        if not armed:
            return None
        # v1.16.0: the DOOMED honest-diagnostic replacement + fresh-session
        # retry are weak-tier escalations. On a strong model the machine may
        # still be armed (poison overlay), but the intervention is INERT —
        # return the answer unchanged (observing only).
        weak = guard_active("doomed_replace", session_id)
        if not weak:
            rec["finished"] = True
            logger.info("selfheal: action=skipped reason=strong-host "
                        "(observing only) session=%s", session_id)
            return None
        degenerate, reason = classify_final(response_text,
                                            cfg["doomed_min_answer"],
                                            weak_host=weak)
        rec["finished"] = True
        if not degenerate:
            if rec.get("state") == "FAILING":
                logger.info("selfheal: forced synthesis produced a real "
                            "answer (%d chars) session=%s",
                            len(strip_think(response_text)), session_id)
            return None
        rec["doomed"] = True
        diagnosis = build_diagnosis(rec, reason)
        logger.info("selfheal: %s->DOOMED session=%s turn=%s trigger=S8:%s",
                    rec.get("state"), session_id or "-",
                    (rec.get("turn_id") or "-")[-12:], reason)
        scheduled, why = (False, "disabled")
        if cfg["fresh_retry"] == "on":
            scheduled, why = _schedule_fresh_retry(session_id, rec, diagnosis, cfg)
        if scheduled:
            logger.info("selfheal: action=fresh_retry attempt=1 session=%s",
                        session_id)
        else:
            logger.info("selfheal: action=failure_report reason=%s session=%s",
                        why, session_id)
        return failure_report(diagnosis, scheduled)
    except Exception:
        logger.debug("selfheal: transform_llm_output failed; text unchanged",
                     exc_info=True)
        return None


def _sh_post_llm(session_id="", turn_id="", assistant_response="",
                 conversation_history=None, **_kw):
    """post_llm_call: close the turn record and feed the session overlay
    (prev_degenerate drives the T3 overlay arm on the NEXT turn)."""
    try:
        cfg = _cfg()
        if cfg["enabled"] != "on" or not session_id:
            return None
        now = time.time()
        deg, _ = classify_final(assistant_response or "",
                                cfg["doomed_min_answer"],
                                weak_host=guard_active("corruption", session_id))
        rec = _turns.get(session_id)
        if rec is not None and (not turn_id or rec.get("turn_id") == turn_id):
            rec["closed"] = True
            if rec.get("doomed"):
                deg = True
        ov = _overlay.setdefault(session_id, {})
        ov["prev_degenerate"] = bool(deg)
        ov["ts"] = now
        _prune(_overlay, now)
    except Exception:
        logger.debug("selfheal: post_llm_call failed", exc_info=True)
    return None


def _maybe_proactive_reset(gateway, session_store, sk, sid, event, cfg, now):
    """FIX 1: reset the existing session if its recent history is poisoned above
    ``poison_reset_hi``. Returns the (possibly rotated) session_id. Runs
    synchronously inside pre_gateway_dispatch — before auth, before the agent is
    built for this message — so a reset here simply makes the normal dispatch
    that follows run the incoming question in a clean session. At most one reset
    per session_key per 120 s (anti-reflood). Never raises: any failure returns
    *sid* unchanged and the turn proceeds normally (the user's message is never
    blocked)."""
    try:
        if cfg.get("proactive_reset") != "on" or not sid or not sk:
            return sid
        plats = cfg.get("platforms") or []
        if plats:
            src = getattr(event, "source", None)
            pv = str(getattr(getattr(src, "platform", None), "value", "")
                     or "").lower()
            if pv and pv not in plats:
                return sid
        rr = _proactive_reset_done.get(sk)
        if rr and (now - float(rr)) < 120:
            return sid
        db = getattr(session_store, "_db", None)
        getm = getattr(db, "get_messages", None)
        if not callable(getm):
            _warn_once("no-get-messages",
                       "selfheal: session_store._db.get_messages unavailable — "
                       "proactive poison reset disarmed")
            return sid
        history = getm(sid)
        if not isinstance(history, list) or len(history) < PROACTIVE_MIN_HISTORY:
            return sid
        # never re-reset a session that is itself a fresh-retry product
        if poison_metrics(history).get("retry_marker"):
            return sid
        score, detail = proactive_poison_score(history)
        if score < float(cfg.get("poison_reset_hi", 0.35)):
            return sid
        reset = getattr(session_store, "reset_session", None)
        if not callable(reset):
            _warn_once("no-reset-session",
                       "selfheal: session_store.reset_session unavailable — "
                       "proactive poison reset disarmed")
            return sid
        new_entry = reset(sk)
        new_sid = (str(getattr(new_entry, "session_id", "") or "")
                   if new_entry is not None else "")
        try:
            ev = getattr(gateway, "_evict_cached_agent", None)
            if callable(ev):
                ev(sk)
        except Exception:
            logger.debug("selfheal: proactive reset evict failed", exc_info=True)
        # per-session overrides — the compression-exhausted reset sequence
        # (gateway/run.py), each behind its own guard (mirror _do_fresh_retry).
        for attr in ("_session_model_overrides", "_pending_model_notes",
                     "_last_resolved_model"):
            try:
                d = getattr(gateway, attr, None)
                if hasattr(d, "pop"):
                    d.pop(sk, None)
            except Exception:
                pass
        _proactive_reset_done[sk] = now
        if len(_proactive_reset_done) > _TABLE_MAX:
            for k in [k for k, v in list(_proactive_reset_done.items())
                      if now - float(v) > _IDLE_S]:
                _proactive_reset_done.pop(k, None)
            if len(_proactive_reset_done) > _TABLE_MAX:
                _proactive_reset_done.clear()
        logger.info("selfheal: proactive poison reset session=%s score=%.2f "
                    "(deg=%s/%s guard=%s) old_session=%s new_session=%s",
                    sk, score, detail.get("deg"), detail.get("asst"),
                    detail.get("guard"), sid, new_sid or "?")
        return new_sid or sid
    except Exception:
        logger.debug("selfheal: proactive poison reset failed; turn proceeds "
                     "normally", exc_info=True)
        return sid


def _sh_gateway_capture(event=None, gateway=None, session_store=None, **_kw):
    """pre_gateway_dispatch: capture the gateway weakref + event loop + the
    per-session_key SessionSource and last user text (everything the DOOMED
    executor needs). Also the belt-and-braces spot: a NEW user message for a
    session_key with a still-pending fresh retry supersedes/cancels it (the
    user moved on). Never influences dispatch (always returns None)."""
    try:
        cfg = _cfg()
        if cfg["enabled"] != "on":
            return None
        now = time.time()
        if gateway is not None:
            try:
                _gw["ref"] = weakref.ref(gateway)
                _gw["ts"] = now
            except Exception:
                pass
            try:
                _gw["loop"] = asyncio.get_running_loop()
            except Exception:
                pass
        src = getattr(event, "source", None)
        if src is None or session_store is None:
            return None
        sk = None
        try:
            gen = getattr(session_store, "_generate_session_key", None)
            if callable(gen):
                sk = gen(src)
        except Exception:
            sk = None
        if not sk:
            _warn_once("no-session-key",
                       "selfheal: session_store._generate_session_key "
                       "unavailable — fresh-retry disarmed for new captures")
            return None
        sid = ""
        try:
            entries = getattr(session_store, "_entries", None)
            e = entries.get(sk) if isinstance(entries, dict) else None
            sid = str(getattr(e, "session_id", "") or "")
        except Exception:
            pass
        pend = _pending_retry.pop(sk, None)
        if pend is not None:
            try:
                fut = pend.get("fut")
                if fut is not None and not fut.done():
                    fut.cancel()
            except Exception:
                pass
            logger.info("selfheal: pending fresh retry for %s superseded by "
                        "a new user message", sk)
        # v1.7.1 (FIX 1): if the EXISTING session is poisoned, reset it here —
        # BEFORE the incoming question is dispatched — so the question runs in a
        # clean session (no re-dispatch needed; normal dispatch continues with
        # the reset session). sid is re-read to the rotated id for the capture.
        sid = _maybe_proactive_reset(gateway, session_store, sk, sid, event,
                                     cfg, now)
        _sessions[sk] = {"source": src,
                         "text": str(getattr(event, "text", "") or ""),
                         "ts": now, "session_id": sid}
        _prune(_sessions, now)
    except Exception:
        logger.debug("selfheal: gateway capture failed", exc_info=True)
    return None


# ---------------------------------------------------------------------------
# DOOMED executor: fresh-session retry through the captured gateway
# ---------------------------------------------------------------------------

def _norm_qhash(text):
    return hashlib.sha256(
        " ".join(str(text or "").casefold().split()).encode("utf-8")).hexdigest()


def _schedule_fresh_retry(session_id, rec, diagnosis, cfg):
    """Decide + schedule the fresh-session retry. Returns (scheduled, why).

    Refuses when: gateway refs missing/stale (>24h), platform not
    allowlisted, the session history already carries the retry marker
    (restart-surviving cap), or (session_key, question-hash) was already
    retried. Marks the retried table BEFORE dispatch so a crash cannot
    double-retry."""
    try:
        ref, loop = _gw.get("ref"), _gw.get("loop")
        gw = ref() if ref is not None else None
        if gw is None or loop is None:
            return False, "no-gateway"
        if time.time() - float(_gw.get("ts") or 0) > 86400:
            return False, "stale-gateway"
        sk, info = None, None
        for k, v in list(_sessions.items()):
            if (v or {}).get("session_id") == session_id:
                sk, info = k, v
                break
        if sk is None:
            # /new etc. rotated the id after capture — resolve via the live
            # session store (guarded private read).
            try:
                entries = getattr(getattr(gw, "session_store", None),
                                  "_entries", None)
                if isinstance(entries, dict):
                    for k, e in list(entries.items()):
                        if str(getattr(e, "session_id", "") or "") == session_id:
                            sk, info = k, _sessions.get(k)
                            break
            except Exception:
                pass
        if sk is None or not info:
            return False, "no-session-capture"
        source = info.get("source")
        question = str(info.get("text") or "").strip()
        if source is None or not question:
            return False, "no-question"
        if question.startswith(RETRY_MARKER_SAFE):
            return False, "question-is-retry"
        plats = cfg.get("platforms") or []
        pv = str(getattr(getattr(source, "platform", None), "value", "")
                 or "").lower()
        if plats and pv not in plats:
            return False, "platform:%s" % (pv or "?")
        if (_overlay.get(session_id) or {}).get("retry_marker"):
            return False, "marker-cap"
        qhash = _norm_qhash(question)
        now = time.time()
        entry = _retried.get((sk, qhash))
        if entry and entry.get("count", 0) >= int(cfg["max_retries_per_question"]):
            return False, "cap"
        _retried[(sk, qhash)] = {"count": (entry or {}).get("count", 0) + 1,
                                 "ts": now}
        _prune(_retried, now)
        fut = asyncio.run_coroutine_threadsafe(
            _do_fresh_retry(sk, source, question, diagnosis,
                            list(rec.get("findings") or [])), loop)
        _pending_retry[sk] = {"ts": now, "fut": fut}
        _prune(_pending_retry, now)
        return True, ""
    except Exception:
        logger.warning("selfheal: could not schedule fresh retry",
                       exc_info=True)
        return False, "schedule-error"


RETRY_MARKER_SAFE = RETRY_PREFIX.split("—")[0].strip()  # "[Fresh session"


def _schedule_corrupt_retry(session_id, cfg):
    """Bounded corruption auto-retry (v1.11.2). Re-dispatch the verbatim
    question in a fresh session — a token-corruption glitch is transient, so a
    re-answer is usually clean — capped at cfg['corrupt_retries']. Returns
    (scheduled, attempt_number, cap). Keeps its OWN counter so it never
    interferes with the DOOMED fresh-retry, and refuses (falls back to withhold)
    whenever a fresh retry can't run: no gateway, no captured question, the
    question is itself a retry, platform not allowlisted, or the cap is reached.
    Never raises."""
    cap = 0
    try:
        cap = int(cfg.get("corrupt_retries") or 0)
        if cap <= 0:
            return False, 0, cap
        ref, loop = _gw.get("ref"), _gw.get("loop")
        gw = ref() if ref is not None else None
        if gw is None or loop is None:
            return False, 0, cap
        if time.time() - float(_gw.get("ts") or 0) > 86400:
            return False, 0, cap
        sk, info = None, None
        for k, v in list(_sessions.items()):
            if (v or {}).get("session_id") == session_id:
                sk, info = k, v
                break
        if sk is None:
            try:
                entries = getattr(getattr(gw, "session_store", None),
                                  "_entries", None)
                if isinstance(entries, dict):
                    for k, e in list(entries.items()):
                        if str(getattr(e, "session_id", "") or "") == session_id:
                            sk, info = k, _sessions.get(k)
                            break
            except Exception:
                pass
        if sk is None or not info:
            return False, 0, cap
        source = info.get("source")
        question = str(info.get("text") or "").strip()
        if source is None or not question:
            return False, 0, cap
        if question.startswith(RETRY_MARKER_SAFE):
            return False, 0, cap
        plats = cfg.get("platforms") or []
        pv = str(getattr(getattr(source, "platform", None), "value", "")
                 or "").lower()
        if plats and pv not in plats:
            return False, 0, cap
        qhash = _norm_qhash(question)
        now = time.time()
        prev = (_corrupt_retried.get((sk, qhash)) or {}).get("count", 0)
        if prev >= cap:
            return False, prev, cap
        attempt = prev + 1
        _corrupt_retried[(sk, qhash)] = {"count": attempt, "ts": now}
        _prune(_corrupt_retried, now)
        fut = asyncio.run_coroutine_threadsafe(
            _do_fresh_retry(sk, source, question,
                            "a transient token-corruption glitch", []), loop)
        _pending_retry[sk] = {"ts": now, "fut": fut}
        _prune(_pending_retry, now)
        return True, attempt, cap
    except Exception:
        logger.warning("selfheal: corruption retry scheduling failed",
                       exc_info=True)
        return False, 0, cap


def _gw_get(obj, name):
    """Guarded private-symbol access: missing symbol logs one warning and
    returns None — callers degrade to the next-weaker action."""
    fn = getattr(obj, name, None)
    if not callable(fn):
        _warn_once("missing:" + name,
                   "selfheal: gateway symbol %r missing/changed — degrading "
                   "to a weaker action", name)
        return None
    return fn


async def _deliver_text(gw, source, text):
    """Deliver text via the platform adapter. False when undeliverable."""
    try:
        afs = _gw_get(gw, "_adapter_for_source")
        adapter = afs(source) if afs else None
        if adapter is None or not hasattr(adapter, "send"):
            _warn_once("no-adapter",
                       "selfheal: no adapter for source — cannot deliver")
            return False
        metadata = None
        tid = getattr(source, "thread_id", None)
        if tid:
            metadata = {"thread_id": tid}
        await adapter.send(chat_id=str(getattr(source, "chat_id", "") or ""),
                           content=text, metadata=metadata)
        return True
    except Exception:
        logger.warning("selfheal: adapter delivery failed", exc_info=True)
        return False


async def _do_fresh_retry(session_key, source, question, diagnosis, findings):
    """The DOOMED action (§1.c): wait for the failing turn to release, then
    interrupt-if-stuck → reset_session → evict → overrides-clear → synthetic
    internal MessageEvent re-dispatch → deliver. Every private symbol goes
    through _gw_get; any failure degrades to the honest failure report."""
    gw = _gw["ref"]() if _gw.get("ref") else None
    if gw is None:
        logger.warning("selfheal: action=skipped reason=gateway-gone")
        return
    try:
        try:
            running = getattr(gw, "_running_agents", None)
            for _ in range(90):
                if not isinstance(running, dict) or session_key not in running:
                    break
                await asyncio.sleep(1.0)
            else:
                fn = _gw_get(gw, "_interrupt_and_clear_session")
                if fn is not None:
                    await fn(session_key, source,
                             interrupt_reason="selfheal fresh retry",
                             invalidation_reason="selfheal-fresh-retry")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("selfheal: run-release wait failed", exc_info=True)

        store = getattr(gw, "session_store", None)
        reset = _gw_get(store, "reset_session") if store is not None else None
        if reset is None:
            await _deliver_text(gw, source, failure_report(diagnosis, False))
            return
        new_entry = reset(session_key)
        if new_entry is None:
            logger.warning("selfheal: reset_session(%s) returned None — "
                           "delivering failure report", session_key)
            await _deliver_text(gw, source, failure_report(diagnosis, False))
            return
        ev = _gw_get(gw, "_evict_cached_agent")
        if ev is not None:
            ev(session_key)
        # Per-session overrides — the compression-exhausted reset sequence
        # (gateway/run.py:11637-11648), each behind its own guard.
        for attr in ("_session_model_overrides", "_pending_model_notes",
                     "_last_resolved_model"):
            try:
                d = getattr(gw, attr, None)
                if hasattr(d, "pop"):
                    d.pop(session_key, None)
            except Exception:
                pass
        try:
            srr = getattr(gw, "_set_session_reasoning_override", None)
            if callable(srr):
                srr(session_key, None)
        except Exception:
            pass
        try:
            sync = getattr(gw, "_sync_telegram_topic_binding", None)
            if callable(sync):
                await asyncio.to_thread(sync, source, new_entry,
                                        reason="selfheal-fresh-retry")
        except Exception:
            logger.debug("selfheal: topic binding sync failed", exc_info=True)

        try:
            from gateway.platforms.base import MessageEvent
        except Exception:
            _warn_once("no-messageevent",
                       "selfheal: MessageEvent unavailable — degrading to "
                       "failure report")
            await _deliver_text(gw, source, failure_report(diagnosis, False))
            return
        hm = _gw_get(gw, "_handle_message")
        if hm is None:
            await _deliver_text(gw, source, failure_report(diagnosis, False))
            return
        prompt = build_retry_prompt(question, diagnosis, findings)
        evt = MessageEvent(text=prompt, source=source, internal=True)
        logger.info("selfheal: fresh retry dispatching session_key=%s "
                    "new_session=%s", session_key,
                    getattr(new_entry, "session_id", "?"))
        response = await hm(evt)
        if response:
            # Streaming may already have delivered it (handoff pattern,
            # gateway/run.py:7475-7500); a returned text means it didn't.
            await _deliver_text(gw, source, response)
        logger.info("selfheal: fresh retry completed session_key=%s "
                    "delivered=%s", session_key, bool(response))
    except asyncio.CancelledError:
        logger.info("selfheal: fresh retry cancelled (superseded) "
                    "session_key=%s", session_key)
    except Exception:
        logger.warning("selfheal: fresh retry failed — attempting honest "
                       "failure report", exc_info=True)
        try:
            await _deliver_text(gw, source, failure_report(diagnosis, False))
        except Exception:
            pass
    finally:
        _pending_retry.pop(session_key, None)


def _sh_scrub_reasoning(assistant_message=None, session_id="", **_kw):
    """post_api_request: v1.15.0 airtight secret scrub of the REASONING channel.

    The transform_llm_output scrub (composed in _sh_transform_out) only sees the
    FINAL answer text. A reasoning model (DeepSeek v4, etc.) emits a separate
    reasoning channel that hermes persists to state.db (msg["reasoning"] /
    msg["reasoning_content"], built AFTER this hook by
    chat_completion_helpers.build_assistant_message — and NOT run through
    hermes's own content redactor) and shows on the CLI. Batch4 r38: the final
    answer was correctly redacted while the raw vw_ key leaked verbatim through
    the reasoning channel and persisted. This hook fires per API call with the
    freshly-normalized assistant_message passed BY REFERENCE, BEFORE the message
    is turned into the stored dict / displayed, so scrubbing its fields in place
    closes both the state.db and CLI exposures.

    MODEL-AGNOSTIC (NOT host-gated — the reasoning channel is a frontier-reasoner
    leak path); gated only by tools.tool_search.secret_scrub via the shared
    scrub bridge. Fully fail-safe: any error leaves the message untouched."""
    try:
        scrub = _host.get("scrub")
        if not callable(scrub) or assistant_message is None:
            return None
        total = 0

        def _do(getval, setval):
            nonlocal total
            try:
                v = getval()
                if isinstance(v, str) and v:
                    out, n = scrub(v)
                    if n:
                        setval(out)
                        total += n
            except Exception:
                pass

        # 1) final/visible content (belt-and-braces: the transform scrub covers
        #    the TURN-final text, but a tool-call step's content isn't seen there
        #    and still persists).
        _do(lambda: getattr(assistant_message, "content", None),
            lambda o: setattr(assistant_message, "content", o))
        # 2) the direct .reasoning attribute (DeepSeek/Qwen).
        _do(lambda: getattr(assistant_message, "reasoning", None),
            lambda o: setattr(assistant_message, "reasoning", o))
        # 3) provider_data-backed reasoning fields (the .reasoning_content /
        #    .reasoning_details properties read from here — mutate the dict).
        pd = getattr(assistant_message, "provider_data", None)
        if isinstance(pd, dict):
            _do(lambda: pd.get("reasoning_content"),
                lambda o: pd.__setitem__("reasoning_content", o))
            rd = pd.get("reasoning_details")
            if isinstance(rd, list):
                for d in rd:
                    if isinstance(d, dict):
                        for _k in ("summary", "thinking", "content", "text"):
                            _do((lambda k: (lambda: d.get(k)))(_k),
                                (lambda k: (lambda o: d.__setitem__(k, o)))(_k))
        # 4) pydantic model_extra fallback (build_assistant_message also checks
        #    it for reasoning_content).
        me = getattr(assistant_message, "model_extra", None)
        if isinstance(me, dict):
            _do(lambda: me.get("reasoning_content"),
                lambda o: me.__setitem__("reasoning_content", o))
        if total:
            logger.info("selfheal: secret scrub redacted %d secret-shaped "
                        "token(s) from the reasoning/content channel "
                        "session=%s", total, session_id or "-")
    except Exception:
        logger.debug("selfheal: reasoning scrub failed; message unchanged",
                     exc_info=True)
    return None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

for _cb in (_sh_middleware, _sh_pre_tool, _sh_pre_llm, _sh_post_llm,
            _sh_transform_out, _sh_gateway_capture, _sh_scrub_reasoning):
    _cb._router_selfheal = True  # dedup marker + verify.py detection
del _cb


def register(ctx, host=None):
    """Register all selfheal surfaces. *host* bridges the v1.5.x per-turn
    counters from plugin/__init__.py (counters/search_hard_cap/unwrap/
    exempt callables). Idempotent on force rescans; never raises."""
    try:
        if isinstance(host, dict):
            _host.update(host)
        if not hasattr(ctx, "register_hook") or not hasattr(ctx, "register_middleware"):
            logger.warning("selfheal: PluginContext lacks register_hook/"
                           "register_middleware; selfheal NOT installed")
            return
        try:  # dedup: a force rescan re-runs register() on the same manager
            hooks = ctx._manager._hooks.get("pre_tool_call", [])
            if any(getattr(cb, "_router_selfheal", False) for cb in hooks):
                return
        except Exception:
            pass  # private layout changed — worst case a redundant copy
        # Middleware AFTER _cd_middleware (registration order within one
        # register() pass is deterministic — __init__ calls us last).
        ctx.register_middleware("llm_request", _sh_middleware)
        ctx.register_hook("pre_tool_call", _sh_pre_tool)
        ctx.register_hook("pre_llm_call", _sh_pre_llm)
        ctx.register_hook("post_llm_call", _sh_post_llm)
        ctx.register_hook("transform_llm_output", _sh_transform_out)
        ctx.register_hook("pre_gateway_dispatch", _sh_gateway_capture)
        # v1.15.0: model-agnostic reasoning-channel secret scrub.
        ctx.register_hook("post_api_request", _sh_scrub_reasoning)
        cfg = _cfg()
        logger.info("selfheal: registered (enabled=%s forced_synthesis=%s "
                    "fresh_retry=%s soft_nudge=%s platforms=%s "
                    "search_hard_cap=%s)", cfg["enabled"],
                    cfg["forced_synthesis"], cfg["fresh_retry"],
                    cfg["soft_nudge"], ",".join(cfg["platforms"]) or "all",
                    cfg["search_hard_cap"])
    except Exception:
        logger.warning("selfheal: failed to install; healer inactive "
                       "(normal operation unaffected)", exc_info=True)
