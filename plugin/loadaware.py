# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Podwarden Inc

"""loadaware — latency-inferred server-load awareness for the router plugin
(v1.8.0; master flag ``tools.tool_search.loadaware.enabled``, default on).

PROBLEM (2026-07-19 "outage" post-mortem): local-27b runs on vllm-local,
a SHARED, often-saturated box. When other workloads pin the GPU the server is
still HEALTHY and generating (e.g. 121 tok/s) but incoming requests sit behind
a deep queue ("Running: 2, Waiting: 14"). hermes's streaming socket read
timeout (``HERMES_STREAM_READ_TIMEOUT`` = 120 s on a non-local host) then fires
while the server is merely busy, hermes RETRIES (``agent.api_max_retries`` = 3,
plus the nested ``HERMES_STREAM_RETRIES`` = 2), and the timed-out requests keep
running server-side — so the retries DEEPEN the queue (congestion collapse).
The misdiagnosis at the time was "EngineCore crash"; it was saturation.

WHAT THIS DOES: infer server state from observed per-call latency + timeout
events and (a) tell the user honestly when the server is slow-but-working so a
long wait isn't mistaken for a hang, (b) tell the user honestly when the server
looks truly unresponsive instead of silently burning the turn, and (c) NEVER
amplify — loadaware issues no LLM calls; its only side effect is a lightweight
platform message. The BIG levers (raise the timeout so a slow-but-working
server isn't abandoned at 120 s; cut the retry count so client timeouts stop
orphaning server-side requests) are CONFIG-ONLY on this hermes build — see the
"WHAT WE CANNOT DO FROM A PLUGIN" note below and the README.

OBSERVABILITY (verified in site-packages 0.18.2):

* ``post_api_request`` (agent/conversation_loop.py:4261) fires once per
  SUCCESSFUL API call with ``api_duration`` (elapsed seconds), ``finish_reason``,
  ``session_id``, ``platform``, ``base_url`` — the clean success-latency source.
* ``api_request_error`` (run_agent.py:2418) fires per FAILED attempt, including
  timeouts, with ``api_duration``, ``error={type,message}``, ``status_code``,
  ``retry_count``, ``retryable`` — the exception-carrying source.
* The ``llm_request`` middleware fires BEFORE each call (no timing) but is the
  only surface that carries live ``session_id`` + ``platform`` at call time —
  so loadaware maintains the load window in the two post-call hooks and ACTS
  (delivers the honest note) from the middleware, right before the next call.

WHAT WE CANNOT DO FROM A PLUGIN (investigated; hence the config levers):

* Extend the per-request timeout: the streaming path rebuilds
  ``httpx.Timeout`` from provider/env config AFTER ``**api_kwargs``
  (chat_completion_helpers.py:2084-2092), discarding any middleware-set
  ``timeout``. Fix via ``providers.<id>.request_timeout_seconds`` (config).
* Suppress retries for the turn: the retry loop is hermes-internal
  (conversation_loop.py:1100, ``agent._api_max_retries``); ``api_request_error``
  is observer-only. Fix via ``agent.api_max_retries`` (config) and the
  ``HERMES_STREAM_RETRIES`` env var for the nested stream retries.

STATE CLASSIFIER (pure, unit-tested): over a process-global rolling window of
recent (latency, outcome) events, restricted to the vLLM allowlist host:

* HEALTHY — latencies normal, no recent timeouts.
* SLOW    — latencies elevated / climbing, but requests are still COMPLETING
            (server saturated → be patient, tell the user).
* STALLED — a trailing run of timeouts with no completions (possibly wedged →
            tell the user honestly, do not keep hammering).

DELIVERY reuses progress.py's captured-adapter path (the same gateway weakref +
event loop + per-session SessionSource it already captures) — loadaware does
NOT register its own capture and does NOT duplicate progress's findings digest;
it coordinates timing so a load note never stacks on a just-sent progress
interim. Every callback is fail-safe: it returns None on any error, delivery is
fire-and-forget, and a missing/renamed symbol disables only the note. Normal
operation is never affected (worst case == today's behavior).

Config (all under ``tools.tool_search.loadaware``, read per call):

    enabled: "on"            master switch ("off" = pure no-op)
    platforms: [telegram]    note-delivery allowlist (empty = all)
    window: 12               max recent events kept in the global window
    window_secs: 600         ignore events older than this (staleness)
    min_samples: 3           need >= this many recent events to leave HEALTHY
    slow_latency_secs: 20    a completed call slower than this counts as slow
    slow_frac: 0.5           fraction of recent completions slow -> SLOW
    climb_ratio: 1.5         newer-half/older-half latency ratio = "climbing"
    stalled_streak: 2        trailing timeout events (no completion) -> STALLED
    note_throttle_secs: 45   min seconds between load notes per session
    max_notes_per_turn: 2    cap load notes per turn
    coord_secs: 12           skip a note if progress sent an interim this recently

Registered from plugin/__init__.py::register() AFTER progress (its middleware
is read-only, like progress's, so ordering is immaterial; it sits last).
"""
import logging
import time
from collections import deque

