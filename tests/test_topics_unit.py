#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Podwarden Inc

"""Unit tests for plugin/topics.py — the multi-topic conversation system.

P0 scope: config/kill-switch, inert hooks (byte-identical stock behaviour when
off), the reply badge no-op, background-review suppression, and fail-safety.
Later phases append their own checks. Run: tests/test_topics_unit.py
"""
import importlib.util
import json
import sys
import threading
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
import atexit  # noqa: E402
import router_plugin.topics as _tp_early  # noqa: E402
# v1.15.0: topics' continuity CONTEXT INJECTION + badge are now gated to the
# weak-model host. This suite exercises the injection path, so force the
# weak-host signal ON for hermeticity (ambient config may point at a frontier
# model). A dedicated section flips it OFF to prove the inert path.
_tp_early._on_weak_host = lambda *a, **k: True
import os  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402

import router_plugin.topics as tp  # noqa: E402

# Isolate ALL disk writes to a throwaway vault — NEVER the real ~/llm-wiki.
TMP_WIKI = tempfile.mkdtemp(prefix="topics-test-")
tp.DEFAULTS["wiki_dir"] = TMP_WIKI
atexit.register(lambda: shutil.rmtree(TMP_WIKI, ignore_errors=True))


def RS():
    """Reset in-memory state AND wipe the shared test store, so a scenario that
    expects fresh ids (t#00001) starts from an empty durable store. (P3 reload
    scenarios call tp._reset_state() directly to KEEP the on-disk store.)"""
    tp._reset_state()
    shutil.rmtree(os.path.join(TMP_WIKI, "topics"), ignore_errors=True)


PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"ok   {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


# --- config / kill-switch ----------------------------------------------------
cfg = tp.DEFAULTS  # source-of-truth defaults, not live-config overlay
check("config loads with defaults", isinstance(cfg, dict) and cfg)
check("enabled defaults OFF (inert until a conscious flip)",
      cfg["enabled"] == "off", cfg.get("enabled"))
check("badge sub-flag defaults on", cfg["badge"] == "on")
check("max_open defaults 8", cfg["max_open"] == 8)
check("block_token_cap defaults 250", cfg["block_token_cap"] == 250)
check("classify defaults to llm", cfg["classify"] == "llm")

# config override respected (flag + int + str)
_orig_cfg = tp._cfg
tp._cfg = lambda: dict(tp.DEFAULTS, enabled="on", badge="off", max_open=5,
                       classify="heuristic")
try:
    c2 = tp._cfg()
    check("override: enabled on / badge off / max_open 5 / classify heuristic",
          c2["enabled"] == "on" and c2["badge"] == "off"
          and c2["max_open"] == 5 and c2["classify"] == "heuristic")
finally:
    tp._cfg = _orig_cfg

# --- inert hooks: OFF = stock (None) -----------------------------------------
_cfg_saved = tp._cfg
tp._cfg = lambda: dict(tp.DEFAULTS)  # force disabled, independent of live config
try:
    check("pre_llm_call returns None when disabled (stock turn)",
          tp._tp_pre_llm(session_id="s", turn_id="t",
                         user_message="hi", conversation_history=[]) is None)
    check("post_llm_call returns None when disabled",
          tp._tp_post_llm(session_id="s", turn_id="t",
                          assistant_response="a", conversation_history=[]) is None)
    check("badge_for returns '' when disabled", tp.badge_for("s") == "")
finally:
    tp._cfg = _cfg_saved

# --- P1: heuristic routing + injection + badge (in-memory, no LLM) -----------
tp._cfg = lambda: dict(tp.DEFAULTS, enabled="on")


def _pre(sid, tid, msg, hist=None):
    return tp._tp_pre_llm(session_id=sid, turn_id=tid, user_message=msg,
                          conversation_history=hist or [])


def _post(sid, tid, msg, ans, hist=None):
    return tp._tp_post_llm(session_id=sid, turn_id=tid, user_message=msg,
                           assistant_response=ans, conversation_history=hist or [])


try:
    # first message opens t#00001 and injects a context block
    RS()
    r = _pre("S", "t1", "How do I adjust valve clearance on a BMW R80 motorcycle?")
    check("first message opens a topic + injects context",
          isinstance(r, dict) and "Background continuity notes" in r["context"]
          and tp._active_topic["S"] == 1)
    check("injected block is SILENT background (no topic id leaked into it)",
          "t#0" not in r["context"]
          and "do not mention" in r["context"].lower())
    check("badge reflects the active topic", tp.badge_for("S") == "t#00001")
    check("injected block carries the verify-before-asserting line",
          "verify it with a tool before asserting" in r["context"]
          and "never invent a source" in r["context"])
    _post("S", "t1", "How do I adjust valve clearance on a BMW R80 motorcycle?",
          "Set them to 0.15mm cold.")

    # a clearly different subject opens a SECOND topic (separation)
    r2 = _pre("S", "t2", "Who wrote the poem about a caged eagle, and the verse?")
    check("unrelated subject opens a new topic (t#00002)",
          isinstance(r2, dict) and tp._active_topic["S"] == 2)
    check("badge switches to the new topic", tp.badge_for("S") == "t#00002")
    _post("S", "t2", "Who wrote the poem about a caged eagle?",
          "Pushkin — The Prisoner (1822).")
    check("two distinct topics now open",
          len([x for x in tp._topics.values() if x["status"] == "open"]) == 2)

    # a message overlapping the FIRST topic's keywords routes BACK to it
    r3 = _pre("S", "t3", "and the valve clearance on that BMW motorcycle again?")
    check("moto message routes back to t#00001 (separation holds)",
          isinstance(r3, dict) and tp._active_topic["S"] == 1,
          "active=%s" % tp._active_topic.get("S"))

    # continuation cue stays on the active topic (anti-sprawl)
    RS()
    _pre("S2", "u1", "Tell me about chain maintenance on a motorcycle drivetrain")
    _post("S2", "u1", "Tell me about chain maintenance", "Clean and lube it.")
    r4 = _pre("S2", "u2", "and the chain?")
    check("short continuation 'and the chain?' stays on the active topic",
          isinstance(r4, dict) and tp._active_topic["S2"] == 1)

    # post_llm_call accumulates entities + recent into the active topic
    RS()
    _pre("S3", "w1", "Explain quaternions for 3D rotation in graphics programming")
    _post("S3", "w1", "Explain quaternions", "They avoid gimbal lock.")
    rec = tp._topics[tp._active_topic["S3"]]
    check("write-back grows entities", any("quaternion" in e for e in rec["entities"]))
    check("write-back records the exchange + bumps turns",
          rec["turns"] == 1 and len(rec["recent"]) == 1)

    # build_topic_block respects the token cap
    RS()
    big = tp._open_topic("Big", ["kw%d" % i for i in range(40)], time.time())
    big["facts"] = ["fact number %d is quite long and wordy" % i for i in range(30)]
    blk = tp.build_topic_block(big, dict(tp.DEFAULTS, block_token_cap=60))
    check("block hard-capped near block_token_cap*4 chars", len(blk) <= 60 * 4 + 8,
          "len=%d" % len(blk))

    # max_open LRU eviction to dormant (5 unrelated subjects, cap 3)
    RS()
    cfg3 = dict(tp.DEFAULTS, enabled="on", max_open=3)
    subjects = [
        "motorcycle valve clearance adjustment procedure",
        "sonnet iambic pentameter rhyme scheme",
        "quaternion rotation matrix graphics",
        "sourdough fermentation hydration bakery",
        "corporate tax depreciation schedule",
    ]
    for i, s in enumerate(subjects):
        tp._route("SE", "e%d" % i, s, [], cfg3)
    check("5 unrelated subjects opened 5 distinct topics",
          len(tp._topics) == 5, "n=%d" % len(tp._topics))
    check("max_open=3 keeps only 3 open topics",
          len([x for x in tp._topics.values() if x["status"] == "open"]) == 3,
          "open=%d" % len([x for x in tp._topics.values() if x["status"] == "open"]))
    check("evicted topics become dormant (retained, not deleted)",
          len([x for x in tp._topics.values() if x["status"] == "dormant"]) == 2)
finally:
    tp._cfg = _orig_cfg

# with routing done, disabled flag still fully inert
_cfg_saved2 = tp._cfg
tp._cfg = lambda: dict(tp.DEFAULTS)  # force disabled, independent of live config
try:
    check("pre_llm_call None again once disabled",
          _pre("Z", "z1", "anything") is None)
    check("badge_for '' again once disabled",
          tp.badge_for("Z") == "")
finally:
    tp._cfg = _cfg_saved2
RS()


# --- P2: LLM classifier + thresholds + /topic overrides ----------------------
class _FakeLlm:
    def __init__(self, parsed):
        self._parsed = parsed
        self.calls = 0

    def complete_structured(self, **kw):
        self.calls += 1

        class _R:
            pass
        r = _R()
        r.parsed = self._parsed
        return r


class _FakeCtxLlm:
    def __init__(self, llm):
        self.llm = llm


_saved_ctx = tp._ctx
try:
    # real topics 1 (Moto) + 2 (Poetry) in the store, so threshold checks that
    # consult `active_tid in _topics` see them.
    RS()
    tp._open_topic("Moto", ["bmw", "valve", "motorcycle"], time.time())
    tp._open_topic("Poetry", ["pushkin", "poem", "verse"], time.time())
    recs = tp._open_recs()
    # _coerce_llm_decision mapping
    d = tp._coerce_llm_decision(
        {"topic_id": "t00002", "action": "route", "confidence": 0.9}, recs)
    check("coerce: 't00002' -> route topic_id=2",
          d["action"] == "route" and d["topic_id"] == 2)
    d = tp._coerce_llm_decision(
        {"topic_id": "new", "action": "open", "confidence": 0.8, "title": "X"}, recs)
    check("coerce: 'new' -> open", d["action"] == "open" and d["topic_id"] is None)
    d = tp._coerce_llm_decision(
        {"topic_id": "t09999", "action": "route", "confidence": 0.8}, recs)
    check("coerce: unknown id -> open (no phantom route)", d["action"] == "open")
    check("coerce: garbage -> None (heuristic fallback)",
          tp._coerce_llm_decision("not a dict", recs) is None)

    # continuity thresholds
    d = tp._apply_thresholds({"action": "open", "topic_id": None,
                              "confidence": 0.4}, 1)
    check("threshold: low-confidence 'open' attaches to active",
          d["action"] == "route" and d["topic_id"] == 1)
    d = tp._apply_thresholds({"action": "route", "topic_id": 2,
                              "confidence": 0.4}, 1)
    check("threshold: low-confidence switch stays on active", d["topic_id"] == 1)
    d = tp._apply_thresholds({"action": "route", "topic_id": 2,
                              "confidence": 0.85}, 1)
    check("threshold: high-confidence switch honored", d["topic_id"] == 2)

    # end-to-end LLM routing via canned complete_structured (contract test).
    # _classify_llm imports agent.plugin_llm (only present in the hermes runtime);
    # stub it so this test exercises the real LLM path instead of falling through
    # to the heuristic in the isolated test env.
    import types as _types
    if "agent" not in sys.modules:
        sys.modules["agent"] = _types.ModuleType("agent")
    if "agent.plugin_llm" not in sys.modules:
        _plm = _types.ModuleType("agent.plugin_llm")
        class PluginLlmTextInput:  # minimal stand-in for the runtime dataclass
            def __init__(self, text="", **kw):
                self.text = text
        _plm.PluginLlmTextInput = PluginLlmTextInput
        sys.modules["agent.plugin_llm"] = _plm
    RS()
    tp._open_topic("Moto", ["bmw", "valve", "motorcycle"], time.time())
    tp._open_topic("Poetry", ["pushkin", "poem", "verse"], time.time())
    tp._active_topic["SL"] = 1
    fake = _FakeLlm({"topic_id": "t00002", "action": "route", "confidence": 0.9})
    tp._ctx = _FakeCtxLlm(fake)
    dec = tp._classify("that eagle verse again", tp._open_recs(), [], 1,
                       dict(tp.DEFAULTS, classify="llm"))
    check("llm classify routes to t#00002 per canned JSON", dec["topic_id"] == 2)
    check("llm classifier actually called once", fake.calls == 1)

    # llm error -> heuristic fallback (never raises)
    class _BoomLlm:
        def complete_structured(self, **kw):
            raise RuntimeError("boom")
    tp._ctx = _FakeCtxLlm(_BoomLlm())
    dec = tp._classify("bmw valve clearance", tp._open_recs(), [], 1,
                       dict(tp.DEFAULTS, classify="llm"))
    check("llm error -> heuristic fallback (routes to moto t#00001)",
          dec.get("topic_id") == 1)

    # classify=heuristic never calls the llm
    fake2 = _FakeLlm({"topic_id": "t00002", "action": "route", "confidence": 0.9})
    tp._ctx = _FakeCtxLlm(fake2)
    tp._classify("bmw valve", tp._open_recs(), [], 1,
                 dict(tp.DEFAULTS, classify="heuristic"))
    check("classify=heuristic skips the llm call", fake2.calls == 0)

    # /topic overrides (deterministic, no llm)
    RS()
    tp._ctx = None
    tp._cfg = lambda: dict(tp.DEFAULTS, enabled="on", classify="heuristic")
    cfgH = tp._cfg()
    check("/topic parses new", tp._parse_override("/topic new My Bike") == ("new", "My Bike"))
    check("/topic parses id", tp._parse_override("/topic t00007") == ("route", 7))
    check("/topic parses close", tp._parse_override("/topic close") == ("close", None))
    check("/topic parses bare list", tp._parse_override("/topic") == ("list", None))
    check("non-command is not an override", tp._parse_override("what is a topic?") is None)

    r = tp._route("SO", "o1", "/topic new Restoring my R80", [], cfgH)
    check("/topic new opens a titled topic",
          r is not None and "R80" in (r.get("title") or ""))
    otid = r["id"]
    tp._route("SO", "o2", "unrelated corporate tax depreciation receipts", [], cfgH)
    r3 = tp._route("SO", "o3", "/topic %d" % otid, [], cfgH)
    check("/topic <id> routes back to that topic",
          r3 is not None and r3["id"] == otid)
    check("badge follows the /topic override",
          tp.badge_for("SO") == "t#%05d" % otid)
    r4 = tp._route("SO", "o4", "/topic close", [], cfgH)
    check("/topic close -> no topic context this turn", r4 is None)
    check("/topic close clears the active badge", tp.badge_for("SO") == "")
    check("/topic close marks the topic closed",
          tp._topics[otid]["status"] == "closed")
