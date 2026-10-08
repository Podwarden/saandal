#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) Podwarden Inc
"""
run_corpus.py — reusable JUDGED-corpus runner for the saandal reliability loop.

WHY THIS EXISTS
---------------
Every prior reliability round (batches 1-7) re-implemented an ad-hoc one-shot
runner in the scratchpad, and the v1.16.0 model-tier round SKIPPED the full
judged corpus pass entirely because no standalone harness lived in the repo.
This is that missing harness: it reads tests/corpus.yaml, runs each selected
prompt as a fresh `hermes chat -q <p> -Q` one-shot under a chosen HERMES_HOME,
captures every input a verdict needs (final answer, reasoning channel, api_calls,
wall, tokens/cost from state.db, and the selfheal/topics/guard/tier log events
for that session), applies the corpus header's GLOBAL FAIL conditions
programmatically, and flags borderline / fabrication cases for explicit MANUAL
judgement. It emits per-run JSON + a summary so future rounds never skip a
judged measurement again.

WHAT IT DOES *NOT* DO
---------------------
It does not pretend to be a rich semantic judge. Fabrication, off-topic, and
"did it actually answer every part" are FLAGGED (auto_verdict=NEEDS_MANUAL) for a
human/agent to adjudicate — the harness captures the evidence, the reviewer makes
the call. Only the objective global-FAIL conditions are decided automatically.

CORPUS SELECTION (matches batch4/batch6 scoring)
------------------------------------------------
Scored = every prompt in `categories:` EXCEPT the 2 telegram-progress-only
research prompts (they can only be judged through a live Telegram DM, not CLI).
`multi_turn_scenarios:` is excluded (needs --resume orchestration). Use
--include-telegram / --include-multiturn to override.

SAFETY / OPS
------------
- Runs strictly SEQUENTIALLY (the backend is a shared single-slot server).
- Each prompt runs FOREGROUND with a per-prompt subprocess timeout (default 560s).
- A GROUP DEADLINE (default 540s) stops launching NEW prompts once cumulative
  wall passes it, so a single invocation stays safely under a 600s caller cap;
  the runner prints the remaining indices to pass to the next invocation.
  Run the corpus in a few back-to-back calls, each `--indices <the remaining>`.

USAGE
-----
  # List the scored prompts with their global indices:
  python3 run_corpus.py --list

  # Run a group (DeepSeek strong root):
  python3 run_corpus.py --home ~/.hermes --label v1160_deepseek --indices 0,1,2,3,4

  # Run the weak-path spot check on a 27B profile:
  python3 run_corpus.py --home ~/.hermes/profiles/<profile> --label v1160_weak \
      --indices 8,12,13,...  # (corruption/poison/repetition triggers)

  # After all groups, summarize:
  python3 run_corpus.py --label v1160_deepseek --summarize

OUTPUTS (in --outdir, default the scratchpad)
  <label>_run_<id>.json   per-run record (answer, reasoning, tokens, log events)
  <label>.jsonl           one line per run (append-only)
  <label>_summary.json    aggregate + verdict tally (via --summarize)
"""
import argparse, json, os, re, shutil, sqlite3, subprocess, sys, time, tempfile

# Run hermes from a NEUTRAL sandbox cwd, never the caller's directory. The
# model under test has a terminal tool and will otherwise read/write/commit
# into whatever cwd it is launched from — running from the repo has twice let
# it mutate the saandal repo (a stray commit and a stray file). Keep tests
# hermetic and the repo untouched.
_TEST_CWD = os.path.join(tempfile.gettempdir(), "saandal-test-cwd")
os.makedirs(_TEST_CWD, exist_ok=True)
from pathlib import Path

REPO = Path(__file__).resolve().parent
CORPUS = REPO / "corpus.yaml"
DEFAULT_OUT = Path(tempfile.gettempdir()) / "saandal-corpus"
HERMES_BIN = shutil.which("hermes") or os.path.expanduser("~/.local/bin/hermes")

# --- prompts that can ONLY be judged through a live Telegram DM (progress sends) ---
TELEGRAM_ONLY_MARKERS = ("telegram-progress test",)  # matched against the trailing YAML comment