logger = logging.getLogger("hermes.plugins.router.loadaware")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULTS = {
    "enabled": "on",
    "platforms": ["telegram"],
    "window": 12,
    "window_secs": 600,
    "min_samples": 3,
    "slow_latency_secs": 20,
    "slow_frac": 0.5,
    "climb_ratio": 1.5,
    "stalled_streak": 2,
    "note_throttle_secs": 45,
    "max_notes_per_turn": 2,
    "coord_secs": 12,
}

_FLAG_KEYS = ("enabled",)
_INT_KEYS = ("window", "window_secs", "min_samples", "slow_latency_secs",
             "stalled_streak", "note_throttle_secs", "max_notes_per_turn",
             "coord_secs")
_FLOAT_KEYS = ("slow_frac", "climb_ratio")

# The vLLM allowlist host(s): the window must reflect the shared, saturated
# server only — a fallback/other provider must never pollute it. Read from the
# same config key the constrained-decoding feature uses; default matches the
# host plugin's CONSTRAINED_HOSTS_DEFAULT.
_HOSTS_DEFAULT = ["vllm.example.com"]


def _norm_flag(v, default):
    if isinstance(v, bool):
        return "on" if v else "off"
    s = str(v).strip().lower()
    return s if s in ("on", "off") else default


def _load_config():
    try:
        from hermes_cli.config import load_config_readonly as _load
    except ImportError:
        try:
            from hermes_cli.config import load_config as _load
        except Exception:
            return {}
    except Exception:
        return {}
    try:
        return _load() or {}
    except Exception:
        return {}


def _cfg():
    """Effective loadaware config (DEFAULTS overlaid with the config file).

    Read per call like every other router knob — a config flip needs no
    restart. Invalid values fall back to the default; never raises.
    """
    out = dict(DEFAULTS)
    out["platforms"] = list(DEFAULTS["platforms"])
    try:
        la = (((_load_config().get("tools") or {}).get("tool_search") or {})
              .get("loadaware") or {})
        if isinstance(la, dict):
            for k in _FLAG_KEYS:
                if k in la and la[k] is not None:
                    out[k] = _norm_flag(la[k], out[k])
            for k in _INT_KEYS:
                if k in la and la[k] is not None and not isinstance(la[k], bool):
                    try:
                        out[k] = int(la[k])
                    except Exception:
                        pass
            for k in _FLOAT_KEYS:
                if k in la and la[k] is not None and not isinstance(la[k], bool):
                    try:
                        out[k] = float(la[k])
                    except Exception:
                        pass
            if isinstance(la.get("platforms"), list):
                out["platforms"] = [str(p).strip().lower()
                                    for p in la["platforms"] if str(p).strip()]
    except Exception:
        pass
    return out


def _allow_hosts():
    """The vLLM host allowlist (tools.tool_search.constrained_hosts) — the
    window only records events for these hosts. Never raises."""
    try:
        ts = (_load_config().get("tools") or {}).get("tool_search") or {}
        raw = ts.get("constrained_hosts")
        if isinstance(raw, list) and raw:
            return [str(h).strip().lower() for h in raw if str(h).strip()]
    except Exception:
        pass
    return list(_HOSTS_DEFAULT)


