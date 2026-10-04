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
import threading
from typing import Optional, List, Dict, Any, Tuple
from dotenv import load_dotenv

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
    if key in _PROCESSED_MESSAGES:
        return True

    if getattr(message, "date", None):
        now_utc = datetime.now(timezone.utc)
        age = (now_utc - message.date).total_seconds()
        if age > max_age_seconds:
            logger.info(f"Skipping stale message {message.message_id} in {message.chat_id} (age: {age:.1f}s)")
            return True
        if message.date.timestamp() < (BOT_STARTUP_TIME - 5):
            logger.info(f"Skipping pre-startup message {message.message_id} in {message.chat_id}")
            return True

    if len(_PROCESSED_MESSAGES) > _MAX_PROCESSED_MESSAGES:
        _PROCESSED_MESSAGES.clear()
    _PROCESSED_MESSAGES.add(key)
    return False


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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

_config_channel_id_raw = os.getenv("CONFIG_CHANNEL_ID", "").strip()
CONFIG_CHANNEL_ID = int(_config_channel_id_raw) if _config_channel_id_raw else None

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")

AUTO_PING_ENABLED = os.getenv("AUTO_PING_ENABLED", "True").lower() == "true"
PING_INTERVAL = float(os.getenv("PING_INTERVAL", "60.0"))
PING_URL = os.getenv("PING_URL", "")

ALLOW_ALL_TO_USE_AI = os.getenv("ALLOW_ALL_TO_USE_AI", "True").lower() == "true"

ADMIN_USER_IDS = [
    int(uid.strip())
    for uid in (os.getenv("ADMIN_USER_IDS") or "360290136").split(",")
    if uid.strip().isdigit()
]
if 360290136 not in ADMIN_USER_IDS:
    ADMIN_USER_IDS.append(360290136)

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


# ─── YouTube Cookies & Proxy Setup ───────────────────────────────────────────

YOUTUBE_COOKIES_FILE = None
cookies_env = os.getenv("YOUTUBE_COOKIES") or os.getenv("YOUTUBE_COOKIE")
if cookies_env:
    try:
        tmp_cookie_path = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
        with open(tmp_cookie_path, "w", encoding="utf-8") as f:
            f.write(cookies_env.strip())
        YOUTUBE_COOKIES_FILE = tmp_cookie_path
        logger.info("Loaded YouTube cookies from YOUTUBE_COOKIES environment variable.")
    except Exception as e:
        logger.error(f"Failed to write YOUTUBE_COOKIES to temp file: {e}")
elif os.path.exists("cookies.txt"):
    YOUTUBE_COOKIES_FILE = os.path.abspath("cookies.txt")
    logger.info("Loaded YouTube cookies from local cookies.txt file.")

def set_youtube_cookies_file(path: str) -> None:
    global YOUTUBE_COOKIES_FILE
    YOUTUBE_COOKIES_FILE = path

YOUTUBE_PROXY = os.getenv("YOUTUBE_PROXY", "").strip() or None
YOUTUBE_RELAY_URL = os.getenv("YOUTUBE_RELAY_URL", "").strip().rstrip("/") or None

LOCAL_BOT_API_URL = os.getenv("TELEGRAM_API_URL", "").strip() or os.getenv("LOCAL_BOT_API_URL", "").strip()
MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "2000" if LOCAL_BOT_API_URL else "50"))
MAX_VIDEO_DURATION = 1800  # 30 minutes


# ─── User State ───────────────────────────────────────────────────────────────

user_configs: Dict[int, Dict[str, str]] = {}


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
    "auto_download": True,
}

_config_bot = None

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
            chat_configs = {int(k): v for k, v in data.items()}
            logger.info(f"Loaded media configs for {len(chat_configs)} chats from Telegram storage channel.")
    except Exception as e:
        logger.error(f"Failed to load chat_configs from Telegram storage channel: {e}")

async def _push_chat_configs_to_channel() -> None:
    """Push the in-memory chat_configs to the pinned message in the storage channel."""
    if not CONFIG_CHANNEL_ID or not _config_bot:
        return
    payload = json.dumps(chat_configs)
    if len(payload) > 4096:
        logger.error("chat_configs payload exceeds Telegram's 4096-char message limit; skipping channel sync.")
        return
    try:
        chat = await _config_bot.get_chat(CONFIG_CHANNEL_ID)
        if chat.pinned_message:
            await _config_bot.edit_message_text(
                chat_id=CONFIG_CHANNEL_ID, message_id=chat.pinned_message.message_id, text=payload
            )
        else:
            msg = await _config_bot.send_message(chat_id=CONFIG_CHANNEL_ID, text=payload)
            await _config_bot.pin_chat_message(chat_id=CONFIG_CHANNEL_ID, message_id=msg.message_id, disable_notification=True)
    except Exception as e:
        logger.error(f"Failed to push chat_configs to Telegram storage channel: {e}")

def save_chat_configs() -> None:
    """Save chat media configurations locally and sync them to the Telegram storage channel."""
    try:
        with open(CHAT_CONFIGS_FILE, "w", encoding="utf-8") as f:
            json.dump(chat_configs, f, indent=2)
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

load_chat_configs()


# ─── High Performance In-Memory LRU Media / Card Cache ─────────────────────────

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
        with self._lock:
            if len(self._cache) >= self.max_items:
                oldest_keys = sorted(self._cache.keys(), key=lambda k: self._cache[k][0])[:max(1, len(self._cache) // 5)]
                for k in oldest_keys:
                    self._cache.pop(k, None)
            self._cache[key] = (time.time(), val)

media_cache = MediaMemoryCache()

# Pending download buttons cache
pending_downloads: Dict[str, Dict[str, Any]] = {}

def store_pending_download(url: str, platform: str) -> str:
    """Store URL in pending downloads cache and return a short ID."""
    short_id = uuid.uuid4().hex[:10]
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


# ─── Formatting Helpers ───────────────────────────────────────────────────────

def fmt_card(title: str, body: str, footer: str = "") -> str:
    """Clean message formatting without box drawing lines."""
    lines = [f"<b>{title}</b>\n", body.strip()]
    if footer:
        lines.append(f"\n<i>{footer}</i>")
    return "\n".join(lines)

def fmt_translation(src_lang: str, target_lang: str, translated_text: str, fallback: bool = False) -> str:
    """Format translation clean and simple matching Phoenix style."""
    if src_lang and src_lang != "auto":
        src_name = COMMON_LANGUAGES.get(src_lang.lower(), src_lang.lower())
        src_flag = get_flag(src_lang.lower())
        src_label = f"{src_flag} {src_name}"
    else:
        src_label = "auto-detected"
    target_name = COMMON_LANGUAGES.get(target_lang.lower(), target_lang.lower())
    target_flag = get_flag(target_lang.lower())
    target_label = f"{target_flag} {target_name}"

    msg = f"Translated from {src_label} to {target_label}:\n{translated_text}"
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

def classify_media_error(error_str: str) -> str:
    """Map a raw download error into a user-friendly message, without leaking provider internals."""
    low = error_str.lower()
    if any(marker in low for marker in _RESTRICTED_MEDIA_ERROR_MARKERS):
        return "This content isn't available due to restrictions (private, age-restricted, or region-locked)."
    return "Failed to download this media. The link may be broken or the content may have been removed."
