# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Podwarden Inc

"""antifab — post-draft claim-grounding annotate (v1.17.0, feature B).

The residual "confident fabrication" on research turns is NOT stale-memory
(the model searches heavily and ~95% of every specific in the answer is
literally present in this turn's retrieved tool corpus). What is genuinely
ungrounded is a small numeric residue: model-computed ROLL-UP AGGREGATES
(summed/rounded totals — "~6,200 commits and ~2,800 merged PRs across those
releases") presented as if sourced. force_verify (a pre-draft "go search"
nudge) is inert against this — the model already searched.

This module implements the load-bearing fix: extract the high-risk numeric
specifics from the finished draft, test each against the turn's retrieved
corpus with aggressive normalization, and — respecting add-vs-withhold — build
an APPEND-ONLY caveat naming only the UNMATCHED figures. It NEVER deletes or
rewrites the answer body and NEVER changes a number in place; the worst case of
a false flag is one slightly-over-cautious footnote on a correct answer.

Precision comes from two disciplines, both biased to UNDER-flag (ambiguity =>
treat as grounded, protect correct content):

* aggressive normalization — thousands commas stripped ("2,245"=="2245"),
  "N percent"=="N %"=="N%", "×"=="x", "≈"=="~", scale suffixes (K/M/B) and
  units folded — so a grounded figure in a different surface form is never
  falsely flagged (the failure mode to avoid);
* a token is GROUNDED iff its normalized numeric core is a substring of the
  normalized corpus; a small bare integer that appears anywhere in the corpus
  (e.g. "18" inside "Access 18 Nous Research models") is treated as grounded.

Pure, no hermes imports — unit-testable under the system interpreter. Every
function is fail-safe: any error returns a no-op value (never raises).
"""
import re

# Idempotency marker: the caveat block is appended at most once. Kept ASCII-safe
# in the marker test (the visible heading uses an emoji, matched by substring).
CAVEAT_MARKER = "unverified figures"
CAVEAT_HEADER = "⚠️ Unverified figures"  # "⚠️ Unverified figures"
_CAVEAT_MAX = 8  # cap the listed items so a many-figure turn stays readable

# High-risk keyword/unit context: a number next to one of these is a specific
# claim (a count, benchmark delta, valuation, model tally, paper id, …).
_RISK_KEYWORD = (
    r"commits?|prs?|pull\s+requests?|contributors?|models?|issues?|stars?|"
    r"forks?|users?|downloads?|parameters?|params?|tokens?|benchmark|valuation|"
    r"funding|revenue|round|gpu[- ]?hours?|speedup|context|window"
)

# --- numeric specific patterns (each yields a surface span) ------------------
# A scale suffix (K/M/B/…) only when it is NOT the first letter of a following
# word — so "2,800 merged" / "5.5 by" / "4 major" do NOT read as "2800m" etc.
_SCALE = r"(?:k|m|b|bn|billion|million|thousand)?(?![a-z])"
# A number with a magnitude >= 1000 (comma form or scale suffix) — the roll-up
# aggregate class. Captures "6,200", "~2,800", "12,300", "512K", "1.5B".
_MAGNITUDE_RE = re.compile(
    r"[~≈≥>]?\s?\$?\d[\d,]*(?:\.\d+)?\s?" + _SCALE, re.I)
# A percentage: "8%", "11 percent", "~80%".
_PERCENT_RE = re.compile(r"[~≈]?\s?\d[\d,]*(?:\.\d+)?\s?(?:%|percent\b)", re.I)
# A multiplier: "17x", "1.4x", "1.7×".
_MULT_RE = re.compile(r"[~≈]?\s?\d[\d,]*(?:\.\d+)?\s?[x×](?![a-z])", re.I)
# A dollar amount: "$1.5B", "$4,800", "$1.5 billion".
_DOLLAR_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?\s?" + _SCALE, re.I)
# An arxiv id: "2605.06546".
_ARXIV_RE = re.compile(r"\b\d{4}\.\d{4,5}\b")
# A number followed (optionally across one adjective, e.g. "merged"/"community")
# by a risk keyword: "18 models", "450+ contributors", "2,800 merged PRs".
_KEYWORD_NUM_RE = re.compile(
    r"[~≈]?\s?\d[\d,]*(?:\.\d+)?\+?\s+(?:[a-z]+\s+)?(?:%s)\b" % _RISK_KEYWORD,
    re.I)