finally:
    tp._cfg = _orig_cfg
    tp._ctx = _saved_ctx
    RS()


# --- P2 e2e: the badge composes through selfheal's REAL finisher -------------
# (deterministic, no vLLM: validates the actual host-bridge integration point —
# the riskiest seam — end to end.)
import router_plugin.selfheal as sh  # noqa: E402

_sh_saved_cfg = sh._cfg
_saved_badge = sh._host.get("badge")
_saved_topics_mod = plugin._topics_mod
plugin._topics_mod = tp                       # wire the bridge as register() would
sh._host["badge"] = plugin._topics_badge
tp._cfg = lambda: dict(tp.DEFAULTS, enabled="on", classify="heuristic")
sh._cfg = lambda: dict(sh.DEFAULTS, enabled="on")
try:
    RS()
    tp._route("E2E", "e1", "How do I true a motorcycle wheel spoke?", [], tp._cfg())
    out = sh._sh_transform_out(
        response_text="Use a spoke wrench and a truing stand.",
        session_id="E2E", platform="telegram")
    check("badge composes onto the answer via selfheal's real finisher",
          isinstance(out, str) and out.rstrip().endswith("t#00001")
          and "spoke wrench" in out, repr(out)[:140])
    out2 = sh._sh_transform_out(response_text="hello world here",
                                session_id="NO_TOPIC", platform="telegram")
    check("no active topic -> answer left unbadged",
          out2 in (None, "hello world here")
          or (isinstance(out2, str) and "t#" not in out2))
    # badge sub-flag off -> no badge even with an active topic
    tp._cfg = lambda: dict(tp.DEFAULTS, enabled="on", classify="heuristic",
                           badge="off")
    out3 = sh._sh_transform_out(response_text="answer text body",
                                session_id="E2E", platform="telegram")
    check("topics.badge=off -> selfheal ships the answer unbadged",
          out3 in (None, "answer text body")
          or (isinstance(out3, str) and "t#" not in out3))
finally:
    sh._cfg = _sh_saved_cfg
    tp._cfg = _orig_cfg
    plugin._topics_mod = _saved_topics_mod
    if _saved_badge is None:
        sh._host.pop("badge", None)
    else:
        sh._host["badge"] = _saved_badge
    RS()


# --- P3: durable llm-wiki store + rehydration (isolated tmp vaults) -----------
def _vault():
    return tempfile.mkdtemp(dir=TMP_WIKI)


tp._cfg = _orig_cfg  # P3 passes cfg explicitly (with its own wiki_dir)
try:
    # persist on route + write-back, then reload into fresh in-memory state
    w = _vault()
    c = dict(tp.DEFAULTS, enabled="on", classify="heuristic", wiki_dir=w)
    tp._cfg = lambda: dict(c)   # _tp_post_llm reads _cfg() internally
    tp._reset_state()
    tp._route("D", "d1", "restoring a 1978 BMW R80 airhead motorcycle", [], c)
    tp._tp_post_llm(session_id="D", turn_id="d1",
                    user_message="restoring a 1978 BMW R80 airhead motorcycle",
                    assistant_response="Start with the top end.",
                    conversation_history=[])
    tp._route("D", "d2", "analyzing Pushkin's poem The Prisoner iambic verse", [], c)
    tdir = os.path.join(w, "topics")
    check("P3: manifest + per-topic files written",
          os.path.exists(os.path.join(tdir, "_index.json"))
          and os.path.exists(os.path.join(tdir, "t00001.md"))
          and os.path.exists(os.path.join(tdir, "t00002.md")))
    check("P3: topics/ carries a README (plugin ownership marker)",
          os.path.exists(os.path.join(tdir, "README.md")))

    tp._reset_state()          # drop in-memory state, KEEP the disk store
    tp._load_store(c)
    check("P3: manifest reloads both topics", len(tp._topics) == 2)
    check("P3: next_id restored past the max", tp._next_id[0] == 3)
    check("P3: reloaded rows are body-unloaded until rehydrated",
          all(not r.get("_body_loaded") for r in tp._topics.values()))

    rec1 = tp._topics[1]
    tp._rehydrate(c, rec1)
    check("P3: rehydrate loads the body (recovers the logged exchange)",
          rec1["_body_loaded"]
          and any("R80" in (u + a) for u, a in rec1["recent"]))

    # eviction persists a dormant file with status: dormant
    w2 = _vault()
    cE = dict(tp.DEFAULTS, enabled="on", classify="heuristic", wiki_dir=w2,
              max_open=2)
    tp._reset_state()
    for i, s in enumerate([
            "how do I balance the alpha widget assembly line throughput",
            "correct torque spec for the beta cylinder head gasket bolts",
            "recommended interval for a gamma radiator coolant system flush"]):
        tp._route("E", "ee%d" % i, s, [], cE)
    dorm = [r for r in tp._topics.values() if r["status"] == "dormant"]
    check("P3: cap=2 -> exactly one dormant topic", len(dorm) == 1,
          "statuses=%s" % {t["id"]: t["status"] for t in tp._topics.values()})
    with open(tp._topic_path(cE, dorm[0]["id"]), encoding="utf-8") as f:
        check("P3: dormant status persisted to its file",
              "status: dormant" in f.read())

    # resume: a message matching a dormant topic reopens it (no new slot)
    w3 = _vault()
    cR = dict(tp.DEFAULTS, enabled="on", classify="heuristic", wiki_dir=w3)
    tp._reset_state()
    dr = tp._open_topic("Beekeeping hives",
                        ["beekeeping", "hive", "apiary", "honey"], time.time())
    dr["status"] = "dormant"
    tp._persist_topic(cR, dr)
    dtid = dr["id"]
    tp._route("R", "r1", "car engine oil change and filter", [], cR)  # unrelated
    r2 = tp._route("R", "r2", "my apiary hive honey harvest question", [], cR)
    check("P3: a matching message RESUMES the dormant topic (no new slot)",
          r2 is not None and r2["id"] == dtid and r2["status"] == "open")

    # grep fallback finds an off-manifest topic file by entity overlap
    w4 = _vault()
    cG = dict(tp.DEFAULTS, enabled="on", classify="heuristic", wiki_dir=w4)
    tp._reset_state()
    grec = tp._open_topic("Widget calibration",
                          ["widget", "calibrate", "torque"], time.time())
    tp._persist_topic(cG, grec)
    gtid = grec["id"]
    tp._reset_state()          # manifest-less in-memory; only the file remains
    _gi = os.path.join(w4, "topics", "_index.json")
    if os.path.exists(_gi):
        os.remove(_gi)
    check("P3: grep fallback finds an off-manifest topic by entities",
          tp._grep_topic_files(cG, "help me calibrate the widget torque") == gtid)

    # concurrent flush -> distinct per-topic files, no lost writes
    w5 = _vault()
    cF = dict(tp.DEFAULTS, enabled="on", classify="heuristic", wiki_dir=w5)
    tp._reset_state()
    for i in range(8):
        tp._persist_topic(cF, tp._open_topic("Topic %d" % i, ["ent%d" % i],
                                             time.time()))
    files = [n for n in os.listdir(os.path.join(w5, "topics"))
             if n.startswith("t") and n.endswith(".md")]
    check("P3: 8 opened topics -> 8 distinct files, none lost",
          len(set(files)) == 8, "files=%d" % len(files))

    # atomic write leaves no .tmp turds and round-trips
    check("P3: atomic_write round-trips",
          tp._atomic_write(os.path.join(w5, "x.txt"), "hello")
          and open(os.path.join(w5, "x.txt")).read() == "hello")
finally:
    tp._cfg = _orig_cfg
    tp._reset_state()


# --- P4: write-back provenance + summary roll-up + curation note --------------
_p4w = [None]


def _p4cfg(**kw):
    _p4w[0] = tempfile.mkdtemp(dir=TMP_WIKI)
    return dict(tp.DEFAULTS, enabled="on", classify="heuristic",
                wiki_dir=_p4w[0], **kw)


class _CountLlm:
    def __init__(self):
        self.completes = 0

    def complete(self, **kw):
        self.completes += 1

        class _R:
            text = "Rolling summary of the topic so far."
        return _R()


try:
    # _to_provenance keeps only url-bearing findings, reformatted with [src:]
    check("P4: provenance keeps url-bearing finding",
          tp._to_provenance("Bridgeway Vet — https://ex.com/a")
          == "Bridgeway Vet [src: https://ex.com/a]")
    check("P4: provenance drops a source-less finding",
          tp._to_provenance("just a bare claim, no url") is None)

    # findings harvested from a web_search tool result carry [src:]
    c4 = _p4cfg()
    tp._cfg = lambda: dict(c4)
    tp._reset_state()
    tp._route("F", "f1", "vet clinics near Mission for cat FHO surgery", [], c4)
    hist = [{"role": "user", "content": "vet clinics near Mission cat FHO"},
            {"role": "tool", "content": json.dumps(
                {"data": {"web": [{"title": "Mission Vet Hospital",
                                   "url": "https://missionvet.example/fho"}]}})},
            {"role": "assistant", "content": "Here are some options."}]
    tp._tp_post_llm(session_id="F", turn_id="f1",
                    user_message="vet clinics near Mission cat FHO",
                    assistant_response="Here are some options.",
                    conversation_history=hist)
    rec = tp._topics[tp._active_topic["F"]]
    check("P4: finding harvested with [src:] provenance",
          any("[src: https://missionvet.example/fho]" in f
              for f in rec["findings"]),
          rec["findings"])
    # the finding lands in the persisted file + the injected block
    with open(tp._topic_path(c4, rec["id"]), encoding="utf-8") as f:
        check("P4: finding persisted under ## findings",
              "[src: https://missionvet.example/fho]" in f.read())
    blk = tp.build_topic_block(rec, c4)
    check("P4: injected block shows the sourced finding + verify line",
          "[src:" in blk and "[src: url]" in blk)

    # amortized roll-up fires only every summarize_every turns
    import json as _json  # noqa
    llm = _CountLlm()
    _saved = tp._ctx
    tp._ctx = type("Cx", (), {"llm": llm})()
    c4b = _p4cfg(summarize_every=3)
    tp._cfg = lambda: dict(c4b)
    tp._reset_state()
    tp._route("G", "g0", "long running project about garden irrigation design",
              [], c4b)
    for i in range(1, 6):
        tp._tp_post_llm(session_id="G", turn_id="g%d" % i,
                        user_message="more on the irrigation drip lines %d" % i,
                        assistant_response="noted %d" % i,
                        conversation_history=[])
    check("P4: roll-up fired only on the threshold turns (5 turns / every 3 -> 1)",
          llm.completes == 1, "completes=%d" % llm.completes)
    recg = tp._topics[tp._active_topic["G"]]
    check("P4: summary was rewritten by the roll-up",
          "Rolling summary" in (recg.get("summary") or ""))

    # summarize=off suppresses the roll-up call entirely
    llm2 = _CountLlm()
    tp._ctx = type("Cx", (), {"llm": llm2})()
    c4c = _p4cfg(summarize_every=1, summarize="off")
    tp._cfg = lambda: dict(c4c)
    tp._reset_state()
    tp._route("H", "h0", "topic about vintage synthesizer repair basics", [], c4c)
    tp._tp_post_llm(session_id="H", turn_id="h1",
                    user_message="the oscillator calibration steps",
                    assistant_response="ok", conversation_history=[])
    check("P4: summarize=off -> no roll-up LLM call", llm2.completes == 0)
    tp._ctx = _saved

    # /topic close emits an inbox curation note with valid target: frontmatter
    c4d = _p4cfg()
    tp._cfg = lambda: dict(c4d)
    tp._reset_state()
    r = tp._route("K", "k1", "planning the summer roof repair project", [], c4d)
    r["facts"] = ["quote from ABC Roofing was 8k"]
    r["findings"] = ["shingle spec [src: https://ex.com/shingle]"]
    ktid = r["id"]
    tp._route("K", "k2", "/topic close", [], c4d)
    note = os.path.join(c4d["wiki_dir"], "inbox", "hermes")
    files = [n for n in os.listdir(note)] if os.path.isdir(note) else []
    check("P4: /topic close wrote a curation inbox note",
          any(n.startswith("topic-t%05d-" % ktid) for n in files), files)
    if files:
        body = open(os.path.join(note, files[0]), encoding="utf-8").read()
        check("P4: note has a valid target: <section>/<slug> frontmatter",
              "target: jobs/" in body and "name: topic-t%05d-" % ktid in body)
        check("P4: note carries facts + sourced findings",
              "ABC Roofing" in body and "[src: https://ex.com/shingle]" in body)
    check("P4: plugin never wrote wiki/ or index.md directly",
          not os.path.exists(os.path.join(c4d["wiki_dir"], "wiki"))
          and not os.path.exists(os.path.join(c4d["wiki_dir"], "index.md")))
