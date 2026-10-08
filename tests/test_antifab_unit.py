#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Podwarden Inc

"""Unit tests for plugin/antifab.py (feature B — post-draft claim-grounding
annotate) and its wiring into selfheal (_sh_middleware corpus cache +
_sh_transform_out compose step). Covers: claim extraction, normalized grounding
match (the comma/rounding/scale/percent/× equivalences that must NOT flag a
grounded figure), the append-only invariant, idempotency, skip-when-no-sources,
fail-safe passthrough, and the tier gate firing on BOTH weak and strong.

The golden fixture (tests/data/antifab_research_web_5.json) is the REAL batch9
clean-home research_web_5 fabrication run pulled from state.db: draft = the
shipped answer, corpus = the turn's 17 retrieved tool results. Its two roll-up
aggregates (~6,200 commits, ~2,800 merged PRs) are the model's own sums and are
absent from the corpus; every other specific (18 models — the corpus literally
says "Access 18 Nous Research models", 8%/11%, 2,245/1,065/450/1,720/998/487,
$1.5B, the model sizes and versions) IS grounded and must be left untouched.

Run: tests/test_antifab_unit.py
"""
import importlib.util
import json
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
import router_plugin.antifab as af      # noqa: E402
import router_plugin.selfheal as sh     # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"ok   {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


FIX = json.loads((REPO / "tests" / "data"
                  / "antifab_research_web_5.json").read_text())
DRAFT = FIX["draft"]
CORPUS = "\n\n".join(m["content"] for m in FIX["messages"]
                     if m.get("role") == "tool")

# ---------------------------------------------------------------------------
# 1. Normalization equivalences — a grounded figure in a different surface form
#    must NORMALIZE-equal its corpus form (so it is never falsely flagged).
# ---------------------------------------------------------------------------
n = af.normalize
check("norm: thousands comma stripped", n("2,245") == n("2245"))
check("norm: 8% == 8 percent", n("8%") == n("8 percent") == n("8 %"))
check("norm: × == x", n("17×") == n("17x"))
check("norm: 512K == 512k", n("512K") == n("512k"))
check("norm: ≈ == ~", n("≈6,200") == n("~6200"))
check("norm: billion word == b", "1.5b" in n("$1.5 billion"))

# grounding matcher directly
check("grounded: 8% in a '8 percent' corpus",
      af.is_grounded("8%", n("aggregator beats by 8 percent")))
check("grounded: 2,245 in a '2245' corpus",
      af.is_grounded("2,245 commits", n("the repo had 2245 commits")))
check("grounded: 512K in a '512k' corpus",
      af.is_grounded("512K", n("supports 512k context")))
check("NOT grounded: 6,200 absent from corpus",
      not af.is_grounded("6,200 commits", n("2245 commits and 1720 commits")))

# ---------------------------------------------------------------------------
# 2. Extraction — high-risk specifics captured, bare small ints skipped.
# ---------------------------------------------------------------------------
specs = af.extract_specifics(DRAFT)
scores = " ".join(specs)
check("extract: captures the roll-up aggregates",
      any("6,200" in s for s in specs) and any("2,800" in s for s in specs))
check("extract: captures percentages", "8%" in specs and "11%" in specs)
check("extract: captures $ amount", any("1.5" in s for s in specs))
check("extract: skips bare small integers (no lone '3'/'4'/'6')",
      not any(s.strip() in ("3", "4", "6", "5") for s in specs))
small = af.extract_specifics("There are 3 options and 5 steps to review.")
check("extract: pure small-int prose yields nothing", small == [])

# scale suffix must not eat a following word ("2,800 merged" != 2800 million)
sm = af.extract_specifics("shipped 2,800 merged PRs and grew 5.5 by summer")
check("extract: scale suffix doesn't swallow 'merged'/'by'",
      not any(s.strip().lower().endswith(" m") for s in sm)
      and not any("5.5 b" == s.strip().lower() for s in sm))

# ---------------------------------------------------------------------------
# 3. ungrounded_specifics on the REAL fixture — EXACTLY the two roll-up sums.
# ---------------------------------------------------------------------------
ung = af.ungrounded_specifics(DRAFT, CORPUS)
digitruns = set()
import re as _re
for u in ung:
    m = _re.search(r"\d[\d,]*", u)
    if m:
        digitruns.add(m.group(0).replace(",", ""))
