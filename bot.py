"""
Telegram Translator & Media Downloader Bot
──────────────────────────────────────────
Main bot orchestrator: initializes Telegram Application, registers command and
media message handlers, and manages application lifecycle.

Submodules:
- config.py: Configuration, environment variables, in-memory state & formatting helpers
- translation.py: Free & AI translation engines, language detection & picker keyboards
- moderation.py: Group admin commands (/ban, /promote, /demote, /report, /setcookies, /maintenance)
- downloaders.py: YouTube, Twitch, Instagram, Threads, TikTok, Reddit media downloaders
- twitter_handler.py: Twitter/X media extraction with photo albums and captions
- quote_sticker.py: Glassmorphism quote stickers with emoji support & dynamic compact width
- card.py: Sleek dark Twitter/X cards
"""
from __future__ import annotations

import os
import re
import html
import shutil
import asyncio
import tempfile
import logging
from typing import Optional, List, Dict, Any, Tuple

import httpx
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    InputFile,
)
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

import config
from config import (
    TELEGRAM_BOT_TOKEN,
    LOCAL_BOT_API_URL,
    AUTO_PING_ENABLED,
    PING_URL,
    PING_INTERVAL,
    CONFIG_CHANNEL_ID,
    load_chat_configs_from_channel,
    save_chat_configs,
    get_chat_config,
    is_downloader_enabled,
    get_download_mode,
    toggle_downloader,
    toggle_download_mode,
    reset_chat_config,
    is_maintenance_active_for_user,
    MAINTENANCE_NOTICE,
    ADMIN_USER_IDS,
    ALLOW_ALL_TO_USE_AI,
    OPENROUTER_MODEL,
    has_ai,
    should_skip_message,
    safe_answer_query,
    logger,
    COMMON_LANGUAGES,
    LANG_FLAGS,
    get_flag,
    fmt_card,
    fmt_translation,
    fmt_success,
    fmt_error,
    fmt_warning,
    YOUTUBE_URL_PATTERN,
    TWITTER_URL_PATTERN,
    TWITCH_CLIP_PATTERN,
    TIKTOK_URL_PATTERN,
    INSTAGRAM_URL_PATTERN,
    THREADS_URL_PATTERN,
    REDDIT_URL_PATTERN,
)

from translation import (
    resolve_language_code,
    build_language_keyboard,
    handle_lang_callback,
    get_user_config,
    is_user_premium_or_admin,
    detect_language_code,
    translate_free,
    translate_ai,
    translate_ai_word_aligned,
    translate_image_ai,
    clean_caption_for_translation,
)

from moderation import (
    ban_command,
    promote_command,
    demote_command,
    warn_command,
    warns_command,
    unwarn_command,
    clearwarns_command,
    set_warn_limit_command,
    report_command,
    setcookies_command,
    maintenance_command,
    handle_maintenance_callback,
)

from downloaders import (
    handle_youtube_message,
    handle_youtube_download_button,
    handle_twitch_clip_message,
    handle_instagram_message,
    handle_threads_message,
    handle_tiktok_message,
    handle_reddit_message,
    handle_pending_download_button,
    download_generic_media,
)

from twitter_handler import handle_twitter_message
from voice_agent import handle_voice_message, voice_command
import quote_sticker
import card



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
        f"  • /languages — interactive language list\n"
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
        f"  /languages Interactive language picker\n"
        f"  /engine    Switch AI / Free engine\n"
        f"  /q         Quote as sticker\n"
        f"  /gif       Convert video to GIF\n"
        f"  /voice     Amharic Voice Assistant\n"
        f"  /status    View your settings\n"
        f"  /report    Report a bug\n"
        f"\n"
        f"─── 👮 Group Moderation ───\n"
        f"\n"
        f"  /warn      Warn a user (3-5 lives before ban)\n"
        f"  /warns     Check user warnings & lives\n"
        f"  /unwarn    Remove 1 warning (restore 1 life)\n"
        f"  /clearwarns Clear all warnings\n"
        f"  /setwarnlimit Set lives limit (3 to 5)\n"
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
    """Change the user's translation target language or open interactive picker."""
    user = update.effective_user
    if not user:
        return

    config = get_user_config(user.id)

    if not context.args:
        current_target = config["target"]
        current_name = COMMON_LANGUAGES.get(current_target, current_target.upper())
        current_flag = get_flag(current_target)
        reply_markup = build_language_keyboard(current_target, page=0)
        await update.message.reply_text(
            fmt_card("🌐 Language Settings",
                f"  Current Target: {current_flag} <b>{current_name}</b> (<code>{current_target}</code>)\n\n"
                f"  Tap any language below to set it as your target.\n"
                f"  Or type: <code>/target &lt;name or code&gt;</code> (e.g. <code>/target dutch</code>)"
            ),
            parse_mode="HTML",
            reply_markup=reply_markup
        )
        return

    raw_arg = " ".join(context.args).strip()
    new_target = resolve_language_code(raw_arg)

    if new_target:
        config["target"] = new_target
        target_name = COMMON_LANGUAGES.get(new_target, new_target.upper())
        new_flag = get_flag(new_target)
        await update.message.reply_text(
            fmt_success(f"Target language set to {new_flag} <b>{target_name}</b> (<code>{new_target}</code>)"),
            parse_mode="HTML"
        )
    else:
        current_target = config["target"]
        reply_markup = build_language_keyboard(current_target, page=0)
        await update.message.reply_text(
            fmt_error(f"Unknown language: <code>{html.escape(raw_arg)}</code>\nSelect from the list below or use /languages:"),
            parse_mode="HTML",
            reply_markup=reply_markup
        )


