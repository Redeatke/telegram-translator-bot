"""
Image search-and-send for interactive chat (search, NOT generation)
───────────────────────────────────────────────────────────────────
"redster send me a dog with a red ball" -> the top 3 photos of that,
sent as a Telegram media group with attribution.

Sources, in order (all free, no API key):
1. DuckDuckGo Images — general web images (the ones users actually
   ask for: celebrities, movie posters, memes). Token (vqd) + i.js
   JSON endpoint, moderate safe search (p=1). Results are general web
   images, NOT only CC-licensed — attribution is the source page, and
   no CC licence is claimed for them.
2. Openverse API (https://api.openverse.org/v1/images/) — Creative
   Commons / public-domain images with creator + licence metadata.
   Anonymous tier is limited (~100 requests/day reported), so this
   module has its own process-wide daily cap (50 searches) and a
   10-minute cache.
3. Wikimedia Commons search API — also free, no key, CC images.

DuckDuckGo HTML is bot-protected and challenges some datacenter /
egress IPs (observed from the sandbox); the fallbacks exist so image
search still works on such networks. If every source fails or is
empty, search returns [] and the caller replies politely.

Images are downloaded with the same guards as downloaders.py
(is_public_url equivalent, replicated here so this module stays
importable without yt-dlp/Telegram): public http(s) hosts only, max
5 MB per image, content-type must start with image/, 10 s timeout.
Never raises for user input — search/download failures return []/None
and the caller replies politely instead.
"""
from __future__ import annotations

import ipaddress
import random
import re
import threading
import time
import unicodedata
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
    """How many image searches this process has done today (UTC)."""
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


# ─── Surprise-me topics ────────────────────────────────────────────────────
# "send me an image, any image, surprise me" has no real topic — answer
# with a random topic from this list instead of searching the literal
# phrase (which returns nothing anywhere).
SURPRISE_TOPICS = [
    "puppy",
    "sunset over the ocean",
    "cute cat",
    "mountains",
    "city skyline at night",
    "space nebula",
    "forest",
    "beach",
    "funny dog",
    "ocean waves",
    "red panda",
    "northern lights",
]


def surprise_topic() -> str:
    """A random topic for a 'surprise me / any image' request."""
    return random.choice(SURPRISE_TOPICS)


# Words that carry no topic on their own — if a query is only these,
# it is a surprise-me request, not a search for that literal phrase.
_SURPRISE_FILLERS = {
    "any", "random", "surprise", "me", "something", "anything",
    "image", "images", "picture", "pictures", "photo", "photos",
    "pic", "pics", "a", "an", "the", "some", "please", "for", "of", "with",
    # Adjectives from _ADJECTIVES: "send a random image" / "send an
    # interesting pic" leave nothing but the adjective behind.
    "interesting", "cool", "nice", "beautiful", "funny", "cute",
    "awesome", "great", "amazing", "breathtaking",
}


def _strip_leading_filler(s: str) -> str:
    """Strip leading filler words/phrases from a request.

    "Now send a random image" -> "send a random image";
    "can you send me a pic of dogs" -> "send me a pic of dogs";
    "please, send me a picture of a cat" -> "send me a picture of a cat".
    Loops until stable (fillers can stack: "now please send me ...").
    The word boundary (\\b) keeps "so" from eating "some" and "ok"
    from eating "okay"; trailing punctuation after a filler ("please,")
    is consumed too.
    """
    s = (s or "").strip()
    for _ in range(6):
        before = s
        s = s.strip(" \t\r\n,.;:!?\"'()[]")
        m = _LEADING_FILLER_RE.match(s)
        if m:
            s = s[m.end():].strip(" \t\r\n,.;:!?\"'()[]")
        if s == before:
            break
    return s


def _is_surprise_request(query: str) -> bool:
    """True when `query` has no content word left after filler removal."""
    words = re.findall(r"[\w]+", (query or "").lower(), flags=re.UNICODE)
    return bool(words) and not any(w not in _SURPRISE_FILLERS for w in words)


# ─── DuckDuckGo Images (first source — general web images) ─────────────────
_VQD_RE = re.compile(r"""vqd=['"]([\d-]+)['"]""")


def extract_vqd(html: str) -> Optional[str]:
    """Extract the DuckDuckGo search token (vqd) from a results page.

    Pure/testable. Returns None when the page is a challenge/block page
    with no token — the caller then falls through to Openverse.
    """
    if not html or not isinstance(html, str):
        return None
    m = _VQD_RE.search(html)
    return m.group(1) if m else None