finally:
    tp._cfg = _orig_cfg
    tp._reset_state()


# --- P5: per-gateway hardening — voice_only badge strip ----------------------
try:
    # _is_voice_session reads the platform:chat_id -> mode map
    tp._reset_state()
    tp._voice_map_cache[0] = 1e18   # pin the cache (skip file re-read)
    tp._voice_map_cache[1] = {"telegram:100000001": "voice_only",
                              "discord:42": "voice"}
    check("P5: _is_voice_session true for a voice_only chat",
          tp._is_voice_session("telegram", "100000001") is True)
    check("P5: _is_voice_session false for a non-voice_only chat",
          tp._is_voice_session("discord", "42") is False)
    check("P5: _is_voice_session false for an unknown chat",
          tp._is_voice_session("telegram", "999") is False)
    check("P5: _is_voice_session fail-safe on missing sender",
          tp._is_voice_session("telegram", None) is False)

    # end to end: a voice_only session gets NO badge; a text session does
    wv = tempfile.mkdtemp(dir=TMP_WIKI)
    tp._cfg = lambda: dict(tp.DEFAULTS, enabled="on", classify="heuristic",
                           wiki_dir=wv)
    tp._reset_state()
    tp._voice_map_cache[0] = 1e18
    tp._voice_map_cache[1] = {"telegram:100000001": "voice_only"}
    tp._tp_pre_llm(session_id="V", turn_id="v1",
                   user_message="how do I restore a vintage motorcycle engine",
                   conversation_history=[], platform="telegram",
                   sender_id="100000001")
    check("P5: voice_only session -> badge stripped (TTS-safe)",
          tp.badge_for("V") == "")
    tp._tp_pre_llm(session_id="T", turn_id="t1",
                   user_message="analysis of a sonnet's rhyme scheme",
                   conversation_history=[], platform="telegram",
                   sender_id="222")
    check("P5: text session -> badge shown",
          tp.badge_for("T").startswith("t#"))

    # the file reader path (gateway_voice_mode.json via HERMES_HOME)
    vh = tempfile.mkdtemp(dir=TMP_WIKI)
    with open(os.path.join(vh, "gateway_voice_mode.json"), "w") as f:
        f.write('{"telegram:555": "voice_only"}')
    _saved_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = vh
    tp._voice_map_cache[0] = 0.0
    tp._voice_map_cache[1] = {}
    check("P5: _voice_mode_map reads gateway_voice_mode.json from HERMES_HOME",
          tp._is_voice_session("telegram", "555") is True)
    if _saved_home is not None:
        os.environ["HERMES_HOME"] = _saved_home
    else:
        os.environ.pop("HERMES_HOME", None)
finally:
    tp._cfg = _orig_cfg
    tp._reset_state()


# --- v1.12.1: research-driven fixes (rehydrate threshold, disclosure) --------
try:
    # P3: rehydrate_overlap config (round-2: 0.30 lifts dormant resume 31->100%)
    check("rehydrate_overlap defaults to 0.30", abs(tp._cfg().get("rehydrate_overlap", 0) - 0.30) < 1e-9)
    check("rehydrate_overlap is configurable",
          abs(tp._rehydrate_overlap(dict(tp.DEFAULTS, rehydrate_overlap=0.45)) - 0.45) < 1e-9)
    check("rehydrate_overlap clamps out-of-range to default",
          abs(tp._rehydrate_overlap(dict(tp.DEFAULTS, rehydrate_overlap=9)) - 0.30) < 1e-9)

    # P3 win: a fresh-phrased cold return now REOPENS the dormant topic (was a dup at 0.50)
    wv = tempfile.mkdtemp(dir=TMP_WIKI)
    c = dict(tp.DEFAULTS, enabled="on", classify="heuristic", wiki_dir=wv)
    tp._reset_state()
    d = tp._open_topic("Islay whisky tasting", ["dram", "malt", "peat"], time.time())
    d["status"] = "dormant"; tp._persist_topic(c, d)
    dtid = d["id"]
    tp._route("W", "w0", "help me pick a new espresso grinder burr size", [], c)  # unrelated active
    # 2 shared exact tokens (malt, peat) out of 6 -> 0.33 overlap: RESUMES at the
    # 0.30 bar, would have spawned a duplicate under the old 0.50.
    msg = "any good malt with a strong peat character to try"
    assert 0.30 <= (len(set(tp._keywords(msg)) & {"dram", "malt", "peat"})
                    / float(len(set(tp._keywords(msg))))) < 0.50
    r = tp._route("W", "w1", msg, [], c)
    check("P3: fresh-phrased cold return RESUMES the dormant topic (0.30 bar)",
          r is not None and r["id"] == dtid and r["status"] == "open",
          "routed to %s (dormant was %s)" % (r.get("id") if r else None, dtid))

    # SIBLING OVER-MERGE VALIDATION (the report's gating check): two legitimately
    # distinct dormant topics that share 1-2 incidental words must NOT merge.
    tp._reset_state()
    wv2 = tempfile.mkdtemp(dir=TMP_WIKI)
    c2 = dict(tp.DEFAULTS, enabled="on", classify="heuristic", wiki_dir=wv2)
    a = tp._open_topic("Python asyncio bug", ["python", "asyncio", "await", "coroutine"], time.time())
    b = tp._open_topic("Python pandas dataframe", ["python", "pandas", "dataframe", "merge"], time.time())
    a["status"] = "dormant"; b["status"] = "dormant"
    tp._persist_topic(c2, a); tp._persist_topic(c2, b)
    tp._route("SB", "s0", "unrelated question about garden soil ph", [], c2)
    # a clearly-asyncio message shares only "python" with the pandas topic
    rr = tp._route("SB", "s1", "my asyncio coroutine never awaits, help", [], c2)
    check("P3 sibling guard: asyncio msg resumes the asyncio topic, not pandas",
          rr is not None and rr["id"] == a["id"], "routed to %s (asyncio=%s pandas=%s)"
          % (rr.get("id") if rr else None, a["id"], b["id"]))
    rr2 = tp._route("SB", "s2", "how do I merge two pandas dataframes on a key", [], c2)
    check("P3 sibling guard: pandas msg resumes the pandas topic, not asyncio",
          rr2 is not None and rr2["id"] == b["id"], "routed to %s" % (rr2.get("id") if rr2 else None))

    # P4: cold-resume disclosure fix — header framing + synthesized anchor
    check("P4: block header drops the leak-seeding 'CONVERSATION MEMORY' phrase",
          "CONVERSATION MEMORY" not in tp._BLOCK_HEADER)
    check("P4: block header carries the anti-disclosure instruction",
          "attribute it to earlier" in tp._BLOCK_HEADER
          and "never say it came from notes" in tp._BLOCK_HEADER)
    cold = tp._open_topic("Beekeeping hive setup", ["hive", "apiary", "frames"], time.time())
    cold["recent"] = []
    blk = tp.build_topic_block(cold, dict(tp.DEFAULTS))
    check("P4: cold resume (no recent) synthesizes an in-conversation anchor",
          "Earlier in this same conversation" in blk)
    warm = tp._open_topic("Warm topic", ["x"], time.time())
    warm["recent"] = [("what about torque", "use 20nm")]
    check("P4: a warm topic still shows real recent exchanges (not the synth line)",
          "Earlier in this same conversation" not in tp.build_topic_block(warm, dict(tp.DEFAULTS)))
finally:
    tp._cfg = _orig_cfg
    tp._reset_state()


# --- background-review suppression (never route/badge a forked review) -------
_review = [{"role": "user",
            "content": "Review the conversation above and update the skills."}]
tp._cfg = lambda: dict(tp.DEFAULTS, enabled="on")
try:
    check("bg-review detected via harness prompt",
          tp._in_background_review(_review) is True)
    check("normal messages not bg-review",
          tp._in_background_review([{"role": "user", "content": "hi"}]) is False)
    check("pre_llm_call no-ops in a background review",
          tp._tp_pre_llm(session_id="s", turn_id="t", user_message="x",
                         conversation_history=_review) is None)

    def _on_bg_thread():
        out = {}
        t = threading.Thread(
            target=lambda: out.__setitem__("v", tp._in_background_review([])),
            name="bg-review")
        t.start(); t.join()
        return out.get("v")

    check("bg-review detected via thread name", _on_bg_thread() is True)
finally:
    tp._cfg = _orig_cfg

# --- fail-safety: every entry point swallows errors --------------------------
_boom = lambda: (_ for _ in ()).throw(RuntimeError("cfg exploded"))
tp._cfg = _boom
try:
    check("pre_llm_call fail-safe on config explosion",
          tp._tp_pre_llm(session_id="s", user_message="x") is None)
    check("post_llm_call fail-safe on config explosion",
          tp._tp_post_llm(session_id="s") is None)
    check("badge_for fail-safe on config explosion", tp.badge_for("s") == "")
finally:
    tp._cfg = _orig_cfg

check("bg-review detection fail-safe on garbage",
      tp._in_background_review("not a list") is False)

# --- registration is idempotent + fail-safe ----------------------------------
class _FakeMgr:
    def __init__(self):
        self._hooks = {}


class _FakeCtx:
    def __init__(self):
        self._manager = _FakeMgr()

    def register_hook(self, event, cb):
        self._manager._hooks.setdefault(event, []).append(cb)


ctx = _FakeCtx()
tp.register(ctx)
tp.register(ctx)  # force rescan — must not double-register
n_pre = len(ctx._manager._hooks.get("pre_llm_call", []))
n_post = len(ctx._manager._hooks.get("post_llm_call", []))
check("register wires pre_llm_call + post_llm_call once (idempotent)",
      n_pre == 1 and n_post == 1, "pre=%s post=%s" % (n_pre, n_post))
check("register fail-safe on a bad ctx (no register_hook)",
      tp.register(object()) is None)

# --- the __init__ badge bridge is wired + inert ------------------------------
check("plugin exposes _topics_badge bridge", hasattr(plugin, "_topics_badge"))
check("_topics_badge returns '' while inert", plugin._topics_badge("s") == "")

# --- v1.14.0 R0: research/plan/scratch foundation (all flags default OFF) -----
_c = tp._cfg()
check("R0 research flag defaults OFF", _c["research"] == "off")
check("R0 research_auto flag defaults OFF", _c["research_auto"] == "off")
check("R0 scratchpad flag defaults OFF", _c["scratchpad"] == "off")
check("R0 resume_surface flag defaults OFF", _c["resume_surface"] == "off")
check("R0 active_recall flag defaults OFF", _c["active_recall"] == "off")
check("R0 self_knowledge flag defaults OFF", _c["self_knowledge"] == "off")
check("R0 autostore flag defaults OFF", _c["autostore"] == "off")
check("R0 research_decompose defaults 'off'", _c["research_decompose"] == "off")
check("R0 plan_max_items default 6", _c["plan_max_items"] == 6)
check("R0 plan_token_cap default 180", _c["plan_token_cap"] == 180)
check("R0 recall_facts_max default 5", _c["recall_facts_max"] == 5)
check("R0 autostore_every default 4", _c["autostore_every"] == 4)
check("R0 plan_overlap default 0.34", abs(_c["plan_overlap"] - 0.34) < 1e-9)

# _open_topic seeds the new fields inert
_ot = tp._open_topic("Vet visit for the dog", ["dog", "vet"], 1000.0)
check("R0 _open_topic seeds research=False", _ot["research"] is False)
check("R0 _open_topic seeds plan=[] scratch=[]",
      _ot["plan"] == [] and _ot["scratch"] == [])

# a non-research topic serializes WITHOUT ## plan / ## scratchpad, round-trips clean
_nr = tp._open_topic("Sourdough starter care", ["sourdough"], 2000.0)
_nr["summary"] = "Feeding schedule for a rye starter."
_nr["facts"] = ["Feed 1:1:1 by weight"]
_md_nr = tp._serialize_topic_md(_nr)
check("R0 non-research topic omits ## plan section", "## plan" not in _md_nr)
check("R0 non-research topic omits ## scratchpad section",
      "## scratchpad" not in _md_nr)
_p_nr = tp._parse_topic_md(_md_nr)
check("R0 non-research parse -> research False, empty plan/scratch",
      _p_nr["research"] is False and _p_nr["plan"] == [] and _p_nr["scratch"] == [])

# a RESEARCH topic round-trips its plan checklist + scratch through serialize->parse
_r = tp._open_topic("Best all-season motorcycle tyres 2027", ["motorcycle", "tyres"], 3000.0)
_r["research"] = True
_r["summary"] = "Compare touring tyre options."
_r["facts"] = ["Rider commutes ~40km/day"]
_r["plan"] = [
    {"text": "Find top-rated all-season tyres", "status": "done",
     "src": "https://example.com/tyres"},
    {"text": "Check wet-grip ratings", "status": "open", "src": ""},
    {"text": "Compare tread life vs price", "status": "open", "src": ""},
]
_r["scratch"] = ["User prefers Michelin", "Budget around 200 EUR/tyre"]
_md_r = tp._serialize_topic_md(_r)
check("R0 research topic emits ## plan and ## scratchpad",
      "## plan" in _md_r and "## scratchpad" in _md_r)
check("R0 research plan renders done box + src",
      "- [x] Find top-rated all-season tyres [src: https://example.com/tyres]" in _md_r)
check("R0 research plan renders open box", "- [ ] Check wet-grip ratings" in _md_r)
_p_r = tp._parse_topic_md(_md_r)
check("R0 research round-trip: research flag preserved", _p_r["research"] is True)
check("R0 research round-trip: plan length preserved", len(_p_r["plan"]) == 3)
check("R0 research round-trip: done item text+status+src",
      _p_r["plan"][0]["text"] == "Find top-rated all-season tyres"
      and _p_r["plan"][0]["status"] == "done"
      and _p_r["plan"][0]["src"] == "https://example.com/tyres")
