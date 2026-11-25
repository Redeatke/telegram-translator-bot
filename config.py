"""
Configuration, Constants, In-Memory State & Formatting Utilities
"""
from __future__ import annotations

import os
import logging
import asyncio
import re
import tempfile
import uuid
import time
from datetime import datetime, timezone
import json
import html
import threading
import itertools
from typing import Optional, List, Dict, Any, Tuple
from dotenv import load_dotenv
from telethon_downloader import is_telethon_available

# Detect Termux environment
IS_TERMUX = bool(os.getenv("TERMUX_VERSION") or (os.getenv("PREFIX", "").startswith("/data/data/com.termux")))

# Load environment variables
load_dotenv(override=True)

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger("telegram_bot")
logging.getLogger("httpx").setLevel(logging.WARNING)

# Anti-flood guards: ignore stale backlogs and deduplicate messages
BOT_STARTUP_TIME = time.time()
_PROCESSED_MESSAGES = set()
_PROCESSED_MESSAGES_LOCK = threading.Lock()
_MAX_PROCESSED_MESSAGES = 10000

def should_skip_message(message, max_age_seconds: int = 120) -> bool:
    """
    Check if a message should be skipped:
    1. If message is older than max_age_seconds (prevents re-processing backlogs).
    2. If message was sent before this bot process started.
    3. If message (chat_id, message_id) has already been processed in this session.
    """
    if not message:
        return True

    key = (message.chat_id, message.message_id)
    with _PROCESSED_MESSAGES_LOCK:
        if key in _PROCESSED_MESSAGES:
            return True

        if len(_PROCESSED_MESSAGES) > _MAX_PROCESSED_MESSAGES:
            _PROCESSED_MESSAGES.clear()
        _PROCESSED_MESSAGES.add(key)

    if getattr(message, "date", None):
        now_utc = datetime.now(timezone.utc)
        age = (now_utc - message.date).total_seconds()
        if age > max_age_seconds:
            logger.info(f"Skipping stale message {message.message_id} in {message.chat_id} (age: {age:.1f}s)")
            return True
        if message.date.timestamp() < (BOT_STARTUP_TIME - 5):
            logger.info(f"Skipping pre-startup message {message.message_id} in {message.chat_id}")
            return True

    return False


# ─── Rate Limiting ──────────────────────────────────────────────────────────
# Per-user sliding-window limits for the paid / heavy paths (AI translation,
# free translation, voice). Previously there was no limit at all, so one user
# could burn the whole OpenRouter quota. Admins get 5x the normal limit.
# Limits are configurable via env, e.g. RATE_LIMIT_AI_PER_HOUR=20.
import collections

RATE_LIMIT_AI_PER_HOUR = max(1, int(os.getenv("RATE_LIMIT_AI_PER_HOUR", "20")))
RATE_LIMIT_FREE_PER_HOUR = max(1, int(os.getenv("RATE_LIMIT_FREE_PER_HOUR", "60")))
RATE_LIMIT_VOICE_PER_HOUR = max(1, int(os.getenv("RATE_LIMIT_VOICE_PER_HOUR", "10")))
RATE_LIMIT_CHAT_PER_HOUR = max(1, int(os.getenv("RATE_LIMIT_CHAT_PER_HOUR", "20")))
MAX_TRANSLATION_CHARS = max(100, int(os.getenv("MAX_TRANSLATION_CHARS", "4000")))
MAX_VOICE_DURATION_SECONDS = max(30, int(os.getenv("MAX_VOICE_DURATION_SECONDS", "300")))
MAX_VOICE_FILE_MB = max(1, int(os.getenv("MAX_VOICE_FILE_MB", "5")))

_RATE_LIMITS = {
    "ai": RATE_LIMIT_AI_PER_HOUR,
    "free": RATE_LIMIT_FREE_PER_HOUR,
    "voice": RATE_LIMIT_VOICE_PER_HOUR,
    "chat": RATE_LIMIT_CHAT_PER_HOUR,
}
_rate_limit_lock = threading.Lock()
_rate_limit_hits: Dict[Tuple[int, str], "collections.deque"] = {}


def _rate_limit_for(user_id: int, kind: str) -> int:
    base = _RATE_LIMITS.get(kind, RATE_LIMIT_FREE_PER_HOUR)
    return base * 5 if user_id in ADMIN_USER_IDS else base


def check_rate_limit(user_id: int, kind: str) -> bool:
    """Return True and record a hit if the user is within their hourly limit.

    Sliding window over the last 3600 seconds, thread-safe. Returns False
    (without recording) when the limit is exceeded — callers should reply
    politely and NOT call the AI / translation service.
    """
    limit = _rate_limit_for(user_id, kind)
    now = time.time()
    key = (user_id, kind)
    with _rate_limit_lock:
        hits = _rate_limit_hits.setdefault(key, collections.deque())
        while hits and hits[0] <= now - 3600:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True


def rate_limit_remaining(user_id: int, kind: str) -> int:
    """How many uses of `kind` this user has left in the current hour."""
    limit = _rate_limit_for(user_id, kind)
    now = time.time()
    with _rate_limit_lock:
        hits = _rate_limit_hits.get((user_id, kind), collections.deque())
        recent = sum(1 for t in hits if t > now - 3600)
    return max(0, limit - recent)


