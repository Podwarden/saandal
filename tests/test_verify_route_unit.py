#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Podwarden Inc

"""Unit tests for plugin/verify_route.py (F1 force_verify + F2 calc_route) and
their wiring into _cd_middleware. Detector precision (incl. NON-matching queries
that must NOT trigger), request transforms, fail-safety, and flag-gating."""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "router_plugin", REPO / "plugin" / "__init__.py",
        submodule_search_locations=[str(REPO / "plugin")])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["router_plugin"] = mod
    spec.loader.exec_module(mod)
    return mod


plugin = load_plugin()
import router_plugin.verify_route as vr  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"ok   {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


# --- F1 attribution/recency detector: MUST match ----------------------------
ATTR_YES = [
    "Who wrote the poem about a caged eagle?",
    "What is the exact next line after «Вскормленный в неволе орел молодой»?",
    "из какого произведения эта строка?",
    "кто написал «Войну и мир»?",
    "In what year did the Treaty of Westphalia get signed?",
    "Give me the full citation for that paper.",
    "Who is the current Prime Minister of the UK?",
    "What's the latest stable version of Python?",
    "How much is an NVIDIA H100 right now?",
    "What happened at the 2027 World Cup?",
    "«To be or not to be» — which play is that from?",
]
for q in ATTR_YES:
    check("F1 detects attribution/recency: %r" % q[:40],
          vr.attribution_recency_intent(q), "MISSED")

# --- F1 detector: MUST NOT match (over-trigger guard) ------------------------
ATTR_NO = [
    "How do I fix a null pointer bug in my Rust code?",
    "Write me a haiku about autumn.",
    "Explain how recursion works.",
    "What's a good recipe for sourdough bread?",
    "Can you refactor this function to be cleaner?",
    "hello, how are you?",
    "Summarize this paragraph for me.",
    "Translate 'good morning' into French.",
]
for q in ATTR_NO:
    check("F1 does NOT over-trigger: %r" % q[:40],
          not vr.attribution_recency_intent(q), "FALSE-POSITIVE")

# --- F1 conceptual-framing exclusion (A/B eval's biggest false-positive class)
CONCEPTUAL_NO = [
    "Explain the newest React hooks",
    "How do I use the latest features in Python?",
    "Teach me the concept of the current best practices for caching",
    "Walk me through how the latest transformer models work",
    "What's the difference between the newest sorting algorithms?",
]
for q in CONCEPTUAL_NO:
    check("F1 excludes conceptual/how-to framing: %r" % q[:40],
          not vr.attribution_recency_intent(q), "FALSE-POSITIVE")
# ...but a STRONG factual anchor overrides the conceptual guard
CONCEPTUAL_YES = [
    "Explain why the current UK Prime Minister won the election",
    "Teach me about «To be or not to be» — who wrote it and when?",
]
for q in CONCEPTUAL_YES:
    check("F1 still fires with a strong factual anchor: %r" % q[:40],
          vr.attribution_recency_intent(q), "MISSED-STRONG-ANCHOR")

# --- F2 arithmetic detector -------------------------------------------------
ARITH_YES = ["What is 68743 * 49216?", "Compute 483726 + 918273 + 5540912",
             "multiply 12345 by 6789", "the product of 99999 and 88888",
             "divide 987654321 by 12345", "68743×49216 = ?"]
for q in ARITH_YES:
    check("F2 detects arithmetic: %r" % q[:36], vr.arithmetic_intent(q), "MISSED")
ARITH_NO = ["What is 2 + 2?", "how many legs does a spider have?",
            "I have 3 apples and 2 oranges", "what year is it",
            "explain big-O notation"]
for q in ARITH_NO:
    check("F2 does NOT over-trigger: %r" % q[:36],
          not vr.arithmetic_intent(q), "FALSE-POSITIVE")

# --- R2 research_intent (multi-part investigation) --------------------------
RESEARCH_YES = [
    "/research compare pnpm vs npm install speed",
    "research the pros and cons of electric vs petrol motorcycles",
    "compare Michelin Road 6 versus Pirelli Angel for touring",
    "find out which is the best budget all-season tyre",
    "investigate the trade-offs between Postgres and MySQL for our workload",
    "look into the different options for self-hosting a vector database",
    "evaluate several alternatives to Redis for caching",
]
for q in RESEARCH_YES:
    check("R2 research_intent detects: %r" % q[:44], vr.research_intent(q), "MISSED")