def normalize(s):
    """Aggressively normalize text for numeric-membership matching. Biased to
    COLLAPSE surface variants so a grounded figure is never falsely flagged:
    lowercase; ×->x, ≈->~; thousands commas stripped between digits;
    "percent"->"%"; "N %"->"N%"; "N x"->"Nx"; scale words -> letters; whitespace
    collapsed. Fail-safe: returns "" on error."""
    try:
        x = str(s or "").lower()
        x = x.replace("×", "x").replace("✕", "x").replace("✖", "x")
        x = x.replace("≈", "~")
        x = re.sub(r"(?<=\d),(?=\d)", "", x)          # thousands commas
        x = x.replace("percent", "%")
        x = re.sub(r"\bbillion\b", "b", x)
        x = re.sub(r"\bmillion\b", "m", x)
        x = re.sub(r"\bthousand\b", "k", x)
        x = re.sub(r"\bbn\b", "b", x)
        x = re.sub(r"(\d)\s+%", r"\1%", x)            # "8 %" -> "8%"
        x = re.sub(r"(\d)\s+([xkmb])\b", r"\1\2", x)  # "512 k" -> "512k"
        x = re.sub(r"\s+", " ", x)
        return x.strip()
    except Exception:
        return ""


def _core(surface):
    """The normalized numeric core used both as the display key and the corpus
    needle for a captured specific. Strips leading ~/≈/>/space, keeps $, %, x,
    scale letters. Returns "" when there is no digit."""
    n = normalize(surface)
    n = n.lstrip("~> ").strip()
    return n if any(ch.isdigit() for ch in n) else ""


def _digits(surface):
    """The bare comma-stripped integer value of a surface token, or None. Used
    for the magnitude (>= 1000) test."""
    try:
        m = re.search(r"\d[\d,]*", str(surface))
        if not m:
            return None
        return int(m.group(0).replace(",", ""))
    except Exception:
        return None


def _has_scale(surface):
    """True when a surface carries a K/M/B/bn scale suffix (magnitude >= 1000)."""
    return bool(re.search(r"\d\s?(?:k|m|b|bn|billion|million|thousand)\b",
                          str(surface), re.I))


def _dedup_key(surface):
    """The collapse key for a specific: its leading number-with-unit, without a
    trailing keyword word — so the bare-magnitude capture "~6,200" and the
    keyword capture "~6,200 commits" collapse to one item, while "8%" and "8B"
    (distinct units) stay separate."""
    n = normalize(surface)
    m = re.match(r"~?\$?\d[\d.]*(?:%|x|k|m|b)?", n)
    return m.group(0) if m and m.group(0) else n


def extract_specifics(text):
    """Extract high-risk numeric specifics from *text* as a de-duplicated list
    of surface strings (original form, for the caveat). Deliberately skips bare
    small integers (magnitude < 1000 with no risk keyword/unit) — they are
    noise, not fabrication-prone — to keep precision high. When several captures
    share a number+unit, the richest (longest) surface is kept. Fail-safe: [] on
    error."""
    try:
        s = str(text or "")
        cands = []

        def _add(surface):
            surf = str(surface).strip()
            if surf and _core(surf):
                cands.append(surf)

        # magnitude >= 1000 (comma form) OR a scale suffix
        for m in _MAGNITUDE_RE.finditer(s):
            surf = m.group(0).strip()
            if not surf or not any(c.isdigit() for c in surf):
                continue
            val = _digits(surf)
            if _has_scale(surf) or (val is not None and val >= 1000):
                _add(surf)
        for rx in (_PERCENT_RE, _MULT_RE, _DOLLAR_RE, _ARXIV_RE, _KEYWORD_NUM_RE):
            for m in rx.finditer(s):
                _add(m.group(0))
        # collapse by number+unit key, keeping the richest (longest) surface;
        # preserve first-seen order for a stable caveat.
        best, order = {}, []
        for surf in cands:
            key = _dedup_key(surf)
            if key not in best:
                best[key] = surf
                order.append(key)
            elif len(surf) > len(best[key]):
                best[key] = surf
        return [best[k] for k in order]
    except Exception:
        return []


