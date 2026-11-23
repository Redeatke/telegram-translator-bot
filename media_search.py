"""
Image search-and-send for interactive chat (search, NOT generation)
───────────────────────────────────────────────────────────────────
"redster send me a dog with a red ball" -> the top 3 openly-licensed
photos of that, sent as a Telegram media group with attribution.

Source: Openverse API (https://api.openverse.org/v1/images/) — free,
no API key, Creative Commons / public-domain images with creator +
licence metadata in every result. Anonymous tier is limited (~100
requests/day reported), so this module has its own process-wide daily
cap (50 searches) and a 10-minute cache. If Openverse fails or is
empty, Wikimedia Commons search API is the fallback (also free, no
key) — Openverse is Cloudflare-protected and challenges some
datacenter/egress IPs.

Images are downloaded with the same guards as downloaders.py
(is_public_url equivalent, replicated here so this module stays
importable without yt-dlp/Telegram): public http(s) hosts only, max
5 MB per image, content-type must start with image/, 10 s timeout.
Never raises for user input — search/download failures return []/None
and the caller replies politely instead.
"""
from __future__ import annotations

import ipaddress
import re
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional
from urllib.parse import quote_plus, urlparse

import httpx

# ─── Tunables ────────────────────────────────────────────────────────────────
IMAGE_TIMEOUT = 10.0
IMAGE_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per image
IMAGE_SEARCH_DAILY_CAP = 50
CACHE_TTL_SECONDS = 600  # 10 minutes
CACHE_MAX_ENTRIES = 100
DEFAULT_COUNT = 3

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# Simple explicit-content blocklist (whole-word match on the query).
EXPLICIT_WORDS = {
    "porn", "sex", "nude", "naked", "xxx", "hentai", "erotic",
    "pornhub", "onlyfans", "nsfw",
}


def is_explicit_query(text: str) -> bool:
    """True when the image query contains an explicit-content word."""
    if not text:
        return False
    words = set(re.findall(r"[a-z0-9]+", text.lower()))
    return bool(words & EXPLICIT_WORDS)


# ─── Public-URL check (mirrors downloaders.is_public_url) ───────────────────
_BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain"}


def is_public_url(url: str) -> bool:
    """True only for http/https URLs to a non-private host."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").lower().strip().rstrip(".")
    if not host or host in _BLOCKED_HOSTNAMES:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # a domain name — allowed (no full DNS-rebinding defence)
    return bool(ip.is_global)


# ─── Cache + daily cap ───────────────────────────────────────────────────────
_cache: Dict[str, tuple] = {}
_cache_lock = threading.Lock()
_searches_by_day: Dict[str, int] = {}


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


def clear_image_cache() -> None:
    with _cache_lock:
        _cache.clear()


def image_searches_today() -> int:
    """How many Openverse searches this process has done today (UTC)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _cache_lock:
        return _searches_by_day.get(today, 0)


