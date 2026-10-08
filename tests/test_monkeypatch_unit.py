#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Podwarden Inc

"""Offline unit suite for the v1.11.0 guarded monkeypatch harness
(plugin/monkeypatch.py): symbol resolution, replace, wrap, the automatic
safe-fallback (wrapper raises -> original runs), signature-drift guards, missing
/ moved targets, idempotency, and the fail-safe covenant (never raises).

Run: tests/test_monkeypatch_unit.py
"""
import importlib
import importlib.util
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "router_plugin", REPO / "plugin" / "__init__.py",
    submodule_search_locations=[str(REPO / "plugin")])
pkg = importlib.util.module_from_spec(spec)
sys.modules["router_plugin"] = pkg
spec.loader.exec_module(pkg)
mp = importlib.import_module("router_plugin.monkeypatch")

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"ok   {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


# ----- a throwaway target module (fresh function per test → isolation) -------
tm = types.ModuleType("mp_target")


def _mk(name, fn):
    setattr(tm, name, fn)


_mk("greet", lambda name: "hello " + name)
_mk("greet2", lambda name: "hi " + name)
_mk("greet3", lambda name: "orig " + name)
_mk("greet4", lambda name: "keep " + name)
_mk("greet5", lambda name: "a")
_mk("notfn", 42)


class Thing:
    def act(self, x):
        return x * 2


tm.Thing = Thing
sys.modules["mp_target"] = tm

# ---------------------------------------------------------- resolve ---
r = mp._resolve("mp_target.greet")
check("resolve: finds module-level function", r is not None and r[1] == "greet")
check("resolve: finds class method holder",
      mp._resolve("mp_target.Thing.act") is not None)
check("resolve: missing -> None", mp._resolve("mp_target.nope") is None)
check("resolve: unimportable -> None", mp._resolve("no.such.module.x") is None)
check("resolve: garbage -> None", mp._resolve(None) is None and mp._resolve("x") is None)

# ---------------------------------------------------------- replace ---
ok = mp.replace("mp_target.greet", lambda name: "HI " + name, tag="t-greet")
check("replace: returns True", ok)
check("replace: new function is called", tm.greet("bob") == "HI bob")
check("replace: original reachable via _router_orig",
      tm.greet._router_orig("bob") == "hello bob")
# idempotent: same tag again is a no-op, keeps the first patch
same = tm.greet
mp.replace("mp_target.greet", lambda name: "X", tag="t-greet")
check("replace: idempotent by tag", tm.greet is same)

# ---------------------------------------------------------- wrap ---
mp.wrap("mp_target.greet2", lambda orig, name: orig(name).upper())
check("wrap: wrapper runs around original", tm.greet2("bob") == "HI BOB")
check("wrap: original reachable via _router_orig",
      tm.greet2._router_orig("bob") == "hi bob")

# automatic safe-fallback: a wrapper that raises falls back to the original
def _bad(orig, *a, **k):
    raise ValueError("boom")


mp.wrap("mp_target.greet3", _bad)
check("wrap: buggy wrapper falls back to original (no raise)",
      tm.greet3("bob") == "orig bob")

# ---------------------------------------------------------- guards ---
check("guard: missing target -> replace False",
      mp.replace("mp_target.nope", lambda: 1) is False)
check("guard: missing target -> wrap False",
      mp.wrap("no.such.thing.at.all", lambda o: o) is False)
check("guard: non-callable target skipped",
      mp.replace("mp_target.notfn", lambda: 1) is False and tm.notfn == 42)

# verify (signature-drift) guard
before = tm.greet4
check("guard: verify mismatch -> skipped",
      mp.replace("mp_target.greet4", lambda name: "Z",
                 verify=mp.expects_params("zzz")) is False)
check("guard: verify-skipped target unchanged", tm.greet4 is before)
check("guard: verify match -> applied",
      mp.replace("mp_target.greet5", lambda name: "B",
                 verify=mp.expects_params("name")) is True)

# expects_params helper
check("expects_params: present -> True", mp.expects_params("name")(tm.greet4))
check("expects_params: absent -> False", not mp.expects_params("zzz")(tm.greet4))
check("expects_params: unreadable sig -> False", not mp.expects_params("x")(None))

# ---------------------------------------------------------- observability ---
names = {p["target"] for p in mp.applied()}
check("applied(): lists live patches",
      "mp_target.greet" in names and "mp_target.greet2" in names)
check("applied(): entries carry kind",
      all("kind" in p for p in mp.applied()))

# ---------------------------------------------------------- fail-safe ---
try:
    mp.replace(None, lambda: 1)
    mp.replace(123, None)
    mp.wrap(None, None)
    mp.wrap("mp_target.greet", None)   # wrapper None; must not raise
    mp.expects_params("a")(None)
    check("fail-safe: garbage never raises", True)
except Exception as e:
    check("fail-safe: garbage never raises", False, repr(e))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