async def safe_answer_query(query, *args, **kwargs) -> bool:
    """Safely answer callback queries, ignoring expired query timeout errors."""
    if not query:
        return False
    try:
        await query.answer(*args, **kwargs)
        return True
    except Exception as e:
        logger.debug(f"Suppressed expired callback query answer error: {e}")
        return False


# ─── API Configuration ───────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()

_config_channel_id_raw = os.getenv("CONFIG_CHANNEL_ID", "").strip()
CONFIG_CHANNEL_ID: Optional[int] = None
if _config_channel_id_raw:
    # If the user pasted a t.me link: e.g. https://t.me/c/3907408082/5 -> 3907408082
    if "/c/" in _config_channel_id_raw:
        _config_channel_id_raw = _config_channel_id_raw.split("/c/")[1].split("/")[0]
    cid_str = _config_channel_id_raw.strip()
    if cid_str.lstrip("-").isdigit():
        cid_int = int(cid_str)
        # Telegram channel IDs require the -100 prefix (e.g. -1003907408082)
        if cid_int > 0:
            cid_int = int(f"-100{cid_int}")
        CONFIG_CHANNEL_ID = cid_int

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")

AUTO_PING_ENABLED = os.getenv("AUTO_PING_ENABLED", "True").lower() == "true"
PING_INTERVAL = float(os.getenv("PING_INTERVAL", "60.0"))
PING_URL = os.getenv("PING_URL", "")

ALLOW_ALL_TO_USE_AI = os.getenv("ALLOW_ALL_TO_USE_AI", "True").lower() == "true"

# Admin user IDs come only from the ADMIN_USER_IDS env var.
# Previously a hardcoded ID was the default and was re-appended even when
# the env var was set — that was a permanent backdoor admin. Now: no env
# var means no bot admins (group admins can still use group features).
ADMIN_USER_IDS = [
    int(uid.strip())
    for uid in (os.getenv("ADMIN_USER_IDS") or "").split(",")
    if uid.strip().isdigit()
]
if not ADMIN_USER_IDS:
    logger.warning(
        "ADMIN_USER_IDS is not set — no bot admins are configured. "
        "Set ADMIN_USER_IDS in the environment to enable admin-only commands."
    )

MAINTENANCE_MODE = os.getenv("MAINTENANCE_MODE", "False").lower() == "true"

def is_maintenance_active_for_user(user_id: int) -> bool:
    """Return True if maintenance mode is enabled and user is not an admin."""
    return MAINTENANCE_MODE and (user_id not in ADMIN_USER_IDS)

def get_maintenance_mode() -> bool:
    return MAINTENANCE_MODE

def set_maintenance_mode(mode: bool) -> None:
    global MAINTENANCE_MODE
    MAINTENANCE_MODE = mode

MAINTENANCE_NOTICE = (
    "🚧 <b>System Maintenance / Update in Progress</b>\n\n"
    "<i>We're actively deploying updates and patching new features. The bot will be fully back online shortly!</i>"
)

# OpenRouter AI Client
has_ai = False
ai_client = None
if OPENROUTER_API_KEY:
    try:
        from openai import OpenAI
        ai_client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_API_KEY,
        )
        has_ai = True
        logger.info(f"OpenRouter API configured with model '{OPENROUTER_MODEL}'.")
    except Exception as e:
        logger.error(f"Error configuring OpenRouter: {e}")
else:
    logger.warning("OPENROUTER_API_KEY not found. AI engine will be unavailable.")


# ─── Cookies Normalization & Setup (YouTube + Instagram) ─────────────────────

import base64 as _b64

def _normalize_cookies(
    raw: str,
    domain_markers: Tuple[str, ...],
    label: str,
    header_domain: Optional[str] = None,
    header_marker: str = "sessionid=",
) -> str:
    """Normalize Netscape, escaped \\n/\\t, base64, Cookie-Editor JSON, or header-style cookies."""
    raw = raw.strip()
    if not raw:
        return ""

    # 1. Try base64 decode first (safest for host dashboards)
    try:
        compact_b64 = "".join(raw.split())
        decoded = _b64.b64decode(compact_b64, validate=True).decode("utf-8", errors="replace")
        if "\t" in decoded and any(m in decoded for m in domain_markers):
            logger.info(f"Decoded {label} cookies from base64.")
            return decoded.strip()
    except Exception:
        pass

    # 2. Unescape literal \n and \t (host dashboards often do this)
    if "\\n" in raw or "\\t" in raw:
        unescaped = raw.replace("\\n", "\n").replace("\\t", "\t")
        if "\t" in unescaped and any(m in unescaped for m in domain_markers):
            logger.info(f"Unescaped literal \\n/\\t in {label} cookies.")
            raw = unescaped

    # 3. Cookie-Editor JSON format: [{"domain":".instagram.com","name":"..."}]
    if raw.startswith("["):
        try:
            cookies_json = json.loads(raw)
            lines = ["# Netscape HTTP Cookie File", ""]
            for c in cookies_json:
                domain = c.get("domain", "")
                if not any(m in domain for m in domain_markers):
                    continue
                flag = "TRUE" if domain.startswith(".") else "FALSE"
                path = c.get("path", "/")
                secure = "TRUE" if c.get("secure", True) else "FALSE"
                expiry = str(int(c.get("expirationDate", 0)))
                name = c.get("name", "")
                value = c.get("value", "")
                lines.append(f"{domain}\t{flag}\t{path}\t{secure}\t{expiry}\t{name}\t{value}")
            if len(lines) > 2:
                logger.info(f"Converted Cookie-Editor JSON for {label} ({len(lines) - 2} cookies).")
                return "\n".join(lines)
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    # 4. Header-style: "key=val; key2=val2"
    if header_domain and header_marker in raw and "\t" not in raw and "\n" not in raw.strip():
        pairs = [p.strip() for p in raw.split(";") if "=" in p]
        lines = ["# Netscape HTTP Cookie File", ""]
        for pair in pairs:
            name, _, value = pair.partition("=")
            name = name.strip()
            value = value.strip()
            if name and value:
                lines.append(f"{header_domain}\tTRUE\t/\tTRUE\t0\t{name}\t{value}")
        if len(lines) > 2:
            logger.info(f"Converted header-style {label} cookies ({len(lines) - 2} cookies).")
            return "\n".join(lines)

    # 5. Already a valid Netscape file
    return raw

