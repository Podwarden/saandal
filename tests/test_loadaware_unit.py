#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Podwarden Inc

"""Unit tests for plugin/loadaware.py — the pure load classifier
(HEALTHY/SLOW/STALLED over synthetic latency sequences), the honest-note
builders, timeout detection, the throttle/cap policy, window maintenance,
the observer hooks (host gating), and the read-only middleware's fail-safety
+ progress coordination (no duplicate/stacked sends) against a fake adapter.

All pure; no live server, no network. Run: tests/test_loadaware_unit.py
"""
import importlib.util
import sys
import time
import weakref
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
import router_plugin.loadaware as la      # noqa: E402
import router_plugin.progress as pg       # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"ok   {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


# Pin config-independent behavior: fixed cfg + host allowlist so the suite
# never depends on the operator's config.yaml (same discipline as the
# regression suite). platforms empty = deliver on all platforms in tests.
CFG = dict(la.DEFAULTS)
CFG["platforms"] = []
la._cfg = lambda: dict(CFG, platforms=[])
la._allow_hosts = lambda: ["vllm.example.com"]
NOW = 10_000.0
OK_URL = "https://vllm.example.com/v1"


def ev(latency, outcome, ts=NOW):
    return {"ts": ts, "latency": latency, "outcome": outcome}


# --- classify_load: HEALTHY ---------------------------------------------------

check("healthy: fast completions",
      la.classify_load([ev(3, "ok")] * 5, CFG, NOW)[0] == "HEALTHY")
check("healthy: too few samples stays HEALTHY",
      la.classify_load([ev(120, "timeout")] * 2, CFG, NOW)[0] == "HEALTHY",
      "min_samples=3 guard")
check("healthy: empty window",
      la.classify_load([], CFG, NOW)[0] == "HEALTHY")

# --- classify_load: SLOW ------------------------------------------------------

check("slow: elevated median latency",
      la.classify_load([ev(30, "ok")] * 5, CFG, NOW)[0] == "SLOW")
check("slow: half of completions slow (slow_frac)",
      la.classify_load([ev(30, "ok"), ev(30, "ok"), ev(30, "ok"),
                        ev(2, "ok"), ev(2, "ok")], CFG, NOW)[0] == "SLOW")
check("slow: climbing trend (older fast, newer slow)",
      la.classify_load([ev(4, "ok")] * 3 + [ev(18, "ok")] * 3,
                       CFG, NOW)[0] == "SLOW")
check("slow: mixed — some complete, some time out",
      la.classify_load([ev(5, "ok"), ev(5, "ok"), ev(120, "timeout")],
                       CFG, NOW)[0] == "SLOW")
check("slow: isolated timeout below the stalled streak",
      la.classify_load([ev(5, "ok"), ev(120, "timeout"), ev(5, "ok")],
                       CFG, NOW)[0] == "SLOW")

# --- classify_load: STALLED ---------------------------------------------------

check("stalled: trailing 2 timeouts, no completion in tail",
      la.classify_load([ev(5, "ok"), ev(120, "timeout"), ev(120, "timeout")],
                       CFG, NOW)[0] == "STALLED")
check("stalled: trailing timeout+error (timeout present)",
      la.classify_load([ev(5, "ok"), ev(120, "timeout"), ev(0, "error")],
                       CFG, NOW)[0] == "STALLED")
check("NOT stalled: trailing errors with NO timeout -> SLOW/HEALTHY not STALLED",
      la.classify_load([ev(5, "ok"), ev(0, "error"), ev(0, "error")],
                       CFG, NOW)[0] != "STALLED",
      "pure-error tail is not the wedged signal")
check("stalled precedence over slow",
      la.classify_load([ev(30, "ok"), ev(120, "timeout"), ev(120, "timeout")],
                       CFG, NOW)[0] == "STALLED")

# --- staleness / recovery -----------------------------------------------------

