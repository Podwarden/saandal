#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Podwarden Inc

"""Unit tests for bin/distill_topics.py — out-of-band topic distillation.

Covers the selection predicate (stale / min-turns / idempotence), topic parsing,
model-result normalisation, note emission, state round-trip, --limit accounting,
and vault isolation. No network: the LLM call is stubbed throughout.
Run: tests/test_distill_unit.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("distill", REPO / "bin" / "distill_topics.py")
dt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dt)

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   %s" % label)
    else:
        FAIL += 1
        print("  FAIL %s" % label)


class Args(object):
    """Stand-in for the argparse namespace process_vault consumes."""
    def __init__(self, **kw):
        self.stale_hours = 12.0
        self.min_turns = 2
        self.limit = 25
        self.model = "test-model"
        self.base_url = "http://invalid.invalid/v1"
        self.api_key = "k"
        self.timeout = 5.0
        self.max_tokens = 2000
        self.merge = False
        self.merge_tool = "/nonexistent/merge.py"
        self.no_commit = True
        self.dry_run = False
        self.retitle = False
        self.force = False
        self.__dict__.update(kw)


def make_vault(tmp, topics):
    """topics: list of (id, turns, last_active, slug, body_extra)."""
    vault = os.path.join(tmp, "vault")
    tdir = os.path.join(vault, "topics")
    os.makedirs(tdir)
    index = {"next_id": len(topics) + 1, "topics": []}
    for tid, turns, last_active, slug, extra in topics:
        index["topics"].append({"id": tid, "slug": slug, "title": slug.replace("-", " "),
                                "entities": ["alpha", "beta"], "status": "open",
                                "turns": turns, "last_active": last_active})
        body = ["---", "id: t%05d" % tid, "slug: %s" % slug, "turns: %d" % turns,
                "---", "## summary", "", "## facts", "", "## findings",
                "- Some finding [src: https://example.com/a]", "", "## log",
                "- u: question one / a: answer one", "- u: question two / a: answer two"]
        if extra:
            body.append(extra)
        with open(os.path.join(tdir, "t%05d.md" % tid), "w") as fh:
            fh.write("\n".join(body) + "\n")
    with open(os.path.join(tdir, "_index.json"), "w") as fh:
        json.dump(index, fh)
    return vault, tdir


NOW = 1_800_000_000.0
HOUR = 3600.0

print("\n== selection predicate ==")
with tempfile.TemporaryDirectory() as tmp:
    vault, tdir = make_vault(tmp, [
        (1, 4, NOW - 30 * HOUR, "stale-and-deep", None),      # candidate
        (2, 4, NOW - 1 * HOUR, "still-warm", None),           # too recent
        (3, 1, NOW - 30 * HOUR, "one-shot", None),            # too few turns
        (4, 2, NOW - 100 * HOUR, "oldest", None),             # candidate, oldest
    ])
    cands = dt.select(tdir, {}, NOW, 12.0, 2)
    keys = [c["key"] for c in cands]
    check("stale + substantial threads selected", keys == ["t00004", "t00001"])
    check("warm thread excluded", "t00002" not in keys)
    check("one-turn thread excluded", "t00003" not in keys)
    check("oldest-quiet-first ordering", keys[0] == "t00004")

    state = {"t00001": {"last_active": NOW - 30 * HOUR, "distilled_at": NOW}}
    keys2 = [c["key"] for c in dt.select(tdir, state, NOW, 12.0, 2)]
    check("already distilled at this revision -> skipped", "t00001" not in keys2)
    check("undistilled sibling still selected", "t00004" in keys2)

    state_old = {"t00001": {"last_active": NOW - 90 * HOUR, "distilled_at": NOW - 80 * HOUR}}
    cands3 = dt.select(tdir, state_old, NOW, 12.0, 2)
    c1 = [c for c in cands3 if c["key"] == "t00001"]
    check("thread that moved since last distill -> re-selected", len(c1) == 1)
    check("re-selection flagged as redistill", c1 and c1[0]["redistill"] is True)

    check("min-turns 1 widens the net", len(dt.select(tdir, {}, NOW, 12.0, 1)) == 3)
    check("stale-hours 0 admits the warm thread",
          len(dt.select(tdir, {}, NOW, 0.0, 2)) == 3)
    check("missing store -> empty, no raise",
          dt.select(os.path.join(tmp, "nope"), {}, NOW, 12.0, 2) == [])

print("\n== topic parsing ==")
with tempfile.TemporaryDirectory() as tmp:
    vault, tdir = make_vault(tmp, [(7, 3, NOW - 20 * HOUR, "parse-me", None)])
    meta, sections = dt.parse_topic_md(os.path.join(tdir, "t00007.md"))
    check("frontmatter parsed", meta.get("slug") == "parse-me")
    check("findings section captured", len(sections.get("findings") or []) == 1)
    check("log section captured", len(sections.get("log") or []) == 2)
    check("empty section present but empty", sections.get("facts") == [])
    check("unreadable file -> empty, no raise",
          dt.parse_topic_md(os.path.join(tdir, "missing.md")) == ({}, {}))
    cand = dt.select(tdir, {}, NOW, 12.0, 2)[0]
    rendered = dt.render_thread(cand)
    check("render carries title", "parse-me" in rendered or "parse me" in rendered)
    check("render carries keywords", "alpha" in rendered)
    check("render carries conversation", "question two" in rendered)

print("\n== result normalisation ==")
check("store=false -> nothing stored", dt.normalise({"store": False, "facts": ["x"]}) is None)
check("non-dict -> None", dt.normalise("nope") is None)
check("empty facts and summary -> None",
      dt.normalise({"store": True, "facts": [], "summary": "  "}) is None)
_n = dt.normalise({"store": True, "section": "user", "page": "Jane Profile",
                   "summary": "s", "facts": ["a", "b"]})
check("valid section preserved", _n["section"] == "user")
check("page slugified", _n["page"] == "jane-profile")
_bad = dt.normalise({"store": True, "section": "not-a-section", "page": "p",
                     "summary": "s", "facts": ["a"]})
check("unknown section -> _team (matches merge tool fallback)", _bad["section"] == "_team")
_cap = dt.normalise({"store": True, "section": "jobs", "page": "p", "summary": "s",
                     "facts": ["f%d" % i for i in range(20)]})
check("facts capped at 8", len(_cap["facts"]) == 8)
_blank = dt.normalise({"store": True, "section": "jobs", "page": "", "summary": "s",
                       "facts": []})
check("blank page tolerated (falls back to slug at write time)", _blank["page"] is None)
check("summary-only note is storable", _blank is not None)

print("\n== note emission ==")
with tempfile.TemporaryDirectory() as tmp:
    vault, tdir = make_vault(tmp, [(12, 3, NOW - 20 * HOUR, "duplex-calling", None)])
    cand = dt.select(tdir, {}, NOW, 12.0, 2)[0]
    spec_ = {"section": "infra", "page": "hermes-voice", "summary": "Duplex works on CLI.",
             "facts": ["Hermes supports full duplex on CLI and Discord."]}
    path = dt.write_note(vault, cand, spec_, ["- A finding [src: https://x/y]"])
    body = open(path).read()
    check("note lands in inbox/hermes", path.startswith(os.path.join(vault, "inbox", "hermes")))
    check("target names the model-chosen section/page",
          "target: infra/hermes-voice" in body)
    check("target is NOT the hardcoded jobs/ of the in-turn path",
          "target: jobs/" not in body)
    check("note carries the topic id", "t#00012" in body)
    check("facts rendered", "full duplex on CLI" in body)
    check("findings rendered with source", "src: https://x/y" in body)
    check("filename is idempotent per topic",
          os.path.basename(path) == "topic-t00012-duplex-calling.md")
    p2 = dt.write_note(vault, cand, spec_, [])
    check("re-emission overwrites, no duplicate", p2 == path and
          len(os.listdir(os.path.join(vault, "inbox", "hermes"))) == 1)
    spec_np = {"section": "jobs", "page": None, "summary": "s", "facts": ["f"]}
    body2 = open(dt.write_note(vault, cand, spec_np, [])).read()
    check("page falls back to the topic slug", "target: jobs/duplex-calling" in body2)

print("\n== state round-trip ==")
with tempfile.TemporaryDirectory() as tmp:
    vault, tdir = make_vault(tmp, [(1, 3, NOW - 20 * HOUR, "s", None)])
    check("absent state -> {}", dt.load_state(tdir) == {})
    dt.save_state(tdir, {"t00001": {"last_active": 1.0, "distilled_at": 2.0}})
    check("state round-trips", dt.load_state(tdir)["t00001"]["distilled_at"] == 2.0)
    check("state file is dot-prefixed (invisible to the plugin's ^t\\d{5}\\.md$ glob)",
          os.path.exists(os.path.join(tdir, ".distilled.json")))
    check("no stray tmp files left behind",
          [n for n in os.listdir(tdir) if n.endswith(".tmp")] == [])
    with open(os.path.join(tdir, ".distilled.json"), "w") as fh:
        fh.write("{ not json")
    check("corrupt state -> {} not a crash", dt.load_state(tdir) == {})

print("\n== process_vault ==")
with tempfile.TemporaryDirectory() as tmp:
    vault, tdir = make_vault(tmp, [
        (1, 3, NOW - 20 * HOUR, "alpha", None),
        (2, 3, NOW - 21 * HOUR, "beta", None),
        (3, 3, NOW - 22 * HOUR, "gamma", None),
    ])
    calls = []

    def fake_llm(text, model, base_url, api_key, timeout=60.0, max_tokens=2000):
        calls.append(text)
        return {"store": True, "section": "jobs", "page": "p%d" % len(calls),
                "summary": "sum", "facts": ["fact"]}

    orig = dt.call_llm
    dt.call_llm = fake_llm
    try:
        r = dt.process_vault(vault, Args(limit=2), NOW)
        check("limit caps the run", r["distilled"] == 2)
        check("deferred count reported, not silently dropped", r["deferred"] == 1)
        check("only capped number of LLM calls made", len(calls) == 2)
        check("state persisted for distilled topics",
              len(dt.load_state(tdir)) == 2)
        r2 = dt.process_vault(vault, Args(limit=25), NOW)
        check("second run picks up only the deferred topic", r2["distilled"] == 1)
        r3 = dt.process_vault(vault, Args(limit=25), NOW)
        check("third run is a no-op (idempotent)", r3["candidates"] == 0)

        dt.call_llm = lambda *a, **k: {"store": False}
        vault2, tdir2 = make_vault(os.path.join(tmp, "b"), [(9, 3, NOW - 20 * HOUR, "x", None)])
        r4 = dt.process_vault(vault2, Args(), NOW)
        check("store=false counted as skipped_empty, not failed",
              r4["skipped_empty"] == 1 and r4["failed"] == 0)
        check("store=false writes no inbox note",
              not os.path.isdir(os.path.join(vault2, "inbox", "hermes")))
        check("store=false still recorded in state (no re-ask next run)",
              dt.load_state(tdir2)["t00009"]["stored"] is False)

        dt.call_llm = lambda *a, **k: None
        vault3, tdir3 = make_vault(os.path.join(tmp, "c"), [(9, 3, NOW - 20 * HOUR, "x", None)])
        r5 = dt.process_vault(vault3, Args(), NOW)
        check("LLM failure counted", r5["failed"] == 1)
        check("failed topic NOT marked -> retried next run", dt.load_state(tdir3) == {})

        dt.call_llm = fake_llm
        vault4, tdir4 = make_vault(os.path.join(tmp, "d"), [(1, 3, NOW - 20 * HOUR, "x", None)])
        before = len(calls)
        r6 = dt.process_vault(vault4, Args(dry_run=True), NOW)
        check("dry-run makes no LLM call", len(calls) == before)
        check("dry-run writes no state", dt.load_state(tdir4) == {})
        check("dry-run still reports the candidate", r6["candidates"] == 1)
    finally:
        dt.call_llm = orig

print("\n== vault isolation ==")
with tempfile.TemporaryDirectory() as tmp:
    va, _ = make_vault(os.path.join(tmp, "a"), [(1, 3, NOW - 20 * HOUR, "secret-payroll", None)])
    vb, _ = make_vault(os.path.join(tmp, "b"), [(1, 3, NOW - 20 * HOUR, "other-co", None)])
    orig = dt.call_llm
    dt.call_llm = lambda *a, **k: {"store": True, "section": "jobs", "page": "p",
                                   "summary": "s", "facts": ["f"]}
    try:
        dt.process_vault(va, Args(), NOW)
        notes_b = os.path.join(vb, "inbox", "hermes")
        check("distilling vault A writes nothing into vault B",
              not os.path.isdir(notes_b))
        check("vault A note stayed in vault A",
              len(os.listdir(os.path.join(va, "inbox", "hermes"))) == 1)
    finally:
        dt.call_llm = orig

print("\n== merge guard ==")
with tempfile.TemporaryDirectory() as tmp:
    vault, _ = make_vault(tmp, [(1, 3, NOW - 20 * HOUR, "x", None)])
    ok, msg = dt.run_merge(vault, "/nonexistent/merge_inboxes.py")
    check("missing merge tool -> reported, not raised", ok is False and "not found" in msg)

import shutil
import subprocess
# Integration check against a real llm-wiki merge tool. Opt-in: point
# SAANDAL_MERGE_TOOL at your vault's tools/merge_inboxes.py. Its section list
# must include "jobs" (the hermes vault schema), so a vault with a different
# schema would fail this for reasons unrelated to saandal.
MERGE_TOOL = os.environ.get("SAANDAL_MERGE_TOOL", "")
if MERGE_TOOL and shutil.which("git") and os.path.isfile(MERGE_TOOL):
    with tempfile.TemporaryDirectory() as tmp:
        vault, tdir = make_vault(tmp, [(1, 3, NOW - 20 * HOUR, "x", None)])
        os.makedirs(os.path.join(vault, "wiki"))
        for f, body in (("index.md", "# x\n"), ("log.md", "# log\n")):
            with open(os.path.join(vault, f), "w") as fh:
                fh.write(body)
        os.makedirs(os.path.join(vault, "inbox", "hermes"))
        with open(os.path.join(vault, "inbox", "hermes", "n.md"), "w") as fh:
            fh.write("---\ntarget: jobs/page\nname: n\n---\n\nA durable fact.\n")
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        os.environ.update(env)
        subprocess.run(["git", "-C", vault, "init", "-q"], capture_output=True)
        subprocess.run(["git", "-C", vault, "commit", "-q", "--allow-empty",
                        "-m", "init"], capture_output=True)
        ok, msg = dt.run_merge(vault, MERGE_TOOL)
        check("merge succeeds and commits", ok and "committed" in msg)
        files = subprocess.run(["git", "-C", vault, "show", "--name-only",
                                "--format=", "HEAD"],
                               capture_output=True, text=True).stdout.split()
        check("merge output committed", "wiki/jobs/page.md" in files)
        check("plugin-owned topics/ churn NOT swept into the automated commit",
              not any(f.startswith("topics/") for f in files))
        check("inbox note consumed by the merge",
              not os.path.exists(os.path.join(vault, "inbox", "hermes", "n.md")))
        # merge_inboxes.py appends an audit line to log.md on every invocation,
        # so a bare re-run is never a true no-op. The guarantee that keeps the
        # nightly job from committing noise lives one level up: process_vault
        # only merges when this run actually distilled something.
        orig_merge = dt.run_merge
        merge_calls = []
        dt.run_merge = lambda *a, **k: (merge_calls.append(a) or (True, "stub"))
        dt.call_llm = lambda *a, **k: {"store": False}
        try:
            r_empty = dt.process_vault(vault, Args(merge=True, merge_tool=MERGE_TOOL), NOW)
            check("nothing distilled -> merge not invoked at all",
                  r_empty["distilled"] == 0 and merge_calls == [])
        finally:
            dt.run_merge = orig_merge
else:
    print("  skip git merge tests (git or merge tool unavailable)")

print("\n== retitle: auto-title detection ==")
check("exact _title_from signature detected",
      dt.is_auto_title({"title": "Now Find Everything Online",
                        "entities": ["now", "find", "everything", "online", "osint"]}))
check("shorter auto-title (all words are keywords) detected",
      dt.is_auto_title({"title": "Vet", "entities": ["vet", "port", "coquitlam"]}))
check("raw-text fallback with no keywords detected",
      dt.is_auto_title({"title": "Are you there?", "entities": []}))
check("empty title detected", dt.is_auto_title({"title": "", "entities": ["a"]}))
check("a human-written name is NOT auto",
      not dt.is_auto_title({"title": "Quarterly board pack",
                            "entities": ["board", "pack", "quarterly", "fees"]}))
check("punctuation-tolerant word match",
      dt.is_auto_title({"title": "Charging.", "entities": ["charging"]}))
check("slugify", dt._slugify("Jane Doe OSINT Research")
      == "jane-doe-osint-research")
check("slugify strips punctuation",
      dt._slugify("Pushkin's Prisoner Poem") == "pushkin-s-prisoner-poem")

print("\n== retitle: file rewrite keeps the body ==")
with tempfile.TemporaryDirectory() as tmp:
    vault, tdir = make_vault(tmp, [(3, 4, NOW - 20 * HOUR, "now-find-everything", None)])
    p = os.path.join(tdir, "t00003.md")
    before = open(p).read()
    body_before = before.split("---", 2)[2]
    check("apply_title reports success", dt.apply_title(tdir, 3, "Jane Doe OSINT Research"))
    after = open(p).read()
    check("title line rewritten", "title: Jane Doe OSINT Research" in after)
    check("slug line rewritten", "slug: jane-doe-osint-research" in after)
    check("BODY byte-identical after retitle", after.split("---", 2)[2] == body_before)
    check("log lines survive", after.count("- u:") == before.count("- u:"))
    check("findings survive", "[src: https://example.com/a]" in after)
    check("missing file -> False, no raise", dt.apply_title(tdir, 999, "x") is False)

print("\n== retitle: index patch is concurrency-safe ==")
with tempfile.TemporaryDirectory() as tmp:
    vault, tdir = make_vault(tmp, [(1, 3, NOW - 20 * HOUR, "a", None),
                                   (2, 3, NOW - 20 * HOUR, "b", None)])
    # a live gateway adds a topic AFTER we loaded the manifest
    idx = json.load(open(os.path.join(tdir, "_index.json")))
    idx["topics"].append({"id": 9, "slug": "late", "title": "Late Arrival",
                          "entities": [], "status": "open", "turns": 1,
                          "last_active": NOW})
    json.dump(idx, open(os.path.join(tdir, "_index.json"), "w"))
    check("patch_index succeeds", dt.patch_index(tdir, {1: ("New One", "new-one")}))
    idx2 = json.load(open(os.path.join(tdir, "_index.json")))
    ids = {r["id"] for r in idx2["topics"]}
    check("concurrently-added topic NOT dropped by a stale snapshot", 9 in ids)
    check("targeted row patched",
          [r for r in idx2["topics"] if r["id"] == 1][0]["title"] == "New One")
    check("untargeted row untouched",
          [r for r in idx2["topics"] if r["id"] == 2][0]["title"] == "b")
    check("no stray tmp files",
          [n for n in os.listdir(tdir) if n.endswith(".tmp")] == [])

print("\n== retitle: vault pass ==")
with tempfile.TemporaryDirectory() as tmp:
    vault, tdir = make_vault(tmp, [(1, 3, NOW - 20 * HOUR, "alpha-beta", None),
                                   (2, 3, NOW - 20 * HOUR, "gamma-delta", None)])
    idx = json.load(open(os.path.join(tdir, "_index.json")))
    for r in idx["topics"]:
        r["title"] = " ".join(r["entities"][:4]).title()   # auto-title shape
    idx["topics"][1]["title"] = "A Name The Human Chose"
    idx["topics"][1]["entities"] = ["unrelated", "keywords"]
    json.dump(idx, open(os.path.join(tdir, "_index.json"), "w"))
    calls = []
    orig_pt = dt.propose_title
    dt.propose_title = lambda rec, sec, *a, **k: (calls.append(rec["id"]) or "Better Name %d" % rec["id"])
    try:
        r = dt.retitle_vault(vault, Args(retitle=True, force=False, limit=0), now=NOW)
        check("only auto-titled threads are candidates", r["candidates"] == 1)
        check("human-named thread left alone", r["skipped_named"] == 1)
        check("no warm threads in this fixture", r["skipped_warm"] == 0)
        check("retitled count", r["retitled"] == 1)
        check("only the candidate hit the model", calls == [1])
        r2 = dt.retitle_vault(vault, Args(retitle=True, force=False, limit=0), now=NOW)
        check("second pass is idempotent (state remembers)", r2["retitled"] == 0)
        r3 = dt.retitle_vault(vault, Args(retitle=True, force=True, limit=0), now=NOW)
        check("--force redoes everything incl. the named one", r3["retitled"] == 2)
        dt.propose_title = lambda *a, **k: None
        r4 = dt.retitle_vault(vault, Args(retitle=True, force=True, limit=0), now=NOW)
        check("model failure counted, nothing written", r4["failed"] == 2
              and r4["retitled"] == 0)
    finally:
        dt.propose_title = orig_pt

# a thread still in active use must not be renamed mid-conversation
with tempfile.TemporaryDirectory() as tmp:
    vault, tdir = make_vault(tmp, [(1, 2, time.time() - 60, "warm-thread", None)])
    idx = json.load(open(os.path.join(tdir, "_index.json")))
    idx["topics"][0]["title"] = " ".join(idx["topics"][0]["entities"][:4]).title()
    json.dump(idx, open(os.path.join(tdir, "_index.json"), "w"))
    calls = []
    orig_pt = dt.propose_title
    dt.propose_title = lambda rec, sec, *a, **k: (calls.append(rec["id"]) or "New")
    try:
        r = dt.retitle_vault(vault, Args(retitle=True, force=False, limit=0))
        check("a thread active minutes ago is NOT retitled", r["retitled"] == 0)
        check("it is reported as warm, not silently dropped", r["skipped_warm"] == 1)
        check("no model call spent on a warm thread", calls == [])
        r2 = dt.retitle_vault(vault, Args(retitle=True, force=True, limit=0))
        check("--force overrides the staleness hold", r2["retitled"] == 1)
    finally:
        dt.propose_title = orig_pt

print("\n== discovery ==")
try:
    import yaml  # noqa: F401
    with tempfile.TemporaryDirectory() as tmp:
        home = os.path.join(tmp, "hermes")
        os.makedirs(os.path.join(home, "profiles", "p1"))
        os.makedirs(os.path.join(home, "profiles", "p2"))

        def wcfg(path, enabled, wiki):
            body = {"tools": {"tool_search": {"topics": {"enabled": enabled}}}}
            if wiki:
                body["tools"]["tool_search"]["topics"]["wiki_dir"] = wiki
            with open(path, "w") as fh:
                yaml.safe_dump(body, fh)

        wcfg(os.path.join(home, "config.yaml"), "on", None)
        wcfg(os.path.join(home, "profiles", "p1", "config.yaml"), "on", "/vaults/p1")
        wcfg(os.path.join(home, "profiles", "p2", "config.yaml"), "off", "/vaults/p2")
        found = dt.discover_vaults(home)
        check("enabled root discovered at default wiki", dt.DEFAULT_WIKI in found)
        check("enabled profile discovered at its own vault", "/vaults/p1" in found)
        check("disabled profile NOT discovered", "/vaults/p2" not in found)
        with open(os.path.join(home, "profiles", "p2", "config.yaml"), "w") as fh:
            fh.write("{{{ not yaml")
        check("unparseable config skipped, others still found",
              "/vaults/p1" in dt.discover_vaults(home))
except ImportError:
    print("  skip discovery tests (PyYAML unavailable)")

print("\n%d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
