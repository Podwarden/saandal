# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Podwarden Inc

"""Multi-topic conversation system (sibling module of selfheal.py / progress.py).

A single hermes chat often interleaves unrelated threads (e.g. motorcycles and
poetry). This module lets the assistant keep up to ``max_open`` (default 8)
concurrent OPEN TOPICS: it routes each inbound message to the right topic (or
opens a new one), injects that topic's accumulated context into the current
turn, tags the reply with a compact ``t#NNNNN`` badge, and (P3+) persists topic
memory to an llm-wiki subtree so a thread can be resumed hours or months later.

It rides THREE existing plugin surfaces and adds NO ``llm_request`` middleware,
so the existing ``_cd → selfheal → progress → loadaware`` chain is untouched:

* ``pre_llm_call``  (``_tp_pre_llm``)  — classify/route the inbound message and
  return ``{"context": <capped block>}``; hermes appends it to the *current-turn
  user message* (turn_context.py:495, ``plugin_user_context``), NOT the system
  prompt, so the cached system prefix stays byte-stable.
* ``post_llm_call`` (``_tp_post_llm``) — fold the turn's findings/log back into
  the active topic.
* the reply BADGE is composed into selfheal's sole ``transform_llm_output``
  finisher via the host bridge (``badge_for``), because transform hooks don't
  chain (first non-empty string wins) — exactly like the existing secret ``scrub``.

EVERYTHING is behind ``tools.tool_search.topics.enabled`` (default ``off``): with
the flag off — or on ANY internal error — every entry point no-ops and the turn
is byte-identical to stock hermes.

Phase status: P0 skeleton + P1 heuristic routing/injection/badge (in-memory, no
LLM). P2 swaps ``_classify`` for the LLM classifier; P3 makes the store durable
(llm-wiki); P4 adds write-back provenance + summary roll-up + verify enforcement.
"""

import json
import logging
import os
import re
import threading
import time

logger = logging.getLogger(__name__)

# PluginContext captured at register() so the classifier can reach ctx.llm.
_ctx = None

# Durable-store (P3) load state: the in-memory _topics dict is a write-through
# hot cache over <wiki>/topics/. We lazy-load the manifest on first touch and
# re-load when the on-disk _index.json mtime changes (a sibling gateway wrote).
_store_loaded = [False]
_store_mtime = [0.0]

DEFAULTS = {
    "enabled": "off",          # master kill-switch (conscious deploy step)
    "badge": "on",             # append the t#NNNNN reply badge (when enabled)
    # v1.20.0: also badge on a STRONG-tier host. The continuity injection stays
    # blanket-inert there; this is only the label, which is what makes a thread
    # referenceable from a phone. "off" restores the v1.15.0 suppression.
    "badge_strong": "on",
    "summarize": "on",         # allow the amortized summary roll-up call (P4)
    "classify": "llm",         # "llm" (classifier call, P2) | "heuristic" (no LLM)
    "max_open": 8,             # concurrent OPEN topics (LRU-evict to dormant past this)
    "block_token_cap": 250,    # hard cap on the injected per-topic context block
    "summarize_every": 6,      # roll up ## summary every N turns (amortized, P4)
    # v1.12.1: keyword-overlap bar to reopen a DORMANT topic on a cold return.
    # Round-2 measured 0.30 lifts dormant resume 31%->100% (duplicates 39->1) vs
    # the old 0.50 — a dormant topic only retains the words seen before dormancy,
    # so a fresh-phrased return scored below 0.50 and spawned a duplicate.
    "rehydrate_overlap": 0.30,
    "wiki_dir": os.path.expanduser("~/llm-wiki"),  # llm-wiki root; topics live in <wiki>/topics/ (P3)
    # v1.14.0 active-memory + research scratchpads — ALL default off (inert until
    # a conscious flip). research: /research plan-checklist; research_auto:
    # heuristic promotion; research_decompose: llm|off plan seeding; scratchpad:
    # capture non-src notes; resume_surface: acknowledge a resumed thread by
    # t#NNNNN; active_recall: proactively surface known facts; self_knowledge:
    # inject the "you have long-term memory" self-knowledge note; autostore:
    # inbox-only fact curation (human merge gate). Sizing knobs follow.
    "research": "off",
    "research_auto": "off",
    "research_decompose": "off",   # off | llm
    "scratchpad": "off",
    "resume_surface": "off",
    "active_recall": "off",
    "self_knowledge": "off",
    "autostore": "off",
    "plan_max_items": 6,
    "plan_token_cap": 180,
    "recall_facts_max": 5,
    "autostore_every": 4,
    "plan_overlap": 0.34,
    # v1.16.0 (W12 signal-gating): the strong-tier bar for injecting the
    # resumption block — a short CONTINUATION message AND keyword/entity overlap
    # >= this fraction with the topic. Below it, a strong model gets NO
    # injection (routing/storage still run), so a trivial one-off prompt can't
    # be derailed by an unrelated thread's context (batch4 r12/r44). weak/mid
    # tier keeps the v1.15.0 always-inject behaviour (threshold 0).
    "inject_overlap_strong": 0.6,
}

_FLAG_KEYS = ("enabled", "badge", "badge_strong", "summarize", "research",
              "research_auto",
              "scratchpad", "resume_surface", "active_recall", "self_knowledge",
              "autostore")
_INT_KEYS = ("max_open", "block_token_cap", "summarize_every", "plan_max_items",
             "plan_token_cap", "recall_facts_max", "autostore_every")
_FLOAT_KEYS = ("rehydrate_overlap", "plan_overlap", "inject_overlap_strong")
_STR_KEYS = ("classify", "wiki_dir", "research_decompose")

# --- module state (bounded like selfheal's tables) ---------------------------
_topics = {}         # topic_id(int) -> record dict (in-memory; P3 = disk-backed)
_next_id = [1]       # monotonic id allocator -> t%05d
_active_topic = {}   # session_id -> topic_id (the latest turn's active topic)
_active_voice = {}   # session_id -> bool (is this a voice_only session?)
_tp_turn = {}        # (session_id, turn_id) -> topic_id (per-turn memo)
_voice_map_cache = [0.0, {}]   # [mtime, gateway_voice_mode.json map]
_TABLE_MAX = 64
_IDLE_S = 3600
_REGISTRY_MAX = 128  # in-memory topic-registry bound (P3 spills to disk instead)

_REVIEW_HARNESS_PREFIX = "Review the conversation above"

# Heuristic routing knobs (used directly in classify=heuristic, and as the LLM
# fallback when classify=llm).
_ROUTE_MIN_OVERLAP = 0.34   # keyword-overlap fraction to route to an existing topic
_ACTIVE_BIAS = 0.15         # continuity boost for the currently-active topic
_ENT_CAP = 20               # entities retained per topic
_RECENT_CAP = 4             # recent exchanges retained per topic
_FIND_CAP = 12              # findings retained per topic
_SCRATCH_CAP = 8            # scratchpad notes retained per research topic (R2)
_FACTS_CAP = 12             # facts retained per topic
_SESSION_CAP = 5            # hermes session ids retained per topic (v1.20.0)
_HEAD = 90                  # chars kept from a message head

# LLM-classifier confidence thresholds (continuity hysteresis, anti-thrash):
# below CONF_SWITCH we don't switch away from the active topic; below CONF_NEW
# we don't spend a slot on a new topic (attach to the active one instead).
CONF_SWITCH = 0.55
CONF_NEW = 0.6
# v1.19.2 — these budgets must cover REASONING tokens, not just the answer.
# Measured against deepseek-v4-pro on the real classify prompt: at 96 tokens the
# model spent all 96 reasoning and returned EMPTY content 3/3 (finish_reason=
# length), so every classification silently fell back to the keyword heuristic —
# root was configured classify:llm and had never once used it. From 512 up it
# succeeds 3/3 (reasoning observed 108-612, so 1024 leaves real headroom).
# Raising a cap costs nothing on a non-reasoning model: max_tokens is a ceiling,
# not a target, and the 27B (enable_thinking=false) still emits ~50 tokens here.
_CLASSIFY_TIMEOUT_S = 12.0   # measured median 3.5s / max 5.8s at this budget
_CLASSIFY_MAX_TOKENS = 1024

# JSON schema the classifier is constrained to (vLLM/xgrammar enforces it).
_CLASSIFY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "topic_id": {"type": "string",
                     "description": "an open topic id like 't00042', or 'new'"},
        "action": {"type": "string", "enum": ["route", "open"]},
        "confidence": {"type": "number"},
        "title": {"type": "string"},
        "entities": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["topic_id", "action", "confidence"],
}

_CLASSIFY_INSTRUCTIONS = (
    "You are a conversation TOPIC ROUTER. You are given the currently OPEN "
    "topics (each with an id, title, and key entities), the last couple of "
    "messages, and a NEW user message. Decide which open topic the new message "
    "continues, or whether it starts a genuinely NEW topic.\n"
    "- Strongly prefer continuing an existing open topic when the subject, "
    "entities, or clear pronoun/anaphora reference overlap.\n"
    "- Only choose action='open' (topic_id='new') for a clearly different "
    "subject.\n"
    "- Set confidence in [0,1]: how sure you are of the routing.\n"
    "Return ONLY the JSON object: {\"topic_id\": \"t00042\" or \"new\", "
    "\"action\": \"route\" or \"open\", \"confidence\": 0.0-1.0, "
    "\"title\": short title if opening, \"entities\": [key nouns]}."
)

_STOPWORDS = frozenset((
    "the a an and or but if then of to in on at for with about from into over "
    "is are was were be been being do does did have has had will would can could "
    "should may might must this that these those it its it's you your yours i me "
    "my we us our they them their he she his her what which who whom how why when "
    "where whas as so not no yes just like get got make made want need know think "
    "there here also more most some any all one two more please thanks thank ok "
    "и в на не что как это по для с о а но же бы то так вот уже еще был была были "
    "это эта этот тот там где когда почему какой мне мой моя твой вы ты они он она"
).split())

# Continuation cues → bias toward staying on the active topic (anti-sprawl).
_CONT_CUES = (
    "and ", "and the", "what about", "how about", "also", "what else", "more ",
    "continue", "go on", "tell me more", "why", "how ", "and how", "and why",
    "и ", "а что", "а как", "еще", "ещё", "продолж", "дальше",
)


def _norm_flag(v, default):
    if isinstance(v, bool):
        return "on" if v else "off"
    s = str(v).strip().lower()
    return s if s in ("on", "off") else default


def _cfg():
    """Effective topics config (DEFAULTS overlaid with the config file), read
    per call like every other router knob — a config flip needs no restart.
    Invalid values fall back to the default; never raises."""
    out = dict(DEFAULTS)
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load
        tp = (((_load().get("tools") or {}).get("tool_search") or {})
              .get("topics") or {})
        if isinstance(tp, dict):
            for k in _FLAG_KEYS:
                if k in tp and tp[k] is not None:
                    out[k] = _norm_flag(tp[k], out[k])
            for k in _INT_KEYS:
                if k in tp and tp[k] is not None and not isinstance(tp[k], bool):
                    try:
                        out[k] = int(tp[k])
                    except Exception:
                        pass
            for k in _FLOAT_KEYS:
                if k in tp and tp[k] is not None and not isinstance(tp[k], bool):
                    try:
                        out[k] = float(tp[k])
                    except Exception:
                        pass
            for k in _STR_KEYS:
                if k in tp and tp[k]:
                    out[k] = str(tp[k]).strip()
    except Exception:
        pass
    return out


def _prune(table, now=None):
    """Bound a session-keyed table: hard-clear if oversized (int-valued tables
    have no per-entry ts, so they rely on the hard-clear). Mirrors selfheal."""
    try:
        now = now if now is not None else time.time()
        if len(table) <= _TABLE_MAX:
            return
        cutoff = now - _IDLE_S
        for k in [k for k, v in list(table.items())
                  if isinstance(v, dict) and v.get("ts", now) < cutoff]:
            table.pop(k, None)
        if len(table) > _TABLE_MAX:
            table.clear()
    except Exception:
        pass


def _reset_state():
    """Test helper: wipe all module state (in-memory only; never deletes disk)."""
    _topics.clear()
    _next_id[0] = 1
    _active_topic.clear()
    _active_voice.clear()
    _tp_turn.clear()
    _store_loaded[0] = False
    _store_mtime[0] = 0.0
    _voice_map_cache[0] = 0.0
    _voice_map_cache[1] = {}


