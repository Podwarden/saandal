#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Podwarden Inc

"""Fixture-P replay harness (promoted from the design phase's
exp_forced_synthesis.py; see docs/selfheal_design.md §5).

Rebuilds the poisoned session 20260719_012254_e3b7f659 (cat-femur-fracture
churn turn, ~65-68k prompt tokens, 130 blank assistant rows) READ-ONLY from
~/.hermes/state.db and replays it against the live vLLM endpoint:

  A  control          tools present (the 10 flat schemas from the live
                      request dump) — the churn must reproduce
                      (finish=tool_calls, yet another paraphrased search).
  C  forced synthesis the request is built by the REAL healer code path
                      (selfheal._sh_middleware with the turn state driven
                      to FAILING) — must yield a real answer:
                      >= doomed_min_answer chars after think-strip, no
                      <tool_call>, finish=stop.

Usage:
  tests/replay_fixture.py A C          # one run each
  tests/replay_fixture.py C --runs 3   # pass criterion for §5: 3/3
  tests/replay_fixture.py --clone      # clone the poisoned rows under a new
                                       # scratch session_id in state.db
                                       # (fixture P for the live e2e agent);
                                       # prints the new session_id

Results are appended to tests/replay_results.json.
"""
import argparse
import importlib.util
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
SESSION = "20260719_012254_e3b7f659"
DB = str(Path.home() / ".hermes" / "state.db")
ENDPOINT = "https://vllm.example.com/v1/chat/completions"
MODEL = "local-27b-model"
DOOMED_MIN = 40


def load_selfheal():
    spec = importlib.util.spec_from_file_location(
        "router_plugin", REPO / "plugin" / "__init__.py",
        submodule_search_locations=[str(REPO / "plugin")])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["router_plugin"] = mod
    spec.loader.exec_module(mod)
    import router_plugin.selfheal as sh
    return mod, sh


