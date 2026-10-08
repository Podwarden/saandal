#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Podwarden Inc

"""Post-upgrade verification for the router plugin. Run after `hermes update`.

Checks, per profile:
  1. tool_search still exposes every symbol the plugin monkeypatches
     (if not, the plugin's guard already fell back to flat tools — capability
     is safe, but the token savings are gone until the plugin is adapted).
  2. A fresh offline inspection agent actually gets the deferred tool list
     (expected tool count and schema budget), proving the patch applied.

Exit 0 = router fully active everywhere. Exit 1 = at least one check failed;
the failing profile is running with flat tools (safe fallback), see output.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path.home() / ".hermes"
HERMES = Path.home() / ".local" / "bin" / "hermes"
PATCHED_SYMBOLS = ["_core_tool_names", "assemble_tool_defs", "classify_tools",
                   "BRIDGE_TOOL_NAMES", "TOOL_SEARCH_NAME"]
EXPECTED_TOOLS = 10         # keep_flat(7, incl. web_search) + bridge(3); adjust if keep_flat changes
MAX_TOOLS_JSON = 12_000     # bytes; flat mode is ~40k, router ~10k


def check_symbols() -> list[str]:
    import tools.tool_search as ts
    return [s for s in PATCHED_SYMBOLS if not hasattr(ts, s)]


# v1.6.0: the selfheal healer must register its middleware + hooks. Runs in
# a venv subprocess with HERMES_HOME set so per-profile plugin discovery is
# exercised exactly like a real process start.
SELFHEAL_PROBE = r"""
import json
from hermes_cli.plugins import discover_plugins, get_plugin_manager
discover_plugins()
m = get_plugin_manager()
def marked(cbs):
    return any(getattr(cb, "_router_selfheal", False) for cb in cbs or [])
def marked_pg(cbs):
    return any(getattr(cb, "_router_progress", False) for cb in cbs or [])
def marked_la(cbs):
    return any(getattr(cb, "_router_loadaware", False) for cb in cbs or [])
out = {
    "middleware": marked(m._middleware.get("llm_request")),
    "hooks": {h: marked(m._hooks.get(h)) for h in (
        "pre_tool_call", "pre_llm_call", "post_llm_call",
        "transform_llm_output", "pre_gateway_dispatch")},
    "progress_middleware": marked_pg(m._middleware.get("llm_request")),
    "progress_capture": marked_pg(m._hooks.get("pre_gateway_dispatch")),
    "loadaware_middleware": marked_la(m._middleware.get("llm_request")),
    "loadaware_hooks": (marked_la(m._hooks.get("post_api_request"))
                        and marked_la(m._hooks.get("api_request_error"))),
}
print(json.dumps(out))
"""


def check_selfheal(home: Path) -> str | None:
    env = dict(os.environ, HERMES_HOME=str(home))
    py = Path.home() / ".hermes" / "venv" / "bin" / "python3"
    out = subprocess.run([str(py), "-c", SELFHEAL_PROBE],
                         env=env, capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        return f"selfheal probe failed: {out.stderr.strip()[:200]}"
    try:
        data = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:
        return f"selfheal probe output unparseable: {out.stdout.strip()[:200]}"
    missing = [k for k, v in data["hooks"].items() if not v]
    if not data["middleware"]:
        missing.append("llm_request middleware")
    if not data.get("progress_middleware"):
        missing.append("progress llm_request middleware")
    if not data.get("progress_capture"):
        missing.append("progress pre_gateway_dispatch capture")
    if not data.get("loadaware_middleware"):
        missing.append("loadaware llm_request middleware")
    if not data.get("loadaware_hooks"):
        missing.append("loadaware post_api_request/api_request_error hooks")
    if missing:
        return f"selfheal/progress/loadaware NOT registered: missing {missing}"
    return None


def check_profile(home: Path) -> str | None:
    env = dict(os.environ, HERMES_HOME=str(home))
    out = subprocess.run(
        [str(HERMES), "prompt-size", "--json", "--platform", "telegram"],
        env=env, capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        return f"prompt-size failed: {out.stderr.strip()[:200]}"
    data = json.loads(out.stdout)
    n, b = data["tools"]["count"], data["tools"]["json_bytes"]
    if n != EXPECTED_TOOLS or b > MAX_TOOLS_JSON:
        return f"router NOT active: {n} tools / {b:,} B (expected {EXPECTED_TOOLS} / <{MAX_TOOLS_JSON:,} B)"
    return None


def main() -> int:
    failed = False
    missing = check_symbols()
    if missing:
        print(f"FAIL upstream API drift: tool_search lost symbols {missing} — "
              "plugin is in flat-tools fallback; adapt plugin/__init__.py")
        failed = True
    else:
        print("ok   tool_search symbols present")

    homes = [ROOT] + sorted(p for p in (ROOT / "profiles").iterdir() if p.is_dir())
    for home in homes:
        err = check_profile(home)
        name = home.name if home != ROOT else "root"
        if err:
            print(f"FAIL {name}: {err}")
            failed = True
        else:
            print(f"ok   {name}: router active ({EXPECTED_TOOLS} tools)")
        err = check_selfheal(home)
        if err:
            print(f"FAIL {name}: {err}")
            failed = True
        else:
            print(f"ok   {name}: selfheal + progress + loadaware registered "
                  "(middleware + 5 hooks; progress mw + capture; "
                  "loadaware mw + 2 hooks)")
    print("\nverdict:", "FAIL — see above (bots are on flat tools, not broken)"
          if failed else "PASS — router active on all profiles")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