check("R0 research round-trip: open item has empty src",
      _p_r["plan"][1]["status"] == "open" and _p_r["plan"][1]["src"] == "")
check("R0 research round-trip: scratch preserved",
      _p_r["scratch"] == ["User prefers Michelin", "Budget around 200 EUR/tyre"])
check("R0 research round-trip: summary/facts still intact",
      _p_r["summary"] == "Compare touring tyre options."
      and _p_r["facts"] == ["Rider commutes ~40km/day"])

# _ensure_topic_loaded rebuilds a dormant research topic's plan/scratch from disk
RS()
_dcfg = tp._cfg()
_dr = tp._open_topic("Dormant research on beekeeping", ["bees"], 4000.0)
_dr_id = tp._next_id[0]
_dr["id"] = _dr_id
tp._next_id[0] += 1
_dr["research"] = True
_dr["plan"] = [{"text": "Find local hive suppliers", "status": "open", "src": ""}]
_dr["scratch"] = ["Spring is the time to start"]
_dr["status"] = "dormant"
tp._topics[_dr_id] = _dr
os.makedirs(os.path.join(TMP_WIKI, "topics"), exist_ok=True)
with open(tp._topic_path(_dcfg, _dr_id), "w", encoding="utf-8") as _f:
    _f.write(tp._serialize_topic_md(_dr))
# drop the body from memory, then reload from disk
_dr["plan"] = []
_dr["scratch"] = []
_dr["research"] = False
_dr["_body_loaded"] = False
tp._rehydrate(_dcfg, _dr)
check("R0 _rehydrate restores research flag from disk", _dr["research"] is True)
check("R0 _rehydrate restores plan from disk",
      len(_dr["plan"]) == 1 and _dr["plan"][0]["text"] == "Find local hive suppliers")
check("R0 _rehydrate restores scratch from disk",
      _dr["scratch"] == ["Spring is the time to start"])
RS()

# --- v1.14.0 R1: /research detection + plan injection + reconcile ------------
# _parse_research grammar
check("R1 /research <goal> -> research_new",
      tp._parse_research("/research compare pnpm vs npm speed")
      == ("research_new", "compare pnpm vs npm speed"))
check("R1 /research done -> research_done",
      tp._parse_research("/research done") == ("research_done", None))
check("R1 /research synthesize -> research_done",
      tp._parse_research("/research synthesize") == ("research_done", None))
check("R1 bare /research -> research_status",
      tp._parse_research("/research") == ("research_status", None))
check("R1 /research status -> research_status",
      tp._parse_research("/research status") == ("research_status", None))
check("R1 non-command -> None",
      tp._parse_research("please research motorcycles for me") is None)

# _seed_plan seeds a single open item = the goal
_rp = tp._open_topic("x", [], 10.0)
tp._seed_plan(dict(tp.DEFAULTS), _rp, "compare pnpm vs npm install speed")
check("R1 _seed_plan seeds one open item",
      len(_rp["plan"]) == 1 and _rp["plan"][0]["status"] == "open"
      and _rp["plan"][0]["text"] == "compare pnpm vs npm install speed"
      and _rp["plan"][0]["src"] == "")
check("R1 _seed_plan no-op on empty goal",
      (lambda r: (tp._seed_plan(dict(tp.DEFAULTS), r, "  "), r["plan"] == [])[1])(
          tp._open_topic("y", [], 11.0)))

# _apply_research: research_new opens a research topic with a plan
RS()
_arcfg = dict(tp.DEFAULTS, research="on")
_r_new = tp._apply_research(("research_new", "best all-season moto tyres"),
                            "SR", 100.0, _arcfg)
check("R1 _apply_research research_new -> research topic + plan",
      _r_new is not None and _r_new["research"] is True
      and len(_r_new["plan"]) == 1
      and _r_new["plan"][0]["text"] == "best all-season moto tyres")
tp._active_topic["SR"] = _r_new["id"]
_r_done = tp._apply_research(("research_done", None), "SR", 101.0, _arcfg)
check("R1 _apply_research research_done sets _synthesize on active",
      _r_done is _r_new and _r_done.get("_synthesize") is True)
check("R1 _apply_research done/status with nothing active -> None",
      tp._apply_research(("research_done", None), "NOBODY", 102.0, _arcfg) is None)

# build_topic_block WORK mode renders the plan + a search directive (flag on)
_wb = tp._open_topic("Tyre research", ["tyre"], 200.0)
_wb["research"] = True
_wb["plan"] = [{"text": "wet-grip ratings", "status": "open", "src": ""},
               {"text": "tread life vs price", "status": "done",
                "src": "https://ex.com/t"}]
_blk_on = tp.build_topic_block(_wb, dict(tp.DEFAULTS, research="on"))
check("R1 block WORK mode names the plan + search directive",
      "web_search" in _blk_on and "- [ ] wet-grip ratings" in _blk_on
      and "- [x] tread life vs price [src: https://ex.com/t]" in _blk_on)
# INERT: with research flag OFF the block has NO plan section
_blk_off = tp.build_topic_block(_wb, dict(tp.DEFAULTS, research="off"))
check("R1 block INERT when research flag off (no plan leak)",
      "web_search" not in _blk_off and "wet-grip ratings" not in _blk_off)
# SYNTHESIZE mode when all items done
_sb = tp._open_topic("Done research", ["x"], 201.0)
_sb["research"] = True
_sb["plan"] = [{"text": "q1", "status": "done", "src": "https://ex.com/1"}]
_blk_syn = tp.build_topic_block(_sb, dict(tp.DEFAULTS, research="on"))
check("R1 block SYNTHESIZE mode when all items done",
      "final answer" in _blk_syn.lower())
# SYNTHESIZE also when _synthesize transient set despite an open item
_sb["plan"] = [{"text": "q1", "status": "open", "src": ""}]
_sb["_synthesize"] = True
_blk_syn2 = tp.build_topic_block(_sb, dict(tp.DEFAULTS, research="on"))
check("R1 block SYNTHESIZE honored via _synthesize transient",
      "final answer" in _blk_syn2.lower())

# _reconcile_plan flips a matching open item to done + stamps src
_rc = tp._open_topic("Reconcile", ["x"], 202.0)
_rc["research"] = True
_rc["plan"] = [{"text": "wet grip ratings for touring tyres", "status": "open",
                "src": ""},
               {"text": "tread life comparison", "status": "open", "src": ""}]
tp._reconcile_plan(dict(tp.DEFAULTS, research="on"), _rc,
                   ["Michelin Road 6 wet grip ratings tested [src: https://ex.com/wg]"])
_it0 = _rc["plan"][0]
check("R1 _reconcile flips matching open item to done + src",
      _it0["status"] == "done" and _it0["src"] == "https://ex.com/wg")
check("R1 _reconcile leaves the unmatched item open",
      _rc["plan"][1]["status"] == "open")
# R1-fix: reconcile uses the overlap COEFFICIENT (normalize by the smaller set).
# Normalizing by the ITEM made the broad single-item plan that /research seeds
# (decompose off) mathematically unsatisfiable — measured in the multi-turn eval,
# the checklist stayed [ ] across every turn. These pin the corrected behaviour.
def _rc_case(item_text, finding):
    r = {"research": True,
         "plan": [{"text": item_text, "status": "open", "src": ""}]}
    tp._reconcile_plan(dict(tp.DEFAULTS, research="on"), r, [finding])
    return r["plan"][0]["status"]


check("R1-fix broad single-item plan CAN reconcile (was impossible)",
      _rc_case("compare the Honda CB500X and Kawasaki Versys 650 for a "
               "Vancouver year-round commuter",
               "Honda CB500X Specs and Review [src: https://ex.com/a]") == "done")
check("R1-fix decomposed short item still reconciles",
      _rc_case("Honda CB500X specs",
               "Honda CB500X Specs and Review [src: https://ex.com/a]") == "done")
check("R1-fix unrelated finding does NOT tick an item",
      _rc_case("wet grip ratings",
               "Kawasaki Versys 650 MSRP price Canada [src: https://ex.com/b]") == "open")
check("R1-fix a single incidental shared word does NOT tick an item",
      _rc_case("tread life versus price",
               "Honda CB500X price [src: https://ex.com/c]") == "open")
check("R1-fix genuine multi-term match ticks",
      _rc_case("wet grip ratings",
               "Michelin tyre wet grip test results [src: https://ex.com/d]") == "done")

# INERT: flag off -> reconcile is a no-op
_rc2 = tp._open_topic("Reconcile2", ["x"], 203.0)
_rc2["research"] = True
_rc2["plan"] = [{"text": "wet grip ratings for touring tyres", "status": "open",
                 "src": ""}]
tp._reconcile_plan(dict(tp.DEFAULTS, research="off"), _rc2,
                   ["wet grip ratings tested [src: https://ex.com/wg]"])
check("R1 _reconcile INERT when flag off",
      _rc2["plan"][0]["status"] == "open")

# end-to-end: /research routes to a research topic and injects the plan
RS()
_orig_cfg2 = tp._cfg
tp._cfg = lambda: dict(tp.DEFAULTS, enabled="on", research="on", classify="heuristic")
try:
    _res = tp._tp_pre_llm(session_id="E", turn_id="t1",
                          user_message="/research compare pnpm vs npm install speed")
    check("R1 e2e: /research injects a context block",
          isinstance(_res, dict) and "context" in _res)
    _etid = tp._active_topic.get("E")
    check("R1 e2e: active topic is flagged research with a seeded plan",
          _etid in tp._topics and tp._topics[_etid]["research"] is True
          and len(tp._topics[_etid]["plan"]) == 1)
    check("R1 e2e: injected block carries the search directive",
          "web_search" in _res.get("context", ""))
    # flag OFF: /research is NOT intercepted (flows as ordinary text/topic)
    tp._cfg = lambda: dict(tp.DEFAULTS, enabled="on", research="off",
                           classify="heuristic")
    _res_off = tp._tp_pre_llm(session_id="E2", turn_id="t1",
                              user_message="/research some new thing here")
    _e2tid = tp._active_topic.get("E2")
    check("R1 e2e INERT: flag off -> topic not flagged research",
          _e2tid not in tp._topics or tp._topics[_e2tid]["research"] is False)
finally:
    tp._cfg = _orig_cfg2
RS()

# --- v1.14.0 R2: LLM decompose + auto-detect + scratch capture --------------
# _decompose_plan drives _seed_plan when research_decompose=llm
_saved_ctx2 = tp._ctx
try:
    tp._ctx = _FakeCtxLlm(_FakeLlm(
        {"items": ["wet grip ratings", "tread life vs price", "noise levels"]}))
    _dp = tp._decompose_plan(dict(tp.DEFAULTS), "best all-season touring tyre")
    check("R2 _decompose_plan returns the LLM sub-questions",
          _dp == ["wet grip ratings", "tread life vs price", "noise levels"])
    _dr2 = tp._open_topic("Tyres", [], 300.0)
    tp._seed_plan(dict(tp.DEFAULTS, research_decompose="llm"), _dr2,
                  "best all-season touring tyre")
    check("R2 _seed_plan(llm) seeds a multi-item plan from decompose",
          len(_dr2["plan"]) == 3 and _dr2["plan"][0]["text"] == "wet grip ratings"
          and all(it["status"] == "open" for it in _dr2["plan"]))
    # decompose OFF -> single-item plan (no LLM call)
    _dr3 = tp._open_topic("Tyres2", [], 301.0)
    tp._seed_plan(dict(tp.DEFAULTS, research_decompose="off"), _dr3,
                  "best all-season touring tyre")
    check("R2 _seed_plan(off) falls back to a single-item plan",
          len(_dr3["plan"]) == 1
          and _dr3["plan"][0]["text"] == "best all-season touring tyre")
    # plan_max_items caps the decomposition
    tp._ctx = _FakeCtxLlm(_FakeLlm({"items": ["a", "b", "c", "d", "e", "f", "g", "h"]}))
    _dr4 = tp._open_topic("Big", [], 302.0)
    tp._seed_plan(dict(tp.DEFAULTS, research_decompose="llm", plan_max_items=4),
                  _dr4, "goal")
    check("R2 _seed_plan(llm) caps at plan_max_items", len(_dr4["plan"]) == 4)
    # decompose failure -> graceful single-item fallback
    class _BoomLlm2:
        def complete_structured(self, **kw):
            raise RuntimeError("boom")
    tp._ctx = _FakeCtxLlm(_BoomLlm2())
    _dr5 = tp._open_topic("Boom", [], 303.0)
    tp._seed_plan(dict(tp.DEFAULTS, research_decompose="llm"), _dr5, "some goal")
    check("R2 _seed_plan(llm) survives a decompose failure -> single item",
          len(_dr5["plan"]) == 1 and _dr5["plan"][0]["text"] == "some goal")
finally:
    tp._ctx = _saved_ctx2