def _normalize_instagram_cookies(raw: str) -> str:
    """Normalize various Instagram cookie formats into a proper Netscape cookies.txt."""
    return _normalize_cookies(raw, ("instagram.com",), "Instagram", header_domain=".instagram.com")


def _normalize_linkedin_cookies(raw: str) -> str:
    """Normalize various LinkedIn cookie formats into a proper Netscape cookies.txt."""
    return _normalize_cookies(raw, ("linkedin.com",), "LinkedIn", header_domain=".linkedin.com", header_marker="li_at=")

YOUTUBE_COOKIES_FILE = None
cookies_env = os.getenv("YOUTUBE_COOKIES") or os.getenv("YOUTUBE_COOKIE")
if cookies_env:
    try:
        _yt_cookie_text = _normalize_cookies(cookies_env, ("youtube.com", "google.com"), "YouTube")
        if _yt_cookie_text:
            tmp_cookie_path = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
            with open(tmp_cookie_path, "w", encoding="utf-8") as f:
                f.write(_yt_cookie_text)
            YOUTUBE_COOKIES_FILE = tmp_cookie_path
            logger.info("Loaded YouTube cookies from YOUTUBE_COOKIES environment variable.")
        else:
            logger.warning("YOUTUBE_COOKIES env var is set but empty after normalization.")
    except Exception as e:
        logger.error(f"Failed to write YOUTUBE_COOKIES to temp file: {e}")
elif os.path.exists("cookies.txt"):
    YOUTUBE_COOKIES_FILE = os.path.abspath("cookies.txt")
    logger.info("Loaded YouTube cookies from local cookies.txt file.")

def set_youtube_cookies_file(path: str) -> None:
    global YOUTUBE_COOKIES_FILE
    YOUTUBE_COOKIES_FILE = path

# ─── Instagram Cookies Setup ──────────────────────────────────────────────────
# Instagram requires login for almost all yt-dlp requests since 2025.
# Set INSTAGRAM_COOKIES env var to a Netscape-format cookie string, or place
# instagram_cookies.txt next to the bot. Export from your browser with the
# "Get cookies.txt LOCALLY" extension while logged into instagram.com.
#
# Both cookie loaders accept:
#   - A real Netscape cookies.txt (multi-line with tabs)
#   - A one-line paste with literal \n and \t (from host dashboards)
#   - Base64-encoded cookies.txt (safest for host dashboards)
#   - Cookie-Editor JSON export ([{"domain":".instagram.com", ...}])
#   - A bare "sessionid=...;csrftoken=..." header string (Instagram)

INSTAGRAM_COOKIES_FILE = None
_ig_cookies_env = os.getenv("INSTAGRAM_COOKIES") or os.getenv("INSTAGRAM_COOKIE")
if _ig_cookies_env:
    try:
        _ig_cookie_text = _normalize_instagram_cookies(_ig_cookies_env)
        if _ig_cookie_text:
            _ig_cookie_path = os.path.join(tempfile.gettempdir(), "ig_cookies.txt")
            with open(_ig_cookie_path, "w", encoding="utf-8") as f:
                f.write(_ig_cookie_text)
            INSTAGRAM_COOKIES_FILE = _ig_cookie_path
            _has_session = "sessionid" in _ig_cookie_text.lower()
            logger.info(
                f"Loaded Instagram cookies from env var "
                f"(sessionid={'present' if _has_session else 'MISSING'})."
            )
            if not _has_session:
                logger.warning(
                    "Instagram cookies loaded but sessionid is MISSING. "
                    "Downloads will likely fail. Re-export cookies while logged in."
                )
        else:
            logger.warning("INSTAGRAM_COOKIES env var is set but empty after normalization.")
    except Exception as e:
        logger.error(f"Failed to write INSTAGRAM_COOKIES to temp file: {e}")
elif os.path.exists("instagram_cookies.txt"):
    INSTAGRAM_COOKIES_FILE = os.path.abspath("instagram_cookies.txt")
    try:
        with open(INSTAGRAM_COOKIES_FILE, "r", encoding="utf-8") as f:
            _file_text = f.read()
        _has_session = "sessionid" in _file_text.lower()
        logger.info(
            f"Loaded Instagram cookies from local file "
            f"(sessionid={'present' if _has_session else 'MISSING'})."
        )
    except Exception:
        logger.info("Loaded Instagram cookies from local instagram_cookies.txt file.")

if not INSTAGRAM_COOKIES_FILE:
    logger.warning(
        "No Instagram cookies configured. Instagram Reels/posts may fail. "
        "Set INSTAGRAM_COOKIES env var or place instagram_cookies.txt next to the bot."
    )

