"""
Full-song send for interactive chat (search YouTube, send the MP3)
───────────────────────────────────────────────────────────────────
"redster play pound the alarm by nicki minaj" -> search YouTube
for the song -> download audio-only with the existing downloader
(downloaders.execute_youtube_download, quality="audio": pytubefix
fast audio engine, yt-dlp FFmpegExtractAudio fallback, Telethon
>49MB fallback) -> send as a Telegram audio message.

This module only parses the request and resolves it to a YouTube
result. It does NOT duplicate any download/upload code — bot.py's
_handle_music calls the existing downloader with the URL found here.

Limits: 1 song per request, top result only, max 10 minutes
(MAX_SONG_SECONDS=600, enforced at the search stage so a long video
is never downloaded), results cached 10 minutes. No new dependency:
yt-dlp is already in requirements.txt (imported lazily, only when a
real search runs, so this module stays importable without it) and
httpx is already used by web_search.py (imported lazily here too).

Search order (2026-11-25): when YOUTUBE_API_KEY is set, the
YouTube Data API v3 is tried FIRST — yt-dlp ytsearch always goes
out as the web client, which YouTube challenges from datacenter
IPs, so no player-client setting can fix it; the official API is
immune to that wall. When the key is absent, or the API fails or
finds nothing, search falls back to yt-dlp ytsearch (the caller's
web fallback is the last resort). Never raises for user input —
search failures return None or {"error": "search_unavailable"}.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import unicodedata
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

# ─── Tunables ────────────────────────────────────────────────────────────────
MAX_SONG_SECONDS = 600  # 10 minutes — longer results are rejected at search
SEARCH_TIMEOUT_SECONDS = 30  # yt-dlp search cap — a hung search must
# never leave the user stuck at "Looking for…" (2026-11-24 live bug:
# the search ran in an executor with NO timeout, so a blocked/slow
# YouTube search never returned and no error was ever shown).
CACHE_TTL_SECONDS = 600  # 10 minutes
CACHE_MAX_ENTRIES = 100
DEFAULT_SEARCH_PREFIX = "ytsearch1:"

# YouTube Data API v3 (official search, tried first when a key is
# configured). search.list costs 100 quota units, videos.list 1 —
# 101 per song search out of the free 10,000/day quota (~100
# searches/day). 10s per call, 25s for the whole API attempt, so a
# hung API can never stall the user longer than the yt-dlp search
# would have on its own.
YOUTUBE_DATA_API_BASE = "https://www.googleapis.com/youtube/v3"
YOUTUBE_API_TIMEOUT_SECONDS = 10
YOUTUBE_API_MAX_TOTAL_SECONDS = 25

# Music is claimed ONLY by "play" (broad mode, i.e. after a trigger)
# or the /song command (Red, 2026-11-26: "for music in general let
# it just be redster play instead of redster send as well"). There
# are NO send-based claims left in this module: "redster send
# <title>" and "redster send me <title>" do not start a song search.
# "play" is broad-only (see _parse_broad): in plain chat "play
# football" must never misfire.
_MEDIA_WORD_RE = re.compile(
    r"\b(?:pictures?|images?|photos?|pics?)\b", re.IGNORECASE
)
_LEADING_ARTICLES = ("a ", "an ", "the ", "some ")

# Leading filler words — "please play ..." must parse the same as
# "play ...". Stripped (repeatedly) before the play pattern is tried,
# mirroring the media parser.
_LEADING_FILLERS = (
    "please ", "now ", "hey ", "ok ", "okay ", "and ", "then ", "so ",
    "btw ", "just ", "can you ", "could you ", "would you ",
    "will you ", "also ",
)


def _strip_leading_filler(text: str) -> str:
    """Strip leading filler words/phrases ("please", "now", "can you")
    so "please play hello" parses like "play hello". Loops until
    stable (fillers can stack: "now please play")."""
    s = (text or "").strip()
    changed = True
    while changed:
        changed = False
        low = s.lower()
        for filler in _LEADING_FILLERS:
            if low.startswith(filler):
                s = s[len(filler):].strip()
                changed = True
                break
    return s


def _drop_emoji(text: str) -> str:
    """Drop emoji/symbols, keep letters in any script.

    Same rule as media_search._drop_emoji: emoji live above U+2500;
    letters (Latin, Amharic, Cyrillic, CJK, ...) are kept via their
    Unicode category.
    """
    out = []
    for ch in text:
        if ord(ch) <= 0x2500 or unicodedata.category(ch).startswith("L"):
            out.append(ch)
    return "".join(out)


def _clean_query(query: str) -> str:
    """Normalise a captured song query.

    Loops (max 3 passes) until stable: drops emoji, strips leading
    articles ("a", "the"), a leading "please" / "for me", a leading
    "the song" / "song" / "music" (+ optional "of"), a trailing
    "please", and surrounding punctuation. The "by <artist>" part is
    KEPT — it helps the YouTube search — and artist names are kept
    exactly as typed (no spelling correction: "nikki minaj" stays
    "nikki minaj").
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
        for art in _LEADING_ARTICLES:
            if low.startswith(art):
                q = q[len(art):].strip()
                low = q.lower()
                break
        m = re.match(
            r"^(?:songs?|music|mp3|audio)\s+(?:of\s+)?(.*)$",
            q,
            re.IGNORECASE,
        )
        if m:
            q = m.group(1).strip()
            low = q.lower()
        # Trailing music word: "radioactive song" / "blinding lights
        # mp3" -> the word is the request type, not part of the title.
        # Only when another word precedes it, so a bare "song" query
        # is left alone.
        m2 = re.match(r"^(.+?)\s+(?:songs?|music|mp3|audio|tracks?|tunes?)$", q, re.IGNORECASE)
        if m2:
            q = m2.group(1).strip()
            low = q.lower()
        if low.endswith(" please"):
            q = q[: -len(" please")].strip()
        q = q.strip(" \t\r\n,.;:!?\"'()[]")
        if q == before:
            break
    return q


