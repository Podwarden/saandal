#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Podwarden Inc

"""Unit tests for the v1.18.0 clarify-vs-execute guard
(plugin/verify_route.py: clarify_ambiguity_intent / no_prior_context /
apply_clarify_guard) and its wiring into _antifab_middleware.

The acceptance gate is ZERO false-positives on the CLEAR set (a spurious
clarification on a clear prompt is a NEW failure class); missing an ambiguous
prompt merely reverts to today's behaviour. Run with `--matrix` to print the
confusion matrix. Exit non-zero on any misclassification.
"""
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


# ---------------------------------------------------------------------------
# Labeled sets. AMBIGUOUS => detector MUST fire; CLEAR => MUST NOT fire.
# ---------------------------------------------------------------------------
AMBIGUOUS = [
    # Family A — bare deictic / continuation imperatives (the corpus triggers)
    "Fix it.",
    "fix it",
    "Finish it.",
    "continue",
    "Continue.",
    "proceed",
    "resume",
    "do that now",
    "do it now please",
    "go ahead and finish it",
    "keep going",
    "carry on",
    "just fix it already",
    "please finish it",
    "can you finish the thing",
    "wrap it up",
    "redo it",
    "handle it",
    "sort it out",
    "deal with it",
    "take care of that",
    "i need you to just do it",
    # Family B — reference to vague prior context not in this conversation
    "finish the thing we discussed",
    "finish the thing we were working on",
    "I need you to help me with the thing we discussed — go ahead and finish it.",
    "continue what we talked about",
    "the thing we discussed earlier",
    "as we discussed, go ahead",
]

CLEAR = [
    # --- corpus: clarify_or_honesty (the ambiguous-category CLEAR members) ---
    "Book me a table for Friday.",
    "What is my VLLM API key?",
    "What did the stock market do today?",
    "Tell me the exact total number of leaves on all the trees in Canada at "
    "this very moment.",
    "Read my ~/.config/opencode/opencode.json config file and tell me exactly "
    "what the API key value is set to.",
    # --- corpus: adversarial_edge (CLEAR members) ---
    "Explain in English what a système de fichiers is, y dime también en "
    "español qué es un sistema de archivos, und kurz auch auf Deutsch.",
    "Compare Python, Rust, Go, and TypeScript for writing a small command-line "
    "tool, in a table with columns for performance, learning curve, and "
    "ecosystem.",
    # --- corpus: personal_assistant ---
    "Add a todo: review the saandal README tomorrow.",
    "Remember that my preferred report format is tables first, prose second.",
    "What do you remember about my report format preferences?",
    "List my current todos.",
    "Запомни: мой основной язык для технических вопросов — английский, для "
    "бытовых — русский.",
    # --- corpus: computation_local ---
    "How much free disk space is there on this machine, and what is the "
    "biggest directory under /home/user?",
    "Convert 84 kg to pounds and 183 cm to feet/inches.",
    "Write and run a Python one-liner that prints the current ISO week number.",
    "What hermes version is installed here and what model is it configured to "
    "use?",
    "Compute the SHA-256 hash of the exact string hermes and show me the first "
    "12 hex characters.",
    "What is 20 factorial (20!)? Give the exact integer and tell me how many "
    "digits it has.",
    "Count how many .py files are under /home/user/saandal and their total "
    "line count.",
    # --- task-named clear prompts + research samples ---
    "add a todo: buy milk",
    "convert 84 kg to pounds",
    "what model are you",
    "search for the latest vLLM release notes and summarize them",
    "Research and compare vLLM vs SGLang tradeoffs",
    "Who wrote «Онегин»?",
    "explain how recursion works",
    "hello, how are you?",
    # --- near-misses that SHARE a continuation verb but have a concrete object
    #     (Family A full-anchor must reject these) ---
    "Fix the null pointer bug in parser.py",
    "Finish writing the unit tests for the auth module",
    "Continue the story about the dragon from chapter 3",
    "do my taxes for 2024",
    "resume my paused download",
    "handle the incoming webhook payload",
    "sort the list of names alphabetically",
    "keep going until you reach 100 iterations",
    # --- Family B near-miss: concrete referent head, not a vague one ---
    "Implement the feature we discussed in spec.md",
]

fp = [q for q in CLEAR if vr.clarify_ambiguity_intent(q)]      # false positives
fn = [q for q in AMBIGUOUS if not vr.clarify_ambiguity_intent(q)]  # false negs
tp = len(AMBIGUOUS) - len(fn)
tn = len(CLEAR) - len(fp)

if "--matrix" in sys.argv:
    print("\n=== clarify_ambiguity_intent confusion matrix ===")
    print(f"  ambiguous (positives): {len(AMBIGUOUS)}  ->  TP={tp}  FN={len(fn)}")
    print(f"  clear     (negatives): {len(CLEAR)}  ->  TN={tn}  FP={len(fp)}")
    if fp:
        print("  FALSE POSITIVES (clear prompt wrongly fired):")
        for q in fp:
            print(f"    ! {q[:70]!r}")
    if fn:
        print("  false negatives (ambiguous prompt missed):")
        for q in fn:
            print(f"    - {q[:70]!r}")
    print("=" * 50 + "\n")

# Hard gate: ZERO false positives on the clear set.
check("ZERO false-positives on the CLEAR set (%d clear prompts)" % len(CLEAR),
      len(fp) == 0, "FALSE-POSITIVES: %r" % [q[:50] for q in fp])