def _hermes_home():
    return os.environ.get("HERMES_HOME") \
        or os.path.join(os.path.expanduser("~"), ".hermes")


def _voice_mode_map():
    """Read <HERMES_HOME>/gateway_voice_mode.json (mtime-cached). Maps
    'platform:chat_id' -> mode string. Never raises."""
    try:
        path = os.path.join(_hermes_home(), "gateway_voice_mode.json")
        mtime = os.path.getmtime(path)
        if mtime <= _voice_map_cache[0] and _voice_map_cache[1]:
            return _voice_map_cache[1]
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _voice_map_cache[0] = mtime
            _voice_map_cache[1] = data
            return data
    except Exception:
        pass
    return _voice_map_cache[1]


def _is_voice_session(platform, sender_id):
    """True when this chat's voice mode is voice_only (badge would be spoken by
    TTS). Keyed 'platform:sender_id' — exact for DMs where chat_id==user_id."""
    try:
        if not platform or not sender_id:
            return False
        key = "%s:%s" % (str(platform).strip().lower(), str(sender_id).strip())
        return str(_voice_mode_map().get(key) or "").lower() == "voice_only"
    except Exception:
        return False


def _in_background_review(messages=None):
    """True when running inside hermes's background memory/skill review — a
    forked, never-delivered session. Topic routing/injection must NOT run there.
    Detected via the review thread name, the thread-local tool whitelist, or the
    replayed review harness prompt. Fail-safe (False on error). Mirrors the guard
    in progress.py / __init__.py."""
    try:
        if str(getattr(threading.current_thread(), "name", "")
               ).startswith("bg-review"):
            return True
    except Exception:
        pass
    try:
        from hermes_cli import plugins as _hp
        if getattr(getattr(_hp, "_thread_tool_whitelist", None),
                   "allowed", None) is not None:
            return True
    except Exception:
        pass
    try:
        for m in (messages or []):
            if isinstance(m, dict) and m.get("role") in ("user", "system"):
                c = m.get("content")
                if isinstance(c, str) \
                        and c.lstrip().startswith(_REVIEW_HARNESS_PREFIX):
                    return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-zЀ-ӿ][A-Za-z0-9Ѐ-ӿ'\-]{2,}")


def _keywords(text, limit=12):
    """Significant lowercased tokens (Latin + Cyrillic), stopwords removed,
    order-preserving dedupe, capped."""
    out, seen = [], set()
    try:
        for t in _WORD_RE.findall(str(text or "").lower()):
            if t in _STOPWORDS or t in seen:
                continue
            seen.add(t)
            out.append(t)
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out


def _is_continuation(text):
    """A short follow-up / continuation cue → bias toward the active topic."""
    t = str(text or "").strip().lower()
    if not t:
        return True
    if len(t.split()) <= 4:
        return True
    return any(t.startswith(c) for c in _CONT_CUES)


def _title_from(text, kws):
    if kws:
        return " ".join(kws[:4]).title()
    t = " ".join(str(text or "").split())
    return t[:40] or "New topic"


def _head(text, n=_HEAD):
    return " ".join(str(text or "").split())[:n]


def _recent_user_texts(history, n=2):
    out = []
    try:
        for m in reversed(history or []):
            if isinstance(m, dict) and m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, str) and c.strip():
                    out.append(c)
                    if len(out) >= n:
                        break
    except Exception:
        pass
    return list(reversed(out))


# ---------------------------------------------------------------------------
# Topic store (in-memory for P1; disk-backed in P3)
# ---------------------------------------------------------------------------

def _oneline(s):
    return " ".join(str(s or "").split())


def _slug(title):
    s = re.sub(r"[^a-z0-9]+", "-", str(title or "").lower()).strip("-")
    return s[:48] or "topic"


def _open_topic(title, entities, now):
    tid = _next_id[0]
    _next_id[0] += 1
    rec = {
        "id": tid,
        "slug": _slug(title),
        "title": title,
        "entities": list(dict.fromkeys(entities))[:_ENT_CAP],
        "summary": "",      # rolling narrative (P4 roll-up)
        "recent": [],       # [(user_head, answer_head)] -> ## log
        "facts": [],        # user-given durable bullets (P4 harvest)
        "findings": [],     # "claim [src: url]" (P4)
        "research": False,  # v1.14.0: is this a research task?
        "plan": [],         # v1.14.0: [{text, status:'open'|'done', src}] -> ## plan
        "scratch": [],      # v1.14.0: short non-src research notes -> ## scratchpad
        "sessions": [],     # v1.20.0: hermes sessions this thread appeared in
        "created": now,
        "last_active": now,
        "turns": 0,
        "status": "open",   # open | dormant | closed
        "_body_loaded": True,  # freshly created -> full body is in memory
    }
    _topics[tid] = rec
    return rec


def _open_recs():
    return [r for r in _topics.values() if r.get("status") == "open"]


def _evict_if_needed(cfg, now):
    """LRU-evict OPEN topics to dormant past max_open. Dormant topics stay on
    disk (retained, not deleted); only the hot 'open' set is bounded. The
    in-memory registry mirrors the manifest (bounded later by archival
    compaction — an open design decision)."""
    try:
        max_open = max(1, int(cfg.get("max_open", 8)))
        opens = sorted(_open_recs(), key=lambda r: r.get("last_active", 0))
        while len(_open_recs()) > max_open and opens:
            victim = opens.pop(0)
            victim["status"] = "dormant"
            _persist_topic(cfg, victim)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Durable store: <wiki>/topics/ (P3). merge_inboxes.py never scans this subtree.
# The in-memory _topics dict is a write-through hot cache; _index.json is the
# only file read during classification (no per-topic fan-out).
# ---------------------------------------------------------------------------

def _store_dir(cfg):
    return os.path.join(str(cfg.get("wiki_dir") or DEFAULTS["wiki_dir"]),
                        "topics")


def _ensure_store(cfg):
    d = _store_dir(cfg)
    try:
        os.makedirs(d, exist_ok=True)
        for name, body in ((".gitkeep", ""),
                           ("README.md",
                            "# topics/\n\nPlugin-owned multi-topic state "
                            "(saandal router). Written only by the plugin; "
                            "`merge_inboxes.py` never scans this subtree. Do "
                            "not hand-edit.\n")):
            p = os.path.join(d, name)
            if not os.path.exists(p):
                try:
                    with open(p, "w", encoding="utf-8") as f:
                        f.write(body)
                except Exception:
                    pass
    except Exception:
        pass
    return d


def _index_path(cfg):
    return os.path.join(_store_dir(cfg), "_index.json")


def _topic_path(cfg, tid):
    return os.path.join(_store_dir(cfg), "t%05d.md" % int(tid))


def _atomic_write(path, text):
    """Write via tmp + os.replace so a reader never sees a torn file and
    concurrent writers to DISTINCT paths never collide. Never raises."""
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def _iso(ts):
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts or 0)))
    except Exception:
        return ""


def _serialize_topic_md(rec):
    ents = ", ".join(_oneline(e) for e in (rec.get("entities") or []))
    out = [
        "---",
        "id: t%05d" % int(rec["id"]),
        "slug: %s" % (rec.get("slug") or ""),
        "title: %s" % _oneline(rec.get("title")),
        "status: %s" % (rec.get("status") or "open"),
        "entities: [%s]" % ents,
        "created: %s" % _iso(rec.get("created") or rec.get("last_active")),
        "last_active: %s" % _iso(rec.get("last_active")),
        "turns: %d" % int(rec.get("turns", 0)),
    ]
    # v1.20.0: hermes session ids this thread appeared in, so a caller can pivot
    # from a thread to its actual messages via the built-in session_search.
    # Emitted ONLY when non-empty, so every pre-v1.20 record still serializes
    # byte-identically (the anti-disclosure/inert regressions stay green).
    sessions = [s for s in (rec.get("sessions") or []) if s][-_SESSION_CAP:]
    if sessions:
        out.append("sessions: [%s]" % ", ".join(_oneline(s) for s in sessions))
    out += [
        "---",
        "## summary",
        _oneline(rec.get("summary") or ""),
        "",
        "## facts",
    ]
    out += ["- " + _oneline(f) for f in (rec.get("facts") or [])]
    out += ["", "## findings"]
    out += ["- " + _oneline(f) for f in (rec.get("findings") or [])]
    # v1.14.0: research plan checklist (only when the topic is a research task)
    if rec.get("research"):
        out += ["", "## plan"]
        for it in (rec.get("plan") or []):
            box = "x" if it.get("status") == "done" else " "
            src = it.get("src") or ""
            line = "- [%s] %s" % (box, _oneline(it.get("text")))
            if src:
                line += " [src: %s]" % src
            out.append(line)
        out += ["", "## scratchpad"]
        out += ["- " + _oneline(s) for s in (rec.get("scratch") or [])]
    out += ["", "## log"]
    for u, a in (rec.get("recent") or []):
        out.append("- u: %s / a: %s" % (_oneline(u), _oneline(a)))
    return "\n".join(out) + "\n"


_LOG_LINE_RE = re.compile(r"^-\s*u:\s*(.*?)\s*/\s*a:\s*(.*)$")
_PLAN_RE = re.compile(r"^-\s*\[( |x)\]\s*(.*?)(?:\s*\[src:\s*(\S+?)\])?$", re.I)


def _parse_topic_md(text):
    """Parse a t<NNNNN>.md file into a full record dict (frontmatter + body).
    The manifest is normally authoritative, but this recovers a topic when only
    the file survives (archival / grep fallback)."""
    try:
        lines = str(text or "").split("\n")
        body, fm = lines, {}
        if lines and lines[0].strip() == "---":
            i = 1
            while i < len(lines) and lines[i].strip() != "---":
                if ":" in lines[i]:
                    k, v = lines[i].split(":", 1)
                    fm[k.strip()] = v.strip()
                i += 1
            body = lines[i + 1:]
        tid = _parse_topic_id(fm.get("id"))
        ents_raw = (fm.get("entities") or "").strip().strip("[]")
        entities = [e.strip() for e in ents_raw.split(",") if e.strip()]
        sess_raw = (fm.get("sessions") or "").strip().strip("[]")
        sessions = [s.strip() for s in sess_raw.split(",") if s.strip()]
        try:
            turns = int(re.search(r"\d+", fm.get("turns") or "0").group())
        except Exception:
            turns = 0
        section = None
        summary, facts, findings, recent, plan, scratch = [], [], [], [], [], []
        for ln in body:
            s = ln.strip()
            if s.startswith("## "):
                section = s[3:].strip().lower()
                continue
            if section == "summary":
                if s:
                    summary.append(s)
            elif section == "facts" and s.startswith("- "):
                facts.append(s[2:])
            elif section == "findings" and s.startswith("- "):
                findings.append(s[2:])
            elif section == "plan":
                m = _PLAN_RE.match(s)
                if m:
                    plan.append({"text": m.group(2).strip(),
                                 "status": "done" if m.group(1).lower() == "x"
                                 else "open", "src": (m.group(3) or "").strip()})
            elif section == "scratchpad" and s.startswith("- "):
                scratch.append(s[2:])
            elif section == "log":
                m = _LOG_LINE_RE.match(s)
                if m:
                    recent.append((m.group(1), m.group(2)))
        return {"id": tid, "slug": fm.get("slug") or "",
                "title": fm.get("title") or "", "entities": entities,
                "status": fm.get("status") or "dormant", "turns": turns,
                "summary": " ".join(summary), "facts": facts,
                "findings": findings, "recent": recent[-_RECENT_CAP:],
                "research": bool(plan), "plan": plan, "scratch": scratch,
                "sessions": sessions}
    except Exception:
        return None


def _ensure_topic_loaded(cfg, tid):
    """Bring a single topic into the hot cache from its file (used when the grep
    fallback matched a topic not in the manifest). Returns the record or None."""
    if tid in _topics:
        return _topics[tid]
    try:
        path = _topic_path(cfg, tid)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            p = _parse_topic_md(f.read())
        if not p or p.get("id") != tid:
            return None
        _topics[tid] = {
            "id": tid, "slug": p.get("slug") or "", "title": p.get("title") or "",
            "entities": list(p.get("entities") or []),
            "summary": p.get("summary") or "", "recent": p.get("recent") or [],
            "facts": p.get("facts") or [], "findings": p.get("findings") or [],
            "research": bool(p.get("research")), "plan": p.get("plan") or [],
            "scratch": p.get("scratch") or [],
            "sessions": list(p.get("sessions") or []),
            "created": 0.0, "last_active": 0.0, "turns": int(p.get("turns") or 0),
            "status": p.get("status") or "dormant", "_body_loaded": True,
        }
        return _topics[tid]
    except Exception:
        return None


