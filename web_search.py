"""
Live web search for interactive chat (no API key, no new dependency)
────────────────────────────────────────────────────────────────────
Why this exists: the chat model (Gemini 2.5 Flash) has knowledge frozen
~2024 and no clock — it told users "It's 2024!" and that the 2025
Superman movie "isn't out yet". The date fix in chat_agent.py tells it
today's date, but facts after 2024 still need live data. This module
fetches it:

- Primary: DuckDuckGo HTML endpoint (free, no key), parsed with the
  stdlib html.parser — no beautifulsoup4. DDG's HTML is fragile and
  it rate-limits scrapers, so this is best-effort by design.
- Fallback: Wikipedia search API (JSON, stable, no key) when DDG
  fails, returns nothing, or serves a challenge page.
- Results are cached 10 minutes per normalised query.

Never raises for user input: on any failure search_web() returns [] and
the caller falls back to "I don't know" instead of fabricating.
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import re
import threading
import time
from html.parser import HTMLParser
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx

# ─── Tunables ────────────────────────────────────────────────────────────────
SEARCH_TIMEOUT = 10.0
CACHE_TTL_SECONDS = 600  # 10 minutes
CACHE_MAX_ENTRIES = 100
MAX_SNIPPET_CHARS = 200

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# Phrases (lowercase, substring match) that mean "this needs live info".
# Explicit and testable — no per-topic special cases (no "superman" rule):
# a 2025/2026 year in the text triggers via the year regex instead.
# Deliberately NOT included: bare "will" (so "will you translate this"
# stays False) — only "will it" / "will there be" trigger.
_SEARCH_PHRASES = (
    "what year", "what date", "what day is it", "today", "current",
    "latest", "recent", "news", "who won", "when did", "is it out",
    "has it come out", "out yet", "released", "release date",
    "how much did it make", "box office", "score", "result", "weather",
    # Entertainment / media: a question naming a movie/show almost
    # always wants current info (release, cast, trailer, season...).
    "movie", "film", "trailer", "tv show", "tv series", "season",
    "episode", "upcoming", "announced", "announcement", "cast",
    "who plays", "who is playing", "directed by", "directed", "marvel", "anime",
    "streaming", "netflix", "coming out", "comes out",
    "when is it coming", "is it coming", "new movie", "new film",
    "new season", "new album", "new song", "new game", "concert", "tour",
    # Future tense / speculation about real-world things.
    "gonna", "going to", "will it", "will there be", "is there gonna",
    "is there going to", "what's next", "what is next",
    # Release / status.
    "release", "premiere", "premieres", "in theaters", "delayed",
    "cancelled", "renewed",
)
_YEAR_RE = re.compile(r"\b(2025|2026)\b")


def needs_web_search(text: str) -> bool:
    """True when `text` looks like it needs live / post-2024 information.

    Pure and testable. "hello how are you" / "tell me a joke" -> False;
    "what year is it?", "is the new superman out yet", "superman 2025
    box office" -> True.
    """
    if not text or not isinstance(text, str):
        return False
    low = text.lower()
    if _YEAR_RE.search(low):
        return True
    return any(phrase in low for phrase in _SEARCH_PHRASES)


# ─── Cache ───────────────────────────────────────────────────────────────────
_cache: Dict[str, tuple] = {}
_cache_lock = threading.Lock()


def _cache_get(query: str) -> Optional[List[Dict[str, str]]]:
    key = " ".join((query or "").lower().split())
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        ts, results = entry
        if time.time() - ts > CACHE_TTL_SECONDS:
            _cache.pop(key, None)
            return None
        return [dict(r) for r in results]


def _cache_put(query: str, results: List[Dict[str, str]]) -> None:
    key = " ".join((query or "").lower().split())
    with _cache_lock:
        if len(_cache) >= CACHE_MAX_ENTRIES:
            _cache.pop(next(iter(_cache)), None)
        _cache[key] = (time.time(), [dict(r) for r in results])


def clear_search_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ─── DuckDuckGo HTML parsing (stdlib only) ───────────────────────────────────
def _unwrap_ddg_url(href: str) -> str:
    """DDG wraps result links in //duckduckgo.com/l/?uddg=<target>.

    Decode the uddg param when present; otherwise return the href as-is
    (adding https: to protocol-relative URLs).
    """
    if not href:
        return ""
    href = href.strip()
    if href.startswith("//"):
        href = "https:" + href
    try:
        parsed = urlparse(href)
        if "duckduckgo.com" in (parsed.netloc or ""):
            qs = parse_qs(parsed.query)
            targets = qs.get("uddg") or qs.get("uddg".lower())
            if targets:
                return unquote(targets[0])
            # A DDG redirect with no usable target is not a real result.
            if "/l/?" in parsed.path or parsed.path.startswith("/l/"):
                return ""
    except Exception:
        return href
    return href


class _DDGParser(HTMLParser):
    """Collect DuckDuckGo HTML results.

    A result looks like:
        <div class="result ...">
          <a class="result__a" href="...">Title</a>
          <a class="result__snippet" href="...">Snippet text</a>
        </div>
    We track whether we are inside a result div (by class) and which
    anchor we are inside, and pair each title link with the snippet
    that follows it in the same result block.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: List[Dict[str, str]] = []
        self._in_result = False
        self._result_depth = 0
        self._capture: Optional[str] = None  # "title" | "snippet" | None
        self._title = ""
        self._title_href = ""
        self._snippet = ""

    def _flush(self) -> None:
        url = _unwrap_ddg_url(self._title_href)
        title = " ".join(self._title.split())
        snippet = " ".join(self._snippet.split())
        if url and title:
            self.results.append(
                {"title": title, "url": url, "snippet": snippet}
            )
        self._title = ""
        self._title_href = ""
        self._snippet = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs_d = dict(attrs)
        classes = attrs_d.get("class", "")
        if tag == "div" and "result" in classes.split():
            if not self._in_result:
                self._in_result = True
                self._result_depth = 1
                self._title = ""
                self._title_href = ""
                self._snippet = ""
            else:
                self._result_depth += 1
            return
        if self._in_result and tag == "div":
            self._result_depth += 1
        if not self._in_result:
            return
        if tag == "a" and "result__a" in classes.split():
            # A new title link inside the same result flushes the last one.
            if self._title:
                self._flush()
            self._capture = "title"
            self._title_href = attrs_d.get("href", "")
        elif tag == "a" and "result__snippet" in classes.split():
            self._capture = "snippet"

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture:
            self._capture = None
        if tag == "div" and self._in_result:
            self._result_depth -= 1
            if self._result_depth <= 0:
                self._flush()
                self._in_result = False

    def handle_data(self, data: str) -> None:
        if not self._in_result or not self._capture:
            return
        if self._capture == "title":
            self._title += data
        elif self._capture == "snippet":
            self._snippet += data


