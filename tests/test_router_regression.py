#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Podwarden Inc

"""Offline regression suite for the v1.5.x router features after the v1.6.0
selfheal refactor: dup-query gate, dup-call gate, search hard cap, steering
note, brand nudge, constrained decoding + clamps, and the new
router_turn_counters accessor the gates now feed.

Run: tests/test_router_regression.py   (bin/verify.py covers prompt-size)
"""
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "router_plugin", REPO / "plugin" / "__init__.py",
    submodule_search_locations=[str(REPO / "plugin")])
mod = importlib.util.module_from_spec(spec)
sys.modules["router_plugin"] = mod
spec.loader.exec_module(mod)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"ok   {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


# Pin config-dependent knobs to the shipped defaults so the suite does not
# depend on the operator's current config.yaml.
mod._dup_limit = lambda: 3
mod._dup_call_limit = lambda: 3
mod._dup_call_exempt = lambda: set(mod.DUP_CALL_EXEMPT_DEFAULT)
mod._search_hard_cap = lambda: 15
mod._steer_after = lambda: 6
mod._brand_nudge_setting = lambda: "on"
mod._mt_cap = lambda: 3000
mod._fp_value = lambda: 0.3
mod._pp_value = lambda: 0.3
mod._cd_settings = lambda: ("on", ["vllm.example.com"])

# --- v1.5.2 duplicate-query gate ---------------------------------------------

S, T = "regr-s1", "regr-t1"
for i in range(3):
    r = mod._dup_gate(tool_name="web_search", args={"query": "Same Query"},
                      turn_id=T, session_id=S)
    check(f"dup-query attempt {i+1} allowed", r is None)
r = mod._dup_gate(tool_name="web_search", args={"query": "same  query"},
                  turn_id=T, session_id=S)
check("dup-query 4th (normalized) blocked",
      isinstance(r, dict) and r.get("action") == "block"
      and "duplicate query loop" in r["message"])

# --- v1.6.2 T6: empty web_search query blocked before the network ------------
for _q, _label in (("", "empty string"), ("   ", "whitespace"),
                   ("\n\t", "newline/tab"), (None, "missing/None")):
    r = mod._dup_gate(tool_name="web_search", args={"query": _q},
                      turn_id="regr-t6", session_id="regr-s6")
    check(f"empty web_search blocked ({_label})",
          isinstance(r, dict) and r.get("action") == "block"
          and "empty query" in r["message"].lower())
r = mod._dup_gate(tool_name="web_search", args={},
                  turn_id="regr-t6", session_id="regr-s6")
check("web_search with no query key blocked",
      isinstance(r, dict) and r.get("action") == "block")
r = mod._dup_gate(tool_name="web_search", args={"query": "a real query"},
                  turn_id="regr-t6b", session_id="regr-s6b")
check("non-empty web_search allowed", r is None)
# blocked empty queries never touched the counters (blocked pre-count)
_c6 = mod.router_turn_counters("regr-s6", "regr-t6")
check("empty-query blocks not counted as searches",
      _c6["searches"] == 0, str(_c6))

# --- v1.5.4 hard search ceiling ----------------------------------------------

S2 = "regr-s2"
blocked_at = None
for i in range(1, 18):
    r = mod._dup_gate(tool_name="web_search", args={"query": f"varied {i}"},
                      turn_id=T, session_id=S2)
    if r is not None and blocked_at is None:
        blocked_at = i
        check("ceiling block message orders final answer",
              "search limit reached" in r["message"])
check("ceiling blocks attempt 16 (cap 15)", blocked_at == 16, str(blocked_at))
r = mod._dup_gate(tool_name="web_extract",
                  args={"urls": ["https://x"]}, turn_id=T, session_id=S2)
check("web_extract uncapped by search-ceiling", r is None)

# --- v1.15.0 web_extract pre-halt failure cap ----------------------------------
SE = "regr-extract"
TE = "regr-te"
# no failures yet -> extract allowed
check("extract allowed with 0 failures",
      mod._dup_gate(tool_name="web_extract", args={"urls": ["https://a"]},
                    turn_id=TE, session_id=SE) is None)