def _host_ok(base_url):
    try:
        from urllib.parse import urlsplit
        host = (urlsplit(base_url or "").hostname or "").lower()
        return bool(host) and host in _allow_hosts()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# State (process-global rolling window + per-session note bookkeeping)
# ---------------------------------------------------------------------------

_TABLE_MAX = 64
_IDLE_S = 3600
_EVENTS_MAX = 128  # hard cap on the deque; the window/window_secs config trims

_events = deque(maxlen=_EVENTS_MAX)  # process-global: [{ts, latency, outcome}]
_state = {"state": "HEALTHY", "detail": "", "ts": 0.0}
_notes = {}      # session_id -> {"turn_id","count","last_ts","ts"}
_warned = set()


def _warn_once(key, msg, *args):
    if key in _warned:
        return
    # v1.8.6: one key is per-session ("no-src-<sid>"); cap the set so a very
    # long-lived process can't grow it unboundedly (rare path, warn-dedup only).
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


def _reset_state():
    """Test helper: wipe all module state."""
    _events.clear()
    _state.update({"state": "HEALTHY", "detail": "", "ts": 0.0})
    _notes.clear()
    _warned.clear()


# ---------------------------------------------------------------------------
# Pure functions (unit-testable without hermes)
# ---------------------------------------------------------------------------

_TIMEOUT_MARKERS = ("timeout", "timedout", "timed out")


def is_timeout_error(error_type, error_message="", status_code=None):
    """True when an api_request_error looks like a client-side TIMEOUT (the
    congestion-collapse signal) rather than a plain server error (e.g. 500).
    Pure; never raises."""
    try:
        t = str(error_type or "").lower()
        m = str(error_message or "").lower()
        if any(mk in t for mk in _TIMEOUT_MARKERS):
            return True
        if any(mk in m for mk in _TIMEOUT_MARKERS):
            return True
    except Exception:
        pass
    return False


def _mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return (sum(xs) / len(xs)) if xs else 0.0


def _median(xs):
    xs = sorted(x for x in xs if isinstance(x, (int, float)))
    if not xs:
        return 0.0
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def classify_load(events, cfg, now):
    """Pure classifier: (state, detail) over the recent event window.

    *events* is an iterable of dicts ``{"ts","latency","outcome"}`` where
    outcome is "ok" | "timeout" | "error". Precedence STALLED > SLOW > HEALTHY.
    Recovers to HEALTHY when the window clears (not monotonic — server load is
    a live, process-global property).
    """
    try:
        wsecs = float(cfg.get("window_secs") or 600)
        win = int(cfg.get("window") or 12)
        recent = [e for e in (events or [])
                  if isinstance(e, dict) and (now - float(e.get("ts") or 0)) <= wsecs]
        recent = recent[-win:]
        if len(recent) < int(cfg.get("min_samples") or 3):
            return "HEALTHY", "n=%d<min" % len(recent)

        outcomes = [str(e.get("outcome") or "") for e in recent]
        oks = [e for e in recent if e.get("outcome") == "ok"]
        timeouts = [e for e in recent if e.get("outcome") == "timeout"]

        # STALLED: a trailing run of >= stalled_streak failures, timeout-present,
        # with NO completion in that tail — requests dying with nothing landing.
        streak = max(1, int(cfg.get("stalled_streak") or 2))
        tail = recent[-streak:]
        if (len(tail) >= streak
                and all(e.get("outcome") in ("timeout", "error") for e in tail)
                and any(e.get("outcome") == "timeout" for e in tail)):
            return "STALLED", "tail=%s" % ",".join(o[:2] for o in outcomes[-streak:])

        # SLOW signals (server saturated but still completing).
        slow_secs = float(cfg.get("slow_latency_secs") or 20)
        lats = [float(e.get("latency") or 0) for e in oks]
        reasons = []
        if oks:
            n_slow = sum(1 for x in lats if x >= slow_secs)
            frac = n_slow / len(oks)
            med = _median(lats)
            if frac >= float(cfg.get("slow_frac") or 0.5) or med >= slow_secs:
                reasons.append("lat med=%.0fs slow=%d/%d" % (med, n_slow, len(oks)))
            # climbing trend: newer half meaningfully slower than older half
            if len(lats) >= 4:
                half = len(lats) // 2
                older, newer = _mean(lats[:half]), _mean(lats[half:])
                if (older > 0 and newer >= older * float(cfg.get("climb_ratio") or 1.5)
                        and newer >= slow_secs * 0.6):
                    reasons.append("climb %.0f->%.0fs" % (older, newer))
        # some completing, some timing out == saturated (queue partially served)
        if timeouts and oks:
            reasons.append("mixed to=%d ok=%d" % (len(timeouts), len(oks)))
        # isolated timeout(s) below the STALLED streak still means "busy"
        elif timeouts:
            reasons.append("timeouts=%d" % len(timeouts))
        if reasons:
            return "SLOW", "; ".join(reasons)
        return "HEALTHY", "med=%.0fs n=%d" % (_median(lats), len(recent))
    except Exception:
        return "HEALTHY", "error"