check("fixture: ungrounded digit-runs == {6200, 2800}",
      digitruns == {"6200", "2800"}, f"got {digitruns} from {ung}")
check("fixture: grounded '18 models' NOT flagged (corpus has '18 models')",
      not any("18" in u for u in ung))
check("fixture: grounded '8%/11%' NOT flagged",
      not any("%" in u for u in ung))
check("fixture: grounded 2,245/1,065/450 NOT flagged",
      not any(x in " ".join(ung) for x in ("2,245", "1,065", "450")))

# ---------------------------------------------------------------------------
# 4. annotate — APPEND-ONLY invariant + idempotency.
# ---------------------------------------------------------------------------
out = af.annotate(DRAFT, ung)
check("annotate: append-only (starts with the verbatim draft)",
      out.startswith(DRAFT))
check("annotate: original body unchanged (only a suffix added)",
      out[:len(DRAFT)] == DRAFT and len(out) > len(DRAFT))
check("annotate: caveat names both roll-ups",
      "6,200" in out[len(DRAFT):] and "2,800" in out[len(DRAFT):])
check("annotate: idempotent (second pass is a no-op)",
      af.annotate(out, ung) == out)
check("annotate: nothing to flag => draft unchanged",
      af.annotate(DRAFT, []) == DRAFT)
check("annotate: empty corpus => ungrounded == []",
      af.ungrounded_specifics(DRAFT, "") == [])
check("annotate: empty draft => []",
      af.ungrounded_specifics("", CORPUS) == [])

# ---------------------------------------------------------------------------
# 5. turn_corpus — WEB tool text after the last real user message; non-web
#    (execute_code / terminal) tool results are excluded.
# ---------------------------------------------------------------------------
def _web(s):
    return '<untrusted_tool_result source="web_search">\n' + s + "\n</untrusted_tool_result>"


msgs = [{"role": "user", "content": "old q"},
        {"role": "tool", "content": _web("OLD tool result")},
        {"role": "user", "content": "new q"},
        {"role": "assistant", "content": "", "tool_calls": [{}]},
        {"role": "tool", "content": _web("NEW result A")},
        {"role": "tool", "content": _web("NEW result B")}]
tc = af.turn_corpus(msgs)
check("turn_corpus: only this turn's web tool results",
      "NEW result A" in tc and "NEW result B" in tc and "OLD" not in tc)
check("turn_corpus: no tool results => ''",
      af.turn_corpus([{"role": "user", "content": "hi"}]) == "")
check("turn_corpus: non-web tool result (execute_code) excluded => ''",
      af.turn_corpus(
          [{"role": "user", "content": "compute"},
           {"role": "tool",
            "content": '{"output": "8cfde6", "exit_code": 0}'}]) == "")
check("turn_corpus: synthetic recovery user row not a boundary",
      "NEW" in af.turn_corpus(
          [{"role": "user", "content": "new q"},
           {"role": "tool", "content": _web("NEW")},
           {"role": "user", "content": "x returned an empty response y"}]))

# ---------------------------------------------------------------------------
# 5b. scale / precision equivalence — a sourced figure in a different scale form
#     (the "1.5B vs 1,500,000,000" case) must NOT be flagged.
# ---------------------------------------------------------------------------
web_pop = _web("India has a population of about 1.45 billion and China 1.41 billion.")
check("scale: 1,450,000,000 grounds on '1.45 billion'",
      af.ungrounded_specifics("India: 1,450,000,000 people.", web_pop) == [])
check("scale: 1.45B grounds on '1.45 billion'",
      af.ungrounded_specifics("India: 1.45B people.", web_pop) == [])
check("fuzzy: 1,412,000,000 grounds on '1.41 billion' (0.14% apart)",
      af.ungrounded_specifics("China: 1,412,000,000 people.", web_pop) == [])
check("fuzzy: a 6,200 roll-up with no near source value STILL flags",
      af.ungrounded_specifics("Total ~6,200 commits.",
                              _web("2,245 commits and 1,720 commits")) != [])

# 5c. user-supplied figures are grounded (not fabrications)
check("given: a % from the user question is not flagged",
      af.ungrounded_specifics("That is 2.3% growth of the figure.",
                              _web("population is 68 million"),
                              given="what is 2.3% annual growth") == [])

# 5d. computation-request detector
check("compute: 'compute what 0.5% of it is' => True",
      af.is_computation_request("find the population then compute what 0.5% of it is"))