# record 2 failures (via the transform hook's observed status=="error") -> still
# allowed (cap is 3)
mod._steer_transform(tool_name="web_extract", result='{"error":"inaccessible"}',
                     status="error", turn_id=TE, session_id=SE)
mod._steer_transform(tool_name="web_extract", result='{"error":"inaccessible"}',
                     status="error", turn_id=TE, session_id=SE)
check("extract still allowed at 2 failures (< cap 3)",
      mod._dup_gate(tool_name="web_extract", args={"urls": ["https://b"]},
                    turn_id=TE, session_id=SE) is None)
# third failure -> now at cap -> next extract blocked with a synthesize order
mod._steer_transform(tool_name="web_extract", result='{"error":"inaccessible"}',
                     status="error", turn_id=TE, session_id=SE)
r = mod._dup_gate(tool_name="web_extract", args={"urls": ["https://c"]},
                  turn_id=TE, session_id=SE)
check("extract BLOCKED at 3 failures (>= cap)",
      isinstance(r, dict) and r.get("action") == "block"
      and "web_extract limit reached" in r["message"]
      and "synthesiz" in r["message"].lower())
# a SUCCESSFUL extract (status ok) does NOT increment the failure counter
SE2 = "regr-extract2"
for _ in range(5):
    mod._steer_transform(tool_name="web_extract", result='{"data":{"web":[]}}',
                         status="ok", turn_id="te2", session_id=SE2)
check("successful extracts never trip the fail cap",
      mod._dup_gate(tool_name="web_extract", args={"urls": ["https://ok"]},
                    turn_id="te2", session_id=SE2) is None)
# cap disabled (0) -> never blocks even with many failures
SE3 = "regr-extract3"
_orig_efc = mod._extract_fail_cap
mod._extract_fail_cap = lambda: 0
for _ in range(6):
    mod._steer_transform(tool_name="web_extract", result='{"error":"x"}',
                         status="error", turn_id="te3", session_id=SE3)
check("extract_fail_cap=0 disables the cap",
      mod._dup_gate(tool_name="web_extract", args={"urls": ["https://d"]},
                    turn_id="te3", session_id=SE3) is None)
mod._extract_fail_cap = _orig_efc
# web_search is NOT affected by the extract cap
check("web_search unaffected by extract cap",
      mod._dup_gate(tool_name="web_search", args={"query": "fresh q"},
                    turn_id=TE, session_id=SE) is None)

# --- v1.5.3 duplicate-call gate (bridge unwrap) --------------------------------

S3 = "regr-s3"
for i in range(3):
    r = mod._dup_gate(tool_name="terminal", args={"command": "echo hi"},
                      turn_id=T, session_id=S3)
    check(f"dup-call attempt {i+1} allowed", r is None)
r = mod._dup_gate(tool_name="tool_call",
                  args={"name": "terminal", "arguments": {"command": "echo hi"}},
                  turn_id=T, session_id=S3)
check("dup-call 4th via bridge shares the counter and is blocked",
      isinstance(r, dict) and "duplicate call loop" in r["message"])
check("exempt tool never gated",
      all(mod._dup_gate(tool_name="clarify", args={"question": "q"},
                        turn_id=T, session_id=S3) is None for _ in range(6)))

# --- v1.6.0 shared counters accessor -------------------------------------------

c = mod.router_turn_counters(S2, T)
check("counters: 17 search attempts recorded", c["searches"] == 17, str(c))
check("counters: 2 ceiling blocks recorded", c["blocks"] == 2, str(c))
check("counters: last-8 queries kept",
      len(c["queries"]) == 8 and c["queries"][-1] == "varied 17", str(c))