def _note_search() -> bool:
    """Record one search; return False when the daily cap is already hit."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _cache_lock:
        count = _searches_by_day.get(today, 0)
        if count >= IMAGE_SEARCH_DAILY_CAP:
            return False
        _searches_by_day[today] = count + 1
        return True


def reset_daily_cap_for_tests() -> None:
    with _cache_lock:
        _searches_by_day.clear()


# ─── Openverse search ────────────────────────────────────────────────────────
async def _default_get(url: str, headers: dict, timeout: float):
    async with httpx.AsyncClient(
        headers=headers, timeout=timeout, follow_redirects=True
    ) as client:
        return await client.get(url)


def parse_openverse_json(payload: dict, count: int = DEFAULT_COUNT) -> List[Dict[str, str]]:
    """Parse an Openverse /v1/images/ response into result dicts."""
    items = []
    try:
        items = payload.get("results") or []
    except AttributeError:
        return []
    out: List[Dict[str, str]] = []
    for item in items[:count]:
        url = str(item.get("url") or "").strip()
        if not url or not url.startswith(("http://", "https://")):
            continue
        out.append(
            {
                "url": url,
                "title": str(item.get("title") or "Untitled").strip(),
                "creator": str(item.get("creator") or "Unknown").strip(),
                "license": str(item.get("license") or "").strip(),
                "attribution": str(item.get("attribution") or "").strip(),
            }
        )
    return out


async def _search_wikimedia(query: str, count: int, _get) -> List[Dict[str, str]]:
    """Fallback image source: Wikimedia Commons search API (free, no key).

    Openverse sits behind Cloudflare and challenges some datacenter /
    egress IPs (observed from the sandbox: 403 "Just a moment..." and
    truncated responses). Commons is a second, independent source so
    image search still works on such networks.
    """
    url = (
        "https://commons.wikimedia.org/w/api.php?action=query&generator=search"
        f"&gsrsearch={quote_plus(query)}%20filetype%3Abitmap&gsrnamespace=6"
        f"&gsrlimit={count}&prop=imageinfo&iiprop=url%7Cextmetadata"
        "&iiurlwidth=1200&format=json"
    )
    resp = await _get(url, {"User-Agent": _BROWSER_UA}, IMAGE_TIMEOUT)
    if getattr(resp, "status_code", 200) != 200:
        return []
    try:
        payload = resp.json() if hasattr(resp, "json") else None
        if payload is None:
            import json as _json

            payload = _json.loads(resp.text)
        pages = (payload.get("query") or {}).get("pages") or {}
    except Exception:
        return []
    out: List[Dict[str, str]] = []
    for page in list(pages.values())[:count]:
        info = (page.get("imageinfo") or [{}])[0]
        img_url = str(info.get("thumburl") or info.get("url") or "").strip()
        if not img_url.startswith(("http://", "https://")):
            continue
        meta = info.get("extmetadata") or {}
        artist = _strip_html(str((meta.get("Artist") or {}).get("value") or ""))
        lic = str((meta.get("LicenseShortName") or {}).get("value") or "").strip()
        title = str(page.get("title") or "").strip()
        if title.startswith("File:"):
            title = title[5:]
        out.append(
            {
                "url": img_url,
                "title": title or "Untitled",
                "creator": artist or "Wikimedia Commons",
                "license": lic,
                "attribution": "",
            }
        )
    return out


def _strip_html(text: str) -> str:
    import html as _html
    import re as _re

    text = _re.sub(r"<[^>]+>", "", text or "")
    return _html.unescape(" ".join(text.split()))


async def search_images(
    query: str,
    count: int = DEFAULT_COUNT,
    _get: Optional[Callable] = None,
) -> List[Dict[str, str]]:
    """Search Openverse for `query`. Returns [] on failure / cap — never raises."""
    if not query or not isinstance(query, str) or not query.strip():
        return []
    query = query.strip()
    cached = _cache_get(query)
    if cached is not None:
        return cached[:count]
    if not _note_search():
        return []

    get = _get or _default_get
    url = (
        "https://api.openverse.org/v1/images/?q=" + quote_plus(query)
        + f"&page_size={count}&filter_dead=True&mature=false"
    )
    results: List[Dict[str, str]] = []
    try:
        resp = await get(url, {"User-Agent": _BROWSER_UA}, IMAGE_TIMEOUT)
        if getattr(resp, "status_code", 200) == 200:
            payload = resp.json() if hasattr(resp, "json") else None
            if payload is None:
                import json as _json

                payload = _json.loads(resp.text)
            results = parse_openverse_json(payload, count)
    except Exception:
        results = []
    if not results:
        # Openverse is Cloudflare-protected and challenges some egress
        # IPs — fall back to Wikimedia Commons so the feature still works.
        try:
            results = await _search_wikimedia(query, count, get)
        except Exception:
            results = []
    _cache_put(query, results)
    return results[:count]


# ─── Image download (same guards as downloaders.py) ─────────────────────────
async def download_image(
    url: str,
    max_bytes: int = IMAGE_MAX_BYTES,
    _get: Optional[Callable] = None,
) -> Optional[bytes]:
    """Download one image, or None. Public URL only, <= max_bytes,
    content-type must start with image/. Streams so a lying
    Content-Length cannot OOM the host."""
    if not is_public_url(url):
        return None
    get = _get or _default_get
    try:
        if _get is not None:
            resp = await get(url, {"User-Agent": _BROWSER_UA}, IMAGE_TIMEOUT)
            if getattr(resp, "status_code", 200) != 200:
                return None
            ctype = ""
            headers = getattr(resp, "headers", {}) or {}
            ctype = str(headers.get("content-type", "") or headers.get("Content-Type", ""))
            if ctype and not ctype.lower().startswith("image/"):
                return None
            data = resp.content if hasattr(resp, "content") else b""
            if not data or len(data) > max_bytes:
                return None
            return bytes(data)
        # Real path: stream with httpx so size is enforced while reading.
        async with httpx.AsyncClient(
            headers={"User-Agent": _BROWSER_UA},
            timeout=IMAGE_TIMEOUT,
            follow_redirects=True,
        ) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return None
                ctype = resp.headers.get("content-type", "")
                if ctype and not ctype.lower().startswith("image/"):
                    return None
                data = bytearray()
                async for chunk in resp.aiter_bytes(65536):
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        return None
                if not data:
                    return None
                return bytes(data)
    except Exception:
        return None


# ─── Parsing an image request out of chat text ───────────────────────────────
_MEDIA_WORDS = r"(?:pictures?|images?|photos?|pics?)"
_LEADING_ARTICLES = ("a ", "an ", "the ", "some ")


def _strip_articles(query: str) -> str:
    q = query.strip()
    low = q.lower()
    for art in _LEADING_ARTICLES:
        if low.startswith(art):
            q = q[len(art):].strip()
            break
    return q.strip(" .!?")


def _strip_media_prefix(query: str) -> str:
    """Strip a leading "pictures of / images of / pics of" from a query."""
    q = query.strip()
    m = re.match(
        r"^(?:pictures?|images?|photos?|pics?)\s+(?:of\s+)?(.+)$",
        q,
        re.IGNORECASE,
    )
    return m.group(1).strip() if m else q


def _parse_broad(text: str) -> Optional[str]:
    """Wake-word / mention context: "send me <anything>" is an image ask.

    Used when the message already had a trigger (redster wake, @mention,
    reply-to-bot, /ask) — "send me a dog with a red ball" -> the query.
    """
    m = re.match(
        r"^(?:send|show|find|give|get)\s+me\s+(?:some\s+)?(.+)$",
        text.strip(),
        re.IGNORECASE,
    )
    if m:
        return _strip_articles(_strip_media_prefix(m.group(1))) or None
    m = re.match(
        r"^(?:send|show|find)\s+(?:some\s+)?" + _MEDIA_WORDS + r"\s+(?:of\s+)?(.+)$",
        text.strip(),
        re.IGNORECASE,
    )
    if m:
        return _strip_articles(m.group(1)) or None
    return None


def _parse_narrow(text: str) -> Optional[str]:
    """No wake word: only an explicit picture request counts, so plain
    chat ("send me a message", "tell me about dogs") never misfires."""
    m = re.match(
        r"^(?:send|show|find|give|get)\s+me\s+(?:some\s+|a\s+|an\s+|the\s+)?" + _MEDIA_WORDS + r"\s+(?:of\s+)?(.+)$",
        text.strip(),
        re.IGNORECASE,
    )
    if m:
        return _strip_articles(m.group(1)) or None
    m = re.match(
        r"^(?:send|show|find)\s+(?:some\s+)?" + _MEDIA_WORDS + r"\s+(?:of\s+)?(.+)$",
        text.strip(),
        re.IGNORECASE,
    )
    if m:
        return _strip_articles(m.group(1)) or None
    m = re.match(
        r"^(?:a\s+)?(?:picture|image|photo|pic)\s+of\s+(.+)$",
        text.strip(),
        re.IGNORECASE,
    )
    if m:
        return _strip_articles(m.group(1)) or None
    return None


def parse_image_request(text: str, parsed_redster=None, broad: bool = False) -> Optional[str]:
    """Extract an image query from chat text, or None.

    - "/pic <query>" -> query.
    - parsed_redster with intent "quote" -> None (quote wins).
    - parsed_redster with intent "chat" -> broad parse of its chat_text.
    - Otherwise: strip a leading bot @mention / redster wake word if
      present, then broad parse; plain text without any trigger gets
      the narrow parse (picture words required).
    - broad=True (used when the caller already knows a trigger fired,
      e.g. a redster wake word, @mention, reply-to-bot or /ask):
      "send me <anything>" counts even without picture words.
    """
    if not text or not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None

    # Explicit command: /pic <query>
    if s.lower().startswith("/pic"):
        rest = s[4:].strip()
        return _strip_articles(rest) or None

    # Redster-parsed input (bot.py passes its parse result through).
    if parsed_redster is not None:
        if parsed_redster.get("intent") == "quote":
            return None
        chat_text = (parsed_redster.get("chat_text") or "").strip()
        if not chat_text:
            return None
        return _parse_broad(chat_text)

    # Leading @mention counts as a trigger ("@R3dSter_Bot send me ...").
    m = re.match(r"^@\S+\s+(.+)$", s)
    if m:
        return _parse_broad(m.group(1))

    # Leading redster wake word counts as a trigger.
    try:
        import redster as _redster

        parsed = _redster.parse_redster_message(s)
        if parsed is not None:
            if parsed.get("intent") == "quote":
                return None
            return _parse_broad(parsed.get("chat_text") or "")
    except Exception:
        pass

    if broad:
        return _parse_broad(s)
    return _parse_narrow(s)


def attribution_caption(item: Dict[str, str]) -> str:
    """One-line credit for an image, for the media-group caption."""
    title = (item.get("title") or "Untitled").strip()
    creator = (item.get("creator") or "Unknown").strip()
    lic = (item.get("license") or "").strip()
    line = f"📷 {title} — {creator}"
    if lic:
        line += f" ({lic})"
    return line
