# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Podwarden Inc

"""Research-driven hallucination mitigations (llm_request request transforms).

From the hermes-lab probe study (2026-07-22): prompt-only "verify if unsure"
nudges do NOT work on this model (its confidence is anti-correlated with
correctness), but STRUCTURAL forcing does. Two features, each behind its own
config flag (default off), applied by the router's llm_request middleware on the
constrained host:

* F1  force_verify  — on the FIRST call of a turn whose latest user message
  looks like a quote/citation/date-attribution or recency question, and only
  when a web_search tool is present, inject a hard "you MUST search first"
  directive AND set tool_choice="required". Measured: the ONLY reliable fix for
  the signature confident-fabrication (the model will not self-trigger the
  search; 0/4 unprompted). Also strips stale 2024/2025 years the model appends
  to its own search queries.

* F2  calc_route  — when the latest user message contains a multi-digit
  arithmetic expression, inject a light directive to use execute_code and never
  present a mentally-computed multi-digit result as exact. Lighter than F1: the
  model already self-routes big arithmetic to execute_code with tool_choice=auto.

Pure + fail-safe: every function returns a boolean/plain value and never raises;
the caller applies them only when the flag is on and swallows any error.
"""
import re

# --- intent detectors --------------------------------------------------------

# Attribution: asking for the author/work/year/verbatim-text/source of a quote.
_ATTR_RE = re.compile(
    r"\b(who\s+(wrote|said|first\s+said|coined|authored)"
    r"|what\s+(poem|book|novel|play|film|movie|song|work|year|date)"
    r"|which\s+(poem|book|novel|play|film|movie|song|work|author)"
    r"|next\s+line|following\s+line|exact\s+(word|words|text|quote|line|wording)"
    r"|verbatim|full\s+(quote|citation|text)|cite|citation|attribut"
    r"|what\s+year\s+(did|was|were)|in\s+what\s+year|when\s+(did|was|were)"
    r"|из\s+какого|кто\s+(написал|сказал|автор)|какого\s+года"
    r"|в\s+каком\s+году|какая\s+строка|следующая\s+строка|чьи?\s+слова)",
    re.I)

# Recency: current/volatile facts that drift with time.
_RECENCY_RE = re.compile(
    r"\b(current(ly)?|latest|newest|most\s+recent|as\s+of|right\s+now|today"
    r"|this\s+(year|month|week)|nowadays|these\s+days"
    r"|who\s+is\s+the\s+(current\s+)?(president|prime\s+minister|pm|ceo|king"
    r"|queen|pope|chancellor|governor|mayor|leader)"
    r"|price\s+of|how\s+much\s+(is|does|are)|stock\s+price|market\s+cap"
    r"|latest\s+version|current\s+version|newest\s+version"
    r"|released?\s+(yet|recently)|standings|who\s+won\s+the)",
    re.I)

# A year at/after the model's staleness horizon → recency-critical.
_YEAR_RE = re.compile(r"\b(20(2[6-9]|[3-9]\d))\b")

# Quote markers — a quoted span + a wh-question => attribution.
_QUOTE_RE = re.compile(r"[«\"“'‘][^«»\"”'’]{8,}[»\"”'’]")
_WH_RE = re.compile(r"\b(who|what|which|where|when|whose|from|автор|какой|где|"
                    r"откуда|кто|чей)\b", re.I)

# Multi-digit exact arithmetic (a big number and an operator / an arithmetic verb).
_ARITH_SYMBOL_RE = re.compile(r"\d{3,}\s*[*x×/÷+\-]\s*\d{2,}"
                             r"|\d{2,}\s*[*x×/÷]\s*\d{3,}")
_ARITH_WORD_RE = re.compile(
    r"\b(multiply|multiplied|divide|divided|times\b|product\s+of|sum\s+of|"
    r"square\s+of|cube\s+of|to\s+the\s+power|factorial|умнож|раздел|"
    r"произвед)", re.I)
_BIGNUM_RE = re.compile(r"\d{4,}")

# Stale years the drifting model appends to its own search queries.
_STALE_YEAR_RE = re.compile(r"\b20(2[0-5])\b")