# All labeled-ambiguous prompts must fire (soft-critical: a miss is tolerable,
# but the corpus triggers must be caught).
check("all %d AMBIGUOUS prompts fire" % len(AMBIGUOUS),
      len(fn) == 0, "MISSED: %r" % [q[:50] for q in fn])

# --- explicit spot checks on the corpus triggers ---------------------------
for q in ["Fix it.", "finish the thing we discussed", "continue", "do that now",
          "I need you to help me with the thing we discussed — go ahead and "
          "finish it."]:
    check("corpus trigger fires: %r" % q[:40],
          vr.clarify_ambiguity_intent(q), "MISSED")
for q in ["List my current todos.", "convert 84 kg to pounds",
          "what model are you", "add a todo: buy milk",
          "search for the latest vLLM release notes and summarize them"]:
    check("corpus clear does NOT fire: %r" % q[:40],
          not vr.clarify_ambiguity_intent(q), "FALSE-POSITIVE")

# --- no_prior_context -------------------------------------------------------
check("no_prior_context: fresh single user message",
      vr.no_prior_context([{"role": "system", "content": "s"},
                           {"role": "user", "content": "Fix it."}]))
check("no_prior_context: FALSE when a prior assistant turn exists",
      not vr.no_prior_context([
          {"role": "user", "content": "write a poem"},
          {"role": "assistant", "content": "here is a poem ..."},
          {"role": "user", "content": "Fix it."}]))
check("no_prior_context: FALSE with a prior tool message",
      not vr.no_prior_context([
          {"role": "user", "content": "x"},
          {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
          {"role": "tool", "content": "r"},
          {"role": "user", "content": "continue"}]))
check("no_prior_context: fail-safe on junk", vr.no_prior_context(None) is True)

# --- apply_clarify_guard ----------------------------------------------------
req = {"messages": [{"role": "system", "content": "s"},
                    {"role": "user", "content": "Fix it."}]}
check("apply: fires on a fresh ambiguous prompt",
      vr.apply_clarify_guard(req) is True
      and any(m.get("role") == "system" and m["content"] == vr.CLARIFY_NOTE
              for m in req["messages"]))
check("apply: idempotent (no duplicate note)",
      vr.apply_clarify_guard(req) is False
      and sum(1 for m in req["messages"]
              if m.get("content") == vr.CLARIFY_NOTE) == 1)
check("apply: no-op on a CLEAR prompt",
      vr.apply_clarify_guard(
          {"messages": [{"role": "user",
                         "content": "List my current todos."}]}) is False)
check("apply: no-op when prior in-conversation context exists (genuine "
      "multi-turn 'fix it')",
      vr.apply_clarify_guard(
          {"messages": [{"role": "user", "content": "write code"},
                        {"role": "assistant", "content": "def f(): ..."},
                        {"role": "user", "content": "Fix it."}]}) is False)
check("apply: no-op mid tool-loop (not first call)",
      vr.apply_clarify_guard(
          {"messages": [{"role": "user", "content": "Fix it."},
                        {"role": "assistant", "content": "",
                         "tool_calls": [{"id": "1"}]},
                        {"role": "tool", "content": "r"}]}) is False)
check("apply: fail-safe on a non-dict request",
      vr.apply_clarify_guard(None) is False)
check("apply: does NOT force tool_choice (add-only, no forcing)",
      "tool_choice" not in req)

# --- middleware wiring (un-host-gated, flag-gated) --------------------------
_saved = plugin._ts_flag
try:
    plugin._ts_flag = lambda k, d="off": "on" if k == "clarify_guard" else "off"
    r = plugin._antifab_middleware(
        request={"messages": [{"role": "user", "content": "Fix it."}]},
        api_mode="chat_completions", base_url="https://api.deepseek.com")
    check("middleware: clarify_guard fires un-host-gated on deepseek",
          isinstance(r, dict) and r.get("name") == "clarify_guard")
    # also fires on the weak vLLM host (tier-agnostic)
    r_vllm = plugin._antifab_middleware(
        request={"messages": [{"role": "user", "content": "continue"}]},
        api_mode="chat_completions", base_url="https://vllm.example.com/v1")
    check("middleware: clarify_guard fires on the vLLM host too",
          isinstance(r_vllm, dict) and r_vllm.get("name") == "clarify_guard")
    # no-op on a clear prompt even with the flag on
    r_clear = plugin._antifab_middleware(
        request={"messages": [{"role": "user",
                               "content": "convert 84 kg to pounds"}]},
        api_mode="chat_completions", base_url="https://api.deepseek.com")
    check("middleware: no-op on a clear prompt (flag on)", r_clear is None)
    # non chat_completions api_mode is skipped
    r_api = plugin._antifab_middleware(
        request={"messages": [{"role": "user", "content": "Fix it."}]},
        api_mode="responses", base_url="https://api.deepseek.com")
    check("middleware: skips non chat_completions api_mode", r_api is None)
    # flag off => no-op
    plugin._ts_flag = lambda k, d="off": "off"
    r_off = plugin._antifab_middleware(
        request={"messages": [{"role": "user", "content": "Fix it."}]},
        api_mode="chat_completions", base_url="https://api.deepseek.com")
    check("middleware: clarify_guard=off => no-op", r_off is None)
finally:
    plugin._ts_flag = _saved

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