def parse_ddg_images_json(payload: dict, count: int = DEFAULT_COUNT) -> List[Dict[str, str]]:
    """Parse a DuckDuckGo i.js images response into result dicts.

    DDG results are general web images, not CC-licensed: `license` is
    always "" and `attribution` is the source page URL, `creator` the
    source domain. Entries without a direct image URL are skipped.
    """
    try:
        items = payload.get("results") or []
    except AttributeError:
        return []
    out: List[Dict[str, str]] = []
    for item in items[:count]:
        url = str(item.get("image") or "").strip()
        if not url or not url.startswith(("http://", "https://")):
            continue
        source_url = str(item.get("url") or "").strip()
        creator = ""
        try:
            creator = urlparse(source_url).hostname or ""
        except Exception:
            creator = ""
        out.append(
            {
                "url": url,
                "title": str(item.get("title") or "Untitled").strip(),
                "creator": creator,
                "license": "",
                "attribution": source_url,
            }
        )
    return out


async def _search_ddg_images(query: str, count: int, _get) -> List[Dict[str, str]]:
    """DuckDuckGo Images: fetch the search page for its vqd token, then
    the i.js JSON endpoint. Moderate safe search (p=1). Returns [] on
    any failure / challenge / empty result — never raises."""
    page_url = (
        "https://duckduckgo.com/?q=" + quote_plus(query)
        + "&iax=images&ia=images"
    )
    resp = await _get(page_url, {"User-Agent": _BROWSER_UA}, IMAGE_TIMEOUT)
    if getattr(resp, "status_code", 200) != 200:
        return []
    vqd = extract_vqd(getattr(resp, "text", "") or "")
    if not vqd:
        return []
    api_url = (
        "https://duckduckgo.com/i.js?l=us-en&o=json&q=" + quote_plus(query)
        + "&vqd=" + vqd + "&f=,,,,,&p=1"
    )
    resp = await _get(api_url, {"User-Agent": _BROWSER_UA}, IMAGE_TIMEOUT)
    if getattr(resp, "status_code", 200) != 200:
        return []
    try:
        payload = resp.json() if hasattr(resp, "json") else None
        if payload is None:
            import json as _json

            payload = _json.loads(resp.text)
    except Exception:
        return []
    return parse_ddg_images_json(payload, count)


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
    """Search for images of `query`, best source first.

    DuckDuckGo Images (general web images) first, then Openverse
    (CC), then Wikimedia Commons (CC). Returns [] on failure / cap —
    never raises. One daily-cap hit and one cache entry per call,
    regardless of how many sources were tried.
    """
    if not query or not isinstance(query, str) or not query.strip():
        return []
    query = query.strip()
    cached = _cache_get(query)
    if cached is not None:
        return cached[:count]
    if not _note_search():
        return []

    get = _get or _default_get
    results: List[Dict[str, str]] = []
    # 1. DuckDuckGo Images — the only source that covers celebrity /
    #    movie / meme queries. DDG challenges some egress IPs, so a
    #    failure here is normal and just means "try the next source".
    try:
        results = await _search_ddg_images(query, count, get)
    except Exception:
        results = []
    if not results:
        # 2. Openverse (CC photos).
        url = (
            "https://api.openverse.org/v1/images/?q=" + quote_plus(query)
            + f"&page_size={count}&filter_dead=True&mature=false"
        )
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
        # 3. Wikimedia Commons — second independent source so the
        #    feature still works where DDG/Openverse are challenged.
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
_VERBS = r"(?:send|show|find|give|get)"
_LEADING_ARTICLES = ("a ", "an ", "the ", "some ", "any ", "random ")

# Adjectives users put between the verb/article and the media word
# ("send an interesting pic", "show me a cool photo of mountains").
# They carry no topic on their own, so they are stripped when they
# sit directly in front of a media word, and count as filler when a
# query is only made of them ("send a random image" -> surprise-me).
_ADJECTIVES = (
    "interesting", "random", "cool", "nice", "beautiful", "funny",
    "cute", "awesome", "great", "amazing", "breathtaking",
)
_ADJECTIVE_RE = (
    r"(?:interesting|random|cool|nice|beautiful|funny|cute|awesome|"
    r"great|amazing|breathtaking)"
)

# Filler words/phrases users put in front of a request ("Now send a
# random image", "can you send me a pic of dogs"). Stripped from the
# start of the text (repeatedly) before the image patterns run.
_LEADING_FILLER_RE = re.compile(
    r"^(?:can you|could you|would you|will you|now|please|hey|okay|ok|"
    r"and|then|so|just|also)\b[\s,.;:!?]*",
    re.IGNORECASE,
)