def _resolve_new(cfg, text, kws, decision, now):
    """Before spending a slot on a NEW topic, try to resume a matching dormant
    topic (hot cache, then disk grep); else open a fresh one."""
    tid = _cold_match_dormant(cfg, text)
    if tid is not None:
        rec = _ensure_topic_loaded(cfg, tid)
        if rec is not None:
            rec["status"] = "open"
            rec["_resumed"] = True   # R3: transient — dormant thread resumed
            return rec
    title = (decision or {}).get("title") or _title_from(text, kws)
    ents = (decision or {}).get("entities") or kws
    return _open_topic(title, ents, now)


def _row_from_rec(r):
    return {
        "id": int(r["id"]), "slug": r.get("slug") or "",
        "title": r.get("title") or "",
        "entities": list(r.get("entities") or [])[:_ENT_CAP],
        "status": r.get("status") or "open",
        "last_active": float(r.get("last_active") or 0),
        "turns": int(r.get("turns", 0)),
        "sessions": [x for x in (r.get("sessions") or []) if x][-_SESSION_CAP:],
        "digest": _oneline(r.get("summary") or r.get("title") or "")[:160],
    }


def _write_index(cfg):
    try:
        _ensure_store(cfg)
        rows = [_row_from_rec(r) for r in sorted(_topics.values(),
                                                 key=lambda x: x["id"])]
        data = {"next_id": int(_next_id[0]), "topics": rows}
        if _atomic_write(_index_path(cfg),
                         json.dumps(data, ensure_ascii=False, indent=1)):
            try:
                _store_mtime[0] = os.path.getmtime(_index_path(cfg))
            except Exception:
                pass
    except Exception:
        logger.debug("topics: index write failed", exc_info=True)


def _load_body_for_write(cfg, rec):
    """Fill a manifest-only record's body from disk BEFORE it is serialized.

    v1.19.1 — this closes a silent data-loss bug. ``_load_store`` materialises
    records from ``_index.json`` with EMPTY body fields and
    ``_body_loaded: False``; only ``_route`` rehydrates them. Every other path
    that persists — LRU eviction in ``_evict_if_needed``, ``/topic close`` —
    handed ``_serialize_topic_md`` those empty fields, and since the serializer
    writes the WHOLE file, it overwrote a file that still held the real
    conversation. The frontmatter survived (it comes from the manifest), which
    is why the wreckage looks like ``turns: 6`` above an empty ``## log``.

    Merges rather than replaces, so a caller that already appended in memory
    keeps its addition. Leaves ``_body_loaded`` False when the body could not
    be read, so the caller can skip the body write rather than destroy it.
    """
    if rec.get("_body_loaded"):
        return
    try:
        path = _topic_path(cfg, rec["id"])
        if not os.path.exists(path):
            rec["_body_loaded"] = True   # nothing on disk to lose
            return
        with open(path, encoding="utf-8") as f:
            parsed = _parse_topic_md(f.read())
        if not parsed:
            return  # unreadable — do NOT claim a body we could not load
        if not rec.get("summary"):
            rec["summary"] = parsed.get("summary") or ""
        for field, cap in (("recent", _RECENT_CAP), ("facts", _FACTS_CAP),
                           ("findings", _FIND_CAP), ("plan", None),
                           ("scratch", _SCRATCH_CAP), ("sessions", _SESSION_CAP)):
            merged = list(parsed.get(field) or [])
            for item in (rec.get(field) or []):
                if item not in merged:
                    merged.append(item)
            rec[field] = merged[-cap:] if cap else merged
        if parsed.get("research"):
            rec["research"] = True
        for e in (parsed.get("entities") or []):
            if e not in rec["entities"]:
                rec["entities"].append(e)
        rec["_body_loaded"] = True
    except Exception:
        logger.debug("topics: body preload failed for t#%05d",
                     int(rec.get("id") or 0), exc_info=True)


def _persist_topic(cfg, rec):
    """Write-through: persist one topic's body + refresh the manifest. Fail-safe
    (disk errors never affect routing).

    The body is written ONLY from a record whose body is actually loaded — see
    _load_body_for_write. A record we could not read is left on disk untouched
    and only the manifest is refreshed: a stale frontmatter is recoverable, an
    erased conversation is not."""
    try:
        _ensure_store(cfg)
        _load_body_for_write(cfg, rec)
        if rec.get("_body_loaded"):
            _atomic_write(_topic_path(cfg, rec["id"]), _serialize_topic_md(rec))
        else:
            logger.warning("topics: body for t#%05d unreadable — wrote manifest "
                           "only, left the file intact",
                           int(rec.get("id") or 0))
        _write_index(cfg)
    except Exception:
        logger.debug("topics: topic persist failed", exc_info=True)


def _load_store(cfg):
    """Lazy-load the manifest into the hot cache on first touch, and re-load on
    a newer on-disk mtime (a sibling gateway wrote). Merges disk rows without
    clobbering hotter in-memory bodies. Never raises."""
    try:
        path = _index_path(cfg)
        if not os.path.exists(path):
            _store_loaded[0] = True
            return
        mtime = os.path.getmtime(path)
        if _store_loaded[0] and mtime <= _store_mtime[0]:
            return
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for row in (data.get("topics") or []):
            try:
                tid = int(row.get("id"))
            except Exception:
                continue
            cur = _topics.get(tid)
            if cur is not None and cur.get("_body_loaded"):
                # keep the hotter in-memory record; only adopt a newer status
                if float(row.get("last_active") or 0) > \
                        float(cur.get("last_active") or 0):
                    cur["status"] = row.get("status") or cur["status"]
                continue
            _topics[tid] = {
                "id": tid, "slug": row.get("slug") or "",
                "title": row.get("title") or "",
                "entities": list(row.get("entities") or []),
                "summary": "", "recent": [], "facts": [], "findings": [],
                "research": False, "plan": [], "scratch": [],
                "sessions": list(row.get("sessions") or []),
                "created": float(row.get("last_active") or 0),
                "last_active": float(row.get("last_active") or 0),
                "turns": int(row.get("turns", 0)),
                "status": row.get("status") or "open",
                "_body_loaded": False,
            }
        _next_id[0] = max(int(data.get("next_id") or 1), int(_next_id[0]),
                          (max(_topics) + 1) if _topics else 1)
        _store_loaded[0] = True
        _store_mtime[0] = mtime
    except Exception:
        logger.debug("topics: store load failed; in-memory only", exc_info=True)
        _store_loaded[0] = True


def _rehydrate(cfg, rec):
    """Load a topic's full body from disk on first activation (dormant→open
    resume, months later). No-op if already loaded. Never raises."""
    try:
        if rec.get("_body_loaded"):
            return
        path = _topic_path(cfg, rec["id"])
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                parsed = _parse_topic_md(f.read())
            if parsed:
                rec["summary"] = parsed.get("summary") or ""
                rec["facts"] = parsed.get("facts") or []
                rec["findings"] = parsed.get("findings") or []
                rec["recent"] = parsed.get("recent") or []
                rec["plan"] = parsed.get("plan") or []
                rec["scratch"] = parsed.get("scratch") or []
                for sid in (parsed.get("sessions") or []):
                    if sid not in (rec.get("sessions") or []):
                        rec.setdefault("sessions", []).append(sid)
                if parsed.get("research"):
                    rec["research"] = True
                for e in (parsed.get("entities") or []):
                    if e not in rec["entities"]:
                        rec["entities"].append(e)
    except Exception:
        pass
    finally:
        rec["_body_loaded"] = True


def _rehydrate_overlap(cfg):
    try:
        v = float(cfg.get("rehydrate_overlap", 0.30))
        return v if 0.0 < v <= 1.0 else 0.30
    except Exception:
        return 0.30


def _plan_overlap(cfg):
    """Keyword-overlap threshold for reconciling a sourced finding against an
    open research-plan item (v1.14.0 R1). Same shape as _rehydrate_overlap."""
    try:
        v = float(cfg.get("plan_overlap", 0.34))
        return v if 0.0 < v <= 1.0 else 0.34
    except Exception:
        return 0.34


def _grep_topic_files(cfg, text, min_overlap=None):
    """Fallback recall: scan t<NNNNN>.md frontmatter for a title/entity match
    when a topic isn't in the hot manifest (archival-compaction case). Returns a
    topic_id or None. Bounded + fail-safe."""
    try:
        if min_overlap is None:
            min_overlap = _rehydrate_overlap(cfg)
        kset = set(_keywords(text))
        if not kset:
            return None
        d = _store_dir(cfg)
        if not os.path.isdir(d):
            return None
        best_tid, best = None, 0.0
        names = [n for n in os.listdir(d)
                 if re.match(r"^t\d{5}\.md$", n)][:512]
        for n in names:
            try:
                with open(os.path.join(d, n), encoding="utf-8") as f:
                    head = f.read(600)
            except Exception:
                continue
            ents = set(_keywords(head))
            if not ents:
                continue
            score = len(kset & ents) / float(len(kset))
            if score > best:
                best, best_tid = score, _parse_topic_id(n)
        return best_tid if best >= min_overlap else None
    except Exception:
        return None


def _cold_match_dormant(cfg, text, min_overlap=None):
    """Before spending a slot on a NEW topic, try to reopen a matching dormant
    topic (title/entity overlap) — first from the hot cache, then via the disk
    grep fallback. Returns a topic_id or None."""
    try:
        if min_overlap is None:
            min_overlap = _rehydrate_overlap(cfg)
        kset = set(_keywords(text))
        if not kset:
            return None
        best_tid, best = None, 0.0
        for r in _topics.values():
            if r.get("status") != "dormant":
                continue
            ents = set(_keywords(" ".join(r.get("entities") or [])
                                 + " " + (r.get("title") or "")))
            if not ents:
                continue
            score = len(kset & ents) / float(len(kset))
            if score > best:
                best, best_tid = score, r["id"]
        if best_tid is not None and best >= min_overlap:
            return best_tid
        return _grep_topic_files(cfg, text, min_overlap)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Classifier seam (P1 heuristic; P2 swaps in the LLM classifier)
# ---------------------------------------------------------------------------

def _classify_heuristic(text, open_recs, recent_texts, active_tid):
    """Keyword-overlap router (used directly for classify=heuristic, and as the
    LLM fallback). Returns a decision dict
    {action: route|open, topic_id, confidence, title, entities}."""
    kws = _keywords(text)
    kset = set(kws)
    # No content words (pure continuation, e.g. "tell me more") → stay on the
    # active topic. A keyword-rich message — even one starting with "and …" —
    # is routed by overlap below, so it can jump back to the topic it names;
    # continuity is a BIAS (_ACTIVE_BIAS), not a hard override.
    if not kset and active_tid in _topics:
        return {"action": "route", "topic_id": active_tid, "confidence": 0.6,
                "title": None, "entities": kws}
    best_tid, best_score = None, 0.0
    for r in open_recs:
        ents = set(_keywords(" ".join(r.get("entities") or [])
                             + " " + (r.get("title") or "")))
        if not ents or not kset:
            continue
        score = len(kset & ents) / float(len(kset))
        if r["id"] == active_tid:
            score += _ACTIVE_BIAS
        if score > best_score:
            best_score, best_tid = score, r["id"]
    if best_tid is not None and best_score >= _ROUTE_MIN_OVERLAP:
        return {"action": "route", "topic_id": best_tid,
                "confidence": min(0.99, best_score + 0.3),
                "title": None, "entities": kws}
    return {"action": "open", "topic_id": None, "confidence": 0.6,
            "title": _title_from(text, kws), "entities": kws}


def _parse_topic_id(raw):
    """'t00042' / 't#42' / '42' -> 42 (int), else None."""
    try:
        m = re.search(r"(\d+)", str(raw or ""))
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _coerce_llm_decision(parsed, open_recs):
    """Map the classifier's JSON {topic_id:'t00042'|'new', action, ...} to the
    internal decision. Returns None if unusable (→ heuristic fallback)."""
    try:
        if not isinstance(parsed, dict):
            return None
        action = str(parsed.get("action") or "").strip().lower()
        conf = float(parsed.get("confidence") or 0.0)
        ents = [str(e) for e in (parsed.get("entities") or []) if str(e).strip()]
        title = str(parsed.get("title") or "").strip() or None
        raw_id = str(parsed.get("topic_id") or "").strip().lower()
        open_ids = {r["id"] for r in open_recs}
        if action == "route" and raw_id not in ("", "new", "none"):
            tid = _parse_topic_id(raw_id)
            if tid in open_ids:
                return {"action": "route", "topic_id": tid,
                        "confidence": conf, "title": None, "entities": ents}
            # LLM named a non-open id → treat as open (rehydration is P3)
        return {"action": "open", "topic_id": None, "confidence": conf,
                "title": title, "entities": ents}
    except Exception:
        return None