check("stale events (older than window_secs) are ignored -> HEALTHY",
      la.classify_load([ev(120, "timeout", ts=NOW - 1000)] * 5,
                       CFG, NOW)[0] == "HEALTHY")
check("recovery: fresh healthy events after a bad patch classify HEALTHY",
      la.classify_load([ev(120, "timeout", ts=NOW - 1000)] * 3
                       + [ev(3, "ok")] * 4, CFG, NOW)[0] == "HEALTHY")
check("classify never raises on garbage",
      la.classify_load([{"bogus": 1}, None, ev(3, "ok")], CFG, NOW)[0]
      in ("HEALTHY", "SLOW", "STALLED"))

# --- is_timeout_error ---------------------------------------------------------

check("timeout: APITimeoutError", la.is_timeout_error("APITimeoutError"))
check("timeout: ReadTimeout", la.is_timeout_error("ReadTimeout"))
check("timeout: message-based", la.is_timeout_error("APIError", "Request timed out"))
check("not timeout: 500 InternalServerError",
      not la.is_timeout_error("InternalServerError", "boom", 500))
check("not timeout: None/empty", not la.is_timeout_error(None))

# --- message builders ---------------------------------------------------------

check("slow note is honest + non-final framed",
      "heavy load" in la.build_slow_note() and "still working" in la.build_slow_note())
check("stalled note is honest + try-again framed",
      "unresponsive" in la.build_stalled_note()
      and "try again" in la.build_stalled_note())

# --- should_note throttle/cap -------------------------------------------------

check("should_note: HEALTHY never notes",
      la.should_note("HEALTHY", None, NOW, "t1", CFG)[0] is False)
check("should_note: first SLOW note allowed",
      la.should_note("SLOW", None, NOW, "t1", CFG)[0] is True)
rec = {"turn_id": "t1", "count": 1, "last_ts": NOW - 5}
check("should_note: throttled within note_throttle_secs",
      la.should_note("SLOW", rec, NOW, "t1", CFG)[0] is False)
rec2 = {"turn_id": "t1", "count": CFG["max_notes_per_turn"], "last_ts": NOW - 999}
check("should_note: capped at max_notes_per_turn",
      la.should_note("SLOW", rec2, NOW, "t1", CFG)[0] is False)
rec3 = {"turn_id": "t0", "count": 9, "last_ts": NOW - 999}
check("should_note: a NEW turn resets the per-turn cap",
      la.should_note("SLOW", rec3, NOW, "t1", CFG)[0] is True)

# --- window maintenance via _record ------------------------------------------

la._reset_state()
for _ in range(4):
    la._record("ok", 3, time.time(), CFG)
check("record: healthy window stays HEALTHY", la._state["state"] == "HEALTHY")
for _ in range(3):
    la._record("ok", 30, time.time(), CFG)
check("record: elevated latency flips to SLOW", la._state["state"] == "SLOW")
la._record("timeout", 120, time.time(), CFG)
la._record("timeout", 120, time.time(), CFG)
check("record: trailing timeouts flip to STALLED",
      la._state["state"] == "STALLED")

# --- observer hooks: host gating ---------------------------------------------

la._reset_state()
la._la_post_api_request(session_id="s", base_url=OK_URL, api_duration=30)
check("post_api_request records ok event on allowlisted host",
      len(la._events) == 1 and la._events[-1]["outcome"] == "ok")
la._la_post_api_request(session_id="s", base_url="https://evil.example/v1",
                        api_duration=30)
check("post_api_request IGNORES a non-allowlisted host",
      len(la._events) == 1)
la._la_api_request_error(session_id="s", base_url=OK_URL, api_duration=120,
                         error={"type": "ReadTimeout", "message": "timed out"})
check("api_request_error records a timeout event",
      la._events[-1]["outcome"] == "timeout")
la._la_api_request_error(session_id="s", base_url=OK_URL, api_duration=1,
                         status_code=500, error={"type": "InternalServerError",
                                                 "message": "boom"})
