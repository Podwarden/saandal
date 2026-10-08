# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Podwarden Inc

"""Guarded runtime-monkeypatch harness (v1.11.0).

A small, fail-safe way to override hermes-core functions the plugin hook/
middleware surfaces can't reach — for the rare change that lives at a clean
function boundary (replace a whole function, wrap one, override a default).

PHILOSOPHY (read before adding a patch):
  * Plugin hooks/middleware first. Monkeypatching reaches into hermes internals
    that are NOT a stable API; every patch is a liability across upgrades.
  * Only patch at a clean boundary — a WHOLE function or a wrapper. NEVER patch
    "a few lines in the middle of a big function": that forces copying the whole
    function, which then silently diverges from every future hermes release.
    For surgical mid-function edits use a patch-overlay (install.py) instead, so
    drift fails LOUDLY rather than silently.
  * Every patch here is symbol-guarded and fail-safe: if the target is missing,
    moved, or fails its `verify`, the patch is SKIPPED (one warning) and stock
    hermes runs unchanged. A wrapped function whose wrapper raises falls back to
    the original. A monkeypatch must never be able to break hermes.

Usage (from _register_monkeypatches in __init__.py):

    from . import monkeypatch as mp
    # replace a whole function
    mp.replace("agent.conversation_loop._get_continuation_prompt",
               my_prompt_fn, verify=mp.expects_params("is_partial_stub"),
               tag="continuation-prompt")
    # wrap a function (our code runs around the original)
    mp.wrap("agent.chat_completion_helpers.some_fn", my_wrapper,
            verify=mp.expects_params("chunk"))

`applied()` returns the list of live patches for observability / verify.py.
"""

import functools
import importlib
import inspect
import logging

logger = logging.getLogger(__name__)

_MARKER = "_router_monkeypatch"
_applied = {}          # tag -> {"target","kind"} for observability + idempotency
_warned = set()        # one warning per (reason, target)


def _warn_once(key, msg, *args, **kw):
    if key in _warned:
        return
    if len(_warned) < 256:
        _warned.add(key)
    logger.warning(msg, *args, **kw)


def _resolve(dotted):
    """'a.b.c.name' -> (holder_obj, 'name', current_value). Imports the longest
    importable module prefix, then walks the remaining attributes to the holder
    that owns the final name. Returns None if anything can't be resolved."""
    try:
        parts = [p for p in str(dotted).replace(":", ".").split(".") if p]
        if len(parts) < 2:
            return None
        mod, idx = None, 0
        for i in range(len(parts), 0, -1):
            try:
                mod = importlib.import_module(".".join(parts[:i]))
                idx = i
                break
            except Exception:
                continue
        if mod is None:
            return None
        attrs = parts[idx:]
        if not attrs:
            return None
        holder = mod
        for a in attrs[:-1]:
            holder = getattr(holder, a, None)
            if holder is None:
                return None
        name = attrs[-1]
        if not hasattr(holder, name):
            return None
        return holder, name, getattr(holder, name)
    except Exception:
        return None


def expects_params(*names):
    """Return a `verify` callable that passes iff the target's signature
    contains every named parameter — a guard against upstream signature drift.
    Fails safe (returns False) if the signature can't be read."""
    def _v(current):
        try:
            params = set(inspect.signature(current).parameters)
            return all(n in params for n in names)
        except Exception:
            return False
    return _v


def _guard(dotted, verify, kind):
    """Shared pre-flight: resolve + already-patched check + verify. Returns
    (holder, name, current) to proceed, or None to skip (already logged)."""
    r = _resolve(dotted)
    if r is None:
        _warn_once("missing:" + str(dotted),
                   "monkeypatch: target %r not found — SKIPPED (stock hermes "
                   "runs unchanged)", dotted)
        return None
    holder, name, current = r
    if getattr(current, _MARKER, False):
        return None  # already patched (re-import / force rescan) — idempotent
    if not callable(current):
        _warn_once("notcallable:" + str(dotted),
                   "monkeypatch: target %r is not callable — SKIPPED", dotted)
        return None
    if verify is not None:
        try:
            ok = bool(verify(current))
        except Exception:
            ok = False
        if not ok:
            _warn_once("verify:" + str(dotted),
                       "monkeypatch: verify failed for %r (signature/shape "
                       "changed upstream?) — SKIPPED", dotted)
            return None
    return holder, name, current


def replace(dotted, new_callable, *, verify=None, tag=None):
    """Replace the function/attribute at *dotted* with *new_callable*.
    *new_callable* may reference the original via its ``_router_orig`` attribute
    (set here). Returns True iff the patch was applied. Never raises."""
    tag = tag or dotted
    try:
        if tag in _applied:
            return True
        g = _guard(dotted, verify, "replace")
        if g is None:
            return tag in _applied
        holder, name, current = g
        try:
            new_callable._router_orig = current
            setattr(new_callable, _MARKER, True)
        except Exception:
            pass
        setattr(holder, name, new_callable)
        _applied[tag] = {"target": dotted, "kind": "replace"}
        logger.info("monkeypatch: replaced %s (tag=%s)", dotted, tag)
        return True
    except Exception:
        _warn_once("replace-failed:" + str(dotted),
                   "monkeypatch: replace(%s) failed — target unchanged",
                   dotted, exc_info=True)
        return False


def wrap(dotted, wrapper, *, verify=None, tag=None):
    """Wrap the function at *dotted*: the installed function calls
    ``wrapper(orig, *args, **kwargs)``. If *wrapper* raises, the ORIGINAL is
    called instead (automatic safe-fallback), so a buggy wrapper can never break
    hermes. Returns True iff applied. Never raises."""
    tag = tag or ("wrap:" + str(dotted))
    try:
        if tag in _applied:
            return True
        g = _guard(dotted, verify, "wrap")
        if g is None:
            return tag in _applied
        holder, name, orig = g

        @functools.wraps(orig)
        def _wrapped(*args, **kwargs):
            try:
                return wrapper(orig, *args, **kwargs)
            except Exception:
                logger.debug("monkeypatch: wrapper for %s raised; falling back "
                             "to the original", dotted, exc_info=True)
                return orig(*args, **kwargs)

        setattr(_wrapped, _MARKER, True)
        _wrapped._router_orig = orig
        setattr(holder, name, _wrapped)
        _applied[tag] = {"target": dotted, "kind": "wrap"}
        logger.info("monkeypatch: wrapped %s (tag=%s)", dotted, tag)
        return True
    except Exception:
        _warn_once("wrap-failed:" + str(dotted),
                   "monkeypatch: wrap(%s) failed — target unchanged",
                   dotted, exc_info=True)
        return False


def applied():
    """List of live patches: [{'tag','target','kind'}, ...] — for verify.py /
    observability."""
    return [{"tag": k, **v} for k, v in sorted(_applied.items())]


def _reset_for_tests():
    _applied.clear()
    _warned.clear()
