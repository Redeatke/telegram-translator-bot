import os
import logging
import asyncio
import re
import html
import tempfile
import uuid
import time
import json
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest
import httpx
from deep_translator import GoogleTranslator
from openai import OpenAI
from langdetect import detect
import yt_dlp
from yt_dlp.extractor.instagram import InstagramBaseIE
import card

# yt-dlp's Instagram extractor only turns `video_versions` into downloadable
# formats, so photo-only carousel items have an empty format list and yt-dlp
# raises "No video formats found!" before we ever see them. Patch it to fall
# back to the best available image candidate when there's no video stream.
_orig_ig_extract_product_media = InstagramBaseIE._extract_product_media

def _ig_extract_product_media_with_photos(self, product_media):
    result = _orig_ig_extract_product_media(self, product_media)
    if not result.get('formats'):
        thumbnails = result.get('thumbnails') or []
        if thumbnails:
            best = thumbnails[-1]
            result['formats'] = [{
                'url': best['url'],
                'format_id': 'image',
                'ext': 'jpg',
                'width': best.get('width'),
                'height': best.get('height'),
            }]
    return result

InstagramBaseIE._extract_product_media = _ig_extract_product_media_with_photos


try:
    from pytubefix import YouTube as PytubeFixYouTube
    has_pytubefix = True
except ImportError:
    has_pytubefix = False

# Load environment variables
load_dotenv(override=True)

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)
# Suppress httpx INFO logs (auto-pinger generates excessive noise)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ─── API Configuration ───────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Private Telegram channel used to persist chat_configs.json across redeploys
# (the bot must be an admin there with Post + Pin permissions).
_config_channel_id_raw = os.getenv("CONFIG_CHANNEL_ID", "").strip()
CONFIG_CHANNEL_ID = int(_config_channel_id_raw) if _config_channel_id_raw else None

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")

# Auto Pinger configuration
AUTO_PING_ENABLED = os.getenv("AUTO_PING_ENABLED", "True").lower() == "true"
PING_INTERVAL = float(os.getenv("PING_INTERVAL", "60.0"))  # 60s default — sufficient to keep free-tier awake
PING_URL = os.getenv("PING_URL", "")

# Toggle to allow all users to use the AI engine (for testing or public deployment)
ALLOW_ALL_TO_USE_AI = os.getenv("ALLOW_ALL_TO_USE_AI", "True").lower() == "true"

# Whitelisted admin user IDs — set ADMIN_USER_IDS=123456,789012 in .env
ADMIN_USER_IDS = [
    int(uid.strip())
    for uid in os.getenv("ADMIN_USER_IDS", "").split(",")
    if uid.strip().isdigit()
]

# Maintenance mode toggle (set MAINTENANCE_MODE=True in .env or toggle via /maintenance)
MAINTENANCE_MODE = os.getenv("MAINTENANCE_MODE", "False").lower() == "true"

def is_maintenance_active_for_user(user_id: int) -> bool:
    """Return True if maintenance mode is enabled and user is not an admin."""
    return MAINTENANCE_MODE and (user_id not in ADMIN_USER_IDS)

MAINTENANCE_NOTICE = (
    "🚧 <b>System Maintenance / Update in Progress</b>\n\n"
    "<i>We're actively deploying updates and patching new features. The bot will be fully back online shortly!</i>"
)

# Initialize OpenRouter client
has_ai = False
ai_client = None
if OPENROUTER_API_KEY:
    try:
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

# ─── YouTube Cookies Setup ────────────────────────────────────────────────────

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

# Log yt-dlp version
logger.info(f"yt-dlp version: {yt_dlp.version.__version__}")

# ─── User State ───────────────────────────────────────────────────────────────

# In-memory user configs: { user_id: { "engine": "free" | "ai", "target": "en" } }
user_configs = {}

# ─── Chat Media Config Persistence ──────────────────────────────────────────

CHAT_CONFIGS_FILE = os.path.join(os.environ.get("DATA_DIR", os.path.dirname(__file__)), "chat_configs.json")
chat_configs = {}

DEFAULT_CHAT_CONFIG = {
    "youtube": True,
    "twitter": True,
    "twitch": True,
    "tiktok": True,
    "instagram": True,
    "reddit": True,
    "auto_download": True,  # True = Auto-download; False = Prompt with button
}

_config_bot = None  # set in post_init() once the Application's Bot exists

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
        pass  # called outside a running event loop (e.g. local scripting); local cache above still saved

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

# Load chat configs on module startup
load_chat_configs()

# Cache for pending download buttons: { short_id: { "url": str, "platform": str, "time": float } }
pending_downloads = {}

def store_pending_download(url: str, platform: str) -> str:
    """Store URL in pending downloads cache and return a short ID."""
    short_id = uuid.uuid4().hex[:10]
    pending_downloads[short_id] = {"url": url, "platform": platform, "time": time.time()}
    now = time.time()
    for k in list(pending_downloads.keys()):
        if now - pending_downloads[k]["time"] > 7200:
            pending_downloads.pop(k, None)
    return short_id


DEFAULT_ENGINE = "free"
DEFAULT_TARGET_LANG = "en"

COMMON_LANGUAGES = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "ru": "Russian",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "ar": "Arabic",
    "hi": "Hindi",
    "tr": "Turkish",
}

# ─── Language flag emoji mapping ──────────────────────────────────────────────

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

# ─── YouTube URL Pattern ──────────────────────────────────────────────────────

YOUTUBE_URL_PATTERN = re.compile(
    r'(?:https?://)?'
    r'(?:www\.|m\.)?'
    r'(?:'
    r'youtube\.com/(?:shorts/|watch\?v=|embed/|live/|v/)'
    r'|youtu\.be/'
    r')'
    r'[\w\-]+',
    re.IGNORECASE
)

# ─── Twitter / X URL Pattern ──────────────────────────────────────────────────

TWITTER_URL_PATTERN = re.compile(
    r'(?:https?://)?(?:www\.|mobile\.)?(?:twitter\.com|x\.com)/([A-Za-z0-9_]+)/status/(\d+)',
    re.IGNORECASE
)

# ─── Twitch Clip URL Pattern ──────────────────────────────────────────────────

TWITCH_CLIP_PATTERN = re.compile(
    r'(?:https?://)?(?:www\.|m\.)?(?:clips\.twitch\.tv/|twitch\.tv/[A-Za-z0-9_]+/clip/)([A-Za-z0-9_-]+)',
    re.IGNORECASE
)

# ─── TikTok URL Pattern ───────────────────────────────────────────────────────

TIKTOK_URL_PATTERN = re.compile(
    r'(?:https?://)?(?:www\.|vm\.|vt\.)?tiktok\.com/(?:@[A-Za-z0-9_.]+/video/|v/|t/)?([A-Za-z0-9_]+)',
    re.IGNORECASE
)

# ─── Instagram URL Pattern ────────────────────────────────────────────────────

INSTAGRAM_URL_PATTERN = re.compile(
    r'(?:https?://)?(?:www\.)?instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)',
    re.IGNORECASE
)

# ─── Reddit URL Pattern ───────────────────────────────────────────────────────

REDDIT_URL_PATTERN = re.compile(
    r'(?:https?://)?(?:www\.|old\.|m\.)?reddit\.com/(?:r/[A-Za-z0-9_]+/(?:comments|s)/|s/)[A-Za-z0-9_-]+|(?:https?://)?v\.redd\.it/[A-Za-z0-9_-]+',
    re.IGNORECASE
)




# Maximum video duration in seconds (30 minutes)
MAX_VIDEO_DURATION = 1800


def get_flag(lang_code: str) -> str:
    """Get flag emoji for a language code, fallback to globe."""
    return LANG_FLAGS.get(lang_code, "🌐")


def get_user_config(user_id: int) -> dict:
    """Retrieve or initialize configuration for a user."""
    if user_id not in user_configs:
        user_configs[user_id] = {
            "engine": DEFAULT_ENGINE,
            "target": DEFAULT_TARGET_LANG,
        }
    return user_configs[user_id]


def is_user_premium_or_admin(update: Update) -> bool:
    """Check if the user is a Telegram Premium subscriber or admin/whitelist."""
    user = update.effective_user
    if not user:
        return False
    if user.is_premium:
        return True
    if user.id in ADMIN_USER_IDS:
        return True
    return False


def detect_language_code(text: str) -> str:
    """Detect the language code of the text, falling back to 'auto' on failure."""
    try:
        lang = detect(text)
        return lang.lower()
    except Exception:
        return "auto"


# ─── Message Formatting Helpers ───────────────────────────────────────────────

def fmt_card(title: str, body: str, footer: str = "") -> str:
    """Clean message formatting without box drawing lines."""
    lines = [f"<b>{title}</b>\n", body.strip()]
    if footer:
        lines.append(f"\n<i>{footer}</i>")
    return "\n".join(lines)


def fmt_translation(src_lang: str, target_lang: str, translated_text: str, fallback: bool = False) -> str:
    """Format translation clean and simple matching Phoenix style."""
    src_label = src_lang.lower() if src_lang != "auto" else "??"
    target_label = target_lang.lower()

    msg = f"Translated from {src_label} to {target_label}:\n{translated_text}"
    if fallback:
        msg += "\n\n⚠️ (Fell back to free engine)"
    return msg


def fmt_success(text: str) -> str:
    """Format a success message."""
    return f"✅  {text}"


def fmt_error(text: str) -> str:
    """Format an error message."""
    return f"❌  {text}"


def fmt_warning(text: str) -> str:
    """Format a warning message."""
    return f"⚠️  {text}"


# ─── Translation Logic ────────────────────────────────────────────────────────

async def translate_free(text: str, target_lang: str) -> str:
    """Translate text using deep-translator (Google Translate free backend)."""
    loop = asyncio.get_running_loop()
    try:
        translated = await loop.run_in_executor(
            None,
            lambda: GoogleTranslator(source="auto", target=target_lang).translate(text)
        )
        return translated
    except Exception as e:
        logger.error(f"Free Translation Error: {e}")
        raise e