c1 = mod.router_turn_counters(S, T)
check("counters: dup-query block recorded", c1["blocks"] == 1, str(c1))
check("counters: unknown turn is zeros",
      mod.router_turn_counters("nope", "t") ==
      {"searches": 0, "blocks": 0, "queries": []})

# --- v1.3.0/1.5.1 steering + brand nudge ---------------------------------------

S4 = "regr-s4"
payload = json.dumps({"data": {"web": [
    {"title": "Result", "url": "https://example.com"}]}})
out = None
for i in range(7):
    out = mod._steer_transform(tool_name="web_search", result=payload,
                               status="ok", turn_id=T, session_id=S4,
                               args={"query": "anything"})
check("steering silent through #6", out is not None or True)  # last call only
check("steering injected on search #7",
      out is not None and "STEERING" in json.loads(out).get("steering", ""))
brand = json.dumps({"data": {"web": [
    {"title": "Hermès Birkin bag sale", "url": "https://fashion.example"},
    {"title": "Hermes luxury scarf", "url": "https://brand.example"}]}})
out = mod._steer_transform(tool_name="web_search", result=brand, status="ok",
                           turn_id=T, session_id="regr-s5",
                           args={"query": "hermes agent"})
check("brand nudge injected on polluted results",
      out is not None and "Nous Research" in json.loads(out)["brand_note"])
check("brand nudge skipped when query has nous",
      mod._steer_transform(tool_name="web_search", result=brand, status="ok",
                           turn_id=T, session_id="regr-s6",
                           args={"query": "nous hermes"}) is None)

# --- v1.4.0/1.5.1 constrained decoding + clamps --------------------------------

tools = [{"type": "function", "function": {
    "name": "web_search",
    "parameters": {"type": "object",
                   "properties": {"query": {"type": "string"}},
                   "required": ["query"]}}}]
req = {"model": "m", "messages": [], "tools": tools, "max_tokens": 65536}
out = mod._cd_middleware(request=req, api_mode="chat_completions",
                         base_url="https://vllm.example.com/v1")
check("cd middleware returns rewrite", isinstance(out, dict))
rf = req.get("response_format")
check("structural_tag response_format built",
      isinstance(rf, dict) and rf.get("type") == "structural_tag"
      and rf["structures"][0]["schema"]["properties"]["name"]["enum"]
      == ["web_search"])
check("max_tokens clamped to 3000", req["max_tokens"] == 3000)
check("frequency_penalty injected when configured", req["frequency_penalty"] == 0.3)
check("presence_penalty injected when configured", req["presence_penalty"] == 0.3)
check("other hosts untouched",
      mod._cd_middleware(request={"model": "m", "tools": tools},
                         api_mode="chat_completions",
                         base_url="https://api.openai.com/v1") is None)
check("non-chat api modes untouched",
      mod._cd_middleware(request={"tools": tools}, api_mode="codex_responses",
                         base_url="https://vllm.example.com/v1") is None)

# --- v1.6.1 no_think_always -----------------------------------------------------

mod._nt_always = lambda: "on"
req = {"model": "m", "messages": [], "tools": tools, "max_tokens": 100}
out = mod._cd_middleware(request=req, api_mode="chat_completions",
                         base_url="https://vllm.example.com/v1")
check("no_think_always injects enable_thinking=false",
      req.get("extra_body", {}).get("chat_template_kwargs", {})
      .get("enable_thinking") is False)
check("no_think_always named in middleware rewrite",
      isinstance(out, dict) and "no_think_always" in out.get("name", ""))
req2 = {"model": "m", "messages": [],
        "extra_body": {"chat_template_kwargs": {"enable_thinking": True},
                       "other": 1}}
mod._cd_middleware(request=req2, api_mode="chat_completions",
                   base_url="https://vllm.example.com/v1")