async def languages_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show interactive language settings and full list of available languages."""
    await target_command(update, context)



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
    if not user or not update.message or should_skip_message(update.message):
        return

    text_to_translate = None
    config = get_user_config(user.id)
    target_lang = config["target"]

    replied = update.message.reply_to_message

    # Check if user explicitly asked for image OCR (e.g. /tr ocr or /tr img)
    force_ocr = bool(context.args and context.args[0].lower() in ["ocr", "image", "img", "vision"])

    # Extract text from replied message (text or caption)
    replied_text = None
    if replied:
        if replied.text:
            replied_text = replied.text.strip()
        elif replied.caption:
            replied_text = clean_caption_for_translation(replied.caption)

    # Scenario A0: Reply to an image without text/caption (or forced OCR) — AI Vision OCR
    if replied and replied.photo and (not replied_text or force_ocr):
        target_arg_idx = 1 if force_ocr else 0
        if context.args and len(context.args) > target_arg_idx:
            resolved = resolve_language_code(context.args[target_arg_idx])
            if resolved:
                target_lang = resolved

        if config["engine"] != "ai" or not has_ai:
            await update.message.reply_text(
                fmt_error("Image translation requires the AI engine. Switch with /engine ai first."),
                parse_mode="HTML",
                reply_to_message_id=update.message.message_id
            )
            return

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        try:
            photo = replied.photo[-1]
            tg_file = await context.bot.get_file(photo.file_id)
            image_bytes = bytes(await tg_file.download_as_bytearray())
            translated_text = await translate_image_ai(image_bytes, target_lang)
        except Exception as e:
            logger.error(f"Image translation failed: {e}")
            await update.message.reply_text(
                fmt_error("Couldn't translate text from this image. Please try again."),
                parse_mode="HTML",
                reply_to_message_id=update.message.message_id
            )
            return

        if translated_text.strip() == "NO_TEXT_FOUND":
            await update.message.reply_text(
                fmt_warning("No readable text was found in this image."),
                parse_mode="HTML",
                reply_to_message_id=update.message.message_id
            )
            return

        target_name = COMMON_LANGUAGES.get(target_lang, target_lang.upper())
        await update.message.reply_photo(
            photo=photo.file_id,
            caption=f"🖼️ <b>Image Translation</b> (→ {html.escape(target_name)}):\n{html.escape(translated_text)}",
            parse_mode="HTML",
            reply_to_message_id=update.message.message_id
        )
        return

    # Scenario A: Reply to a message with text or media caption
    if replied_text:
        text_to_translate = replied_text
        if context.args:
            resolved = resolve_language_code(context.args[0])
            if resolved:
                target_lang = resolved

    # Scenario B: Inline arguments provided without a replied message
    elif context.args:
        resolved = resolve_language_code(context.args[0])
        if resolved and len(context.args) >= 2:
            target_lang = resolved
            text_to_translate = " ".join(context.args[1:])
        elif not resolved:
            text_to_translate = " ".join(context.args)
        # If user only passed a language code (e.g. /tr en) with no reply and no other text, text_to_translate stays None

    if not text_to_translate:
        await update.message.reply_text(
            fmt_card("🌐 /tr — Translate",
                f"  <b>Reply mode:</b>\n"
                f"  Reply to a message, video, or photo with /tr\n"
                f"  or <code>/tr dutch</code> (by name or code)\n"
                f"\n"
                f"  <b>Image mode (AI engine only):</b>\n"
                f"  Reply to a photo with /tr to translate text inside it\n"
                f"\n"
                f"  <b>Inline mode:</b>\n"
                f"  <code>/tr dutch hello world</code>\n"
                f"  <code>/tr es hello world</code>\n"
                f"\n"
                f"  See languages: /languages"
            ),
            parse_mode="HTML"
        )
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    engine = config["engine"]
    translated_text = None
    fallback = False
    aligned_pairs = []

    src_lang = detect_language_code(text_to_translate)
    is_short = len(text_to_translate.split()) <= 10

    # 1. Try AI engine (short texts also get a word-by-word alignment)
    if engine == "ai" and has_ai:
        try:
            if is_short:
                result = await translate_ai_word_aligned(text_to_translate, target_lang)
                translated_text = result["translation"]
                aligned_pairs = result["pairs"]
            else:
                translated_text = await translate_ai(text_to_translate, target_lang)
        except Exception as e:
            logger.error(f"AI translation failed: {e}. Falling back to free engine.")
            fallback = True

    # 2. Free engine (or fallback)
    if not translated_text:
        try:
            translated_text = await translate_free(text_to_translate, target_lang, src_lang=src_lang)
        except Exception:
            await update.message.reply_text(
                fmt_error("Translation failed. Please try again later."),
                parse_mode="HTML"
            )
            return

    response_msg = fmt_translation(src_lang, target_lang, translated_text, fallback=fallback)
    if aligned_pairs:
        pair_lines = "\n".join(f"{html.escape(s)}: {html.escape(t)}" for s, t in aligned_pairs)
        response_msg += f"\n\n<b>Word by word:</b>\n{pair_lines}"

    await update.message.reply_text(
        response_msg,
        parse_mode="HTML",
        reply_to_message_id=update.message.message_id
    )

async def _get_user_avatar_bytes(bot, user) -> Optional[bytes]:
    """Download a user's profile photo as bytes, or return None."""
    user_id = getattr(user, "id", None)
    if not user_id:
        return None

    # 1. Try get_user_profile_photos
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if photos and photos.total_count > 0:
            photo = photos.photos[0][-1]  # Highest resolution
            tg_file = await bot.get_file(photo.file_id)
            return bytes(await tg_file.download_as_bytearray())
    except Exception as e:
        logger.debug(f"Could not fetch user profile photos for {user_id}: {e}")

    # 2. Fallback to get_chat (which can return user chat photo)
    try:
        chat_obj = await bot.get_chat(user_id)
        if chat_obj and chat_obj.photo:
            tg_file = await bot.get_file(chat_obj.photo.big_file_id)
            return bytes(await tg_file.download_as_bytearray())
    except Exception as e:
        logger.debug(f"Could not fetch chat avatar for {user_id}: {e}")

    return None