async def translate_ai(text: str, target_lang: str) -> str:
    """Translate text using OpenRouter AI."""
    if not has_ai or not ai_client:
        raise ValueError("OpenRouter API key is not configured.")

    lang_name = COMMON_LANGUAGES.get(target_lang, target_lang.upper())
    prompt = (
        f"You are a professional translator. Translate the following text into {lang_name} "
        f"(ISO code: '{target_lang}'). Return ONLY the direct translation — no explanations, "
        f"no formatting notes, no introductory text.\n\n"
        f"Text to translate:\n{text}"
    )

    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: ai_client.chat.completions.create(
                model=OPENROUTER_MODEL,
                messages=[
                    {"role": "system", "content": "You are a professional translation engine. Output only the translated text, nothing else."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=4096,
            )
        )
        translation = response.choices[0].message.content.strip()
        if not translation:
            raise ValueError("Empty response received from AI.")
        return translation
    except Exception as e:
        logger.error(f"AI Translation Error: {e}")
        raise e


# ─── Bot Command Handlers ────────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a welcoming message when /start is issued."""
    user = update.effective_user
    first_name = user.first_name if user else "there"
    config = get_user_config(user.id if user else 0)

    target_name = COMMON_LANGUAGES.get(config["target"], config["target"].upper())
    engine_name = "AI (OpenRouter)" if config["engine"] == "ai" else "Free (Google Translate)"
    target_flag = get_flag(config["target"])

    body = (
        f"👋 Welcome, <b>{first_name}</b>!\n"
        f"\n"
        f"I'm your personal translator. Send me any\n"
        f"text and I'll translate it instantly.\n"
        f"\n"
        f"─── ⚙️ Your Settings ───\n"
        f"\n"
        f"  {target_flag}  Language    <b>{target_name}</b>\n"
        f"  ⚡  Engine      <b>{engine_name}</b>\n"
        f"\n"
        f"─── 🚀 Quick Start ───\n"
        f"\n"
        f"  • Send any text to translate it\n"
        f"  • /tr — translate in groups\n"
        f"  • /target es — switch to Spanish\n"
        f"  • /engine — toggle AI engine\n"
        f"  • /help — full command list"
    )

    await update.message.reply_text(
        fmt_card("🤖 Translation Bot", body),
        parse_mode="HTML"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help with all commands and language codes."""
    lang_lines = "\n".join(
        [f"  {get_flag(code)}  <code>{code}</code>  {name}" for code, name in COMMON_LANGUAGES.items()]
    )

    body = (
        f"─── 🤖 Commands ───\n"
        f"\n"
        f"  /start     Start the bot\n"
        f"  /help      This help page\n"
        f"  /tr        Translate text or reply\n"
        f"  /target    Set target language\n"
        f"  /engine    Switch AI / Free engine\n"
        f"  /status    View your settings\n"
        f"  /report    Report a bug\n"
        f"\n"
        f"─── 👮 Admin Only ───\n"
        f"\n"
        f"  /ban       Ban a user\n"
        f"  /promote   Promote to admin\n"
        f"  /demote    Demote an admin\n"
        f"\n"
        f"─── 🌍 Languages ───\n"
        f"\n"
        f"{lang_lines}\n"
        f"\n"
        f"Use any ISO 639-1 code with /target"
    )

    await update.message.reply_text(
        fmt_card("📖 Help", body),
        parse_mode="HTML"
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display user config and premium status."""
    user = update.effective_user
    if not user:
        return

    config = get_user_config(user.id)
    is_premium = user.is_premium or False
    engine_name = "AI (OpenRouter)" if config["engine"] == "ai" else "Free (Google Translate)"
    target_name = COMMON_LANGUAGES.get(config["target"], config["target"].upper())
    target_flag = get_flag(config["target"])

    premium_badge = "⭐ Yes" if is_premium else "No"

    pinger_status = f"Active ({PING_INTERVAL:.1f}s)" if AUTO_PING_ENABLED else "Disabled"

    body = (
        f"  👤  User ID       <code>{user.id}</code>\n"
        f"  💎  Premium       {premium_badge}\n"
        f"  {target_flag}  Language     <b>{target_name}</b> (<code>{config['target']}</code>)\n"
        f"  ⚡  Engine        <b>{engine_name}</b>\n"
        f"  🔔  Auto Pinger   <b>{pinger_status}</b>\n"
        f"\n"
        f"AI access: <i>{'open to all' if ALLOW_ALL_TO_USE_AI else 'Premium & Admins only'}</i>"
    )

    await update.message.reply_text(
        fmt_card("📊 Status", body),
        parse_mode="HTML"
    )


async def target_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Change the user's translation target language."""
    user = update.effective_user
    if not user:
        return

    config = get_user_config(user.id)

    if not context.args:
        current_target = config["target"]
        current_name = COMMON_LANGUAGES.get(current_target, current_target.upper())
        current_flag = get_flag(current_target)
        await update.message.reply_text(
            fmt_card("🌐 Target Language",
                f"  Current: {current_flag} <b>{current_name}</b> (<code>{current_target}</code>)\n"
                f"\n"
                f"  To change: <code>/target es</code>\n"
                f"  See codes: /help"
            ),
            parse_mode="HTML"
        )
        return

    new_target = context.args[0].lower().strip()

    try:
        GoogleTranslator(source="auto", target=new_target)
        config["target"] = new_target
        target_name = COMMON_LANGUAGES.get(new_target, new_target.upper())
        new_flag = get_flag(new_target)
        await update.message.reply_text(
            fmt_success(f"Target language set to {new_flag} <b>{target_name}</b> (<code>{new_target}</code>)"),
            parse_mode="HTML"
        )
    except Exception:
        await update.message.reply_text(
            fmt_error(f"Unknown language code: <code>{new_target}</code>\nUse /help to see valid codes."),
            parse_mode="HTML"
        )


async def engine_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle between AI and Free translation engines."""
    user = update.effective_user
    if not user:
        return

    config = get_user_config(user.id)
    current_engine = config["engine"]

    eligible = ALLOW_ALL_TO_USE_AI or is_user_premium_or_admin(update)

    if current_engine == "free":
        # Trying to switch to AI
        if not has_ai:
            await update.message.reply_text(
                fmt_warning("AI engine is unavailable — no API key configured."),
                parse_mode="HTML"
            )
            return

        if not eligible:
            body = (
                f"  The AI engine is a premium feature.\n"
                f"\n"
                f"  ⭐ Get Telegram Premium or contact\n"
                f"  the bot admin for access."
            )
            await update.message.reply_text(
                fmt_card("🔒 Premium Required", body),
                parse_mode="HTML"
            )
            return

        config["engine"] = "ai"
        await update.message.reply_text(
            fmt_card("⚡ Engine Switched",
                f"  Now using: <b>AI (OpenRouter)</b>\n"
                f"  Model: <code>{OPENROUTER_MODEL}</code>\n"
                f"\n"
                f"  Enjoy context-aware translations!"
            ),
            parse_mode="HTML"
        )
    else:
        config["engine"] = "free"
        await update.message.reply_text(
            fmt_card("🔄 Engine Switched",
                f"  Now using: <b>Free (Google Translate)</b>\n"
                f"\n"
                f"  Use /engine to switch back to AI."
            ),
            parse_mode="HTML"
        )


# ─── Group Moderation Commands ────────────────────────────────────────────────

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ban a user from the group. Usage: Reply to a message with /ban or run /ban <user_id>."""
    chat = update.effective_chat
    caller = update.effective_user

    if not chat or chat.type not in ["group", "supergroup"]:
        await update.message.reply_text(fmt_error("This command can only be used in groups."), parse_mode="HTML")
        return

    if not caller:
        return

    # 1. Verify caller is admin
    try:
        caller_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=caller.id)
        if caller_member.status not in ["administrator", "creator"]:
            await update.message.reply_text(fmt_error("You must be a group admin to use this."), parse_mode="HTML")
            return
    except Exception as e:
        logger.error(f"Error checking caller admin status: {e}")
        await update.message.reply_text(fmt_error("Failed to verify your admin privileges."), parse_mode="HTML")
        return

    # 2. Get target user
    target_user_id = None
    target_user_name = None

    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        if target_user:
            target_user_id = target_user.id
            target_user_name = target_user.first_name
    elif context.args:
        arg = context.args[0]
        if arg.isdigit():
            target_user_id = int(arg)
        elif arg.startswith("@"):
            await update.message.reply_text(
                fmt_error("Can't resolve usernames. Reply to their message or use a numeric ID."),
                parse_mode="HTML"
            )
            return

    if not target_user_id:
        await update.message.reply_text(
            fmt_warning("Reply to a message with /ban or use <code>/ban &lt;user_id&gt;</code>"),
            parse_mode="HTML"
        )
        return

    # 3. Prevent self-ban
    if target_user_id == caller.id:
        await update.message.reply_text(fmt_error("You cannot ban yourself."), parse_mode="HTML")
        return

    # 4. Check if target is admin/creator
    try:
        target_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=target_user_id)
        if target_member.status in ["administrator", "creator"]:
            await update.message.reply_text(fmt_error("Cannot ban administrators or owners."), parse_mode="HTML")
            return
    except Exception as e:
        logger.error(f"Error checking target member status: {e}")

    # 5. Check bot permissions
    try:
        bot_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=context.bot.id)
        if bot_member.status not in ["administrator", "creator"] or not bot_member.can_restrict_members:
            await update.message.reply_text(
                fmt_error("I need <b>Restrict Members</b> permission to ban users."),
                parse_mode="HTML"
            )
            return
    except Exception as e:
        logger.error(f"Error checking bot status: {e}")
        await update.message.reply_text(fmt_error("Failed to verify my permissions."), parse_mode="HTML")
        return

    # 6. Perform ban
    try:
        await context.bot.ban_chat_member(chat_id=chat.id, user_id=target_user_id)
        name_str = f"<b>{target_user_name}</b> (<code>{target_user_id}</code>)" if target_user_name else f"<code>{target_user_id}</code>"
        await update.message.reply_text(
            fmt_success(f"{name_str} has been banned from the group."),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Failed to ban user {target_user_id}: {e}")
        await update.message.reply_text(
            fmt_error(f"Failed to ban user: <code>{str(e)}</code>"),
            parse_mode="HTML"
        )


async def promote_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Promote a user to group administrator."""
    chat = update.effective_chat
    caller = update.effective_user

    if not chat or chat.type not in ["group", "supergroup"]:
        await update.message.reply_text(fmt_error("This command can only be used in groups."), parse_mode="HTML")
        return

    if not caller:
        return

    # 1. Verify caller is admin
    try:
        caller_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=caller.id)
        if caller_member.status not in ["administrator", "creator"]:
            await update.message.reply_text(fmt_error("You must be a group admin to use this."), parse_mode="HTML")
            return
    except Exception as e:
        logger.error(f"Error checking caller admin status: {e}")
        await update.message.reply_text(fmt_error("Failed to verify your admin privileges."), parse_mode="HTML")
        return

    # 2. Get target user
    target_user_id = None
    target_user_name = None

    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        if target_user:
            target_user_id = target_user.id
            target_user_name = target_user.first_name
    elif context.args:
        arg = context.args[0]
        if arg.isdigit():
            target_user_id = int(arg)
        elif arg.startswith("@"):
            await update.message.reply_text(
                fmt_error("Can't resolve usernames. Reply to their message or use a numeric ID."),
                parse_mode="HTML"
            )
            return

    if not target_user_id:
        await update.message.reply_text(
            fmt_warning("Reply to a message with /promote or use <code>/promote &lt;user_id&gt;</code>"),
            parse_mode="HTML"
        )
        return

    # 3. Prevent self-promotion
    if target_user_id == caller.id:
        await update.message.reply_text(fmt_error("You are already an administrator."), parse_mode="HTML")
        return

    # 4. Check if already admin
    try:
        target_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=target_user_id)
        if target_member.status in ["administrator", "creator"]:
            await update.message.reply_text(fmt_error("This user is already an admin."), parse_mode="HTML")
            return
    except Exception as e:
        logger.error(f"Error checking target member status: {e}")

    # 5. Check bot permissions
    try:
        bot_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=context.bot.id)
        if bot_member.status not in ["administrator", "creator"] or not bot_member.can_promote_members:
            await update.message.reply_text(
                fmt_error("I need <b>Add New Admins</b> permission to promote users."),
                parse_mode="HTML"
            )
            return
    except Exception as e:
        logger.error(f"Error checking bot status: {e}")
        await update.message.reply_text(fmt_error("Failed to verify my permissions."), parse_mode="HTML")
        return

    # 6. Perform promotion
    try:
        await context.bot.promote_chat_member(
            chat_id=chat.id,
            user_id=target_user_id,
            can_change_info=False,
            can_post_messages=True,
            can_edit_messages=True,
            can_delete_messages=True,
            can_invite_users=True,
            can_restrict_members=True,
            can_pin_messages=True,
            can_promote_members=False
        )
        name_str = f"<b>{target_user_name}</b> (<code>{target_user_id}</code>)" if target_user_name else f"<code>{target_user_id}</code>"
        await update.message.reply_text(
            fmt_success(f"{name_str} has been promoted to Admin."),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Failed to promote user {target_user_id}: {e}")
        await update.message.reply_text(
            fmt_error(f"Failed to promote user: <code>{str(e)}</code>"),
            parse_mode="HTML"
        )


async def demote_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Demote an administrator to a regular member."""
    chat = update.effective_chat
    caller = update.effective_user

    if not chat or chat.type not in ["group", "supergroup"]:
        await update.message.reply_text(fmt_error("This command can only be used in groups."), parse_mode="HTML")
        return

    if not caller:
        return

    # 1. Verify caller is admin
    try:
        caller_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=caller.id)
        if caller_member.status not in ["administrator", "creator"]:
            await update.message.reply_text(fmt_error("You must be a group admin to use this."), parse_mode="HTML")
            return
    except Exception as e:
        logger.error(f"Error checking caller admin status: {e}")
        await update.message.reply_text(fmt_error("Failed to verify your admin privileges."), parse_mode="HTML")
        return

    # 2. Get target user
    target_user_id = None
    target_user_name = None

    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        if target_user:
            target_user_id = target_user.id
            target_user_name = target_user.first_name
    elif context.args:
        arg = context.args[0]
        if arg.isdigit():
            target_user_id = int(arg)
        elif arg.startswith("@"):
            await update.message.reply_text(
                fmt_error("Can't resolve usernames. Reply to their message or use a numeric ID."),
                parse_mode="HTML"
            )
            return

    if not target_user_id:
        await update.message.reply_text(
            fmt_warning("Reply to a message with /demote or use <code>/demote &lt;user_id&gt;</code>"),
            parse_mode="HTML"
        )
        return

    # 3. Prevent self-demotion
    if target_user_id == caller.id:
        await update.message.reply_text(fmt_error("You cannot demote yourself."), parse_mode="HTML")
        return

    # 4. Check target user status
    try:
        target_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=target_user_id)
        if target_member.status == "creator":
            await update.message.reply_text(fmt_error("Cannot demote the group owner."), parse_mode="HTML")
            return
        if target_member.status not in ["administrator"]:
            await update.message.reply_text(fmt_error("This user is not an administrator."), parse_mode="HTML")
            return
    except Exception as e:
        logger.error(f"Error checking target member status: {e}")

    # 5. Check bot permissions
    try:
        bot_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=context.bot.id)
        if bot_member.status not in ["administrator", "creator"] or not bot_member.can_promote_members:
            await update.message.reply_text(
                fmt_error("I need <b>Add New Admins</b> permission to demote users."),
                parse_mode="HTML"
            )
            return
    except Exception as e:
        logger.error(f"Error checking bot status: {e}")
        await update.message.reply_text(fmt_error("Failed to verify my permissions."), parse_mode="HTML")
        return

    # 6. Perform demotion
    try:
        await context.bot.promote_chat_member(
            chat_id=chat.id,
            user_id=target_user_id,
            can_change_info=False,
            can_post_messages=False,
            can_edit_messages=False,
            can_delete_messages=False,
            can_invite_users=False,
            can_restrict_members=False,
            can_pin_messages=False,
            can_promote_members=False
        )
        name_str = f"<b>{target_user_name}</b> (<code>{target_user_id}</code>)" if target_user_name else f"<code>{target_user_id}</code>"
        await update.message.reply_text(
            fmt_success(f"{name_str} has been demoted to regular member."),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Failed to demote user {target_user_id}: {e}")
        await update.message.reply_text(
            fmt_error(f"Failed to demote user: <code>{str(e)}</code>"),
            parse_mode="HTML"
        )


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a problem report to the bot administrators."""
    user = update.effective_user
    if not user:
        return

    if not context.args:
        await update.message.reply_text(
            fmt_card("📨 Report a Problem",
                f"  Type /report followed by a description.\n"
                f"\n"
                f"  <i>Example:</i>\n"
                f"  <code>/report Spanish translations show</code>\n"
                f"  <code>weird characters</code>"
            ),
            parse_mode="HTML"
        )
        return

    report_text = html.escape(" ".join(context.args))

    if not ADMIN_USER_IDS:
        await update.message.reply_text(
            fmt_error("No administrators configured to receive reports."),
            parse_mode="HTML"
        )
        return

    admin_msg = fmt_card("⚠️ Bug Report",
        f"  <b>From:</b> {user.first_name} (@{user.username if user.username else 'N/A'})\n"
        f"  <b>User ID:</b> <code>{user.id}</code>\n"
        f"\n"
        f"  <b>Message:</b>\n"
        f"  <i>{report_text}</i>"
    )

    sent_count = 0
    for admin_id in ADMIN_USER_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=admin_msg, parse_mode="HTML")
            sent_count += 1
        except Exception as e:
            logger.error(f"Failed to forward report to admin {admin_id}: {e}")

    if sent_count > 0:
        await update.message.reply_text(
            fmt_success("Your report has been sent to the administrators. Thank you!"),
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            fmt_error("Couldn't reach administrators right now. Try again later."),
            parse_mode="HTML"
        )


# ─── /setcookies Command ─────────────────────────────────────────────────────

async def setcookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Update YouTube cookies by sending a cookies.txt file or pasting content."""
    user = update.effective_user
    if not user:
        return

    # Only allow in private chat for security
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            fmt_error("This command can only be used in private chat for security."),
            parse_mode="HTML"
        )
        return

    global YOUTUBE_COOKIES_FILE
    cookie_content = None

    # Option 1: Direct document attached to the /setcookies command or reply to document
    doc = update.message.document or (update.message.reply_to_message.document if update.message.reply_to_message else None)
    if doc:
        # If user uploaded a document directly without /setcookies command/caption, ignore non-txt files
        is_explicit_cmd = (update.message.text and update.message.text.startswith("/setcookies")) or \
                           (update.message.caption and "/setcookies" in update.message.caption)
        if not is_explicit_cmd and doc.file_name and not doc.file_name.lower().endswith(".txt"):
            return

        try:
            file = await context.bot.get_file(doc.file_id)
            raw = await file.download_as_bytearray()
            cookie_content = raw.decode("utf-8")
        except Exception as e:
            await update.message.reply_text(
                fmt_error(f"Failed to read file: {e}"),
                parse_mode="HTML"
            )
            return

    # Option 2: Text arguments (for small cookie sets)
    elif context.args:
        cookie_content = " ".join(context.args)

    if not cookie_content:
        await update.message.reply_text(
            fmt_card("🍪 Update YouTube Cookies",
                "Send your <code>cookies.txt</code> file to this chat,\n"
                "then reply to it with /setcookies\n"
                "\n"
                "Or paste cookie content directly:\n"
                "<code>/setcookies &lt;paste here&gt;</code>\n"
                "\n"
                "<i>Use the 'Get cookies.txt LOCALLY' browser\n"
                "extension, or run refresh_cookies.py locally.</i>"
            ),
            parse_mode="HTML"
        )
        return

    # Save cookies to temp file
    try:
        tmp_cookie_path = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
        with open(tmp_cookie_path, "w", encoding="utf-8") as f:
            f.write(cookie_content.strip())
        YOUTUBE_COOKIES_FILE = tmp_cookie_path

        cookie_lines = [l for l in cookie_content.strip().split('\n') if l.strip() and not l.startswith('#')]
        cookie_count = len(cookie_lines)

        await update.message.reply_text(
            fmt_success(
                f"YouTube cookies updated! ({cookie_count} cookies loaded)\n\n"
                "These will be used for future YouTube downloads."
            ),
            parse_mode="HTML"
        )
        logger.info(f"YouTube cookies updated via /setcookies by user {user.id} ({cookie_count} cookies)")
    except Exception as e:
        logger.error(f"Failed to update cookies via /setcookies: {e}")
        await update.message.reply_text(
            fmt_error(f"Failed to save cookies: {e}"),
            parse_mode="HTML"
        )