def _has_media_word(text: str) -> bool:
    """True when the text asks for a picture — music must never steal
    an image request ("send an image of a dog by the river" contains
    "by" but is a picture, not a song)."""
    return bool(_MEDIA_WORD_RE.search(text or ""))


def _parse_broad(text: str) -> Optional[str]:
    """Wake-word / mention context: only "play <title>" counts as a
    music request (Red, 2026-11-26 — music is "redster play", never
    "redster send"). Returns the cleaned song query, or None.

    A media word anywhere still vetoes the claim, so "play pictures
    of cats" is an image request, not a song. "play" itself is kept
    exactly as broad as before — "play football" still claims in
    broad mode; narrowing "play" was explicitly out of scope.
    """
    s = _strip_leading_filler(text.strip())
    if _has_media_word(s):
        return None
    # "play <query>" — the only music trigger besides /song.
    # "play blinding lights" -> the song.
    m = re.match(r"^play\s+(.+)$", s, re.IGNORECASE)
    if m:
        return _clean_query(m.group(1))
    return None


def is_bare_play_request(text: str) -> bool:
    """True when the (trigger-stripped) text is just "play" with no
    title — "redster play" with nothing to play. The caller answers
    with usage instead of searching."""
    s = _strip_leading_filler((text or "").strip())
    return s.lower() == "play"


def _parse_narrow(text: str) -> Optional[str]:
    """No wake word: nothing counts as music (Red, 2026-11-26).

    Music requires a trigger — "redster play <title>" or /song.
    Plain text never starts a song search, not even "send me the
    song hello" or "play football": "play" is NOT a trigger here,
    only in broad mode. Always returns None; kept so callers keep
    one parse entry point."""
    return None


def parse_music_request(text: str, parsed_redster=None, broad: bool = False) -> Optional[str]:
    """Extract a song query from chat text, or None.

    - "/song <query>" -> query ("/song" with no query -> None).
    - parsed_redster with intent "quote" -> None (quote wins).
    - parsed_redster with intent "chat" -> broad parse of its chat_text.
    - Otherwise: strip a leading bot @mention / redster wake word if
      present, then broad parse; plain text without any trigger gets
      the narrow parse (which never claims — music needs a trigger).
    - broad=True (used when the caller already knows a trigger fired,
      e.g. a redster wake word, @mention, reply-to-bot or /ask).

    Music is claimed ONLY by /song and "play <title>" (Red,
    2026-11-26). "redster send <title>" / "redster send me <title>"
    never claim: they fall through to image/chat as if the music
    parser never saw them — so "send me a dog by the river" stays
    an image request and "send me a message" stays chat.
    """
    if not text or not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None

    # Explicit command: /song <query>
    if s.lower().startswith("/song"):
        rest = s[5:].strip()
        return _clean_query(rest) or None

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


