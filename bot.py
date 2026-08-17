import os
import logging
import asyncio
import re
import html
import tempfile
import uuid
import time
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest
import httpx
from deep_translator import GoogleTranslator
from openai import OpenAI
from langdetect import detect
import yt_dlp

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
    """Auto-detect YouTube links in messages. Shorts auto-download; standard videos show a download button."""
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    # Extract YouTube URL using regex
    match = YOUTUBE_URL_PATTERN.search(text)
    if not match:
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


# ─── Auto-Translate in Private Chat ──────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Translate incoming text messages in private chats."""
    user = update.effective_user
    if not user or not update.message or not update.message.text:
        return

    if update.message.text.startswith("/"):
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
    await application.bot.set_my_commands([
        ("start", "Start the bot and see configuration"),
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
    application.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.Document.ALL,
        setcookies_command
    ))

    # Register YouTube button callback handler
    application.add_handler(CallbackQueryHandler(handle_youtube_download_button, pattern="^ytdl:"))

    # Register YouTube auto-download handler (before general text handler so it takes priority)
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(YOUTUBE_URL_PATTERN),
        handle_youtube_message
    ))

    # Register text message handler
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

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