# ─── /maintenance Command ───────────────────────────────────────────────────

async def maintenance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to toggle maintenance mode."""
    user = update.effective_user
    if not user or user.id not in ADMIN_USER_IDS:
        return

    global MAINTENANCE_MODE
    if context.args and context.args[0].lower() in ["on", "true", "enable", "1"]:
        MAINTENANCE_MODE = True
    elif context.args and context.args[0].lower() in ["off", "false", "disable", "0"]:
        MAINTENANCE_MODE = False
    else:
        MAINTENANCE_MODE = not MAINTENANCE_MODE

    status_str = "ENABLED (Users will see update/patching notice)" if MAINTENANCE_MODE else "DISABLED (Bot is live)"
    await update.message.reply_text(
        fmt_card("🛠️ Maintenance Mode", f"Maintenance mode is now: <b>{status_str}</b>"),
        parse_mode="HTML"
    )
    logger.info(f"Maintenance mode set to {MAINTENANCE_MODE} by admin {user.id}")


# ─── Global Error Handler ────────────────────────────────────────────────────

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch unhandled exceptions and reassure users with an update/patching notice."""
    logger.error("Unhandled exception occurred:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "🛠️ <i>We're currently updating, patching, or working on a fix for this feature. Please try again shortly!</i>",
                parse_mode="HTML"
            )
        except Exception:
            pass


# ─── /tr Command ──────────────────────────────────────────────────────────────