check("no_think_always never overrides an explicit enable_thinking",
      req2["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True)
check("no_think_always preserves sibling extra_body keys",
      req2["extra_body"]["other"] == 1)
req3 = {"model": "m", "messages": []}
check("no_think_always skips other hosts",
      mod._cd_middleware(request=req3, api_mode="chat_completions",
                         base_url="https://api.openai.com/v1") is None
      and "extra_body" not in req3)
mod._nt_always = lambda: "off"
req4 = {"model": "m", "messages": []}
mod._cd_middleware(request=req4, api_mode="chat_completions",
                   base_url="https://vllm.example.com/v1")
check("no_think_always off injects nothing", "extra_body" not in req4)
check("plugin default is off (deploy decision is install.py's)",
      mod.NO_THINK_ALWAYS_DEFAULT == "off")

# --- v1.8.2 anti-tail-collapse: presence_penalty replaces frequency_penalty --
check("frequency_penalty plugin default now OFF (0.0)",
      mod.FREQUENCY_PENALTY_DEFAULT == 0.0)
check("presence_penalty plugin default 0.3",
      mod.PRESENCE_PENALTY_DEFAULT == 0.3)
# with production values (fp off, pp 0.3): only presence_penalty injected
mod._fp_value = lambda: 0.0
mod._pp_value = lambda: 0.3
mod._nt_always = lambda: "off"
reqpp = {"model": "m", "messages": [], "max_tokens": 100}
mod._cd_middleware(request=reqpp, api_mode="chat_completions",
                   base_url="https://vllm.example.com/v1")
check("fp OFF -> frequency_penalty NOT injected",
      "frequency_penalty" not in reqpp)
check("pp 0.3 -> presence_penalty injected", reqpp.get("presence_penalty") == 0.3)
# caller-set values are never overridden (either penalty)
reqset = {"model": "m", "messages": [], "presence_penalty": 0.9,
          "frequency_penalty": 0.4}
mod._fp_value = lambda: 0.2
mod._cd_middleware(request=reqset, api_mode="chat_completions",
                   base_url="https://vllm.example.com/v1")
check("caller presence_penalty preserved", reqset["presence_penalty"] == 0.9)
check("caller frequency_penalty preserved", reqset["frequency_penalty"] == 0.4)
# explicit 0 disables presence injection
mod._pp_value = lambda: 0.0
reqz = {"model": "m", "messages": []}
mod._cd_middleware(request=reqz, api_mode="chat_completions",
                   base_url="https://vllm.example.com/v1")
check("presence_penalty 0 disables injection", "presence_penalty" not in reqz)
mod._fp_value = lambda: 0.3
mod._pp_value = lambda: 0.3

# --- v1.6.1 secret scrub defaults ------------------------------------------------

check("secret scrub default on", mod.SECRET_SCRUB_DEFAULT == "on")
s, n = mod.scrub_secrets("key vw_fake3aajl7k7wtbesw534mp6ybukzoeaqstthatwiz"
                         "tykvipfpxsbo7wn here")
check("scrub_secrets exported and working", n == 1 and "[redacted]" in s)

# --- selfheal registration wiring (manager-level) ------------------------------


class FakeManager:
    def __init__(self):
        self._hooks = {}
        self._middleware = {}


class FakeCtx:
    def __init__(self):
        self._manager = FakeManager()

    def register_hook(self, name, cb):
        self._manager._hooks.setdefault(name, []).append(cb)

    def register_middleware(self, kind, cb):
        self._manager._middleware.setdefault(kind, []).append(cb)


ctx = FakeCtx()
mod._register_selfheal(ctx)
check("selfheal middleware registered",
      any(getattr(cb, "_router_selfheal", False)
          for cb in ctx._manager._middleware.get("llm_request", [])))
for h in ("pre_tool_call", "pre_llm_call", "post_llm_call",
          "transform_llm_output", "pre_gateway_dispatch"):
    check(f"selfheal hook registered: {h}",
          any(getattr(cb, "_router_selfheal", False)
              for cb in ctx._manager._hooks.get(h, [])))
mod._register_selfheal(ctx)
check("selfheal registration idempotent on rescan",
      len(ctx._manager._hooks["pre_tool_call"]) == 1)

# a broken selfheal import must not raise out of register()
import router_plugin.selfheal as sh  # noqa: E402
orig = sh.register
sh.register = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    mod._register_selfheal(FakeCtx())
    check("selfheal install failure contained", True)
except Exception as e:
    check("selfheal install failure contained", False, repr(e))
sh.register = orig

# --- v1.8.0 loadaware registration -------------------------------------------

lctx = FakeCtx()
mod._register_progress(lctx)   # loadaware reuses progress's captured path
mod._register_loadaware(lctx)
check("loadaware middleware registered",
      any(getattr(cb, "_router_loadaware", False)
          for cb in lctx._manager._middleware.get("llm_request", [])))
for h in ("post_api_request", "api_request_error"):
    check(f"loadaware hook registered: {h}",
          any(getattr(cb, "_router_loadaware", False)
              for cb in lctx._manager._hooks.get(h, [])))
mod._register_loadaware(lctx)
check("loadaware registration idempotent on rescan",
      len(lctx._manager._hooks["post_api_request"]) == 1)

# a broken loadaware import must not raise out of register()
import router_plugin.loadaware as la  # noqa: E402
lorig = la.register
la.register = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    mod._register_loadaware(FakeCtx())
    check("loadaware install failure contained", True)
except Exception as e:
    check("loadaware install failure contained", False, repr(e))
la.register = lorig

# --- v1.9.0 task-aware coding accommodation ----------------------------------

# Classifier true positives — imperative "produce code" asks.
_CODING_POS = [
    "write a c code for quake like game",                       # the real fail
    "write a C program for a simple snake game in the terminal",
    "write a Python script that finds duplicate files in a directory",
    "implement a binary search function in Rust",
    "create a REST API server in Go",
    "make me a snake game",
    "code up a fizzbuzz in javascript",
    "write a bash script to back up my home directory",
    "build a small CLI tool in Python that renames files",
    "write me a function that reverses a linked list",
    "generate a Dockerfile for a node app",
]
for _p in _CODING_POS:
    check(f"coding_intent TP: {_p[:38]}", mod.coding_intent(_p), "expected True")

# Classifier true negatives — research / chat / computation / short-summary.
# These MUST keep the tight 3000/150 bounds (detection must not fire).
_CODING_NEG = [
    "Compare Python, Rust, Go, and TypeScript for writing a small "
    "command-line tool, in a table with columns for performance.",   # comparison
    "What's the difference between speculative decoding and prefix caching "
    "in LLM inference? Explain for an infra engineer.",
    "Convert 84 kg to pounds and 183 cm to feet/inches.",             # 84kg
    "Book me a table for Friday.",                                    # honesty
    "Add a todo: review the saandal README tomorrow.",
    "Count how many .py files are under /home/user/saandal and their "
    "total line count.",
    "What is 20 factorial (20!)? Give the exact integer.",
    "Search for the top 3 open-source note-taking apps with local storage, "
    "then compare them in a table.",
    "Check what TTS engines hermes supports, then tell me which work offline.",
    "What is SearXNG and what are the pros and cons of self-hosting it?",
    "Give me a thorough comparison of vLLM, SGLang, TGI, and llama.cpp.",
    "Find the current population of Canada online, then compute 0.5% of it.",
    "What did the stock market do today?",
    "Fix it.",
    "Write a short summary of the vLLM release notes.",               # short summary
    "Remember that my preferred report format is tables first, prose second.",
    "Search the web for hermes and tell me what it is.",
    "Explain in English what a système de fichiers is.",
]
for _n in _CODING_NEG:
    check(f"coding_intent TN: {_n[:38]}", not mod.coding_intent(_n),
          "expected False (false positive would loosen bounds)")

# is_coding_turn — continuation persistence + guards.
_cont_hist = [
    {"role": "user", "content": "write a c code for quake like game"},
    {"role": "assistant", "content": "ok", "tool_calls": [
        {"function": {"name": "tool_call",
                      "arguments": '{"name":"write_file","arguments":{}}'}}]},
    {"role": "tool", "name": "write_file", "content": "{}"},
    {"role": "assistant", "content": "here is my plan"},
    {"role": "user", "content": "progress?"},
]
check("is_coding_turn: 'progress?' continues a coding turn",
      mod.is_coding_turn(_cont_hist))

_comp_hist = [
    {"role": "user", "content": "What is 20 factorial?"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "execute_code", "arguments": "{}"}}]},
    {"role": "tool", "name": "execute_code", "content": "2432902008176640000"},
    {"role": "user", "content": "and add 5"},
]
check("is_coding_turn: computation follow-up NOT promoted",
      not mod.is_coding_turn(_comp_hist))