# auto-detect: research_auto promotes a FRESH topic on a research-intent message
RS()
_orig_cfg3 = tp._cfg
try:
    tp._cfg = lambda: dict(tp.DEFAULTS, enabled="on", research_auto="on",
                           classify="heuristic")
    tp._tp_pre_llm(session_id="AP", turn_id="t1",
                   user_message="compare pnpm vs npm install speed for the monorepo")
    _aptid = tp._active_topic.get("AP")
    check("R2 auto-promote: research-intent on a fresh topic -> research+plan",
          _aptid in tp._topics and tp._topics[_aptid]["research"] is True
          and len(tp._topics[_aptid]["plan"]) >= 1)
    # a NON-research message does not auto-promote even with the flag on
    tp._tp_pre_llm(session_id="AP2", turn_id="t1",
                   user_message="what's a good sourdough recipe")
    _ap2 = tp._active_topic.get("AP2")
    check("R2 auto-promote: non-research message stays a plain topic",
          _ap2 in tp._topics and tp._topics[_ap2]["research"] is False)
    # INERT: flag off -> a research-intent message is NOT promoted (fresh store so
    # it can't route to the already-research topic seeded above)
    RS()
    tp._cfg = lambda: dict(tp.DEFAULTS, enabled="on", research_auto="off",
                           classify="heuristic")
    tp._tp_pre_llm(session_id="AP3", turn_id="t1",
                   user_message="compare pnpm vs npm install speed for the monorepo")
    _ap3 = tp._active_topic.get("AP3")
    check("R2 auto-promote INERT when research_auto off",
          _ap3 in tp._topics and tp._topics[_ap3]["research"] is False)
finally:
    tp._cfg = _orig_cfg3
RS()

# scratch capture: non-src harvest lines land in rec['scratch'] when scratchpad on
_orig_cfg4 = tp._cfg
try:
    tp._cfg = lambda: dict(tp.DEFAULTS, enabled="on", research="on",
                           scratchpad="on", classify="heuristic")
    # seed a research topic on this session
    tp._tp_pre_llm(session_id="SC", turn_id="t1",
                   user_message="/research compare tyre grip options")
    _sctid = tp._active_topic.get("SC")
    _hist = [{"role": "tool",
              "content": "Michelin Road 6 offers excellent wet grip and long "
                         "tread life, a strong all-season touring choice."}]
    tp._tp_post_llm(session_id="SC", turn_id="t1",
                    user_message="/research compare tyre grip options",
                    assistant_response="Looking into it.",
                    conversation_history=_hist)
    check("R2 scratch capture: non-src harvest note stored on the research topic",
          _sctid in tp._topics and any("Michelin Road 6" in s
                                       for s in tp._topics[_sctid]["scratch"]))
    # INERT: scratchpad off -> nothing captured
    tp._cfg = lambda: dict(tp.DEFAULTS, enabled="on", research="on",
                           scratchpad="off", classify="heuristic")
    tp._tp_pre_llm(session_id="SC2", turn_id="t1",
                   user_message="/research another grip question")
    _sc2 = tp._active_topic.get("SC2")
    tp._tp_post_llm(session_id="SC2", turn_id="t1",
                    user_message="/research another grip question",
                    assistant_response="ok", conversation_history=_hist)
    check("R2 scratch capture INERT when scratchpad off",
          _sc2 in tp._topics and tp._topics[_sc2]["scratch"] == [])
finally:
    tp._cfg = _orig_cfg4
RS()

# --- v1.14.0 R3: resume acknowledgment + self-knowledge + active recall ------
# base rec used across R3 block tests
def _mk_resumed():
    r = tp._open_topic("Vet visit for the dog", ["dog", "vet"], 400.0)
    r["id"] = 42
    r["summary"] = "The dog needed a dental cleaning."
    r["facts"] = ["Dog is a 6yo beagle", "Vet is Dr. Lee",
                  "Cleaning quoted at 300 EUR", "Next visit in autumn",
                  "Dog dislikes the carrier", "Owner prefers morning slots"]
    r["_resumed"] = True
    return r

# resume header + invite line (resume_surface on)
_blk_res = tp.build_topic_block(_mk_resumed(),
                                dict(tp.DEFAULTS, resume_surface="on"))
check("R3 resume uses the RESUME header (relaxes topic-number naming)",
      "RESUMING after a gap" in _blk_res)
check("R3 resume appends an acknowledgment invite with the t#NNNNN tag",
      "t#00042" in _blk_res and "picking this thread back up" in _blk_res)
# INERT: resume_surface off -> normal silent header, NO tag leak
_blk_res_off = tp.build_topic_block(_mk_resumed(),
                                    dict(tp.DEFAULTS, resume_surface="off"))
check("R3 resume INERT when flag off: normal header, no t#NNNNN in block",
      "RESUMING after a gap" not in _blk_res_off and "t#00042" not in _blk_res_off
      and "Do not mention these notes" in _blk_res_off)
# voice suppresses the spoken tag but keeps the acknowledgment
_blk_res_voice = tp.build_topic_block(_mk_resumed(),
                                      dict(tp.DEFAULTS, resume_surface="on"),
                                      voice=True)
check("R3 voice session: acknowledgment kept but t#NNNNN tag suppressed",
      "picking this thread back up" in _blk_res_voice
      and "t#00042" not in _blk_res_voice)

# self_knowledge injection
_blk_sk = tp.build_topic_block(_mk_resumed(),
                               dict(tp.DEFAULTS, self_knowledge="on"))
check("R3 self_knowledge on -> memory self-knowledge note injected",
      "you carry memory across conversations" in _blk_sk)
_blk_sk_off = tp.build_topic_block(_mk_resumed(), dict(tp.DEFAULTS))
check("R3 self_knowledge off -> no note (inert)",
      "you carry memory across conversations" not in _blk_sk_off)

# active_recall: permission line + widened fact budget
_blk_ar = tp.build_topic_block(_mk_resumed(),
                               dict(tp.DEFAULTS, active_recall="on",
                                    recall_facts_max=6))
check("R3 active_recall on -> surfacing-permission line present",
      "recalling what they told you" in _blk_ar)
check("R3 active_recall widens the fact budget to recall_facts_max",
      "Owner prefers morning slots" in _blk_ar)  # the 6th fact
_blk_ar_off = tp.build_topic_block(_mk_resumed(), dict(tp.DEFAULTS))
check("R3 active_recall off -> no permission line, facts capped at 5",
      "recalling what they told you" not in _blk_ar_off
      and "Owner prefers morning slots" not in _blk_ar_off)

# DEFAULT (all R3 flags off) block is byte-identical to a pre-R3 hand build for a
# non-research, non-resumed topic: header + facts(5) + verify line, no extras.
_plain = tp._open_topic("Plain topic", ["x"], 401.0)
_plain["summary"] = "Talking about x."
_plainblk = tp.build_topic_block(_plain, dict(tp.DEFAULTS))
check("R3 default block keeps the SILENT header (anti-disclosure intact)",
      _plainblk.startswith(tp._BLOCK_HEADER)
      and "RESUMING" not in _plainblk
      and "you carry memory" not in _plainblk
      and "recalling what they told you" not in _plainblk)

# transient _resumed lifecycle: set on dormant resume, popped after the turn
RS()
_orig_cfg5 = tp._cfg
try:
    tp._cfg = lambda: dict(tp.DEFAULTS, enabled="on", resume_surface="on",
                           classify="heuristic")
    # create + persist a dormant topic with a distinctive entity
    _dt = tp._open_topic("Motorcycle valve clearance", ["motorcycle", "valve",
                                                        "clearance", "shim"], 500.0)
    _dt["status"] = "dormant"
    _dt["summary"] = "Adjusting valve clearance on the bike."
    tp._topics[_dt["id"]] = _dt
    tp._persist_topic(tp._cfg(), _dt)
    # a fresh session whose message matches the dormant topic -> resume
    _res5 = tp._tp_pre_llm(session_id="RSU", turn_id="t1",
                           user_message="back to the motorcycle valve clearance shim question")
    _rtid = tp._active_topic.get("RSU")
    check("R3 lifecycle: dormant match sets _resumed True this turn",
          _rtid in tp._topics and tp._topics[_rtid].get("_resumed") is True)
    check("R3 lifecycle: resumed block carried the RESUME framing",
          isinstance(_res5, dict) and "RESUMING after a gap" in _res5.get("context", ""))
    # post_llm clears the transient so it never leaks into the next turn
    tp._tp_post_llm(session_id="RSU", turn_id="t1",
                    user_message="back to the motorcycle valve clearance shim question",
                    assistant_response="Sure, picking that back up.",
                    conversation_history=[])
    check("R3 lifecycle: _resumed popped after the turn",
          "_resumed" not in tp._topics[_rtid])
finally:
    tp._cfg = _orig_cfg5
RS()

# --- v1.14.0 R4: conclude-and-store + autostore (inbox-only) -----------------
import glob as _glob  # noqa: E402


def _read_inbox(tid):
    hits = _glob.glob(os.path.join(TMP_WIKI, "inbox", "hermes",
                                   "topic-t%05d-*.md" % int(tid)))
    return open(hits[0], encoding="utf-8").read() if hits else ""


# conclude-and-store: _curation_note carries the research checklist + scratch,
# to inbox/hermes ONLY (never wiki/), with a target: frontmatter for merge.
RS()
_cs = tp._open_topic("Compare touring tyres", ["tyre", "touring"], 600.0)
_cs["research"] = True
_cs["summary"] = "Comparing all-season touring tyres."
_cs["facts"] = ["User rides a BMW R1250GS"]
_cs["plan"] = [{"text": "wet grip ratings", "status": "done",
                "src": "https://ex.com/wg"},
               {"text": "tread life vs price", "status": "open", "src": ""}]
_cs["scratch"] = ["Michelin Road 6 is a strong all-rounder"]
tp._topics[_cs["id"]] = _cs
tp._curation_note(dict(tp.DEFAULTS), _cs)
_note = _read_inbox(_cs["id"])
check("R4 curation note lands in inbox/hermes (not wiki/)",
      _note and "target: jobs/" in _note
      and not os.path.exists(os.path.join(TMP_WIKI, "index.md")))
check("R4 curation note carries the research checklist w/ done+src + open item",
      "Research checklist:" in _note
      and "- [x] wet grip ratings [src: https://ex.com/wg]" in _note
      and "- [ ] tread life vs price" in _note)
check("R4 curation note carries the working notes (scratch)",
      "Working notes:" in _note and "Michelin Road 6 is a strong all-rounder" in _note)
# a NON-research topic omits the research sections
_cs2 = tp._open_topic("Plain chat", ["x"], 601.0)
_cs2["summary"] = "Just chatting."
_cs2["facts"] = ["User likes tea"]
tp._curation_note(dict(tp.DEFAULTS), _cs2)
_note2 = _read_inbox(_cs2["id"])
check("R4 non-research curation note omits the checklist",
      _note2 and "Research checklist:" not in _note2 and "User likes tea" in _note2)
RS()

# _extract_facts pulls durable first-person facts via the LLM
_saved_ctx3 = tp._ctx
try:
    tp._ctx = _FakeCtxLlm(_FakeLlm(
        {"facts": ["User rides a BMW R1250GS", "User commutes 40km daily"]}))
    _ef = tp._extract_facts(dict(tp.DEFAULTS),
                            "I ride a BMW R1250GS and commute 40km a day")
    check("R4 _extract_facts returns durable user facts",
          _ef == ["User rides a BMW R1250GS", "User commutes 40km daily"])
    # empty / no-fact extraction is fine
    tp._ctx = _FakeCtxLlm(_FakeLlm({"facts": []}))
    check("R4 _extract_facts returns [] when nothing durable",
          tp._extract_facts(dict(tp.DEFAULTS), "what time is it?") == [])
finally:
    tp._ctx = _saved_ctx3

# autostore wiring: amortized extraction into rec['facts'] + inbox note (on),
# fully inert when the flag is off.
RS()
_orig_cfg6 = tp._cfg
_saved_ctx4 = tp._ctx
try:
    tp._ctx = _FakeCtxLlm(_FakeLlm({"facts": ["User rides a BMW R1250GS"]}))
    tp._cfg = lambda: dict(tp.DEFAULTS, enabled="on", autostore="on",
                           autostore_every=1, classify="heuristic")
    tp._tp_pre_llm(session_id="AS", turn_id="t1",
                   user_message="I ride a BMW R1250GS, any tips for winter?")
    _astid = tp._active_topic.get("AS")
    tp._tp_post_llm(session_id="AS", turn_id="t1",
                    user_message="I ride a BMW R1250GS, any tips for winter?",
                    assistant_response="Sure — here are some winter tips.",
                    conversation_history=[])
    check("R4 autostore ON: durable fact stored on the topic",
          _astid in tp._topics
          and "User rides a BMW R1250GS" in tp._topics[_astid]["facts"])
    check("R4 autostore ON: inbox curation note written",
          "User rides a BMW R1250GS" in _read_inbox(_astid))
    # INERT: autostore off -> no fact extracted, no note (fresh store + fresh
    # inbox so a reused topic id can't surface a stale note from an earlier test)
    RS()
    shutil.rmtree(os.path.join(TMP_WIKI, "inbox"), ignore_errors=True)
    tp._ctx = _FakeCtxLlm(_FakeLlm({"facts": ["User rides a BMW R1250GS"]}))
    tp._cfg = lambda: dict(tp.DEFAULTS, enabled="on", autostore="off",
                           autostore_every=1, classify="heuristic")
    tp._tp_pre_llm(session_id="AS2", turn_id="t1",
                   user_message="I ride a BMW R1250GS, any winter tips?")
    _as2 = tp._active_topic.get("AS2")
    tp._tp_post_llm(session_id="AS2", turn_id="t1",
                    user_message="I ride a BMW R1250GS, any winter tips?",
                    assistant_response="Sure.", conversation_history=[])
    check("R4 autostore INERT when flag off: no auto-fact, no note",
          _as2 in tp._topics
          and "User rides a BMW R1250GS" not in tp._topics[_as2]["facts"]
          and _read_inbox(_as2) == "")
finally:
    tp._cfg = _orig_cfg6
    tp._ctx = _saved_ctx4
RS()