async def tr_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Translate text from a command argument or a replied-to message."""
    user = update.effective_user
    if not user:
        return

    text_to_translate = None
    config = get_user_config(user.id)
    target_lang = config["target"]

    # Scenario A: Reply to a message
    if update.message.reply_to_message and update.message.reply_to_message.text:
        text_to_translate = update.message.reply_to_message.text
        if context.args:
            lang_candidate = context.args[0].lower().strip()
            try:
                GoogleTranslator(source="auto", target=lang_candidate)
                target_lang = lang_candidate
            except Exception:
                pass

    # Scenario B: Arguments provided
    elif context.args:
        if len(context.args) >= 2:
            lang_candidate = context.args[0].lower().strip()
            try:
                GoogleTranslator(source="auto", target=lang_candidate)
                target_lang = lang_candidate
                text_to_translate = " ".join(context.args[1:])
            except Exception:
                text_to_translate = " ".join(context.args)
        else:
            text_to_translate = " ".join(context.args)

    if not text_to_translate:
        await update.message.reply_text(
            fmt_card("🌐 /tr — Translate",
                f"  <b>Reply mode:</b>\n"
                f"  Reply to a message with /tr\n"
                f"  or <code>/tr es</code> for a specific language\n"
                f"\n"
                f"  <b>Inline mode:</b>\n"
                f"  <code>/tr es hello world</code>\n"
                f"  <code>/tr hello world</code>"
            ),
            parse_mode="HTML"
        )
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    engine = config["engine"]
    translated_text = None
    fallback = False

    src_lang = detect_language_code(text_to_translate)

    # 1. Try AI engine
    if engine == "ai" and has_ai:
        try:
            translated_text = await translate_ai(text_to_translate, target_lang)
        except Exception as e:
            logger.error(f"AI translation failed: {e}. Falling back to free engine.")
            fallback = True

    # 2. Free engine (or fallback)
    if not translated_text:
        try:
            translated_text = await translate_free(text_to_translate, target_lang)
        except Exception:
            await update.message.reply_text(
                fmt_error("Translation failed. Please try again later."),
                parse_mode="HTML"
            )
            return

    response_msg = fmt_translation(src_lang, target_lang, translated_text, fallback=fallback)

    await update.message.reply_text(
        response_msg,
        parse_mode="HTML",
        reply_to_message_id=update.message.message_id
    )


# ─── YouTube Auto-Download ────────────────────────────────────────────────────

async def download_youtube_video(url: str, output_dir: str) -> dict:
    """Download a YouTube video using optimized yt-dlp player clients with pytubefix fallback. Returns dict with filepath, title, duration."""
    filename = f"{uuid.uuid4().hex}"
    output_template = os.path.join(output_dir, f"{filename}.%(ext)s")

    loop = asyncio.get_running_loop()

    def _download():
        logger.info(f"Downloading YouTube video: {url}...")

        # Multi-client strategy optimized for datacenter IPs and YouTube Shorts
        # Strategy 1: web, mweb with cookies and JS challenge solvers (deno, node)
        # Strategy 2: tv, android, ios fallback
        # Strategy 3: default auto-selection fallback
        # Multi-client strategy optimized for ultra-fast speed on datacenter IPs
        # Format string prioritizes 720p pre-merged MP4 streams (skips ffmpeg processing completely & shrinks upload size)
        fast_format = 'best[ext=mp4][height<=720]/bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]/best[height<=1080]/best'
        ydl_opts_list = [
            {
                'outtmpl': output_template,
                'merge_output_format': 'mp4',
                'format': fast_format,
                'extractor_args': {'youtube': {'player_client': ['web', 'mweb']}},
                'js_runtimes': {'deno': {}, 'node': {}},
                'concurrent_fragment_downloads': 4,
                'socket_timeout': 15,
                'retries': 3,
                'quiet': True,
                'nocheckcertificate': True,
            },
            {
                'outtmpl': output_template,
                'merge_output_format': 'mp4',
                'format': fast_format,
                'extractor_args': {'youtube': {'player_client': ['tv', 'android']}},
                'js_runtimes': {'deno': {}, 'node': {}},
                'concurrent_fragment_downloads': 4,
                'socket_timeout': 15,
                'retries': 3,
                'quiet': True,
            },
            {
                'outtmpl': output_template,
                'merge_output_format': 'mp4',
                'format': 'best[height<=720]/best',
                'socket_timeout': 15,
                'retries': 3,
                'quiet': True,
            },
        ]

        if YOUTUBE_COOKIES_FILE and os.path.exists(YOUTUBE_COOKIES_FILE):
            logger.info(f"Using cookies file for web strategies: {YOUTUBE_COOKIES_FILE}")
            # Attach cookies to strategy 1 (web/mweb) and strategy 3 (default)
            ydl_opts_list[0]['cookiefile'] = YOUTUBE_COOKIES_FILE
            ydl_opts_list[2]['cookiefile'] = YOUTUBE_COOKIES_FILE

        for i, opts in enumerate(ydl_opts_list, 1):
            try:
                logger.info(f"Trying yt-dlp strategy {i} for {url}...")
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    filepath = ydl.prepare_filename(info)
                    if not os.path.exists(filepath):
                        base = os.path.splitext(filepath)[0]
                        for ext in ['.mp4', '.webm', '.mkv']:
                            if os.path.exists(base + ext):
                                filepath = base + ext
                                break
                    if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                        return {
                            'filepath': filepath,
                            'title': info.get('title', 'Video'),
                            'duration': info.get('duration', 0),
                        }
            except Exception as e:
                logger.warning(f"yt-dlp strategy {i} failed for {url}: {e}")
                if i < len(ydl_opts_list):
                    time.sleep(2)  # Brief delay between strategies to avoid rate limiting

        # Fallback to pytubefix if available
        if has_pytubefix:
            try:
                logger.info(f"Trying pytubefix fallback for {url}...")
                yt = PytubeFixYouTube(url)
                stream = yt.streams.filter(progressive=True, file_extension='mp4').get_highest_resolution()
                if not stream:
                    stream = yt.streams.filter(file_extension='mp4').first()
                if stream:
                    fp = stream.download(output_path=output_dir, filename=f"{filename}.mp4")
                    return {
                        'filepath': fp,
                        'title': yt.title or 'Video',
                        'duration': yt.length or 0,
                    }
            except Exception as e:
                logger.warning(f"pytubefix fallback failed for {url}: {e}")

        raise Exception("Failed to download YouTube video after trying all available engines.")

    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _download),
            timeout=300  # 5 minute hard cap on any single download
        )
    except asyncio.TimeoutError:
        raise Exception("YouTube download timed out after 5 minutes.")




async def execute_youtube_download(target_message, yt_url: str, context: ContextTypes.DEFAULT_TYPE, status_msg=None) -> None:
    """Execute download and upload for YouTube video."""
    if not status_msg:
        status_msg = await target_message.reply_text("⏳ Downloading YouTube video (720p)...")
    else:
        await status_msg.edit_text("⏳ Downloading YouTube video (720p)...")

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            result = await download_youtube_video(yt_url, tmp_dir)
            filepath = result['filepath']
            title = result['title']
            duration = result.get('duration', 0)

            # Check duration limit (30 mins = 1800s)
            if duration and duration > MAX_VIDEO_DURATION:
                mins = duration // 60
                secs = duration % 60
                await status_msg.edit_text(
                    fmt_warning(f"Video is too long ({mins}m {secs}s). Max allowed duration is 30 minutes.")
                )
                return

            # Check file size (Telegram limit: 50 MB for bots)
            file_size = os.path.getsize(filepath)
            if file_size > 50 * 1024 * 1024:
                mb_size = file_size / (1024 * 1024)
                await status_msg.edit_text(
                    fmt_warning(f"Video file is too large for Telegram ({mb_size:.1f} MB). Max limit is 50 MB.")
                )
                return

            await status_msg.edit_text("📤 Uploading to Telegram...")

            await context.bot.send_chat_action(
                chat_id=target_message.chat_id, action="upload_video"
            )

            with open(filepath, 'rb') as video_file:
                await target_message.reply_video(
                    video=video_file,
                    caption=f"📹 {title}",
                    supports_streaming=True,
                    read_timeout=120,
                    write_timeout=120,
                )

            try:
                await status_msg.delete()
            except Exception:
                pass

            logger.info(f"YouTube video sent successfully: {title}")

    except yt_dlp.utils.DownloadError as e:
        error_str = str(e)
        logger.error(f"YouTube DownloadError for {yt_url}: {error_str}")
        error_lower = error_str.lower()
        if 'is a live stream' in error_lower or 'live stream' in error_lower or 'is live' in error_lower:
            await status_msg.edit_text(fmt_warning("This is an active live stream and cannot be downloaded until it ends."))
        elif '429' in error_lower or 'too many requests' in error_lower:
            await status_msg.edit_text(fmt_warning("YouTube is temporarily rate-limiting requests. Please try again in a moment."))
        elif 'private' in error_lower or 'unavailable' in error_lower:
            await status_msg.edit_text(fmt_error("This video is private or unavailable."))
        elif 'age' in error_lower:
            await status_msg.edit_text(fmt_error("This video is age-restricted and cannot be downloaded."))
        else:
            await status_msg.edit_text(fmt_error("Couldn't download this video."))
    except Exception as e:
        logger.error(f"YouTube download failed for {yt_url}: {type(e).__name__}: {e}")
        try:
            await status_msg.edit_text(fmt_error("Something went wrong downloading this video."))
        except Exception:
            pass


async def handle_youtube_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-detect YouTube links in messages."""
    if not update.message or not update.message.text:
        return

    user = update.effective_user
    if user and is_maintenance_active_for_user(user.id):
        await update.message.reply_text(MAINTENANCE_NOTICE, parse_mode="HTML")
        return

    text = update.message.text.strip()
    match = YOUTUBE_URL_PATTERN.search(text)
    if not match:
        return

    chat_id = update.effective_chat.id
    if not is_downloader_enabled(chat_id, "youtube"):
        return

    yt_url = match.group(0)
    logger.info(f"YouTube URL detected: {yt_url}")

    # If it's a Short, download directly without button
    if "/shorts/" in yt_url.lower():
        await execute_youtube_download(update.message, yt_url, context)
        return

    # For standard videos & live streams, show Inline Download Button
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬇️ Download Video (720p)", callback_data=f"ytdl:{yt_url}")]
    ])

    await update.message.reply_text(
        f"📹 <b>YouTube Link Detected</b>\n"
        f"🔗 <code>{yt_url}</code>\n\n"
        f"<i>Click below to download (720p, max 30 min / 50MB):</i>",
        parse_mode="HTML",
        reply_markup=keyboard,
        reply_to_message_id=update.message.message_id
    )


async def handle_youtube_download_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 'Download Video' inline keyboard button presses."""
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data or not data.startswith("ytdl:"):
        return

    yt_url = data[5:]
    logger.info(f"User clicked YouTube download button for: {yt_url}")

    status_msg = await query.message.reply_text("⏳ Starting YouTube download...")
    await execute_youtube_download(query.message, yt_url, context, status_msg=status_msg)


# ─── Twitch Clip Auto-Download ────────────────────────────────────────────────

async def download_twitch_clip(url: str, output_dir: str) -> dict:
    """Download a Twitch clip using yt-dlp. Returns dict with filepath, title, duration, uploader."""
    filename = f"{uuid.uuid4().hex}"
    output_template = os.path.join(output_dir, f"{filename}.%(ext)s")

    loop = asyncio.get_running_loop()

    def _download():
        logger.info(f"Downloading Twitch clip: {url}...")
        ydl_opts = {
            'outtmpl': output_template,
            'format': 'best[ext=mp4]/best',
            'socket_timeout': 20,
            'retries': 3,
            'quiet': True,
            'nocheckcertificate': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)
            if not os.path.exists(filepath):
                base = os.path.splitext(filepath)[0]
                for ext in ['.mp4', '.mkv', '.webm']:
                    if os.path.exists(base + ext):
                        filepath = base + ext
                        break
            if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                return {
                    'filepath': filepath,
                    'title': info.get('title', 'Twitch Clip'),
                    'duration': info.get('duration', 0),
                    'uploader': info.get('uploader') or info.get('creator') or 'Twitch',
                }
        raise Exception("Failed to download Twitch clip.")

    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _download),
            timeout=180  # 3 minute cap for clips
        )
    except asyncio.TimeoutError:
        raise Exception("Twitch clip download timed out.")


async def handle_twitch_clip_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-detect Twitch clip links in messages and download/upload the clip."""
    if not update.message or not update.message.text:
        return

    user = update.effective_user
    if user and is_maintenance_active_for_user(user.id):
        await update.message.reply_text(MAINTENANCE_NOTICE, parse_mode="HTML")
        return

    text = update.message.text.strip()
    match = TWITCH_CLIP_PATTERN.search(text)
    if not match:
        return

    chat_id = update.effective_chat.id
    if not is_downloader_enabled(chat_id, "twitch"):
        return

    clip_url = match.group(0)
    logger.info(f"Twitch Clip URL detected: {clip_url}")

    auto_dl = get_download_mode(chat_id)
    if not auto_dl:
        short_id = store_pending_download(clip_url, "twitch")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ Download Twitch Clip", callback_data=f"dlmed:{short_id}")]])
        await update.message.reply_text(
            f"🎮 <b>Twitch Clip Detected</b>\n<code>{clip_url}</code>\n\n<i>Click below to download clip:</i>",
            parse_mode="HTML",
            reply_markup=kb,
            reply_to_message_id=update.message.message_id
        )
        return

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_video")
    except Exception:
        pass

    status_msg = await update.message.reply_text("⏳ Downloading Twitch clip...", reply_to_message_id=update.message.message_id)

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            clip_info = await download_twitch_clip(clip_url, tmp_dir)
            filepath = clip_info['filepath']
            title = clip_info.get('title', 'Twitch Clip')
            uploader = clip_info.get('uploader', 'Twitch')
            duration = int(clip_info.get('duration', 0))

            file_size = os.path.getsize(filepath)
            if file_size > 50 * 1024 * 1024:
                await status_msg.edit_text(
                    fmt_warning(f"This clip exceeds Telegram's 50MB bot upload limit ({file_size / (1024*1024):.1f}MB).")
                )
                return

            caption = (
                f"🎮 <b>{html.escape(title)}</b>\n"
                f"👤 <i>Channel: {html.escape(uploader)}</i>\n\n"
                f"🔗 <a href='{clip_url}'>Twitch Clip Link</a>"
            )

            await status_msg.edit_text("📤 Uploading clip to Telegram...")

            with open(filepath, "rb") as video_file:
                await update.message.reply_video(
                    video=video_file,
                    caption=caption,
                    parse_mode="HTML",
                    duration=duration,
                    supports_streaming=True,
                    reply_to_message_id=update.message.message_id
                )

            try:
                await status_msg.delete()
            except Exception:
                pass

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Twitch clip download failed for {clip_url}: {type(e).__name__}: {e}")
        error_lower = error_msg.lower()
        if 'no longer available' in error_lower or 'deleted' in error_lower or '404' in error_lower:
            await status_msg.edit_text(fmt_error("This Twitch clip is deleted or no longer available."))
        elif 'private' in error_lower or 'unavailable' in error_lower:
            await status_msg.edit_text(fmt_error("This clip is private or restricted."))
        else:
            await status_msg.edit_text(fmt_error(f"Failed to download Twitch clip: {html.escape(error_msg)}"))