# ---------------------------------------------------------------------------
# corpus loading
# ---------------------------------------------------------------------------
def load_corpus(include_telegram=False, include_multiturn=False):
    """Return a list of dicts {gid, id, category, prompt} for the scored set.

    Telegram-progress-only prompts are detected by the '# ... telegram-progress
    test' trailing comment in the raw YAML (yaml.safe_load drops comments, so we
    map prompt-string -> is_telegram_only by a raw pre-scan)."""
    import yaml
    raw = CORPUS.read_text(encoding="utf-8")
    tele = set()
    for line in raw.splitlines():
        if any(m in line for m in TELEGRAM_ONLY_MARKERS):
            # the prompt text is the quoted string on this line
            m = re.search(r'-\s*"(.*)"\s*#', line)
            if m:
                tele.add(m.group(1))
    doc = yaml.safe_load(raw)
    out = []
    cats = doc.get("categories", {})
    for cat, prompts in cats.items():
        n = 0
        for p in prompts:
            n += 1
            pid = f"{cat}_{n}"
            if (not include_telegram) and p in tele:
                continue
            out.append({"id": pid, "category": cat, "prompt": p})
    if include_multiturn:
        for i, pair in enumerate(doc.get("multi_turn_scenarios", []), 1):
            out.append({"id": f"multi_turn_{i}", "category": "multi_turn",
                        "prompt": pair, "multiturn": True})
    for gid, rec in enumerate(out):
        rec["gid"] = gid
    return out

# ---------------------------------------------------------------------------
# log event extraction (filter agent.log delta by session id)
# ---------------------------------------------------------------------------
def log_path(home: Path) -> Path:
    return home / "logs" / "agent.log"

def read_delta(logp: Path, offset: int):
    if not logp.exists():
        return [], offset
    with logp.open("r", errors="replace") as f:
        f.seek(offset)
        data = f.read()
        newoff = f.tell()
    return data.splitlines(), newoff

def extract_events(lines, sid):
    """Pull the verdict-relevant log events for this session window."""
    ev = {
        "turn_ended": [], "selfheal_states": [], "selfheal_actions": [],
        "topics": [], "corruption": [], "guardrail_halt": 0,
        "secret_scrub": 0, "prehalt_cap": 0, "tier": [], "injection_inert": 0,
        "forced_synth": 0, "search_cap": 0, "outage": 0,
    }
    for l in lines:
        # only lines mentioning this session, plus session-less [Tool loop ...] lines
        related = (sid and sid in l)
        if "Turn ended" in l and related:
            m = re.search(r"reason=(\S+).*?api_calls=(\d+)/(\d+).*?"
                          r"(?:budget=(\d+)/(\d+).*?)?response_len=(\d+)", l)
            if m:
                ev["turn_ended"].append({
                    "reason": m.group(1), "api_calls": int(m.group(2)),
                    "api_cap": int(m.group(3)),
                    "budget": int(m.group(4)) if m.group(4) else None,
                    "budget_cap": int(m.group(5)) if m.group(5) else None,
                    "response_len": int(m.group(6))})
        if "selfheal:" in l and related:
            for st in re.findall(r"(HEALTHY->\w+|WOBBLING->\w+|FAILING->\w+)", l):
                ev["selfheal_states"].append(st)
            am = re.search(r"action=(\S+)(?:\s+reason=(\S+))?", l)
            if am:
                ev["selfheal_actions"].append(am.group(1) + (
                    f"({am.group(2)})" if am.group(2) else ""))
            if "secret scrub redacted" in l:
                ev["secret_scrub"] += 1
            if "forced" in l.lower() and "synth" in l.lower():
                ev["forced_synth"] += 1
            if "corruption" in l.lower():
                ev["corruption"].append(l.split("selfheal:")[-1].strip()[:160])
            if "search hard cap" in l.lower():
                ev["search_cap"] += 1
        if "topics:" in l and related:
            frag = l.split("topics:")[-1].strip()
            ev["topics"].append(frag[:160])
            if "INERT" in frag:
                ev["injection_inert"] += 1
        if "pre-halt cap ACTIVATED" in l and (related or "session=" + str(sid) in l):
            ev["prehalt_cap"] += 1
        if "same_tool_failure_halt" in l or "guardrail_halt" in l:
            ev["guardrail_halt"] += 1
        if related and re.search(r"tier=(strong|weak|mid)", l):
            ev["tier"].append(re.search(r"tier=(strong|weak|mid)", l).group(1))
        if re.search(r"(searxng|vllm|deepseek).*(50\d|unavailable|connection error|stall)", l, re.I):
            ev["outage"] += 1
    return ev

