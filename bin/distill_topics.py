#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Podwarden Inc
"""Distill stale saandal topic threads into llm-wiki inbox curation notes.

Runs OUT OF BAND (cron), never on a live turn: a topic becomes a candidate only
once it has gone quiet, so the whole thread is distilled at once instead of the
in-turn ``autostore`` path's user-message-only snapshot every N turns.

Selection is the "timeout" half; cron is the clock:

    stale        now - last_active >= --stale-hours   (thread has gone quiet)
    substantial  turns >= --min-turns                 (skip one-shot trivia)
    changed      last_active > recorded distilled_at  (idempotent; re-distils
                                                       only when it moved again)

One bounded JSON call per candidate to a hosted model (DeepSeek by default) so
the shared vLLM the profile bots run on takes zero load. Output goes ONLY to
``<vault>/inbox/hermes/`` — the merge pass is what reaches curated ``wiki/``.

Distiller state lives in ``<vault>/topics/.distilled.json``: the plugin rewrites
``t*.md`` from its own record on every turn, so a marker inside those files would
be silently dropped, and the plugin's loader only ever globs ``^t\\d{5}\\.md$``,
so the sidecar is invisible to it.

Vaults are auto-discovered from every hermes config with ``topics.enabled: on``,
so the profile rollout carries this along with no second place to update. Each
vault is processed independently and notes never cross vaults — the profile
isolation contracts (1legion/imi/otter/olga-pm) are enforced by construction.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

DEFAULT_WIKI = os.path.expanduser("~/llm-wiki")
HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
# Mirrors merge_inboxes.py SECTIONS — a target naming any other section is
# silently re-homed to _team by the merge pass, so constrain the model to these.
SECTIONS = ["_team", "user", "household", "jobs", "infra"]
TOPIC_RE = re.compile(r"^t\d{5}\.md$")
STATE_NAME = ".distilled.json"

SCHEMA_HINT = """Return ONLY a JSON object with these keys:
{
  "store":   true|false,   // false when the thread holds nothing worth keeping
  "section": "one of: _team | user | household | jobs | infra",
  "page":    "kebab-case-page-name",
  "summary": "one sentence, <=110 chars, stating what is now known",
  "facts":   ["short third-person durable statements", "..."]
}"""

INSTRUCTIONS = """You curate a personal knowledge vault from finished assistant \
conversations.

Given one conversation thread, extract only DURABLE knowledge — things still \
true next month: decisions reached, capabilities confirmed or ruled out, \
configuration and how things work, facts the user stated about their life, \
work, tools, or lasting preferences.

Do NOT store: the user's questions, transient state, one-off task instructions, \
pleasantries, or anything the assistant merely speculated. A thread that is just \
a question and an answer about general knowledge is NOT durable — set \
store=false. Prefer store=false over guessing; an empty vault beats a wrong one.

Choose the section by subject: _team (cross-host, agents, meta), user (who the \
user is), household (home logistics), jobs (work, companies, projects, tasks), \
infra (this host, servers, channels, integrations).