# ─── Universal Generic Downloader (TikTok, Instagram, Reddit) ─────────────────

async def download_generic_media(url: str, output_dir: str, platform_name: str = "Media") -> dict:
    """Download video from TikTok, Instagram, Reddit, etc. using yt-dlp. Returns dict with filepath, title, duration, uploader."""
    filename = f"{uuid.uuid4().hex}"
    output_template = os.path.join(output_dir, f"{filename}.%(ext)s")

    loop = asyncio.get_running_loop()

    def _download():
        logger.info(f"Downloading {platform_name} video: {url}...")
        ydl_opts = {
            'outtmpl': output_template,
            'format': 'best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best',
            'merge_output_format': 'mp4',
            'socket_timeout': 20,
            'retries': 3,
            'quiet': True,
            'nocheckcertificate': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)
            if not os.path.exists(filepath):
                base = os.path.splitext(filepath)[0]
                for ext in ['.mp4', '.mkv', '.webm', '.jpg', '.png']:
                    if os.path.exists(base + ext):
                        filepath = base + ext
                        break
            if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                return {
                    'filepath': filepath,
                    'title': info.get('title') or f"{platform_name} Media",
                    'duration': info.get('duration', 0),
                    'uploader': info.get('uploader') or info.get('creator') or info.get('channel') or platform_name,
                }
        raise Exception(f"Failed to download {platform_name} media.")

    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _download),
            timeout=180
        )
    except asyncio.TimeoutError:
        raise Exception(f"{platform_name} download timed out.")


async def execute_generic_media_download(target_message, url: str, platform_name: str, context: ContextTypes.DEFAULT_TYPE, status_msg=None) -> None:
    """Execute download and upload for generic media (TikTok, Instagram, Reddit)."""
    if not status_msg:
        status_msg = await target_message.reply_text(f"⏳ Downloading {platform_name} media...", reply_to_message_id=target_message.message_id)
    else:
        try:
            await status_msg.edit_text(f"⏳ Downloading {platform_name} media...")
        except Exception:
            pass  # e.g. text is already identical to the current status message

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            info = await download_generic_media(url, tmp_dir, platform_name)
            filepath = info['filepath']
            title = info.get('title', f'{platform_name} Media')
            uploader = info.get('uploader', platform_name)
            duration = int(info.get('duration', 0))

            file_size = os.path.getsize(filepath)
            if file_size > 50 * 1024 * 1024:
                await status_msg.edit_text(
                    fmt_warning(f"This media exceeds Telegram's 50MB bot limit ({file_size / (1024*1024):.1f}MB).")
                )
                return

            caption = (
                f"<b>{html.escape(title)}</b>\n"
                f"👤 <i>Source: {html.escape(uploader)}</i>\n\n"
                f"🔗 <a href='{url}'>{platform_name} Link</a>"
            )

            await status_msg.edit_text("📤 Uploading to Telegram...")

            is_image = filepath.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))
            if is_image:
                with open(filepath, "rb") as photo_file:
                    await target_message.reply_photo(
                        photo=photo_file,
                        caption=caption,
                        parse_mode="HTML",
                        reply_to_message_id=target_message.message_id
                    )
            else:
                with open(filepath, "rb") as video_file:
                    await target_message.reply_video(
                        video=video_file,
                        caption=caption,
                        parse_mode="HTML",
                        duration=duration,
                        supports_streaming=True,
                        reply_to_message_id=target_message.message_id
                    )

            try:
                await status_msg.delete()
            except Exception:
                pass

    except Exception as e:
        error_msg = str(e)
        logger.error(f"{platform_name} download failed for {url}: {type(e).__name__}: {e}")
        try:
            await status_msg.edit_text(fmt_error(f"Failed to download {platform_name} media: {html.escape(error_msg)}"))
        except Exception:
            pass