def _classify_llm(text, open_recs, recent_texts, cfg):
    """One bounded structured classify call via ctx.llm. Returns a decision dict
    or None (→ caller falls back to the heuristic). Never raises."""
    ctx = _ctx
    llm = getattr(ctx, "llm", None) if ctx is not None else None
    if llm is None or not hasattr(llm, "complete_structured"):
        return None
    try:
        from agent.plugin_llm import PluginLlmTextInput
    except Exception:
        return None
    try:
        manifest = [{"id": "t%05d" % r["id"],
                     "title": (r.get("title") or "")[:60],
                     "entities": (r.get("entities") or [])[:8]}
                    for r in open_recs]
        payload = json.dumps(
            {"open_topics": manifest,
             "recent_messages": [str(t)[:200] for t in (recent_texts or [])[-2:]],
             "new_message": str(text)[:600]},
            ensure_ascii=False)[:2400]
        res = llm.complete_structured(
            instructions=_CLASSIFY_INSTRUCTIONS,
            input=[PluginLlmTextInput(text=payload)],
            json_mode=True,
            json_schema=_CLASSIFY_SCHEMA,
            schema_name="topic_decision",
            temperature=0.0,
            max_tokens=_CLASSIFY_MAX_TOKENS,
            timeout=_CLASSIFY_TIMEOUT_S,
            purpose="router.topic_classify",
        )
        parsed = getattr(res, "parsed", None)
        if not parsed:
            # v1.19.2: LOUD. This was a debug-level silence, and it hid that
            # root ran classify:llm for a full day while every call returned
            # empty (a reasoning model spent the entire 96-token budget on
            # reasoning) and silently fell back to keyword routing.
            logger.warning("topics: llm classify returned nothing (budget %d "
                           "tokens, %.0fs) — heuristic fallback. Raise the "
                           "budget if this model reasons.",
                           _CLASSIFY_MAX_TOKENS, _CLASSIFY_TIMEOUT_S)
        return _coerce_llm_decision(parsed, open_recs)
    except Exception:
        logger.debug("topics: llm classify failed; heuristic fallback",
                     exc_info=True)
        return None


def _apply_thresholds(d, active_tid):
    """Continuity hysteresis on an LLM decision: don't switch away from / spend a
    slot for below-threshold confidence — attach to the active topic instead."""
    try:
        action = d.get("action")
        conf = float(d.get("confidence") or 0.0)
        tid = d.get("topic_id")
        if action == "open" and conf < CONF_NEW and active_tid in _topics:
            return {"action": "route", "topic_id": active_tid,
                    "confidence": conf, "title": None,
                    "entities": d.get("entities") or []}
        if action == "route" and tid != active_tid and conf < CONF_SWITCH \
                and active_tid in _topics:
            return {"action": "route", "topic_id": active_tid,
                    "confidence": conf, "title": None,
                    "entities": d.get("entities") or []}
    except Exception:
        pass
    return d


def _classify(text, open_recs, recent_texts, active_tid, cfg=None):
    """Routing dispatcher (injectable seam). classify=heuristic → keyword
    router; classify=llm → the bounded structured call with the heuristic as a
    timeout/error fallback, then continuity-threshold hysteresis. Empty-content
    messages always stay on the active topic."""
    cfg = cfg or _cfg()
    if not _keywords(text) and active_tid in _topics:
        return {"action": "route", "topic_id": active_tid, "confidence": 0.6,
                "title": None, "entities": []}
    if str(cfg.get("classify")) != "heuristic":
        d = _classify_llm(text, open_recs, recent_texts, cfg)
        if d is not None:
            return _apply_thresholds(d, active_tid)
    return _classify_heuristic(text, open_recs, recent_texts, active_tid)


_OVERRIDE_RE = re.compile(r"^/topics?(?:\s+(.*))?$", re.I | re.S)


def _parse_override(text):
    """Parse an explicit ``/topic …`` control command (deterministic, no LLM).
    Returns (kind, arg) — kind in {list, close, new, route} — or None."""
    m = _OVERRIDE_RE.match(str(text or "").strip())
    if not m:
        return None
    arg = (m.group(1) or "").strip()
    low = arg.lower()
    # v1.19.0: `list`/`status` may carry filter tokens (`list open since:7d`).
    # Before this, only a BARE `list` matched and anything trailing fell through
    # to the ("new", …) tail below — so `/topic list open` silently OPENED a
    # thread titled "list open" instead of listing anything.
    head = low.split(None, 1)[0] if low else ""
    if low == "" or head in ("list", "status"):
        return ("list", arg or None)
    if low.startswith(("close", "done", "end")):
        return ("close", None)
    if low.startswith("new"):
        return ("new", arg[3:].strip() or None)
    tid = _parse_topic_id(arg)
    if tid is not None:
        return ("route", tid)
    return ("new", arg or None)


def _apply_override(ov, session_id, now, cfg):
    """Apply a /topic control. Returns the resolved record, or None when the
    turn should carry NO topic context (close / list / unknown id)."""
    kind, arg = ov
    if kind == "route" and arg in _topics:
        rec = _topics[arg]
        if rec.get("status") in ("dormant", "closed"):
            rec["_resumed"] = True   # R3: explicit /topic <id> reopening a cold one
        rec["status"] = "open"
        return rec
    if kind == "new":
        rec = _open_topic(arg or "New topic", _keywords(arg or ""), now)
        if arg:
            # v1.19.2: a name the USER chose is never overwritten by a roll-up
            # retitle. In-memory only — after a restart the thread is
            # indistinguishable from an auto-titled one and may be renamed.
            rec["_user_titled"] = True
        return rec
    if kind == "close":
        tid = _active_topic.get(session_id)
        if tid in _topics:
            _topics[tid]["status"] = "closed"
            _curation_note(cfg, _topics[tid])   # fold durable state into inbox/
            _persist_topic(cfg, _topics[tid])
        _active_topic.pop(session_id, None)
        return None
    return None  # list / route-to-unknown-id → no injection (rehydration is P3)


# ---------------------------------------------------------------------------
# v1.14.0 R1: /research plan detection + seeding  (gated by the research flag)
# ---------------------------------------------------------------------------

_RESEARCH_RE = re.compile(r"^/research(?:\s+(.*))?$", re.I | re.S)


def _parse_research(text):
    """Parse an explicit ``/research …`` control command. Returns (kind, arg) —
    kind in {research_new, research_done, research_status} — or None. The caller
    gates this on the research flag, so with the flag off ``/research …`` is
    never intercepted and flows as ordinary text."""
    m = _RESEARCH_RE.match(str(text or "").strip())
    if not m:
        return None
    arg = (m.group(1) or "").strip()
    low = arg.lower()
    if low in ("done", "finish", "finished", "synthesize", "synthesise", "wrap up"):
        return ("research_done", None)
    if low in ("", "status", "plan"):
        return ("research_status", None)
    return ("research_new", arg)


# R2: optional LLM decomposition of a research goal into sub-questions.
_DECOMPOSE_TIMEOUT_S = 15.0
_DECOMPOSE_MAX_TOKENS = 900   # v1.19.2: must cover reasoning tokens
_DECOMPOSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "items": {"type": "array", "items": {"type": "string"},
                  "description": "2-6 focused, independently web-searchable "
                                 "sub-questions"},
    },
    "required": ["items"],
}
_DECOMPOSE_INSTRUCTIONS = (
    "You break a research GOAL into a short checklist of focused, independently "
    "web-searchable sub-questions. Return 2-6 items, each a concrete question or "
    "fact to find — NO meta-steps like 'gather information', 'compare results', "
    "or 'write the answer'. Keep each item under 12 words. Return ONLY the JSON "
    "object {\"items\": [ ... ]}.")


def _decompose_plan(cfg, goal):
    """R2: LLM-decompose a research goal into sub-question texts (may be empty →
    caller falls back to a single-item plan). One bounded structured call, temp 0.
    Never raises."""
    ctx = _ctx
    llm = getattr(ctx, "llm", None) if ctx is not None else None
    if llm is None or not hasattr(llm, "complete_structured"):
        return []
    try:
        from agent.plugin_llm import PluginLlmTextInput
    except Exception:
        return []
    try:
        cap = max(1, int(cfg.get("plan_max_items") or 6))
        res = llm.complete_structured(
            instructions=_DECOMPOSE_INSTRUCTIONS,
            input=[PluginLlmTextInput(text=str(goal)[:600])],
            json_mode=True,
            json_schema=_DECOMPOSE_SCHEMA,
            schema_name="research_plan",
            temperature=0.0,
            max_tokens=_DECOMPOSE_MAX_TOKENS,
            timeout=_DECOMPOSE_TIMEOUT_S,
            purpose="router.research_plan",
        )
        parsed = getattr(res, "parsed", None)
        items = parsed.get("items") if isinstance(parsed, dict) else None
        out = []
        for it in (items or []):
            t = _oneline(it)
            if t and t.lower() not in (x.lower() for x in out):
                out.append(t)
        return out[:cap]
    except Exception:
        logger.debug("topics: research decompose failed; single-item plan",
                     exc_info=True)
        return []


# R4: autostore — amortized durable-fact extraction (inbox-only; human merge gate).
_CURATE_TIMEOUT_S = 15.0
_CURATE_MAX_TOKENS = 800      # v1.19.2: must cover reasoning tokens
_CURATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "facts": {"type": "array", "items": {"type": "string"},
                  "description": "durable first-person facts the USER stated about "
                                 "themselves, their life, work, or preferences"},
    },
    "required": ["facts"],
}
_CURATE_INSTRUCTIONS = (
    "From the USER's message, extract ONLY durable, first-person facts the user "
    "stated about themselves — their life, relationships, work, tools, or lasting "
    "preferences — that would still be true next month. Do NOT include questions, "
    "transient state, task instructions, or anything the assistant said. Prefer "
    "returning 0 facts over guessing. Return ONLY the JSON object {\"facts\": "
    "[ ... ]}, each a short third-person statement like 'User rides a BMW "
    "R1250GS'.")


def _extract_facts(cfg, user_message, assistant_response=""):
    """R4 autostore: one bounded structured call extracting durable first-person
    user facts (temp 0, schema-constrained). Returns a list of fact strings
    (possibly empty). Never raises."""
    ctx = _ctx
    llm = getattr(ctx, "llm", None) if ctx is not None else None
    if llm is None or not hasattr(llm, "complete_structured"):
        return []
    try:
        from agent.plugin_llm import PluginLlmTextInput
    except Exception:
        return []
    try:
        res = llm.complete_structured(
            instructions=_CURATE_INSTRUCTIONS,
            input=[PluginLlmTextInput(text=str(user_message)[:800])],
            json_mode=True,
            json_schema=_CURATE_SCHEMA,
            schema_name="user_facts",
            temperature=0.0,
            max_tokens=_CURATE_MAX_TOKENS,
            timeout=_CURATE_TIMEOUT_S,
            purpose="router.topic_curate",
        )
        parsed = getattr(res, "parsed", None)
        items = parsed.get("facts") if isinstance(parsed, dict) else None
        out = []
        for it in (items or []):
            t = _oneline(it)
            if t and t.lower() not in (x.lower() for x in out):
                out.append(t[:180])
        return out
    except Exception:
        logger.debug("topics: fact extraction failed", exc_info=True)
        return []


def _seed_plan(cfg, rec, goal):
    """Seed a research topic's plan checklist. Default: a single item = the raw
    goal. When research_decompose=llm, one bounded LLM call fans the goal into up
    to plan_max_items sub-questions (falling back to the single item on any
    failure). Never raises."""
    try:
        g = _oneline(goal)
        if not g:
            return
        cap = max(1, int(cfg.get("plan_max_items") or 6))
        items = []
        if str(cfg.get("research_decompose")) == "llm":
            items = _decompose_plan(cfg, g)
        if not items:
            items = [g]
        rec["plan"] = [{"text": t, "status": "open", "src": ""}
                       for t in items[:cap]]
    except Exception:
        pass


