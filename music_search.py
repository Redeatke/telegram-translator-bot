"""
Full-song send for interactive chat (search YouTube, send the MP3)
───────────────────────────────────────────────────────────────────
"redster send me pound the alarm by nicki minaj" -> search YouTube
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
real search runs, so this module stays importable without it).
Never raises for user input — search failures return None.
"""
from __future__ import annotations

import asyncio
import os
import re
import threading
import time
import unicodedata
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

# ─── Tunables ────────────────────────────────────────────────────────────────
MAX_SONG_SECONDS = 600  # 10 minutes — longer results are rejected at search
SEARCH_TIMEOUT_SECONDS = 30  # yt-dlp search cap — a hung search must
# never leave the user stuck at "Looking for…" (2026-11-24 live bug:
# the search ran in an executor with NO timeout, so a blocked/slow
# YouTube search never returned and no error was ever shown).
CACHE_TTL_SECONDS = 600  # 10 minutes
CACHE_MAX_ENTRIES = 100
DEFAULT_SEARCH_PREFIX = "ytsearch1:"

# Whole words that mark a request as music. "play" is broad-only (see
# _parse_broad): in plain chat "play football" must never misfire.
_MUSIC_WORDS = r"(?:songs?|music|mp3|audio|tracks?|tunes?)"
_MUSIC_WORD_RE = re.compile(
    r"\b(?:songs?|music|mp3|audio|tracks?|tunes?|play)\b", re.IGNORECASE
)
_MEDIA_WORD_RE = re.compile(
    r"\b(?:pictures?|images?|photos?|pics?)\b", re.IGNORECASE
)
_VERBS = r"(?:send|show|find|give|get)"
_LEADING_ARTICLES = ("a ", "an ", "the ", "some ")

# Leading filler words — "please send me the song ..." and "now send
# me ..." must parse the same as "send me ...". Stripped (repeatedly)
# before the verb patterns are tried, mirroring the media parser.
_LEADING_FILLERS = (
    "please ", "now ", "hey ", "ok ", "okay ", "and ", "then ", "so ",
    "btw ", "just ", "can you ", "could you ", "would you ",
    "will you ", "also ",
)


def _strip_leading_filler(text: str) -> str:
    """Strip leading filler words/phrases ("please", "now", "can you")
    so "please send me the song hello" parses like "send me the song
    hello". Loops until stable (fillers can stack: "now please send")."""
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


def _has_music_word(text: str) -> bool:
    return bool(_MUSIC_WORD_RE.search(text or ""))


def _has_media_word(text: str) -> bool:
    """True when the text asks for a picture — music must never steal
    an image request ("send me a picture of a dog by the river"
    contains "by" but is a picture, not a song)."""
    return bool(_MEDIA_WORD_RE.search(text or ""))


def _parse_broad(text: str) -> Optional[str]:
    """Wake-word / mention context: music words (or a "by <artist>"
    construction) mark a music request.

    Used when the message already had a trigger (redster wake,
    @mention, reply-to-bot, /ask, /song). Returns None when it is not
    a music request at all ("send me a dog with a red ball").
    """
    s = _strip_leading_filler(text.strip())
    if _has_media_word(s):
        return None
    # Verb + "me" + "the song/music" + topic: "send me the song
    # pound the alarm by nicki minaj", "send me the music hello".
    m = re.match(
        r"^" + _VERBS + r"\s+me\s+(?:the\s+)?" + _MUSIC_WORDS + r"\s+(?:of\s+)?(.+)$",
        s,
        re.IGNORECASE,
    )
    if m:
        return _clean_query(m.group(1))
    # Verb + "me" + "<song> by <artist>": the "by" construction is
    # itself the music signal, so no music word is needed here.
    # "send me pound the alarm by nikki minaj" -> the whole phrase.
    m = re.match(
        r"^" + _VERBS + r"\s+me\s+(.+?)\s+by\s+(.+)$",
        s,
        re.IGNORECASE,
    )
    if m:
        return _clean_query(m.group(1) + " by " + m.group(2))
    # Verb + "me" + "<query> song/music/mp3": "send me blinding
    # lights song", "send me shape of you mp3".
    m = re.match(
        r"^" + _VERBS + r"\s+me\s+(.+?)\s+" + _MUSIC_WORDS + r"\s*$",
        s,
        re.IGNORECASE,
    )
    if m:
        return _clean_query(m.group(1))
    # "play <query>" — broad only. "play blinding lights" -> the song.
    m = re.match(r"^play\s+(.+)$", s, re.IGNORECASE)
    if m:
        return _clean_query(m.group(1))
    # Bare "send <query>" — broad only: "redster send blinding lights".
    # No "me" (the "send me <x>" rule above stays stricter: music only
    # with a music word or "by <artist>", so "send me a dog with a
    # red ball" is still an image) and no media word (guarded at the
    # top). TRADEOFF: this claims any bare "send <x>" for music before
    # images can see it, so a failed music search answers "couldn't
    # find as a song" instead of sending a picture.
    m = re.match(r"^send\s+(?!me\s)(.+)$", s, re.IGNORECASE)
    if m:
        return _clean_query(m.group(1))
    # Verb + "me" + anything, but ONLY when a music word is present
    # somewhere ("send me that new drake song" is caught above; this
    # covers "send me the music <query>" leftovers and "send me
    # <query>" with a music word inside). Without a music word this
    # must stay None — "send me a dog with a red ball" is an image.
    m = re.match(r"^" + _VERBS + r"\s+me\s+(.+)$", s, re.IGNORECASE)
    if m and _has_music_word(m.group(1)):
        return _clean_query(m.group(1))
    return None