_SLOW_NOTE = ("⏳ The model server is under heavy load right now — this is "
              "taking longer than usual, but it's still working. I'll keep "
              "going and send the answer when it's ready.")

_STALLED_NOTE = ("⚠️ The model server looks unresponsive at the moment "
                 "(requests are timing out). Rather than keep hammering it, "
                 "I'm stopping here — please try again in a little while.")


def build_slow_note():
    return _SLOW_NOTE


def build_stalled_note():
    return _STALLED_NOTE


def should_note(state, note_rec, now, turn_id, cfg):
    """Throttle/cap decision for a load note. Pure over the per-session
    *note_rec* dict (or None). Returns (send: bool, reason: str)."""
    try:
        if state not in ("SLOW", "STALLED"):
            return False, "healthy"
        rec = note_rec or {}
        same_turn = bool(turn_id) and rec.get("turn_id") == turn_id
        count = int(rec.get("count") or 0) if same_turn else 0
        if count >= int(cfg.get("max_notes_per_turn") or 2):
            return False, "cap"
        gap = now - float(rec.get("last_ts") or 0)
        if gap < float(cfg.get("note_throttle_secs") or 45):
            return False, "throttled"
        return True, "ok"
    except Exception:
        return False, "error"


# ---------------------------------------------------------------------------
# Window maintenance (called from the two post-call observer hooks)
# ---------------------------------------------------------------------------

def _record(outcome, latency, now, cfg):
    """Append an event and recompute the global load state. Never raises."""
    try:
        _events.append({"ts": now, "latency": float(latency or 0),
                        "outcome": outcome})
        prev = _state.get("state")
        new, detail = classify_load(_events, cfg, now)
        _state.update({"state": new, "detail": detail, "ts": now})
        if new != prev:
            logger.info("loadaware: %s->%s (%s) [%s/%s ok/timeout in window]",
                        prev, new, detail,
                        sum(1 for e in _events if e.get("outcome") == "ok"),
                        sum(1 for e in _events if e.get("outcome") == "timeout"))
    except Exception:
        logger.debug("loadaware: record failed", exc_info=True)


# ---------------------------------------------------------------------------
# Delivery (REUSE progress.py's captured-adapter path — no duplicate capture)
# ---------------------------------------------------------------------------

def _progress_mod():
    """The sibling progress module (holds the captured gateway ref/loop/
    sources). Guarded: returns None if unavailable — the note is then skipped
    and the classifier/logging still work."""
    try:
        from . import progress as _pg  # normal package context
        return _pg
    except Exception:
        pass
    try:
        import sys
        for name in ("router_plugin.progress",
                     "hermes_plugins_router_progress"):
            m = sys.modules.get(name)
            if m is not None:
                return m
    except Exception:
        pass
    _warn_once("no-progress",
               "loadaware: progress module unavailable — load notes disabled "
               "(classifier still active)")
    return None