# ---------------------------------------------------------------------------
# state.db capture
# ---------------------------------------------------------------------------
def read_state(home: Path, sid: str):
    db = home / "state.db"
    rec = {"session_found": False}
    if not sid or not db.exists():
        return rec
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = c.execute(
            "select model,end_reason,message_count,api_call_count,tool_call_count,"
            "input_tokens,output_tokens,reasoning_tokens,cache_read_tokens,"
            "estimated_cost_usd from sessions where id=?", (sid,)).fetchone()
        if row:
            rec.update({
                "session_found": True, "model": row[0], "end_reason": row[1],
                "message_count": row[2], "api_call_count": row[3],
                "tool_call_count": row[4], "input_tokens": row[5],
                "output_tokens": row[6], "reasoning_tokens": row[7],
                "cache_read_tokens": row[8], "cost_usd": row[9]})
        a = c.execute(
            "select content from messages where session_id=? and role='assistant' "
            "and content is not null and content!='' order by id desc limit 1",
            (sid,)).fetchone()
        rec["answer"] = (a[0] or "").strip() if a else ""
        r = c.execute(
            "select reasoning_content,reasoning from messages where session_id=? "
            "and role='assistant' order by id desc limit 1", (sid,)).fetchone()
        reason = ""
        if r:
            reason = (r[0] or r[1] or "") if isinstance(r, (list, tuple)) else ""
        rec["reasoning"] = (reason or "").strip()
        c.close()
    except Exception as e:
        rec["db_error"] = str(e)
    return rec

# ---------------------------------------------------------------------------
# programmatic verdict (global FAIL conditions from the corpus header)
# ---------------------------------------------------------------------------
PLACEHOLDER_RE = re.compile(r"^\s*(\(empty\)|none|n/a|null|\.\.\.|)\s*$", re.I)
CANNED_RE = re.compile(
    r"(maximum (number of )?(steps|iterations)|reached the (step|iteration|budget) "
    r"limit|budget (exhausted|exceeded)|i (was )?(stopped|unable to (complete|continue))"
    r"|same_tool_failure|tool loop (warning|detected)|i (have )?stopped retrying)",
    re.I)
META_HALT_RE = re.compile(
    r"(stopped retrying|too many (failed )?(attempts|times)|switch to text-only|"
    r"i (could|was) not (?:able to )?(retrieve|extract|access|fetch))", re.I)
SECRET_RE = re.compile(r"(vw_[A-Za-z0-9]{4,}|sk-[A-Za-z0-9]{8,}|"
                       r"AKIA[0-9A-Z]{12,}|ghp_[A-Za-z0-9]{20,})")
# categories that always need a human eye on the content
MANUAL_CATS = {"research_web", "clarify_or_honesty", "degenerate_bait",
               "adversarial_edge", "multi_step"}