def _research_intent_text(text):
    """R2 auto-detect bridge: True when the message reads as a multi-part
    research request. Delegates to verify_route.research_intent (kept with its
    detector siblings); fail-safe False if that module is unavailable."""
    try:
        from . import verify_route as _vr
        return bool(_vr.research_intent(text))
    except Exception:
        return False


def _apply_research(rv, session_id, now, cfg):
    """Apply a /research control (caller gates on the research flag). Returns the
    resolved research topic record, or None when the turn carries no topic
    context (status/done with nothing active)."""
    kind, arg = rv
    if kind == "research_new" and arg:
        rec = _open_topic(arg[:80], _keywords(arg or ""), now)
        rec["research"] = True
        rec["_synthesize"] = False
        _seed_plan(cfg, rec, arg)
        return rec
    active_tid = _active_topic.get(session_id)
    if active_tid in _topics:
        rec = _topics[active_tid]
        rec["status"] = "open"
        if kind == "research_done":
            # transient (never serialized): drive final synthesis THIS turn even
            # if some plan items are still open (the user said they're satisfied).
            rec["_synthesize"] = True
        return rec
    return None  # status / done with no active topic → no injection


def _route(session_id, turn_id, user_message, history, cfg):
    """Resolve the active topic for this turn: memo → /topic override → gates →
    _classify → open/route. Returns the topic record, or None when the turn
    should carry no topic context (e.g. ``/topic close``)."""
    now = time.time()
    _load_store(cfg)  # lazy: bring the durable manifest into the hot cache
    memo = _tp_turn.get((session_id, turn_id))
    if memo:
        return _topics.get(memo)
    text = str(user_message or "").strip()
    rec = None
    # R1: /research control commands (only when the research flag is on)
    if str(cfg.get("research")) == "on":
        rv = _parse_research(text)
        if rv is not None:
            rec = _apply_research(rv, session_id, now, cfg)
            if rec is None:
                return None
    auto_ok = False  # R2: only the classify/open path is eligible for auto-promo
    if rec is not None:
        pass  # research branch already resolved rec → fall to the common tail
    elif (ov := _parse_override(text)) is not None:
        rec = _apply_override(ov, session_id, now, cfg)
        if rec is None:
            return None
    else:
        auto_ok = True
        kws = _keywords(text)
        opens = _open_recs()
        active_tid = _active_topic.get(session_id)
        if opens and len(opens) == 1 and (_is_continuation(text) or not kws):
            # single open topic + a continuation → stay, no classify
            rec = opens[0]
        elif opens:
            decision = _classify(text, opens, _recent_user_texts(history),
                                 active_tid, cfg)
            if decision.get("action") == "route" \
                    and decision.get("topic_id") in _topics:
                rec = _topics[decision["topic_id"]]
            else:
                rec = _resolve_new(cfg, text, kws, decision, now)
        else:
            # no open topics → resume a matching dormant one, or open fresh
            rec = _resolve_new(cfg, text, kws, None, now)
    # R2: auto-promote a FRESH (first-turn) topic to research when research_auto
    # is on and the message reads as a multi-part research request. Only on the
    # classify/open path (never overriding an explicit /topic or /research), and
    # only turn 0 so it can't hijack an established conversation.
    if (auto_ok and str(cfg.get("research_auto")) == "on"
            and not rec.get("research") and int(rec.get("turns", 0)) == 0
            and _research_intent_text(text)):
        rec["research"] = True
        rec["_synthesize"] = False
        _seed_plan(cfg, rec, text)
    _rehydrate(cfg, rec)  # load full body on a dormant→open resume
    rec["last_active"] = now
    rec["status"] = "open"
    _active_topic[session_id] = rec["id"]
    _tp_turn[(session_id, turn_id)] = rec["id"]
    _prune(_active_topic, now)
    _prune(_tp_turn, now)
    _evict_if_needed(cfg, now)
    _persist_topic(cfg, rec)  # write-through
    return rec


# ---------------------------------------------------------------------------
# Injected context block
# ---------------------------------------------------------------------------

_VERIFY_LINE = ("(Sources above carry their origin as [src: url] — rely on "
                "those. For ANY other specific claim — a citation, quote, named "
                "source, date, or figure — verify it with a tool before "
                "asserting; never invent a source.)")


_PROV_RE = re.compile(r"^(.*?)\s+[—\-]\s+(https?://\S+)\s*$")


def _to_provenance(finding):
    """harvest_findings gives 'title — url'; keep only url-bearing findings,
    reformatted as 'title [src: url]'. Returns None when there's no source."""
    m = _PROV_RE.match(str(finding or "").strip())
    if not m:
        return None
    claim = _oneline(m.group(1))[:180]
    return "%s [src: %s]" % (claim, m.group(2).strip()) if claim else None


# v1.19.2: the roll-up now returns a TITLE alongside the summary.
#
# A topic's title used to be `_title_from` forever: the first four stopword-
# stripped tokens of the OPENING message, title-cased, never revisited. Opening
# messages are commands, so the tokens are the ask rather than the subject — a
# six-turn OSINT investigation was titled "Now Find Everything Online", and a
# typo'd first message produced "Mark Fnished". The roll-up is the right place
# to fix it: it already runs every summarize_every turns, off the critical path
# (post_llm_call, after the answer), and by then the thread has enough content
# to earn a real name. Same one call — the title rides the existing budget.
_SUMMARY_TIMEOUT_S = 20.0
_SUMMARY_MAX_TOKENS = 1200   # covers reasoning + a 120-word summary + a title
_ROLLUP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string",
                    "description": "<=120 words, factual, no preamble, no "
                                   "markdown headers"},
        "title": {"type": "string",
                  "description": "a short noun-phrase name for this thread, "
                                 "3-6 words, naming its SUBJECT not the user's "
                                 "request (e.g. 'vLLM speculative decoding', "
                                 "not 'Tell Me About Vllm')"},
    },
    "required": ["summary"],
}
# The word "json" is LOAD-BEARING: DeepSeek rejects response_format=json_object
# with HTTP 400 ("Prompt must contain the word 'json' in some form") unless it
# appears in the prompt, and hermes does not inject it. Without this the
# structured call 400s and the roll-up silently degrades to summary-only with no
# retitle. The classify and curate instructions already satisfy this.
_ROLLUP_INSTRUCTIONS = (
    "Summarize and name one conversation thread. Return ONLY a JSON object "
    "with keys \"summary\" (<=120 words, factual, no preamble, no markdown "
    "headers) and \"title\" (a short noun-phrase name for the thread, 3-6 "
    "words, naming its SUBJECT rather than the user's request — e.g. \"vLLM "
    "speculative decoding\", not \"Tell Me About Vllm\"). Always write the "
    "title in ENGLISH, even when the thread itself was in another language.")


def _rollup_summary(cfg, rec):
    """Amortized ## summary roll-up (gated by summarize=on, fired every
    summarize_every turns). Rewrites the running summary from the recent
    exchanges and re-titles the thread from what it turned out to be about.
    On any failure the old summary AND the old title stand. Never raises."""
    if str(cfg.get("summarize")) != "on":
        return
    llm = getattr(_ctx, "llm", None) if _ctx is not None else None
    if llm is None:
        return
    try:
        recent = "\n".join("- you: %s / me: %s" % (u, a)
                           for u, a in (rec.get("recent") or []))
        if not recent:
            return
        prompt = ("Maintain a running summary of one ongoing conversation "
                  "topic, and name the thread.\n\nCurrent name: %s\nPrior "
                  "summary: %s\n\nRecent exchanges:\n%s"
                  % (rec.get("title") or "", rec.get("summary") or "(none)",
                     recent))
        summary = title = ""
        structured_ran = False
        if hasattr(llm, "complete_structured"):
            try:
                from agent.plugin_llm import PluginLlmTextInput
                res = llm.complete_structured(
                    instructions=_ROLLUP_INSTRUCTIONS,
                    input=[PluginLlmTextInput(text=prompt)],
                    json_mode=True, json_schema=_ROLLUP_SCHEMA,
                    schema_name="topic_rollup", temperature=0.2,
                    max_tokens=_SUMMARY_MAX_TOKENS, timeout=_SUMMARY_TIMEOUT_S,
                    purpose="router.topic_summary")
                structured_ran = True
                parsed = getattr(res, "parsed", None)
                if isinstance(parsed, dict):
                    summary = str(parsed.get("summary") or "")
                    title = str(parsed.get("title") or "")
            except Exception:
                logger.debug("topics: structured roll-up failed; trying plain",
                             exc_info=True)
        if not summary and not structured_ran and hasattr(llm, "complete"):
            # Plain completion ONLY when structured is unavailable or raised —
            # never as a retry of a structured call that ran and came back
            # empty, which would double the cost of every empty response.
            # Summary only: no title rather than a guessed one.
            res = llm.complete(
                messages=[{"role": "user", "content":
                           prompt + "\n\nUpdated summary:"}],
                temperature=0.2, max_tokens=_SUMMARY_MAX_TOKENS,
                timeout=_SUMMARY_TIMEOUT_S, purpose="router.topic_summary")
            summary = str(getattr(res, "text", "") or "")
        if not summary.strip():
            logger.warning("topics: roll-up for t#%05d returned nothing (budget "
                           "%d tokens) — summary unchanged",
                           int(rec.get("id") or 0), _SUMMARY_MAX_TOKENS)
            return
        rec["summary"] = _oneline(summary)[:900]
        _retitle(rec, title)
    except Exception:
        logger.debug("topics: summary roll-up failed", exc_info=True)


def _retitle(rec, title):
    """Adopt a roll-up's proposed title. Never overrides a name the USER chose
    with `/topic new <title>`, and never accepts an empty or absurd one."""
    try:
        t = _oneline(title)[:80].strip(" .\"'")
        if not t or rec.get("_user_titled"):
            return
        if len(t.split()) > 10:
            return
        if t == rec.get("title"):
            return
        logger.info("topics: t#%05d retitled %r -> %r",
                    int(rec.get("id") or 0), rec.get("title"), t)
        rec["title"] = t
        rec["slug"] = _slug(t)
    except Exception:
        pass


def _curation_note(cfg, rec):
    """On topic close (or a durable fact), drop an inbox/hermes curation note so
    tools/merge_inboxes.py folds it into the human-reviewed wiki/. NEVER writes
    wiki/, index.md, or log.md directly. Never raises."""
    try:
        wiki = str(cfg.get("wiki_dir") or DEFAULTS["wiki_dir"])
        inbox = os.path.join(wiki, "inbox", "hermes")
        os.makedirs(inbox, exist_ok=True)
        slug = rec.get("slug") or _slug(rec.get("title"))
        name = "topic-t%05d-%s" % (int(rec["id"]), slug)
        out = [
            "---",
            "target: jobs/%s" % slug,   # jobs is a valid merge SECTION
            "name: %s" % name,
            "---",
            "# %s (topic t#%05d)" % (rec.get("title") or "", int(rec["id"])),
        ]
        if rec.get("summary"):
            out.append(_oneline(rec["summary"]))
        facts = rec.get("facts") or []
        if facts:
            out += ["", "Facts:"] + ["- " + _oneline(f) for f in facts[:_FACTS_CAP]]
        finds = rec.get("findings") or []
        if finds:
            out += ["", "Key findings:"] + ["- " + _oneline(f) for f in finds[:6]]
        # R4: on a research topic, carry the plan checklist (done items with their
        # source, plus any still-open loose ends) and the scratch notes into the
        # note so the human merge pass sees what was and wasn't covered.
        if rec.get("research"):
            plan = rec.get("plan") or []
            if plan:
                out += ["", "Research checklist:"]
                for it in plan:
                    box = "x" if it.get("status") == "done" else " "
                    src = it.get("src") or ""
                    tail = " [src: %s]" % src if src else ""
                    out.append("- [%s] %s%s" % (box, _oneline(it.get("text")), tail))
            scratch = rec.get("scratch") or []
            if scratch:
                out += ["", "Working notes:"] + ["- " + _oneline(s)
                                                 for s in scratch[:_SCRATCH_CAP]]
        _atomic_write(os.path.join(inbox, name + ".md"), "\n".join(out) + "\n")
    except Exception:
        logger.debug("topics: curation note failed", exc_info=True)