def _value_forms(core):
    """Scale-equivalent normalized needles for a numeric core, so a grounded
    figure written in a different SCALE form is never falsely flagged
    (1,450,000,000 == "1.45 billion" == 1.45b == 1450m). Only ADDS candidates
    (never removes) => strictly biased to ground. Returns a set of strings."""
    try:
        m = re.match(r"~?\$?(\d+(?:\.\d+)?)([kmb])?", core)
        if not m:
            return set()
        num = float(m.group(1))
        scale = {"k": 1e3, "m": 1e6, "b": 1e9}.get(m.group(2), 1.0)
        value = num * scale
        forms = set()
        if value >= 1000 and value == int(value):
            forms.add(str(int(value)))          # fully-expanded integer
        for suf, div in (("b", 1e9), ("m", 1e6), ("k", 1e3)):
            q = value / div
            if 1 <= q < 100000:
                forms.add(("%.6f" % q).rstrip("0").rstrip(".") + suf)
        return forms
    except Exception:
        return set()


def _value_of(core):
    """Numeric value of a normalized core (folding a k/m/b scale letter), or
    None. E.g. "1.45b"->1.45e9, "6200"->6200.0."""
    try:
        m = re.match(r"~?\$?(\d+(?:\.\d+)?)([kmb])?", core)
        if not m:
            return None
        return float(m.group(1)) * {"k": 1e3, "m": 1e6, "b": 1e9}.get(
            m.group(2), 1.0)
    except Exception:
        return None


def _corpus_values(corpus_norm):
    """The set of numeric values >= 1000 in the (normalized) corpus, folding a
    k/m/b scale letter. Used for approximate (scale/precision) grounding."""
    vals = set()
    try:
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*([kmb])?", corpus_norm):
            try:
                v = float(m.group(1)) * {"k": 1e3, "m": 1e6, "b": 1e9}.get(
                    m.group(2), 1.0)
                if v >= 1000:
                    vals.add(v)
            except Exception:
                pass
    except Exception:
        pass
    return vals


# Relative tolerance for approximate (scale/precision) grounding of large values
# — grounds "1,412,000,000" against a source's "1.41 billion" (0.14% apart) while
# a genuine roll-up sum with no near source value (6,200; 2,800) still flags.
_FUZZ_TOL = 0.01


def is_grounded(surface, corpus_norm):
    """True when *surface*'s normalized numeric core (or a scale-equivalent form,
    or its bare digit run) is a substring of the already-normalized corpus.
    Biased to ground: returns True on any doubt (empty core, error).
    *corpus_norm* must be normalize(corpus)."""
    try:
        core = _core(surface)
        if not core:
            return True
        if core in corpus_norm:
            return True
        # scale-equivalent forms (1,450,000,000 <-> "1.45 billion" <-> 1.45b)
        for f in _value_forms(core):
            if f in corpus_norm:
                return True
        # bare digit run (drops $/%/x/scale) — "18 models" grounds on a lone "18"
        m = re.search(r"\d[\d.]*", core)
        if m and m.group(0) in corpus_norm:
            return True
        return False
    except Exception:
        return True


def ungrounded_specifics(draft, corpus, given=""):
    """The list of numeric specifics in *draft* NOT matched in *corpus*
    (de-duplicated, order-preserving). *given* is extra grounded reference text
    (e.g. the user's own question — figures the user supplied are not
    fabrications). Empty when the draft or corpus is empty. Fail-safe: [] on
    error."""
    try:
        if not str(draft or "").strip() or not str(corpus or "").strip():
            return []
        corpus_norm = normalize(str(corpus or "") + "\n" + str(given or ""))
        if not corpus_norm:
            return []
        cvals = _corpus_values(corpus_norm)
        out = []
        for s in extract_specifics(draft):
            if is_grounded(s, corpus_norm):
                continue
            # approximate (scale/precision) grounding for large values: a source
            # value within _FUZZ_TOL grounds it (1.41 billion ~= 1,412,000,000).
            v = _value_of(_core(s))
            if (v is not None and v >= 1000
                    and any(abs(v - cv) <= _FUZZ_TOL * max(v, cv)
                            for cv in cvals)):
                continue
            out.append(s)
        return out
    except Exception:
        return []