def api_key():
    for line in open(Path.home() / ".hermes" / ".env"):
        line = line.strip()
        if line.startswith("VLLM_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("no VLLM_API_KEY in ~/.hermes/.env")


def load_session_messages():
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    sysprompt = db.execute("select system_prompt from sessions where id=?",
                           (SESSION,)).fetchone()[0]
    rows = list(db.execute(
        "select role, content, tool_calls, tool_call_id from messages "
        "where session_id=? and active=1 order by id", (SESSION,)))
    db.close()
    msgs = []
    for r in rows:
        m = {"role": r["role"], "content": r["content"] or ""}
        if r["role"] == "assistant" and r["tool_calls"]:
            try:
                m["tool_calls"] = json.loads(r["tool_calls"])
            except Exception:
                pass
        if r["role"] == "tool":
            m["tool_call_id"] = r["tool_call_id"]
        msgs.append(m)
    # End the history mid-turn at the last tool result (guards just fired).
    while msgs and msgs[-1]["role"] == "assistant" \
            and not msgs[-1].get("tool_calls") \
            and msgs[-1]["content"].strip() in ("", "(empty)"):
        msgs.pop()
    # Drop orphan tool results (defensive pairing).
    out, pending = [], set()
    for m in msgs:
        if m["role"] == "assistant" and m.get("tool_calls"):
            pending = {tc.get("id") for tc in m["tool_calls"]
                       if isinstance(tc, dict)}
            out.append(m)
        elif m["role"] == "tool":
            if m.get("tool_call_id") in pending:
                out.append(m)
        else:
            pending = set()
            out.append(m)
    return sysprompt, out


def call(body, key, label):
    req = urllib.request.Request(
        ENDPOINT, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            out = json.load(resp)
    except Exception as e:
        print(f"[{label}] FAILED: {e}")
        return {"label": label, "error": str(e)}
    dt = time.time() - t0
    ch = out["choices"][0]
    msg = ch["message"]
    content = msg.get("content") or ""
    vis = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
    tcs = msg.get("tool_calls") or []
    res = {"label": label, "secs": round(dt, 1),
           "finish": ch.get("finish_reason"),
           "prompt_tokens": out.get("usage", {}).get("prompt_tokens"),
           "tool_calls": len(tcs), "content_len": len(vis),
           "inline_tc": "<tool_call>" in content,
           "visible_head": vis[:300]}
    print(f"=== [{label}] {dt:.1f}s finish={res['finish']} "
          f"prompt_toks={res['prompt_tokens']} tool_calls={len(tcs)} "
          f"content_len={len(vis)} inline_tc={res['inline_tc']}")
    if tcs:
        fn = tcs[0].get("function", {})
        print(f"    first tool_call: {fn.get('name')} "
              f"{str(fn.get('arguments'))[:120]}")
    print("    visible:", (vis[:400] + ("..." if len(vis) > 400 else ""))
          or "<EMPTY>")
    return res


def build_variant_a(sysprompt, msgs):
    tools = json.load(open(HERE / "data" / "tools_flat.json"))
    return {"model": MODEL,
            "messages": [{"role": "system", "content": sysprompt}] + msgs,
            "tools": tools, "max_tokens": 3000, "frequency_penalty": 0.3,
            "temperature": 0.6}


def build_variant_c(sysprompt, msgs, sh):
    """Variant C through the REAL healer code path: _sh_middleware with the
    turn driven to FAILING by the S1>cap trigger (the poisoned session's
    real counters: 69 searches, ~48 blocks)."""
    request = build_variant_a(sysprompt, msgs)
    request["messages"] = [{"role": "system", "content": sysprompt}] + \
        [dict(m) for m in msgs]
    sh._reset_state()
    cfg = dict(sh.DEFAULTS)
    cfg["search_hard_cap"] = 15
    sh._cfg = lambda: dict(cfg)
    sh._host["counters"] = lambda s, t: {
        "searches": 69, "blocks": 48,
        "queries": ["перелом шейки бедра у котов лечение"] * 8}
    sh._host["hosts"] = lambda: ["vllm.example.com"]
    out = sh._sh_middleware(request=request, api_mode="chat_completions",
                            base_url="https://vllm.example.com/v1",
                            session_id="fixtureP", turn_id="turnP",
                            api_call_count=60)
    assert isinstance(out, dict) and out.get("name") == \
        "selfheal_forced_synthesis", "healer did not force synthesis"
    assert "tools" not in request and "response_format" not in request \
        and "tool_choice" not in request, "healer did not strip"
    assert request["messages"][-1]["role"] == "user" and \
        request["messages"][-1]["content"].startswith(sh.CORRECTIVE_PREFIX), \
        "corrective message missing"
    rec = sh._turns["fixtureP"]
    assert rec["state"] == "FAILING", rec["state"]
    # tripwire must be armed too
    blk = sh._sh_pre_tool(tool_name="web_search", args={"query": "x"},
                          turn_id="turnP", session_id="fixtureP")
    assert isinstance(blk, dict) and blk.get("action") == "block", \
        "tripwire not armed"
    # Mimic the OpenAI client: extra_body keys are merged into the JSON
    # body top-level (this raw-HTTP harness has no client to do it).
    eb = request.pop("extra_body", None)
    if isinstance(eb, dict):
        assert eb.get("chat_template_kwargs", {}).get("enable_thinking") \
            is False, "no_think not applied"
        request.update(eb)
    else:
        raise AssertionError("healer did not set extra_body.chat_template_kwargs")
    return request


def clone_fixture():
    """Fixture P: clone the poisoned session's rows under a scratch
    session_id (additive INSERTs only; WAL + busy timeout). The live-e2e
    agent binds a test chat to the printed id via session_store."""
    new_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    db = sqlite3.connect(DB, timeout=30)
    db.execute("pragma busy_timeout=30000")
    try:
        row = db.execute(
            "select * from sessions where id=?", (SESSION,)).fetchone()
        cols = [d[0] for d in db.execute(
            "select * from sessions limit 0").description]
        vals = dict(zip(cols, row))
        vals["id"] = new_id
        vals["title"] = "selfheal fixture P (clone of %s)" % SESSION
        vals["session_key"] = None      # unbound until the e2e agent binds it
        vals["ended_at"] = None
        vals["end_reason"] = None
        db.execute("insert into sessions (%s) values (%s)"
                   % (",".join(vals), ",".join("?" * len(vals))),
                   list(vals.values()))
        mcols = [d[0] for d in db.execute(
            "select * from messages limit 0").description if d[0] != "id"]
        rows = db.execute(
            "select %s from messages where session_id=? and active=1 "
            "order by id" % ",".join(mcols), (SESSION,)).fetchall()
        for r in rows:
            v = dict(zip(mcols, r))
            v["session_id"] = new_id
            db.execute("insert into messages (%s) values (%s)"
                       % (",".join(v), ",".join("?" * len(v))),
                       list(v.values()))
        db.commit()
        n = db.execute("select count(*) from messages where session_id=?",
                       (new_id,)).fetchone()[0]
        print(f"fixture P cloned: session_id={new_id} ({n} rows)")
        return new_id
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("variants", nargs="*", default=[],
                    help="A and/or C")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--clone", action="store_true")
    args = ap.parse_args()
    if args.clone:
        clone_fixture()
        return 0
    variants = [v.upper() for v in args.variants] or ["A", "C"]
    _, sh = load_selfheal()
    key = api_key()
    sysprompt, msgs = load_session_messages()
    print(f"fixture: {len(msgs)} messages from {SESSION}")
    results, failures = [], 0
    for run in range(1, args.runs + 1):
        for v in variants:
            label = f"{v} run{run}"
            if v == "A":
                res = call(build_variant_a(sysprompt, msgs), key, label)
                # pass = the pathology reproduces: either the search churn
                # (finish=tool_calls) or the sibling degenerate outcome (the
                # answer disappears into the reasoning channel -> empty/
                # too-short content) — both are exactly what the healer
                # exists to catch.
                churn = (res.get("finish") == "tool_calls"
                         and res.get("tool_calls", 0) >= 1)
                degenerate = (not res.get("error")
                              and res.get("content_len", 0) < DOOMED_MIN)
                good = churn or degenerate
                verdict = ("churn reproduced" if churn else
                           "degenerate (empty-answer) reproduced" if degenerate
                           else "PATHOLOGY NOT REPRODUCED")
            else:
                res = call(build_variant_c(sysprompt, msgs, sh), key, label)
                good = (not res.get("error")
                        and res.get("tool_calls") == 0
                        and not res.get("inline_tc")
                        and res.get("content_len", 0) >= DOOMED_MIN)
                verdict = ("forced synthesis cured" if good
                           else "FORCED SYNTHESIS FAILED")
            res["pass"] = bool(good)
            res["verdict"] = verdict
            print(f"    -> {verdict}\n")
            results.append(res)
            if not good:
                failures += 1
    out_path = HERE / "replay_results.json"
    prior = []
    if out_path.exists():
        try:
            prior = json.loads(out_path.read_text())
        except Exception:
            prior = []
    out_path.write_text(json.dumps(prior + results, ensure_ascii=False,
                                   indent=1))
    print(f"{len(results) - failures}/{len(results)} passed; results "
          f"appended to {out_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