# --- v1.15.0: WEAK-MODEL-HOST gating of continuity injection + badge ----------
# On a clean frontier host the routing/classification/storage still runs, but
# the resumption CONTEXT INJECTION and the badge are INERT (deepseek ≈ vanilla).
_orig_cfg_wh = tp._cfg
tp._cfg = lambda: dict(tp.DEFAULTS, enabled="on", classify="heuristic",
                       badge="on")
try:
    # STRONG host: pre_llm routes but injects NO context; badge suppressed.
    _tp_early._on_weak_host = lambda *a, **k: False
    RS()
    r = tp._tp_pre_llm(session_id="WH", turn_id="t1",
                       user_message="How do I adjust valve clearance on a BMW?")
    check("strong host: continuity context injection INERT (returns None)",
          r is None)
    check("strong host: topic STILL routed/stored (classification preserved)",
          tp._active_topic.get("WH") in tp._topics)
    # v1.20.0 SPLIT the badge from the injection: the label is appended by the
    # finisher and cannot derail a turn, and without it a strong-tier bot gives
    # the user no handle to reference a thread by. Suppression now requires the
    # explicit badge_strong:off rollback (asserted in the v1.20.0 section).
    check("strong host: badge now shown (v1.20.0 default)",
          tp.badge_for("WH").startswith("t#"))
    # WEAK host: same message injects context + badges.
    _tp_early._on_weak_host = lambda *a, **k: True
    RS()
    r = tp._tp_pre_llm(session_id="WH2", turn_id="t1",
                       user_message="How do I adjust valve clearance on a BMW?")
    check("weak host: continuity context INJECTED",
          isinstance(r, dict) and "Background continuity notes" in r["context"])
    check("weak host: badge present", tp.badge_for("WH2") == "t#%05d"
          % int(tp._active_topic["WH2"]))
finally:
    tp._cfg = _orig_cfg_wh
    _tp_early._on_weak_host = lambda *a, **k: True
RS()

# --- v1.16.1: strong-tier injection is BLANKET-INERT (W12 reverted) ----------
# v1.16.0's W12 signal-gated injection re-opened a strong-tier resumption path
# that over-fired on 3/44 DeepSeek prompts (batch8), contributing to the
# clarify_5 "Fix it." derail. v1.16.1 restores v1.15.0's blanket-inert: on
# strong tier _tp_pre_llm NEVER injects, regardless of any resumption signal.
# Routing/classification/storage still run on every tier.
_orig_cfg_sg = tp._cfg
tp._cfg = lambda: dict(tp.DEFAULTS, enabled="on", classify="heuristic")
# _resume_signal_ok survives as a retired pure predicate (no longer wired into
# the strong injection path); its deterministic gate logic is still asserted.
_rec_content = {"title": "BMW valve clearance", "turns": 3,
                "entities": ["valve", "clearance", "bmw", "motorcycle"],
                "recent": [("how to adjust?", "use a feeler gauge")]}
_rec_fresh = {"title": "BMW valve clearance", "turns": 0, "entities": [],
              "recent": []}
check("retired gate: fresh topic (no content) -> False",
      tp._resume_signal_ok(_rec_fresh, "and the valve clearance spec?",
                           tp.DEFAULTS) is False)
check("retired gate: content + continuation + overlap -> True",
      tp._resume_signal_ok(_rec_content, "and the valve clearance spec?",
                           tp.DEFAULTS) is True)
# integration through _tp_pre_llm on a strong tier: BLANKET-INERT now.
_tp_early._on_weak_host = lambda *a, **k: False   # strong tier
try:
    RS()
    tp._tp_pre_llm(session_id="SG", turn_id="t1",
                   user_message="How do I adjust valve clearance on a BMW?")
    tp._tp_post_llm(session_id="SG", turn_id="t1",
                    user_message="How do I adjust valve clearance on a BMW?",
                    assistant_response="Use a 0.15mm feeler gauge on the intake.")
    # even a GENUINE continuation+overlap that W12 would have injected must now
    # return None on strong (the batch8 regression close).
    _r_cont = tp._tp_pre_llm(session_id="SG", turn_id="t2",
                             user_message="and the valve clearance spec?")
    check("v1.16.1 strong: genuine continuation -> INERT (blanket, no inject)",
          _r_cont is None)
    # a short ambiguous prompt matching a loosely-titled thread (the "Fix it."
    # class) must also never inject on strong.
    _r_fix = tp._tp_pre_llm(session_id="SG", turn_id="t3",
                            user_message="Fix it.")
    check("v1.16.1 strong: short ambiguous 'Fix it.' -> INERT (no derail)",
          _r_fix is None)
    _r_triv = tp._tp_pre_llm(session_id="SG", turn_id="t4",
                             user_message="What is the capital of France?")
    check("v1.16.1 strong: unrelated one-off -> INERT", _r_triv is None)
    # topic routing/storage STILL happened on every one of those turns.
    check("v1.16.1 strong: routing/storage preserved despite inert injection",
          tp._active_topic.get("SG") in tp._topics)
finally:
    tp._cfg = _orig_cfg_sg
    _tp_early._on_weak_host = lambda *a, **k: True
RS()

# ---------------------------------------------------------------------------
# v1.19.0: topic listing — the topics_list tool and /topic list rendering.
# ---------------------------------------------------------------------------
print("\n== v1.19.0 listing: duration + age helpers ==")
check("parse '7d'", tp.parse_duration_hours("7d") == 168.0)
check("parse '12h'", tp.parse_duration_hours("12h") == 12.0)
check("parse '30m'", tp.parse_duration_hours("30m") == 0.5)
check("parse '2w'", tp.parse_duration_hours("2w") == 336.0)
check("parse garbage -> None", tp.parse_duration_hours("soon") is None)
check("parse empty -> None", tp.parse_duration_hours("") is None)
check("age seconds", tp._age_str(42) == "42s")
check("age minutes", tp._age_str(600) == "10m")
check("age hours", tp._age_str(7200) == "2h")
check("age days", tp._age_str(259200) == "3d")
check("age weeks", tp._age_str(1209600) == "2w")
check("age of nonsense -> '?'", tp._age_str("x") == "?")

RS()
_LNOW = 1_800_000_000.0
_lcfg = dict(tp.DEFAULTS, enabled="on", wiki_dir=TMP_WIKI)


def _mk(title, ents, age_hours, turns, status="open", summary="", research=False):
    rec = tp._open_topic(title, ents, _LNOW - age_hours * 3600.0)
    rec["last_active"] = _LNOW - age_hours * 3600.0
    rec["turns"] = turns
    rec["status"] = status
    rec["summary"] = summary
    rec["research"] = research
    return rec


_mk("Duplex calling on Telegram", ["duplex", "telegram", "call"], 1, 4)
_mk("Quake raycaster in pygame", ["quake", "pygame", "raycaster"], 30, 12)
_mk("Garbage collection schedule", ["garbage", "collection"], 200, 2,
    status="dormant")
_mk("Board decision BD-2026-014", ["board", "decision"], 5, 7, status="closed")
_mk("Silverton itinerary", ["silverton", "itinerary"], 400, 3,
    status="dormant", summary="Trip planning with duplex radio notes")
_mk("Vault zone isolation", ["vault", "zone"], 2, 1, research=True)

print("\n== v1.19.0 listing: filters ==")
_rows, _m = tp.list_topics(_lcfg, now=_LNOW)
check("no filters returns every thread", _m == 6 and len(_rows) == 6)
_rows, _m = tp.list_topics(_lcfg, status="open", now=_LNOW)
check("status=open filters", _m == 3 and all(r["status"] == "open" for r in _rows))
_rows, _m = tp.list_topics(_lcfg, status="dormant", now=_LNOW)
check("status=dormant filters", _m == 2)
_rows, _m = tp.list_topics(_lcfg, status="closed", now=_LNOW)
check("status=closed filters", _m == 1)
_rows, _m = tp.list_topics(_lcfg, status="bogus", now=_LNOW)
check("invalid status falls back to all", _m == 6)
_rows, _m = tp.list_topics(_lcfg, since_hours=24, now=_LNOW)
check("since_hours=24 keeps only recent", _m == 3)
_rows, _m = tp.list_topics(_lcfg, since_hours=0.5, now=_LNOW)
check("since_hours=0.5 keeps none", _m == 0)
_rows, _m = tp.list_topics(_lcfg, contains="duplex", now=_LNOW)
check("contains matches title AND summary", _m == 2)
_rows, _m = tp.list_topics(_lcfg, contains="PYGAME", now=_LNOW)
check("contains is case-insensitive and hits entities", _m == 1)
_rows, _m = tp.list_topics(_lcfg, contains="nothingmatchesthis", now=_LNOW)
check("contains with no match -> empty", _m == 0 and _rows == [])
_rows, _m = tp.list_topics(_lcfg, status="dormant", contains="silverton",
                           now=_LNOW)
check("filters compose", _m == 1 and _rows[0]["title"].startswith("Silverton"))

print("\n== v1.19.0 listing: sorting + limit ==")
_rows, _ = tp.list_topics(_lcfg, sort="last_active", now=_LNOW)
check("default sort is most-recent-first", _rows[0]["title"].startswith("Duplex"))
_rows, _ = tp.list_topics(_lcfg, sort="last_active", order="asc", now=_LNOW)
check("order=asc reverses", _rows[0]["title"].startswith("Silverton"))
_rows, _ = tp.list_topics(_lcfg, sort="turns", now=_LNOW)
check("sort=turns picks the longest thread", _rows[0]["turns"] == 12)
_rows, _ = tp.list_topics(_lcfg, sort="turns", order="asc", now=_LNOW)
check("sort=turns asc picks the shortest", _rows[0]["turns"] == 1)
_rows, _ = tp.list_topics(_lcfg, sort="id", order="asc", now=_LNOW)
check("sort=id asc is creation order", _rows[0]["id"] == 1)
_rows, _ = tp.list_topics(_lcfg, sort="bogus", now=_LNOW)
check("invalid sort falls back to last_active",
      _rows[0]["title"].startswith("Duplex"))
_rows, _m = tp.list_topics(_lcfg, limit=2, now=_LNOW)
check("limit caps rows but matched reports the true total",
      len(_rows) == 2 and _m == 6)
_rows, _ = tp.list_topics(_lcfg, limit=9999, now=_LNOW)
check("limit clamped to the max", len(_rows) <= tp._LIST_LIMIT_MAX)
_rows, _ = tp.list_topics(_lcfg, limit=0, now=_LNOW)
check("limit=0 clamped up to 1", len(_rows) == 1)
_rows, _ = tp.list_topics(_lcfg, limit="junk", now=_LNOW)
check("non-numeric limit falls back to default", len(_rows) == 6)
_row = tp.list_topics(_lcfg, contains="duplex calling", now=_LNOW)[0][0]
check("row carries the t#NNNNN tag", _row["tag"] == "t#%05d" % _row["id"])
check("row carries a human age", _row["age"] == "1h")

print("\n== v1.19.0 listing: rendering ==")
_r = tp.render_listing(*tp.list_topics(_lcfg, status="open", now=_LNOW),
                       header="Open topics")
check("render shows the count line", "showing 3 of 3" in _r)
check("render includes tags", "t#00001" in _r)
check("render pluralises turns", "4 turns" in _r)
_r1 = tp.render_listing(*tp.list_topics(_lcfg, contains="vault", now=_LNOW))
check("render singularises one turn", "1 turn," in _r1)
check("render flags a research thread", "research" in _r1)
_rd = tp.render_listing(*tp.list_topics(_lcfg, status="dormant", now=_LNOW))
check("render flags non-open status", "dormant" in _rd)
check("render of nothing says so",
      "none matched" in tp.render_listing([], 0))

print("\n== v1.19.0 listing: /topic list arg parsing ==")
check("bare list -> no filters", tp.parse_list_args("list") == {})
check("status token", tp.parse_list_args("open").get("status") == "open")
check("since token", tp.parse_list_args("since:7d").get("since_hours") == 168.0)
check("limit token", tp.parse_list_args("limit:5").get("limit") == 5)
check("sort token", tp.parse_list_args("sort:turns").get("sort") == "turns")
check("order token", tp.parse_list_args("asc").get("order") == "asc")
check("contains token", tp.parse_list_args("contains:duplex").get("contains") == "duplex")
check("bare word becomes contains", tp.parse_list_args("duplex").get("contains") == "duplex")
check("bare words join", tp.parse_list_args("duplex calling").get("contains")
      == "duplex calling")
_mix = tp.parse_list_args("dormant since:30d sort:turns asc limit:3 silverton")
check("mixed tokens all parse",
      _mix.get("status") == "dormant" and _mix.get("sort") == "turns"
      and _mix.get("order") == "asc" and _mix.get("limit") == 3
      and _mix.get("contains") == "silverton")
check("bad since ignored, not fatal", "since_hours" not in tp.parse_list_args("since:soon"))
check("bad limit ignored", "limit" not in tp.parse_list_args("limit:many"))
check("bad sort ignored", "sort" not in tp.parse_list_args("sort:colour"))

print("\n== v1.19.0 listing: /topic list context ==")
check("non-command message -> None", tp.listing_context(_lcfg, "how are you") is None)
check("/topic new is not a listing", tp.listing_context(_lcfg, "/topic new x") is None)
check("/topic close is not a listing", tp.listing_context(_lcfg, "/topic close") is None)
_lc = tp.listing_context(_lcfg, "/topic list", now=_LNOW)
check("/topic list renders", _lc is not None and "showing" in _lc)
check("listing block instructs presenting it as the answer",
      "Present these threads to the user" in _lc)