async def execute_instagram_download(target_message, url: str, context: ContextTypes.DEFAULT_TYPE, status_msg=None) -> None:
    """Download and upload Instagram media, including photo-only and multi-item carousel posts."""
    if not status_msg:
        status_msg = await target_message.reply_text("⏳ Downloading Instagram media...", reply_to_message_id=target_message.message_id)
    else:
        try:
            await status_msg.edit_text("⏳ Downloading Instagram media...")
        except Exception:
            pass  # e.g. text is already identical to the current status message

    try:
        loop = asyncio.get_running_loop()

        def _extract():
            ydl_opts = {
                'format': 'best',
                'quiet': True,
                'no_warnings': True,
                'skip_download': True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                entries = info.get('entries') if info.get('_type') == 'playlist' else [info]
                items = []
                for e in entries:
                    if not e:
                        continue
                    item_url = e.get('url')
                    if not item_url:
                        formats = e.get('formats') or []
                        if formats:
                            item_url = formats[-1].get('url')
                    if not item_url:
                        continue
                    items.append({'url': item_url, 'is_video': e.get('ext') != 'jpg'})
                return {
                    'items': items,
                    'title': info.get('title') or 'Instagram Post',
                    'uploader': info.get('channel') or info.get('uploader') or 'Instagram',
                }

        data = await loop.run_in_executor(None, _extract)
        items = data['items']
        if not items:
            raise Exception("No media found in this post.")

        # A single video is handled by the generic yt-dlp downloader, which
        # merges best video+audio and enforces the 50MB size check properly.
        if len(items) == 1 and items[0]['is_video']:
            await execute_generic_media_download(target_message, url, "Instagram", context, status_msg=status_msg)
            return

        caption = (
            f"<b>{html.escape(data['title'])}</b>\n"
            f"👤 <i>Source: {html.escape(data['uploader'])}</i>\n\n"
            f"🔗 <a href='{url}'>Instagram Link</a>"
        )
        if len(caption) > 1024:
            caption = caption[:1020] + "…"

        await status_msg.edit_text("📤 Uploading to Telegram...")

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        media_items = []
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            for item in items[:10]:
                try:
                    resp = await client.get(item['url'], headers=headers)
                    if resp.status_code == 200 and len(resp.content) <= 50 * 1024 * 1024:
                        media_items.append({'bytes': resp.content, 'is_video': item['is_video']})
                except Exception as fetch_err:
                    logger.warning(f"Failed to fetch Instagram media item: {fetch_err}")

        if not media_items:
            raise Exception("Failed to download any media from this post.")

        if len(media_items) == 1:
            m = media_items[0]
            if m['is_video']:
                await target_message.reply_video(
                    video=m['bytes'], caption=caption, parse_mode="HTML",
                    supports_streaming=True, reply_to_message_id=target_message.message_id
                )
            else:
                await target_message.reply_photo(
                    photo=m['bytes'], caption=caption, parse_mode="HTML",
                    reply_to_message_id=target_message.message_id
                )
        else:
            media_group = []
            for i, m in enumerate(media_items):
                kwargs = {'caption': caption, 'parse_mode': 'HTML'} if i == 0 else {}
                if m['is_video']:
                    media_group.append(InputMediaVideo(media=m['bytes'], **kwargs))
                else:
                    media_group.append(InputMediaPhoto(media=m['bytes'], **kwargs))
            await target_message.reply_media_group(media=media_group, reply_to_message_id=target_message.message_id)

        try:
            await status_msg.delete()
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Instagram download failed for {url}: {type(e).__name__}: {e}")
        try:
            await status_msg.edit_text(fmt_error(f"Failed to download Instagram media: {html.escape(str(e))}"))
        except Exception:
            pass


async def handle_tiktok_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-detect TikTok links."""
    if not update.message or not update.message.text: return
    user = update.effective_user
    if user and is_maintenance_active_for_user(user.id):
        await update.message.reply_text(MAINTENANCE_NOTICE, parse_mode="HTML"); return

    text = update.message.text.strip()
    match = TIKTOK_URL_PATTERN.search(text)
    if not match: return
    url = match.group(0)

    chat_id = update.effective_chat.id
    if not is_downloader_enabled(chat_id, "tiktok"):
        return

    auto_dl = get_download_mode(chat_id)
    if not auto_dl:
        short_id = store_pending_download(url, "TikTok")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ Download TikTok Video", callback_data=f"dlmed:{short_id}")]])
        await update.message.reply_text("🎵 <b>TikTok Link Detected</b>\n<i>Click below to download:</i>", parse_mode="HTML", reply_markup=kb, reply_to_message_id=update.message.message_id)
        return

    await execute_generic_media_download(update.message, url, "TikTok", context)


async def handle_instagram_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-detect Instagram links."""
    if not update.message or not update.message.text: return
    user = update.effective_user
    if user and is_maintenance_active_for_user(user.id):
        await update.message.reply_text(MAINTENANCE_NOTICE, parse_mode="HTML"); return

    text = update.message.text.strip()
    match = INSTAGRAM_URL_PATTERN.search(text)
    if not match: return
    url = match.group(0)

    chat_id = update.effective_chat.id
    if not is_downloader_enabled(chat_id, "instagram"):
        return

    auto_dl = get_download_mode(chat_id)
    if not auto_dl:
        short_id = store_pending_download(url, "Instagram")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ Download Instagram Media", callback_data=f"dlmed:{short_id}")]])
        await update.message.reply_text("📸 <b>Instagram Link Detected</b>\n<i>Click below to download:</i>", parse_mode="HTML", reply_markup=kb, reply_to_message_id=update.message.message_id)
        return

    await execute_instagram_download(update.message, url, context)


async def handle_reddit_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-detect Reddit links. Downloads video if present, or sends a dark Reddit Card for text posts."""
    if not update.message or not update.message.text: return
    user = update.effective_user
    if user and is_maintenance_active_for_user(user.id):
        await update.message.reply_text(MAINTENANCE_NOTICE, parse_mode="HTML"); return

    text = update.message.text.strip()
    match = REDDIT_URL_PATTERN.search(text)
    if not match: return
    raw_url = match.group(0)

    chat_id = update.effective_chat.id
    if not is_downloader_enabled(chat_id, "reddit"):
        return

    logger.info(f"Reddit link detected: {raw_url}")

    # Resolve share link / mobile redirect to canonical URL
    browser_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
    canonical_url = raw_url
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True, headers=browser_headers) as client:
            resp = await client.get(raw_url)
            final_url = str(resp.url).split("?")[0]
            if "/comments/" in final_url:
                canonical_url = final_url
            elif "/s/" in raw_url and resp.text:
                m_canon = re.search(r'<link rel="canonical" href="([^"]+)"', resp.text) or re.search(r'property="og:url" content="([^"]+)"', resp.text)
                if m_canon:
                    canonical_url = m_canon.group(1).split("?")[0]
    except Exception as e:
        logger.warning(f"Failed to resolve Reddit redirect for {raw_url}: {e}")

    auto_dl = get_download_mode(chat_id)
    if not auto_dl:
        short_id = store_pending_download(canonical_url, "Reddit")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ Download Reddit Media / Card", callback_data=f"dlmed:{short_id}")]])
        await update.message.reply_text("🤖 <b>Reddit Link Detected</b>\n<i>Click below to process:</i>", parse_mode="HTML", reply_markup=kb, reply_to_message_id=update.message.message_id)
        return

    status_msg = await update.message.reply_text("⏳ Processing Reddit link...", reply_to_message_id=update.message.message_id)

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            # 1. Attempt yt-dlp media download first
            try:
                info = await download_generic_media(canonical_url, tmp_dir, "Reddit")
                filepath = info["filepath"]
                if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                    file_size = os.path.getsize(filepath)
                    if file_size > 50 * 1024 * 1024:
                        await status_msg.edit_text(fmt_warning(f"This video exceeds Telegram's 50MB bot limit ({file_size / (1024*1024):.1f}MB)."))
                        return

                    title = info.get("title", "Reddit Video")
                    uploader = info.get("uploader", "Reddit")
                    duration = int(info.get("duration", 0))

                    caption = (
                        f"🤖 <b>{html.escape(title)}</b>\n"
                        f"👤 <i>Source: {html.escape(uploader)}</i>\n\n"
                        f"🔗 <a href='{canonical_url}'>Reddit Link</a>"
                    )

                    await status_msg.edit_text("📤 Uploading video to Telegram...")
                    with open(filepath, "rb") as video_file:
                        await update.message.reply_video(
                            video=video_file,
                            caption=caption,
                            parse_mode="HTML",
                            duration=duration,
                            supports_streaming=True,
                            reply_to_message_id=update.message.message_id
                        )
                    try:
                        await status_msg.delete()
                    except Exception:
                        pass
                    return
            except Exception as dl_err:
                logger.info(f"yt-dlp video download not applicable for Reddit URL ({dl_err}). Generating Reddit Card...")

            # 2. Extract complete Reddit metadata and attached image URLs via multi-source resolution
            subreddit = "r/reddit"
            author = "user"
            title = "Reddit Post"
            selftext = ""
            score = 0
            num_comments = 0
            image_urls = []

            m_sub = re.search(r'r/([A-Za-z0-9_]+)', canonical_url)
            if m_sub:
                subreddit = f"r/{m_sub.group(1)}"

            browser_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}

            # Source A: Query Official Reddit oEmbed API for guaranteed Title & Author
            try:
                oembed_url = f"https://www.reddit.com/oembed?url={canonical_url}"
                async with httpx.AsyncClient(timeout=5.0) as client:
                    o_resp = await client.get(oembed_url, headers=browser_headers)
                    if o_resp.status_code == 200:
                        o_data = o_resp.json()
                        if o_data.get("title"):
                            title = html.unescape(o_data["title"].strip())
                        if o_data.get("author_name"):
                            author = o_data["author_name"].strip()
            except Exception as oe_err:
                logger.warning(f"Reddit oEmbed error: {oe_err}")

            # Source B: Query old.reddit.com for images, full selftext body, upvotes, and comments
            old_url = re.sub(r'https?://(?:www\.|old\.)?reddit\.com', 'https://old.reddit.com', canonical_url)
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    resp = await client.get(old_url, headers=browser_headers, follow_redirects=True)
                    if resp.status_code == 200:
                        html_text = resp.text
                        m_post = re.search(r'<div[^>]*id="siteTable"[^>]*>(.*?)<div[^>]*class="[^"]*commentarea', html_text, re.DOTALL)
                        target_html = m_post.group(1) if m_post else html_text

                        if author == "user":
                            m_author = re.search(r'data-author="([^"]+)"', target_html) or re.search(r'class="author[^"]*"[^>]*>([^<]+)<', target_html)
                            if m_author:
                                author = m_author.group(1).strip()

                        if title == "Reddit Post":
                            m_title = re.search(r'<a[^>]*class="title[^"]*"[^>]*>([^<]+)<', target_html)
                            if m_title:
                                title = html.unescape(m_title.group(1).strip())

                        m_score = re.search(r'data-score="(\d+)"', target_html) or re.search(r'<div[^>]*class="score unvoted"[^>]*title="(\d+)"', target_html)
                        if m_score:
                            score = int(m_score.group(1))

                        m_comments = re.search(r'(\d+)\s+comments', target_html)
                        if m_comments:
                            num_comments = int(m_comments.group(1))

                        # Extract image URLs (i.redd.it, preview.redd.it, i.imgur.com)
                        imgs = re.findall(r'href="(https://(?:i|preview)\.redd\.it/[^"]+\.(?:jpg|jpeg|png|gif|webp))"', target_html, re.IGNORECASE)
                        if not imgs:
                            imgs = re.findall(r'href="(https://i\.imgur\.com/[^"]+\.(?:jpg|jpeg|png|gif|webp))"', target_html, re.IGNORECASE)
                        if not imgs:
                            m_exp = re.search(r'data-url="(https://(?:i|preview)\.redd\.it/[^"]+)"', target_html)
                            if m_exp: imgs = [m_exp.group(1)]

                        for img_u in imgs:
                            clean_u = html.unescape(img_u)
                            if clean_u not in image_urls:
                                image_urls.append(clean_u)

                        m_selftext = re.search(r'<div[^>]*class="[^"]*usertext-body[^"]*"[^>]*>(.*?)</div>', target_html, re.DOTALL)
                        if m_selftext:
                            raw_md = m_selftext.group(1)
                            raw_md = re.sub(r'</p>\s*<p>', '\n\n', raw_md)
                            clean_body = re.sub(r'<[^>]+>', '', raw_md).strip()
                            selftext = html.unescape(clean_body)
            except Exception as r_err:
                logger.warning(f"old.reddit.com metadata extraction error: {r_err}")

            # Source C: Fallback to TelegramBot headers if selftext or score/comments still missing
            if not selftext or score == 0:
                try:
                    bot_headers = {"User-Agent": "TelegramBot (like TwitterBot)"}
                    async with httpx.AsyncClient(timeout=8.0) as client:
                        r_bot = await client.get(canonical_url, headers=bot_headers, follow_redirects=True)
                        if r_bot.status_code == 200:
                            bot_html = r_bot.text
                            if title == "Reddit Post":
                                m_bt = re.search(r'<title>(.*?)\s*:\s*(r/[A-Za-z0-9_]+)</title>', bot_html, re.IGNORECASE)
                                if m_bt: title = m_bt.group(1).strip()

                            m_meta = re.search(r'<meta [^>]*content="(\d+\s+votes?,\s*\d+\s+comments?\.[^"]*)"', bot_html, re.IGNORECASE)
                            if m_meta:
                                meta_str = m_meta.group(1)
                                m_parsed = re.search(r'(\d+)\s+votes?,\s*(\d+)\s+comments?\.\s*(.*)', meta_str, re.DOTALL)
                                if m_parsed:
                                    if score == 0: score = int(m_parsed.group(1))
                                    if num_comments == 0: num_comments = int(m_parsed.group(2))
                                    if not selftext: selftext = m_parsed.group(3).strip()

                            if not image_urls:
                                m_og_img = re.search(r'<meta [^>]*property="og:image" content="([^"]+)"', bot_html)
                                if m_og_img and "share.redd.it" not in m_og_img.group(1):
                                    image_urls.append(html.unescape(m_og_img.group(1)))
                except Exception as b_err:
                    logger.warning(f"TelegramBot HTML fallback error: {b_err}")

            # 3. If image post: download and send photo(s)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Open Reddit Post", url=canonical_url)]])
            if image_urls:
                try:
                    photo_bytes_list = []
                    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                        for img_link in image_urls[:8]:
                            try:
                                p_resp = await client.get(img_link, headers=browser_headers)
                                if p_resp.status_code == 200:
                                    photo_bytes_list.append(p_resp.content)
                            except Exception as p_err:
                                logger.warning(f"Failed to fetch Reddit photo {img_link}: {p_err}")

                    if photo_bytes_list:
                        caption = (
                            f"🤖 <b>{html.escape(title)}</b>\n"
                            f"👤 <i>Posted by u/{html.escape(author)} in {html.escape(subreddit)}</i>\n\n"
                            f"🔗 <a href='{canonical_url}'>View on Reddit</a>"
                        )
                        if len(caption) > 1024:
                            caption = caption[:1020] + "…"

                        if len(photo_bytes_list) == 1:
                            await update.message.reply_photo(
                                photo=photo_bytes_list[0],
                                caption=caption,
                                parse_mode="HTML",
                                reply_markup=kb,
                                reply_to_message_id=update.message.message_id
                            )
                        else:
                            media_group = [InputMediaPhoto(media=photo_bytes_list[0], caption=caption, parse_mode="HTML")] + [
                                InputMediaPhoto(media=pb) for pb in photo_bytes_list[1:]
                            ]
                            await update.message.reply_media_group(
                                media=media_group,
                                reply_to_message_id=update.message.message_id
                            )
                        try:
                            await status_msg.delete()
                        except Exception:
                            pass
                        return
                except Exception as img_send_err:
                    logger.error(f"Failed to send Reddit photo: {img_send_err}")

            # 4. If text post (or image download failed): generate & send Dark Reddit Card
            reddit_data = {
                "subreddit": subreddit,
                "author": author,
                "title": title,
                "body": selftext,
                "score": score,
                "num_comments": num_comments,
                "url": canonical_url,
            }

            card_png = card.generate_reddit_card(reddit_data)

            await update.message.reply_photo(
                photo=card_png,
                caption=f"🤖 <b>From {html.escape(subreddit)} on Reddit</b>\n\n🔗 <a href='{canonical_url}'>View Post</a>",
                parse_mode="HTML",
                reply_markup=kb,
                reply_to_message_id=update.message.message_id
            )
            try:
                await status_msg.delete()
            except Exception:
                pass

    except Exception as e:
        logger.error(f"Reddit message handler failed: {e}")
        try:
            await status_msg.edit_text(fmt_error("Failed to process Reddit link."))
        except Exception:
            pass


async def handle_pending_download_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle download buttons created in button-prompt mode (dlmed:<short_id>)."""
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data or not data.startswith("dlmed:"): return
    short_id = data[6:]
    item = pending_downloads.get(short_id)
    if not item:
        await query.message.reply_text(fmt_error("Download link expired. Please post the link again."))
        return
    url = item["url"]
    platform = item["platform"]
    status_msg = await query.message.reply_text(f"⏳ Starting {platform} download...")
    if platform == "Instagram":
        await execute_instagram_download(query.message, url, context, status_msg=status_msg)
    else:
        await execute_generic_media_download(query.message, url, platform, context, status_msg=status_msg)


# ─── Twitter / X Media & Card Handler ─────────────────────────────────────────

async def handle_twitter_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Detect Twitter/X links."""
    if not update.message or not update.message.text:
        return

    user = update.effective_user
    if user and is_maintenance_active_for_user(user.id):
        await update.message.reply_text(MAINTENANCE_NOTICE, parse_mode="HTML")
        return

    text = update.message.text.strip()
    match = TWITTER_URL_PATTERN.search(text)
    if not match:
        return

    chat_id = update.effective_chat.id
    if not is_downloader_enabled(chat_id, "twitter"):
        return

    username, tweet_id = match.groups()
    logger.info(f"Twitter/X link detected: @{username} status {tweet_id}")

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_video")
    except Exception:
        pass

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    tweet_data = None

    # 1. Query FxTwitter API
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"https://api.fxtwitter.com/{username}/status/{tweet_id}", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                raw_tweet = data.get("tweet")
                if raw_tweet:
                    author = raw_tweet.get("author") or {}
                    verification = author.get("verification") or {}
                    quote = raw_tweet.get("quote") or {}
                    media = raw_tweet.get("media") or {}
                    quote_media = quote.get("media") or {}

                    videos = (media.get("videos") or []) + (quote_media.get("videos") or [])
                    photos = (media.get("photos") or []) + (quote_media.get("photos") or [])

                    tweet_data = {
                        "author_name": author.get("name") or username,
                        "author_screen_name": author.get("screen_name") or username,
                        "author_avatar_url": author.get("avatar_url"),
                        "verified": verification.get("verified", False),
                        "text": raw_tweet.get("text", ""),
                        "created_at": raw_tweet.get("created_at"),
                        "created_timestamp": raw_tweet.get("created_timestamp"),
                        "retweets": raw_tweet.get("retweets", 0),
                        "likes": raw_tweet.get("likes", 0),
                        "replies": raw_tweet.get("replies", 0),
                        "views": raw_tweet.get("views"),
                        "url": raw_tweet.get("url") or f"https://x.com/{username}/status/{tweet_id}",
                        "quote": quote,
                        "videos": videos,
                        "photos": photos,
                    }
    except Exception as e:
        logger.warning(f"FxTwitter check failed for @{username}/{tweet_id}: {e}")

    # 2. Fallback to VxTwitter if FxTwitter did not resolve
    if not tweet_data:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"https://api.vxtwitter.com/{username}/status/{tweet_id}", headers=headers)
                if resp.status_code == 200:
                    raw_tweet = resp.json()
                    media_urls = raw_tweet.get("mediaURLs") or []
                    videos = []
                    photos = []
                    for m_url in media_urls:
                        if any(ext in m_url.lower() for ext in [".mp4", ".mov", ".m3u8", "video"]):
                            videos.append({"url": m_url})
                        else:
                            photos.append({"url": m_url})

                    tweet_data = {
                        "author_name": raw_tweet.get("user_name") or username,
                        "author_screen_name": raw_tweet.get("user_screen_name") or username,
                        "author_avatar_url": raw_tweet.get("user_profile_image_url"),
                        "verified": False,
                        "text": raw_tweet.get("text", ""),
                        "created_at": raw_tweet.get("date"),
                        "created_timestamp": raw_tweet.get("date_epoch"),
                        "retweets": raw_tweet.get("retweets", 0),
                        "likes": raw_tweet.get("likes", 0),
                        "replies": raw_tweet.get("replies", 0),
                        "views": None,
                        "url": raw_tweet.get("tweetURL") or f"https://x.com/{username}/status/{tweet_id}",
                        "quote": None,
                        "videos": videos,
                        "photos": photos,
                    }
        except Exception as e:
            logger.warning(f"VxTwitter fallback check failed for @{username}/{tweet_id}: {e}")

    if not tweet_data:
        logger.info(f"Ignoring Twitter link (failed to fetch metadata for @{username}/{tweet_id})")
        return

    author_name = tweet_data.get("author_name") or username
    screen_name = tweet_data.get("author_screen_name") or username
    main_text = tweet_data.get("text", "").strip()
    quote = tweet_data.get("quote")
    likes = tweet_data.get("likes", 0)
    retweets = tweet_data.get("retweets", 0)
    views = tweet_data.get("views")

    caption_parts = [f"𝕏 <b>{html.escape(author_name)}</b> (<code>@{html.escape(screen_name)}</code>)\n"]
    if main_text:
        caption_parts.append(html.escape(main_text))

    if quote:
        q_author = quote.get("author", {}).get("name") or "Quoted"
        q_screen = quote.get("author", {}).get("screen_name") or ""
        q_text = (quote.get("text") or "").strip()
        if q_text:
            caption_parts.append(
                f"\n💬 <b>Quoting {html.escape(q_author)} (@{html.escape(q_screen)}):</b>\n<i>{html.escape(q_text[:300])}</i>"
            )

    stats = []
    if likes:
        stats.append(f"❤️ {card.format_count(likes)}")
    if retweets:
        stats.append(f"🔁 {card.format_count(retweets)}")
    if views:
        stats.append(f"👁️ {card.format_count(views)}")

    if stats:
        caption_parts.append(f"\n{'  •  '.join(stats)}")

    caption = "\n".join(caption_parts)
    if len(caption) > 1024:
        caption = caption[:1020] + "…"

    tweet_url = tweet_data.get("url") or f"https://x.com/{username}/status/{tweet_id}"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("↗️ View on 𝕏", url=tweet_url)]])

    videos = tweet_data.get("videos") or []
    photos = tweet_data.get("photos") or []

    # ─── 1. If Video is present: send Video only (with text caption & button) ─────
    if videos:
        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_video")
        except Exception:
            pass

        v_obj = videos[0]
        video_url = v_obj.get("url")
        variants = v_obj.get("variants") or v_obj.get("formats") or []
        mp4_variants = [
            v for v in variants
            if v.get("content_type") == "video/mp4" or v.get("container") == "mp4" or ".mp4" in v.get("url", "")
        ]
        if mp4_variants:
            mp4_variants.sort(key=lambda x: x.get("bitrate", 0), reverse=True)
            video_url = mp4_variants[0].get("url") or video_url

        video_bytes = None
        if video_url:
            try:
                async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as v_client:
                    v_resp = await v_client.get(video_url, headers=headers)
                    if v_resp.status_code == 200 and len(v_resp.content) <= 50 * 1024 * 1024:
                        video_bytes = v_resp.content
            except Exception as e:
                logger.warning(f"Direct Twitter video download failed for @{username}/{tweet_id}: {e}")

        if not video_bytes:
            try:
                loop = asyncio.get_running_loop()
                def _yt_dlp_tw():
                    with tempfile.TemporaryDirectory() as tmp_dir:
                        out_template = os.path.join(tmp_dir, "%(id)s.%(ext)s")
                        ydl_opts = {
                            "outtmpl": out_template,
                            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                            "quiet": True,
                            "no_warnings": True,
                        }
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            ydl.extract_info(tweet_url, download=True)
                        for f in os.listdir(tmp_dir):
                            if f.endswith((".mp4", ".mkv", ".webm")):
                                with open(os.path.join(tmp_dir, f), "rb") as vf:
                                    return vf.read()
                        return None

                tw_b = await loop.run_in_executor(None, _yt_dlp_tw)
                if tw_b and len(tw_b) <= 50 * 1024 * 1024:
                    video_bytes = tw_b
            except Exception as e:
                logger.error(f"yt-dlp fallback failed for Twitter video @{username}/{tweet_id}: {e}")

        if video_bytes:
            try:
                await update.message.reply_video(
                    video=video_bytes,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    supports_streaming=True,
                    reply_to_message_id=update.message.message_id
                )
                logger.info(f"Sent auto-playing Twitter video for @{username}/status/{tweet_id}")
                return
            except Exception as e:
                logger.error(f"Failed to send video: {e}")

    # ─── 2. If Photos are present, send Photos alone in 1 media group ─────────────
    if photos:
        try:
            photo_bytes_list = []
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                for p in photos[:8]:
                    p_url = p.get("url")
                    if p_url:
                        try:
                            p_resp = await client.get(p_url, headers=headers)
                            if p_resp.status_code == 200:
                                photo_bytes_list.append(p_resp.content)
                        except Exception as p_err:
                            logger.warning(f"Failed to download photo {p_url}: {p_err}")

            if photo_bytes_list:
                media_group = [InputMediaPhoto(media=pb) for pb in photo_bytes_list]
                await update.message.reply_media_group(
                    media=media_group,
                    reply_to_message_id=update.message.message_id
                )
                logger.info(f"Sent Photo(s) media group for @{username}/status/{tweet_id}")
                return
        except Exception as e:
            logger.error(f"Failed to send photo media group: {e}")

    # ─── 3. Text-Only (or Media Fallback): Send Dark Tweet Card ───────────────────
    card_data = dict(tweet_data)
    card_text = main_text
    if quote:
        q_author = quote.get("author", {}).get("name") or "Quoted"
        q_screen = quote.get("author", {}).get("screen_name") or ""
        q_text = (quote.get("text") or "").strip()
        if q_text:
            if card_text:
                card_text += f"\n\nQuoting {q_author} (@{q_screen}):\n{q_text}"
            else:
                card_text = f"Quoting {q_author} (@{q_screen}):\n{q_text}"
    card_data["text"] = card_text

    avatar_bytes = None
    if tweet_data.get("author_avatar_url"):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                av_resp = await client.get(tweet_data["author_avatar_url"], headers=headers)
                if av_resp.status_code == 200:
                    avatar_bytes = av_resp.content
        except Exception as e:
            logger.warning(f"Failed to fetch avatar for @{username}: {e}")

    card_png = None
    try:
        loop = asyncio.get_running_loop()
        card_png = await loop.run_in_executor(
            None,
            lambda: card.generate_twitter_card(card_data, avatar_bytes)
        )
    except Exception as e:
        logger.error(f"Failed to render tweet card: {e}")

    if card_png:
        try:
            await update.message.reply_photo(
                photo=card_png,
                reply_markup=keyboard,
                reply_to_message_id=update.message.message_id
            )
            logger.info(f"Sent dark tweet card for @{username}/status/{tweet_id}")
            return
        except Exception as e:
            logger.error(f"Failed to send tweet card photo: {e}")

    # Final Text Fallback
    try:
        await update.message.reply_text(
            caption,
            parse_mode="HTML",
            reply_markup=keyboard,
            reply_to_message_id=update.message.message_id,
            disable_web_page_preview=True
        )
    except Exception:
        pass


# ─── Auto-Translate in Private Chat ──────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Translate incoming text messages in private chats."""
    user = update.effective_user
    if not user or not update.message or not update.message.text:
        return

    if is_maintenance_active_for_user(user.id):
        await update.message.reply_text(MAINTENANCE_NOTICE, parse_mode="HTML")
        return

    if update.message.text.startswith("/"):
        return

    # Do not auto-translate if message contains HTTP/HTTPS URLs
    text_lower = update.message.text.lower()
    if "http://" in text_lower or "https://" in text_lower:
        return

    # Only auto-translate in private chats
    if update.effective_chat.type != "private":
        return

    config = get_user_config(user.id)
    engine = config["engine"]
    target_lang = config["target"]
    text = update.message.text

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    src_lang = detect_language_code(text)

    translated_text = None
    fallback = False

    # 1. Try AI engine
    if engine == "ai" and has_ai:
        try:
            translated_text = await translate_ai(text, target_lang)
        except Exception as e:
            logger.error(f"AI translation failed: {e}. Falling back to free engine.")
            fallback = True

    # 2. Free engine (or fallback)
    if not translated_text:
        try:
            translated_text = await translate_free(text, target_lang)
        except Exception:
            await update.message.reply_text(
                fmt_error("Translation failed. Please try again later."),
                parse_mode="HTML"
            )
            return

    response_msg = fmt_translation(src_lang, target_lang, translated_text, fallback=fallback)
    await update.message.reply_text(response_msg, parse_mode="HTML")


# ─── Interactive Media Settings Menu (/downloads) ─────────────────────────────

def build_downloads_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Build interactive inline keyboard for per-chat download settings."""
    cfg = get_chat_config(chat_id)

    def btn_txt(key: str, name: str) -> str:
        icon = "🟢" if cfg.get(key, True) else "🔴"
        return f"{icon} {name}"

    mode_txt = "⚡ Mode: Auto-Download" if cfg.get("auto_download", True) else "🔘 Mode: Button-Prompt"

    keyboard = [
        [
            InlineKeyboardButton(btn_txt("youtube", "YouTube"), callback_data="dltog:youtube"),
            InlineKeyboardButton(btn_txt("twitter", "Twitter/X"), callback_data="dltog:twitter"),
        ],
        [
            InlineKeyboardButton(btn_txt("twitch", "Twitch Clips"), callback_data="dltog:twitch"),
            InlineKeyboardButton(btn_txt("tiktok", "TikTok"), callback_data="dltog:tiktok"),
        ],
        [
            InlineKeyboardButton(btn_txt("instagram", "Instagram"), callback_data="dltog:instagram"),
            InlineKeyboardButton(btn_txt("reddit", "Reddit"), callback_data="dltog:reddit"),
        ],
        [
            InlineKeyboardButton(mode_txt, callback_data="dltog:mode"),
        ],
        [
            InlineKeyboardButton("✖️ Close Settings", callback_data="dltog:close"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_downloads_text(chat_id: int, chat_title: str = "") -> str:
    """Build text summary for `/downloads` command."""
    cfg = get_chat_config(chat_id)
    title_str = f" for <b>{html.escape(chat_title)}</b>" if chat_title else ""

    platforms = [
        ("YouTube", cfg.get("youtube", True)),
        ("Twitter / X", cfg.get("twitter", True)),
        ("Twitch Clips", cfg.get("twitch", True)),
        ("TikTok", cfg.get("tiktok", True)),
        ("Instagram", cfg.get("instagram", True)),
        ("Reddit", cfg.get("reddit", True)),
    ]

    status_lines = []
    for name, enabled in platforms:
        status_lines.append(f"{'🟢' if enabled else '🔴'} <b>{name}</b>")

    mode_desc = (
        "⚡ <b>Auto-Download Mode</b>\n<i>Link downloads trigger automatically upon posting.</i>"
        if cfg.get("auto_download", True)
        else "🔘 <b>Button-Prompt Mode</b>\n<i>Links display a download button before fetching media.</i>"
    )

    return (
        f"⚙️ <b>Media Download Settings</b>{title_str}\n\n"
        f"<b>Platform Permissions:</b>\n"
        + "  •  ".join(status_lines[:3]) + "\n"
        + "  •  ".join(status_lines[3:]) + "\n\n"
        f"<b>Current Download Mode:</b>\n{mode_desc}\n\n"
        f"<i>Group Administrators can click the buttons below to toggle permissions or modes live.</i>"
    )


async def downloads_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /downloads or /mediaconfig command."""
    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user:
        return

    if is_maintenance_active_for_user(user.id):
        await update.message.reply_text(MAINTENANCE_NOTICE, parse_mode="HTML")
        return

    # Check admin privileges in group chats
    if chat.type in ["group", "supergroup"]:
        try:
            member = await context.bot.get_chat_member(chat_id=chat.id, user_id=user.id)
            if member.status not in ["administrator", "creator"] and user.id not in ADMIN_USER_IDS:
                await update.message.reply_text(
                    fmt_error("Only Group Administrators can configure media settings."),
                    parse_mode="HTML"
                )
                return
        except Exception as e:
            logger.error(f"Admin check error in /downloads: {e}")

    text = build_downloads_text(chat.id, chat.title or "")
    kb = build_downloads_keyboard(chat.id)

    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def handle_downloads_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button presses for download settings (dltog:...)."""
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("dltog:"):
        return

    user = query.from_user
    chat = query.message.chat if query.message else None

    if not chat:
        await query.answer()
        return

    # Check admin privileges in group chats
    if chat.type in ["group", "supergroup"]:
        try:
            member = await context.bot.get_chat_member(chat_id=chat.id, user_id=user.id)
            if member.status not in ["administrator", "creator"] and user.id not in ADMIN_USER_IDS:
                await query.answer("❌ Only Group Administrators can modify settings.", show_alert=True)
                return
        except Exception:
            await query.answer("❌ Verification failed.", show_alert=True)
            return

    action = query.data[6:]

    if action == "close":
        await query.answer("Closed settings.")
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if action == "menu":
        await query.answer()
        text = build_downloads_text(chat.id, chat.title or "")
        kb = build_downloads_keyboard(chat.id)
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        return

    if action == "mode":
        new_mode = toggle_download_mode(chat.id)
        mode_str = "Auto-Download" if new_mode else "Button-Prompt"
        await query.answer(f"Switched mode to {mode_str}")
    else:
        new_state = toggle_downloader(chat.id, action)
        state_str = "Enabled" if new_state else "Disabled"
        await query.answer(f"{action.capitalize()} {state_str}")

    # Update message text and inline keyboard live
    text = build_downloads_text(chat.id, chat.title or "")
    kb = build_downloads_keyboard(chat.id)

    try:
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass


async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send welcome greeting when bot joins a group."""
    result = update.my_chat_member
    if not result:
        return
    new_status = result.new_chat_member.status
    old_status = result.old_chat_member.status

    if old_status in ["left", "kicked"] and new_status in ["member", "administrator"]:
        chat = result.chat
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚙️ Configure Download Settings", callback_data="dltog:menu")]
        ])
        await context.bot.send_message(
            chat_id=chat.id,
            text=(
                f"👋 <b>Hello {html.escape(chat.title or 'everyone')}!</b>\n\n"
                f"I'm your <b>Translator & Media Downloader Bot</b>!\n"
                f"• Auto-translates text in private chats\n"
                f"• Downloads video/media from <b>YouTube, Twitter, Twitch Clips, TikTok, Instagram, and Reddit</b>\n\n"
                f"<i>Group Administrators can run /downloads to configure platform permissions.</i>"
            ),
            parse_mode="HTML",
            reply_markup=keyboard
        )