# ─── Cache ───────────────────────────────────────────────────────────────────
_cache: Dict[str, tuple] = {}
_cache_lock = threading.Lock()


def _cache_get(query: str) -> Optional[Dict[str, object]]:
    key = " ".join((query or "").lower().split())
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        ts, result = entry
        if time.time() - ts > CACHE_TTL_SECONDS:
            _cache.pop(key, None)
            return None
        return dict(result)


def _cache_put(query: str, result: Dict[str, object]) -> None:
    key = " ".join((query or "").lower().split())
    with _cache_lock:
        if len(_cache) >= CACHE_MAX_ENTRIES:
            _cache.pop(next(iter(_cache)), None)
        _cache[key] = (time.time(), dict(result))


def clear_music_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ─── YouTube search (yt-dlp, top result only) ────────────────────────────────
def _youtube_search_opts() -> dict:
    """yt-dlp opts for song search AND direct-URL probing.

    The search call used to run with bare yt-dlp defaults — no player
    client, no cookies, no headers. The download path
    (downloaders._make_audio_opts) already uses the Android/iOS player
    clients, because the default web client is the one YouTube
    challenges most from datacenter IPs, and the PO-token provider
    installed in the Dockerfile is disabled process-wide
    (downloaders.py stubs yt_dlp.plugins.load_plugins out: bgutil
    spawns deno/node and stalls/OOMs the 0.1 vCPU / 256 MB box), so
    search cannot rely on it either. Match the downloader's first
    strategy here (Android/iOS clients + YouTube cookies when set)
    so search is no longer the weakest link.
    """
    opts: dict = {
        "quiet": True,
        "noplaylist": True,
        "skip_download": True,
        "extract_flat": False,
        "no_warnings": True,
        "http_headers": {"Accept-Language": "en-US,en;q=0.9"},
        "socket_timeout": 10,
        "retries": 1,
        "extractor_retries": 0,
        "extractor_args": {"youtube": {"player_client": ["android", "ios"]}},
    }
    try:
        from config import YOUTUBE_COOKIES_FILE

        if YOUTUBE_COOKIES_FILE and os.path.exists(YOUTUBE_COOKIES_FILE):
            opts["cookiefile"] = YOUTUBE_COOKIES_FILE
    except Exception:
        pass
    return opts


def parse_search_result(info: Optional[dict]) -> Optional[Dict[str, object]]:
    """Parse a yt-dlp search result (extract_info of ytsearch1:...)
    into {video_id, title, artist, duration_seconds, url}.

    Returns {"error": "too_long", "title", "duration_seconds"} when
    the top result is longer than MAX_SONG_SECONDS, and None when
    there is no usable result. Pure/testable.
    """
    if not info or not isinstance(info, dict):
        return None
    entries = info.get("entries")
    if not entries:
        # A direct video info dict (no search wrapper) is also accepted.
        entries = [info] if info.get("id") else []
    if not entries:
        return None
    top = entries[0]
    if not isinstance(top, dict):
        return None
    video_id = str(top.get("id") or "").strip()
    if not video_id:
        return None
    duration = top.get("duration") or 0
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        duration = 0
    title = str(top.get("title") or "Unknown").strip()
    if duration and duration > MAX_SONG_SECONDS:
        return {
            "error": "too_long",
            "title": title,
            "duration_seconds": duration,
        }
    url = str(top.get("webpage_url") or "").strip()
    if not url:
        url = f"https://www.youtube.com/watch?v={video_id}"
    return {
        "video_id": video_id,
        "title": title,
        "artist": str(top.get("uploader") or top.get("channel") or "").strip(),
        "duration_seconds": duration,
        "url": url,
    }