# ---------------------------------------------------------------------------
# v1.19.0: topic listing — the ``topics_list`` tool and ``/topic list``.
#
# Both read the same hot manifest the classifier already loads: no LLM call, no
# network, no disk fan-out beyond the one _index.json the store keeps warm.
#
# Scope is always the CALLING agent's own wiki_dir, so a profile bot can only
# ever enumerate the threads in its own zone vault — the per-profile isolation
# contract holds here by construction, not by a filter that could be forgotten.
# ---------------------------------------------------------------------------
_LIST_SORTS = ("last_active", "turns", "id", "created")
_LIST_STATUSES = ("open", "dormant", "closed", "all")
_LIST_LIMIT_MAX = 100
_LIST_DEFAULT_LIMIT = 20
_DUR_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([mhdw])$", re.I)
_DUR_HOURS = {"m": 1 / 60.0, "h": 1.0, "d": 24.0, "w": 168.0}


def parse_duration_hours(text):
    """'30m'/'12h'/'7d'/'2w' -> hours as a float. None when unparseable."""
    m = _DUR_RE.match(str(text or "").strip())
    if not m:
        return None
    try:
        return float(m.group(1)) * _DUR_HOURS[m.group(2).lower()]
    except Exception:
        return None


def _age_str(seconds):
    """Compact age for display: 40s / 12m / 3h / 6d / 2w."""
    try:
        s = max(0.0, float(seconds))
    except Exception:
        return "?"
    for unit, size in (("w", 604800.0), ("d", 86400.0), ("h", 3600.0), ("m", 60.0)):
        if s >= size:
            return "%d%s" % (int(s // size), unit)
    return "%ds" % int(s)


def list_topics(cfg, status="all", since_hours=None, contains=None,
                sort="last_active", order="desc", limit=_LIST_DEFAULT_LIMIT,
                now=None):
    """Filtered, sorted view over the topic manifest.

    Returns ``(rows, matched)`` — the page of rows after the limit, and how many
    matched before it, so a caller can say "showing 20 of 57" instead of
    silently truncating. Never raises: a broken store yields ([], 0).
    """
    try:
        _load_store(cfg)
        now = float(now if now is not None else time.time())
        status = str(status or "all").lower()
        if status not in _LIST_STATUSES:
            status = "all"
        sort = str(sort or "last_active").lower()
        if sort not in _LIST_SORTS:
            sort = "last_active"
        desc = str(order or "desc").lower() != "asc"
        try:
            limit = int(limit)
        except Exception:
            limit = _LIST_DEFAULT_LIMIT
        limit = max(1, min(_LIST_LIMIT_MAX, limit))
        needle = str(contains or "").strip().lower()
        cutoff = None
        if since_hours is not None:
            try:
                cutoff = now - float(since_hours) * 3600.0
            except Exception:
                cutoff = None

        matched = []
        for rec in _topics.values():
            try:
                if status != "all" and rec.get("status") != status:
                    continue
                last_active = float(rec.get("last_active") or 0)
                if cutoff is not None and last_active < cutoff:
                    continue
                if needle:
                    hay = " ".join([
                        str(rec.get("title") or ""), str(rec.get("slug") or ""),
                        str(rec.get("summary") or ""),
                        " ".join(str(e) for e in (rec.get("entities") or [])),
                    ]).lower()
                    if needle not in hay:
                        continue
                matched.append(rec)
            except Exception:
                continue

        def key(rec):
            if sort == "turns":
                return float(rec.get("turns") or 0)
            if sort in ("id", "created"):
                return float(rec.get("id") if sort == "id"
                             else (rec.get("created") or 0))
            return float(rec.get("last_active") or 0)

        matched.sort(key=key, reverse=desc)
        rows = []
        for rec in matched[:limit]:
            last_active = float(rec.get("last_active") or 0)
            sessions = [s for s in (rec.get("sessions") or []) if s]
            rows.append({
                "id": int(rec.get("id") or 0),
                "tag": "t#%05d" % int(rec.get("id") or 0),
                "title": (rec.get("title") or "")[:80],
                "status": rec.get("status") or "dormant",
                "turns": int(rec.get("turns") or 0),
                "age": _age_str(now - last_active) if last_active else "?",
                "last_active": last_active,
                "research": bool(rec.get("research")),
                # v1.20.0: the hermes session this thread was last spoken in —
                # feed it to the built-in session_search to read the real
                # messages. Absent on threads that predate the field.
                "session_id": sessions[-1] if sessions else None,
            })
        return rows, len(matched)
    except Exception:
        logger.debug("topics: list_topics failed", exc_info=True)
        return [], 0


def render_listing(rows, matched, header="Open topics"):
    """Plain-text listing for the /topic list injection. Compact on purpose —
    it rides in the user-message context block, which is token-capped."""
    if not rows:
        return "%s: none matched." % header
    out = ["%s (showing %d of %d):" % (header, len(rows), matched)]
    for r in rows:
        flags = []
        if r["status"] != "open":
            flags.append(r["status"])
        if r["research"]:
            flags.append("research")
        tail = " [%s]" % ", ".join(flags) if flags else ""
        out.append("- %s  %s  (%d turn%s, %s ago)%s"
                   % (r["tag"], r["title"] or "(untitled)", r["turns"],
                      "" if r["turns"] == 1 else "s", r["age"], tail))
    return "\n".join(out)


def parse_list_args(arg):
    """Parse ``/topic list`` filter tokens into list_topics kwargs.

    Accepts ``open|dormant|closed|all``, ``since:7d``, ``contains:<text>``,
    ``sort:turns``, ``asc|desc``, ``limit:5``. Bare words that aren't keywords
    accumulate into ``contains`` so ``/topic list duplex`` does the obvious
    thing. Unparseable tokens are ignored rather than erroring — this is a
    convenience command, not a CLI.
    """
    kw = {}
    free = []
    for tok in str(arg or "").split():
        low = tok.lower()
        if low in ("list", "status"):
            continue
        if low in _LIST_STATUSES:
            kw["status"] = low
        elif low in ("asc", "desc"):
            kw["order"] = low
        elif low.startswith("since:"):
            hours = parse_duration_hours(tok.split(":", 1)[1])
            if hours is not None:
                kw["since_hours"] = hours
        elif low.startswith("limit:"):
            try:
                kw["limit"] = int(tok.split(":", 1)[1])
            except Exception:
                pass
        elif low.startswith("sort:"):
            val = low.split(":", 1)[1]
            if val in _LIST_SORTS:
                kw["sort"] = val
        elif low.startswith("contains:"):
            free.append(tok.split(":", 1)[1])
        else:
            free.append(tok)
    if free:
        kw["contains"] = " ".join(free)
    return kw


_LIST_HEADER = (
    "[Topic list requested by the user with an explicit /topic list command. "
    "Present these threads to the user as a list, exactly as given — this is "
    "the answer to their message. Keep the t#NNNNN tags: the user references "
    "threads by them. Do not invent threads that are not listed.]\n")


def listing_context(cfg, text, now=None):
    """Return the rendered ``/topic list`` block, or None when the message is
    not a list command. Never raises."""
    try:
        ov = _parse_override(text)
        if ov is None or ov[0] != "list":
            return None
        kw = parse_list_args(ov[1] or "")
        rows, matched = list_topics(cfg, now=now, **kw)
        header = "Topics" if kw.get("status", "all") == "all" \
            else "%s topics" % kw["status"].capitalize()
        return _LIST_HEADER + render_listing(rows, matched, header=header)
    except Exception:
        logger.debug("topics: listing_context failed", exc_info=True)
        return None


TOPICS_LIST_SCHEMA = {
    "name": "topics_list",
    "description": (
        "List this assistant's own conversation threads (topics) with optional "
        "filters and sorting. Use when the user asks what threads/topics exist, "
        "what they were working on, or to find a past thread. Returns thread "
        "tags (t#NNNNN) the user can reference. Local and instant — no search."),
    "parameters": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": list(_LIST_STATUSES),
                       "description": "open = active now, dormant = idle but "
                                      "resumable, closed = ended. Default all."},
            "since_hours": {"type": "number",
                            "description": "only threads active within this "
                                           "many hours (24 = today, 168 = week)"},
            "contains": {"type": "string",
                         "description": "case-insensitive match on title, "
                                        "summary, or keywords"},
            "sort": {"type": "string", "enum": list(_LIST_SORTS),
                     "description": "default last_active"},
            "order": {"type": "string", "enum": ["asc", "desc"],
                      "description": "default desc (most recent first)"},
            "limit": {"type": "integer",
                      "description": "max threads to return, 1-100 (default 20)"},
        },
        "additionalProperties": False,
    },
}
# Pivot path, stated in the description so the model knows the two tools compose:
# topics_list -> a t#NNNNN tag (topics_show) and a session_id (session_search).
TOPICS_LIST_SCHEMA["description"] += (
    " Each result carries a t#NNNNN tag for topics_show and, when known, the "
    "session_id to read the original messages with session_search.")


def topics_list_handler(args, **_kw):
    """``topics_list`` tool handler. Returns a JSON string. Never raises."""
    try:
        cfg = _cfg()
        if cfg.get("enabled") != "on":
            return json.dumps({"error": "Topic tracking is not enabled for "
                                        "this assistant.", "topics": []})
        args = args if isinstance(args, dict) else {}
        rows, matched = list_topics(
            cfg,
            status=args.get("status", "all"),
            since_hours=args.get("since_hours"),
            contains=args.get("contains"),
            sort=args.get("sort", "last_active"),
            order=args.get("order", "desc"),
            limit=args.get("limit", _LIST_DEFAULT_LIMIT))
        for r in rows:
            r.pop("last_active", None)  # age is the useful form; epoch is noise
        return json.dumps({"topics": rows, "matched": matched,
                           "returned": len(rows)}, ensure_ascii=False)
    except Exception:
        logger.debug("topics: topics_list handler failed", exc_info=True)
        return json.dumps({"error": "Could not read the topic store.",
                           "topics": []})


TOPICS_SHOW_SCHEMA = {
    "name": "topics_show",
    "description": (
        "Read one conversation thread (topic) in full: its summary, durable "
        "facts, sourced findings, and recent exchanges. Takes the t#NNNNN tag "
        "or the bare number from topics_list. Use when the user asks what a "
        "thread was about or to pick a past thread back up. Local and instant. "
        "The returned session_id can be passed to session_search to read the "
        "original messages verbatim."),
    "parameters": {
        "type": "object",
        "properties": {
            "topic_id": {"type": "string",
                         "description": "thread tag, e.g. 't#00042', 't00042' "
                                        "or '42'"},
        },
        "required": ["topic_id"],
        "additionalProperties": False,
    },
}


def show_topic(cfg, topic_id):
    """Full view of one thread, read from the durable store WITHOUT disturbing
    routing — it never marks a topic active, open, or recently used. Returns a
    plain dict, or None when the id is unknown. Never raises."""
    try:
        tid = _parse_topic_id(topic_id)
        if tid is None:
            return None
        _load_store(cfg)
        rec = _topics.get(tid)
        # Prefer the file: a manifest-only record has an empty body, and reading
        # it here must not mutate the hot cache the live turn is routing with.
        parsed = None
        try:
            path = _topic_path(cfg, tid)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    parsed = _parse_topic_md(f.read())
        except Exception:
            parsed = None
        body = parsed if (parsed and parsed.get("id") == tid) else None
        if body is None and rec is None:
            return None
        # Split the authorities the way the plugin itself does: the MANIFEST
        # owns the frontmatter (title/status/turns — _load_store reads them from
        # there, and a retitle patches the index), the FILE owns the body. Taking
        # the title from the file made topics_show disagree with topics_list
        # whenever a live turn re-persisted a hot record after a backfill.
        head = rec or body
        body = body or rec
        sessions = [s for s in (body.get("sessions")
                                or (rec or {}).get("sessions") or []) if s]
        last_active = float((rec or {}).get("last_active") or 0)
        out = {
            "id": tid,
            "tag": "t#%05d" % tid,
            "title": head.get("title") or body.get("title") or "",
            "status": head.get("status") or "dormant",
            "turns": int(head.get("turns") or 0),
            "entities": list(head.get("entities")
                             or body.get("entities") or [])[:20],
            "summary": body.get("summary") or "",
            "facts": list(body.get("facts") or [])[:_FACTS_CAP],
            "findings": list(body.get("findings") or [])[:_FIND_CAP],
            "exchanges": [{"user": u, "assistant": a}
                          for u, a in (body.get("recent") or [])],
            "session_id": sessions[-1] if sessions else None,
            "sessions": sessions,
        }
        if last_active:
            out["age"] = _age_str(time.time() - last_active)
        if body.get("research"):
            out["plan"] = list(body.get("plan") or [])
            out["scratchpad"] = list(body.get("scratch") or [])
        return out
    except Exception:
        logger.debug("topics: show_topic failed", exc_info=True)
        return None