# ─── Main Application Runner ─────────────────────────────────────────────────

async def auto_pinger_loop(application: Application) -> None:
    """Background task that pings Render root URL to prevent free-tier sleep."""
    # Ping the root URL (not the webhook token path) to avoid 405 errors
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    target_url = PING_URL or render_url
    target_desc = target_url or "Telegram API (getMe)"
    logger.info(f"Auto-pinger active. Interval: {PING_INTERVAL}s | Target: {target_desc}")

    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                ping_target = PING_URL or os.getenv("RENDER_EXTERNAL_URL")
                if ping_target:
                    await client.get(ping_target)
                else:
                    await application.bot.get_me()
            except asyncio.CancelledError:
                logger.info("Auto-pinger task cancelled.")
                break
            except Exception:
                pass  # Silently ignore ping failures

            await asyncio.sleep(PING_INTERVAL)


async def post_init(application: Application) -> None:
    """Register bot commands in Telegram's menu button and start background tasks on startup."""
    global _config_bot
    _config_bot = application.bot
    await load_chat_configs_from_channel()

    await application.bot.set_my_commands([
        ("start", "Start the bot and see configuration"),
        ("downloads", "Configure media download permissions (Admins)"),
        ("tr", "Translate text (reply or inline)"),
        ("target", "Set translation target language"),
        ("engine", "Switch AI / Free engine"),
        ("status", "Show settings and status"),
        ("help", "Full help guide"),
        ("report", "Report a problem to admins"),
        ("setcookies", "Update YouTube cookies (DM only)"),
        ("ban", "Ban user from group (Admins)"),
        ("promote", "Promote user to Admin (Admins)"),
        ("demote", "Demote Admin (Admins)"),
    ])

    if AUTO_PING_ENABLED:
        asyncio.create_task(auto_pinger_loop(application))