def parse_ddg_html(html_text: str, max_results: int = 5) -> List[Dict[str, str]]:
    """Parse DuckDuckGo HTML into [{title, url, snippet}], deduplicated."""
    if not html_text:
        return []
    parser = _DDGParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        return []
    seen = set()
    out: List[Dict[str, str]] = []
    for r in parser.results:
        url = (r.get("url") or "").strip()
        if not url or url in seen:
            continue
        if not url.startswith(("http://", "https://")):
            continue
        seen.add(url)
        out.append(
            {
                "title": (r.get("title") or "").strip(),
                "url": url,
                "snippet": (r.get("snippet") or "").strip()[:MAX_SNIPPET_CHARS],
            }
        )
        if len(out) >= max_results:
            break
    return out


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return _html.unescape(" ".join(text.split()))


def parse_wikipedia_json(payload: dict, max_results: int = 5) -> List[Dict[str, str]]:
    """Parse a Wikipedia search-API response into result dicts."""
    try:
        items = payload["query"]["search"]
    except (KeyError, TypeError):
        return []
    out: List[Dict[str, str]] = []
    for item in items[:max_results]:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        out.append(
            {
                "title": title,
                "url": "https://en.wikipedia.org/wiki/"
                + quote_plus(title.replace(" ", "_")),
                "snippet": _strip_html(str(item.get("snippet") or ""))[:MAX_SNIPPET_CHARS],
            }
        )
    return out


# ─── HTTP layer (injectable for tests) ───────────────────────────────────────
async def _default_get(url: str, headers: dict, timeout: float):
    """Default fetcher: one httpx AsyncClient per call (low volume)."""
    async with httpx.AsyncClient(
        headers=headers, timeout=timeout, follow_redirects=True
    ) as client:
        return await client.get(url)


async def _search_ddg(query: str, max_results: int, _get) -> List[Dict[str, str]]:
    url = "https://html.duckduckgo.com/html/?q=" + quote_plus(query)
    resp = await _get(url, {"User-Agent": _BROWSER_UA}, SEARCH_TIMEOUT)
    status = getattr(resp, "status_code", 200)
    if status in (403, 429):
        return []
    text = getattr(resp, "text", "") or ""
    if not text or "challenge" in text.lower()[:2000] and "result__a" not in text:
        return []
    return parse_ddg_html(text, max_results)


async def _search_wikipedia(query: str, max_results: int, _get) -> List[Dict[str, str]]:
    url = (
        "https://en.wikipedia.org/w/api.php?action=query&list=search"
        f"&srsearch={quote_plus(query)}&format=json&srlimit={max_results}"
    )
    resp = await _get(url, {"User-Agent": _BROWSER_UA}, SEARCH_TIMEOUT)
    if getattr(resp, "status_code", 200) != 200:
        return []
    try:
        payload = resp.json() if hasattr(resp, "json") else json.loads(resp.text)
    except Exception:
        return []
    return parse_wikipedia_json(payload, max_results)


async def search_web(
    query: str,
    max_results: int = 5,
    _get: Optional[Callable] = None,
) -> List[Dict[str, str]]:
    """Search the web for `query`. Returns [] on any failure — never raises.

    Primary DuckDuckGo HTML, fallback Wikipedia search API. Cached
    10 minutes per normalised query. `_get` is an injectable async
    fetcher (url, headers, timeout) -> response, for tests.
    """
    if not query or not isinstance(query, str) or not query.strip():
        return []
    query = query.strip()
    cached = _cache_get(query)
    if cached is not None:
        return cached[:max_results]

    get = _get or _default_get
    results: List[Dict[str, str]] = []
    try:
        results = await _search_ddg(query, max_results, get)
    except Exception:
        results = []
    if not results:
        try:
            results = await _search_wikipedia(query, max_results, get)
        except Exception:
            results = []

    _cache_put(query, results)
    return results[:max_results]


def format_search_context(query: str, results: List[Dict[str, str]], date_str: str) -> str:
    """Render results as a system-message block for the chat model."""
    lines = [f"Web search results for '{query}' (searched {date_str}):"]
    for i, r in enumerate(results, 1):
        snippet = (r.get("snippet") or "").strip()
        line = f"{i}. {r.get('title', '')}"
        if snippet:
            line += f" — {snippet}"
        line += f" ({r.get('url', '')})"
        lines.append(line)
    lines.append(
        "Answer from these search results and cite the sources by name; "
        "if the results don't cover the question, say so instead of guessing."
    )
    return "\n".join(lines)