def _parse_narrow(text: str) -> Optional[str]:
    """No wake word: only an explicit song request counts, so plain
    chat ("send me a message", "play football") never misfires.
    "play" is NOT a trigger here — only in broad mode."""
    s = _strip_leading_filler(text.strip())
    if _has_media_word(s):
        return None
    m = re.match(
        r"^" + _VERBS + r"\s+(?:me\s+)?(?:the\s+)?" + _MUSIC_WORDS + r"\s+(?:of\s+)?(.+)$",
        s,
        re.IGNORECASE,
    )
    if m:
        return _clean_query(m.group(1))
    m = re.match(
        r"^" + _VERBS + r"\s+(?:me\s+)?(.+?)\s+" + _MUSIC_WORDS + r"\s*$",
        s,
        re.IGNORECASE,
    )
    if m:
        return _clean_query(m.group(1))
    # "<song> by <artist>" with a send verb but no music word is NOT
    # enough in narrow mode — it needs a trigger (redster/@mention/
    # reply) to count as music. "send me pound the alarm by nicki
    # minaj" with no wake word stays chat.
    return None


def parse_music_request(text: str, parsed_redster=None, broad: bool = False) -> Optional[str]:
    """Extract a song query from chat text, or None.

    - "/song <query>" -> query ("/song" with no query -> None).
    - parsed_redster with intent "quote" -> None (quote wins).
    - parsed_redster with intent "chat" -> broad parse of its chat_text.
    - Otherwise: strip a leading bot @mention / redster wake word if
      present, then broad parse; plain text without any trigger gets
      the narrow parse (music words required).
    - broad=True (used when the caller already knows a trigger fired,
      e.g. a redster wake word, @mention, reply-to-bot or /ask).
    - A music request is only recognised when the text contains a
      music word ("song", "music", "mp3", "audio", "track", "play"),
      a "<song> by <artist>" construction, or came via /song — so
      "send me a dog with a red ball" stays an image request and
      "send me a message" stays chat.
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


def _default_search(query: str) -> Optional[dict]:
    """Run yt-dlp ytsearch1 for `query`, return the raw info dict.

    yt-dlp is imported lazily so this module stays importable without
    it. Runs in an executor (blocking call) — the caller is async.
    Returns None on any failure. Never raises.
    """
    try:
        import yt_dlp
    except ImportError:
        return None
    try:
        with yt_dlp.YoutubeDL(_youtube_search_opts()) as ydl:
            return ydl.extract_info(DEFAULT_SEARCH_PREFIX + query, download=False)
    except Exception:
        return None


async def search_song(
    query: str,
    _search: Optional[Callable[[str], Optional[dict]]] = None,
) -> Optional[Dict[str, object]]:
    """Search YouTube for `query`, return the top result or None.

    Result: {video_id, title, artist, duration_seconds, url}, or
    {"error": "too_long", "title", "duration_seconds"} when the top
    result exceeds MAX_SONG_SECONDS (10 min) — the caller surfaces
    that instead of downloading. A failed search (yt-dlp raised, or
    the 30s timeout hit — the live symptom on a blocked datacenter
    IP) returns {"error": "search_unavailable"} instead of None, so
    the caller can tell "YouTube search isn't answering" apart from
    "no such song": None now means only "the search ran and found
    nothing". Results are cached 10 minutes (failures are not cached).
    `_search` is an injectable raw-info fetcher for tests; the default
    runs yt-dlp. Never raises.
    """
    if not query or not isinstance(query, str) or not query.strip():
        return None
    query = query.strip()
    cached = _cache_get(query)
    if cached is not None:
        return cached

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