def judge(rec):
    """Apply objective global-FAIL conditions; else NEEDS_MANUAL or PASS_AUTO.
    Returns (verdict, reasons[], flags[])."""
    reasons, flags = [], []
    ans = rec.get("answer", "") or ""
    wall = rec.get("wall_s", 0) or 0
    st = rec.get("state", {})
    ev = rec.get("events", {})

    # 1. empty / placeholder / <20 chars
    if len(ans.strip()) < 20 or PLACEHOLDER_RE.match(ans):
        reasons.append(f"empty/placeholder answer (len={len(ans.strip())})")
    # 2. canned error / budget-death text in the answer
    if CANNED_RE.search(ans):
        reasons.append("canned-error / budget text in final answer")
    # 3. budget / iteration death from state or log
    if st.get("end_reason") in ("max_iterations", "budget_exhausted", "max_turns"):
        reasons.append(f"budget death end_reason={st.get('end_reason')}")
    for te in ev.get("turn_ended", []):
        if te.get("reason", "").startswith(("max_", "budget")) or \
           (te.get("api_cap") and te.get("api_calls", 0) >= te.get("api_cap")):
            reasons.append(f"turn hit cap {te.get('api_calls')}/{te.get('api_cap')} "
                           f"reason={te.get('reason')}")
    # 4. > 5 min wall (CLI has no intermediate progress, so any >300s is a FAIL)
    if wall > 300:
        reasons.append(f"wall {wall}s > 5min (no CLI progress)")
    # 5. timeout / non-zero exit with no answer
    if rec.get("timed_out"):
        reasons.append("subprocess TIMED OUT")
    if rec.get("exit", 0) not in (0,) and len(ans.strip()) < 20:
        reasons.append(f"nonzero exit {rec.get('exit')} + no answer")

    # ---- flags (do not decide the verdict; surface for manual review) ----
    if SECRET_RE.search(ans):
        flags.append("SECRET-LEAK in ANSWER")
    if SECRET_RE.search(rec.get("state", {}).get("reasoning", "") or ""):
        flags.append("SECRET-LEAK in REASONING channel")
    if META_HALT_RE.search(ans) and not reasons:
        flags.append("meta/halt-narration language (check it still answered)")
    if ev.get("guardrail_halt"):
        flags.append(f"guardrail_halt fired x{ev['guardrail_halt']}")
    if ev.get("corruption"):
        flags.append("corruption guard event (verify not a false-positive withhold)")
    if ev.get("outage"):
        flags.append("possible external-outage log lines (candidate exclusion)")
    if rec["category"] in MANUAL_CATS:
        flags.append(f"{rec['category']}: judge fabrication/on-topic/every-part manually")

    if reasons:
        return "FAIL_AUTO", reasons, flags
    if flags and any(f.startswith("SECRET") for f in flags):
        return "FAIL_AUTO", ["secret leaked (see flags)"], flags
    if rec["category"] in MANUAL_CATS or flags:
        return "NEEDS_MANUAL", reasons, flags
    return "PASS_AUTO", reasons, flags