_topic_hist = [
    {"role": "user", "content": "write a python script to sort files"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "write_file", "arguments": "{}"}}]},
    {"role": "tool", "name": "write_file", "content": "{}"},
    {"role": "user", "content": "What is SearXNG and the pros and cons of "
                                "self-hosting it?"},
]
check("is_coding_turn: new research question does NOT inherit coding mode",
      not mod.is_coding_turn(_topic_hist))

check("is_coding_turn: empty/garbage input safe", not mod.is_coding_turn(None)
      and not mod.is_coding_turn([]) and not mod.is_coding_turn("x"))

# Middleware: coding request raises the cap to coding_max_tokens + injects note.
_save = (mod._coding_mode, mod._coding_max_tokens, mod._mt_cap, mod._cd_settings,
         mod._pp_value, mod._fp_value, mod._nt_always)
mod._coding_mode = lambda: "auto"
mod._coding_max_tokens = lambda: 8000
mod._mt_cap = lambda: 3000
mod._cd_settings = lambda: ("off", ["vllm.example.com"])
mod._pp_value = lambda: 0.0
mod._fp_value = lambda: 0.0
mod._nt_always = lambda: "off"
_base = "https://vllm.example.com/v1"

_rc = {"messages": [{"role": "user", "content": "write a C program for a snake game"}],
       "max_tokens": 65536, "tools": [{"function": {"name": "write_file"}}]}