def topics_show_handler(args, **_kw):
    """``topics_show`` tool handler. Returns a JSON string. Never raises."""
    try:
        cfg = _cfg()
        if cfg.get("enabled") != "on":
            return json.dumps({"error": "Topic tracking is not enabled for "
                                        "this assistant."})
        args = args if isinstance(args, dict) else {}
        rec = show_topic(cfg, args.get("topic_id"))
        if rec is None:
            return json.dumps({"error": "No such thread. Use topics_list to "
                                        "see the available t#NNNNN tags."})
        if not (rec["summary"] or rec["facts"] or rec["findings"]
                or rec["exchanges"]):
            # A thread whose body the pre-v1.19.1 eviction bug erased: say so
            # rather than presenting an empty record as if nothing was said.
            rec["note"] = ("Only this thread's index entry survives; its stored "
                           "body is empty. The original messages may still be "
                           "readable with session_search.")
        return json.dumps(rec, ensure_ascii=False)
    except Exception:
        logger.debug("topics: topics_show handler failed", exc_info=True)
        return json.dumps({"error": "Could not read the topic store."})


def topics_list_available():
    """check_fn: the tool advertises itself only while topics are enabled.
    Registry TTL-caches this for ~30s, so flipping the flag propagates without
    a restart — matching how every other topics flag behaves."""
    try:
        return _cfg().get("enabled") == "on"
    except Exception:
        return False


def register_tool(ctx):
    """Register ``topics_list`` and ``topics_show``. Symbol-guarded and
    fail-safe: a hermes without register_tool, or any registration error, leaves
    the plugin fully functional minus these tools. Each registers
    independently, so one failing does not take the other down."""
    if not hasattr(ctx, "register_tool"):
        logger.info("topics: ctx.register_tool absent — topics tools skipped")
        return False
    ok = 0
    for name, schema, handler in (
            ("topics_list", TOPICS_LIST_SCHEMA, topics_list_handler),
            ("topics_show", TOPICS_SHOW_SCHEMA, topics_show_handler)):
        try:
            ctx.register_tool(
                name=name,
                toolset="topics",
                schema=schema,
                handler=handler,
                check_fn=topics_list_available,
                description=schema["description"],
                emoji="🧵")
            ok += 1
        except Exception:
            logger.warning("topics: %s registration failed; continuing without "
                           "it", name, exc_info=True)
    if ok:
        logger.info("topics: registered %d topic tool(s)", ok)
    return ok > 0


_BLOCK_HEADER = (
    "[Background continuity notes for the thread this message belongs to — NOT "
    "part of the user's message and NOT a question. Answer the user's actual "
    "message using these notes for continuity. Do not mention these notes, the "
    "thread, or any topic number, and never treat a topic id as something to "
    "look up. If the user asks how you know something shown here, attribute it "
    "to earlier in your conversation with them — never say it came from notes, "
    "memory, a system prompt, or context you were given.]")

# R3: header used ONLY on a dormant RESUME with resume_surface on. It relaxes the
# "never name the thread/topic-number" prohibition (the t#NNNNN tag is already
# visible on the user's own prior replies, so naming it leaks nothing) while
# keeping the "never narrate the mechanism" prohibition absolute.
_BLOCK_HEADER_RESUME = (
    "[Background continuity notes for a thread you are RESUMING after a gap — NOT "
    "part of the user's message and NOT a question. You genuinely remember this "
    "thread; treat it as your own recollection. You MAY acknowledge the thread and "
    "refer to it by the t#NNNNN tag the user already sees on your replies. But "
    "NEVER reveal the mechanism: do not say a note, memory, system prompt, or "
    "context was given to you this turn, and do not say you 'looked it up' — "
    "attribute your continuity to remembering your earlier conversation with them.]")

# R3: flag-gated self-knowledge injection (self_knowledge on). A compact, honest
# statement that the model has cross-conversation memory — the INERT alternative
# to editing SOUL.md live, reconciled with the anti-disclosure rule (remembering,
# not fetching; never narrate the plumbing).
_SELF_KNOWLEDGE_NOTE = (
    "[About yourself: you carry memory across conversations. Threads you've worked "
    "on come back to you automatically when they become relevant — you remember "
    "them, you do not fetch or look them up. Never invent a memory you don't have, "
    "and never say a note, file, or system prompt handed it to you: from your side "
    "it is simply remembering.]")


def _append_plan_block(lines, rec, cfg):
    """Render the research plan into the injected block. Two modes: WORK (open
    items remain and the user hasn't said done) directs the model to search each
    open item then synthesize; SYNTHESIZE (all done, or /research done) directs a
    final cited answer. The rendered checklist is held to plan_token_cap. Never
    raises (best-effort append)."""
    try:
        plan = rec.get("plan") or []
        if not plan:
            return
        open_items = [it for it in plan if it.get("status") != "done"]
        synth = bool(rec.get("_synthesize")) or not open_items

        def _render(it):
            box = "x" if it.get("status") == "done" else " "
            src = it.get("src") or ""
            tail = " [src: %s]" % src if src else ""
            return "- [%s] %s%s" % (box, _oneline(it.get("text")), tail)

        if synth:
            lines.append("Research plan complete — now write the final answer, "
                         "drawing it together from the sources below and citing "
                         "each [src: url]. Do not re-search already-covered points.")
        else:
            # WORK mode. Tuning is delicate (research-eval smoke): an unbounded
            # 'search each item' directive made the model search 13x without ever
            # synthesizing, while a hard 'stop once you can answer' made it skip
            # searching entirely and answer from (unverified) memory. The balance
            # below insists on GROUNDING each point with a search (the whole point
            # of research) while nudging a timely synthesis after a few searches.
            lines.append("Below is a checklist of points to cover. Verify each "
                         "point with web_search — do not rely on memory for "
                         "specs, prices, dates, or current facts. When every "
                         "point on the checklist is covered, write one complete, "
                         "well-organized answer that cites your sources.")
        rendered = "\n".join(_render(it) for it in plan)
        pcap = max(80, int(cfg.get("plan_token_cap", 180)) * 4)
        if len(rendered) > pcap:
            rendered = rendered[:pcap].rsplit("\n", 1)[0] + "\n- (…)"
        lines.append(rendered)
    except Exception:
        pass


_PROV_SPLIT_RE = re.compile(r"^(.*?)\s*\[src:\s*(\S+?)\]\s*$")


def _reconcile_plan(cfg, rec, new_findings):
    """R1: keyword-overlap each newly-sourced finding against each OPEN plan item;
    flip the best match to done and stamp its src when overlap >= plan_overlap.
    Plugin-authored (the model never edits the checklist). Flag-gated; never
    raises."""
    try:
        if str(cfg.get("research")) != "on" or not rec.get("research"):
            return
        plan = rec.get("plan") or []
        if not plan or not new_findings:
            return
        thr = _plan_overlap(cfg)
        for prov in new_findings:
            m = _PROV_SPLIT_RE.match(str(prov or "").strip())
            claim = m.group(1) if m else str(prov or "")
            url = m.group(2) if m else ""
            fkw = set(_keywords(claim))
            if not fkw:
                continue
            best, best_ov, best_shared = None, 0.0, 0
            for it in plan:
                if it.get("status") == "done":
                    continue
                ikw = set(_keywords(it.get("text") or ""))
                if not ikw:
                    continue
                # Overlap COEFFICIENT (normalize by the smaller set), not
                # by the item: dividing by the item made a broad single-item
                # plan — exactly what /research seeds when decompose is off —
                # mathematically unsatisfiable (8-keyword goal vs a 3-keyword
                # finding caps at ~0.25 < 0.34, so it never completed; measured
                # in the multi-turn eval, plan stayed [ ] across every turn).
                shared = len(fkw & ikw)
                ov = shared / max(1, min(len(fkw), len(ikw)))
                if ov > best_ov:
                    best, best_ov, best_shared = it, ov, shared
            # Require a real lexical anchor (>=2 shared terms) so a single
            # incidental word can't tick an item off; 1 is allowed only when
            # the smaller side genuinely IS one term.
            anchored = best_shared >= 2 or (
                best is not None and best_shared >= 1
                and min(len(fkw), len(set(_keywords(best.get("text") or "")))) == 1)
            if best is not None and best_ov >= thr and anchored:
                best["status"] = "done"
                if url and not best.get("src"):
                    best["src"] = url
    except Exception:
        pass


def build_topic_block(rec, cfg, voice=False):
    """Assemble the capped per-topic context block injected into the user
    message. It is framed as SILENT background memory (no topic id, explicit
    'don't mention this / just answer their message') — an earlier framing that
    led with '# Active topic t#NNNNN' made the model mistake the topic id for
    something the user was asking about. Fixed section order; hard-truncated to
    block_token_cap (est len//4). Never raises.

    R3: on a dormant RESUME with resume_surface on, the header relaxes to allow
    naming the (already-visible) t#NNNNN tag; self_knowledge/active_recall append
    optional framing lines. All default off → byte-identical to before."""
    try:
        resumed = (bool(rec.get("_resumed"))
                   and str(cfg.get("resume_surface")) == "on")
        lines = [_BLOCK_HEADER_RESUME if resumed else _BLOCK_HEADER]
        # R3: flag-gated self-knowledge (the inert alternative to a live SOUL edit)
        if str(cfg.get("self_knowledge")) == "on":
            lines.append(_SELF_KNOWLEDGE_NOTE)
        title = _oneline(rec.get("title"))
        if title:
            lines.append("Ongoing topic: %s" % title)
        summ = _oneline(rec.get("summary"))
        if summ:
            lines.append("So far: %s" % summ)
        facts = rec.get("facts") or []
        if facts:
            # R3: active_recall widens the fact budget to recall_facts_max
            fact_cap = 5
            if str(cfg.get("active_recall")) == "on":
                fact_cap = max(1, int(cfg.get("recall_facts_max") or 5))
            lines.append("Known facts:")
            lines.extend("- " + _oneline(f) for f in facts[:fact_cap])
        findings = rec.get("findings") or []
        if findings:
            lines.append("Verified sources (prefer these; each carries [src: url]):")
            lines.extend("- " + _oneline(f) for f in findings[:4])
        recent = rec.get("recent") or []
        if recent:
            lines.append("Recent exchanges in this thread:")
            for u, a in recent[-3:]:
                lines.append("- you: " + _oneline(u))
                if a:
                    lines.append("  me: " + _oneline(a))
        elif title or summ:
            # Cold resume (dormant topic, no recent exchanges loaded): synthesize
            # an in-conversation anchor so, if asked "how do you know?", the model
            # has something honest to attribute to instead of leaking the note
            # (measured load-bearing in the round-2 disclosure-leak experiment).
            lines.append("Earlier in this same conversation, you and I were "
                         "discussing this — pick the thread back up naturally.")
        # R3: resume acknowledgment invite (only when the relaxed header is active).
        # The t#NNNNN tag is suppressed in voice sessions (TTS would read it aloud),
        # mirroring badge_for's voice guard.
        if resumed:
            tag = "" if voice else " (t#%05d)" % int(rec["id"])
            lines.append("You're picking this thread back up after a gap%s — "
                         "acknowledge it naturally as something you remember, "
                         "not as something you were just told." % tag)
        # R3: active_recall permission — surface a known fact as *their* telling.
        if str(cfg.get("active_recall")) == "on" and facts:
            lines.append("You MAY proactively bring up a relevant fact above when "
                         "it helps, phrased as recalling what they told you earlier.")
        # R1: research plan checklist (flag-gated; carved from block_token_cap
        # under its own plan_token_cap sub-budget so a long plan can't starve
        # the summary/findings above).
        if str(cfg.get("research")) == "on" and rec.get("research"):
            _append_plan_block(lines, rec, cfg)
        lines.append(_VERIFY_LINE)
        block = "\n".join(lines)
        cap_chars = max(200, int(cfg.get("block_token_cap", 250)) * 4)
        if len(block) > cap_chars:
            block = block[:cap_chars].rsplit("\n", 1)[0] + "\n(…)"
        return block
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Plugin hooks
# ---------------------------------------------------------------------------

