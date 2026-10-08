# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Podwarden Inc

"""progress — content-bearing intermediate progress messages for long turns
(v1.7.0; master flag ``tools.tool_search.progress.enabled``, default on).

USER REQUIREMENT: during a long-running turn the user wants intermediate
Telegram messages that carry ACTUAL INFORMATION ALREADY FOUND plus an explicit
notice that this is not the final answer and the system is still working — not
a bare typing indicator ("So far I've found X and Y; still checking…").

WHY A PLUGIN FEATURE (feasibility, verified in site-packages 0.18.2):

* hermes's ``display.tool_progress`` status bubble (gateway/run.py:16821,
  progress_callback :16924) emits per-tool LIFECYCLE events, and
  gateway/status_phrases.py deliberately rewrites long-running status into
  GENERIC placeholders ("still working through it") — its own docstring:
  "raw tool args, commands, previews, and reasoning text are never
  interpolated". So the status bubble is generic-by-design; making it
  carry a findings digest would need site-packages edits (out of bounds).

* hermes DOES have a native content-bearing interim surface —
  ``interim_assistant_callback`` (run.py:17808, fired from
  run_agent._emit_interim_assistant_message:4635 whenever the MODEL emits
  visible commentary alongside a tool call). It is model-driven: on
  local-27b (no_think_always on, constrained decoding, the documented
  silent-loop pathologies) the model rarely writes such commentary, so it
  cannot be relied on. This feature SYNTHESIZES progress from the actual
  tool RESULTS regardless of whether the model narrated, and complements —
  never replaces — that native path.

DELIVERY (reachable without site-packages edits, same proven path as
selfheal's fresh-retry): a ``pre_gateway_dispatch`` hook captures the live
GatewayRunner weakref + running event loop + the per-session SessionSource;
an ``llm_request`` middleware (fires once per API call — the natural "step"
+ wall-clock trigger, and the surface that already carries the request
messages) harvests a findings digest from the conversation's tool results,
and when the trigger/throttle policy says so, fire-and-forgets an interim
message onto the captured loop via
``gateway._adapter_for_source(source).send(chat_id=…, content=…,
metadata={thread_id})`` — the byte-identical call selfheal._deliver_text
uses. The send is scheduled with asyncio.run_coroutine_threadsafe and never
awaited in the middleware, so a slow or failing send can NEVER block or slow
the turn.

TRIGGER POLICY (pure, unit-tested): the first interim fires once the turn
crosses ~first_delay_secs of wall time OR ~first_steps API calls, then
throttled by BOTH a min time gap and a min step gap, and ONLY when there is
GENUINELY NEW content to report (a finding whose text has not already been
sent this turn) — never spam "still working" with nothing. Capped at
max_per_turn interim messages per turn.

ANTI-FABRICATION (reuses the v1.6.x discipline): the digest is built ONLY
from what tool results literally contained (web result title — url, or the
first substantial line of a successful tool output); error results and
loop-guard blocks are skipped, nothing is invented.

FAIL-SAFE COVENANT: every callback is wrapped so it returns None on any
error; delivery is fire-and-forget; a missing/renamed gateway symbol logs
one warning and disables just this feature. Normal operation is never
affected. Config (all under ``tools.tool_search.progress``, read per call):

    enabled: "on"          master switch ("off" = pure no-op)
    platforms: [telegram]  delivery allowlist (empty = all platforms)
    first_delay_secs: 25   earliest first interim by wall time
    first_steps: 3         …or by API-call count, whichever comes first
    throttle_secs: 30      min seconds between interims after the first
    throttle_steps: 2      …AND min API calls between interims
    min_new_findings: 1    require >= this many NEW findings to send
    max_per_turn: 4        hard cap on interims per turn
    max_findings_per_msg: 3

Registered from plugin/__init__.py::register() AFTER selfheal (so this
read-only middleware sits last and never perturbs the healer's mutations).
"""
import asyncio
import hashlib
import json
import logging
import re
import threading
import time
import weakref