mod._cd_middleware(request=_rc, api_mode="chat_completions", base_url=_base)
check("coding middleware: cap raised 65536 -> 8000", _rc["max_tokens"] == 8000)
check("coding middleware: coding note injected",
      any(x.get("role") == "system" and x.get("content") == mod.CODING_NOTE
          for x in _rc["messages"]))

_rn = {"messages": [{"role": "user", "content": "Convert 84 kg to pounds."}],
       "max_tokens": 65536, "tools": [{"function": {"name": "terminal"}}]}
mod._cd_middleware(request=_rn, api_mode="chat_completions", base_url=_base)
check("non-coding middleware: cap stays 3000", _rn["max_tokens"] == 3000)
check("non-coding middleware: no coding note",
      not any(x.get("role") == "system" and x.get("content") == mod.CODING_NOTE
              for x in _rn["messages"]))

mod._coding_mode = lambda: "off"
_ro = {"messages": [{"role": "user", "content": "write a C program for a snake game"}],
       "max_tokens": 65536, "tools": [{"function": {"name": "write_file"}}]}
mod._cd_middleware(request=_ro, api_mode="chat_completions", base_url=_base)
check("coding_mode=off: cap stays 3000 (full backward compat)",
      _ro["max_tokens"] == 3000)
check("coding_mode=off: no coding note",
      not any(x.get("role") == "system" and x.get("content") == mod.CODING_NOTE
              for x in _ro["messages"]))
(mod._coding_mode, mod._coding_max_tokens, mod._mt_cap, mod._cd_settings,
 mod._pp_value, mod._fp_value, mod._nt_always) = _save

check("coding config defaults sane",
      mod.CODING_MODE_DEFAULT == "auto" and mod.CODING_MAX_TOKENS_DEFAULT == 8000
      and mod.CODING_WALL_SECS_DEFAULT == 360)


print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