# A cleaned query that is only one of these words is not an image
# request at all ("send me a message" must not search for "message").
_NON_IMAGE_WORDS = {
    "message", "messages", "text", "texts", "email", "emails",
    "dm", "dms", "note", "notes", "link", "links", "file", "files",
}


def _drop_emoji(text: str) -> str:
    """Drop emoji/symbols, keep letters in any script.

    Emoji live above U+2500; letters (Latin, Amharic U+1200–U+137F,
    Cyrillic, CJK, ...) are kept via their Unicode category, so
    "አዲስ አበባ" survives while "🐸" / "🐶" / variation selectors do not.
    """
    out = []
    for ch in text:
        if ord(ch) <= 0x2500 or unicodedata.category(ch).startswith("L"):
            out.append(ch)
    return "".join(out)


def _clean_query(query: str) -> str:
    """Normalise a captured image query.

    Loops (max 3 passes) until stable: drops emoji, strips leading
    articles/determiners ("a", "the", "any", "random"), a leading
    "please" / "for me" / "surprise me (with)", a leading media word
    ("picture of", "pic", "images of"), a trailing "please", and
    surrounding punctuation. The loop matters because the pieces can
    stack — "a picture of will smith..." needs article, then media
    word, in that order — and the old code stripped them in the wrong
    order, leaving "picture of will smith eating spaghetti" as the
    search query.
    """
    q = (query or "").strip()
    for _ in range(3):
        before = q
        q = _drop_emoji(q)
        q = q.strip(" \t\r\n,.;:!?\"'()[]")
        low = q.lower()
        if low.startswith("please "):
            q = q[len("please "):].strip()
            low = q.lower()
        if low.startswith("for me "):
            q = q[len("for me "):].strip()
            low = q.lower()
        if low.startswith("surprise me"):
            q = q[len("surprise me"):].strip()
            low = q.lower()
            if low.startswith("with "):
                q = q[len("with "):].strip()
                low = q.lower()
        for art in _LEADING_ARTICLES:
            if low.startswith(art):
                q = q[len(art):].strip()
                low = q.lower()
                break
        # Adjectives directly in front of a media word carry no topic
        # ("a beautiful picture of a sunset" -> "picture of a sunset").
        # Only stripped when a media word follows — "a cool dog" keeps
        # "cool", because there the adjective IS part of the topic.
        m = re.match(
            r"^(?:" + _ADJECTIVE_RE + r"\s+)+(?=" + _MEDIA_WORDS + r"\b)",
            q,
            re.IGNORECASE,
        )
        if m:
            q = q[m.end():].strip()
            low = q.lower()
        m = re.match(
            r"^(?:pictures?|images?|photos?|pics?)\s+(?:of\s+)?(.*)$",
            q,
            re.IGNORECASE,
        )
        if m:
            q = m.group(1).strip()
            low = q.lower()
        if low.endswith(" please"):
            q = q[: -len(" please")].strip()
        q = q.strip(" \t\r\n,.;:!?\"'()[]")
        if q == before:
            break
    return q


def _match_image_patterns(s: str, allow_anything: bool) -> Optional[str]:
    """Match the image-request patterns against `s` (filler already
    stripped). Returns "" when it matched an image request but no
    topic is left ("send a random image" -> surprise-me), and None
    when it is not an image request at all ("send me a message").

    Patterns are tried anchored first, then with re.search so a
    request buried mid-sentence ("btw send me a picture of cats")
    still matches. The bare "<media> of <topic>" pattern is only
    tried anchored — searching for it mid-sentence would misfire on
    plain chat like "I saw a picture of cats yesterday".
    """
    s = _strip_leading_filler(s)
    if not s:
        return None

    # Verb + [me] + [article] + [adjectives] + media word + [tail].
    # The tail (topic) is optional: "send a random image" has none,
    # so it returns "" (surprise-me) instead of None. The tail allows
    # a stray comma ("send me an image, any image, surprise me").
    media_body = (
        _VERBS + r"\s+(?:me\s+)?(?:some\s+|a\s+|an\s+|the\s+)?"
        + r"(?:" + _ADJECTIVE_RE + r"\s+)*" + _MEDIA_WORDS
        + r"(?:\s*,?\s*(?:of\s+)?(.+))?$"
    )
    # Verb + "me" + anything (broad only): "send me a dog with a
    # red ball". Guarded by _NON_IMAGE_WORDS ("send me a message").
    anything_body = _VERBS + r"\s+me\s+(.+)$"
    # Bare "<media> of <topic>" ("picture of cats") — anchored only.
    bare_body = (
        r"(?:a\s+|an\s+|the\s+)?(?:"
        + _ADJECTIVE_RE + r"\s+)*" + _MEDIA_WORDS + r"\s+of\s+(.+)$"
    )

    for body, group, is_anything, allow_search in (
        (media_body, 1, False, True),
        (anything_body, 1, True, True) if allow_anything else (None, 0, False, False),
        (bare_body, 1, False, False),
    ):
        if body is None:
            continue
        m = re.match(r"^" + body, s, re.IGNORECASE)
        if m is None and allow_search:
            # Buried mid-sentence: "btw send me a picture of cats".
            m = re.search(r"\b" + body, s, re.IGNORECASE)
        if m is None:
            continue
        raw = m.group(group)
        if raw is None:
            return ""
        q = _clean_query(raw)
        if is_anything and q.lower() in _NON_IMAGE_WORDS:
            return None
        return q
    return None