def _progress_recent_send(pg, session_id, turn_id, now, coord_secs):
    """True when progress delivered an interim for this turn within
    *coord_secs* — we then suppress our note to avoid stacking two messages."""
    try:
        turns = getattr(pg, "_turns", None)
        keyfn = getattr(pg, "_turn_key", None)
        if not isinstance(turns, dict) or not callable(keyfn):
            return False
        st = turns.get(keyfn(session_id, turn_id))
        if not isinstance(st, dict):
            return False
        last = float(st.get("last_sent_ts") or 0)
        return last > 0 and (now - last) < float(coord_secs)
    except Exception:
        return False


def _deliver(pg, session_id, text):
    """Fire-and-forget delivery through progress's captured adapter. Returns
    True when scheduled. Never raises; never awaited (cannot slow the turn)."""
    try:
        gwref = getattr(pg, "_gw", None)
        ref = (gwref or {}).get("ref") if isinstance(gwref, dict) else None
        gw = ref() if callable(ref) else None
        if gw is None:
            _warn_once("no-gw", "loadaware: no captured gateway — cannot "
                       "deliver load note")
            return False
        resolve = getattr(pg, "_resolve_source", None)
        fire = getattr(pg, "_fire_and_forget", None)
        if not callable(resolve) or not callable(fire):
            _warn_once("no-send", "loadaware: progress send helpers "
                       "missing/changed — load notes disabled")
            return False
        source = resolve(gw, session_id)
        if source is None:
            _warn_once("no-src-" + session_id,
                       "loadaware: no captured source for session %s — cannot "
                       "deliver load note", session_id)
            return False
        return bool(fire(gw, source, text))
    except Exception:
        logger.debug("loadaware: delivery failed", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Hooks / middleware
# ---------------------------------------------------------------------------

def _la_post_api_request(session_id="", platform="", base_url="",
                         api_duration=0.0, finish_reason="", **_kw):
    """post_api_request: record a successful call's latency into the global
    window. Observer only; never influences anything."""
    try:
        cfg = _cfg()
        if cfg["enabled"] != "on" or not _host_ok(base_url):
            return None
        _record("ok", api_duration, time.time(), cfg)
    except Exception:
        logger.debug("loadaware: post_api_request failed", exc_info=True)
    return None


def _la_api_request_error(session_id="", platform="", base_url="",
                          api_duration=0.0, status_code=None, error=None,
                          retry_count=None, retryable=None, **_kw):
    """api_request_error: record a failed attempt (timeout vs other error)
    into the global window. Observer only; never influences retry (it can't —
    the retry loop is hermes-internal)."""
    try:
        cfg = _cfg()
        if cfg["enabled"] != "on" or not _host_ok(base_url):
            return None
        et = (error or {}).get("type") if isinstance(error, dict) else None
        em = (error or {}).get("message") if isinstance(error, dict) else None
        outcome = "timeout" if is_timeout_error(et, em, status_code) else "error"
        _record(outcome, api_duration, time.time(), cfg)
    except Exception:
        logger.debug("loadaware: api_request_error failed", exc_info=True)
    return None


def _la_middleware(request=None, api_mode="", base_url="", session_id="",
                   turn_id="", api_call_count=0, platform="", **_kw):
    """llm_request: READ-ONLY. Never mutates *request* (it couldn't extend the
    timeout anyway — the streaming path rebuilds it from config). Reads the
    current global load state (maintained by the two observer hooks) and, when
    SLOW/STALLED, delivers ONE honest, throttled, progress-coordinated note.
    Always returns None."""
    try:
        cfg = _cfg()
        if cfg["enabled"] != "on":
            return None
        if not session_id or not _host_ok(base_url):
            return None
        # platform allowlist (cheap gate before any delivery work)
        plats = cfg.get("platforms") or []
        if plats:
            pv = str(platform or "").strip().lower()
            if pv and pv not in plats:
                return None
        now = time.time()
        state = _state.get("state", "HEALTHY")
        # Staleness guard: if the window hasn't been updated within window_secs
        # (e.g. the first call after a long idle gap, before this call's own
        # post_api_request fires), don't act on a stale SLOW/STALLED verdict.
        if (now - float(_state.get("ts") or 0)) > float(cfg.get("window_secs") or 600):
            return None
        if state == "HEALTHY":
            return None
        rec = _notes.get(session_id)
        send, reason = should_note(state, rec, now, turn_id, cfg)
        if not send:
            return None
        pg = _progress_mod()
        if pg is None:
            return None
        # Coordinate: don't stack a load note on a just-sent progress interim.
        if _progress_recent_send(pg, session_id, turn_id, now,
                                 cfg.get("coord_secs") or 12):
            return None
        text = build_stalled_note() if state == "STALLED" else build_slow_note()
        if not _deliver(pg, session_id, text):
            return None
        same_turn = bool(turn_id) and (rec or {}).get("turn_id") == turn_id
        count = (int((rec or {}).get("count") or 0) + 1) if same_turn else 1
        _notes[session_id] = {"turn_id": turn_id or "", "count": count,
                              "last_ts": now, "ts": now}
        _prune(_notes, now)
        logger.info("loadaware: %s note sent session=%s turn=%s (%s)",
                    state.lower(), session_id or "-", (turn_id or "-")[-12:],
                    _state.get("detail", ""))
    except Exception:
        logger.debug("loadaware: middleware failed; turn unaffected",
                     exc_info=True)
    return None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

for _cb in (_la_middleware, _la_post_api_request, _la_api_request_error):
    _cb._router_loadaware = True  # dedup marker + verify.py detection
del _cb


def _observed_timeout_retry():
    """Best-effort snapshot of the CONFIG levers loadaware depends on (for a
    single informative log line at register): the provider request timeout and
    the outer retry count. Never raises."""
    info = {"provider": "", "request_timeout_seconds": None, "api_max_retries": None}
    try:
        cfg = _load_config()
        model = cfg.get("model") or {}
        pid = str(model.get("provider") or "")
        info["provider"] = pid
        prov = ((cfg.get("providers") or {}).get(pid) or {}) if pid else {}
        if isinstance(prov, dict):
            info["request_timeout_seconds"] = prov.get("request_timeout_seconds")
        info["api_max_retries"] = (cfg.get("agent") or {}).get("api_max_retries")
    except Exception:
        pass
    return info


def register(ctx, host=None):
    """Register the loadaware surfaces (post_api_request + api_request_error
    observers + a read-only llm_request middleware that delivers the honest
    note). Idempotent on force rescans; never raises. *host* is accepted for
    symmetry with selfheal/progress but unused."""
    try:
        if not hasattr(ctx, "register_hook") or not hasattr(ctx, "register_middleware"):
            logger.warning("loadaware: PluginContext lacks register_hook/"
                           "register_middleware; loadaware NOT installed")
            return
        try:  # dedup: a force rescan re-runs register() on the same manager
            hooks = ctx._manager._hooks.get("post_api_request", [])
            if any(getattr(cb, "_router_loadaware", False) for cb in hooks):
                return
        except Exception:
            pass  # private layout changed — worst case a redundant copy
        ctx.register_hook("post_api_request", _la_post_api_request)
        ctx.register_hook("api_request_error", _la_api_request_error)
        # Read-only middleware registered last (after progress); order among
        # read-only middlewares is immaterial.
        ctx.register_middleware("llm_request", _la_middleware)
        cfg = _cfg()
        tr = _observed_timeout_retry()
        logger.info("loadaware: registered (enabled=%s platforms=%s "
                    "window=%d/%ds slow>=%ds stalled_streak=%d) — config "
                    "levers: provider=%s request_timeout_seconds=%s "
                    "api_max_retries=%s", cfg["enabled"],
                    ",".join(cfg["platforms"]) or "all", cfg["window"],
                    cfg["window_secs"], cfg["slow_latency_secs"],
                    cfg["stalled_streak"], tr["provider"] or "-",
                    tr["request_timeout_seconds"], tr["api_max_retries"])
    except Exception:
        logger.warning("loadaware: failed to install; feature inactive "
                       "(normal operation unaffected)", exc_info=True)
