#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Podwarden Inc

"""Autonomous GATEWAY test harness for the hermes reliability loop.

WHY THIS EXISTS
---------------
The corpus harness drives ``hermes chat -q -Q`` one-shots. Those run the agent
through the *CLI* path and NEVER exercise the gateway/telegram dispatch path:
they skip the ``pre_gateway_dispatch`` plugin hook, the router's intermediate
PROGRESS messages, the selfheal proactive poison-reset / fresh-retry, and --
critically -- persistent multi-turn session accumulation on ONE session. Every
real user-facing failure happened on that gateway path and was invisible to the
one-shots.

WHAT THIS DOES
--------------
Drives the agent the way Telegram does, IN-PROCESS, with full fidelity:

  * Constructs a real ``gateway.run.GatewayRunner`` against a DEDICATED test
    HERMES_HOME (never production).
  * Loads the router plugin (pre_gateway_dispatch + constrained-decoding +
    selfheal + progress middleware) via the normal plugin manager.
  * Injects a ``CaptureAdapter`` (a real ``BasePlatformAdapter`` masquerading as
    Platform.TELEGRAM so the telegram-gated progress/selfheal features fire) and
    calls ``runner._handle_message(event)`` -- the exact function every platform
    adapter's message handler is wired to (run.py:6939 set_message_handler).
  * ``_handle_message`` fires ``pre_gateway_dispatch`` (run.py:8754), which is
    where progress.py/selfheal.py capture the live gateway weakref + running
    event loop + SessionSource. Progress + final delivery both go out through
    ``gateway._adapter_for_source(source).send(...)`` == our CaptureAdapter,
    so we capture the delivered answer AND every interim progress message.
  * Multi-turn: the SAME SessionSource across turns -> same session key -> the
    session store accumulates history on ONE persistent session (reproduces the
    poisoned-long-session failure mode).

Fidelity vs real Telegram: see scratchpad/gateway_harness_README.md.

USAGE
-----
  gateway_harness.py --home <testhome> ask "question"
  gateway_harness.py --home <testhome> convo "q1" "q2" "q3"
  gateway_harness.py --home <testhome> --seed seed.json ask "question"
      seed.json: [{"role":"user","content":"..."},
                  {"role":"assistant","content":"..."}, ...]
      Prior history is planted on the session BEFORE the real turn(s) so a
      degenerate/blank assistant history can arm the selfheal poison-reset.

Output: a JSON report on stdout. Per turn:
  {delivered_answer, progress_messages[], session_id, api_calls, wall_time,
   selfheal_actions[], guard_events[], poison_reset_fired}
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path


# --------------------------------------------------------------------------
# Environment bootstrap -- MUST run before importing gateway/hermes modules so
# HERMES_HOME + provider creds are in place.
# --------------------------------------------------------------------------
def _bootstrap_env(home: str) -> None:
    home = str(Path(home).resolve())
    os.environ["HERMES_HOME"] = home
    # Load the test home's .env (VLLM_API_KEY, SEARXNG_URL, ...).
    env_path = Path(home, ".env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    # Keep everything inside the test home; never phone production dirs.
    os.environ.setdefault("HERMES_NO_UPDATE_CHECK", "1")


# --------------------------------------------------------------------------
# Log capture -- the selfheal / router guards emit their decisions through the
# stdlib logging tree. We attach a handler for the duration of a turn and scan.
# --------------------------------------------------------------------------
class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        try:
            self.records.append(record.getMessage())
        except Exception:
            pass

    def reset(self):
        self.records = []


# Guard / self-heal event patterns (mirrors the corpus harness vocabulary,
# adapted to the gateway-side selfheal log strings in plugin/selfheal.py).
_GUARD_PATTERNS = {
    "steering": r"steering note injected|search steer",
    "dup_call_block": r"duplicate-call gate BLOCKED|duplicate call loop",
    "dup_query_block": r"duplicate-query gate BLOCKED|duplicate query loop",
    "search_hardcap": r"search hard cap",
    "empty_query_block": r"blocked empty web_search|empty/whitespace-only web_search",
    "repetition_guard": r"repetition-collapse guard|repetition guard",
    "intent_guard": r"intent-announcement",
    "constrained": r"constrained tool-call decoding|structural_tag",
    "clamp": r"clamped max_tokens|router plugin: clamped",
    "brand_nudge": r"brand nudge|Herm[eè]s",
    "coding_detected": r"coding turn detected",
}
_SELFHEAL_ACTION_RE = re.compile(r"selfheal:\s*action=(\w+)")
_SELFHEAL_STATE_RE = re.compile(r"selfheal:\s*(HEALTHY|WOBBLING|FAILING|DOOMED)->(HEALTHY|WOBBLING|FAILING|DOOMED)")
_POISON_RESET_RE = re.compile(
    r"action=fresh_retry|session poison high at turn start|reset_session|"
    r"proactive poison reset")  # v1.7.1 FIX 1 log string (was missed -> false negative)
_API_CALLS_RE = re.compile(r"api_calls?=(\d+)")


def _scan_records(records):
    guard_events = []
    for name, pat in _GUARD_PATTERNS.items():
        c = sum(1 for m in records if re.search(pat, m))
        if c:
            guard_events.append({"event": name, "count": c})
    selfheal_actions = []
    states = []
    for m in records:
        ma = _SELFHEAL_ACTION_RE.search(m)
        if ma:
            selfheal_actions.append(ma.group(1))
        ms = _SELFHEAL_STATE_RE.search(m)
        if ms:
            states.append(f"{ms.group(1)}->{ms.group(2)}")
    poison_reset_fired = any(_POISON_RESET_RE.search(m) for m in records)
    api_calls = None
    for m in records:
        mc = _API_CALLS_RE.search(m)
        if mc:
            api_calls = max(api_calls or 0, int(mc.group(1)))
    return {
        "guard_events": guard_events,
        "selfheal_actions": selfheal_actions,
        "selfheal_states": states,
        "poison_reset_fired": poison_reset_fired,
        "api_calls_from_log": api_calls,
    }


# --------------------------------------------------------------------------
# The capture adapter -- a real platform adapter that records outbound sends.
# --------------------------------------------------------------------------
def _build_capture_adapter_cls():
    from gateway.platforms.base import BasePlatformAdapter, SendResult

    class CaptureAdapter(BasePlatformAdapter):
        """Records every outbound send. No network. Masquerades as the
        configured platform so platform-gated features fire."""

        def __init__(self, config, platform):
            super().__init__(config, platform)
            self.sent = []          # list of (chat_id, content, metadata)
            self._send_seq = 0

        # --- streaming OFF so the final answer arrives as one plain send() ---
        def supports_draft_streaming(self, *a, **k) -> bool:
            return False

        def prefers_fresh_final_streaming(self, *a, **k) -> bool:
            return False

        async def connect(self, *, is_reconnect: bool = False) -> bool:
            self._running = True
            self._mark_connected()
            return True

        async def disconnect(self) -> None:
            self._running = False

        async def send(self, chat_id, content, metadata=None, **kwargs) -> "SendResult":
            self._send_seq += 1
            self.sent.append({
                "seq": self._send_seq,
                "chat_id": str(chat_id),
                "content": content if isinstance(content, str) else str(content),
                "metadata": dict(metadata) if isinstance(metadata, dict) else None,
                "ts": time.time(),
            })
            return SendResult(success=True, message_id=f"cap-{self._send_seq}")

        async def send_typing(self, chat_id, metadata=None) -> None:
            return None

        async def get_chat_info(self, chat_id):
            return {"chat_id": chat_id, "type": "dm"}

    return CaptureAdapter


# Progress messages carry this literal head (plugin/progress.py _STILL_WORKING_HEAD).
_PROGRESS_MARK = "⏳ Still working"


def _classify_sends(sent):
    """Split captured sends into interim progress messages vs the final answer."""
    progress = []
    finals = []
    for s in sent:
        c = s.get("content") or ""
        if c.startswith(_PROGRESS_MARK) or "this is a progress update, not the final answer" in c:
            progress.append(c)
        else:
            finals.append(c)
    delivered = finals[-1] if finals else None
    return delivered, progress, finals


# --------------------------------------------------------------------------
# Core: build the runner + a persistent source, run turns, capture.
# --------------------------------------------------------------------------
class GatewayProbe:
    def __init__(self, chat_id="99900001", user_id="770001", user_name="HarnessUser"):
        from gateway.run import GatewayRunner
        from gateway.config import Platform, PlatformConfig
        from gateway.session import SessionSource

        self.Platform = Platform
        self.SessionSource = SessionSource

        # Masquerade as telegram: progress.platforms defaults to [telegram] and
        # selfheal.platforms=[telegram,cli]. Using TELEGRAM makes both fire.
        self.platform = Platform.TELEGRAM

        self.runner = GatewayRunner()
        # Bypass user authorization: pre_gateway_dispatch fires only for
        # non-internal events, and non-internal events must pass authz. For a
        # synthetic harness user we authorize unconditionally.
        self.runner._is_user_authorized = lambda source: True  # type: ignore

        CaptureAdapter = _build_capture_adapter_cls()
        self.adapter = CaptureAdapter(PlatformConfig(enabled=True), self.platform)
        self.adapter.set_session_store(self.runner.session_store)
        self.adapter.set_message_handler(self.runner._handle_message)
        # Inject so _adapter_for_source(source) resolves to us.
        self.runner.adapters[self.platform] = self.adapter

        # Accurate per-turn API-call counter: register a lightweight
        # post_llm_call hook that increments a counter each model round-trip.
        # (invoke_hook fans out to every registered callback for the hook.)
        self._api_call_counter = {"n": 0}

        def _count_api_call(*a, **k):
            self._api_call_counter["n"] += 1
            return None

        try:
            from hermes_cli.plugins import get_plugin_manager
            mgr = get_plugin_manager()
            for _hk in ("post_llm_call", "pre_llm_call"):
                if _hk in getattr(mgr, "_hooks", {}):
                    mgr._hooks[_hk].append(_count_api_call)
                    self._api_count_hook = _hk
                    break
        except Exception:
            self._api_count_hook = None

        # ONE persistent source reused across turns => one accumulating session.
        self.source = SessionSource(
            platform=self.platform,
            chat_id=chat_id,
            chat_name="harness-dm",
            chat_type="dm",
            user_id=user_id,
            user_name=user_name,
        )

    def _resolve_session_id(self):
        store = self.runner.session_store
        try:
            key_fn = getattr(self.runner, "_session_key_for_source", None)
            if key_fn is None:
                key = store._generate_session_key(self.source)
            else:
                key = key_fn(self.source)
            peek = getattr(store, "peek_session_id", None)
            if callable(peek):
                return peek(key)
            store._ensure_loaded()
            entry = store._entries.get(key)
            return getattr(entry, "session_id", None) if entry else None
        except Exception:
            return None

    async def seed(self, seed_messages):
        """Plant prior conversation history on the session before real turns.

        Two fidelity requirements, both load-bearing for exercising the plugin's
        proactive poison-reset (pre_gateway_dispatch) end-to-end:

        1. TRANSCRIPT FIDELITY. Write to the SAME ``SessionDB`` the plugin reads
           (``session_store._db.get_messages``) and preserve ``tool_calls`` /
           ``tool_name`` / ``tool_call_id``. Dropping ``tool_calls`` turns every
           interim tool-calling assistant turn into what looks like a degenerate
           *final* answer, which inflates ``proactive_poison_score`` (measured:
           the real Silverton poison turn scores 0.625 with fidelity vs a bogus
           1.0 when tool_calls are stripped) and can false-positive clean
           sessions.

        2. ENTRY BOOKKEEPING SYNC. A raw transcript append leaves the
           SessionStore's in-memory ``SessionEntry`` desynced from a naturally
           accumulated one (``updated_at`` never re-bumped, ``last_prompt_tokens``
           stuck at 0, gateway session peer not re-registered). Under any
           non-``none`` reset policy that makes hermes's own
           ``get_or_create_session`` staleness check (``_should_reset`` ->
           ``end_session(..., "session_reset")``, gateway/session.py) treat the
           seeded session as idle/stale and reset it BEFORE the plugin ever
           reads the seeded history. Re-sync via the store's supported
           ``update_session()`` API (bumps ``updated_at`` to now, sets
           ``last_prompt_tokens`` > 0, re-registers the peer) so the seeded
           session is indistinguishable from a live accumulated one and only the
           plugin decides whether to reset. Never raises."""
        if not seed_messages:
            return {"seeded": 0, "note": "no seed"}
        store = self.runner.session_store
        try:
            sk = store._generate_session_key(self.source)
            entry = store.get_or_create_session(self.source)
            session_id = getattr(entry, "session_id", None) or self._resolve_session_id()
        except Exception as exc:
            return {"seeded": 0, "note": f"session setup failed: {exc}"}
        if session_id is None:
            return {"seeded": 0, "note": "no session_id; seed skipped"}
        # Append to the exact SessionDB the plugin's proactive reset reads from
        # (store._db); fall back to the runner's async wrapper's inner handle.
        db = getattr(store, "_db", None)
        if db is None or not hasattr(db, "append_message"):
            sdb = getattr(self.runner, "_session_db", None)
            db = getattr(sdb, "_db", sdb)
        if db is None or not hasattr(db, "append_message"):
            return {"seeded": 0, "note": "no SessionDB.append_message; seed skipped"}
        appended = 0
        approx_tokens = 0
        for msg in seed_messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            try:
                db.append_message(
                    session_id, role, content,
                    tool_calls=msg.get("tool_calls"),
                    tool_name=msg.get("tool_name"),
                    tool_call_id=msg.get("tool_call_id"),
                )
                appended += 1
                approx_tokens += max(1, len(str(content)) // 4)
            except TypeError:
                # Leaner append signature: role+content only.
                try:
                    db.append_message(session_id, role, content)
                    appended += 1
                    approx_tokens += max(1, len(str(content)) // 4)
                except Exception:
                    continue
            except Exception:
                continue
        # Re-sync the in-memory SessionStore entry to a naturally-accumulated
        # shape so hermes's staleness check will NOT reset it out from under the
        # plugin. Best effort — a store without update_session simply skips this.
        entry_synced = False
        try:
            upd = getattr(store, "update_session", None)
            if callable(upd) and appended:
                upd(sk, last_prompt_tokens=max(approx_tokens, 1))
                entry_synced = True
        except Exception:
            entry_synced = False
        return {"seeded": appended, "session_id": session_id, "session_key": sk,
                "entry_synced": entry_synced,
                "note": "transcript append (fidelity) + entry resync"
                        if appended else "no append performed"}

    async def _drain_tasks(self):
        """Await the background processing task(s) the adapter spawned, plus any
        follow-on tasks (progress sends, pending drains) until quiescent."""
        import asyncio as _a
        stable = 0
        while stable < 3:
            pending = [t for t in list(self.adapter._session_tasks.values()) if not t.done()]
            pending += [t for t in list(self.adapter._background_tasks) if not t.done()]
            if not pending:
                stable += 1
                await _a.sleep(0.1)
                continue
            stable = 0
            await _a.wait(pending, return_when=_a.ALL_COMPLETED)

    async def run_turn(self, text, per_turn_timeout=600):
        from gateway.platforms.base import MessageEvent, MessageType

        cap = _LogCapture()
        root = logging.getLogger()
        prev_level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(cap)
        self.adapter.sent = []
        self.adapter._send_seq = 0
        self._api_call_counter["n"] = 0

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=self.source,
            message_id=f"harness-{int(time.time()*1000)}",
        )
        t0 = time.time()
        err = None
        try:
            # Drive the REAL adapter entry point exactly as a platform poller
            # does: handle_message() spawns _process_message_background (which
            # calls _handle_message -> pre_gateway_dispatch, runs the agent,
            # and DELIVERS the final answer via adapter.send). We then await
            # the spawned background task(s) to completion.
            await self.adapter.handle_message(event)
            await asyncio.wait_for(self._drain_tasks(), timeout=per_turn_timeout)
        except asyncio.TimeoutError:
            err = f"TIMEOUT after {per_turn_timeout}s"
        except Exception as e:
            import traceback
            err = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}"
        # Let the loop flush any fire-and-forget progress sends.
        for _ in range(6):
            await asyncio.sleep(0.15)
        wall = round(time.time() - t0, 1)

        root.removeHandler(cap)
        root.setLevel(prev_level)

        delivered, progress, finals = _classify_sends(self.adapter.sent)
        scan = _scan_records(cap.records)
        sid = self._resolve_session_id()

        # Multi-topic (v1.12.0): surface the topic badge the reply carried, so a
        # `convo` run with topics.enabled=on shows per-turn topic routing.
        _badge = None
        try:
            import re as _re
            _m = _re.search(r"t#(\d{5})", delivered or "")
            _badge = _m.group(0) if _m else None
        except Exception:
            _badge = None
        return {
            "question": text,
            "delivered_answer": delivered,
            "topic_badge": _badge,
            "progress_messages": progress,
            "n_progress": len(progress),
            "all_sends": [s["content"][:400] for s in self.adapter.sent],
            "session_id": sid,
            "api_calls": self._api_call_counter["n"] or scan["api_calls_from_log"],
            "api_calls_hook": self._api_call_counter["n"],
            "wall_time": wall,
            "selfheal_actions": scan["selfheal_actions"],
            "selfheal_states": scan["selfheal_states"],
            "guard_events": scan["guard_events"],
            "poison_reset_fired": scan["poison_reset_fired"],
            "error": err,
        }


async def _amain(args):
    probe = GatewayProbe()
    report = {"home": os.environ.get("HERMES_HOME"), "mode": args.mode, "turns": []}

    if args.seed:
        seed_msgs = json.loads(Path(args.seed).read_text())
        report["seed"] = await probe.seed(seed_msgs)

    questions = args.questions
    for q in questions:
        turn = await probe.run_turn(q, per_turn_timeout=args.timeout)
        report["turns"].append(turn)
        # progress line to stderr so stdout stays clean JSON
        print(
            f"[turn] wall={turn['wall_time']}s progress={turn['n_progress']} "
            f"selfheal={turn['selfheal_actions']} poison_reset={turn['poison_reset_fired']} "
            f"answer={(turn['delivered_answer'] or '')[:70]!r} err={turn['error']}",
            file=sys.stderr, flush=True,
        )
    return report


def main():
    ap = argparse.ArgumentParser(description="Gateway-path test harness for hermes")
    ap.add_argument("--home", required=True, help="dedicated TEST hermes home")
    ap.add_argument("--seed", default=None, help="JSON file of prior [{role,content}] to plant")
    ap.add_argument("--timeout", type=int, default=600, help="per-turn timeout seconds")
    ap.add_argument("--out", default=None, help="also write full JSON report here")
    ap.add_argument("mode", choices=["ask", "convo"], help="ask=one turn, convo=multi-turn on ONE session")
    ap.add_argument("questions", nargs="+", help="question(s)")
    args = ap.parse_args()

    if args.mode == "ask" and len(args.questions) != 1:
        ap.error("ask takes exactly one question (use convo for multiple)")
    args.mode = args.mode

    _bootstrap_env(args.home)
    # Load plugins (router: pre_gateway_dispatch + constrained + selfheal + progress).
    from hermes_cli.plugins import discover_plugins
    discover_plugins(force=True)

    report = asyncio.run(_amain(args))
    out = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(out)
    print(out)


if __name__ == "__main__":
    main()