async def _get_chat_avatar_bytes(bot, chat) -> Optional[bytes]:
    """Download a chat or channel's avatar as bytes, or return None."""
    try:
        chat_id = getattr(chat, "id", None)
        if not chat_id:
            return None
        chat_obj = await bot.get_chat(chat_id)
        if chat_obj and chat_obj.photo:
            tg_file = await bot.get_file(chat_obj.photo.big_file_id)
            return bytes(await tg_file.download_as_bytearray())
    except Exception as e:
        logger.debug(f"Could not fetch avatar for chat: {e}")
    return None


async def _extract_author_and_avatar(bot, msg) -> tuple[str, Optional[bytes]]:
    """
    Extract author name and avatar bytes from a message,
    properly supporting forwarded messages (Telegram Bot API 7.0+ MessageOrigin and legacy forwards).
    """
    author_name = None
    author_user = None
    author_chat = None

    # 1. Telegram Bot API 7.0+ MessageOrigin
    fo = getattr(msg, "forward_origin", None)
    if fo:
        # MessageOriginUser
        if getattr(fo, "sender_user", None):
            author_user = fo.sender_user
            author_name = author_user.first_name
            if getattr(author_user, "last_name", None):
                author_name = f"{author_user.first_name} {author_user.last_name}"
        # MessageOriginHiddenUser (user has forward privacy enabled)
        elif getattr(fo, "sender_user_name", None):
            author_name = fo.sender_user_name
        # MessageOriginChat
        elif getattr(fo, "sender_chat", None):
            author_chat = fo.sender_chat
            author_name = getattr(author_chat, "title", None) or "Chat"
        # MessageOriginChannel
        elif getattr(fo, "chat", None):
            author_chat = fo.chat
            author_name = getattr(author_chat, "title", None) or "Channel"

    # 2. Legacy forward attributes (pre-7.0 or fallback)
    if not author_name:
        if getattr(msg, "forward_from", None):
            author_user = msg.forward_from
            author_name = author_user.first_name
            if getattr(author_user, "last_name", None):
                author_name = f"{author_user.first_name} {author_user.last_name}"
        elif getattr(msg, "forward_sender_name", None):
            author_name = msg.forward_sender_name
        elif getattr(msg, "forward_from_chat", None):
            author_chat = msg.forward_from_chat
            author_name = getattr(author_chat, "title", None) or "Channel"

    # 3. Direct message author fallback (if not a forward)
    if not author_name:
        if getattr(msg, "from_user", None):
            author_user = msg.from_user
            author_name = author_user.first_name
            if getattr(author_user, "last_name", None):
                author_name = f"{author_user.first_name} {author_user.last_name}"
        elif getattr(msg, "sender_chat", None):
            author_chat = msg.sender_chat
            author_name = getattr(author_chat, "title", None) or "Chat"

    author_name = (author_name or "User").strip()

    # 4. Fetch avatar bytes
    avatar_bytes = None
    if author_user:
        avatar_bytes = await _get_user_avatar_bytes(bot, author_user)
    elif author_chat:
        avatar_bytes = await _get_chat_avatar_bytes(bot, author_chat)

    return author_name, avatar_bytes


