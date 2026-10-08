#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Podwarden Inc

"""Unit tests for the v1.19.0 clarify-finalize + tier-independent runaway wall
(plugin/__init__.py: _clarify_finalize_gate, _note_clarify_flagged, and the
_antifab_middleware flagged-recording wiring).

Invariants under test:
  * clarify-finalize fires ONLY on a clarify_guard-FLAGGED turn AND ONLY when the
    model actually invokes the `clarify` tool — it finalizes (blocks clarify +
    every later tool call). It must NEVER touch an unflagged turn (a clear prompt
    or a normal multi-tool turn), preserving clarify_guard's 0-FP property.
  * the runaway wall is tier-INDEPENDENT and fires only past a high call-count /
    wall-clock, and is inert below.
  * both flags roll back to a pure no-op; every path is fail-safe.
Exit non-zero on any failure.
"""
import importlib.util
import sys
import time
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

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"ok   {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


def bridge_call(inner_name, inner_args):
    """Build a tool_call-bridge wrapper so we also exercise _dup_unwrap."""
    return ("tool_call", {"name": inner_name, "arguments": inner_args})


# Deterministic flag control (config isn't available in the offline harness).
_ALL_ON = {"clarify_finalize": "on", "runaway_wall": "on"}


def set_flags(mapping):
    plugin._ts_flag = lambda k, d="off": mapping.get(k, d)


def set_caps(call_cap=50, wall_secs=480):
    plugin._cf_int = lambda k, d: {"runaway_call_cap": call_cap,
                                   "runaway_wall_secs": wall_secs}.get(k, d)