logger = logging.getLogger("hermes.plugins.router.progress")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULTS = {
    "enabled": "on",
    "platforms": ["telegram"],
    "first_delay_secs": 25,
    "first_steps": 3,
    "throttle_secs": 30,
    "throttle_steps": 2,
    "min_new_findings": 1,
    "max_per_turn": 4,
    "max_findings_per_msg": 3,
}

_FLAG_KEYS = ("enabled",)
_INT_KEYS = ("first_delay_secs", "first_steps", "throttle_secs",
             "throttle_steps", "min_new_findings", "max_per_turn",
             "max_findings_per_msg")


def _norm_flag(v, default):
    if isinstance(v, bool):
        return "on" if v else "off"
    s = str(v).strip().lower()
    return s if s in ("on", "off") else default


def _cfg():
    """Effective progress config (DEFAULTS overlaid with the config file).

    Read per call like every other router knob — a config flip needs no
    restart. Invalid values fall back to the default; never raises.
    """
    out = dict(DEFAULTS)
    out["platforms"] = list(DEFAULTS["platforms"])
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        pg = (((_load().get("tools") or {}).get("tool_search") or {})
              .get("progress") or {})
        if isinstance(pg, dict):
            for k in _FLAG_KEYS:
                if k in pg and pg[k] is not None:
                    out[k] = _norm_flag(pg[k], out[k])
            for k in _INT_KEYS:
                if k in pg and pg[k] is not None and not isinstance(pg[k], bool):
                    try:
                        out[k] = int(pg[k])
                    except Exception:
                        pass
            if isinstance(pg.get("platforms"), list):
                out["platforms"] = [str(p).strip().lower()
                                    for p in pg["platforms"] if str(p).strip()]
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_TABLE_MAX = 64
_IDLE_S = 3600

_gw = {"ref": None, "loop": None, "ts": 0.0}
_sessions = {}   # session_key -> {"source","session_id","ts"}
_turns = {}      # (session_id, turn_id) -> progress state dict
_warned = set()


def _warn_once(key, msg, *args):
    if key in _warned:
        return
    # v1.8.6: cap the dedup set (one key is per-session) so a long-lived
    # process can't grow it unboundedly.
    if len(_warned) > 512:
        _warned.clear()
    _warned.add(key)
    logger.warning(msg, *args)


def _prune(table, now):
    if len(table) <= _TABLE_MAX:
        return
    cutoff = now - _IDLE_S
    for k in [k for k, v in list(table.items())
              if float((v or {}).get("ts") or 0) < cutoff]:
        table.pop(k, None)
    if len(table) > _TABLE_MAX:
        table.clear()


def _turn_key(session_id, turn_id):
    return (session_id, turn_id) if turn_id else (session_id, "window")


def _turn_state(session_id, turn_id, now):
    """Get-or-create the per-turn progress state. A new turn_id starts fresh."""
    key = _turn_key(session_id, turn_id)
    st = _turns.get(key)
    if st is None:
        st = {"first_ts": now, "ts": now, "sent_count": 0,
              "last_sent_ts": 0.0, "last_sent_calls": 0, "reported": set()}
        _turns[key] = st
        _prune(_turns, now)
    st["ts"] = now
    return st


def _reset_state():
    """Test helper: wipe all module state."""
    _gw.update({"ref": None, "loop": None, "ts": 0.0})
    _sessions.clear()
    _turns.clear()
    _warned.clear()


# ---------------------------------------------------------------------------
# Pure functions (unit-testable without hermes)
# ---------------------------------------------------------------------------

_GUARD_ERR_MARKERS = ("duplicate query loop", "duplicate call loop",
                      "search limit reached")