check("api_request_error records a non-timeout as 'error'",
      la._events[-1]["outcome"] == "error")

# --- middleware: fail-safety --------------------------------------------------

la._reset_state()
check("middleware no-op without session_id",
      la._la_middleware(request={}, session_id="", base_url=OK_URL) is None)
check("middleware no-op on non-allowlisted host",
      la._la_middleware(request={}, session_id="s", base_url="http://x/v1") is None)
check("middleware fail-safe on bad request",
      la._la_middleware(request="nope", session_id="s", base_url=OK_URL) is None)
check("middleware HEALTHY -> no delivery attempt (returns None)",
      la._la_middleware(request={}, session_id="s", base_url=OK_URL,
                        turn_id="t1", platform="telegram") is None)

# --- middleware: delivery + progress-coordination via a fake adapter ---------

sent = []


def fake_fire(gw, source, text):
    sent.append(text)
    return True


class FakeGW:
    pass


class FakeSource:
    platform = None
    chat_id = "1"


fake_gw = FakeGW()
# Reuse progress's captured-adapter path: populate the symbols loadaware reads.
pg._gw = {"ref": weakref.ref(fake_gw), "loop": object(), "ts": NOW}
pg._resolve_source = lambda gw, sid: FakeSource()
pg._fire_and_forget = fake_fire
pg._turns = {}  # no recent progress send

# Force the global state to SLOW and deliver.
la._reset_state()
for _ in range(4):
    la._record("ok", 30, time.time(), CFG)
assert la._state["state"] == "SLOW"
la._la_middleware(request={}, session_id="s1", base_url=OK_URL, turn_id="t1",
                  platform="telegram", api_call_count=3)
check("middleware delivers a SLOW note via progress's captured adapter",
      len(sent) == 1 and "heavy load" in sent[0])
la._la_middleware(request={}, session_id="s1", base_url=OK_URL, turn_id="t1",
                  platform="telegram", api_call_count=4)
check("middleware throttles the immediate 2nd note (same turn)",
      len(sent) == 1)

# STALLED delivery on a different session (fresh throttle).
for _ in range(2):
    la._record("timeout", 120, time.time(), CFG)
assert la._state["state"] == "STALLED"
la._la_middleware(request={}, session_id="s2", base_url=OK_URL, turn_id="t9",
                  platform="telegram", api_call_count=2)
check("middleware delivers a STALLED note for a new session",
      len(sent) == 2 and "unresponsive" in sent[1])

# Coordination: a just-sent progress interim suppresses our note.
sent.clear()
la._reset_state()
for _ in range(4):
    la._record("ok", 30, time.time(), CFG)
# _progress_recent_send compares against real wall-clock time.time().
pg._turns = {pg._turn_key("s3", "t3"): {"last_sent_ts": time.time()}}  # just sent
la._la_middleware(request={}, session_id="s3", base_url=OK_URL, turn_id="t3",
                  platform="telegram", api_call_count=3)
check("middleware SKIPS its note when progress sent an interim just now",
      len(sent) == 0)
pg._turns = {pg._turn_key("s3", "t3"): {"last_sent_ts": time.time() - 999}}  # long ago
la._la_middleware(request={}, session_id="s3", base_url=OK_URL, turn_id="t3",
                  platform="telegram", api_call_count=3)
check("middleware sends once the progress-coordination window has passed",
      len(sent) == 1)

# Delivery degrades cleanly when the captured gateway is gone.
sent.clear()
pg._gw = {"ref": None, "loop": None, "ts": 0.0}
la._reset_state()
for _ in range(4):
    la._record("ok", 30, time.time(), CFG)
r = la._la_middleware(request={}, session_id="s4", base_url=OK_URL,
                      turn_id="t4", platform="telegram", api_call_count=3)
check("middleware returns None + sends nothing when no gateway captured",
      r is None and len(sent) == 0)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