check("compute: a pure research ask => False",
      not af.is_computation_request("Look up what Nous released and summarize"))
check("compute: 'compute cluster' noun use => False (negative lookahead)",
      not af.is_computation_request("research the best compute cluster options"))

# 5e. caveat list is capped for readability
many = ["%d,000,000" % k for k in range(20, 40)]
capped = af.annotate("Body.", many)
check("annotate: caps the listed items with 'and N more'",
      "and " in capped and "more" in capped
      and capped.startswith("Body."))

# ---------------------------------------------------------------------------
# 6. Fail-safe — every function swallows errors.
# ---------------------------------------------------------------------------
check("fail-safe: normalize(None) == ''", af.normalize(None) == "")
check("fail-safe: extract_specifics(None) == []", af.extract_specifics(None) == [])
check("fail-safe: ungrounded_specifics(None,None) == []",
      af.ungrounded_specifics(None, None) == [])
check("fail-safe: annotate(None, x) == ''", af.annotate(None, ["6,200"]) == "")
check("fail-safe: turn_corpus(None) == ''", af.turn_corpus(None) == "")

# ---------------------------------------------------------------------------
# 7. selfheal wiring — corpus cache + transform compose step, BOTH tiers.
# ---------------------------------------------------------------------------
sh._cfg = lambda: dict(sh.DEFAULTS, search_hard_cap=15)


def _pipe(tier_weak, cfg_ts, draft, messages, tools=True, sid="T"):
    sh._host_is_weak = lambda *a, **k: tier_weak
    sh.session_on_weak_host = lambda *a, **k: tier_weak
    sh._wh_ts = lambda: dict(cfg_ts)
    sh._reset_state()
    req = {"messages": messages}
    if tools:
        req["tools"] = [{"function": {"name": "web_search"}}]
    base = "https://vllm.example.com/v1" if tier_weak else "https://api.deepseek.com"
    sh._sh_middleware(request=req, api_mode="chat_completions", base_url=base,
                      session_id=sid, model="m")
    return sh._sh_transform_out(response_text=draft, session_id=sid,
                                platform="cli")


o_strong = _pipe(False, {}, DRAFT, FIX["messages"], sid="S")
check("wiring: STRONG tier B fires (default-on) + append-only",
      isinstance(o_strong, str) and o_strong.startswith(DRAFT)
      and "Unverified figures" in o_strong)
o_weak = _pipe(True, {}, DRAFT, FIX["messages"], sid="W")
check("wiring: WEAK tier B also fires (ADD-only, ceiling strong)",
      isinstance(o_weak, str) and "Unverified figures" in o_weak)
check("wiring: guard_active(claim_grounding) True on both tiers",
      sh.GUARD_MAX_TIER.get("claim_grounding") == "strong")

o_off = _pipe(False, {"antifab": "off"}, DRAFT, FIX["messages"], sid="O")
check("wiring: antifab=off => no caveat",
      o_off is None or "Unverified figures" not in o_off)

o_nocorpus = _pipe(False, {}, "Paris has ~2,100,000 residents.",
                   [{"role": "user", "content": "capital?"}], tools=False,
                   sid="N")
check("wiring: no retrieved corpus => no-op (not a research turn)",
      o_nocorpus is None or "Unverified" not in (o_nocorpus or ""))

# armed/replaced path: B must NOT run when the text was already replaced.
# (simulate by pre-seeding a corrupt finisher replacement is complex; instead
# assert the compose guard: a degenerate answer that the guards replace never
# carries a caveat because B only runs on the healthy path — covered by the
# `if not replaced` gate; here we assert a grounded healthy answer is untouched.)
grounded = "Nous shipped ~2,245 commits and 998 PRs; beats Opus by 8%."
o_grounded = _pipe(False, {}, grounded, FIX["messages"], sid="G")
check("wiring: fully-grounded healthy answer untouched (no false caveat)",
      o_grounded is None or "Unverified" not in o_grounded)

# fail-safe wiring: antifab raises => passthrough
_orig = af.ungrounded_specifics
try:
    af.ungrounded_specifics = lambda *a, **k: (_ for _ in ()).throw(RuntimeError())
    o_fs = _pipe(False, {}, DRAFT, FIX["messages"], sid="FS")
    check("wiring: antifab exception => passthrough (no crash, no caveat)",
          o_fs is None or "Unverified" not in o_fs)
finally:
    af.ungrounded_specifics = _orig

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
