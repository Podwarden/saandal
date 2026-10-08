#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Podwarden Inc

"""Offline unit tests for the gateway-harness seed() fidelity fix.

Regression cover for the harness bug where ``GatewayProbe.seed()`` planted
prior history via a raw ``SessionDB.append_message`` (role+content only) and
never re-synced the SessionStore's in-memory entry. Two consequences, both
fixed and asserted here:

  1. Dropping ``tool_calls`` made interim tool-calling assistant turns look
     like degenerate FINAL answers, inflating ``proactive_poison_score``
     (Silverton poison turn: 0.625 with fidelity vs a bogus 1.0 stripped).
  2. The desynced entry (updated_at never re-bumped, last_prompt_tokens=0,
     peer not re-registered) let hermes's own get_or_create_session staleness
     check (``_should_reset`` -> end_session(..., "session_reset")) reset the
     seeded session out from under the plugin under any non-"none" reset
     policy.

Everything here is OFFLINE: no model calls, no real turn driven. We seed the
real Silverton fixtures into a real SessionStore/SessionDB in a dedicated temp
HERMES_HOME, assert hermes's staleness predicate now returns "not stale", and
assert the plugin's real proactive-poison-reset path fires on poison and not on
clean. Run: tests/test_harness_seed.py
"""
import asyncio
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# --- dedicated temp HERMES_HOME BEFORE importing gateway/hermes_state --------
_HOME = tempfile.mkdtemp(prefix="hermes_seedtest_")
os.environ["HERMES_HOME"] = _HOME
os.environ["HERMES_NO_UPDATE_CHECK"] = "1"
Path(_HOME, "sessions").mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(REPO / "tests"))

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"ok   {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "router_plugin", REPO / "plugin" / "__init__.py",
        submodule_search_locations=[str(REPO / "plugin")])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["router_plugin"] = mod
    spec.loader.exec_module(mod)
    return mod


load_plugin()
import router_plugin.selfheal as sh  # noqa: E402

from gateway.config import GatewayConfig, SessionResetPolicy  # noqa: E402
from gateway.session import (  # noqa: E402
    SessionStore, SessionSource, Platform)
import gateway.session as gsession  # noqa: E402
from hermes_state import SessionDB  # noqa: E402

_SIL = json.load(open(REPO / "tests" / "data" / "silverton_fixtures.json"))
POISON = _SIL["poisoned_last_turn"]
CLEAN = _SIL["clean_last_turn"]

# Deterministic plugin config (real DEFAULTS: proactive_reset on, telegram,
# poison_reset_hi=0.35). Pin _cfg so the run does not depend on any config.yaml
# that might exist in the environment.
_CFG = dict(sh.DEFAULTS)
_CFG["platforms"] = ["telegram"]


# ---------------------------------------------------------------------------
# 1. Pure-score fidelity: fixture with tool_calls vs the raw stripped form.
# ---------------------------------------------------------------------------
def _strip(msgs):
    return [{"role": m.get("role"), "content": m.get("content", "")} for m in msgs]


ps_poison = sh.proactive_poison_score(POISON)
ps_clean = sh.proactive_poison_score(CLEAN)
ps_poison_stripped = sh.proactive_poison_score(_strip(POISON))
check("fidelity poison score matches real 0.625",
      abs(ps_poison[0] - 0.625) < 1e-6, ps_poison)
check("fidelity clean score below threshold (~0.111)",
      ps_clean[0] < 0.35, ps_clean)
check("stripping tool_calls INFLATES poison score (the bug)",
      ps_poison_stripped[0] > ps_poison[0], (ps_poison_stripped, ps_poison))


# ---------------------------------------------------------------------------
# 2. Harness seed() through the REAL GatewayProbe (real runner/store/db).
# ---------------------------------------------------------------------------
from gateway_harness import GatewayProbe  # noqa: E402


def _seed_and_probe(history):
    probe = GatewayProbe(chat_id="99900001", user_id="770001")
    seed_res = asyncio.run(probe.seed(history))
    store = probe.runner.session_store
    sk = seed_res["session_key"]
    sid = seed_res["session_id"]
    return probe, store, sk, sid, seed_res


# --- poison seed ------------------------------------------------------------
probe_p, store_p, sk_p, sid_p, res_p = _seed_and_probe(POISON)
check("seed appended all poison messages", res_p["seeded"] == len(POISON), res_p)
check("seed re-synced the SessionStore entry", res_p["entry_synced"] is True, res_p)

# transcript fidelity: tool_calls survived into what the plugin will read
_got = store_p._db.get_messages(sid_p)
_tc = [m for m in _got if m.get("tool_calls")]
check("seeded transcript preserves tool_calls",
      len(_tc) > 0 and len(_got) == len(POISON), (len(_tc), len(_got)))