check("listing block preserves tags for reference", "t#00001" in _lc)
_lc2 = tp.listing_context(_lcfg, "/topic list open limit:1", now=_LNOW)
check("/topic list honours filters", "showing 1 of 3" in _lc2)
check("/topics plural alias works",
      tp.listing_context(_lcfg, "/topics", now=_LNOW) is not None)
check("/topic status alias works",
      tp.listing_context(_lcfg, "/topic status", now=_LNOW) is not None)

print("\n== v1.19.0 listing: pre_llm_call serves the command ==")
_saved_cfg = tp._cfg
tp._cfg = lambda: dict(_lcfg)
try:
    _before_active = dict(tp._active_topic)
    _res = tp._tp_pre_llm(session_id="SL", turn_id="l1", user_message="/topic list")
    check("pre_llm returns the listing as context",
          isinstance(_res, dict) and "showing" in _res.get("context", ""))
    check("a listing never joins or opens a thread",
          tp._active_topic.get("SL") is None)
    # the key property: an EXPLICIT command must work on strong tier too, where
    # continuity injection is blanket-inert.
    _saved_weak = _tp_early._on_weak_host
    _tp_early._on_weak_host = lambda *a, **k: False
    try:
        _res_s = tp._tp_pre_llm(session_id="SL2", turn_id="l2",
                                user_message="/topic list")
        check("STRONG tier: /topic list still served (explicit command)",
              isinstance(_res_s, dict) and "showing" in _res_s.get("context", ""))
        _res_n = tp._tp_pre_llm(session_id="SL2", turn_id="l3",
                                user_message="what is the capital of France?")
        check("STRONG tier: ordinary turn still inert", _res_n is None)
    finally:
        _tp_early._on_weak_host = _saved_weak
    tp._cfg = lambda: dict(_lcfg, enabled="off")
    check("disabled -> pre_llm ignores /topic list",
          tp._tp_pre_llm(session_id="SL3", turn_id="l4",
                         user_message="/topic list") is None)
finally:
    tp._cfg = _saved_cfg

print("\n== v1.19.0 listing: topics_list tool ==")
tp._cfg = lambda: dict(_lcfg)
try:
    # Anchored to list_topics rather than a literal: the strong-tier section
    # above routes an ordinary turn, which legitimately opens a thread.
    _total = tp.list_topics(_lcfg, now=_LNOW)[1]
    _out = json.loads(tp.topics_list_handler({}))
    check("handler returns JSON with topics + counts",
          _out["returned"] == _total and _out["matched"] == _total and _total >= 6)
    check("handler rows carry tag/title/status/turns/age",
          set(("tag", "title", "status", "turns", "age")) <= set(_out["topics"][0]))
    check("handler strips the raw epoch", "last_active" not in _out["topics"][0])
    _out2 = json.loads(tp.topics_list_handler({"status": "dormant", "limit": 1}))
    check("handler applies filters", _out2["returned"] == 1 and _out2["matched"] == 2)
    _out3 = json.loads(tp.topics_list_handler({"contains": "pygame"}))
    check("handler applies contains", _out3["matched"] == 1)
    _out4 = json.loads(tp.topics_list_handler({"sort": "turns"}))
    check("handler applies sort", _out4["topics"][0]["turns"] == 12)
    check("handler tolerates a non-dict arg",
          "topics" in json.loads(tp.topics_list_handler("nope")))
    check("check_fn true while enabled", tp.topics_list_available() is True)
    tp._cfg = lambda: dict(_lcfg, enabled="off")
    _off = json.loads(tp.topics_list_handler({}))
    check("disabled -> handler returns an explanatory error, not a crash",
          _off["topics"] == [] and "error" in _off)
    check("check_fn false while disabled", tp.topics_list_available() is False)
    tp._cfg = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    check("check_fn false when config read explodes",
          tp.topics_list_available() is False)
    check("handler survives a config explosion",
          "error" in json.loads(tp.topics_list_handler({})))
finally:
    tp._cfg = _saved_cfg

print("\n== v1.19.0 listing: tool registration ==")


class _FakeCtx:
    def __init__(self, boom=False):
        self.boom = boom
        self.registered = {}

    def register_tool(self, **kw):
        if self.boom:
            raise RuntimeError("registry rejected")
        # v1.20.0 registers two tools; keep them addressable by name.
        self.registered = kw
        self.by_name[kw["name"]] = kw

    by_name = None

    def __init_subclass__(cls):  # pragma: no cover
        pass


_FakeCtx.__init__ = lambda self, boom=False: (
    setattr(self, "boom", boom), setattr(self, "registered", None),
    setattr(self, "by_name", {}), setattr(self, "calls", 0))[0]

_ctx_ok = _FakeCtx()
check("registers on a capable ctx", tp.register_tool(_ctx_ok) is True)
check("registered under its own toolset",
      _ctx_ok.by_name["topics_list"].get("toolset") == "topics")
check("registered with the documented name",
      "topics_list" in _ctx_ok.by_name)
check("registered with a check_fn so the flag gates visibility",
      _ctx_ok.by_name["topics_list"].get("check_fn") is tp.topics_list_available)
_schema = _ctx_ok.by_name["topics_list"].get("schema") or {}
check("schema advertises the filter params",
      set(("status", "since_hours", "contains", "sort", "order", "limit"))
      == set(_schema["parameters"]["properties"]))
check("schema constrains status to the known set",
      set(_schema["parameters"]["properties"]["status"]["enum"]) == set(tp._LIST_STATUSES))


class _BareCtx:
    pass


check("ctx without register_tool -> skipped, not fatal",
      tp.register_tool(_BareCtx()) is False)
check("registry rejection -> skipped, not fatal",
      tp.register_tool(_FakeCtx(boom=True)) is False)

RS()

# ---------------------------------------------------------------------------
# v1.19.1: persist must never serialize an unloaded body over a real one.
# Regression for the silent data-loss bug — 69 of 82 live topics had been
# stripped to an empty ## log under a frontmatter still claiming turns: 6.
# ---------------------------------------------------------------------------
print("\n== v1.19.1 persist guard: body preservation ==")
_pcfg = dict(tp.DEFAULTS, enabled="on", wiki_dir=TMP_WIKI, classify="heuristic",
             summarize="off", max_open=2)
_saved_cfg2 = tp._cfg
tp._cfg = lambda: dict(_pcfg)
try:
    def _turn(sess, i, msg):
        tp._tp_pre_llm(session_id=sess, turn_id="p%s%d" % (sess, i), user_message=msg)
        tp._tp_post_llm(session_id=sess, turn_id="p%s%d" % (sess, i),
                        user_message=msg, assistant_response="Answer %d." % i)

    RS()
    _turn("S", 0, "tell me about vllm speculative decoding acceptance")
    _turn("S", 1, "and how does that interact with fp8 quantisation")
    _rec = tp._topics[tp._active_topic["S"]]
    _rec["summary"] = "Discussed vLLM spec decoding and fp8."
    _rec["findings"] = ["Acceptance 91.5% [src: https://x/y]"]
    _rec["facts"] = ["User runs vLLM on a shared GPU."]
    tp._persist_topic(_pcfg, _rec)
    _p = tp._topic_path(_pcfg, _rec["id"])
    _before = open(_p, encoding="utf-8").read()
    check("baseline thread persisted with a full body",
          _before.count("- u:") == 2 and "Discussed vLLM" in _before
          and "[src:" in _before)

    # gateway restart: records reload from the manifest with EMPTY bodies
    tp._reset_state()
    check("post-restart record is body-less", tp._topics == {})
    _turn("A", 0, "what is the weather in vancouver tomorrow")
    _turn("B", 0, "convert 84 kilograms into pounds please")
    _turn("C", 0, "write me a haiku about otters swimming")
    _after = open(_p, encoding="utf-8").read()
    check("EVICTION no longer erases the conversation log",
          _after.count("- u:") == 2)
    check("eviction preserves the summary", "Discussed vLLM" in _after)
    check("eviction preserves findings", "[src: https://x/y]" in _after)
    check("eviction preserves facts", "shared GPU" in _after)
    check("eviction still records the status change",
          "status: dormant" in _after)

    # /topic close is the other caller that persists an unrehydrated record
    tp._reset_state()
    _ov = tp._parse_override("/topic 1")
    tp._apply_override(_ov, "CL", time.time(), _pcfg)
    tp._apply_override(("close", None), "CL", time.time(), _pcfg)
    _closed = open(_p, encoding="utf-8").read()
    check("/topic close preserves the body too", _closed.count("- u:") == 2
          and "Discussed vLLM" in _closed)

    print("\n== v1.19.1 persist guard: merge + failure modes ==")
    tp._reset_state()
    tp._load_store(_pcfg)
    _cold = tp._topics[1]
    check("manifest-loaded record starts body-less",
          _cold.get("_body_loaded") is False and _cold["recent"] == [])
    _cold["recent"].append(("a brand new question", "a brand new answer"))
    tp._persist_topic(_pcfg, _cold)
    _merged = open(_p, encoding="utf-8").read()
    check("in-memory addition is KEPT when merging with disk",
          "a brand new question" in _merged)
    check("disk content is kept alongside it",
          "vllm speculative decoding" in _merged)
    check("merge marks the body loaded", _cold.get("_body_loaded") is True)

    # caps still apply after a merge
    tp._reset_state()
    tp._load_store(_pcfg)
    _c2 = tp._topics[1]
    for i in range(10):
        _c2["recent"].append(("q%d" % i, "a%d" % i))
    tp._persist_topic(_pcfg, _c2)
    check("merged log respects _RECENT_CAP",
          open(_p, encoding="utf-8").read().count("- u:") <= tp._RECENT_CAP)

    # unreadable body -> leave the file alone, still refresh the manifest
    tp._reset_state()
    tp._load_store(_pcfg)
    _c3 = tp._topics[1]
    _intact = open(_p, encoding="utf-8").read()
    _orig_parse = tp._parse_topic_md
    tp._parse_topic_md = lambda *_a, **_k: {}
    try:
        _c3["status"] = "closed"
        tp._persist_topic(_pcfg, _c3)
        check("unreadable body -> file left byte-identical",
              open(_p, encoding="utf-8").read() == _intact)
        check("unreadable body -> record NOT falsely marked loaded",
              _c3.get("_body_loaded") is False)
        _idx = json.load(open(tp._index_path(_pcfg), encoding="utf-8"))
        check("unreadable body -> manifest still refreshed",
              any(r.get("id") == 1 and r.get("status") == "closed"
                  for r in _idx["topics"]))
    finally:
        tp._parse_topic_md = _orig_parse

    # a record whose file does not exist yet must still write
    tp._reset_state()
    _fresh = tp._open_topic("Brand new thread", ["brand", "new"], time.time())
    _fresh["_body_loaded"] = False          # simulate the cold-record shape
    _fresh["recent"] = [("hello there", "hi back")]
    tp._persist_topic(_pcfg, _fresh)
    _fp = tp._topic_path(_pcfg, _fresh["id"])
    check("no file on disk -> body written, nothing lost",
          os.path.exists(_fp) and "hello there" in open(_fp, encoding="utf-8").read())

    check("hot record skips the preload entirely",
          (lambda r: (tp._load_body_for_write(_pcfg, r), r["recent"] == [])[1])(
              {"id": 1, "_body_loaded": True, "recent": []}))
finally:
    tp._cfg = _saved_cfg2
    RS()

# ---------------------------------------------------------------------------
# v1.19.2: reasoning-safe aux budgets + roll-up retitle.
# ---------------------------------------------------------------------------
print("\n== v1.19.2 aux budgets cover reasoning tokens ==")
check("classify budget raised off the measured-failing 96",
      tp._CLASSIFY_MAX_TOKENS >= 512)
check("classify timeout covers the measured 5.8s worst case",
      tp._CLASSIFY_TIMEOUT_S >= 8.0)
check("summary budget covers reasoning + 120 words + a title",
      tp._SUMMARY_MAX_TOKENS >= 1000)
check("curate budget raised", tp._CURATE_MAX_TOKENS >= 512)
check("decompose budget raised", tp._DECOMPOSE_MAX_TOKENS >= 512)

print("\n== v1.19.2 retitle ==")
_rt = {"id": 5, "title": "Now Find Everything Online", "slug": "now-find-everything-online"}
tp._retitle(_rt, "OSINT profile of Jane Doe")
check("roll-up title replaces the keyword-salad title",
      _rt["title"] == "OSINT profile of Jane Doe")
check("slug follows the new title", _rt["slug"] == "osint-profile-of-jane-doe")
_rt2 = {"id": 6, "title": "Keep This", "slug": "keep-this"}
tp._retitle(_rt2, "")
check("empty proposal leaves the title alone", _rt2["title"] == "Keep This")
tp._retitle(_rt2, "   ")
check("blank proposal leaves the title alone", _rt2["title"] == "Keep This")
tp._retitle(_rt2, " ".join(["word"] * 15))
check("absurdly long proposal rejected", _rt2["title"] == "Keep This")
_rt3 = {"id": 7, "title": "My Project", "slug": "my-project", "_user_titled": True}
tp._retitle(_rt3, "Something The Model Preferred")
check("a USER-chosen name is never overwritten", _rt3["title"] == "My Project")
_rt4 = {"id": 8, "title": "Same", "slug": "same"}
tp._retitle(_rt4, '"Same"')
check("quoted/identical proposal is a no-op", _rt4["title"] == "Same")
check("retitle never raises on a junk record",
      (tp._retitle({}, "x"), True)[1])

_saved_cfg3 = tp._cfg
_rcfg = dict(tp.DEFAULTS, enabled="on", wiki_dir=TMP_WIKI, summarize="on")
tp._cfg = lambda: dict(_rcfg)
_saved_ctx = tp._ctx