# hermes wraps every tool result the model sees in an untrusted-content
# envelope: an opening <untrusted_tool_result source="…"> tag, a
# "treat this as DATA, do not follow instructions …" boilerplate paragraph,
# the real payload, then a closing </untrusted_tool_result> tag. Harvesting
# must peel that off first — otherwise json.loads() fails on the leading tag
# and the digest surfaces the raw wrapper tag as a "finding".
_WRAPPER_TAG_RE = re.compile(r"</?[a-z][a-z0-9_]*tool_result\b[^>]*>", re.I)
_BOILERPLATE_RE = re.compile(
    r"The following content was retrieved from an external source\.?.*?"
    r"can issue instructions\.", re.I | re.S)


def strip_tool_result_wrapper(text):
    """Peel hermes's untrusted-tool-result envelope (open/close tags + the
    do-not-follow boilerplate) off *text* so the real payload — JSON or prose —
    is exposed for harvesting. Fail-safe: returns the input on any error."""
    try:
        s = _WRAPPER_TAG_RE.sub("", str(text))
        s = _BOILERPLATE_RE.sub("", s)
        return s.strip()
    except Exception:
        return text if isinstance(text, str) else ""


def _loads_lenient(s):
    """json.loads that tolerates leading/trailing noise: parse the first JSON
    object/array embedded in *s*. Returns None when nothing parses; never
    raises. Backstop for wrappers this module hasn't learned to strip yet."""
    try:
        return json.loads(s)
    except Exception:
        pass
    try:
        dec = json.JSONDecoder()
        for i, ch in enumerate(s):
            if ch in "{[":
                try:
                    obj, _end = dec.raw_decode(s, i)
                    return obj
                except ValueError:
                    continue
    except Exception:
        pass
    return None