# Conceptual / how-to / teaching framings — the biggest false-positive class in
# the A/B eval (e.g. "explain the newest React hooks" is not a fact lookup). A
# strong factual anchor (a quote, "who is the current …", "what year …") still
# forces; a bare conceptual "latest/newest <thing>" does not.
_CONCEPTUAL_RE = re.compile(
    r"\b(explain|how\s+do(es)?\b|how\s+to\b|teach\s+me|walk\s+me\s+through"
    r"|the\s+concept|help\s+me\s+understand|tutorial|guide\s+to|difference"
    r"\s+between|pros\s+and\s+cons|best\s+practices?)\b", re.I)
# Strong factual anchors that OVERRIDE the conceptual guard.
_STRONG_FACT_RE = re.compile(
    r"(current\s+\w*\s*(president|prime\s+minister|pm\b|ceo|king|queen|pope"
    r"|chancellor|governor|mayor)"
    r"|who\s+is\s+the\s+(current\s+)?(president|prime\s+minister|pm|ceo|king"
    r"|queen|pope|chancellor|governor|mayor)|what\s+year|in\s+what\s+year"
    r"|who\s+(wrote|said|first\s+said)|кто\s+(написал|сказал)|какого\s+года)",
    re.I)


def attribution_recency_intent(text):
    """True when the message asks for a checkable attribution/quote/date or a
    current/volatile fact — the classes forced verification helps. Excludes
    conceptual/how-to/teaching framings unless a strong factual anchor is present
    (A/B eval: the largest false-positive class)."""
    try:
        s = str(text or "")
        hit = bool(_ATTR_RE.search(s) or _RECENCY_RE.search(s)
                   or _YEAR_RE.search(s)
                   or (_QUOTE_RE.search(s) and _WH_RE.search(s)))
        if not hit:
            return False
        if _CONCEPTUAL_RE.search(s) and not _STRONG_FACT_RE.search(s) \
                and not _QUOTE_RE.search(s):
            return False   # conceptual framing without a hard factual anchor
        return True
    except Exception:
        return False


# Research intent: a multi-part investigation request (NOT a single checkable
# fact — attribution_recency_intent already covers those and would mislabel every
# citation question as "research"). Requires a research verb AND a comparison /
# enumeration / question cue, so a bare "what's the capital of France" stays out.
_RESEARCH_VERB_RE = re.compile(
    r"\b(research|investigate|look\s+into|dig\s+into|find\s+out|figure\s+out"
    r"|compare|contrast|evaluate|assess|survey|explore|analyz|analys"
    r"|pros\s+and\s+cons|trade[- ]?offs?|options?\s+for|alternatives?\s+to"
    r"|deep\s+dive|write\s+(me\s+)?a\s+report|literature\s+review)\b", re.I)
_RESEARCH_CUE_RE = re.compile(
    r"\b(vs\.?|versus|compared?\s+to|between|which\s+(is|one|are)|best\s+\w+"
    r"|better|cheaper|faster|fastest|cheapest|top\s+\d+|several|various"
    r"|different|and\b.*\bor\b|list\s+of)\b", re.I)


def research_intent(text):
    """True when the message reads as a multi-part research task — a research/
    compare verb PLUS a comparison/enumeration cue (or an explicit /research).
    Deliberately narrower than attribution_recency_intent: a single checkable
    fact ("who wrote X", "current PM") is NOT research and must not match here."""
    try:
        s = str(text or "")
        if re.match(r"^\s*/research\b", s, re.I):
            return True
        return bool(_RESEARCH_VERB_RE.search(s) and _RESEARCH_CUE_RE.search(s))
    except Exception:
        return False


def arithmetic_intent(text):
    """True when the message contains an exact multi-digit computation."""
    try:
        s = str(text or "")
        if _ARITH_SYMBOL_RE.search(s):
            return True
        if _ARITH_WORD_RE.search(s) and _BIGNUM_RE.search(s):
            return True
        return False
    except Exception:
        return False


# --- request-shape helpers ---------------------------------------------------

def latest_user_text(messages):
    try:
        for m in reversed(messages or []):
            if isinstance(m, dict) and m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, str):
                    return c
                if isinstance(c, list):  # multimodal content parts
                    return " ".join(p.get("text", "") for p in c
                                    if isinstance(p, dict))
        return ""
    except Exception:
        return ""