check("score over seeded transcript == real 0.625 (not the 1.0 inflation)",
      abs(sh.proactive_poison_score(_got)[0] - 0.625) < 1e-6,
      sh.proactive_poison_score(_got))

# --- staleness predicate: seeded session is NOT stale ----------------------
entry_p = store_p._entries[sk_p]
check("last_prompt_tokens > 0 after resync (looks active)",
      entry_p.last_prompt_tokens > 0, entry_p.last_prompt_tokens)
check("_should_reset returns None (not stale) under default policy",
      store_p._should_reset(entry_p, probe_p.source) is None)
check("_is_session_ended_in_db False (no session_reset end_reason)",
      store_p._is_session_ended_in_db(sid_p) is False)
# hermes's own routing-time reset would rotate the id; confirm it does NOT.
entry_again = store_p.get_or_create_session(probe_p.source)
check("get_or_create_session keeps the SAME session id (hermes does not reset)",
      entry_again.session_id == sid_p and not entry_again.was_auto_reset,
      (entry_again.session_id, sid_p))

# --- plugin decision on poison via the REAL pre_gateway_dispatch path -------
sh._reset_state()
_orig_cfg = sh._cfg
sh._cfg = lambda: dict(_CFG)
try:
    event_p = type("Ev", (), {"source": probe_p.source,
                              "text": "Research Silverton",
                              "internal": False})()
    sh._sh_gateway_capture(event=event_p, gateway=probe_p.runner,
                           session_store=store_p)
    rotated = store_p._entries[sk_p].session_id
    check("plugin proactive reset FIRED on poison (session id rotated)",
          rotated != sid_p, (rotated, sid_p))
    check("plugin recorded the rotated (clean) session id in capture",
          sh._sessions.get(sk_p, {}).get("session_id") == rotated)
finally:
    sh._cfg = _orig_cfg
    sh._reset_state()


# --- clean seed: plugin must NOT reset -------------------------------------
probe_c, store_c, sk_c, sid_c, res_c = _seed_and_probe(CLEAN)
check("clean seed appended + resynced",
      res_c["seeded"] == len(CLEAN) and res_c["entry_synced"], res_c)
check("clean seeded session also not stale",
      store_c._should_reset(store_c._entries[sk_c], probe_c.source) is None)

sh._reset_state()
sh._cfg = lambda: dict(_CFG)
try:
    event_c = type("Ev", (), {"source": probe_c.source,
                              "text": "any question",
                              "internal": False})()
    sh._sh_gateway_capture(event=event_c, gateway=probe_c.runner,
                           session_store=store_c)
    still = store_c._entries[sk_c].session_id
    check("plugin does NOT reset a CLEAN seeded session (id unchanged)",
          still == sid_c, (still, sid_c))
finally:
    sh._cfg = _orig_cfg
    sh._reset_state()


# ---------------------------------------------------------------------------
# 3. Aggressive reset policy: prove the entry resync defeats _should_reset.
#    Without the update_session() resync a raw-appended entry with an aged
#    updated_at is judged "idle" -> end_session(session_reset). With it, the
#    bumped updated_at makes the session look freshly active -> not stale.
# ---------------------------------------------------------------------------
cfg_agg = GatewayConfig()
cfg_agg.default_reset_policy = SessionResetPolicy(
    mode="both", idle_minutes=1, at_hour=4)
store_a = SessionStore(Path(_HOME, "sessions_agg"), cfg_agg)
src_a = SessionSource(platform=Platform.TELEGRAM, chat_id="42",
                      chat_type="dm", user_id="42")
sk_a = store_a._generate_session_key(src_a)
entry_a = store_a.get_or_create_session(src_a)
sid_a = entry_a.session_id
for m in POISON:
    store_a._db.append_message(sid_a, m.get("role", "user"),
                               m.get("content", ""),
                               tool_calls=m.get("tool_calls"),
                               tool_name=m.get("tool_name"),
                               tool_call_id=m.get("tool_call_id"))
# Simulate an accumulated (aged) session: the raw-append path never bumps
# updated_at, so a real seeded session's clock is stale.
entry_a.updated_at = gsession._now() - timedelta(minutes=5)
check("aggressive policy: aged un-synced entry IS judged stale (idle)",
      store_a._should_reset(entry_a, src_a) == "idle")
# Apply the fix's resync step.
store_a.update_session(sk_a, last_prompt_tokens=123)
check("aggressive policy: after update_session resync -> NOT stale",
      store_a._should_reset(store_a._entries[sk_a], src_a) is None)


# ---------------------------------------------------------------------------
shutil.rmtree(_HOME, ignore_errors=True)
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
