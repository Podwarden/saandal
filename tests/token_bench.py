#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) Podwarden Inc
"""
token_bench.py — reusable TOKEN-CONSUMPTION benchmark for saandal.

Separate from run_corpus.py on purpose: run_corpus.py JUDGES reliability;
this harness MEASURES tokens. It runs tests/token_bench.yaml as fresh
`hermes chat -q <p> -Q` one-shots under a chosen HERMES_HOME, and captures a
per-prompt token breakdown straight from that home's state.db:

    fresh-input  (input_tokens)        prompt tokens billed fresh (NOT cached)
    cache-read   (cache_read_tokens)   prompt tokens served from provider cache
    output       (output_tokens)       visible completion tokens
    reasoning    (reasoning_tokens)    hidden CoT tokens (billed at output rate)
    total        fresh+cache+output+reasoning
    api_calls    (api_call_count)      LLM round-trips this turn
    tool_calls   (tool_call_count)
    wall_s       subprocess wall clock
    cost_est_usd OUR estimate from documented $/M rates (see RATES below)
    db_cost_usd  hermes's own estimated_cost_usd (its internal pricing)

Each one-shot is its OWN session (no --resume/--continue), so counts are
per-prompt clean and never accumulate a growing session. The session id is
read from stdout ("session_id: ...") and used to pull that exact row.

USAGE
-----
  # list the prompts with global indices
  python3 token_bench.py --list

  # run the whole set under a home, label the run
  python3 token_bench.py --home ~/.hermes-ds-clean --label ds_on
  python3 token_bench.py --home ~/.hermes-armb     --label ds_off

  # run only some indices (to stay under a caller wall cap; group deadline
  # stops launching NEW prompts and prints the remaining indices to re-invoke)
  python3 token_bench.py --home ... --label ds_on --indices 0,1,2,3

  # aggregate one label into a per-category + overall summary
  python3 token_bench.py --label ds_on --summarize

  # diff two labels (with vs without) into a delta table
  python3 token_bench.py --diff ds_on,ds_off

OUTPUTS (in --outdir, default the scratchpad)
  <label>_<id>.json     per-prompt record
  <label>.jsonl         append-only, one line per prompt run
  <label>_summary.json  per-category + overall aggregate (via --summarize)
  diff_<a>_vs_<b>.json  delta table (via --diff)
"""
import argparse, json, os, re, shutil, sqlite3, subprocess, sys, time, tempfile

# Run hermes from a NEUTRAL sandbox cwd, never the caller's directory — the
# model under test has a terminal tool and would otherwise operate in (and
# mutate) whatever cwd it is launched from. Keep the repo untouched.
_TEST_CWD = os.path.join(tempfile.gettempdir(), "saandal-test-cwd")
os.makedirs(_TEST_CWD, exist_ok=True)
from pathlib import Path

REPO = Path(__file__).resolve().parent
BENCH = REPO / "token_bench.yaml"
DEFAULT_OUT = Path(tempfile.gettempdir()) / "saandal-bench"
HERMES_BIN = shutil.which("hermes") or os.path.expanduser("~/.local/bin/hermes")

# Documented DeepSeek $/M ESTIMATES (label as estimates in any report).
# hermes stores its own estimated_cost_usd too; we capture both and let the
# report compare them. Override on the CLI with --rate-in/--rate-cache/--rate-out.
RATES = {"input": 0.28, "cache_read": 0.028, "output": 0.42}  # USD per 1M tokens


# ---------------------------------------------------------------------------
def load_bench():
    """Return [{gid,id,category,prompt}] in file order, gid = global index."""
    import yaml
    doc = yaml.safe_load(BENCH.read_text(encoding="utf-8"))
    out = []
    for cat, prompts in doc.get("categories", {}).items():
        for n, p in enumerate(prompts, 1):
            out.append({"id": f"{cat}_{n}", "category": cat, "prompt": p})
    for gid, rec in enumerate(out):
        rec["gid"] = gid
    return out


def read_state(home: Path, sid: str):
    """Pull the token row for one session id (read-only)."""
    db = home / "state.db"
    rec = {"session_found": False}
    if not sid or not db.exists():
        return rec
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = c.execute(
            "select model,end_reason,api_call_count,tool_call_count,"
            "input_tokens,cache_read_tokens,cache_write_tokens,output_tokens,"
            "reasoning_tokens,estimated_cost_usd,cost_status "
            "from sessions where id=?", (sid,)).fetchone()
        if row:
            rec.update({
                "session_found": True, "model": row[0], "end_reason": row[1],
                "api_call_count": row[2] or 0, "tool_call_count": row[3] or 0,
                "input_tokens": row[4] or 0, "cache_read_tokens": row[5] or 0,
                "cache_write_tokens": row[6] or 0, "output_tokens": row[7] or 0,
                "reasoning_tokens": row[8] or 0,
                "db_cost_usd": row[9], "cost_status": row[10]})
        a = c.execute(
            "select content from messages where session_id=? and role='assistant' "
            "and content is not null and content!='' order by id desc limit 1",
            (sid,)).fetchone()
        rec["answer"] = (a[0] or "").strip() if a else ""
        c.close()
    except Exception as e:
        rec["db_error"] = str(e)
    return rec