def first_call_of_turn(messages):
    """True when no assistant/tool message follows the LAST user message — i.e.
    this is the first model call of the current turn (no tool loop yet)."""
    try:
        last_user = -1
        for i, m in enumerate(messages or []):
            if isinstance(m, dict) and m.get("role") == "user":
                last_user = i
        if last_user < 0:
            return False
        for m in (messages or [])[last_user + 1:]:
            if isinstance(m, dict) and m.get("role") in ("assistant", "tool"):
                return False
        return True
    except Exception:
        return False


def has_web_search(tools):
    try:
        for t in tools or []:
            fn = (t or {}).get("function") if isinstance(t, dict) else None
            name = (fn or {}).get("name") if isinstance(fn, dict) else None
            if name in ("web_search", "web_extract", "tool_search"):
                return True
        return False
    except Exception:
        return False


# --- directives --------------------------------------------------------------

FORCE_VERIFY_NOTE = (
    "This question asks for a specific verifiable fact — a quote/line, an "
    "attribution (author/work/year), a citation, or current/volatile "
    "information. You MUST call web_search to confirm it before answering. Do "
    "NOT answer from memory, and do not state any source, verbatim quote, date, "
    "or current figure you have not confirmed from this turn's search results.")

CALC_ROUTE_NOTE = (
    "This involves exact arithmetic. Use the execute_code tool (Python) for any "
    "multi-digit computation and report its result — never present a "
    "mentally-computed multi-digit number as exact.")

# v1.17.0 (A — grounding directive). Tuned to the ACTUAL research residual (the
# model already searches and grounds ~95%; the failure is presenting SELF-
# COMPUTED roll-up totals as if sourced). A cheap always-injectable rider on
# research turns; ~60 tokens, no extra API call. Cache-stable, appended once.
GROUNDING_NOTE = (
    "When you state a specific figure — a count, percentage, dollar amount, "
    "multiplier, date, version, or paper ID — it must appear in THIS turn's "
    "retrieved search/extract results. Do NOT compute and present aggregate or "
    "roll-up totals (e.g. a summed commit or PR count across releases) as if "
    "they were sourced facts; if you derive a number, say so, or omit it. Flag "
    "anything a source calls rumored, upcoming, or 'in talks' as unconfirmed, "
    "and cite the source for headline figures where practical. Prefer omitting "
    "a number to guessing it.")


def _append_system(request, note):
    """Append a cache-stable system message once (idempotent)."""
    msgs = request.get("messages")
    if not isinstance(msgs, list):
        return False
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "system" \
                and m.get("content") == note:
            return False
    msgs.append({"role": "system", "content": note})
    return True


def apply_force_verify(request):
    """F1: on a first-call attribution/recency turn with web_search available,
    inject the forcing directive + set tool_choice='required'. Returns True if
    applied. Caller gates on the flag; this never raises via the caller's guard."""
    if not isinstance(request, dict):
        return False
    msgs = request.get("messages")
    if not isinstance(msgs, list):
        return False
    if not first_call_of_turn(msgs):
        return False
    if not attribution_recency_intent(latest_user_text(msgs)):
        return False
    if not has_web_search(request.get("tools")):
        return False
    # A/B eval finding: the strong directive ALONE reliably triggers the search
    # (4/4 in re-test) WITHOUT tool_choice="required" — and directive-only is
    # SAFE: on a false positive the model can still answer from memory instead of
    # being trapped in a forced tool loop with no fallback (the eval's blocker).
    # So we inject the directive and leave tool_choice untouched.
    return _append_system(request, FORCE_VERIFY_NOTE)


def apply_calc_route(request):
    """F2: on an arithmetic turn, inject the calc directive. Returns True if
    applied. Lighter than F1 — no tool_choice forcing (the model self-routes)."""
    if not isinstance(request, dict):
        return False
    msgs = request.get("messages")
    if not isinstance(msgs, list):
        return False
    if not arithmetic_intent(latest_user_text(msgs)):
        return False
    return _append_system(request, CALC_ROUTE_NOTE)