def _default_search(query: str):
    """Run yt-dlp ytsearch1 for `query`, return the raw info dict.

    yt-dlp is imported lazily so this module stays importable without
    it. Runs in an executor (blocking call) — the caller is async.
    Returns None ONLY when the search ran and found nothing.
    Returns {"error": "search_unavailable"} when yt-dlp is missing
    or raised. Previously every failure collapsed into None, which
    made the caller's "YouTube search isn't answering" message
    unreachable (found 2026-11-25: a raised search and an empty
    search looked identical). Never raises.
    """
    try:
        import yt_dlp
    except ImportError:
        return {"error": "search_unavailable"}
    try:
        with yt_dlp.YoutubeDL(_youtube_search_opts()) as ydl:
            return ydl.extract_info(DEFAULT_SEARCH_PREFIX + query, download=False)
    except Exception:
        return {"error": "search_unavailable"}


def _get_youtube_api_key() -> str:
    """Read the YouTube Data API v3 key from config (preferred,
    mirroring how YOUTUBE_COOKIES_FILE is read) or the environment.
    Returns "" when unset. The value is never logged."""
    try:
        from config import YOUTUBE_API_KEY

        if YOUTUBE_API_KEY:
            return str(YOUTUBE_API_KEY).strip()
    except Exception:
        pass
    return (os.getenv("YOUTUBE_API_KEY") or "").strip()


_ISO8601_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)


def _parse_iso8601_duration(value) -> Optional[int]:
    """Parse an ISO 8601 duration (YouTube contentDetails.duration,
    e.g. PT3M43S, PT1H2M3S) into seconds, or None when unparsable
    or carrying no time at all ("PT0S" counts as unknown — the
    downloader's own caps are the backstop then). Pure/testable."""
    if not value or not isinstance(value, str):
        return None
    m = _ISO8601_DURATION_RE.match(value.strip())
    if not m:
        return None
    try:
        total = (
            int(m.group("days") or 0) * 86400
            + int(m.group("hours") or 0) * 3600
            + int(m.group("minutes") or 0) * 60
            + float(m.group("seconds") or 0)
        )
    except (TypeError, ValueError):
        return None
    return int(total) if total > 0 else None


async def _default_api_get(url: str, params: dict, timeout: float):
    """Default Data API fetcher: one httpx AsyncClient per call (low
    volume), mirroring web_search._default_get. `params` go in the
    query string, which is how the YouTube Data API takes its key."""
    import httpx

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        return await client.get(url, params=params)