# single checkable facts / non-research must NOT match (attribution owns those)
RESEARCH_NO = [
    "Who wrote «Онегин»?",
    "Who is the current Prime Minister of the UK?",
    "What's the latest stable version of Python?",
    "hello, how are you?",
    "refactor this function please",
    "what's the capital of France",
    "explain how recursion works",
    "translate good morning into French",
]
for q in RESEARCH_NO:
    check("R2 research_intent does NOT over-trigger: %r" % q[:44],
          not vr.research_intent(q), "FALSE-POSITIVE")
check("R2 research_intent fail-safe on non-str", vr.research_intent(None) is False)

# --- request-shape helpers --------------------------------------------------
check("first_call_of_turn: fresh user turn is first call",
      vr.first_call_of_turn([{"role": "system", "content": "s"},
                             {"role": "user", "content": "who wrote it?"}]))
check("first_call_of_turn: after a tool result it is NOT first call",
      not vr.first_call_of_turn([
          {"role": "user", "content": "who wrote it?"},
          {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
          {"role": "tool", "content": "result"}]))
check("has_web_search true", vr.has_web_search(
    [{"type": "function", "function": {"name": "web_search"}}]))
check("has_web_search false", not vr.has_web_search(
    [{"type": "function", "function": {"name": "calculator"}}]))

# --- apply_force_verify (F1) ------------------------------------------------
req = {"messages": [{"role": "user", "content": "Who wrote «Онегин»?"}],
       "tools": [{"type": "function", "function": {"name": "web_search"}}],
       "tool_choice": "auto"}
applied = vr.apply_force_verify(req)
check("F1 applies: directive injected, tool_choice LEFT auto (safe, no hard-force)",
      applied and req["tool_choice"] == "auto"
      and any(m.get("content") == vr.FORCE_VERIFY_NOTE
              for m in req["messages"] if m.get("role") == "system"))
# idempotent (no duplicate directive) — but tool_choice already required
n_before = sum(1 for m in req["messages"] if m.get("content") == vr.FORCE_VERIFY_NOTE)
vr.apply_force_verify(req)
n_after = sum(1 for m in req["messages"] if m.get("content") == vr.FORCE_VERIFY_NOTE)
check("F1 idempotent (directive not duplicated)", n_before == n_after == 1)

# F1 no-ops: no web_search tool
req2 = {"messages": [{"role": "user", "content": "Who wrote «Онегин»?"}],
        "tools": [], "tool_choice": "auto"}
check("F1 no-op without web_search", not vr.apply_force_verify(req2)
      and req2["tool_choice"] == "auto")
# F1 no-ops: not first call of turn
req3 = {"messages": [{"role": "user", "content": "Who wrote it?"},
                     {"role": "assistant", "content": "",
                      "tool_calls": [{"id": "1"}]},
                     {"role": "tool", "content": "r"}],
        "tools": [{"type": "function", "function": {"name": "web_search"}}],
        "tool_choice": "auto"}
check("F1 no-op after the first call (mid tool loop)",
      not vr.apply_force_verify(req3))
# F1 no-ops: non-attribution question
req4 = {"messages": [{"role": "user", "content": "Refactor this loop please"}],
        "tools": [{"type": "function", "function": {"name": "web_search"}}],
        "tool_choice": "auto"}
check("F1 no-op on a non-attribution question", not vr.apply_force_verify(req4)
      and req4["tool_choice"] == "auto")

# --- apply_calc_route (F2) --------------------------------------------------
reqc = {"messages": [{"role": "user", "content": "What is 68743 * 49216?"}]}
check("F2 applies the calc directive", vr.apply_calc_route(reqc)
      and any(m.get("content") == vr.CALC_ROUTE_NOTE
              for m in reqc["messages"] if m.get("role") == "system"))
check("F2 does NOT force tool_choice (light touch)",
      "tool_choice" not in reqc or reqc.get("tool_choice") in (None, "auto"))
reqc2 = {"messages": [{"role": "user", "content": "what is 2+2"}]}
check("F2 no-op on trivial arithmetic", not vr.apply_calc_route(reqc2))

# --- stale-year strip -------------------------------------------------------
check("strip_stale_years removes 2024/2025",
      vr.strip_stale_years("current UK prime minister 2024") == "current UK prime minister"
      and "2025" not in vr.strip_stale_years("latest python 2025 release"))
check("strip_stale_years keeps a relevant future year",
      "2027" in vr.strip_stale_years("2027 World Cup schedule"))

# --- fail-safety ------------------------------------------------------------
check("detectors fail-safe on non-str", vr.attribution_recency_intent(None) is False
      and vr.arithmetic_intent(12345) is False)
check("apply fns fail-safe on a bad request",
      vr.apply_force_verify("nope") is False and vr.apply_calc_route({}) is False)

# --- _cd_middleware wiring (flag-gated) --------------------------------------
_saved = plugin._ts_flag
_saved_cd = plugin._cd_settings
plugin._cd_settings = lambda: ("off", {"vllm.example.com"})  # host constrained, cd off
try:
    plugin._ts_flag = lambda k, d="off": "on" if k == "force_verify" else "off"
    req = {"messages": [{"role": "user", "content": "Who first said «cogito ergo sum»?"}],
           "tools": [{"type": "function", "function": {"name": "web_search"}}],
           "tool_choice": "auto"}
    out = plugin._cd_middleware(request=req, api_mode="chat_completions",
                               base_url="https://vllm.example.com/v1")
    check("_cd_middleware applies force_verify when flag on + host constrained",
          isinstance(out, dict) and "force_verify" in out.get("name", "")
          and req["tool_choice"] == "auto")
finally:
    plugin._ts_flag = _saved
    plugin._cd_settings = _saved_cd

# both flags off -> F1/F2 never applied
_saved = plugin._ts_flag
try:
    plugin._ts_flag = lambda k, d="off": "off"
    req = {"messages": [{"role": "user", "content": "Who wrote it?"}],
           "tools": [{"type": "function", "function": {"name": "web_search"}}],
           "tool_choice": "auto"}
    out = plugin._cd_middleware(request=req, api_mode="chat_completions",
                               base_url="https://vllm.example.com/v1")
    nm = out.get("name", "") if isinstance(out, dict) else ""
    check("flags OFF -> no force_verify/calc_route in the middleware output",
          "force_verify" not in nm and "calc_route" not in nm
          and req["tool_choice"] == "auto")
finally:
    plugin._ts_flag = _saved
    plugin._cd_settings = _saved_cd

# ---------------------------------------------------------------------------
# v1.17.0 (A) — grounding directive (apply_grounding_directive + middleware).
# ---------------------------------------------------------------------------
WS = [{"type": "function", "function": {"name": "web_search"}}]

req = {"messages": [{"role": "user",
                     "content": "Research and compare vLLM vs SGLang tradeoffs"}],
       "tools": WS}
check("A: applies on a first-call research turn with web_search",
      vr.apply_grounding_directive(req) is True
      and any(m.get("role") == "system" and m["content"] == vr.GROUNDING_NOTE
              for m in req["messages"]))
check("A: idempotent (no second note)",
      vr.apply_grounding_directive(req) is False
      and sum(1 for m in req["messages"] if m.get("role") == "system") == 1)
check("A: skips a non-research single fact",
      vr.apply_grounding_directive(
          {"messages": [{"role": "user", "content": "what is 2+2"}],
           "tools": WS}) is False)
check("A: skips when no web_search tool present",
      vr.apply_grounding_directive(
          {"messages": [{"role": "user",
                         "content": "research and compare X vs Y"}],
           "tools": []}) is False)
check("A: skips mid-turn (not first call)",
      vr.apply_grounding_directive(
          {"messages": [{"role": "user", "content": "research X vs Y"},
                        {"role": "assistant", "content": "",
                         "tool_calls": [{}]}],
           "tools": WS}) is False)
check("A: fires on an attribution/recency turn too",
      vr.apply_grounding_directive(
          {"messages": [{"role": "user",
                         "content": "Who is the current CEO and latest funding?"}],
           "tools": WS}) is True)
check("A: fail-safe on a non-dict request",
      vr.apply_grounding_directive(None) is False)

# middleware gating: un-host-gated (fires on deepseek), flag-gated, api_mode gate
_saved = plugin._ts_flag
try:
    plugin._ts_flag = lambda k, d="off": "on" if k == "ground_directive" else d
    r = plugin._antifab_middleware(
        request={"messages": [{"role": "user",
                               "content": "research and compare A vs B"}],
                 "tools": WS},
        api_mode="chat_completions", base_url="https://api.deepseek.com")
    check("A middleware: fires un-host-gated on deepseek",
          isinstance(r, dict) and r.get("name") == "ground_directive")
    r2 = plugin._antifab_middleware(
        request={"messages": [{"role": "user", "content": "research A vs B"}],
                 "tools": WS},
        api_mode="responses", base_url="https://api.deepseek.com")
    check("A middleware: skips non chat_completions api_mode", r2 is None)
    plugin._ts_flag = lambda k, d="off": "off"
    r3 = plugin._antifab_middleware(
        request={"messages": [{"role": "user", "content": "research A vs B"}],
                 "tools": WS},
        api_mode="chat_completions", base_url="https://api.deepseek.com")
    check("A middleware: ground_directive=off => no-op", r3 is None)
finally:
    plugin._ts_flag = _saved

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