def apply_grounding_directive(request):
    """A (v1.17.0): on a first-call research/attribution turn with a web_search
    tool present, inject the grounding directive once. Un-host-gated (fabrication
    is on both tiers); the caller gates on the ``antifab_directive`` flag. No
    tool_choice forcing (pure directive). Returns True if applied. Never raises
    via the caller's guard."""
    if not isinstance(request, dict):
        return False
    msgs = request.get("messages")
    if not isinstance(msgs, list):
        return False
    if not first_call_of_turn(msgs):
        return False
    txt = latest_user_text(msgs)
    if not (research_intent(txt) or attribution_recency_intent(txt)):
        return False
    if not has_web_search(request.get("tools")):
        return False
    return _append_system(request, GROUNDING_NOTE)


# ---------------------------------------------------------------------------
# v1.18.0 (clarify guard) — a clarify-vs-execute nudge for genuinely ambiguous,
# no-referent prompts. Observed failure (corpus batches 8/9): on "Fix it.",
# "finish the thing we discussed", "I need you to help me with the thing we
# discussed — go ahead and finish it" the model does NOT ask a clarifying
# question — it session_searches real history, latches onto an unrelated past
# project, and over-executes (a 30-call spiral / a confident wrong answer). The
# corpus asserts these must CLARIFY. This is an ADD-ONLY pre-turn directive
# (never withholds/rewrites an answer). The primary risk is a FALSE POSITIVE on
# a CLEAR short prompt ("list my todos", "convert 84 kg to pounds") — so the
# detector is deliberately conservative and biased to silence: a miss just
# reverts to today's behaviour, a false fire creates a NEW failure class.
# ---------------------------------------------------------------------------

# Family A — a bare deictic/continuation IMPERATIVE with no concrete object.
# The regex is anchored to the WHOLE (trimmed) prompt: it matches only when the
# entire message is [optional content-free framing] + a continuation/repair verb
# + [an anaphoric object or nothing] + [adverbial fillers]. That full-anchor is
# the safety mechanism — a prompt with any concrete object ("fix the parser bug",
# "finish the report by Friday") fails the anchor and does not match.
_CLARIFY_A_RE = re.compile(
    r"^\s*"
    r"(?:(?:please|just|now|ok(?:ay)?|so|hey|yeah|yep|alright|kindly|"
    r"go\s+ahead\s+and|can\s+you|could\s+you|would\s+you|will\s+you|"
    r"i\s+need\s+you\s+to|i\s+want\s+you\s+to|i'?d\s+like\s+you\s+to|"
    r"i\s+need\s+you\s+to\s+just|let'?s|you\s+can|maybe\s+you\s+can|"
    r"please\s+can\s+you)\s+)*"
    r"(?:fix|finish|complete|redo|re-?do|retry|re-?try|repeat|continue|"
    r"proceed|resume|do|handle|address|sort|deal\s+with|take\s+care\s+of|"
    r"carry\s+on|keep\s+going|keep\s+at\s+it|go\s+ahead|go\s+on|wrap\s+up|"
    r"wrap|finalize|finalise|get\s+it\s+done|make\s+it\s+work)"
    r"(?:\s+(?:it|that|this|them|those|these|the\s+thing|the\s+things|"
    r"the\s+stuff|the\s+one|the\s+ones|everything|all\s+of\s+it|the\s+rest|"
    r"up|out|on|with\s+it))*"
    r"(?:\s+(?:now|again|already|please|for\s+me|too|as\s+well|first|then|"
    r"ok(?:ay)?|quickly|asap|thanks|thx|finally))*"
    r"[\s.!?]*$",
    re.I)