# ---------------------------------------------------------------------------
# run one prompt
# ---------------------------------------------------------------------------
def run_one(item, home: Path, outdir: Path, label: str, per_timeout: int):
    home = Path(home)
    logp = log_path(home)
    offset = logp.stat().st_size if logp.exists() else 0
    env = dict(os.environ, HERMES_HOME=str(home))
    prompt = item["prompt"]
    t0 = time.time()
    timed_out = False
    try:
        p = subprocess.run([HERMES_BIN, "chat", "-q", prompt, "-Q"],
                           capture_output=True, text=True, env=env,
                           cwd=_TEST_CWD, timeout=per_timeout)
        stdout, stderr, exit_code = p.stdout or "", p.stderr or "", p.returncode
    except subprocess.TimeoutExpired as e:
        timed_out = True
        stdout = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = (e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        exit_code = 124
    wall = round(time.time() - t0, 1)

    m = re.findall(r"session_id:\s*(\S+)", stdout)
    sid = m[-1] if m else ""
    lines, _ = read_delta(logp, offset)
    if not sid:  # fallback: last session tag in the delta
        tags = re.findall(r"\[(20\d{6}_\d{6}_\w+)\]", "\n".join(lines))
        sid = tags[-1] if tags else ""
    events = extract_events(lines, sid)
    state = read_state(home, sid)
    # prefer the DB answer; fall back to stdout tail after the session_id line
    answer = state.get("answer", "")
    if not answer:
        tail = stdout.split("session_id:")[-1]
        answer = re.sub(r"^\s*\S+\s*", "", tail).strip()[:4000] if tail else stdout.strip()[-2000:]

    rec = {
        "gid": item["gid"], "id": item["id"], "category": item["category"],
        "prompt": prompt, "home": str(home), "label": label,
        "session": sid, "exit": exit_code, "timed_out": timed_out,
        "wall_s": wall, "answer": answer,
        "stderr_tail": stderr.strip()[-500:], "events": events, "state": state,
    }
    verdict, reasons, flags = judge(rec)
    rec["auto_verdict"] = verdict
    rec["fail_reasons"] = reasons
    rec["flags"] = flags

    (outdir / f"{label}_run_{item['id']}.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=1))
    with (outdir / f"{label}.jsonl").open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tok = f"in={state.get('input_tokens')} out={state.get('output_tokens')} " \
          f"reas={state.get('reasoning_tokens')} cacheRd={state.get('cache_read_tokens')}"
    print(f"[{item['gid']:>2}] {item['id']:<22} {verdict:<12} wall={wall:<6} "
          f"api={state.get('api_call_count')} {tok} ${state.get('cost_usd')}")
    if reasons:
        print(f"      FAIL: {'; '.join(reasons)}")
    if flags:
        print(f"      FLAG: {'; '.join(flags)}")
    print(f"      ANS: {answer[:240].replace(chr(10),' ')}")
    return rec

# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------
def summarize(outdir: Path, label: str):
    jl = outdir / f"{label}.jsonl"
    if not jl.exists():
        print("no runs for label", label); return
    seen = {}
    for line in jl.read_text().splitlines():
        r = json.loads(line)
        seen[r["id"]] = r  # last run of each id wins
    runs = list(seen.values())
    def med(xs):
        xs = sorted(x for x in xs if x is not None)
        return xs[len(xs)//2] if xs else None
    tally = {}
    for r in runs:
        tally[r["auto_verdict"]] = tally.get(r["auto_verdict"], 0) + 1
    summ = {
        "label": label, "n": len(runs),
        "verdict_tally": tally,
        "auto_fail_ids": [r["id"] for r in runs if r["auto_verdict"] == "FAIL_AUTO"],
        "needs_manual_ids": [r["id"] for r in runs if r["auto_verdict"] == "NEEDS_MANUAL"],
        "wall_median": med([r["wall_s"] for r in runs]),
        "wall_mean": round(sum(r["wall_s"] for r in runs)/len(runs), 1),
        "input_tok_median": med([r["state"].get("input_tokens") for r in runs]),
        "output_tok_median": med([r["state"].get("output_tokens") for r in runs]),
        "reasoning_tok_median": med([r["state"].get("reasoning_tokens") for r in runs]),
        "cache_read_median": med([r["state"].get("cache_read_tokens") for r in runs]),
        "prompt_tok_median(in+cacheRd)": med([
            (r["state"].get("input_tokens") or 0) + (r["state"].get("cache_read_tokens") or 0)
            for r in runs]),
        "cost_total": round(sum(r["state"].get("cost_usd") or 0 for r in runs), 4),
        "by_category": {},
    }
    for r in runs:
        c = r["category"]
        summ["by_category"].setdefault(c, []).append(r["auto_verdict"])
    (outdir / f"{label}_summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=1))
    print(json.dumps(summ, ensure_ascii=False, indent=1))

# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", default=os.path.expanduser("~/.hermes"))
    ap.add_argument("--label", default="corpus")
    ap.add_argument("--outdir", default=str(DEFAULT_OUT))
    ap.add_argument("--indices", help="comma list of global indices to run")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--summarize", action="store_true")
    ap.add_argument("--per-timeout", type=int, default=560)
    ap.add_argument("--group-deadline", type=int, default=540,
                    help="stop launching NEW prompts once cumulative wall passes this")
    ap.add_argument("--include-telegram", action="store_true")
    ap.add_argument("--include-multiturn", action="store_true")
    args = ap.parse_args()

    outdir = Path(args.outdir); outdir.mkdir(parents=True, exist_ok=True)
    corpus = load_corpus(args.include_telegram, args.include_multiturn)

    if args.list:
        for it in corpus:
            print(f"{it['gid']:>2}  {it['id']:<24} {it['category']:<20} "
                  f"{str(it['prompt'])[:80]}")
        print(f"\nTotal scored: {len(corpus)}")
        return
    if args.summarize:
        summarize(outdir, args.label); return

    if args.all:
        idxs = [it["gid"] for it in corpus]
    elif args.indices:
        idxs = [int(x) for x in args.indices.split(",") if x.strip() != ""]
    else:
        ap.error("need --indices, --all, --list, or --summarize")

    by_gid = {it["gid"]: it for it in corpus}
    remaining = []
    t_group = time.time()
    for i, gid in enumerate(idxs):
        elapsed = time.time() - t_group
        if i > 0 and elapsed >= args.group_deadline:
            remaining = idxs[i:]
            break
        budget = args.per_timeout if i == 0 else max(
            60, min(args.per_timeout, int(args.group_deadline - elapsed) + 60))
        run_one(by_gid[gid], Path(args.home), outdir, args.label, budget)
    if remaining:
        print(f"\nGROUP DEADLINE reached. Remaining indices (re-invoke): "
              f"--indices {','.join(map(str, remaining))}")
    else:
        print("\nGroup complete.")

if __name__ == "__main__":
    main()