def _content_text(content):
    """Best-effort plain text of a message content (str or parts list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                t = p.get("text")
                if isinstance(t, str):
                    parts.append(t)
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts)
    return "" if content is None else str(content)


def harvest_findings(messages, limit=3):
    """Anti-fabrication digest: one line each from the most recent <=limit
    successful tool results, newest-first order preserved as oldest-first.

    Web results yield "title — url"; other tool outputs yield the first
    substantial line. Error results and loop-guard blocks are skipped.
    Reports ONLY what the tool result literally contained (same discipline
    as selfheal.harvest_findings). Never raises.
    """
    out = []
    try:
        for m in reversed(messages or []):
            if not isinstance(m, dict) or m.get("role") != "tool":
                continue
            c = _content_text(m.get("content")).strip()
            if not c:
                continue
            # Peel the untrusted-tool-result envelope + boilerplate first so a
            # web result parses as JSON (and the prose fallback never surfaces
            # the raw "<untrusted_tool_result …>" tag as a finding).
            c = strip_tool_result_wrapper(c)
            if not c:
                continue
            low = c.lower()
            if any(mk in low for mk in _GUARD_ERR_MARKERS):
                continue
            line = ""
            try:
                data = _loads_lenient(c)
                if isinstance(data, dict):
                    if data.get("error"):
                        continue
                    web = (data.get("data") or {}).get("web")
                    if isinstance(web, list) and web and isinstance(web[0], dict):
                        r = web[0]
                        line = "%s — %s" % (str(r.get("title") or "").strip(),
                                            str(r.get("url") or "").strip())
            except Exception:
                pass
            if not line:
                first = next((ln.strip() for ln in c.splitlines() if ln.strip()), "")
                if len(first) < 20 or first in ("{", "["):
                    continue
                line = first
            line = line.strip(" —-")
            if line:
                out.append(line[:200])
            if len(out) >= limit:
                break
    except Exception:
        return []
    return list(reversed(out))


def _finding_hash(text):
    """Stable content key for new-vs-seen dedup (casefold + ws-collapse)."""
    norm = " ".join(str(text or "").casefold().split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def new_findings(findings, reported):
    """The subset of *findings* whose content hash is not in *reported* (a set
    of hashes). Preserves order, de-dups within the batch too."""
    seen = set(reported or ())
    out = []
    for f in findings or []:
        h = _finding_hash(f)
        if h in seen:
            continue
        seen.add(h)
        out.append(f)
    return out


def should_send(state, now, api_call_count, new_count, cfg):
    """Trigger/throttle decision. Returns (send: bool, reason: str).

    Pure over the per-turn *state* dict + the current signals; no I/O.
    * requires >= cfg["min_new_findings"] genuinely new findings,
    * caps at cfg["max_per_turn"] interims,
    * first interim: wall >= first_delay_secs OR api_calls >= first_steps,
    * later interims: BOTH a min time gap AND a min step gap since the last.
    """
    try:
        if new_count < max(1, int(cfg["min_new_findings"])):
            return False, "no-new-content"
        if state["sent_count"] >= int(cfg["max_per_turn"]):
            return False, "cap"
        wall = now - float(state["first_ts"])
        if state["sent_count"] == 0:
            if (wall >= float(cfg["first_delay_secs"])
                    or int(api_call_count or 0) >= int(cfg["first_steps"])):
                return True, "first"
            return False, "warming"
        gap_ok = (now - float(state["last_sent_ts"])) >= float(cfg["throttle_secs"])
        step_ok = (int(api_call_count or 0) - int(state["last_sent_calls"])
                   ) >= int(cfg["throttle_steps"])
        if gap_ok and step_ok:
            return True, "throttle-ok"
        return False, "throttled"
    except Exception:
        return False, "error"


_STILL_WORKING_HEAD = ("⏳ Still working on your question — this is a progress "
                       "update, not the final answer.")
_STILL_WORKING_TAIL = ("Still checking… I'll send the complete answer when "
                       "it's ready.")


def build_message(findings, cfg):
    """Compose the interim message from the NEW findings only. Honest and
    explicitly marked as non-final. Returns "" when there is nothing to say."""
    fs = [str(f).strip() for f in (findings or []) if str(f).strip()]
    fs = fs[:max(1, int(cfg.get("max_findings_per_msg", 3)))]
    if not fs:
        return ""
    lines = [_STILL_WORKING_HEAD, "", "So far I've found:"]
    lines.extend("• %s" % f for f in fs)
    lines.append("")
    lines.append(_STILL_WORKING_TAIL)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Delivery (captured-adapter path — same as selfheal._deliver_text)
# ---------------------------------------------------------------------------

def _gw_get(obj, name):
    """Guarded private-symbol access: missing symbol logs one warning and
    returns None — the caller then skips delivery (feature self-disables)."""
    fn = getattr(obj, name, None)
    if not callable(fn):
        _warn_once("missing:" + name,
                   "progress: gateway symbol %r missing/changed — interim "
                   "delivery disabled", name)
        return None
    return fn


def _resolve_source(gw, session_id):
    """Best-effort SessionSource for *session_id*, from the captured per-turn
    sources; falls back to the live session store to map session_id -> key
    (mirrors selfheal's resolution so a rotated/fresh id still resolves)."""
    for v in list(_sessions.values()):
        if (v or {}).get("session_id") == session_id and v.get("source") is not None:
            return v["source"]
    try:
        entries = getattr(getattr(gw, "session_store", None), "_entries", None)
        if isinstance(entries, dict):
            for k, e in list(entries.items()):
                if str(getattr(e, "session_id", "") or "") == session_id:
                    info = _sessions.get(k)
                    if info and info.get("source") is not None:
                        return info["source"]
    except Exception:
        pass
    return None


async def _deliver_async(gw, source, text):
    """Deliver *text* via the platform adapter for *source*. Never raises."""
    try:
        afs = _gw_get(gw, "_adapter_for_source")
        adapter = afs(source) if afs else None
        if adapter is None or not hasattr(adapter, "send"):
            _warn_once("no-adapter",
                       "progress: no adapter for source — cannot deliver")
            return
        metadata = None
        tid = getattr(source, "thread_id", None)
        if tid:
            metadata = {"thread_id": tid}
        await adapter.send(chat_id=str(getattr(source, "chat_id", "") or ""),
                           content=text, metadata=metadata)
    except Exception:
        logger.debug("progress: adapter delivery failed", exc_info=True)


def _platform_ok(source, cfg):
    plats = cfg.get("platforms") or []
    if not plats:
        return True
    pv = str(getattr(getattr(source, "platform", None), "value", "") or "").lower()
    return pv in plats


def _fire_and_forget(gw, source, text):
    """Schedule the async send on the captured loop and return immediately.
    Fire-and-forget — the future is never awaited, so the turn is never
    blocked or slowed by a slow/failing send."""
    loop = _gw.get("loop")
    if loop is None:
        _warn_once("no-loop", "progress: no captured event loop — cannot "
                   "deliver interim message")
        return False
    try:
        asyncio.run_coroutine_threadsafe(_deliver_async(gw, source, text), loop)
        return True
    except Exception:
        logger.debug("progress: could not schedule interim delivery",
                     exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Hooks / middleware
# ---------------------------------------------------------------------------

def _pg_gateway_capture(event=None, gateway=None, session_store=None, **_kw):
    """pre_gateway_dispatch: capture the gateway weakref + running loop + the
    per-session_key SessionSource (everything delivery needs). Never
    influences dispatch (always returns None)."""
    try:
        if _cfg()["enabled"] != "on":
            return None
        now = time.time()
        if gateway is not None:
            try:
                _gw["ref"] = weakref.ref(gateway)
                _gw["ts"] = now
            except Exception:
                pass
            try:
                _gw["loop"] = asyncio.get_running_loop()
            except Exception:
                pass
        src = getattr(event, "source", None)
        if src is None or session_store is None:
            return None
        sk = None
        try:
            gen = getattr(session_store, "_generate_session_key", None)
            if callable(gen):
                sk = gen(src)
        except Exception:
            sk = None
        if not sk:
            return None
        sid = ""
        try:
            entries = getattr(session_store, "_entries", None)
            e = entries.get(sk) if isinstance(entries, dict) else None
            sid = str(getattr(e, "session_id", "") or "")
        except Exception:
            pass
        _sessions[sk] = {"source": src, "session_id": sid, "ts": now}
        _prune(_sessions, now)
    except Exception:
        logger.debug("progress: gateway capture failed", exc_info=True)
    return None


_REVIEW_HARNESS_PREFIX = "Review the conversation above"


def _in_background_review(messages=None):
    """True when this call is running inside hermes's background memory/skill
    review (agent/background_review.py). Those run the full agent loop — with
    our llm_request middleware active — in a daemon thread, under a hard
    tool-whitelist, on a FORKED session that is NEVER delivered to the user.
    Any interim we send there leaks internal curator chatter (and raw tool
    schemas) into the user's chat. Detected three independent ways so a change
    to any one still trips the guard; fail-safe (returns False on error, which
    preserves prior behaviour)."""
    try:
        if str(getattr(threading.current_thread(), "name", "")
               ).startswith("bg-review"):
            return True
    except Exception:
        pass
    try:  # thread-local tool whitelist is set for the duration of the review
        from hermes_cli import plugins as _hp
        if getattr(getattr(_hp, "_thread_tool_whitelist", None),
                   "allowed", None) is not None:
            return True
    except Exception:
        pass
    try:  # the review harness prompt itself, replayed as a user/system turn
        for m in (messages or []):
            if isinstance(m, dict) and m.get("role") in ("user", "system"):
                c = m.get("content")
                if isinstance(c, str) \
                        and c.lstrip().startswith(_REVIEW_HARNESS_PREFIX):
                    return True
    except Exception:
        pass
    return False


def _pg_middleware(request=None, api_mode="", base_url="", session_id="",
                   turn_id="", api_call_count=0, platform="", **_kw):
    """llm_request: the trigger + harvest surface. READ-ONLY over *request*
    (never mutates it — selfheal owns the mutations and runs before us).
    Harvests a findings digest from the conversation's tool results and, when
    the trigger/throttle policy fires and there is genuinely new content,
    fire-and-forgets an interim message via the captured adapter. Returns
    None always (this middleware never changes the outgoing request)."""
    try:
        cfg = _cfg()
        if cfg["enabled"] != "on":
            return None
        if not isinstance(request, dict) or not session_id:
            return None
        # Never emit interims during hermes's background memory/skill review —
        # it runs our middleware on a forked, never-delivered session and would
        # otherwise leak curator chatter + raw tool schemas into the user's chat.
        if _in_background_review(request.get("messages")):
            _warn_once("bg-review-" + (session_id or "?"),
                       "progress: suppressing interims during background "
                       "review session=%s", session_id or "-")
            return None
        # Cheap early gate: if the dispatch platform is known and not on the
        # allowlist, skip before any harvesting work.
        if cfg.get("platforms"):
            pv = str(platform or "").strip().lower()
            if pv and pv not in cfg["platforms"]:
                return None
        gw = _gw["ref"]() if _gw.get("ref") else None
        if gw is None:
            return None
        now = time.time()
        st = _turn_state(session_id, turn_id, now)

        msgs = request.get("messages")
        if not isinstance(msgs, list):
            return None
        findings = harvest_findings(
            msgs, limit=max(1, int(cfg["max_findings_per_msg"])))
        fresh = new_findings(findings, st["reported"])

        send, reason = should_send(st, now, api_call_count, len(fresh), cfg)
        if not send:
            return None

        source = _resolve_source(gw, session_id)
        if source is None:
            _warn_once("no-source-" + session_id,
                       "progress: no captured source for session %s — cannot "
                       "deliver interim", session_id)
            return None
        if not _platform_ok(source, cfg):
            return None

        text = build_message(fresh, cfg)
        if not text:
            return None
        if not _fire_and_forget(gw, source, text):
            return None

        # Mark sent AFTER a successful schedule so a scheduling failure
        # doesn't consume the budget or suppress these findings next time.
        for f in fresh:
            st["reported"].add(_finding_hash(f))
        st["sent_count"] += 1
        st["last_sent_ts"] = now
        st["last_sent_calls"] = int(api_call_count or 0)
        logger.info("progress: interim #%d sent (%s) session=%s turn=%s "
                    "new_findings=%d", st["sent_count"], reason,
                    session_id or "-", (turn_id or "-")[-12:], len(fresh))
    except Exception:
        logger.debug("progress: middleware failed; turn unaffected",
                     exc_info=True)
    return None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

for _cb in (_pg_middleware, _pg_gateway_capture):
    _cb._router_progress = True  # dedup marker + verify.py detection
del _cb


def register(ctx, host=None):
    """Register the progress surfaces (llm_request middleware +
    pre_gateway_dispatch capture). Idempotent on force rescans; never raises.
    *host* is accepted for symmetry with selfheal but unused (this feature
    harvests from the request messages directly)."""
    try:
        if not hasattr(ctx, "register_hook") or not hasattr(ctx, "register_middleware"):
            logger.warning("progress: PluginContext lacks register_hook/"
                           "register_middleware; progress NOT installed")
            return
        try:  # dedup: a force rescan re-runs register() on the same manager
            hooks = ctx._manager._hooks.get("pre_gateway_dispatch", [])
            if any(getattr(cb, "_router_progress", False) for cb in hooks):
                return
        except Exception:
            pass  # private layout changed — worst case a redundant copy
        # Middleware registered LAST (after selfheal): read-only, so it must
        # observe the request AFTER any healer mutation, never before.
        ctx.register_middleware("llm_request", _pg_middleware)
        ctx.register_hook("pre_gateway_dispatch", _pg_gateway_capture)
        cfg = _cfg()
        logger.info("progress: registered (enabled=%s platforms=%s "
                    "first=%ss/%dcalls throttle=%ss/%dcalls max_per_turn=%d)",
                    cfg["enabled"], ",".join(cfg["platforms"]) or "all",
                    cfg["first_delay_secs"], cfg["first_steps"],
                    cfg["throttle_secs"], cfg["throttle_steps"],
                    cfg["max_per_turn"])
    except Exception:
        logger.warning("progress: failed to install; feature inactive "
                       "(normal operation unaffected)", exc_info=True)