async def _find_or_create_sticker_pack(bot, user, sticker_bytes, is_video=False) -> str:
    """Find existing pack or create a new one. Returns the pack name. Handles full packs."""
    from telegram import InputSticker
    from telegram.constants import StickerFormat

    bot_me = await bot.get_me()
    bot_username = bot_me.username
    user_name = user.first_name or "User"

    # Try packs 1, 2, 3... until we find one with space or create a new one
    for pack_num in range(1, 100):
        pack_name = quote_sticker.get_sticker_pack_name(user_name, user.id, bot_username, pack_num)
        pack_title = quote_sticker.get_sticker_pack_title(user_name, pack_num)

        try:
            sticker_set = await bot.get_sticker_set(pack_name)
            if len(sticker_set.stickers) < quote_sticker.MAX_STICKERS_PER_PACK:
                # Pack exists and has room — add to it
                sticker_format = StickerFormat.VIDEO if is_video else StickerFormat.STATIC
                input_sticker = InputSticker(
                    sticker=sticker_bytes,
                    emoji_list=["💬"],
                    format=sticker_format,
                )
                await bot.add_sticker_to_set(
                    user_id=user.id,
                    name=pack_name,
                    sticker=input_sticker,
                )
                return pack_name
            else:
                # Pack is full, try next number
                continue
        except Exception:
            # Pack doesn't exist yet — create it
            try:
                sticker_format = StickerFormat.VIDEO if is_video else StickerFormat.STATIC
                input_sticker = InputSticker(
                    sticker=sticker_bytes,
                    emoji_list=["💬"],
                    format=sticker_format,
                )
                await bot.create_new_sticker_set(
                    user_id=user.id,
                    name=pack_name,
                    title=pack_title,
                    stickers=[input_sticker],
                )
                return pack_name
            except Exception as e2:
                logger.error(f"Failed to create sticker pack '{pack_name}': {e2}")
                raise e2

    raise RuntimeError("Could not find or create a sticker pack")