def _parse_broad(text: str) -> Optional[str]:
    """Wake-word / mention context: "send me <anything>" is an image ask.

    Used when the message already had a trigger (redster wake, @mention,
    reply-to-bot, /ask). Returns "" when it matched an image request
    but no topic is left ("send me an image" -> surprise-me), and
    None when it is not an image request at all ("send me a message").
    """
    return _match_image_patterns(text, allow_anything=True)


def _parse_narrow(text: str) -> Optional[str]:
    """No wake word: only an explicit picture request counts, so plain
    chat ("send me a message", "tell me about dogs") never misfires.
    Same patterns as _parse_broad minus the bare "send me <anything>"."""
    return _match_image_patterns(text, allow_anything=False)


def parse_image_request(text: str, parsed_redster=None, broad: bool = False) -> Optional[str]:
    """Extract an image query from chat text, or None.

    - "/pic <query>" -> query ("/pic" with no query -> None).
    - parsed_redster with intent "quote" -> None (quote wins).
    - parsed_redster with intent "chat" -> broad parse of its chat_text.
    - Otherwise: strip a leading bot @mention / redster wake word if
      present, then broad parse; plain text without any trigger gets
      the narrow parse (picture words required).
    - broad=True (used when the caller already knows a trigger fired,
      e.g. a redster wake word, @mention, reply-to-bot or /ask):
      "send me <anything>" counts even without picture words.
    - A request with no real topic ("send me an image, any image,
      surprise me") returns a random SURPRISE_TOPICS topic, never the
      literal filler phrase.
    """
    if not text or not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None

    # Explicit command: /pic <query>
    if s.lower().startswith("/pic"):
        rest = s[4:].strip()
        if not rest:
            return None
        return _resolve_query(_clean_query(rest))

    # Leading filler ("Now redster send me ..." / "please send me ...")
    # is stripped before the wake-word / mention checks below, so a
    # request buried behind "now" / "can you" still routes correctly.
    s = _strip_leading_filler(s)
    if not s:
        return None

    # Redster-parsed input (bot.py passes its parse result through).
    if parsed_redster is not None:
        if parsed_redster.get("intent") == "quote":
            return None
        chat_text = (parsed_redster.get("chat_text") or "").strip()
        if not chat_text:
            return None
        return _resolve_query(_parse_broad(chat_text))

    # Leading @mention counts as a trigger ("@R3dSter_Bot send me ...").
    m = re.match(r"^@\S+\s+(.+)$", s)
    if m:
        return _resolve_query(_parse_broad(m.group(1)))

    # Leading redster wake word counts as a trigger.
    try:
        import redster as _redster

        parsed = _redster.parse_redster_message(s)
        if parsed is not None:
            if parsed.get("intent") == "quote":
                return None
            return _resolve_query(_parse_broad(parsed.get("chat_text") or ""))
    except Exception:
        pass

    if broad:
        return _resolve_query(_parse_broad(s))
    return _resolve_query(_parse_narrow(s))


def _resolve_query(query: Optional[str]) -> Optional[str]:
    """Turn a parser result into the final query: None stays None (not
    an image request), "" or a filler-only phrase becomes a random
    surprise topic, anything else is the search query."""
    if query is None:
        return None
    if not query or _is_surprise_request(query):
        return surprise_topic()
    return query


def attribution_caption(item: Dict[str, str]) -> str:
    """One-line credit for an image, for the media-group caption."""
    title = (item.get("title") or "Untitled").strip()
    creator = (item.get("creator") or "Unknown").strip()
    lic = (item.get("license") or "").strip()
    line = f"📷 {title} — {creator}"
    if lic:
        line += f" ({lic})"
    return line