class _StubLlm:
    def __init__(self, parsed=None, boom=False):
        self.parsed = parsed
        self.boom = boom
        self.calls = 0

    def complete_structured(self, **kw):
        self.calls += 1
        if self.boom:
            raise RuntimeError("no structured support")
        return type("R", (), {"parsed": self.parsed})()

    def complete(self, **kw):
        self.calls += 1
        return type("R", (), {"text": "plain fallback summary"})()


try:
    _rec = {"id": 9, "title": "Tell Vllm Speculative Decoding",
            "slug": "tell-vllm-speculative-decoding", "summary": "",
            "recent": [("what about spec decoding", "it drafts tokens")]}
    tp._ctx = type("C", (), {"llm": _StubLlm(
        {"summary": "Discussed vLLM speculative decoding acceptance rates.",
         "title": "vLLM speculative decoding"})})()
    tp._rollup_summary(_rcfg, _rec)
    check("roll-up stores the summary",
          _rec["summary"].startswith("Discussed vLLM"))
    check("roll-up retitles from what the thread was about",
          _rec["title"] == "vLLM speculative decoding")

    _rec2 = {"id": 10, "title": "Original", "slug": "original", "summary": "old",
             "recent": [("q", "a")]}
    tp._ctx = type("C", (), {"llm": _StubLlm({"summary": "", "title": "New"})})()
    tp._rollup_summary(_rcfg, _rec2)
    check("empty summary leaves BOTH summary and title untouched",
          _rec2["summary"] == "old" and _rec2["title"] == "Original")

    _rec3 = {"id": 11, "title": "Original", "slug": "original", "summary": "",
             "recent": [("q", "a")]}
    _stub = _StubLlm(boom=True)
    tp._ctx = type("C", (), {"llm": _stub})()
    tp._rollup_summary(_rcfg, _rec3)
    check("structured failure falls back to the plain completion",
          _rec3["summary"] == "plain fallback summary")
    check("plain fallback proposes no title rather than guessing",
          _rec3["title"] == "Original")

    _rec4 = {"id": 12, "title": "T", "slug": "t", "summary": "keep", "recent": []}
    tp._ctx = type("C", (), {"llm": _StubLlm({"summary": "x", "title": "y"})})()
    tp._rollup_summary(_rcfg, _rec4)
    check("no recent exchanges -> no call, summary kept", _rec4["summary"] == "keep")

    _rec5 = {"id": 13, "title": "T", "slug": "t", "summary": "keep",
             "recent": [("q", "a")]}
    tp._cfg = lambda: dict(_rcfg, summarize="off")
    tp._rollup_summary(dict(_rcfg, summarize="off"), _rec5)
    check("summarize=off still disables the roll-up entirely",
          _rec5["summary"] == "keep" and _rec5["title"] == "T")

    tp._cfg = lambda: dict(_rcfg)
    tp._ctx = None
    _rec6 = {"id": 14, "title": "T", "slug": "t", "summary": "keep",
             "recent": [("q", "a")]}
    tp._rollup_summary(_rcfg, _rec6)
    check("no ctx.llm -> no crash, summary kept", _rec6["summary"] == "keep")
finally:
    tp._ctx = _saved_ctx
    tp._cfg = _saved_cfg3

print("\n== v1.19.2 /topic new marks a user-chosen name ==")
RS()
_ucfg = dict(tp.DEFAULTS, enabled="on", wiki_dir=TMP_WIKI)
_un = tp._apply_override(("new", "Quarterly board pack"), "UT", time.time(), _ucfg)
check("/topic new <title> marks the record user-titled",
      _un.get("_user_titled") is True)
_ua = tp._apply_override(("new", None), "UT2", time.time(), _ucfg)
check("/topic new with no title is NOT user-titled (auto name may improve)",
      not _ua.get("_user_titled"))
RS()

# ---------------------------------------------------------------------------
# v1.20.0: hermes integration — strong-tier badge, session pivot, topics_show.
# ---------------------------------------------------------------------------
print("\n== v1.20.0 badge on strong tier ==")
_saved_cfg4 = tp._cfg
_saved_weak4 = _tp_early._on_weak_host
try:
    RS()
    _bcfg = dict(tp.DEFAULTS, enabled="on", wiki_dir=TMP_WIKI, badge="on",
                 classify="heuristic", summarize="off")
    tp._cfg = lambda: dict(_bcfg)
    _tp_early._on_weak_host = lambda *a, **k: True
    tp._tp_pre_llm(session_id="BW", turn_id="b1", user_message="a question about vllm")
    check("weak tier badges as before", tp.badge_for("BW") == "t#00001")
    _tp_early._on_weak_host = lambda *a, **k: False
    tp._tp_pre_llm(session_id="BS", turn_id="b2", user_message="another question entirely")
    check("STRONG tier now badges too (badge_strong on)",
          tp.badge_for("BS").startswith("t#"))
    tp._cfg = lambda: dict(_bcfg, badge_strong="off")
    check("badge_strong:off restores the v1.15.0 suppression",
          tp.badge_for("BS") == "")
    tp._cfg = lambda: dict(_bcfg, badge="off")
    check("master badge flag still wins on strong", tp.badge_for("BS") == "")
    tp._cfg = lambda: dict(_bcfg)
    tp._active_voice["BS"] = True
    check("voice sessions are still never badged (TTS would read it aloud)",
          tp.badge_for("BS") == "")
    tp._active_voice.pop("BS", None)
    # the INJECTION must stay inert on strong — only the label came back
    _r = tp._tp_pre_llm(session_id="BS", turn_id="b3", user_message="and what about fp8")
    check("strong-tier context injection stays blanket-inert", _r is None)
finally:
    tp._cfg = _saved_cfg4
    _tp_early._on_weak_host = _saved_weak4
    RS()

print("\n== v1.20.0 session pivot ==")
_saved_cfg5 = tp._cfg
try:
    RS()
    _scfg = dict(tp.DEFAULTS, enabled="on", wiki_dir=TMP_WIKI,
                 classify="heuristic", summarize="off")
    tp._cfg = lambda: dict(_scfg)
    for _i, _m in enumerate(["tell me about vllm speculative decoding",
                             "and what is the acceptance rate"]):
        tp._tp_pre_llm(session_id="SESS-ABC", turn_id="s%d" % _i, user_message=_m)
        tp._tp_post_llm(session_id="SESS-ABC", turn_id="s%d" % _i,
                        user_message=_m, assistant_response="Answer %d" % _i)
    _rec = tp._topics[tp._active_topic["SESS-ABC"]]
    check("session recorded on the thread", _rec["sessions"] == ["SESS-ABC"])
    check("repeat turns don't duplicate the session", len(_rec["sessions"]) == 1)
    _txt = open(tp._topic_path(_scfg, _rec["id"]), encoding="utf-8").read()
    check("sessions persisted to frontmatter", "sessions: [SESS-ABC]" in _txt)
    _p = tp._parse_topic_md(_txt)
    check("sessions round-trip through the parser", _p["sessions"] == ["SESS-ABC"])
    _rows, _ = tp.list_topics(_scfg)
    check("topics_list hands back a session_id to pivot with",
          _rows[0]["session_id"] == "SESS-ABC")
    # the same thread spoken in further sessions: most recent last, capped.
    # Each needs its pre_llm turn too — post_llm only records against a topic
    # the session is already routed to.
    for _n in range(7):
        tp._tp_pre_llm(session_id="S%d" % _n, turn_id="x%d" % _n,
                       user_message="more about vllm speculative decoding")
        tp._tp_post_llm(session_id="S%d" % _n, turn_id="x%d" % _n,
                        user_message="more about vllm speculative decoding",
                        assistant_response="ok")
    check("session list capped", len(_rec["sessions"]) <= tp._SESSION_CAP)
    check("most recent session is last", _rec["sessions"][-1] == "S6")
    # byte-identity for a record that has no sessions (pre-v1.20 shape)
    _no_sess = {"id": 99, "slug": "s", "title": "T", "status": "open",
                "entities": [], "created": 0, "last_active": 0, "turns": 0,
                "summary": "", "facts": [], "findings": [], "recent": []}
    check("a sessionless record emits NO sessions line (byte-compatible)",
          "sessions:" not in tp._serialize_topic_md(_no_sess))
finally:
    tp._cfg = _saved_cfg5
    RS()

print("\n== v1.20.0 topics_show ==")
_saved_cfg6 = tp._cfg
try:
    RS()
    _hcfg = dict(tp.DEFAULTS, enabled="on", wiki_dir=TMP_WIKI,
                 classify="heuristic", summarize="off")
    tp._cfg = lambda: dict(_hcfg)
    tp._tp_pre_llm(session_id="SH", turn_id="h1", user_message="explain fp8 quantisation")
    tp._tp_post_llm(session_id="SH", turn_id="h1", user_message="explain fp8 quantisation",
                    assistant_response="It stores weights in 8-bit float.")
    _r = tp._topics[tp._active_topic["SH"]]
    _r["summary"] = "Discussed fp8 quantisation."
    _r["facts"] = ["User runs fp8 on vLLM."]
    _r["findings"] = ["fp8 is lossless enough [src: https://x/y]"]
    tp._persist_topic(_hcfg, _r)
    _tid = _r["id"]

    _s = tp.show_topic(_hcfg, "t#%05d" % _tid)
    check("show by t#NNNNN tag", _s and _s["id"] == _tid)
    check("show by bare number", tp.show_topic(_hcfg, str(_tid))["id"] == _tid)
    check("show by t00001 form", tp.show_topic(_hcfg, "t%05d" % _tid)["id"] == _tid)
    check("carries the summary", _s["summary"] == "Discussed fp8 quantisation.")
    check("carries facts", _s["facts"] == ["User runs fp8 on vLLM."])
    check("carries sourced findings", "[src:" in _s["findings"][0])
    check("carries the exchanges as user/assistant pairs",
          _s["exchanges"][0]["user"].startswith("explain fp8"))
    check("carries the session pivot", _s["session_id"] == "SH")
    # The manifest owns the frontmatter, the file owns the body. A live turn can
    # re-persist a hot record after a retitle backfill, leaving the FILE with a
    # stale title while the index carries the new one — show must agree with
    # topics_list, which reads the manifest.
    _stale = open(tp._topic_path(_hcfg, _tid), encoding="utf-8").read()
    with open(tp._topic_path(_hcfg, _tid), "w", encoding="utf-8") as _fh:
        _fh.write(_stale.replace("title: %s" % _r["title"], "title: Stale Old Name"))
    _r["title"] = "Fresh Indexed Name"
    check("title comes from the manifest, not a stale file",
          tp.show_topic(_hcfg, str(_tid))["title"] == "Fresh Indexed Name")
    check("body still comes from the file",
          tp.show_topic(_hcfg, str(_tid))["summary"] == "Discussed fp8 quantisation.")
    check("unknown id -> None", tp.show_topic(_hcfg, "t#09999") is None)
    check("junk id -> None", tp.show_topic(_hcfg, "not-an-id") is None)
    check("empty id -> None", tp.show_topic(_hcfg, "") is None)

    # reading a thread must not disturb routing
    _active_before = dict(tp._active_topic)
    _status_before = _r["status"]
    tp.show_topic(_hcfg, "t#%05d" % _tid)
    check("show never changes the active topic", tp._active_topic == _active_before)
    check("show never reopens a thread", _r["status"] == _status_before)

    _out = json.loads(tp.topics_show_handler({"topic_id": "t#%05d" % _tid}))
    check("handler returns the thread as JSON", _out["tag"] == "t#%05d" % _tid)
    check("handler has no note on a thread with a body", "note" not in _out)
    _miss = json.loads(tp.topics_show_handler({"topic_id": "t#09999"}))
    check("handler explains an unknown id", "error" in _miss)
    check("handler tolerates a non-dict arg",
          "error" in json.loads(tp.topics_show_handler("nope")))
    tp._cfg = lambda: dict(_hcfg, enabled="off")
    check("disabled -> handler explains, no crash",
          "error" in json.loads(tp.topics_show_handler({"topic_id": "1"})))
    tp._cfg = lambda: dict(_hcfg)

    # a thread the pre-v1.19.1 bug hollowed out must SAY so, not look empty
    _r2 = tp._open_topic("Wiped thread", ["wiped"], time.time())
    _r2["turns"] = 6
    tp._persist_topic(_hcfg, _r2)
    _out2 = json.loads(tp.topics_show_handler({"topic_id": str(_r2["id"])}))
    check("hollowed thread carries an explanatory note", "note" in _out2)
    check("note points at session_search for the real messages",
          "session_search" in _out2["note"])
finally:
    tp._cfg = _saved_cfg6
    RS()

print("\n== v1.20.0 both tools register ==")


class _Ctx2:
    def __init__(self, fail=None):
        self.fail = fail
        self.names = []

    def register_tool(self, **kw):
        if kw["name"] == self.fail:
            raise RuntimeError("rejected")
        self.names.append(kw["name"])


_c = _Ctx2()
check("both tools register", tp.register_tool(_c) is True
      and _c.names == ["topics_list", "topics_show"])
_c2 = _Ctx2(fail="topics_list")
check("one failing does not take the other down",
      tp.register_tool(_c2) is True and _c2.names == ["topics_show"])
_c3 = _Ctx2(fail="topics_show")
check("and vice versa",
      tp.register_tool(_c3) is True and _c3.names == ["topics_list"])
check("ctx without register_tool -> False, not fatal",
      tp.register_tool(_BareCtx()) is False)
check("topics_show schema requires an id",
      tp.TOPICS_SHOW_SCHEMA["parameters"]["required"] == ["topic_id"])
check("topics_list description advertises the pivot",
      "session_search" in tp.TOPICS_LIST_SCHEMA["description"])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
