# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Podwarden Inc

"""Local web_extract backend — direct httpx fetch + stdlib HTML-to-text.

Registered by the router plugin (see __init__.py) via
PluginContext.register_web_search_provider (hermes_cli/plugins.py:740), which
feeds agent.web_search_registry.register_provider (web_search_registry.py:48).
Selected with ``web.extract_backend: local`` in config.yaml; the dispatcher in
tools/web_tools.py:web_extract_tool resolves the name through
_get_extract_backend() -> registry get_provider() and calls extract() in a
worker thread (sync providers are wrapped with asyncio.to_thread).

Why it exists: this install runs SearXNG (search-only) with no
firecrawl/tavily/exa keys, so web_extract had NO backend at all — the model
could find URLs but never read them. This provider fetches pages directly
from this machine and converts HTML to readable text using only the stdlib
html.parser (the venv ships no bs4/lxml/markdownify/trafilatura), keeping
headings, paragraphs, list items, and pre blocks.

Scope limits (deliberate):
  * max 3 URLs per call (extras come back as per-URL errors, not silently
    dropped);
  * 15 s timeout per URL, ~2 MB fetch cap, 5 redirect hops max;
  * every hop (including redirect targets) is re-checked against
    private/loopback/link-local addresses — the dispatcher's SSRF gate only
    sees the URL the model asked for, not where redirects lead;
  * clean text capped at 200k chars — the dispatcher then applies the real
    per-page char_limit (web.extract_char_limit, default 15000) with its
    head+tail truncation and cache/web full-text store.

Fail-safe: build_provider() returns None when the WebSearchProvider ABC is
unimportable (upstream rename / stripped install); the caller logs a warning
and web_extract simply keeps its previous behavior.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

logger = logging.getLogger("hermes.plugins.router.web_local")

PROVIDER_NAME = "local"
MAX_URLS_PER_CALL = 3
FETCH_TIMEOUT_S = 15.0
MAX_FETCH_BYTES = 2_000_000
MAX_REDIRECT_HOPS = 5
MAX_CLEAN_CHARS = 200_000
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 "
    "Firefox/128.0 HermesLocalExtract/1.0"
)

try:  # guarded: absent/renamed ABC must never break plugin load
    from agent.web_search_provider import WebSearchProvider as _ProviderBase
    _BASE_OK = True
except Exception:  # noqa: BLE001
    _ProviderBase = object  # type: ignore[assignment,misc]
    _BASE_OK = False


# ---------------------------------------------------------------------------
# HTML -> readable text (stdlib only)
# ---------------------------------------------------------------------------

# NOTE: "head" is deliberately NOT skipped — <title> lives inside it and the
# title callbacks would never fire (skip-depth check runs first). head's noisy
# children (script/style/template) are skipped individually; meta/link carry
# no text data.
_SKIP_TAGS = frozenset({
    "script", "style", "noscript", "template", "svg",
    "iframe", "canvas", "object", "embed", "audio", "video",
})
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "header", "footer", "main", "aside",
    "nav", "ul", "ol", "table", "thead", "tbody", "tr", "figure",
    "figcaption", "blockquote", "details", "summary", "form", "fieldset",
    "dl", "dt", "dd", "hr", "li", "pre",
}) | frozenset(_HEADINGS)


class _HTMLToText(HTMLParser):
    """Minimal readable-text extractor.

    Keeps document structure the model actually uses — headings (as
    ``#``-prefixed lines), paragraphs, list items (``- ``), pre blocks
    (whitespace preserved), table cells (space-joined) — and drops
    script/style/nav-chrome payloads. Whitespace inside non-pre blocks is
    collapsed to single spaces.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._blocks: list[tuple[str, str]] = []  # (kind, text)
        self._buf: list[str] = []
        self._skip_depth = 0
        self._pre_depth = 0
        self._kind = "p"  # kind of the block currently being buffered
        self._in_title = False
        self.title = ""

    # -- block plumbing --
    def _flush(self) -> None:
        text = "".join(self._buf)
        self._buf = []
        if self._pre_depth > 0:
            text = text.strip("\n")
            if text.strip():
                self._blocks.append(("pre", text))
            return
        text = " ".join(text.split())
        if text:
            self._blocks.append((self._kind, text))

    # -- parser callbacks --
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "br":
            self._buf.append("\n" if self._pre_depth else " ")
            return
        if tag in ("td", "th"):
            self._buf.append(" | ")
            return
        if tag in _BLOCK_TAGS:
            self._flush()
            if tag in _HEADINGS:
                self._kind = tag
            elif tag == "li":
                self._kind = "li"
            elif tag == "pre":
                self._pre_depth += 1
                self._kind = "pre"
            else:
                self._kind = "p"

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in _BLOCK_TAGS:
            self._flush()
            if tag == "pre":
                self._pre_depth = max(0, self._pre_depth - 1)
            self._kind = "p"

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
            return
        self._buf.append(data)

    # -- result assembly --
    def get_text(self) -> str:
        self._flush()
        out: list[str] = []
        prev_kind = None
        for kind, text in self._blocks:
            if kind in _HEADINGS:
                text = "#" * _HEADINGS[kind] + " " + text
            elif kind == "li":
                text = "- " + text
            if not out:
                out.append(text)
            elif kind == "li" and prev_kind == "li":
                out.append("\n" + text)
            else:
                out.append("\n\n" + text)
            prev_kind = kind
        joined = "".join(out)
        return re.sub(r"\n{3,}", "\n\n", joined).strip()


def html_to_text(html: str) -> tuple[str, str]:
    """Return (clean_text, title). Never raises — falls back to a regex strip."""
    try:
        parser = _HTMLToText()
        parser.feed(html)
        parser.close()
        return parser.get_text(), " ".join(parser.title.split())
    except Exception as exc:  # noqa: BLE001 — malformed HTML must not kill the call
        logger.debug("html.parser failed (%s); regex fallback", exc)
        stripped = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
        stripped = re.sub(r"(?s)<[^>]+>", " ", stripped)
        stripped = unescape(stripped)
        title_m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        title = " ".join(unescape(title_m.group(1)).split()) if title_m else ""
        return " ".join(stripped.split()), title


# ---------------------------------------------------------------------------
# Fetch with per-hop private-network guard
# ---------------------------------------------------------------------------


def _url_block_reason(url: str) -> str | None:
    """Return a block reason when *url* is not a safely-fetchable public URL.

    The dispatcher's SSRF gate (async_is_safe_url in web_extract_tool) checks
    only the URLs the model asked for; we re-check EVERY hop here because
    redirects can point anywhere. DNS resolution happens at fetch time anyway,
    so getaddrinfo here is not extra latency in practice.
    """
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return "unparseable URL"
    if parsed.scheme not in ("http", "https"):
        return f"unsupported scheme {parsed.scheme!r}"
    host = parsed.hostname
    if not host:
        return "URL has no host"
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        return f"DNS resolution failed: {exc}"
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return f"resolves to a private/internal address ({addr})"
    return None


def _fetch(url: str) -> dict:
    """Fetch one URL and return a result dict in the provider contract shape."""
    import httpx

    def _err(msg: str) -> dict:
        return {"url": url, "title": "", "content": "", "raw_content": "",
                "error": msg}

    current = url
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
        "Accept-Language": "en-US,en;q=0.8",
    }
    try:
        with httpx.Client(timeout=FETCH_TIMEOUT_S, follow_redirects=False,
                          headers=headers) as client:
            for _hop in range(MAX_REDIRECT_HOPS + 1):
                reason = _url_block_reason(current)
                if reason:
                    return _err(f"blocked: {reason}")
                with client.stream("GET", current) as resp:
                    if resp.is_redirect:
                        loc = resp.headers.get("location")
                        if not loc:
                            return _err(f"HTTP {resp.status_code} redirect "
                                        "with no Location header")
                        current = urljoin(current, loc)
                        continue
                    if resp.status_code >= 400:
                        return _err(f"HTTP {resp.status_code}")
                    body = b""
                    for chunk in resp.iter_bytes():
                        body += chunk
                        if len(body) >= MAX_FETCH_BYTES:
                            break
                    ctype = (resp.headers.get("content-type") or "").lower()
                    encoding = resp.charset_encoding or "utf-8"
                    break
            else:
                return _err(f"too many redirects (>{MAX_REDIRECT_HOPS})")
    except httpx.TimeoutException:
        return _err(f"timed out after {FETCH_TIMEOUT_S:.0f}s")
    except httpx.HTTPError as exc:
        return _err(f"request failed: {exc}")
    except Exception as exc:  # noqa: BLE001 — one bad URL must not kill the batch
        logger.debug("local extract: unexpected failure for %s", url, exc_info=True)
        return _err(f"fetch failed: {exc}")

    try:
        text = body.decode(encoding, errors="replace")
    except (LookupError, ValueError):
        text = body.decode("utf-8", errors="replace")

    is_html = ("html" in ctype) or (
        not ctype and re.search(r"(?is)<html|<body|<div|<p[ >]", text[:4000])
    )
    if is_html:
        clean, title = html_to_text(text)
    elif ctype.startswith("text/") or any(
            t in ctype for t in ("json", "xml", "yaml", "markdown", "javascript")):
        clean, title = text, ""
    else:
        return _err(f"unsupported content type {ctype!r} — this backend reads "
                    "HTML/text pages only")

    if not clean.strip():
        return _err("page fetched but no readable text found")
    truncated_fetch = len(body) >= MAX_FETCH_BYTES
    if len(clean) > MAX_CLEAN_CHARS:
        clean = clean[:MAX_CLEAN_CHARS]
        truncated_fetch = True
    return {
        "url": url,
        "title": title,
        "content": clean,
        "raw_content": clean,
        "metadata": {
            "backend": PROVIDER_NAME,
            "final_url": current,
            "content_type": ctype,
            "fetched_bytes": len(body),
            "fetch_truncated": truncated_fetch,
        },
    }


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class LocalWebExtractProvider(_ProviderBase):  # type: ignore[misc,valid-type]
    """Extract-only provider: direct fetch from this machine, no API key."""

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def display_name(self) -> str:
        return "Local fetcher"

    def is_available(self) -> bool:
        # Cheap, no-network probe per the ABC contract: httpx importable.
        import importlib.util
        return importlib.util.find_spec("httpx") is not None

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    def extract(self, urls, **kwargs):
        urls = [str(u) for u in (urls or [])]
        results = []
        for u in urls[:MAX_URLS_PER_CALL]:
            results.append(_fetch(u))
        for u in urls[MAX_URLS_PER_CALL:]:
            results.append({
                "url": u, "title": "", "content": "", "raw_content": "",
                "error": (f"skipped: local extract reads at most "
                          f"{MAX_URLS_PER_CALL} URLs per call — call "
                          "web_extract again with the remaining URLs"),
            })
        return results

    def get_setup_schema(self):
        return {
            "name": "Local fetcher",
            "badge": "free · no key",
            "tag": ("Fetches pages directly from this machine (httpx + stdlib "
                    "HTML-to-text). Extract only — pair with a search backend."),
            "env_vars": [],
        }


def build_provider():
    """Return a provider instance, or None when the ABC is unimportable."""
    if not _BASE_OK:
        return None
    return LocalWebExtractProvider()