# Family B — an explicit reference to VAGUE prior context that is not carried in
# this conversation ("the thing we discussed", "what we talked about", "as we
# said"). Deliberately requires a VAGUE head (thing/one/stuff/task/issue/problem
# /what/that) so a concrete "the feature we discussed in spec.md" does NOT match.
_CLARIFY_B_RE = re.compile(
    r"\b(?:"
    r"the\s+(?:thing|things|stuff|one|task|issue|problem|item|work|bit|part)\s+"
    r"(?:we|you|i)\s+(?:discussed|talked\s+about|were\s+(?:working|talking)\s+on|"
    r"mentioned|said|agreed(?:\s+on)?|went\s+over|spoke\s+about|had)"
    r"|what\s+(?:we|you)\s+(?:discussed|talked\s+about|were\s+working\s+on|"
    r"agreed(?:\s+on)?|mentioned|said|spoke\s+about)"
    r"|that\s+thing\s+(?:we|you)\s+(?:discussed|mentioned|talked\s+about|said|"
    r"were\s+working\s+on)"
    r"|(?:like|as)\s+(?:we|i)\s+(?:discussed|said|mentioned|agreed|talked\s+about)"
    r"|the\s+(?:thing|one|stuff|task|issue|problem)\s+from\s+(?:before|earlier|"
    r"last\s+time|yesterday|our\s+last\s+\w+)"
    r"|what\s+you\s+(?:mentioned|suggested|proposed|said)\s+(?:earlier|before|"
    r"last\s+time)"
    r")\b",
    re.I)


def clarify_ambiguity_intent(text):
    """True when the message is a genuinely ambiguous prompt with NO concrete,
    actionable object — either a bare deictic imperative (Family A: "Fix it.",
    "continue", "do that now", "go ahead and finish it") or an explicit
    reference to vague prior context not carried in this conversation (Family B:
    "finish the thing we discussed", "what we talked about"). Conservative and
    biased to silence: a clear short request ("list my todos", "convert 84 kg to
    pounds", "add a todo: buy milk", "search X and summarize") never matches."""
    try:
        s = str(text or "").strip()
        if not s:
            return False
        if _CLARIFY_A_RE.match(s):
            return True
        # Family B is a strong signal on its own, but cap total length so a long
        # substantive request that merely says "as we discussed" in passing (and
        # then spells out the concrete task) does not trip it.
        if _CLARIFY_B_RE.search(s) and len(s.split()) <= 30:
            return True
        return False
    except Exception:
        return False


def no_prior_context(messages):
    """True when the conversation carries NO prior assistant/tool exchange — the
    current user message is effectively the start of the conversation, so there
    is no in-conversation referent an ambiguous 'it'/'the thing' could resolve
    to. Any prior assistant turn means a live referent may exist in THIS
    conversation (a genuine multi-turn "fix it"), so the guard stays silent
    (bias to silence). This is the "(fresh session OR no in-turn context)" gate:
    the observed over-execution failures are all fresh one-shot prompts whose
    only 'context' lives in cross-session history the model spelunks via
    session_search — never in the request messages."""
    try:
        for m in messages or []:
            if isinstance(m, dict) and m.get("role") in ("assistant", "tool"):
                return False
        return True
    except Exception:
        return False


# ~35 tokens; the exact clarify-vs-execute nudge. Add-only — it asks the model
# to CLARIFY, it never blocks or rewrites the model's own answer, and it does
# not name a specific tool (hermes has its own clarify tool; this nudges USE).
CLARIFY_NOTE = (
    "If this request is ambiguous or refers to context not present in THIS "
    "conversation, ask a brief clarifying question instead of guessing or "
    "searching past history for what it might mean.")


def apply_clarify_guard(request):
    """v1.18.0: on a first-call turn with NO prior in-conversation context whose
    user message is genuinely ambiguous (clarify_ambiguity_intent), append the
    clarify nudge once. Un-host-gated (the ambiguity failure is tier-agnostic);
    the caller gates on the ``clarify_guard`` flag. Pure directive (no
    tool_choice, no withhold). Returns True if applied. Never raises via the
    caller's guard."""
    if not isinstance(request, dict):
        return False
    msgs = request.get("messages")
    if not isinstance(msgs, list):
        return False
    if not first_call_of_turn(msgs):
        return False
    if not no_prior_context(msgs):
        return False
    if not clarify_ambiguity_intent(latest_user_text(msgs)):
        return False
    return _append_system(request, CLARIFY_NOTE)


def strip_stale_years(query):
    """Remove stale 2020-2025 years the drifting model appends to a web_search
    query (they poison even a forced search). Returns the cleaned query."""
    try:
        cleaned = _STALE_YEAR_RE.sub("", str(query or ""))
        return re.sub(r"\s{2,}", " ", cleaned).strip()
    except Exception:
        return query