# ─── LinkedIn Cookies Setup ───────────────────────────────────────────────────
# LinkedIn blocks datacenter IPs with HTTP 999 / a login wall for many video
# posts. Set LINKEDIN_COOKIES env var to a Netscape-format cookie string, or
# place linkedin_cookies.txt next to the bot. Export from your browser with
# the "Get cookies.txt LOCALLY" extension while logged into linkedin.com.
#
# Accepted formats are the same as Instagram's (see _normalize_cookies):
# Netscape cookies.txt, one-line paste with literal \n and \t, base64,
# Cookie-Editor JSON, or a bare "li_at=...;JSESSIONID=..." header string.
# The login cookie that matters is li_at — without it, downloads will
# likely still be blocked.

LINKEDIN_COOKIES_FILE = None
_li_cookies_env = os.getenv("LINKEDIN_COOKIES") or os.getenv("LINKEDIN_COOKIE")
if _li_cookies_env:
    try:
        _li_cookie_text = _normalize_linkedin_cookies(_li_cookies_env)
        if _li_cookie_text:
            _li_cookie_path = os.path.join(tempfile.gettempdir(), "li_cookies.txt")
            with open(_li_cookie_path, "w", encoding="utf-8") as f:
                f.write(_li_cookie_text)
            LINKEDIN_COOKIES_FILE = _li_cookie_path
            _has_li_at = "li_at" in _li_cookie_text.lower()
            logger.info(
                f"Loaded LinkedIn cookies from env var "
                f"(li_at={'present' if _has_li_at else 'MISSING'})."
            )
            if not _has_li_at:
                logger.warning(
                    "LinkedIn cookies loaded but li_at is MISSING. "
                    "Downloads will likely fail. Re-export cookies while logged in."
                )
        else:
            logger.warning("LINKEDIN_COOKIES env var is set but empty after normalization.")
    except Exception as e:
        logger.error(f"Failed to write LINKEDIN_COOKIES to temp file: {e}")
elif os.path.exists("linkedin_cookies.txt"):
    LINKEDIN_COOKIES_FILE = os.path.abspath("linkedin_cookies.txt")
    try:
        with open(LINKEDIN_COOKIES_FILE, "r", encoding="utf-8") as f:
            _file_text = f.read()
        _has_li_at = "li_at" in _file_text.lower()
        logger.info(
            f"Loaded LinkedIn cookies from local file "
            f"(li_at={'present' if _has_li_at else 'MISSING'})."
        )
    except Exception:
        logger.info("Loaded LinkedIn cookies from local linkedin_cookies.txt file.")

if not LINKEDIN_COOKIES_FILE:
    logger.warning(
        "No LinkedIn cookies configured. LinkedIn video downloads may be blocked (HTTP 999). "
        "Set LINKEDIN_COOKIES env var or place linkedin_cookies.txt next to the bot."
    )

# ─── Proxy & Throttling Setup (Instagram + YouTube) ──────────────────────────
def _build_proxy_rotator(raw_value: str):
    """Parse a comma-separated proxy list and return (proxies, next_proxy_fn)."""
    proxies = [p.strip() for p in (raw_value or "").split(",") if p.strip()]
    lock = threading.Lock()
    cycle = itertools.cycle(proxies) if proxies else None

    def _next() -> Optional[str]:
        if not cycle:
            return None
        with lock:
            return next(cycle)

    return proxies, _next

_raw_ig_proxies, get_instagram_proxy = _build_proxy_rotator(
    os.getenv("INSTAGRAM_PROXY") or os.getenv("INSTAGRAM_PROXIES") or os.getenv("PROXY") or ""
)
INSTAGRAM_PROXY = _raw_ig_proxies[0] if _raw_ig_proxies else None
INSTAGRAM_MAX_CONCURRENT = max(1, int(os.getenv("INSTAGRAM_MAX_CONCURRENT", "1")))
INSTAGRAM_REQUEST_DELAY = max(0.0, float(os.getenv("INSTAGRAM_REQUEST_DELAY", "2.5")))

if INSTAGRAM_PROXY:
    logger.info(f"Loaded {len(_raw_ig_proxies)} Instagram proxy endpoint(s) (concurrency={INSTAGRAM_MAX_CONCURRENT}, delay={INSTAGRAM_REQUEST_DELAY}s).")
else:
    logger.info(f"No Instagram proxy configured (concurrency={INSTAGRAM_MAX_CONCURRENT}, delay={INSTAGRAM_REQUEST_DELAY}s).")

_raw_yt_proxies, get_youtube_proxy = _build_proxy_rotator(
    os.getenv("YOUTUBE_PROXY") or os.getenv("YOUTUBE_PROXIES") or os.getenv("PROXY") or ""
)
YOUTUBE_PROXY = _raw_yt_proxies[0] if _raw_yt_proxies else None
if YOUTUBE_PROXY:
    logger.info(f"Loaded {len(_raw_yt_proxies)} YouTube proxy endpoint(s).")

YOUTUBE_RELAY_URL = os.getenv("YOUTUBE_RELAY_URL", "").strip().rstrip("/") or None