""" + SCHEMA_HINT


# --------------------------------------------------------------------------
# vault discovery
# --------------------------------------------------------------------------
def discover_vaults(hermes_home=HERMES_HOME):
    """Every distinct wiki_dir whose hermes config has topics.enabled: on."""
    try:
        import yaml
    except Exception:
        sys.stderr.write("discover: PyYAML unavailable; pass --vault explicitly\n")
        return []
    cfgs = [os.path.join(hermes_home, "config.yaml")]
    cfgs += sorted(glob.glob(os.path.join(hermes_home, "profiles", "*", "config.yaml")))
    out = []
    for path in cfgs:
        try:
            with open(path, encoding="utf-8") as fh:
                cfg = yaml.safe_load(fh) or {}
            topics = (((cfg.get("tools") or {}).get("tool_search") or {})
                      .get("topics") or {})
            if str(topics.get("enabled", "off")).lower() not in ("on", "true"):
                continue
            vault = str(topics.get("wiki_dir") or DEFAULT_WIKI)
            if vault not in out:
                out.append(vault)
        except Exception as exc:
            sys.stderr.write("discover: skipped %s (%s)\n" % (path, exc))
    return out


# --------------------------------------------------------------------------
# topic store
# --------------------------------------------------------------------------
def load_state(topics_dir):
    try:
        with open(os.path.join(topics_dir, STATE_NAME), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(topics_dir, state):
    path = os.path.join(topics_dir, STATE_NAME)
    fd, tmp = tempfile.mkstemp(dir=topics_dir, prefix=".distilled-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def load_index(topics_dir):
    """The plugin's hot manifest. Returns [] when the store is absent/unreadable."""
    try:
        with open(os.path.join(topics_dir, "_index.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    recs = data.get("topics") if isinstance(data, dict) else data
    if isinstance(recs, dict):
        recs = list(recs.values())
    return recs if isinstance(recs, list) else []


def parse_topic_md(path):
    """Split a t*.md into {frontmatter keys} + {section name: [lines]}."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except Exception:
        return {}, {}
    meta = {}
    body = text
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        body = m.group(2)
    sections, current = {}, None
    for line in body.splitlines():
        if line.startswith("## "):
            current = line[3:].strip().lower()
            sections.setdefault(current, [])
        elif current is not None and line.strip():
            sections[current].append(line.rstrip())
    return meta, sections


def select(topics_dir, state, now, stale_hours, min_turns):
    """Candidates in oldest-quiet-first order, each with its reason for selection."""
    out = []
    for rec in load_index(topics_dir):
        try:
            tid = int(rec.get("id"))
            key = "t%05d" % tid
            last_active = float(rec.get("last_active") or 0)
            turns = int(rec.get("turns") or 0)
            if turns < min_turns:
                continue
            if now - last_active < stale_hours * 3600.0:
                continue
            prev = state.get(key) or {}
            if float(prev.get("last_active") or -1) >= last_active:
                continue  # already distilled at this exact revision
            out.append({"key": key, "id": tid, "rec": rec,
                        "last_active": last_active, "turns": turns,
                        "path": os.path.join(topics_dir, key + ".md"),
                        "redistill": bool(prev)})
        except Exception:
            continue
    out.sort(key=lambda c: c["last_active"])
    return out


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------
def render_thread(cand, max_log_lines=40):
    meta, sections = parse_topic_md(cand["path"])
    rec = cand["rec"]
    parts = ["Title: %s" % (rec.get("title") or meta.get("title") or "")]
    ents = rec.get("entities") or []
    if ents:
        parts.append("Keywords: %s" % ", ".join(str(e) for e in ents[:20]))
    parts.append("Turns: %d" % cand["turns"])
    for name in ("summary", "facts", "findings"):
        lines = sections.get(name) or []
        if lines:
            parts.append("\n## %s\n%s" % (name, "\n".join(lines[:20])))
    log = sections.get("log") or []
    if log:
        parts.append("\n## conversation\n%s" % "\n".join(log[-max_log_lines:]))
    return "\n".join(parts)


def read_env_key(name, env_file=None):
    val = os.environ.get(name)
    if val:
        return val
    path = env_file or os.path.join(HERMES_HOME, ".env")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    except Exception:
        pass
    return None


def call_llm(thread_text, model, base_url, api_key, timeout=60.0, max_tokens=2000):
    """One bounded JSON call. Returns the parsed object, or None on any failure.

    max_tokens must cover REASONING as well as the answer on a reasoning model:
    deepseek-v4-pro spent all 600 tokens of a first attempt on reasoning_tokens
    and returned an empty content with finish_reason=length. 2000 leaves room.
    """
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": INSTRUCTIONS},
            {"role": "user", "content": thread_text[:12000]},
        ],
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % api_key},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        choice = body["choices"][0]
        content = choice["message"]["content"]
    except (urllib.error.URLError, KeyError, IndexError, ValueError, OSError) as exc:
        sys.stderr.write("llm: call failed (%s)\n" % exc)
        return None
    if not (content or "").strip():
        sys.stderr.write("llm: empty content (finish_reason=%s) — raise --max-tokens\n"
                         % choice.get("finish_reason"))
        return None
    try:
        m = re.search(r"\{.*\}", content, re.S)
        return json.loads(m.group(0) if m else content)
    except Exception:
        sys.stderr.write("llm: unparseable JSON response\n")
        return None


def normalise(result):
    """Coerce a model result into a note spec, or None when nothing to store."""
    if not isinstance(result, dict) or not result.get("store"):
        return None
    facts = [str(f).strip() for f in (result.get("facts") or []) if str(f).strip()]
    summary = str(result.get("summary") or "").strip()
    if not facts and not summary:
        return None
    section = str(result.get("section") or "").strip()
    if section not in SECTIONS:
        section = "_team"
    page = re.sub(r"[^a-z0-9-]+", "-", str(result.get("page") or "").lower()).strip("-")
    return {"section": section, "page": page or None,
            "summary": summary[:300], "facts": facts[:8]}


# --------------------------------------------------------------------------
# note emission
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# retitle backfill
#
# The plugin retitles a thread at its next summary roll-up (every
# summarize_every turns), which never happens for a dormant thread nobody
# reopens. This backfills those names from what the thread was about.
# --------------------------------------------------------------------------
RETITLE_INSTRUCTIONS = """You name conversation threads.

Given a thread's current auto-generated name, its keywords, and any surviving
exchanges, return a short noun-phrase name for it: 3-6 words naming the SUBJECT,
not the user's request. "Jane Doe OSINT research", not "Now Find Everything
Online". Fix obvious typos. Use the keywords — they are often all that survives.

Always write the title in ENGLISH, even when the thread itself was in another
language — these names are browsed as one list.

If there is genuinely nothing to name (an empty or contentless thread), return
the best short label you can rather than an empty string.

Return ONLY a JSON object: {"title": "..."}"""


def is_auto_title(rec):
    """True when a title was machine-generated by the plugin's _title_from —
    the first four stopword-stripped tokens of the opening message, title-cased.
    Reproducing it exactly identifies auto-titles without touching a name the
    user chose. Also catches the shorter/degenerate forms (fewer keywords, or
    the raw-text fallback when a message had no keywords at all)."""
    title = str(rec.get("title") or "").strip()
    ents = [str(e).lower() for e in (rec.get("entities") or [])]
    if not title:
        return True
    if title == " ".join(ents[:4]).title():
        return True
    if not ents:
        return True  # _title_from's raw-text fallback ("Are you there?")
    # Shorter auto-titles ("Vet", "Dissident") are the leading keywords IN ORDER.
    # Requiring the prefix — not merely set membership — is what keeps a human
    # name like "Quarterly board pack" (whose words are all keywords, but in a
    # different order) from being mistaken for a generated one.
    words = [w.lower().strip(".,?!") for w in title.split()]
    return bool(words) and words == ents[:len(words)]


def propose_title(rec, sections, model, base_url, api_key, timeout, max_tokens):
    """One bounded call returning a better title, or None."""
    parts = ["Current name: %s" % (rec.get("title") or "(none)")]
    ents = rec.get("entities") or []
    if ents:
        parts.append("Keywords: %s" % ", ".join(str(e) for e in ents[:20]))
    log = (sections or {}).get("log") or []
    if log:
        parts.append("Exchanges:\n%s" % "\n".join(log[:6]))
    summary = (sections or {}).get("summary") or []
    if summary:
        parts.append("Summary: %s" % " ".join(summary[:3]))
    payload = {
        "model": model, "temperature": 0, "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": RETITLE_INSTRUCTIONS},
                     {"role": "user", "content": "\n".join(parts)[:4000]}],
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % api_key}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"] or ""
        m = re.search(r"\{.*\}", content, re.S)
        title = json.loads(m.group(0) if m else content).get("title")
    except Exception as exc:
        sys.stderr.write("retitle: call failed (%s)\n" % exc)
        return None
    title = " ".join(str(title or "").split())[:80].strip(" .\"'")
    if not title or len(title.split()) > 10:
        return None
    return title


def _slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:60]


def apply_title(topics_dir, tid, title):
    """Rewrite ONLY the title/slug frontmatter lines of t<NNNNN>.md, leaving the
    body byte-identical. Returns True when the file was updated."""
    path = os.path.join(topics_dir, "t%05d.md" % tid)
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
    except Exception:
        return False
    slug = _slugify(title)
    out, in_fm, done, seen_title = [], False, 0, False
    for line in lines:
        if line == "---":
            if in_fm and not seen_title:
                out.append("title: %s" % title)   # frontmatter had no title line
                seen_title = True
            in_fm = not in_fm if done < 2 else in_fm
            if not in_fm:
                done = 2
            out.append(line)
            continue
        if in_fm and line.startswith("title: "):
            out.append("title: %s" % title); seen_title = True; continue
        if in_fm and line.startswith("slug: "):
            out.append("slug: %s" % slug); continue
        out.append(line)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    os.replace(tmp, path)
    return True


def patch_index(topics_dir, updates):
    """Merge {tid: (title, slug)} into _index.json.

    Re-reads immediately before writing and patches only the title/slug of ids
    that are still present, so a topic a live gateway created since we loaded
    is never dropped by a stale-snapshot overwrite.
    """
    path = os.path.join(topics_dir, "_index.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        for row in (data.get("topics") or []):
            upd = updates.get(int(row.get("id", -1)))
            if upd:
                row["title"], row["slug"] = upd
        fd, tmp = tempfile.mkstemp(dir=topics_dir, prefix="._idx-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception as exc:
        sys.stderr.write("retitle: index patch failed (%s)\n" % exc)
        return False


def retitle_vault(vault, args, now=None):
    topics_dir = os.path.join(vault, "topics")
    res = {"vault": vault, "candidates": 0, "retitled": 0, "skipped_named": 0,
           "skipped_warm": 0, "failed": 0, "notes": []}
    if not os.path.isdir(topics_dir):
        res["error"] = "no topics/ store"
        return res
    state = load_state(topics_dir)
    recs = load_index(topics_dir)
    updates = {}
    now = float(now if now is not None else time.time())
    for rec in recs:
        try:
            tid = int(rec.get("id"))
        except Exception:
            continue
        key = "t%05d" % tid
        if not args.force and not is_auto_title(rec):
            res["skipped_named"] += 1
            continue
        # Only name a thread once it has gone quiet: it has whatever content it
        # is ever going to have, and the title can't change out from under a
        # conversation in progress (the reply badge carries the thread name).
        if not args.force:
            try:
                if now - float(rec.get("last_active") or 0) < args.stale_hours * 3600.0:
                    res["skipped_warm"] += 1
                    continue
            except Exception:
                pass
        if (state.get(key) or {}).get("retitled_at") and not args.force:
            continue
        res["candidates"] += 1
        if args.limit and res["retitled"] + res["failed"] >= args.limit:
            continue
        _, sections = parse_topic_md(os.path.join(topics_dir, key + ".md"))
        if args.dry_run:
            res["notes"].append("(dry-run) %s  %s" % (key, rec.get("title")))
            continue
        title = propose_title(rec, sections, args.model, args.base_url,
                              args.api_key, args.timeout, args.max_tokens)
        if not title:
            res["failed"] += 1
            continue
        if apply_title(topics_dir, tid, title):
            updates[tid] = (title, _slugify(title))
            res["retitled"] += 1
            res["notes"].append("%s  %r -> %r" % (key, rec.get("title"), title))
            entry = state.setdefault(key, {})
            entry["retitled_at"] = time.time()
        else:
            res["failed"] += 1
    if updates:
        patch_index(topics_dir, updates)
    if not args.dry_run:
        try:
            save_state(topics_dir, state)
        except Exception as exc:
            sys.stderr.write("retitle: state write failed (%s)\n" % exc)
    return res


def write_note(vault, cand, spec, findings):
    inbox = os.path.join(vault, "inbox", "hermes")
    os.makedirs(inbox, exist_ok=True)
    rec = cand["rec"]
    slug = rec.get("slug") or ("topic-%d" % cand["id"])
    page = spec["page"] or slug
    name = "topic-%s-%s" % (cand["key"], slug)
    out = ["---",
           "target: %s/%s" % (spec["section"], page),
           "name: %s" % name,
           "type: reference",
           "---",
           "# %s (topic %s)" % (rec.get("title") or slug, "t#" + cand["key"][1:])]
    if spec["summary"]:
        out += ["", spec["summary"]]
    if spec["facts"]:
        out += ["", "Facts:"] + ["- " + f for f in spec["facts"]]
    if findings:
        out += ["", "Key findings:"] + ["- " + f.lstrip("- ") for f in findings[:6]]
    path = os.path.join(inbox, name + ".md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    return path


def run_merge(vault, merge_tool, commit=True):
    """Fold this vault's inbox into wiki/ and commit, so every automated merge
    is one revertible commit. Returns (ok, message)."""
    if not os.path.isfile(merge_tool):
        return False, "merge tool not found: %s" % merge_tool
    try:
        proc = subprocess.run([sys.executable, merge_tool, vault],
                              capture_output=True, text=True, timeout=120)
    except Exception as exc:
        return False, "merge failed: %s" % exc
    msg = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if proc.returncode != 0:
        return False, "merge exit %d: %s" % (proc.returncode, msg)
    if commit and os.path.isdir(os.path.join(vault, ".git")):
        try:
            # Stage ONLY what the merge pass owns. `git add -A` would sweep in
            # the plugin's topics/ store, which is rewritten on every turn —
            # that would produce a large churn commit nightly and bury the
            # actual curation diff. Unrelated vault work stays untouched too.
            paths = [p for p in ("wiki", "index.md", "log.md", "inbox")
                     if os.path.exists(os.path.join(vault, p))]
            subprocess.run(["git", "-C", vault, "add", "--"] + paths,
                           capture_output=True, timeout=60)
            st = subprocess.run(["git", "-C", vault, "diff", "--cached",
                                 "--name-only"],
                                capture_output=True, text=True, timeout=60)
            if (st.stdout or "").strip():
                subprocess.run(
                    ["git", "-C", vault, "commit", "-m",
                     "distill: fold topic curation notes (automated)"],
                    capture_output=True, timeout=60)
                msg += " | committed"
            else:
                msg += " | nothing to commit"
        except Exception as exc:
            msg += " | commit skipped (%s)" % exc
    return True, msg


# --------------------------------------------------------------------------
def process_vault(vault, args, now):
    topics_dir = os.path.join(vault, "topics")
    res = {"vault": vault, "candidates": 0, "distilled": 0, "skipped_empty": 0,
           "failed": 0, "deferred": 0, "notes": [], "merge": None}
    if not os.path.isdir(topics_dir):
        res["error"] = "no topics/ store"
        return res
    state = load_state(topics_dir)
    cands = select(topics_dir, state, now, args.stale_hours, args.min_turns)
    res["candidates"] = len(cands)
    if args.limit and len(cands) > args.limit:
        res["deferred"] = len(cands) - args.limit
        cands = cands[:args.limit]
    for cand in cands:
        _, sections = parse_topic_md(cand["path"])
        findings = sections.get("findings") or []
        if args.dry_run:
            res["notes"].append("(dry-run) %s %s" % (cand["key"],
                                                     cand["rec"].get("slug") or ""))
            continue
        result = call_llm(render_thread(cand), args.model, args.base_url,
                          args.api_key, timeout=args.timeout,
                          max_tokens=args.max_tokens)
        if result is None:
            res["failed"] += 1
            continue  # no state write -> retried next run
        spec = normalise(result)
        if spec is None:
            res["skipped_empty"] += 1
        else:
            try:
                path = write_note(vault, cand, spec, findings)
                res["distilled"] += 1
                res["notes"].append("%s -> %s/%s (%d facts)" % (
                    cand["key"], spec["section"], spec["page"] or "", len(spec["facts"])))
            except Exception as exc:
                res["failed"] += 1
                sys.stderr.write("note write failed for %s: %s\n" % (cand["key"], exc))
                continue
        state[cand["key"]] = {"last_active": cand["last_active"],
                              "distilled_at": now,
                              "stored": spec is not None}
    if not args.dry_run:
        try:
            save_state(topics_dir, state)
        except Exception as exc:
            sys.stderr.write("state write failed for %s: %s\n" % (vault, exc))
    if args.merge and res["distilled"] and not args.dry_run:
        res["merge"] = run_merge(vault, args.merge_tool, commit=not args.no_commit)
    return res


def write_report(results, started, args):
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(started))
    d = os.path.join(HERMES_HOME, "logs", "distill", stamp)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        return None
    lines = ["# Distill run — %s UTC" % time.strftime("%Y-%m-%dT%H:%M:%S",
                                                      time.gmtime(started)),
             "",
             "Model: `%s`  ·  stale>=%sh  ·  min-turns %s  ·  limit %s  ·  %s"
             % (args.model, args.stale_hours, args.min_turns,
                args.limit or "none", "DRY RUN" if args.dry_run else "live"),
             ""]
    for r in results:
        lines.append("## %s" % r["vault"])
        if r.get("error"):
            lines += ["", "- skipped: %s" % r["error"], ""]
            continue
        lines += ["",
                  "- candidates: **%d**" % r["candidates"],
                  "- distilled to inbox: **%d**" % r["distilled"],
                  "- nothing durable (store=false): %d" % r["skipped_empty"],
                  "- failed (will retry next run): %d" % r["failed"]]
        if r["deferred"]:
            lines.append("- **deferred by --limit: %d** (picked up next run)"
                         % r["deferred"])
        if r["merge"]:
            lines.append("- merge: %s" % ("OK — " + r["merge"][1] if r["merge"][0]
                                          else "FAILED — " + r["merge"][1]))
        if r["notes"]:
            lines += ["", "### Notes"] + ["- " + n for n in r["notes"]]
        lines.append("")
    path = os.path.join(d, "REPORT.md")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        with open(os.path.join(d, "run.json"), "w", encoding="utf-8") as fh:
            json.dump({"started": started, "results": results}, fh, indent=1)
    except Exception:
        return None
    return path


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--vault", action="append", default=[],
                   help="vault root (repeatable); default: auto-discover")
    p.add_argument("--stale-hours", type=float, default=12.0)
    p.add_argument("--min-turns", type=int, default=2)
    p.add_argument("--limit", type=int, default=25,
                   help="max topics per vault per run (0 = no cap)")
    p.add_argument("--model", default="deepseek-v4-pro")
    p.add_argument("--base-url", default="https://api.deepseek.com/v1")
    p.add_argument("--key-env", default="DEEPSEEK_API_KEY")
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--max-tokens", type=int, default=2000,
                   help="must cover reasoning tokens on a reasoning model")
    p.add_argument("--merge", action="store_true",
                   help="fold inbox into wiki/ and commit after distilling")
    p.add_argument("--merge-tool",
                   default=os.path.join(DEFAULT_WIKI, "tools", "merge_inboxes.py"))
    p.add_argument("--no-commit", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would be distilled; no LLM calls, no writes")
    p.add_argument("--retitle", action="store_true",
                   help="backfill auto-generated topic titles instead of "
                        "distilling (the plugin only retitles at a roll-up, "
                        "which never happens for a dormant thread)")
    p.add_argument("--force", action="store_true",
                   help="--retitle: include titles that do NOT look "
                        "auto-generated, and redo ones already done")
    args = p.parse_args(argv)

    args.api_key = read_env_key(args.key_env)
    if not args.api_key and not args.dry_run:
        sys.stderr.write("no %s in env or %s/.env\n" % (args.key_env, HERMES_HOME))
        return 2

    vaults = args.vault or discover_vaults()
    if not vaults:
        sys.stderr.write("no vaults with topics.enabled: on\n")
        return 1

    started = time.time()
    if args.retitle:
        for r in [retitle_vault(v, args) for v in vaults]:
            if r.get("error"):
                print("%s: skipped (%s)" % (r["vault"], r["error"]))
                continue
            print("%s: %d candidates, %d retitled, %d failed, %d left named, "
                  "%d still warm"
                  % (r["vault"], r["candidates"], r["retitled"], r["failed"],
                     r["skipped_named"], r["skipped_warm"]))
            for n in r["notes"]:
                print("   " + n)
        return 0
    results = [process_vault(v, args, started) for v in vaults]
    report = write_report(results, started, args)
    for r in results:
        print("%s: %d candidates, %d distilled, %d empty, %d failed%s"
              % (r["vault"], r["candidates"], r["distilled"], r["skipped_empty"],
                 r["failed"],
                 ", %d deferred" % r["deferred"] if r["deferred"] else ""))
    if report:
        print("report: %s" % report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