async def _youtube_api_duration(video_id: str, get, key: str) -> Optional[int]:
    """videos.list contentDetails for one video id (1 quota unit).

    Returns the duration in seconds, or None when the call fails —
    the caller then returns the result without a duration and lets
    the downloader enforce its caps (same posture as
    probe_video_duration). Never raises. Never logs the key."""
    try:
        resp = await get(
            f"{YOUTUBE_DATA_API_BASE}/videos",
            {"part": "contentDetails", "id": video_id, "key": key},
            YOUTUBE_API_TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.debug("YouTube Data API videos.list failed: %s", type(e).__name__)
        return None
    if getattr(resp, "status_code", 200) != 200:
        logger.debug(
            "YouTube Data API videos.list returned HTTP %s",
            getattr(resp, "status_code", "?"),
        )
        return None
    try:
        payload = resp.json() if hasattr(resp, "json") else json.loads(resp.text)
    except Exception:
        return None
    try:
        items = (payload or {}).get("items") or []
        if not items or not isinstance(items[0], dict):
            return None
        raw = ((items[0].get("contentDetails") or {}).get("duration") or "")
        return _parse_iso8601_duration(raw)
    except Exception:
        return None


async def _search_youtube_api(
    query: str,
    _get: Optional[Callable] = None,
    _api_key: Optional[str] = None,
) -> Optional[Dict[str, object]]:
    """Search YouTube via the Data API v3 (official search endpoint).

    Returns the top result {video_id, title, artist,
    duration_seconds, url}, or {"error": "too_long", ...} when the
    top video exceeds MAX_SONG_SECONDS. Returns None when the API
    found nothing (zero items) OR when the API call itself failed
    (non-200, timeout, malformed body) — the caller falls through
    to yt-dlp in both cases, silently, so no API-specific error
    ever reaches the user. The key value is never logged. When the
    key is absent (no _api_key and none configured) the API is not
    called at all. Never raises.
    """
    key = _api_key if _api_key is not None else _get_youtube_api_key()
    if not key:
        return None
    get = _get or _default_api_get
    try:
        resp = await get(
            f"{YOUTUBE_DATA_API_BASE}/search",
            {
                "part": "snippet",
                "q": query,
                "type": "video",
                "maxResults": 1,
                "key": key,
            },
            YOUTUBE_API_TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.debug("YouTube Data API search request failed: %s", type(e).__name__)
        return None
    if getattr(resp, "status_code", 200) != 200:
        logger.debug(
            "YouTube Data API search returned HTTP %s; falling back to yt-dlp",
            getattr(resp, "status_code", "?"),
        )
        return None
    try:
        payload = resp.json() if hasattr(resp, "json") else json.loads(resp.text)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    items = payload.get("items") or []
    if not items or not isinstance(items[0], dict):
        return None
    top = items[0]
    video_id = ""
    if isinstance(top.get("id"), dict):
        video_id = str(top["id"].get("videoId") or "").strip()
    if not _VIDEO_ID_RE.match(video_id):
        return None
    snippet = top.get("snippet") or {}
    title = str(snippet.get("title") or "Unknown").strip() or "Unknown"
    artist = str(snippet.get("channelTitle") or "").strip()
    url = f"https://www.youtube.com/watch?v={video_id}"
    duration = await _youtube_api_duration(video_id, get=get, key=key)
    if duration and duration > MAX_SONG_SECONDS:
        return {
            "error": "too_long",
            "title": title,
            "duration_seconds": duration,
        }
    return {
        "video_id": video_id,
        "title": title,
        "artist": artist,
        "duration_seconds": duration or 0,
        "url": url,
    }


async def search_song(
    query: str,
    _search: Optional[Callable[[str], Optional[dict]]] = None,
    _api_get: Optional[Callable] = None,
    _api_key: Optional[str] = None,
) -> Optional[Dict[str, object]]:
    """Search YouTube for `query`, return the top result or None.

    Search order: (1) YouTube Data API v3 when YOUTUBE_API_KEY is
    set (or _api_key is passed) — the official endpoint, immune to
    the datacenter-IP wall that blocks yt-dlp ytsearch; (2) yt-dlp
    ytsearch1; the caller tries the web fallback after this. Any
    Data API failure or zero items falls through to yt-dlp silently.

    Result: {video_id, title, artist, duration_seconds, url}, or
    {"error": "too_long", "title", "duration_seconds"} when the top
    result exceeds MAX_SONG_SECONDS (10 min) — the caller surfaces
    that instead of downloading. A failed yt-dlp search (yt-dlp
    raised, yt-dlp missing, or the 30s timeout hit) returns
    {"error": "search_unavailable"} instead of None, so the caller
    can tell "YouTube search isn't answering" apart from "no such
    song": None now means only "the search ran and found nothing"
    (previously _default_search swallowed every yt-dlp exception
    into None, so search_unavailable was only reachable via the
    timeout — fixed 2026-11-25). Results are cached 10 minutes
    (failures are not cached). `_search` is an injectable raw-info
    fetcher for tests; `_api_get` is an injectable Data API fetcher
    (url, params, timeout) -> response; `_api_key` overrides the
    configured key (None = read it, "" = no key). Never raises.
    """
    if not query or not isinstance(query, str) or not query.strip():
        return None
    query = query.strip()
    cached = _cache_get(query)
    if cached is not None:
        return cached

    # 1. YouTube Data API v3 first, when a key is configured. Any
    # failure or zero items falls through to yt-dlp below.
    api_key = _api_key if _api_key is not None else _get_youtube_api_key()
    if api_key:
        try:
            api_result = await asyncio.wait_for(
                _search_youtube_api(query, _get=_api_get, _api_key=api_key),
                timeout=YOUTUBE_API_MAX_TOTAL_SECONDS,
            )
        except Exception:
            api_result = None
        if api_result is not None:
            _cache_put(query, api_result)
            return api_result

    # 2. yt-dlp ytsearch1 (top result only).
    search = _search or _default_search
    try:
        loop = asyncio.get_running_loop()
        # A hung yt-dlp search (blocked/slow YouTube from the host's
        # IP) must time out instead of leaving the user stuck at
        # "Looking for…" forever — wait_for raises TimeoutError, which
        # is caught below and reported as search_unavailable.
        # (The executor thread itself keeps running; only the wait ends.)
        info = await asyncio.wait_for(
            loop.run_in_executor(None, search, query),
            timeout=SEARCH_TIMEOUT_SECONDS,
        )
    except Exception:
        return {"error": "search_unavailable"}
    # _default_search returns the search_unavailable sentinel (not
    # None) when yt-dlp raised — a failed search must stay
    # distinguishable from "ran, found nothing".
    if isinstance(info, dict) and info.get("error") == "search_unavailable":
        return {"error": "search_unavailable"}
    try:
        result = parse_search_result(info)
    except Exception:
        return {"error": "search_unavailable"}
    if result is None:
        return None
    _cache_put(query, result)
    return result


# ─── Web fallback: resolve the song through a web search ─────────────────────
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
}
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _extract_youtube_video_id(url: str) -> Optional[str]:
    """Return the 11-char video id of a youtube.com/watch or youtu.be
    URL, or None. Only YouTube links are ever accepted — a web search
    result pointing anywhere else (Spotify, lyrics sites, ...) is
    rejected, never downloaded."""
    if not url or not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return None
    host = (parsed.netloc or "").lower().split(":")[0]
    if host in _YOUTUBE_HOSTS:
        vid = (parse_qs(parsed.query).get("v") or [None])[0]
        return vid if vid and _VIDEO_ID_RE.match(vid) else None
    if host in ("youtu.be", "www.youtu.be"):
        vid = parsed.path.strip("/").split("/")[0]
        return vid if _VIDEO_ID_RE.match(vid) else None
    return None


async def find_song_via_web(
    query: str,
    _search_web: Optional[Callable] = None,
) -> Optional[str]:
    """Second chance when yt-dlp search fails: web-search for the
    song's YouTube video and return its watch URL, or None.

    Uses web_search.search_web (DuckDuckGo HTML, Wikipedia fallback)
    with a `site:youtube.com` query and takes the first result that
    is a real YouTube watch/shorts link. Never raises. Returns None
    when the web search itself fails — the caller then reports the
    search as unavailable instead of pretending the song doesn't
    exist.
    """
    if not query or not isinstance(query, str) or not query.strip():
        return None
    try:
        from web_search import search_web
    except ImportError:
        return None
    search = _search_web or search_web
    try:
        results = await search(f"site:youtube.com {query.strip()}", max_results=5)
    except Exception:
        return None
    for item in results or []:
        if not isinstance(item, dict):
            continue
        vid = _extract_youtube_video_id(str(item.get("url") or ""))
        if vid:
            return f"https://www.youtube.com/watch?v={vid}"
    return None


async def probe_video_duration(
    url: str,
    _extract: Optional[Callable[[str], Optional[dict]]] = None,
) -> Optional[int]:
    """Return the duration in seconds of a direct YouTube URL, or None
    when it cannot be determined (probe failed/blocked — the caller
    then lets the downloader try anyway, since it enforces its own
    30-minute cap and size limits as a backstop).

    Used on the web-fallback path so the 10-minute song cap still
    applies there: the yt-dlp search stage normally rejects long
    videos before any download, and a web result has no duration
    attached. `_extract` is an injectable info fetcher for tests.
    Never raises.
    """
    if not url or not isinstance(url, str) or not url.strip():
        return None

    def _default_extract(u: str) -> Optional[dict]:
        try:
            import yt_dlp
        except ImportError:
            return None
        try:
            with yt_dlp.YoutubeDL(_youtube_search_opts()) as ydl:
                return ydl.extract_info(u, download=False)
        except Exception:
            return None

    extract = _extract or _default_extract
    try:
        loop = asyncio.get_running_loop()
        info = await asyncio.wait_for(
            loop.run_in_executor(None, extract, url.strip()),
            timeout=15,
        )
    except Exception:
        return None
    if not info or not isinstance(info, dict):
        return None
    try:
        duration = int(info.get("duration") or 0)
    except (TypeError, ValueError):
        return None
    return duration or None