def _resume_signal_ok(rec, text, cfg):
    """v1.16.0 (W12) strong-tier injection gate. RETIRED from the injection
    path in v1.16.1 (strong tier is now blanket-inert again, per _tp_pre_llm);
    kept only as a pure, well-tested predicate in case a future opt-in resume
    is reintroduced. It is no longer consulted on any live turn.

    When it was live it injected the resumption block only on an OBSERVED
    strong resumption signal:
      * the topic actually has accumulated content to resume, AND
      * the message is a short CONTINUATION, AND
      * its keywords overlap the topic entities/title >= inject_overlap_strong.
    A trivial one-off prompt shows none of these -> no injection -> no derail
    (batch4 r12/r44). Fail-safe: on error return False (match strong's v1.15.0
    INERT — never inject noise into a frontier turn on uncertainty)."""
    try:
        has_content = bool(rec.get("summary") or rec.get("recent")
                           or rec.get("findings")
                           or int(rec.get("turns", 0)) > 0)
        if not has_content:
            return False
        if not _is_continuation(text):
            return False
        # a SHORT continuation ("continue", "go on", "and why?", "what else") is
        # an unambiguous, user-intended resume of the active thread -> inject.
        if len(str(text or "").split()) <= 4:
            return True
        kws = set(_keywords(text))
        if not kws:
            return True  # pure continuation with no content nouns
        ents = set(rec.get("entities") or [])
        ents |= set(_keywords(rec.get("title") or ""))
        overlap = len(kws & ents) / len(kws)
        return overlap >= float(cfg.get("inject_overlap_strong", 0.6))
    except Exception:
        return False


def _tp_pre_llm(session_id="", turn_id="", user_message="",
                conversation_history=None, **_kw):
    """pre_llm_call: route the inbound message to a topic and return
    ``{"context": <block>}`` for hermes to append to the current-turn user
    message. Returns None (stock turn) when disabled, in a background review,
    empty message, or on any error."""
    try:
        cfg = _cfg()
        if cfg["enabled"] != "on":
            return None
        if _in_background_review(conversation_history):
            return None
        if not str(user_message or "").strip():
            return None
        # P5: remember whether this session is voice_only so badge_for can strip
        # the badge from spoken replies (computed here where platform/sender are
        # available; badge_for only gets session_id).
        try:
            _active_voice[session_id] = _is_voice_session(
                _kw.get("platform"), _kw.get("sender_id"))
        except Exception:
            pass
        # v1.19.0: an explicit `/topic list` is answered directly and BEFORE the
        # tier gate below — the user asked for it by name, so it must work on
        # strong tier too, where continuity injection is blanket-inert. It also
        # runs before _route so a listing never joins or opens a thread.
        listing = listing_context(cfg, user_message)
        if listing is not None:
            logger.info("topics: /topic list served session=%s", session_id or "-")
            return {"context": listing}
        rec = _route(session_id, turn_id, user_message,
                     conversation_history, cfg)
        if not rec:
            return None
        # v1.16.1: the continuity / auto-resume CONTEXT INJECTION is BLANKET-INERT
        # on strong tier — exactly as v1.15.0 kept it. v1.16.0's W12 signal-gated
        # injection re-opened a strong-tier resumption path (`_resume_signal_ok`)
        # that over-fired on 3/44 DeepSeek prompts (batch8: pa_4, comp_1 harmless;
        # clarify_5 "Fix it." matched a topic titled "Fix" and contributed to the
        # 30-call spiral). Routing/classification/storage still run above (_route)
        # on every tier — only the INJECTION is suppressed on strong.
        #   * weak/mid tier: inject as in v1.15.0 (the proven 27B path is
        #     unchanged — its threshold is effectively 0);
        #   * strong tier: never inject (blanket-inert, regardless of any
        #     resumption signal), so no frontier turn is ever derailed by an
        #     unrelated/loosely-matched thread.
        if not _on_weak_host(session_id):
            logger.info("topics: turn routed to t#%05d (%s) session=%s — "
                        "context injection INERT (strong tier, blanket)",
                        int(rec["id"]), (rec.get("title") or "")[:40],
                        session_id or "-")
            return None
        block = build_topic_block(rec, cfg,
                                  voice=bool(_active_voice.get(session_id)))
        if not block:
            return None
        logger.info("topics: turn routed to t#%05d (%s) session=%s",
                    int(rec["id"]), (rec.get("title") or "")[:40],
                    session_id or "-")
        return {"context": block}
    except Exception:
        logger.debug("topics: pre_llm_call failed; stock turn", exc_info=True)
        return None


def _tp_post_llm(session_id="", turn_id="", user_message="",
                 assistant_response="", conversation_history=None, **_kw):
    """post_llm_call: fold this turn into the active topic (entities, recent
    exchange, turn count). P1: in-memory. Returns None (observer)."""
    try:
        cfg = _cfg()
        if cfg["enabled"] != "on":
            return None
        if _in_background_review(conversation_history):
            return None
        tid = _tp_turn.get((session_id, turn_id)) or _active_topic.get(session_id)
        if not tid or tid not in _topics:
            return None
        rec = _topics[tid]
        for k in _keywords(user_message):
            if k not in rec["entities"]:
                rec["entities"].append(k)
        rec["entities"] = rec["entities"][:_ENT_CAP]
        uh = _head(user_message)
        if uh:
            rec["recent"].append((uh, _head(assistant_response)))
            rec["recent"] = rec["recent"][-_RECENT_CAP:]
        # P4: harvest this turn's web_search/web_extract findings WITH [src:]
        # provenance (drop any finding that has no source).
        try:
            from . import selfheal as _sh
        except Exception:
            _sh = None
        harvested, new_provs = [], []
        if _sh is not None and hasattr(_sh, "harvest_findings"):
            try:
                harvested = list(_sh.harvest_findings(conversation_history, limit=3))
            except Exception:
                harvested = []
        for f in harvested:
            prov = _to_provenance(f)
            if prov and prov not in rec["findings"]:
                rec["findings"].append(prov)
                new_provs.append(prov)
        rec["findings"] = rec["findings"][-_FIND_CAP:]
        # R2: scratchpad capture (flag-gated) — the non-src harvest lines
        # (execute_code stdout, extract text, url-less notes) _to_provenance
        # drops, kept as short working-memory notes on a research topic.
        if str(cfg.get("scratchpad")) == "on" and rec.get("research"):
            for f in harvested:
                if _to_provenance(f) is None:
                    note = _oneline(f)[:160]
                    if note and note not in rec["scratch"]:
                        rec["scratch"].append(note)
            rec["scratch"] = rec["scratch"][-_SCRATCH_CAP:]
        # R1: reconcile this turn's newly-sourced findings against the research
        # plan (flag-gated inside), then drop the per-turn transients (R1/R3).
        _reconcile_plan(cfg, rec, new_provs)
        rec.pop("_synthesize", None)
        rec.pop("_resumed", None)   # R3: transient — never carries to next turn
        rec["turns"] = int(rec.get("turns", 0)) + 1
        # v1.20.0: remember which hermes session this thread was spoken in, so
        # topics_list can hand the caller a session_id to pivot into the real
        # messages with the built-in session_search.
        if session_id:
            sess = rec.setdefault("sessions", [])
            if session_id in sess:
                sess.remove(session_id)     # keep most-recent-last
            sess.append(str(session_id))
            rec["sessions"] = sess[-_SESSION_CAP:]
        rec["last_active"] = time.time()
        # P4: amortized ## summary roll-up — only every summarize_every turns,
        # so the steady state stays at one LLM call/turn (the classifier).
        try:
            every = max(0, int(cfg.get("summarize_every") or 0))
            if every and rec["turns"] % every == 0:
                _rollup_summary(cfg, rec)
        except Exception:
            pass
        # R4: autostore — amortized durable-fact extraction into rec['facts'] plus
        # an idempotent inbox curation note (inbox-only; the human merge pass is
        # the gate for anything reaching curated wiki/). Default off.
        try:
            if str(cfg.get("autostore")) == "on":
                a_every = max(1, int(cfg.get("autostore_every") or 4))
                if rec["turns"] % a_every == 0:
                    added = False
                    for f in _extract_facts(cfg, user_message, assistant_response):
                        if f not in rec["facts"]:
                            rec["facts"].append(f)
                            added = True
                    rec["facts"] = rec["facts"][-_FACTS_CAP:]
                    if added:
                        _curation_note(cfg, rec)
        except Exception:
            pass
        _persist_topic(cfg, rec)  # write-through to <wiki>/topics/
        return None
    except Exception:
        logger.debug("topics: post_llm_call failed; turn unaffected",
                     exc_info=True)
        return None


def _on_weak_host(session_id=""):
    """True when the topics context injection + badge are ACTIVE for this
    session's model tier. v1.16.0: delegates to selfheal's per-guard tier
    registry (guard 'topic_injection', weak-tier ceiling) — byte-equivalent to
    the v1.15.0 weak-host gate with model_tier absent. Fail-safe: any
    uncertainty -> True (inject; prior behaviour)."""
    try:
        from . import selfheal as _sh
        return bool(_sh.guard_active("topic_injection", session_id))
    except Exception:
        try:
            import importlib
            _sh = importlib.import_module(
                "hermes_plugins_router_selfheal")
            return bool(_sh.guard_active("topic_injection", session_id))
        except Exception:
            return True


def badge_for(session_id=""):
    """Compact reply badge (e.g. ``t#00042``) for the session's active topic, or
    "" when disabled / no active topic / badge sub-flag off. Composed into
    selfheal's finisher and appended to the outgoing answer. Never raises."""
    try:
        cfg = _cfg()
        if cfg["enabled"] != "on" or cfg["badge"] != "on":
            return ""
        # P5: never badge a voice_only reply — TTS would read "t#00042" aloud.
        if _active_voice.get(session_id):
            return ""
        # v1.15.0 suppressed the badge on a frontier host along with the context
        # injection, on the reasoning that naming a thread the model was told
        # nothing about is incoherent. v1.20.0 splits the two: the badge is a
        # LABEL appended by the finisher, not something the model reasons about,
        # so it cannot derail a turn the way injection could — and without it a
        # strong-tier bot gives the user no handle to reference a thread by.
        # The injection stays blanket-inert on strong; only the label returns.
        if not _on_weak_host(session_id) and cfg.get("badge_strong") != "on":
            return ""
        tid = _active_topic.get(session_id)
        if not tid:
            return ""
        return "t#%05d" % int(tid)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

for _cb in (_tp_pre_llm, _tp_post_llm):
    _cb._router_topics = True  # dedup marker for force rescans
del _cb


def register(ctx):
    """Register the topics hooks (pre_llm_call + post_llm_call). Idempotent on
    force rescans; never raises. NO llm_request middleware — the healer chain is
    untouched. The reply badge is wired separately via the host bridge in
    selfheal (badge_for). Captures *ctx* so the classifier can reach ctx.llm, and
    declares the router_topic_classify auxiliary task so an operator can pin a
    cheap model for classification."""
    global _ctx
    try:
        _ctx = ctx
        try:
            if hasattr(ctx, "register_auxiliary_task"):
                ctx.register_auxiliary_task(
                    "router_topic_classify",
                    display_name="Router — topic classifier",
                    description="Routes each message to a conversation topic "
                                "(multi-topic system).",
                    defaults={"provider": "auto", "model": "", "timeout": 5},
                )
                # R4: durable-fact curation (autostore) + research plan decompose
                # share this cheap-model slot; both are default-off features.
                ctx.register_auxiliary_task(
                    "router_topic_curate",
                    display_name="Router — memory curator",
                    description="Extracts durable user facts and decomposes "
                                "research goals (active long-term memory).",
                    defaults={"provider": "auto", "model": "", "timeout": 6},
                )
        except Exception:
            logger.debug("topics: aux-task registration skipped", exc_info=True)
        if not hasattr(ctx, "register_hook"):
            logger.warning("topics: PluginContext lacks register_hook; topics "
                           "NOT installed")
            return
        for event, cb in (("pre_llm_call", _tp_pre_llm),
                          ("post_llm_call", _tp_post_llm)):
            try:
                hooks = ctx._manager._hooks.get(event, [])
                if any(getattr(h, "_router_topics", False) for h in hooks):
                    continue
            except Exception:
                pass
            ctx.register_hook(event, cb)
        # v1.19.0: the topics_list tool. Its own check_fn gates visibility on
        # the enabled flag, so registering unconditionally here keeps the
        # "flags read per-request, no restart" property.
        register_tool(ctx)
        logger.info("router plugin: topics registered (pre/post_llm_call; "
                    "enabled=%s)", _cfg()["enabled"])
    except Exception:
        logger.debug("topics: register failed; feature inactive", exc_info=True)