LOCAL_BOT_API_URL = os.getenv("TELEGRAM_API_URL", "").strip() or os.getenv("LOCAL_BOT_API_URL", "").strip()
HAS_TELETHON_UPLOAD = is_telethon_available()
MAX_UPLOAD_SIZE_MB = int(
    os.getenv("MAX_UPLOAD_SIZE_MB", "2000" if (LOCAL_BOT_API_URL or HAS_TELETHON_UPLOAD) else "50")
)
MAX_VIDEO_DURATION = 1800  # 30 minutes
if HAS_TELETHON_UPLOAD and not LOCAL_BOT_API_URL:
    logger.info(f"Telethon MTProto upload enabled (up to {MAX_UPLOAD_SIZE_MB} MB for files >50 MB).")


# ─── User State ───────────────────────────────────────────────────────────────

user_configs: Dict[int, Dict[str, str]] = {}

# Per-user language/engine settings persist across restarts in
# user_configs.json (same DATA_DIR pattern as chat_configs.json).
# Previously these lived only in memory and reset to en/free on every deploy.
USER_CONFIGS_FILE = os.path.join(
    os.environ.get("DATA_DIR", os.path.dirname(__file__)), "user_configs.json"
)
_user_configs_lock = threading.Lock()


def load_user_configs() -> None:
    """Load per-user configs from user_configs.json, if present."""
    global user_configs
    if not os.path.exists(USER_CONFIGS_FILE):
        return
    try:
        with open(USER_CONFIGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        loaded: Dict[int, Dict[str, str]] = {}
        for k, v in data.items():
            if not isinstance(v, dict):
                continue
            # Literals match DEFAULT_ENGINE/DEFAULT_TARGET_LANG ("free"/"en"),
            # which are defined later in this module.
            loaded[int(k)] = {
                "engine": str(v.get("engine", "free")),
                "target": str(v.get("target", "en")),
            }
        user_configs = loaded
        logger.info(f"Loaded user configs for {len(user_configs)} users from local cache.")
    except Exception as e:
        logger.error(f"Failed to load user_configs.json: {e}")
        user_configs = {}


def save_user_configs() -> None:
    """Atomically save per-user configs (temp file in same dir + os.replace)."""
    with _user_configs_lock:
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=os.path.dirname(USER_CONFIGS_FILE) or ".",
                prefix=".user_configs.",
                suffix=".tmp",
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({str(k): v for k, v in user_configs.items()}, f, indent=2)
            os.replace(tmp_path, USER_CONFIGS_FILE)
            tmp_path = None
        except Exception as e:
            logger.error(f"Failed to save user_configs.json: {e}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


load_user_configs()


# ─── Chat Media Config Persistence ──────────────────────────────────────────

CHAT_CONFIGS_FILE = os.path.join(os.environ.get("DATA_DIR", os.path.dirname(__file__)), "chat_configs.json")
chat_configs: Dict[int, Dict[str, Any]] = {}

DEFAULT_CHAT_CONFIG = {
    "youtube": True,
    "twitter": True,
    "twitch": True,
    "tiktok": True,
    "instagram": True,
    "threads": True,
    "reddit": True,
    "linkedin": True,
    "auto_download": True,
    "chat": False,
}

_config_bot = None


def _atomic_write_json(path: str, data: Any) -> None:
    """Write JSON atomically: temp file in the same directory + os.replace,
    so a crash or concurrent write cannot leave a truncated file."""
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(path) or ".", prefix=".tmp.", suffix=".json"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


_chat_configs_lock = threading.Lock()


def load_chat_configs() -> None:
    """Load chat media configurations from the local chat_configs.json cache."""
    global chat_configs
    if os.path.exists(CHAT_CONFIGS_FILE):
        try:
            with open(CHAT_CONFIGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                chat_configs = {int(k): v for k, v in data.items()}
                logger.info(f"Loaded media configs for {len(chat_configs)} chats from local cache.")
        except Exception as e:
            logger.error(f"Failed to load chat_configs.json: {e}")
            chat_configs = {}

async def load_chat_configs_from_channel() -> None:
    """Override the local cache with the copy pinned in the Telegram storage channel, if configured."""
    global chat_configs
    if not CONFIG_CHANNEL_ID or not _config_bot:
        return
    try:
        chat = await _config_bot.get_chat(CONFIG_CHANNEL_ID)
        if chat.pinned_message and chat.pinned_message.text:
            data = json.loads(chat.pinned_message.text)
            # Only overwrite the local cache with a payload that actually
            # parses and is non-empty. A stale/empty pinned message must
            # never wipe the local settings (previously it overwrote
            # unconditionally).
            if not isinstance(data, dict) or not data:
                logger.warning(
                    "Pinned chat-configs payload is empty or invalid; "
                    "keeping the local chat_configs.json instead."
                )
                return
            chat_configs = {int(k): v for k, v in data.items()}
            logger.info(f"Loaded media configs for {len(chat_configs)} chats from Telegram storage channel.")
            try:
                _atomic_write_json(CHAT_CONFIGS_FILE, chat_configs)
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Failed to load chat_configs from Telegram storage channel: {e}")

async def _push_chat_configs_to_channel() -> None:
    """Push the in-memory chat_configs to the pinned message in the storage channel."""
    if not CONFIG_CHANNEL_ID or not _config_bot:
        return
    # Use compact JSON representation to fit as many chats as possible under 4096 chars
    payload = json.dumps(chat_configs, separators=(',', ':'))
    if len(payload) > 4096:
        logger.error(
            "chat_configs payload exceeds Telegram's 4096-char message limit "
            f"({len(payload)} chars for {len(chat_configs)} chats); skipping channel sync. "
            "Local chat_configs.json is still saved, but settings will NOT survive "
            "the next redeploy until the payload shrinks (fewer chats / shorter config)."
        )
        return
    try:
        chat = await _config_bot.get_chat(CONFIG_CHANNEL_ID)
        if chat.pinned_message:
            try:
                await _config_bot.edit_message_text(
                    chat_id=CONFIG_CHANNEL_ID, message_id=chat.pinned_message.message_id, text=payload
                )
            except Exception as e:
                if "Message is not modified" not in str(e):
                    raise
        else:
            msg = await _config_bot.send_message(chat_id=CONFIG_CHANNEL_ID, text=payload)
            await _config_bot.pin_chat_message(chat_id=CONFIG_CHANNEL_ID, message_id=msg.message_id, disable_notification=True)
    except Exception as e:
        logger.error(f"Failed to push chat_configs to Telegram storage channel: {e}")

def save_chat_configs() -> None:
    """Save chat media configurations locally and sync them to the Telegram storage channel."""
    with _chat_configs_lock:
        try:
            _atomic_write_json(CHAT_CONFIGS_FILE, chat_configs)
        except Exception as e:
            logger.error(f"Failed to save chat_configs.json: {e}")

    try:
        asyncio.get_running_loop().create_task(_push_chat_configs_to_channel())
    except RuntimeError:
        pass

def get_chat_config(chat_id: int) -> dict:
    """Get or initialize media download configuration for a chat."""
    if chat_id not in chat_configs:
        chat_configs[chat_id] = DEFAULT_CHAT_CONFIG.copy()
        save_chat_configs()
    else:
        updated = False
        for k, v in DEFAULT_CHAT_CONFIG.items():
            if k not in chat_configs[chat_id]:
                chat_configs[chat_id][k] = v
                updated = True
        if updated:
            save_chat_configs()
    return chat_configs[chat_id]

def is_downloader_enabled(chat_id: int, platform: str) -> bool:
    """Check if a platform downloader is enabled for a given chat."""
    config = get_chat_config(chat_id)
    return config.get(platform, True)

def get_download_mode(chat_id: int) -> bool:
    """Return True if auto-download mode is enabled, False if button-prompt mode."""
    config = get_chat_config(chat_id)
    return config.get("auto_download", True)

def toggle_downloader(chat_id: int, platform: str) -> bool:
    """Toggle a platform downloader for a chat and persist change."""
    config = get_chat_config(chat_id)
    new_state = not config.get(platform, True)
    config[platform] = new_state
    save_chat_configs()
    return new_state

def toggle_download_mode(chat_id: int) -> bool:
    """Toggle auto_download mode for a chat and persist change."""
    config = get_chat_config(chat_id)
    new_state = not config.get("auto_download", True)
    config["auto_download"] = new_state
    save_chat_configs()
    return new_state

def reset_chat_config(chat_id: int) -> Dict[str, Any]:
    """Reset a chat's configuration back to defaults (all platforms enabled)."""
    chat_configs[chat_id] = DEFAULT_CHAT_CONFIG.copy()
    save_chat_configs()
    return chat_configs[chat_id]

load_chat_configs()


# ─── High Performance In-Memory LRU Media / Card Cache ─────────────────────────

# Cap for raw bytes stored in the in-memory media cache (see MediaMemoryCache.set).
MAX_MEDIA_CACHE_BYTES = max(1, int(os.getenv("MAX_MEDIA_CACHE_MB", "20"))) * 1024 * 1024


class MediaMemoryCache:
    """Thread-safe bounded in-memory LRU cache with TTL expiration."""
    def __init__(self, max_items: int = 150, ttl_seconds: int = 86400):
        self.max_items = max_items
        self.ttl = ttl_seconds
        self._cache = {}
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            if key in self._cache:
                ts, val = self._cache[key]
                if time.time() - ts < self.ttl:
                    return val
                del self._cache[key]
            return None

    def set(self, key: str, val):
        # Never cache large raw bytes (e.g. a full video) — 150 items of
        # 20-50 MB would be gigabytes of RAM. Small values (file_ids,
        # card PNGs, avatars) are fine. Callers that need the video again
        # re-download it instead of OOMing the host.
        if isinstance(val, (bytes, bytearray)) and len(val) > MAX_MEDIA_CACHE_BYTES:
            logger.warning(
                f"Skipping media cache for {key}: {len(val)} bytes exceeds "
                f"{MAX_MEDIA_CACHE_BYTES} byte cap."
            )
            return
        with self._lock:
            if len(self._cache) >= self.max_items:
                oldest_keys = sorted(self._cache.keys(), key=lambda k: self._cache[k][0])[:max(1, len(self._cache) // 5)]
                for k in oldest_keys:
                    self._cache.pop(k, None)
            self._cache[key] = (time.time(), val)

media_cache = MediaMemoryCache()
url_media_cache = MediaMemoryCache(max_items=1000, ttl_seconds=7 * 86400)

# Pending download buttons cache
pending_downloads: Dict[str, Dict[str, Any]] = {}
_pending_downloads_lock = threading.Lock()

def store_pending_download(url: str, platform: str) -> str:
    """Store URL in pending downloads cache and return a short ID."""
    short_id = uuid.uuid4().hex[:10]
    with _pending_downloads_lock:
        pending_downloads[short_id] = {"url": url, "platform": platform, "time": time.time()}
        now = time.time()
        for k in list(pending_downloads.keys()):
            if now - pending_downloads[k]["time"] > 7200:
                pending_downloads.pop(k, None)
    return short_id


# ─── Language Constants ───────────────────────────────────────────────────────

DEFAULT_ENGINE = "free"
DEFAULT_TARGET_LANG = "en"

COMMON_LANGUAGES = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German",
    "it": "Italian", "pt": "Portuguese", "ru": "Russian", "zh": "Chinese",
    "ja": "Japanese", "ko": "Korean", "ar": "Arabic", "hi": "Hindi",
    "tr": "Turkish", "nl": "Dutch", "uk": "Ukrainian", "pl": "Polish",
    "sv": "Swedish", "da": "Danish", "fi": "Finnish", "no": "Norwegian",
    "el": "Greek", "he": "Hebrew", "th": "Thai", "vi": "Vietnamese",
    "id": "Indonesian", "ms": "Malay", "tl": "Filipino", "sw": "Swahili",
    "am": "Amharic", "bn": "Bengali", "ro": "Romanian", "hu": "Hungarian",
    "cs": "Czech", "fa": "Persian", "ur": "Urdu",
}

LANG_FLAGS = {
    "en": "🇬🇧", "es": "🇪🇸", "fr": "🇫🇷", "de": "🇩🇪", "it": "🇮🇹",
    "pt": "🇧🇷", "ru": "🇷🇺", "zh": "🇨🇳", "ja": "🇯🇵", "ko": "🇰🇷",
    "ar": "🇸🇦", "hi": "🇮🇳", "tr": "🇹🇷", "uk": "🇺🇦", "nl": "🇳🇱",
    "pl": "🇵🇱", "sv": "🇸🇪", "da": "🇩🇰", "fi": "🇫🇮", "no": "🇳🇴",
    "el": "🇬🇷", "he": "🇮🇱", "th": "🇹🇭", "vi": "🇻🇳", "id": "🇮🇩",
    "ms": "🇲🇾", "tl": "🇵🇭", "sw": "🇰🇪", "am": "🇪🇹", "bn": "🇧🇩",
    "ro": "🇷🇴", "hu": "🇭🇺", "cs": "🇨🇿", "sk": "🇸🇰", "bg": "🇧🇬",
    "hr": "🇭🇷", "sr": "🇷🇸", "ca": "🇪🇸", "fa": "🇮🇷", "ur": "🇵🇰",
}

def get_flag(lang_code: str) -> str:
    """Get flag emoji for a language code, fallback to globe."""
    return LANG_FLAGS.get(lang_code, "🌐")


# ─── URL Patterns ─────────────────────────────────────────────────────────────

YOUTUBE_URL_PATTERN = re.compile(
    r'(?:https?://)?(?:www\.|m\.)?(?:youtube\.com/(?:shorts/|watch\?v=|embed/|live/|v/)|youtu\.be/)[\w\-]+',
    re.IGNORECASE
)

TWITTER_URL_PATTERN = re.compile(
    r'(?:https?://)?(?:www\.|mobile\.)?(?:twitter\.com|x\.com)/([A-Za-z0-9_]+)/status/(\d+)',
    re.IGNORECASE
)

TWITCH_CLIP_PATTERN = re.compile(
    r'(?:https?://)?(?:www\.|m\.)?(?:clips\.twitch\.tv/|twitch\.tv/[A-Za-z0-9_]+/clip/)([A-Za-z0-9_-]+)',
    re.IGNORECASE
)

TIKTOK_URL_PATTERN = re.compile(
    r'(?:https?://)?(?:www\.|vm\.|vt\.)?tiktok\.com/(?:@[A-Za-z0-9_.]+/video/|v/|t/)?([A-Za-z0-9_]+)',
    re.IGNORECASE
)

INSTAGRAM_URL_PATTERN = re.compile(
    r'(?:https?://)?(?:www\.)?instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)',
    re.IGNORECASE
)

THREADS_URL_PATTERN = re.compile(
    r'(?:https?://)?(?:www\.)?threads\.(?:net|com)/(?:@[A-Za-z0-9_.]+/post/[A-Za-z0-9_-]+|share/[A-Za-z0-9_-]+|t/[A-Za-z0-9_-]+)',
    re.IGNORECASE
)

THREADS_CANONICAL_PATTERN = re.compile(
    r'threads\.(?:net|com)/@([A-Za-z0-9_.]+)/post/([A-Za-z0-9_-]+)',
    re.IGNORECASE
)

REDDIT_URL_PATTERN = re.compile(
    r'(?:https?://)?(?:www\.|old\.|m\.)?reddit\.com/(?:r/[A-Za-z0-9_]+/(?:comments|s)/|s/)[A-Za-z0-9_-]+|(?:https?://)?v\.redd\.it/[A-Za-z0-9_-]+',
    re.IGNORECASE
)

LINKEDIN_URL_PATTERN = re.compile(
    # Post URLs that can carry video only: /posts/<slug>-activity-<id> and
    # /feed/update/urn:li:activity:<id>, plus lnkd.in short links (the form
    # LinkedIn's app share hands out — resolved to the real post URL before
    # download; see resolve_linkedin_short_link in downloaders.py).
    # Deliberately NOT matched: profiles (/in/), companies, jobs, Pulse
    # articles, or lnkd.in links with no path segment.
    r'(?:https?://)?(?:www\.)?linkedin\.com/(?:posts/[^\s?#]*activity-\d{10,}[^\s?#]*|feed/update/urn:li:activity:\d{10,})'
    r'|(?:https?://)?(?:www\.)?lnkd\.in/(?:p/[A-Za-z0-9_-]+|[A-Za-z0-9_-]{2,})',
    re.IGNORECASE
)


# ─── Formatting Helpers ───────────────────────────────────────────────────────

def fmt_card(title: str, body: str, footer: str = "") -> str:
    """Clean message formatting without box drawing lines.

    The title is escaped — it can contain user-controlled text in some
    callers. The body is intentionally NOT escaped here: callers build it
    with their own <b>/<code> formatting and escape user data themselves.
    """
    lines = [f"<b>{html.escape(title)}</b>\n", body.strip()]
    if footer:
        lines.append(f"\n<i>{footer}</i>")
    return "\n".join(lines)

def fmt_translation(src_lang: str, target_lang: str, translated_text: str, fallback: bool = False) -> str:
    """Format translation clean and simple matching Phoenix style."""
    if src_lang and src_lang != "auto":
        src_name = COMMON_LANGUAGES.get(src_lang.lower(), src_lang.lower())
        src_flag = get_flag(src_lang.lower())
        src_label = f"{src_flag} {html.escape(src_name)}"
    else:
        src_label = "auto-detected"
    target_name = COMMON_LANGUAGES.get(target_lang.lower(), target_lang.lower())
    target_flag = get_flag(target_lang.lower())
    target_label = f"{target_flag} {html.escape(target_name)}"

    # translated_text is user/model-controlled — escape it so a translation
    # containing <b>, <a href=...>, or malformed tags cannot inject HTML.
    msg = f"Translated from {src_label} to {target_label}:\n{html.escape(translated_text)}"
    if fallback:
        msg += "\n\n⚠️ (Fell back to free engine)"
    return msg

def fmt_success(text: str) -> str:
    return f"✅  {text}"

def fmt_error(text: str) -> str:
    return f"❌  {text}"

def fmt_warning(text: str) -> str:
    return f"⚠️  {text}"

_RESTRICTED_MEDIA_ERROR_MARKERS = (
    "isn't available to everyone", "isn’t available to everyone", "certain audiences",
    "this account is private", "login required", "sign in", "log in",
    "age-restricted", "age restricted", "private video", "content isn't available",
)

def classify_media_error(error_str: str, platform: str | None = None, cookies_configured: bool = False) -> str:
    """Map a raw download error into a clean user-friendly message, without leaking raw stack traces.

    For LinkedIn login-wall blocks, when no LinkedIn cookies are configured,
    the message adds one line saying cookies may help.
    """
    low = error_str.lower()
    # LinkedIn login wall / datacenter block (HTTP 999). Checked first:
    # the restricted markers below also catch "login required"/"sign in",
    # but for LinkedIn the cause is a block, not missing content.
    if "999" in low or ("linkedin" in low and any(m in low for m in ("login", "sign in", "log in", "member"))):
        msg = "LinkedIn blocked this request (login wall / HTTP 999) — the post may need a LinkedIn login, or LinkedIn refused the server's request. Try a public post, or try again later."
        if ("linkedin" in low or (platform or "").lower() == "linkedin") and not cookies_configured:
            msg += " Adding LinkedIn cookies (LINKEDIN_COOKIES) may help — LinkedIn often requires a login for video."
        return msg
    if any(marker in low for marker in _RESTRICTED_MEDIA_ERROR_MARKERS):
        return "This content isn't available due to restrictions (private, age-restricted, or region-locked)."
    if "429" in low or "too many requests" in low:
        return "Instagram rate limit reached (HTTP 429). The request was throttled. Please try again shortly or configure a proxy."
    if "empty media response" in low:
        return "Failed to download this media. The content may be restricted or requires an active login."
    if "timeout" in low or "timed out" in low:
        return "Download timed out. The server or platform took too long to respond."
    return "Failed to download this media. The link may be broken or unavailable."


async def send_media_error_dm(
    context,
    target_message,
    url: str,
    platform_name: str,
    raw_error: str,
) -> None:
    """Send detailed error diagnostics via DM so chat stays clean."""
    if not target_message or not raw_error or not context:
        return

    chat_id = target_message.chat_id
    is_private = getattr(target_message.chat, "type", "") == "private"
    requesting_user = getattr(target_message, "from_user", None)
    clean_err = str(raw_error).strip()[:800]

    dm_text = (
        f"⚠️ <b>{platform_name} Download Error Diagnostic</b>\n\n"
        f"🔗 <b>Link:</b> <code>{html.escape(url)}</code>\n"
        f"💬 <b>Chat:</b> {html.escape(getattr(target_message.chat, 'title', None) or 'Private Chat')} (<code>{chat_id}</code>)\n\n"
        f"📋 <b>Diagnostic Error:</b>\n"
        f"<code>{html.escape(clean_err)}</code>"
    )

    # 1. In groups, try sending DM to the requesting user
    if not is_private and requesting_user and not requesting_user.is_bot:
        try:
            await context.bot.send_message(
                chat_id=requesting_user.id,
                text=dm_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.debug(f"Could not DM download error to user {requesting_user.id}: {e}")

    # 2. Try sending DM to admin users
    for admin_id in ADMIN_USER_IDS:
        if not is_private and requesting_user and admin_id == requesting_user.id:
            continue
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=dm_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.debug(f"Could not DM download error to admin {admin_id}: {e}")