def main() -> None:
    """Bootstrap and start the Telegram Bot."""
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN is missing in environment variables. Exiting.")
        print("\n[CRITICAL ERROR] TELEGRAM_BOT_TOKEN is missing. Please add it to your .env file.\n")
        return

    # Configure custom HTTP request settings with proxy & timeout support
    proxy_url = (
        os.getenv("HTTPS_PROXY")
        or os.getenv("HTTP_PROXY")
        or os.getenv("https_proxy")
        or os.getenv("http_proxy")
    )
    request_kwargs = {
        "connect_timeout": 30.0,
        "read_timeout": 30.0,
        "write_timeout": 30.0,
        "pool_timeout": 30.0,
    }
    if proxy_url:
        request_kwargs["proxy_url"] = proxy_url

    request = HTTPXRequest(**request_kwargs)

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(request)
        .post_init(post_init)
        .build()
    )

    # Register command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler(["downloads", "mediaconfig"], downloads_command))
    application.add_handler(CommandHandler("tr", tr_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("target", target_command))
    application.add_handler(CommandHandler("engine", engine_command))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("promote", promote_command))
    application.add_handler(CommandHandler("demote", demote_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("setcookies", setcookies_command))
    application.add_handler(CommandHandler("maintenance", maintenance_command))
    application.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.Document.ALL,
        setcookies_command
    ))

    # Register Settings & Media Download Button callback handlers
    application.add_handler(CallbackQueryHandler(handle_downloads_callback, pattern="^dltog:"))
    application.add_handler(CallbackQueryHandler(handle_pending_download_button, pattern="^dlmed:"))
    application.add_handler(CallbackQueryHandler(handle_youtube_download_button, pattern="^ytdl:"))

    # Register Bot Join Greeting handler
    application.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    # Register platform media handlers (before general text handler so they take priority)
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(YOUTUBE_URL_PATTERN),
        handle_youtube_message
    ))

    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(TWITTER_URL_PATTERN),
        handle_twitter_message
    ))

    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(TWITCH_CLIP_PATTERN),
        handle_twitch_clip_message
    ))

    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(TIKTOK_URL_PATTERN),
        handle_tiktok_message
    ))

    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(INSTAGRAM_URL_PATTERN),
        handle_instagram_message
    ))

    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(REDDIT_URL_PATTERN),
        handle_reddit_message
    ))

    # Register text message handler
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Register global error handler (catches unexpected crashes and sends patching notice)
    application.add_error_handler(global_error_handler)

    # Detect deployment environment
    webhook_base = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("KOYEB_PUBLIC_URL") or os.getenv("WEBHOOK_URL")
    port = int(os.getenv("PORT", "8443"))

    if webhook_base:
        # ─── Webhook mode (Render / Koyeb / Custom) ───
        webhook_url = f"{webhook_base.rstrip('/')}/{TELEGRAM_BOT_TOKEN}"
        logger.info(f"Running in webhook mode on {webhook_base} (port {port})")
        print("\n" + "─" * 45)
        print("  🤖  Translation Bot (Webhook Mode)")
        print(f"  Listening on port {port}")
        print("─" * 45 + "\n")
        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=TELEGRAM_BOT_TOKEN,
            webhook_url=webhook_url,
        )
    else:
        # ─── Local / other: Use polling mode ───
        logger.info("Bot is starting polling...")
        print("\n" + "─" * 45)
        print("  🤖  Translation Bot is now running!")
        print("  Press Ctrl+C to stop.")
        print("─" * 45 + "\n")
        application.run_polling(bootstrap_retries=-1)


if __name__ == "__main__":
    main()