def cost_est(st, rates):
    """OUR cost estimate from documented $/M rates. Reasoning tokens are
    generated at the output rate, so bill (output+reasoning) as output."""
    inp = (st.get("input_tokens") or 0)
    cr = (st.get("cache_read_tokens") or 0)
    out = (st.get("output_tokens") or 0) + (st.get("reasoning_tokens") or 0)
    return round(inp * rates["input"] / 1e6
                 + cr * rates["cache_read"] / 1e6
                 + out * rates["output"] / 1e6, 6)


def total_tokens(st):
    return ((st.get("input_tokens") or 0) + (st.get("cache_read_tokens") or 0)
            + (st.get("output_tokens") or 0) + (st.get("reasoning_tokens") or 0))


# ---------------------------------------------------------------------------
def run_one(item, home: Path, outdir: Path, label: str, per_timeout: int, rates):
    home = Path(home)
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
        stdout = (e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes)
                  else (e.stdout or ""))
        stderr = (e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes)
                  else (e.stderr or ""))
        exit_code = 124
    wall = round(time.time() - t0, 1)

    # -Q prints "session_id: ..." to stderr on some builds, stdout on others —
    # search both streams.
    m = re.findall(r"session_id:\s*(\S+)", (stdout or "") + "\n" + (stderr or ""))
    sid = m[-1] if m else ""
    st = read_state(home, sid)

    rec = {
        "gid": item["gid"], "id": item["id"], "category": item["category"],
        "prompt": prompt, "home": str(home), "label": label, "session": sid,
        "exit": exit_code, "timed_out": timed_out, "wall_s": wall,
        "tokens": {
            "fresh_input": st.get("input_tokens"),
            "cache_read": st.get("cache_read_tokens"),
            "cache_write": st.get("cache_write_tokens"),
            "output": st.get("output_tokens"),
            "reasoning": st.get("reasoning_tokens"),
            "total": total_tokens(st) if st.get("session_found") else None,
        },
        "api_calls": st.get("api_call_count"),
        "tool_calls": st.get("tool_call_count"),
        "cost_est_usd": cost_est(st, rates) if st.get("session_found") else None,
        "db_cost_usd": st.get("db_cost_usd"),
        "model": st.get("model"), "end_reason": st.get("end_reason"),
        "session_found": st.get("session_found", False),
        "answer_head": (st.get("answer", "") or "")[:200],
        "stderr_tail": (stderr or "").strip()[-400:],
    }
    (outdir / f"{label}_{item['id']}.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=1))
    with (outdir / f"{label}.jsonl").open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    t = rec["tokens"]
    print(f"[{item['gid']:>2}] {item['id']:<16} wall={wall:<6} "
          f"api={rec['api_calls']} tool={rec['tool_calls']} | "
          f"fresh={t['fresh_input']} cacheRd={t['cache_read']} "
          f"out={t['output']} reas={t['reasoning']} tot={t['total']} | "
          f"est=${rec['cost_est_usd']} db=${rec['db_cost_usd']}")
    if not rec["session_found"]:
        print(f"     !! session not captured (sid='{sid}', exit={exit_code}, "
              f"timed_out={timed_out}) stderr: {rec['stderr_tail'][:160]}")
    return rec


# ---------------------------------------------------------------------------
def _load_runs(outdir, label):
    jl = outdir / f"{label}.jsonl"
    if not jl.exists():
        return {}
    seen = {}
    for line in jl.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        seen[r["id"]] = r          # last run of each id wins
    return seen


def _agg(runs):
    """Sum/mean the metrics over a list of run records (found sessions only)."""
    rs = [r for r in runs if r.get("session_found")]
    if not rs:
        return None
    def s(key_path):
        tot = 0
        for r in rs:
            v = r
            for k in key_path:
                v = v.get(k) if isinstance(v, dict) else None
            tot += (v or 0)
        return tot
    n = len(rs)
    return {
        "n": n,
        "fresh_input": s(["tokens", "fresh_input"]),
        "cache_read": s(["tokens", "cache_read"]),
        "output": s(["tokens", "output"]),
        "reasoning": s(["tokens", "reasoning"]),
        "total": s(["tokens", "total"]),
        "api_calls": s(["api_calls"]),
        "tool_calls": s(["tool_calls"]),
        "cost_est_usd": round(sum(r.get("cost_est_usd") or 0 for r in rs), 6),
        "db_cost_usd": round(sum(r.get("db_cost_usd") or 0 for r in rs), 6),
        "wall_s": round(sum(r.get("wall_s") or 0 for r in rs), 1),
        # per-prompt means (handier than sums for reading the table)
        "mean_fresh_input": round(s(["tokens", "fresh_input"]) / n, 1),
        "mean_total": round(s(["tokens", "total"]) / n, 1),
        "mean_api_calls": round(s(["api_calls"]) / n, 2),
        "mean_cost_est_usd": round((sum(r.get("cost_est_usd") or 0 for r in rs)) / n, 6),
    }


def summarize(outdir, label):
    runs = list(_load_runs(outdir, label).values())
    if not runs:
        print("no runs for label", label); return
    by_cat = {}
    for r in runs:
        by_cat.setdefault(r["category"], []).append(r)
    summ = {"label": label, "overall": _agg(runs),
            "by_category": {c: _agg(rs) for c, rs in sorted(by_cat.items())},
            "missing_sessions": [r["id"] for r in runs if not r.get("session_found")]}
    (outdir / f"{label}_summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=1))
    print(json.dumps(summ, ensure_ascii=False, indent=1))


def diff(outdir, a, b):
    """delta = a - b, per category and overall (a=with, b=without by convention)."""
    ra, rb = _load_runs(outdir, a), _load_runs(outdir, b)
    cats = sorted({r["category"] for r in list(ra.values()) + list(rb.values())})
    METRICS = ["fresh_input", "cache_read", "output", "reasoning", "total",
               "api_calls", "tool_calls", "cost_est_usd", "db_cost_usd", "wall_s"]

    def delta(rowsa, rowsb):
        aa, ab = _agg(rowsa), _agg(rowsb)
        if not aa or not ab:
            return None
        return {"with": {k: aa[k] for k in METRICS},
                "without": {k: ab[k] for k in METRICS},
                "delta": {k: round(aa[k] - ab[k], 6) for k in METRICS}}

    out = {"with_label": a, "without_label": b, "by_category": {}, "overall": None}
    for c in cats:
        d = delta([r for r in ra.values() if r["category"] == c],
                  [r for r in rb.values() if r["category"] == c])
        if d:
            out["by_category"][c] = d
    out["overall"] = delta(list(ra.values()), list(rb.values()))
    (outdir / f"diff_{a}_vs_{b}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))

    def line(name, d):
        if not d:
            print(f"{name:<14} (incomplete)"); return
        w, wo, dl = d["with"], d["without"], d["delta"]
        print(f"{name:<14} fresh {w['fresh_input']:>8}/{wo['fresh_input']:<8} "
              f"d={dl['fresh_input']:>+8}  out {w['output']:>6}/{wo['output']:<6} "
              f"tot {w['total']:>8}/{wo['total']:<8} d={dl['total']:>+8}  "
              f"api {w['api_calls']}/{wo['api_calls']}  "
              f"est$ {w['cost_est_usd']:.4f}/{wo['cost_est_usd']:.4f} "
              f"d={dl['cost_est_usd']:>+.4f}")
    print(f"\nDELTA = {a} (with) − {b} (without)\n")
    for c in cats:
        line(c, out["by_category"].get(c))
    line("OVERALL", out["overall"])
    return out


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", default=os.path.expanduser("~/.hermes-ds-clean"))
    ap.add_argument("--label", default="bench")
    ap.add_argument("--outdir", default=str(DEFAULT_OUT))
    ap.add_argument("--indices", help="comma list of global indices to run")
    ap.add_argument("--all", action="store_true", default=True)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--summarize", action="store_true")
    ap.add_argument("--diff", help="two labels 'a,b'; prints/saves delta a-b")
    ap.add_argument("--per-timeout", type=int, default=560)
    ap.add_argument("--group-deadline", type=int, default=540,
                    help="stop launching NEW prompts once cumulative wall passes this")
    ap.add_argument("--rate-in", type=float, default=RATES["input"])
    ap.add_argument("--rate-cache", type=float, default=RATES["cache_read"])
    ap.add_argument("--rate-out", type=float, default=RATES["output"])
    args = ap.parse_args()

    outdir = Path(args.outdir); outdir.mkdir(parents=True, exist_ok=True)
    rates = {"input": args.rate_in, "cache_read": args.rate_cache, "output": args.rate_out}
    bench = load_bench()

    if args.list:
        for it in bench:
            print(f"{it['gid']:>2}  {it['id']:<16} {str(it['prompt'])[:78]}")
        print(f"\nTotal: {len(bench)}"); return
    if args.summarize:
        summarize(outdir, args.label); return
    if args.diff:
        a, b = [x.strip() for x in args.diff.split(",")]
        diff(outdir, a, b); return

    if args.indices:
        idxs = [int(x) for x in args.indices.split(",") if x.strip() != ""]
    else:
        idxs = [it["gid"] for it in bench]

    by_gid = {it["gid"]: it for it in bench}
    remaining, t_group = [], time.time()
    for i, gid in enumerate(idxs):
        elapsed = time.time() - t_group
        if i > 0 and elapsed >= args.group_deadline:
            remaining = idxs[i:]; break
        budget = args.per_timeout if i == 0 else max(
            60, min(args.per_timeout, int(args.group_deadline - elapsed) + 60))
        run_one(by_gid[gid], Path(args.home), outdir, args.label, budget, rates)
    if remaining:
        print(f"\nGROUP DEADLINE reached. Re-invoke: --indices {','.join(map(str, remaining))}")
    else:
        print("\nGroup complete.")


if __name__ == "__main__":
    main()