async def q_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Quote a message as a sticker and add it to the user's sticker pack.

    Usage:
      /q          – Quote the replied text message as a card sticker
      /q r        – Quote with reply context (shows who they replied to)
      /q 5        – Quote the last 5 messages above as individual stickers
      /q 2 5      – For video: clip from 2s to 5s as a video sticker
    """
    user = update.effective_user
    if not user or not update.message or should_skip_message(update.message):
        return

    if is_maintenance_active_for_user(user.id):
        await update.message.reply_text(MAINTENANCE_NOTICE, parse_mode="HTML")
        return

    replied = update.message.reply_to_message

    # ── Parse arguments ──
    args = context.args or []
    include_reply_context = False
    multi_count = 0
    video_start = None
    video_end = None

    if args:
        if args[0].lower() == "r":
            include_reply_context = True
        elif len(args) == 1 and args[0].isdigit():
            num = int(args[0])
            # If replying to a video, treat single number as video end time
            if replied and (replied.video or replied.video_note or replied.animation):
                video_start = 0
                video_end = float(num)
            else:
                multi_count = min(num, 10)  # Cap at 10 to avoid spam
        elif len(args) == 2:
            try:
                video_start = float(args[0])
                video_end = float(args[1])
            except ValueError:
                pass

    # ── Multi-quote mode: /q N ──
    if multi_count > 0 and not replied:
        await update.message.reply_text(
            fmt_error("Reply to a message with /q N to quote N messages."),
            parse_mode="HTML"
        )
        return

    if not replied and multi_count == 0:
        await update.message.reply_text(
            fmt_card("💬 /q — Quote Sticker",
                f"  <b>Reply to a message with:</b>\n"
                f"  <code>/q</code> — Quote as sticker\n"
                f"  <code>/q r</code> — Quote with reply context\n"
                f"  <code>/q 5</code> — Quote last 5 messages\n"
                f"\n"
                f"  <b>For videos:</b>\n"
                f"  <code>/q 1 3</code> — Video sticker (1s to 3s)\n"
                f"  <code>/q 3 6</code> — Video sticker (3s to 6s)"
            ),
            parse_mode="HTML"
        )
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        # ── Case 1: Video reply with time range → video sticker ──
        if replied and (replied.video or replied.video_note or replied.animation) and video_start is not None and video_end is not None:
            duration = video_end - video_start
            if duration <= 0:
                await update.message.reply_text(fmt_error("End time must be after start time."), parse_mode="HTML")
                return
            if duration > 3:
                await update.message.reply_text(
                    fmt_warning(f"Video stickers can be max 3 seconds. Trimming to {video_start}s → {video_start + 3}s."),
                    parse_mode="HTML"
                )
                video_end = video_start + 3

            # Download the video
            video = replied.video or replied.video_note or replied.animation
            tg_file = await context.bot.get_file(video.file_id)
            video_bytes = bytes(await tg_file.download_as_bytearray())

            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                tmp.write(video_bytes)
                tmp_path = tmp.name

            try:
                sticker_bytes = await quote_sticker.video_to_sticker(tmp_path, video_start, video_end)
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

            if not sticker_bytes:
                await update.message.reply_text(
                    fmt_error("Failed to convert video to sticker. The video may be too large or incompatible."),
                    parse_mode="HTML"
                )
                return

            pack_name = await _find_or_create_sticker_pack(
                context.bot, user, sticker_bytes, is_video=True
            )
            sticker_set = await context.bot.get_sticker_set(pack_name)
            last_sticker = sticker_set.stickers[-1]
            await update.message.reply_sticker(sticker=last_sticker.file_id)
            return

        # ── Case 2: Image reply → raw image sticker (no card) ──
        if replied and replied.photo and not multi_count:
            photo = replied.photo[-1]
            tg_file = await context.bot.get_file(photo.file_id)
            image_bytes = bytes(await tg_file.download_as_bytearray())
            sticker_bytes = quote_sticker.image_to_sticker(image_bytes)

            pack_name = await _find_or_create_sticker_pack(
                context.bot, user, sticker_bytes, is_video=False
            )
            sticker_set = await context.bot.get_sticker_set(pack_name)
            last_sticker = sticker_set.stickers[-1]
            await update.message.reply_sticker(sticker=last_sticker.file_id)
            return

        # ── Case 3: Text message → quote card sticker ──
        if replied and (replied.text or replied.caption):
            messages_to_quote = [replied]

            # Multi-quote: collect N messages above
            if multi_count > 1:
                try:
                    # We already have the replied message; get messages above it
                    # Note: Telegram Bot API doesn't have a "get messages" method
                    # We can only quote the single replied message in standard mode
                    # For multi-quote, user needs to reply to the oldest message they want
                    pass  # Single quote for now, multi requires chat history access
                except Exception:
                    pass

            for msg in messages_to_quote:
                msg_text = msg.text or msg.caption or ""
                username, avatar_bytes = await _extract_author_and_avatar(context.bot, msg)

                # Check for reply context
                reply_username = None
                reply_text = None
                reply_avatar_bytes = None
                if include_reply_context and msg.reply_to_message:
                    reply_msg = msg.reply_to_message
                    reply_text = reply_msg.text or reply_msg.caption or ""
                    reply_username, reply_avatar_bytes = await _extract_author_and_avatar(context.bot, reply_msg)

                # Generate the card
                time_str = msg.date.strftime("%I:%M %p").lstrip("0") if getattr(msg, "date", None) else None
                sticker_bytes = quote_sticker.generate_quote_card(
                    username=username,
                    text=msg_text,
                    avatar_bytes=avatar_bytes,
                    reply_username=reply_username,
                    reply_text=reply_text,
                    reply_avatar_bytes=reply_avatar_bytes,
                    timestamp=time_str,
                )

                # Add to sticker pack
                pack_name = await _find_or_create_sticker_pack(
                    context.bot, user, sticker_bytes, is_video=False
                )
                sticker_set = await context.bot.get_sticker_set(pack_name)
                last_sticker = sticker_set.stickers[-1]
                await update.message.reply_sticker(sticker=last_sticker.file_id)
            return

        # ── Case 4: Sticker reply → add sticker directly (re-sticker) ──
        if replied and replied.sticker:
            # Just add the existing sticker to their pack
            sticker = replied.sticker
            tg_file = await context.bot.get_file(sticker.file_id)
            sticker_bytes = bytes(await tg_file.download_as_bytearray())
            is_video = sticker.is_video

            pack_name = await _find_or_create_sticker_pack(
                context.bot, user, sticker_bytes, is_video=is_video
            )
            sticker_set = await context.bot.get_sticker_set(pack_name)
            last_sticker = sticker_set.stickers[-1]
            await update.message.reply_sticker(sticker=last_sticker.file_id)
            return

        await update.message.reply_text(
            fmt_warning("Couldn't find quotable content in that message."),
            parse_mode="HTML"
        )

    except Exception as e:
        logger.error(f"Quote sticker error: {e}", exc_info=True)
        await update.message.reply_text(
            fmt_error(f"Failed to create sticker. Make sure you've started the bot in DM first.\n\n<i>{html.escape(str(e)[:200])}</i>"),
            parse_mode="HTML"
        )


# ─── GIF Command (/gif) ──────────────────────────────────────────────────────

def _extract_video_media(message):
    """Extract a video, animation, video note, or video document from a Telegram message."""
    if not message:
        return None
    if message.video:
        return message.video
    if message.animation:
        return message.animation
    if message.video_note:
        return message.video_note
    if message.document and message.document.mime_type:
        mime = message.document.mime_type.lower()
        if mime.startswith("video/") or mime == "image/gif":
            return message.document
    return None


def _extract_url_from_message(message) -> Optional[str]:
    """Extract a media URL from message entities, caption entities, or text."""
    if not message:
        return None

    # Check caption entities first (e.g. hyperlinked text like 'Instagram Link')
    if getattr(message, "caption_entities", None):
        for ent in message.caption_entities:
            if ent.type == "text_link" and ent.url:
                return ent.url
            elif ent.type == "url" and message.caption:
                return message.caption[ent.offset : ent.offset + ent.length]

    # Check text entities
    if getattr(message, "entities", None):
        for ent in message.entities:
            if ent.type == "text_link" and ent.url:
                return ent.url
            elif ent.type == "url" and message.text:
                return message.text[ent.offset : ent.offset + ent.length]

    # Fallback to regex in caption or text
    raw_text = (getattr(message, "caption", "") or "") + " " + (getattr(message, "text", "") or "")
    match = re.search(r'https?://[^\s<>"]+', raw_text)
    if match:
        return match.group(0)

    return None


async def gif_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Convert a replied video, attached video, or video link to GIF.

    Usage:
      /gif                     – Convert replied/attached video or link to GIF (up to 30s)
      /gif 5                   – First 5 seconds
      /gif 4 10                – Convert from 4s to 10s
      /gif <url> [start] [end] – Convert video at URL to GIF
    """
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or should_skip_message(msg):
        return

    if is_maintenance_active_for_user(user.id):
        await msg.reply_text(MAINTENANCE_NOTICE, parse_mode="HTML")
        return

    replied = msg.reply_to_message

    # Parse arguments: extract optional URL and time range
    args = context.args or []
    if not args:
        raw_text = msg.text or msg.caption or ""
        tokens = raw_text.split()
        for idx, t in enumerate(tokens):
            if t.startswith("/gif"):
                args = tokens[idx + 1:]
                break

    url_from_args = None
    time_args = []
    for arg in args:
        if arg.startswith("http://") or arg.startswith("https://"):
            if not url_from_args:
                url_from_args = arg
        else:
            time_args.append(arg)

    start_sec = 0.0
    end_sec = None
    if len(time_args) >= 2:
        try:
            start_sec = max(0.0, float(time_args[0]))
            end_sec = float(time_args[1])
            if end_sec <= start_sec:
                await msg.reply_text(fmt_error("End time must be after start time."), parse_mode="HTML")
                return
        except ValueError:
            await msg.reply_text(fmt_error("Invalid time values. Use numbers like: <code>/gif 4 10</code>"), parse_mode="HTML")
            return
    elif len(time_args) == 1:
        try:
            val = float(time_args[0])
            if val <= 0:
                await msg.reply_text(fmt_error("Duration must be greater than 0."), parse_mode="HTML")
                return
            end_sec = val
        except ValueError:
            await msg.reply_text(fmt_error("Invalid time value. Use numbers like: <code>/gif 5</code>"), parse_mode="HTML")
            return

    # Check target message for video object
    target_msg = replied if (replied and _extract_video_media(replied)) else msg
    video = _extract_video_media(target_msg)

    # Check if there is an URL in args, replied message, or current message
    fallback_url = (
        url_from_args
        or _extract_url_from_message(replied)
        or _extract_url_from_message(msg)
    )

    if not video and not fallback_url:
        await msg.reply_text(
            fmt_card("🎞️ /gif — Video to GIF",
                f"  Reply to a video or send a video/link with:\n"
                f"  <code>/gif</code> — Convert full video (up to 30s)\n"
                f"  <code>/gif 5</code> — First 5 seconds\n"
                f"  <code>/gif 4 10</code> — Convert 4s to 10s\n"
                f"  <code>/gif https://... 2 8</code> — Convert from link"
            ),
            parse_mode="HTML"
        )
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_video")

    video_source_path = None
    cleanup_temp_file = False
    cleanup_temp_dir = None

    # Step 1: If Telegram video object is available and within 20MB limit, try downloading via Telegram Bot API
    if video:
        file_size = getattr(video, "file_size", None)
        if not file_size or file_size <= 20 * 1024 * 1024:
            try:
                tg_file = await context.bot.get_file(video.file_id)
                video_bytes = bytes(await tg_file.download_as_bytearray())
                suffix = ".mp4"
                if hasattr(video, "file_name") and video.file_name:
                    _, ext = os.path.splitext(video.file_name)
                    if ext:
                        suffix = ext
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                    tmp.write(video_bytes)
                    video_source_path = tmp.name
                    cleanup_temp_file = True
            except Exception as e:
                logger.warning(f"Telegram get_file failed for /gif ({e}), checking for fallback URL...")

    # Step 2: Fallback to yt-dlp downloader if Telegram download failed or file too big or direct link
    if not video_source_path and fallback_url:
        try:
            cleanup_temp_dir = tempfile.mkdtemp()
            info = await download_generic_media(fallback_url, cleanup_temp_dir, platform_name="Video")
            video_source_path = info.get("filepath")
        except Exception as e:
            logger.error(f"URL video download failed for /gif ({fallback_url}): {e}")
            if not video:
                await msg.reply_text(
                    fmt_error(f"Failed to download video from link: {str(e)[:120]}"),
                    parse_mode="HTML"
                )
                if cleanup_temp_dir and os.path.exists(cleanup_temp_dir):
                    shutil.rmtree(cleanup_temp_dir, ignore_errors=True)
                return

    if not video_source_path or not os.path.exists(video_source_path):
        if video and getattr(video, "file_size", None) and video.file_size > 20 * 1024 * 1024:
            size_mb = video.file_size / (1024 * 1024)
            await msg.reply_text(
                fmt_error(f"Video is too large ({size_mb:.1f} MB) and exceeds Telegram's 20 MB download limit."),
                parse_mode="HTML"
            )
        else:
            await msg.reply_text(
                fmt_error("Failed to retrieve video file (Telegram file was not found or link download failed)."),
                parse_mode="HTML"
            )
        if cleanup_temp_dir and os.path.exists(cleanup_temp_dir):
            shutil.rmtree(cleanup_temp_dir, ignore_errors=True)
        return

    # Step 3: Convert video to GIF using quote_sticker.video_to_gif
    try:
        gif_bytes = await quote_sticker.video_to_gif(video_source_path, start_sec, end_sec)
    except Exception as e:
        logger.error(f"GIF execution error: {e}", exc_info=True)
        await msg.reply_text(fmt_error(f"GIF conversion error: {str(e)[:150]}"), parse_mode="HTML")
        return
    finally:
        if cleanup_temp_file and video_source_path:
            try:
                os.unlink(video_source_path)
            except OSError:
                pass
        if cleanup_temp_dir and os.path.exists(cleanup_temp_dir):
            try:
                shutil.rmtree(cleanup_temp_dir, ignore_errors=True)
            except OSError:
                pass

    if not gif_bytes:
        await msg.reply_text(
            fmt_error("Failed to convert video to GIF. The video codec may be incompatible or damaged."),
            parse_mode="HTML"
        )
        return

    # Send as animation (GIF) or fallback to document if Telegram animation fails
    try:
        await msg.reply_animation(
            animation=gif_bytes,
            reply_to_message_id=msg.message_id,
            filename="converted.gif",
        )
    except Exception as e:
        logger.warning(f"reply_animation failed ({e}), falling back to reply_document")
        try:
            await msg.reply_document(
                document=gif_bytes,
                reply_to_message_id=msg.message_id,
                filename="converted.gif",
                caption="🎞️ Converted GIF",
            )
        except Exception as e2:
            logger.error(f"Failed to send GIF: {e2}")
            await msg.reply_text(fmt_error(f"Failed to send converted GIF: {str(e2)[:120]}"), parse_mode="HTML")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Translate incoming text messages in private chats."""
    user = update.effective_user
    if not user or not update.message or not update.message.text:
        return

    if is_maintenance_active_for_user(user.id):
        await update.message.reply_text(MAINTENANCE_NOTICE, parse_mode="HTML")
        return

    if should_skip_message(update.message):
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
            translated_text = await translate_free(text, target_lang, src_lang=src_lang)
        except Exception:
            await update.message.reply_text(
                fmt_error("Translation failed. Please try again later."),
                parse_mode="HTML"
            )
            return

    response_msg = fmt_translation(src_lang, target_lang, translated_text, fallback=fallback)
    await update.message.reply_text(response_msg, parse_mode="HTML")

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
            InlineKeyboardButton(btn_txt("threads", "Threads"), callback_data="dltog:threads"),
        ],
        [
            InlineKeyboardButton(btn_txt("reddit", "Reddit"), callback_data="dltog:reddit"),
        ],
        [
            InlineKeyboardButton(mode_txt, callback_data="dltog:mode"),
        ],
        [
            InlineKeyboardButton("🔄 Reset to Defaults", callback_data="dltog:reset"),
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
        ("Threads", cfg.get("threads", True)),
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

    rows = [status_lines[i:i + 3] for i in range(0, len(status_lines), 3)]
    permissions_block = "\n".join("  •  ".join(row) for row in rows)

    return (
        f"⚙️ <b>Media Download Settings</b>{title_str}\n\n"
        f"<b>Platform Permissions:</b>\n"
        f"{permissions_block}\n\n"
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

    if action == "reset":
        reset_chat_config(chat.id)
        await query.answer("✅ Reset all media settings to default (all enabled).")
    elif action == "mode":
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
                f"• Downloads video/media from <b>YouTube, Twitter, Twitch Clips, TikTok, Instagram, Threads, and Reddit</b>\n\n"
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
    config._config_bot = application.bot
    await load_chat_configs_from_channel()

    await application.bot.set_my_commands([
        ("start", "Start the bot and see configuration"),
        ("downloads", "Configure media download permissions (Admins)"),
        ("tr", "Translate text, or an image (reply)"),
        ("target", "Set translation target language"),
        ("engine", "Switch AI / Free engine"),
        ("q", "Quote a message as a sticker"),
        ("gif", "Convert a video to GIF"),
        ("voice", "Amharic Voice Assistant"),
        ("status", "Show settings and status"),
        ("help", "Full help guide"),
        ("report", "Report a problem to admins"),
        ("setcookies", "Update YouTube cookies (DM only)"),
        ("warn", "Warn a user (Admins)"),
        ("warns", "Check warnings and lives"),
        ("unwarn", "Remove 1 warning (Admins)"),
        ("clearwarns", "Clear all warnings (Admins)"),
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

    builder = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(request)
        .post_init(post_init)
        .concurrent_updates(True)
    )

    if LOCAL_BOT_API_URL:
        base_url = LOCAL_BOT_API_URL.rstrip("/")
        if not base_url.endswith("/bot"):
            base_url = f"{base_url}/bot"
        base_file_url = base_url.replace("/bot", "/file/bot")

        # Test if Local Bot API is reachable before switching over
        is_reachable = False
        try:
            test_resp = httpx.get(f"{base_url}{TELEGRAM_BOT_TOKEN}/getMe", timeout=4.0)
            if test_resp.status_code == 200:
                is_reachable = True
            else:
                logger.warning(f"Local Bot API at {base_url} returned status {test_resp.status_code}: {test_resp.text}")
        except Exception as e:
            logger.warning(f"Local Bot API at {base_url} is currently unreachable ({e}).")

        if is_reachable:
            logger.info(f"Using custom Local Telegram Bot API: {base_url} (file url: {base_file_url})")
            builder = builder.base_url(base_url).base_file_url(base_file_url)
        else:
            logger.warning("Falling back to official Telegram Bot API (https://api.telegram.org) to keep bot online.")

    application = builder.build()

    # Register command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler(["downloads", "mediaconfig"], downloads_command))
    application.add_handler(CommandHandler("tr", tr_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("target", target_command))
    application.add_handler(CommandHandler(["languages", "langs", "settings"], languages_command))
    application.add_handler(CommandHandler("engine", engine_command))
    application.add_handler(CommandHandler("warn", warn_command))
    application.add_handler(CommandHandler("warns", warns_command))
    application.add_handler(CommandHandler(["unwarn", "rmwarn"], unwarn_command))
    application.add_handler(CommandHandler(["clearwarns", "resetwarns"], clearwarns_command))
    application.add_handler(CommandHandler(["setwarnlimit", "warnlimit", "setwarns"], set_warn_limit_command))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("promote", promote_command))
    application.add_handler(CommandHandler("demote", demote_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("setcookies", setcookies_command))
    application.add_handler(CommandHandler(["maintenance", "admin"], maintenance_command))
    application.add_handler(CommandHandler("q", q_command, block=False))
    application.add_handler(CommandHandler("gif", gif_command, block=False))
    application.add_handler(MessageHandler(filters.CaptionRegex(r"^/gif(?:\s|$)"), gif_command, block=False))
    application.add_handler(CommandHandler("voice", voice_command))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice_message, block=False))
    application.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.Document.ALL,
        setcookies_command
    ))

    # Register Settings & Media Download Button callback handlers (block=False for non-blocking UI)
    application.add_handler(CallbackQueryHandler(handle_maintenance_callback, pattern="^maint:", block=False))
    application.add_handler(CallbackQueryHandler(handle_lang_callback, pattern="^lang:", block=False))
    application.add_handler(CallbackQueryHandler(handle_downloads_callback, pattern="^dltog:", block=False))
    application.add_handler(CallbackQueryHandler(handle_pending_download_button, pattern="^dlmed:", block=False))
    application.add_handler(CallbackQueryHandler(handle_youtube_download_button, pattern="^ytdl:", block=False))

    # Register Bot Join Greeting handler
    application.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    # Register platform media handlers (non-blocking so long downloads never freeze the bot)
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(YOUTUBE_URL_PATTERN),
        handle_youtube_message,
        block=False
    ))

    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(TWITTER_URL_PATTERN),
        handle_twitter_message,
        block=False
    ))

    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(TWITCH_CLIP_PATTERN),
        handle_twitch_clip_message,
        block=False
    ))

    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(TIKTOK_URL_PATTERN),
        handle_tiktok_message,
        block=False
    ))

    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(INSTAGRAM_URL_PATTERN),
        handle_instagram_message,
        block=False
    ))

    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(THREADS_URL_PATTERN),
        handle_threads_message,
        block=False
    ))

    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(REDDIT_URL_PATTERN),
        handle_reddit_message,
        block=False
    ))

    # Register text message handler
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message, block=False))

    # Register global error handler (catches unexpected crashes and sends patching notice)
    application.add_error_handler(global_error_handler)

    # Detect deployment environment
    webhook_base = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("KOYEB_PUBLIC_URL") or os.getenv("WEBHOOK_URL")
    port = int(os.getenv("PORT", "8443"))

    if webhook_base:
        # ─── Webhook mode (Render / Koyeb / Custom) ───
        webhook_url = f"{webhook_base.rstrip('/')}/{TELEGRAM_BOT_TOKEN}"
        logger.info(f"Running in webhook mode on {webhook_base} (port {port})")
        print("\n" + "-" * 45)
        print("  Translation Bot (Webhook Mode)")
        print(f"  Listening on port {port}")
        print("-" * 45 + "\n")
        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=TELEGRAM_BOT_TOKEN,
            webhook_url=webhook_url,
            drop_pending_updates=True,
        )
    else:
        # ─── Local / other: Use polling mode ───
        logger.info("Bot is starting polling...")
        print("\n" + "-" * 45)
        print("  Translation Bot is now running!")
        print("  Press Ctrl+C to stop.")
        print("-" * 45 + "\n")
        application.run_polling(drop_pending_updates=True, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