_saved_flag = plugin._ts_flag
_saved_int = plugin._cf_int
try:
    # ======================================================================
    # 1. clarify-finalize: FLAGGED turn + clarify invoked -> finalize
    # ======================================================================
    set_flags(_ALL_ON)
    set_caps()  # high, so the runaway wall never interferes here
    plugin._cf_reset_state()
    sid, tid = "s1", "t1"
    plugin._note_clarify_flagged(sid, tid)
    check("flagged turn is recorded",
          plugin._cf_key(sid, tid) in plugin._clarify_flagged)

    r = plugin._clarify_finalize_gate(
        tool_name="clarify",
        args={"question": "What exactly should I finish?"},
        turn_id=tid, session_id=sid)
    check("clarify on a flagged turn is BLOCKED (finalize)",
          isinstance(r, dict) and r.get("action") == "block")
    check("finalize message echoes the clarifying question",
          isinstance(r, dict) and "What exactly should I finish?" in r["message"])
    check("the turn is now ARMED",
          plugin._cf_key(sid, tid) in plugin._clarify_armed)

    # any further tool call on the armed turn is blocked
    r2 = plugin._clarify_finalize_gate(
        tool_name="session_search", args={"query": "prior work"},
        turn_id=tid, session_id=sid)
    check("further tool call on an armed turn is BLOCKED",
          isinstance(r2, dict) and r2.get("action") == "block")
    r3 = plugin._clarify_finalize_gate(
        tool_name="terminal", args={"command": "ls"},
        turn_id=tid, session_id=sid)
    check("a second further tool call on an armed turn is BLOCKED",
          isinstance(r3, dict) and r3.get("action") == "block")

    # bridge-wrapped clarify also finalizes (unwrap path)
    plugin._cf_reset_state()
    plugin._note_clarify_flagged("s1b", "t1b")
    bn, ba = bridge_call("clarify", {"question": "Which task did you mean?"})
    rb = plugin._clarify_finalize_gate(tool_name=bn, args=ba,
                                       turn_id="t1b", session_id="s1b")
    check("bridge-wrapped clarify on a flagged turn finalizes",
          isinstance(rb, dict) and rb.get("action") == "block"
          and "Which task did you mean?" in rb["message"])

    # ======================================================================
    # 2. UNFLAGGED turns are NEVER touched (0-FP preservation)
    # ======================================================================
    plugin._cf_reset_state()
    # clarify called on an UNFLAGGED turn (a genuine multi-turn "fix it", or a
    # normal turn where the model legitimately clarifies) — must pass through.
    r_unflagged = plugin._clarify_finalize_gate(
        tool_name="clarify", args={"question": "prod or staging?"},
        turn_id="t2", session_id="s2")
    check("clarify on an UNFLAGGED turn is NOT touched (allow)",
          r_unflagged is None)
    check("an unflagged clarify does NOT arm the turn",
          plugin._cf_key("s2", "t2") not in plugin._clarify_armed)
    # a normal multi-tool turn (non-clarify tools) on an unflagged turn: allow
    for tool in ("web_search", "read_file", "terminal", "todo"):
        rr = plugin._clarify_finalize_gate(
            tool_name=tool, args={"x": 1}, turn_id="t2", session_id="s2")
        check(f"normal multi-tool turn: {tool} allowed (unflagged)", rr is None)

    # ======================================================================
    # 3. tier-independent runaway wall
    # ======================================================================
    plugin._cf_reset_state()
    set_caps(call_cap=5, wall_secs=99999)  # trip on call count only
    sid, tid = "s3", "t3"
    results = []
    for i in range(7):
        results.append(plugin._clarify_finalize_gate(
            tool_name="web_search", args={"query": f"q{i}"},
            turn_id=tid, session_id=sid))
    # first 5 allowed (<= cap), the 6th+ blocked (attempts 6,7 > cap 5)
    check("runaway wall inert at/below the call cap",
          all(x is None for x in results[:5]))
    check("runaway wall fires past the call cap",
          isinstance(results[5], dict) and results[5].get("action") == "block"
          and isinstance(results[6], dict))

    # wall-clock trip (call cap high, wall = 1s; seed an old first_ts so elapsed
    # exceeds it — a negative/zero wall is treated as DISABLED by design).
    plugin._cf_reset_state()
    set_caps(call_cap=99999, wall_secs=1)
    key3b = plugin._cf_key("s3b", "t3b")
    plugin._runaway_state[key3b] = [time.time() - 30, 3, False]  # 30s elapsed
    r_wall = plugin._clarify_finalize_gate(
        tool_name="web_search", args={"query": "x"},
        turn_id="t3b", session_id="s3b")
    check("runaway wall fires on wall-clock breach",
          isinstance(r_wall, dict) and r_wall.get("action") == "block")
    # a fresh turn (no elapsed time) is NOT tripped by the wall
    plugin._cf_reset_state()
    set_caps(call_cap=99999, wall_secs=480)
    check("runaway wall inert on a fresh turn (no elapsed, low call count)",
          plugin._clarify_finalize_gate(
              tool_name="web_search", args={"query": "x"},
              turn_id="t3c", session_id="s3c") is None)

    # runaway wall is tier-independent: no tier resolution involved, fires the
    # same regardless of session — demonstrated by a fresh unrelated session.
    plugin._cf_reset_state()
    set_caps(call_cap=2, wall_secs=99999)
    for sess in ("weakish", "strongish"):
        outs = [plugin._clarify_finalize_gate(
            tool_name="terminal", args={"c": i}, turn_id="tt",
            session_id=sess) for i in range(4)]
        check(f"runaway wall tier-independent (session={sess})",
              outs[0] is None and outs[1] is None
              and isinstance(outs[2], dict))

    # ======================================================================
    # 4. rollback: both flags off => pure no-op
    # ======================================================================
    plugin._cf_reset_state()
    set_flags({"clarify_finalize": "off", "runaway_wall": "off"})
    set_caps(call_cap=1, wall_secs=-1)  # would trip if the wall were on
    plugin._note_clarify_flagged("s4", "t4")
    check("clarify_finalize off => clarify on a flagged turn is NOT blocked",
          plugin._clarify_finalize_gate(
              tool_name="clarify", args={"question": "q"},
              turn_id="t4", session_id="s4") is None)
    check("runaway_wall off => no block even past caps",
          all(plugin._clarify_finalize_gate(
              tool_name="web_search", args={"query": str(i)},
              turn_id="t4", session_id="s4") is None for i in range(5)))

    # clarify_finalize off but runaway_wall on: clarify passes, wall still guards
    plugin._cf_reset_state()
    set_flags({"clarify_finalize": "off", "runaway_wall": "on"})
    set_caps(call_cap=2, wall_secs=99999)
    plugin._note_clarify_flagged("s5", "t5")
    check("clarify_finalize off: clarify not finalized even if flagged",
          plugin._clarify_finalize_gate(
              tool_name="clarify", args={"question": "q"},
              turn_id="t5", session_id="s5") is None)

    # ======================================================================
    # 5. fail-safe passthrough on junk
    # ======================================================================
    set_flags(_ALL_ON)
    set_caps()
    check("fail-safe: empty tool name => allow",
          plugin._clarify_finalize_gate(tool_name="", args=None,
                                        turn_id="t", session_id="s") is None)
    check("fail-safe: None args => allow (no crash)",
          plugin._clarify_finalize_gate(tool_name="web_search", args=None,
                                        turn_id="t9", session_id="s9") is None)

    # ======================================================================
    # 6. middleware records the flagged turn when clarify_guard fires
    # ======================================================================
    plugin._cf_reset_state()
    _saved_mw_flag = plugin._ts_flag
    plugin._ts_flag = lambda k, d="off": "on" if k == "clarify_guard" else "off"
    try:
        res = plugin._antifab_middleware(
            request={"messages": [{"role": "user", "content": "Fix it."}]},
            api_mode="chat_completions", base_url="https://api.deepseek.com",
            session_id="msid", turn_id="mtid")
        check("middleware applied clarify_guard",
              isinstance(res, dict) and "clarify_guard" in (res.get("name") or ""))
        check("middleware RECORDED the flagged turn",
              plugin._cf_key("msid", "mtid") in plugin._clarify_flagged)
        # a CLEAR prompt does NOT flag the turn
        r_clear = plugin._antifab_middleware(
            request={"messages": [{"role": "user",
                                   "content": "convert 84 kg to pounds"}]},
            api_mode="chat_completions", base_url="https://api.deepseek.com",
            session_id="csid", turn_id="ctid")
        check("middleware does NOT flag a clear prompt",
              r_clear is None
              and plugin._cf_key("csid", "ctid") not in plugin._clarify_flagged)
    finally:
        plugin._ts_flag = _saved_mw_flag
finally:
    plugin._ts_flag = _saved_flag
    plugin._cf_int = _saved_int
    plugin._cf_reset_state()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