# A computation/conversion request — the user is asking the model to DERIVE a
# number (compute/calculate a %, growth, conversion, arithmetic), so a derived
# result that is legitimately absent from the web sources is EXPECTED, not a
# fabrication. Claim-grounding is skipped on these turns to avoid caveating a
# correct, user-requested derivation.
_COMPUTE_RE = re.compile(
    r"\b(calculate|calculating|compute(?!\s+(?:cost|power|cluster|node|"
    r"resource|infrastructur|budget|unit|instance|capacity))|"
    r"how\s+much\s+(?:is|would|are)|what\s+is\s+\d|convert\b|"
    r"\d+(?:\.\d+)?\s*%\s+of\b|percent\s+of\b|multiply|divide|"
    r"\bsum\s+of\b|product\s+of\b|average\s+of\b|раздели|умнож|вычисли|"
    r"посчитай|сколько\s+будет)", re.I)


def is_computation_request(text):
    """True when the user asked the model to DERIVE/compute a number. Fail-safe:
    False on error."""
    try:
        return bool(_COMPUTE_RE.search(str(text or "")))
    except Exception:
        return False


def turn_user_text(messages):
    """The last real user message text (skipping synthetic recovery/corrective
    rows). "" on error."""
    try:
        for m in reversed(messages or []):
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            c = _content_text(m.get("content"))
            if "returned an empty response" in c or c.startswith("SYSTEM NOTICE:"):
                continue
            return c
        return ""
    except Exception:
        return ""


def annotate(draft, ungrounded):
    """Append (append-only, idempotent) a caveat listing *ungrounded* specifics
    to *draft*. Returns the draft UNCHANGED when there is nothing to flag or the
    caveat is already present. NEVER deletes/rewrites the body. Fail-safe:
    returns *draft* on error."""
    try:
        body = str(draft or "")
        if not ungrounded or not body.strip():
            return body
        if CAVEAT_MARKER in body.lower():
            return body  # idempotent — already annotated
        # de-dup preserve order (defensive; caller already de-dups)
        seen, items = set(), []
        for u in ungrounded:
            k = str(u).strip()
            if k and k.lower() not in seen:
                seen.add(k.lower())
                items.append(k)
        if not items:
            return body
        shown, extra = items[:_CAVEAT_MAX], max(0, len(items) - _CAVEAT_MAX)
        listed = "; ".join(shown)
        if extra:
            listed += "; and %d more" % extra
        caveat = ("\n\n%s: I could not match these specific figures to the "
                  "sources I retrieved this turn; treat them as unverified "
                  "(they may be derived or estimated): %s."
                  % (CAVEAT_HEADER, listed))
        return body + caveat
    except Exception:
        return str(draft or "")


# hermes wraps every retrieved WEB result in this envelope; execute_code /
# terminal / other tool results are NOT wrapped this way. Gating the corpus on
# it makes claim-grounding fire ONLY on genuine web-research turns (no web
# sources => "" => the annotate step is a no-op).
_WEB_SOURCE_RE = re.compile(
    r'untrusted_tool_result\s+source="web(?:_search|_extract|_[a-z]+)?"', re.I)


def turn_corpus(messages):
    """Build this turn's retrieved WEB corpus: the concatenated text of
    web_search / web_extract tool-result messages that follow the LAST real user
    message. Non-web tool results (execute_code, terminal, …) are EXCLUDED so a
    computation turn is not mistaken for a research turn. Returns "" when there
    are no web results. Pure; fail-safe: "" on error.

    Synthetic mid-turn user rows (the empty-recovery nudge / the forced-synthesis
    corrective) do NOT reset the turn boundary."""
    try:
        msgs = messages or []
        last_user = -1
        for i, m in enumerate(msgs):
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            c = _content_text(m.get("content"))
            if "returned an empty response" in c or c.startswith("SYSTEM NOTICE:"):
                continue
            last_user = i
        parts = []
        for m in msgs[last_user + 1:] if last_user >= 0 else msgs:
            if isinstance(m, dict) and m.get("role") == "tool":
                t = _content_text(m.get("content"))
                if t.strip() and _WEB_SOURCE_RE.search(t):
                    parts.append(t)
        return "\n\n".join(parts)
    except Exception:
        return ""


def _content_text(content):
    """Plain text of a message content (str or a list of parts). Never raises."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, dict):
                t = p.get("text")
                if isinstance(t, str):
                    out.append(t)
            elif isinstance(p, str):
                out.append(p)
        return "\n".join(out)
    return "" if content is None else str(content)
