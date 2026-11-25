"""
Telegram Translator & Media Downloader Bot
──────────────────────────────────────────
Main bot orchestrator: initializes Telegram Application, registers command and
media message handlers, and manages application lifecycle.

Submodules:
- config.py: Configuration, environment variables, in-memory state & formatting helpers
- translation.py: Free & AI translation engines, language detection & picker keyboards
- moderation.py: Group admin commands (/ban, /promote, /demote, /report, /maintenance)
- downloaders.py: YouTube, Twitch, Instagram, Threads, TikTok, Reddit media downloaders
- twitter_handler.py: Twitter/X media extraction with photo albums and captions
- quote_sticker.py: Glassmorphism quote stickers with emoji support & dynamic compact width
- card.py: Sleek dark Twitter/X cards
"""
from __future__ import annotations

import os
import re
import html
import time
import uuid
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
    save_user_configs,
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
    check_rate_limit,
    rate_limit_remaining,
    MAX_TRANSLATION_CHARS,
    safe_answer_query,
    logger,
    media_cache,
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
    LINKEDIN_URL_PATTERN,
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
    handle_linkedin_message,
    handle_pending_download_button,
    execute_youtube_download,
    download_generic_media,
    download_video_source,
)

from twitter_handler import handle_twitter_message
from voice_agent import handle_voice_message, voice_command
import quote_sticker
import chat_agent
import redster
import media_search
import music_search
import card
import telethon_downloader



async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a welcoming message when /start is issued."""
    user = update.effective_user
    first_name = user.first_name if user else "there"
    config = get_user_config(user.id if user else 0)

    target_name = COMMON_LANGUAGES.get(config["target"], config["target"].upper())
    target_flag = get_flag(config["target"])

    body = (
        f"👋 Welcome, <b>{html.escape(first_name)}</b>!\n"
        f"\n"
        f"I'm your personal translator and media assistant. Use /tr to\n"
        f"translate — reply to a message with /tr, or /tr &lt;text&gt;.\n"
        f"\n"
        f"─── ⚙️ Your Settings ───\n"
        f"\n"
        f"  {target_flag}  Target Language    <b>{target_name}</b>\n"
        f"\n"
        f"─── 🚀 Quick Start ───\n"
        f"\n"
        f"  • /tr &lt;text&gt; — translate text, or reply with /tr\n"
        f"  • /tr — translate in groups or reply to images\n"
        f"  • /ask &lt;question&gt; — chat with the bot\n"
        f"  • /pic &lt;thing&gt; — send the top 3 pictures of it\n"
        f"  • /song &lt;song&gt; by &lt;artist&gt; — send a song as MP3 (max 10 min)\n"
        f"  • redster ... — reply to a message with “redster quote this” to quote it, say “redster hello” to chat, “redster send me a dog with a red ball” for pictures, or “redster send me pound the alarm by nicki minaj” for a song\n"
        f"  • /sticker — make/add stickers from images, stickers, or text\n"
        f"  • /gif — convert video to animated GIF\n"
        f"  • Paste a video link — downloads from YouTube, TikTok, Instagram, Threads, Reddit, and LinkedIn\n"
        f"  • /target es — switch target language to Spanish\n"
        f"  • /languages — interactive language list\n"
        f"  • /help — full command list"
    )

    await update.message.reply_text(
        fmt_card("🤖 Translation Bot", body),
        parse_mode="HTML"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help with all commands and language codes."""
    top_5_langs = list(COMMON_LANGUAGES.items())[:5]
    lang_lines = "\n".join(
        [f"  {get_flag(code)}  <code>{code}</code>  {name}" for code, name in top_5_langs]
    )

    body = (
        f"─── 🤖 Commands ───\n"
        f"\n"
        f"  /start     Start the bot\n"
        f"  /help      This help page\n"
        f"  /tr        Translate text or reply\n"
        f"  /ask       Chat with the bot\n"
        f"  /pic       Send the top 3 pictures of something\n"
        f"  /song      Send a song as MP3 (e.g. /song pound the alarm by nicki minaj, max 10 min)\n"
        f"  /chat      Turn group chat on/off (group admins)\n"
        f"  /target    Set target language\n"
        f"  /languages Interactive language picker\n"
        f"  /sticker   Create / add sticker (photo, sticker, quote)\n"
        f"  /q         Quote as sticker (use <code>/q r</code> with reply, <code>/q yellow</code> or <code>/q #FFD700</code> for color)\n"
        f"  redster quote this — quote by keyword: reply to a message and say “redster quote this”, “redster quote this in yellow”, or “redster quote and add a background color of red”\n"
        f"  /gif       Convert video to GIF\n"
        f"  Links      Paste a video link to download it (YouTube, TikTok, Instagram, Threads, Reddit, LinkedIn)\n"
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
        f"─── 🌍 Languages (Top 5) ───\n"
        f"\n"
        f"{lang_lines}\n"
        f"\n"
        f"Use /languages to see all languages, or any ISO 639-1 code with /target"
    )

    await update.message.reply_text(
        fmt_card("📖 Help", body),
        parse_mode="HTML"
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display user config and status."""
    user = update.effective_user
    if not user:
        return

    config = get_user_config(user.id)
    is_premium = user.is_premium or False
    target_name = COMMON_LANGUAGES.get(config["target"], config["target"].upper())
    target_flag = get_flag(config["target"])

    premium_badge = "⭐ Yes" if is_premium else "No"
    pinger_status = f"Active ({PING_INTERVAL:.1f}s)" if AUTO_PING_ENABLED else "Disabled"
    ai_status = "Available (OpenRouter)" if has_ai else "Free (Google Translate)"

    body = (
        f"  👤  User ID       <code>{user.id}</code>\n"
        f"  💎  Premium       {premium_badge}\n"
        f"  {target_flag}  Language     <b>{target_name}</b> (<code>{config['target']}</code>)\n"
        f"  ⚡  Translation   <b>{ai_status}</b>\n"
        f"  🔔  Auto Pinger   <b>{pinger_status}</b>"
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
        save_user_configs()
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

    # If replied to media without text or caption, do nothing
    if replied and not replied.text and not replied.caption:
        if (
            replied.photo
            or replied.video
            or replied.animation
            or replied.document
            or replied.sticker
            or replied.video_note
            or replied.voice
            or replied.audio
        ):
            return

    # Extract text from replied message (text or caption)
    replied_text = None
    if replied:
        if replied.text:
            replied_text = replied.text.strip()
        elif replied.caption:
            replied_text = clean_caption_for_translation(replied.caption)

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
                f"  Reply to a message or caption with /tr\n"
                f"  or <code>/tr dutch</code> (by name or code)\n"
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

    if len(text_to_translate) > MAX_TRANSLATION_CHARS:
        await update.message.reply_text(
            fmt_error(f"That text is too long ({len(text_to_translate)} chars, max {MAX_TRANSLATION_CHARS}). Please send a shorter message."),
            parse_mode="HTML"
        )
        return

    _kind = "ai" if has_ai else "free"
    if not check_rate_limit(user.id, _kind):
        await update.message.reply_text(
            fmt_warning("You've hit the hourly translation limit. Please try again in a little while."),
            parse_mode="HTML"
        )
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    translated_text = None
    fallback = False
    aligned_pairs = []

    src_lang = detect_language_code(text_to_translate)
    is_short = len(text_to_translate.split()) <= 10

    # 1. Try AI translation if configured (short texts also get a word-by-word alignment)
    if has_ai:
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
            author_name = author_user.first_name or getattr(author_user, "username", None)
            if getattr(author_user, "last_name", None) and author_name:
                author_name = f"{author_name} {author_user.last_name}"
        elif getattr(msg, "sender_chat", None):
            author_chat = msg.sender_chat
            author_name = getattr(author_chat, "title", None) or "Chat"

    author_name = (author_name or "User").strip()
    if author_name == "User":
        logger.debug(f"Could not determine author name for message {getattr(msg, 'message_id', None)}; using 'User'")

    # 4. Fetch avatar bytes
    avatar_bytes = None
    if author_user:
        avatar_bytes = await _get_user_avatar_bytes(bot, author_user)
    elif author_chat:
        avatar_bytes = await _get_chat_avatar_bytes(bot, author_chat)

    return author_name, avatar_bytes


async def _find_or_create_sticker_pack(bot, user, sticker_input, is_video=False, emoji: Optional[str] = None) -> str:
    """Find existing pack or create a new one. Returns the pack name.
    sticker_input can be bytes (raw data) or str (existing Telegram file_id).
    Handles full packs, emoji assignment, and user isolation.
    """
    from telegram import InputSticker
    from telegram.constants import StickerFormat

    bot_me = await bot.get_me()
    bot_username = bot_me.username
    user_name = user.first_name or "User"

    sticker_format = StickerFormat.VIDEO if is_video else StickerFormat.STATIC
    emoji_list = [emoji] if emoji else ["💬"]
    input_sticker = InputSticker(
        sticker=sticker_input,
        emoji_list=emoji_list,
        format=sticker_format,
    )

    # Try packs 1, 2, 3...
    for pack_num in range(1, 100):
        pack_name = quote_sticker.get_sticker_pack_name(user_name, user.id, bot_username, pack_num, is_video=is_video)
        pack_title = quote_sticker.get_sticker_pack_title(user_name, pack_num, is_video=is_video)

        exists = False
        sticker_set = None
        try:
            sticker_set = await bot.get_sticker_set(pack_name)
            exists = True
        except Exception as set_err:
            set_err_str = str(set_err).lower()
            if "stickerset_invalid" in set_err_str or "not found" in set_err_str or "invalidtoken" in set_err_str:
                exists = False
            else:
                logger.warning(f"Error checking pack '{pack_name}': {type(set_err).__name__}: {set_err}")
                exists = False

        if exists and sticker_set:
            if len(sticker_set.stickers) >= quote_sticker.MAX_STICKERS_PER_PACK:
                # Pack is full, move to next number
                continue
            try:
                await bot.add_sticker_to_set(
                    user_id=user.id,
                    name=pack_name,
                    sticker=input_sticker,
                )
                return pack_name
            except Exception as e:
                err_str = str(e).lower()
                if "stickers_too_much" in err_str or "full" in err_str:
                    continue
                logger.error(f"Failed to add sticker to existing pack '{pack_name}': {type(e).__name__}: {e}")
                raise e
        else:
            # Create new pack
            try:
                await bot.create_new_sticker_set(
                    user_id=user.id,
                    name=pack_name,
                    title=pack_title,
                    stickers=[input_sticker],
                )
                return pack_name
            except Exception as e2:
                logger.error(f"Failed to create new sticker pack '{pack_name}': {type(e2).__name__}: {e2}")
                raise e2

    raise RuntimeError("Could not find or create a sticker pack")


async def _send_quote_card(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           msg, target_msg, include_reply_context: bool,
                           quote_bg_color=None, command_hint: str = "/q r") -> bool:
    """Build a quote card sticker from target_msg's text/caption and send it.

    Shared by /q (handle_sticker_or_quote) and the "redster quote ..."
    keyword (handle_redster_quote). Returns True when a card was sent,
    False when the target has no quotable text.
    """
    user = update.effective_user
    msg_text = (target_msg.text or target_msg.caption or "").strip()
    if not msg_text:
        return False

    username, avatar_bytes = await _extract_author_and_avatar(context.bot, target_msg)
    if username == "User":
        # No username bug was reproduced locally — the generator
        # renders "Red" correctly — but log it so a live
        # recurrence is diagnosable from Northflank logs.
        logger.warning(
            "Could not determine author name for /q target "
            f"message {getattr(target_msg, 'message_id', None)} "
            f"in chat {update.effective_chat.id}; using 'User'"
        )

    # Check for reply context (who target_msg replied to).
    # _resolve_reply_context() first uses the reply in the
    # Telegram update, then falls back to the reply-chain
    # tracker — the update alone omits the nested reply in
    # DMs, which is why "/q r white" used to come out with
    # no reply context at all.
    reply_username = None
    reply_text = None
    reply_avatar_bytes = None
    reply_context_missing = False
    if include_reply_context:
        reply_username, reply_text, reply_avatar_bytes = await _resolve_reply_context(
            context.bot, target_msg, update.effective_chat.id
        )
        if not reply_text:
            reply_context_missing = True
            logger.warning(
                "/q r requested but no reply context found for "
                f"message {getattr(target_msg, 'message_id', None)} "
                f"in chat {update.effective_chat.id}"
            )

    sticker_bytes = quote_sticker.generate_quote_card(
        username=username,
        text=msg_text,
        avatar_bytes=avatar_bytes,
        reply_username=reply_username,
        reply_text=reply_text,
        reply_avatar_bytes=reply_avatar_bytes,
        bg_color=quote_bg_color,
    )

    pack_name = await _find_or_create_sticker_pack(
        context.bot, user, sticker_bytes, is_video=False
    )
    sticker_set = await context.bot.get_sticker_set(pack_name)
    last_sticker = sticker_set.stickers[-1]
    await msg.reply_sticker(sticker=last_sticker.file_id)
    if reply_context_missing:
        await msg.reply_text(
            fmt_warning(
                "Made the quote, but I couldn't see what that message was "
                "replying to — Telegram doesn't always include the reply chain "
                "in DMs, and it wasn't in my recent-message memory. "
                f"Try {command_hint} again on a message sent after the bot restarted."
            ),
            parse_mode="HTML",
        )
    return True


async def handle_sticker_or_quote(update: Update, context: ContextTypes.DEFAULT_TYPE, is_quote_cmd: bool = False) -> None:
    """Quote a message or convert media/stickers into sticker pack.

    Usage:
      /sticker     – Convert replied image/video/sticker to sticker pack
      /q           – Quote the replied text message (or media caption) as a card sticker
      /q r         – Quote with reply context (shows who they replied to)
      /q yellow    – Quote with a yellow card background (named color or hex)
      /q #FFD700  – Quote with a custom hex-color card background
      /q r yellow  – Quote with reply context and a yellow background
      /sticker 1 3 – For video: clip from 1s to 3s as a video sticker
      /q 2 5       – Video sticker clip
    """
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return

    # Track the /q (or /sticker) command message itself, before the
    # should_skip dedup check — its reply_to_message is the quote
    # target, so this records the command in the reply chain for any
    # later /q r on it.
    _track_message_for_quote(update.effective_chat.id, msg)
    if should_skip_message(msg):
        return

    if is_maintenance_active_for_user(user.id):
        await msg.reply_text(MAINTENANCE_NOTICE, parse_mode="HTML")
        return

    # Check whether invoking command was explicitly /q or /sticker
    raw_cmd_text = (msg.text or msg.caption or "").strip()
    if raw_cmd_text.startswith("/q"):
        is_quote_cmd = True
    elif raw_cmd_text.startswith(("/sticker", "/addsticker", "/kang")):
        is_quote_cmd = False

    replied = msg.reply_to_message
    # Target message is replied message, OR current message if media was attached directly
    target_msg = replied or (
        msg if (
            msg.photo
            or msg.video
            or msg.video_note
            or msg.animation
            or msg.sticker
            or (msg.document and msg.document.mime_type and (
                msg.document.mime_type.lower().startswith("image/")
                or msg.document.mime_type.lower().startswith("video/")
            ))
        ) else None
    )

    # ── Parse arguments ──
    args = context.args or []
    if not args and (msg.text or msg.caption):
        tokens = (msg.text or msg.caption).split()
        for idx, t in enumerate(tokens):
            if t.startswith(("/sticker", "/addsticker", "/kang", "/q")):
                args = tokens[idx + 1:]
                break

    include_reply_context = False
    multi_count = 0
    video_start = None
    video_end = None
    quote_bg_color = None

    if args:
        if is_quote_cmd:
            # /q arg parsing: reply flag, numeric (video/multi-count) and
            # color tokens may appear in any order, e.g. /q yellow,
            # /q #FFD700, /q r yellow, /q yellow r. Numeric handling is
            # unchanged from before: one number = multi-count / video end,
            # two numbers = video clip start/end.
            numeric_args: List[str] = []
            color_args: List[str] = []
            for tok in args:
                low = tok.lower()
                if low in ("r", "reply", "-r"):
                    include_reply_context = True
                elif low.replace(".", "", 1).isdigit():
                    numeric_args.append(tok)
                else:
                    color_args.append(tok)
            if len(color_args) > 1:
                await msg.reply_text(
                    fmt_error(
                        "Please use only one color with /q, e.g. <code>/q yellow</code> or <code>/q #FFD700</code>."
                    ),
                    parse_mode="HTML",
                )
                return
            if color_args:
                parsed_color = quote_sticker.parse_quote_color(color_args[0])
                if parsed_color is None:
                    valid_names = ", ".join(sorted(quote_sticker.QUOTE_COLORS))
                    await msg.reply_text(
                        fmt_error(
                            f"Unknown color “{html.escape(color_args[0])}”. Try a named color "
                            f"like <code>/q yellow</code> or a hex code like <code>/q #FFD700</code>.\n"
                            f"Named colors: {html.escape(valid_names)}"
                        ),
                        parse_mode="HTML",
                    )
                    return
                quote_bg_color = parsed_color
            if len(numeric_args) == 1 and numeric_args[0].isdigit():
                num = int(numeric_args[0])
                if target_msg and (target_msg.video or target_msg.video_note or target_msg.animation):
                    video_start = 0.0
                    video_end = float(num)
                else:
                    multi_count = min(num, 10)
            elif len(numeric_args) >= 2:
                try:
                    video_start = float(numeric_args[0])
                    video_end = float(numeric_args[1])
                except ValueError:
                    pass
        elif args[0].lower() in ("r", "reply", "-r"):
            include_reply_context = True
        elif len(args) == 1 and args[0].isdigit():
            num = int(args[0])
            if target_msg and (target_msg.video or target_msg.video_note or target_msg.animation):
                video_start = 0.0
                video_end = float(num)
            else:
                multi_count = min(num, 10)
        elif len(args) >= 2:
            try:
                video_start = float(args[0])
                video_end = float(args[1])
            except ValueError:
                pass

    if not target_msg:
        await msg.reply_text(
            fmt_card("🎨 /sticker & /q — Sticker Creator",
                f"  <b>Reply to any message with:</b>\n"
                f"  <code>/sticker</code> — Convert photo/video/sticker to your pack\n"
                f"  <code>/q</code> — Quote text message or media caption as a sticker\n"
                f"  <code>/q r</code> — Quote with replied message context\n"
                f"  <code>/q yellow</code> — Quote with a yellow background\n"
                f"  <code>/q #FFD700</code> — Quote with a custom hex-color background\n"
                f"  <code>/q r yellow</code> — Quote with reply context and a color\n"
                f"\n"
                f"  <b>For videos:</b>\n"
                f"  <code>/sticker</code> — Video sticker (first 3s)\n"
                f"  <code>/sticker 1 3</code> — Video sticker (1s to 3s)\n"
                f"  <code>/q 2 5</code> — Video sticker clip"
            ),
            parse_mode="HTML"
        )
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="choose_sticker")

    try:
        # Detect media types on target message
        video_obj = target_msg.video or target_msg.video_note or target_msg.animation
        if not video_obj and target_msg.document and target_msg.document.mime_type:
            if target_msg.document.mime_type.lower().startswith("video/"):
                video_obj = target_msg.document

        is_photo = bool(target_msg.photo)
        is_doc_img = bool(
            target_msg.document and target_msg.document.mime_type
            and target_msg.document.mime_type.lower().startswith("image/")
        )
        photo_obj = (target_msg.photo[-1] if is_photo else (target_msg.document if is_doc_img else None))

        sticker_obj = target_msg.sticker
        has_media = bool(video_obj or photo_obj or sticker_obj)
        msg_text = (target_msg.text or target_msg.caption or "").strip()

        # Decision rule:
        # - /q: If there is text/caption, QUOTE it into a card sticker! Only fallback to media if no text.
        # - /sticker: If there is media, CREATE STICKER FROM MEDIA! Only fallback to quote text if no media.
        should_convert_media = (has_media and not is_quote_cmd) or (has_media and not msg_text)

        if should_convert_media:
            # ── 1. Video sticker ──
            if video_obj:
                v_size = getattr(video_obj, "file_size", None)
                if v_size and v_size > 20 * 1024 * 1024:
                    await msg.reply_text(
                        fmt_error(f"Video is too large ({v_size / (1024 * 1024):.1f} MB). Telegram's Bot API limits bot downloads to 20 MB. Please send a shorter or compressed clip."),
                        parse_mode="HTML"
                    )
                    return

                v_start = video_start if video_start is not None else 0.0
                if video_end is not None:
                    v_end = video_end
                else:
                    v_end = v_start + 3.0

                duration = v_end - v_start
                if duration <= 0:
                    await msg.reply_text(fmt_error("End time must be after start time."), parse_mode="HTML")
                    return
                if duration > 3:
                    await msg.reply_text(
                        fmt_warning(f"Video stickers can be max 3 seconds. Trimming to {v_start}s → {v_start + 3}s."),
                        parse_mode="HTML"
                    )
                    v_end = v_start + 3.0

                video_bytes = None
                # 1. Standard Bot API download (if under 20MB)
                if not v_size or v_size <= 20 * 1024 * 1024:
                    try:
                        tg_file = await context.bot.get_file(video_obj.file_id)
                        video_bytes = bytes(await tg_file.download_as_bytearray())
                    except Exception as ve:
                        logger.warning(f"Bot API video download for sticker failed ({ve}), attempting Telethon fallback...")

                # 2. Telethon MTProto download fallback (up to 2GB)
                if not video_bytes and telethon_downloader.is_telethon_available():
                    import tempfile
                    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_tt:
                        tmp_tt_path = tmp_tt.name
                    ok = await telethon_downloader.download_message_media(
                        target_msg.chat_id, target_msg.message_id, tmp_tt_path
                    )
                    if ok and os.path.exists(tmp_tt_path) and os.path.getsize(tmp_tt_path) > 0:
                        _tt_size = os.path.getsize(tmp_tt_path)
                        if _tt_size > 50 * 1024 * 1024:
                            logger.warning(f"Telethon sticker video too large ({_tt_size} bytes); refusing to load into RAM.")
                            await msg.reply_text(
                                fmt_error(f"Video is too large ({_tt_size / (1024 * 1024):.1f} MB, max 50 MB for stickers). Please send a shorter or compressed clip."),
                                parse_mode="HTML"
                            )
                            try:
                                os.unlink(tmp_tt_path)
                            except OSError:
                                pass
                            return
                        with open(tmp_tt_path, "rb") as f:
                            video_bytes = f.read()
                    try:
                        os.unlink(tmp_tt_path)
                    except OSError:
                        pass

                if not video_bytes:
                    if v_size and v_size > 20 * 1024 * 1024:
                        await msg.reply_text(
                            fmt_error(f"Video is too large ({v_size / (1024 * 1024):.1f} MB). Telegram's Bot API limits bot downloads to 20 MB. Please send a shorter or compressed clip."),
                            parse_mode="HTML"
                        )
                    else:
                        await msg.reply_text(
                            fmt_error("Failed to retrieve video for sticker."),
                            parse_mode="HTML"
                        )
                    return

                import tempfile
                with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                    tmp.write(video_bytes)
                    tmp_path = tmp.name

                try:
                    sticker_bytes = await quote_sticker.video_to_sticker(tmp_path, v_start, v_end)
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

                if not sticker_bytes:
                    await msg.reply_text(
                        fmt_error("Failed to convert video to sticker. The video may be too large or incompatible."),
                        parse_mode="HTML"
                    )
                    return

                pack_name = await _find_or_create_sticker_pack(
                    context.bot, user, sticker_bytes, is_video=True
                )
                sticker_set = await context.bot.get_sticker_set(pack_name)
                last_sticker = sticker_set.stickers[-1]
                await msg.reply_sticker(sticker=last_sticker.file_id)
                return

            # ── 2. Photo / Image sticker ──
            if photo_obj and not multi_count:
                raw_bytes = None
                # 1. Try standard Bot API download across available photo sizes (largest first)
                if is_photo and target_msg.photo:
                    for p in reversed(target_msg.photo):
                        try:
                            tg_file = await context.bot.get_file(p.file_id)
                            raw_bytes = bytes(await tg_file.download_as_bytearray())
                            if raw_bytes and len(raw_bytes) > 0:
                                break
                        except Exception as pe:
                            logger.warning(f"Bot API download failed for photo size {p.file_id}: {type(pe).__name__}: {pe}")
                elif photo_obj:
                    try:
                        tg_file = await context.bot.get_file(photo_obj.file_id)
                        raw_bytes = bytes(await tg_file.download_as_bytearray())
                    except Exception as pe:
                        logger.warning(f"Bot API download failed for doc image {photo_obj.file_id}: {type(pe).__name__}: {pe}")

                # 2. Telethon MTProto download fallback if Bot API returned 404 / InvalidToken
                if not raw_bytes and telethon_downloader.is_telethon_available():
                    import tempfile
                    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp_p:
                        tmp_p_path = tmp_p.name
                    ok = await telethon_downloader.download_message_media(
                        target_msg.chat_id, target_msg.message_id, tmp_p_path
                    )
                    if ok and os.path.exists(tmp_p_path) and os.path.getsize(tmp_p_path) > 0:
                        with open(tmp_p_path, "rb") as f:
                            raw_bytes = f.read()
                        logger.info("Successfully downloaded photo via Telethon MTProto fallback!")
                    try:
                        os.unlink(tmp_p_path)
                    except OSError:
                        pass

                if not raw_bytes:
                    await msg.reply_text(
                        fmt_error("Failed to retrieve image from Telegram. Please try sending it again or upload as an uncompressed file."),
                        parse_mode="HTML"
                    )
                    return

                sticker_bytes = quote_sticker.image_to_sticker(raw_bytes)

                pack_name = await _find_or_create_sticker_pack(
                    context.bot, user, sticker_bytes, is_video=False
                )
                sticker_set = await context.bot.get_sticker_set(pack_name)
                last_sticker = sticker_set.stickers[-1]
                await msg.reply_sticker(sticker=last_sticker.file_id)
                return

            # ── 3. Sticker reply → add to pack (fetch from another pack) ──
            if sticker_obj:
                stk = sticker_obj
                stk_emoji = stk.emoji or "💬"
                if stk.is_animated:
                    await msg.reply_text(
                        fmt_warning("⚠️ Telegram animated (.tgs vector) stickers cannot be added to standard packs.\n\nOnly static image stickers, photos, videos, and quote cards can be added."),
                        parse_mode="HTML"
                    )
                    return

                # Prefer passing stk.file_id directly to Telegram.
                # Telegram's servers already hold the file, avoiding get_file 404 / 'Not Found' errors.
                pack_name = None
                try:
                    pack_name = await _find_or_create_sticker_pack(
                        context.bot, user, stk.file_id, is_video=stk.is_video, emoji=stk_emoji
                    )
                except Exception as direct_err:
                    logger.warning(f"Direct file_id sticker copy failed ({direct_err}), falling back to download and re-encode...")
                    tg_file = await context.bot.get_file(stk.file_id)
                    raw_bytes = bytes(await tg_file.download_as_bytearray())

                    if stk.is_video:
                        import tempfile
                        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp_v:
                            tmp_v.write(raw_bytes)
                            tmp_v_path = tmp_v.name
                        try:
                            encoded_bytes = await quote_sticker.video_to_sticker(tmp_v_path, 0, 3)
                        finally:
                            if os.path.exists(tmp_v_path):
                                os.remove(tmp_v_path)
                        pack_name = await _find_or_create_sticker_pack(
                            context.bot, user, encoded_bytes or raw_bytes, is_video=True, emoji=stk_emoji
                        )
                    else:
                        sticker_bytes = quote_sticker.image_to_sticker(raw_bytes)
                        pack_name = await _find_or_create_sticker_pack(
                            context.bot, user, sticker_bytes, is_video=False, emoji=stk_emoji
                        )

                sticker_set = await context.bot.get_sticker_set(pack_name)
                last_sticker = sticker_set.stickers[-1]
                await msg.reply_sticker(sticker=last_sticker.file_id)
                return

        # ── Quote Card Sticker (from text / caption) ──
        if msg_text:
            await _send_quote_card(
                update, context, msg, target_msg,
                include_reply_context, quote_bg_color,
            )
            return

        await msg.reply_text(
            fmt_warning("Couldn't find quotable or stickify-able content in that message."),
            parse_mode="HTML"
        )

    except Exception as e:
        logger.error(f"Sticker creation error: {type(e).__name__}: {e}", exc_info=True)
        err_str = str(e).lower()
        bot_me = await context.bot.get_me()
        bot_username = bot_me.username or "R3dSter_Bot"

        # Only show the DM-start prompt for genuine bot-blocked / user-not-reachable errors.
        _dm_required_keywords = [
            "forbidden", "bot was blocked", "user_is_blocked",
            "bot can't initiate", "peer_id_invalid",
            "user not participant", "chat_write_forbidden",
            "user not found",
        ]
        if any(k in err_str for k in _dm_required_keywords):
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("👉 Start Bot in DM", url=f"https://t.me/{bot_username}?start=stickers")
            ]])
            await msg.reply_text(
                fmt_error(
                    "<b>Action Required:</b> Telegram requires you to start the bot in private chat (DM) at least once before it can create or add stickers to your pack.\n\n"
                    "Tap below to start the bot, then try again!"
                ),
                parse_mode="HTML",
                reply_markup=kb,
            )
        else:
            await msg.reply_text(
                fmt_error(f"Failed to create sticker: <i>{html.escape(f'{type(e).__name__}: {str(e)[:200]}')}</i>"),
                parse_mode="HTML"
            )


async def q_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Quote command: quotes text/caption by default, falls back to media."""
    await handle_sticker_or_quote(update, context, is_quote_cmd=True)


async def sticker_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sticker command: converts media to sticker by default, falls back to text."""
    await handle_sticker_or_quote(update, context, is_quote_cmd=False)


# ─── "redster ..." keyword + interactive chat ────────────────────────────────

async def handle_redster_quote(update: Update, context: ContextTypes.DEFAULT_TYPE, parsed: dict) -> None:
    """Handle a parsed "redster quote ..." message (see redster.py).

    The message must reply to the message to quote. Colour and reply
    context come from the parsed dict; the card itself is built by the
    same _send_quote_card() helper /q uses, so behaviour matches.
    """
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return

    target_msg = msg.reply_to_message
    if not target_msg:
        await msg.reply_text(
            fmt_warning("Reply to the message you want me to quote, then say <code>redster quote this</code>."),
            parse_mode="HTML",
        )
        return

    if parsed.get("unknown_color"):
        valid_names = ", ".join(sorted(quote_sticker.QUOTE_COLORS))
        await msg.reply_text(
            fmt_error(
                f"I don't know the color “{html.escape(str(parsed['unknown_color']))}”. Try a named color "
                f"like <code>redster quote this in yellow</code> or a hex code like "
                f"<code>redster quote this in #FFD700</code>.\n"
                f"Named colors: {html.escape(valid_names)}"
            ),
            parse_mode="HTML",
        )
        return

    _track_message_for_quote(update.effective_chat.id, msg)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="choose_sticker")
    try:
        sent = await _send_quote_card(
            update, context, msg, target_msg,
            bool(parsed.get("with_reply", False)),
            parsed.get("color"),
            command_hint="redster quote this with reply",
        )
        if not sent:
            await msg.reply_text(
                fmt_warning("Couldn't find quotable text in that message."),
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Redster quote error: {type(e).__name__}: {e}", exc_info=True)
        await msg.reply_text(
            fmt_error("Failed to make that quote. Please try again."),
            parse_mode="HTML"
        )


def _chat_enabled_for(chat) -> bool:
    """DMs: chat is always on. Groups: off by default, /chat on enables it."""
    if not chat or getattr(chat, "type", "") == "private":
        return True
    return bool(get_chat_config(chat.id).get("chat", False))


async def _do_chat_reply(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         user_text: str, msg=None, allow_broad_image: bool = False) -> None:
    """One chat turn, shared by /ask, @mention, reply-to-bot and redster chat.

    Plain text reply (no parse_mode) — AI output may contain stray
    <b> / <a> fragments that would break Telegram HTML parsing.
    Rate-limited on the separate "chat" bucket (20/hr, admins 5x).

    Image requests are handled here first, before plain chat: "send me
    a dog with a red ball" (broad when the caller already knows a
    trigger fired — redster wake, @mention, reply-to-bot) or "send me
    pictures of cats" (narrow, picture words required) goes to
    _handle_images instead of the AI. An explicit-content request is
    refused without consuming a rate-limit hit. Music requests are
    checked before images: "send me pound the alarm by nicki minaj"
    goes to _handle_music (YouTube audio download), not to images
    and not to the AI.
    """
    user = update.effective_user
    chat = update.effective_chat
    msg = msg or update.effective_message
    if not user or not chat or not msg:
        return

    text = (user_text or "").strip()
    if not text:
        await msg.reply_text(
            "Hey! What do you want to ask? Try /ask <your question> or just say “redster …”.",
        )
        return

    if not _chat_enabled_for(chat):
        await msg.reply_text(
            fmt_warning("Chat is off in this group. A group admin can turn it on with <code>/chat on</code>."),
            parse_mode="HTML",
        )
        return

    # Music request? (before images and before plain chat — music
    # words are more specific than "send me <anything>", and the
    # image parser would also match "send me <song> by <artist>".)
    music_query = music_search.parse_music_request(text, broad=allow_broad_image)
    if music_query is not None:
        if not check_rate_limit(user.id, "chat"):
            remaining = rate_limit_remaining(user.id, "chat")
            await msg.reply_text(
                fmt_warning(
                    f"You've hit the hourly chat limit — {remaining} chats left this hour "
                    f"(20/hr). Please try again in a little while."
                ),
                parse_mode="HTML",
            )
            return
        await _handle_music(update, context, music_query, msg, rate_checked=True)
        return

    # Image request? (before plain chat — and before the rate check for
    # explicit refusals, which must not consume a hit.)
    image_query = media_search.parse_image_request(text, broad=allow_broad_image)
    if image_query is not None:
        if media_search.is_explicit_query(image_query):
            await msg.reply_text(
                "I can't search for that kind of picture — try something else."
            )
            return
        if not check_rate_limit(user.id, "chat"):
            remaining = rate_limit_remaining(user.id, "chat")
            await msg.reply_text(
                fmt_warning(
                    f"You've hit the hourly chat limit — {remaining} chats left this hour "
                    f"(20/hr). Please try again in a little while."
                ),
                parse_mode="HTML",
            )
            return
        await _handle_images(update, context, image_query, msg, rate_checked=True)
        return

    if not check_rate_limit(user.id, "chat"):
        remaining = rate_limit_remaining(user.id, "chat")
        await msg.reply_text(
            fmt_warning(
                f"You've hit the hourly chat limit — {remaining} chats left this hour "
                f"(20/hr). Please try again in a little while."
            ),
            parse_mode="HTML",
        )
        return

    if chat.type == "private":
        history_key = chat_agent.history_key_for_dm(user.id)
    else:
        history_key = chat_agent.history_key_for_group(chat.id)

    await context.bot.send_chat_action(chat_id=chat.id, action="typing")
    try:
        reply = await chat_agent.chat_reply(text, history_key)
    except Exception as e:
        logger.error(f"Chat reply failed: {type(e).__name__}: {e}")
        await msg.reply_text(
            fmt_error("Sorry, I couldn't answer that just now. Please try again."),
            parse_mode="HTML",
        )
        return

    await msg.reply_text(reply, reply_to_message_id=msg.message_id)


async def _handle_images(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         query: str, msg=None, rate_checked: bool = False) -> None:
    """Search the web for `query` and send up to 3 photos as a group.

    Search-and-send only — no AI image generation. First source is
    DuckDuckGo Images (general web images, source page credited in
    the caption — these are NOT only CC-licensed), then Openverse /
    Wikimedia Commons (CC images, creator + licence credited, as CC
    licences require). Images are downloaded with the same guards as
    downloaders.py (public URL, <= 5 MB, image/* content-type).
    Sends whatever downloaded (1-3); if none, replies politely.
    Rate-limited on the "chat" bucket unless the caller already
    consumed a hit (rate_checked=True).
    """
    user = update.effective_user
    chat = update.effective_chat
    msg = msg or update.effective_message
    if not user or not chat or not msg:
        return

    query = (query or "").strip()
    if not query:
        return

    if media_search.is_explicit_query(query):
        await msg.reply_text(
            "I can't search for that kind of picture — try something else."
        )
        return

    if not _chat_enabled_for(chat):
        await msg.reply_text(
            fmt_warning("Chat is off in this group. A group admin can turn it on with <code>/chat on</code>."),
            parse_mode="HTML",
        )
        return

    if not rate_checked and not check_rate_limit(user.id, "chat"):
        remaining = rate_limit_remaining(user.id, "chat")
        await msg.reply_text(
            fmt_warning(
                f"You've hit the hourly chat limit — {remaining} chats left this hour "
                f"(20/hr). Please try again in a little while."
            ),
            parse_mode="HTML",
        )
        return

    await context.bot.send_chat_action(chat_id=chat.id, action="upload_photo")
    try:
        results = await media_search.search_images(query, count=3)
    except Exception as e:
        logger.error(f"Image search failed: {type(e).__name__}: {e}")
        results = []
    if not results:
        await msg.reply_text(
            f"I couldn't find any pictures of “{html.escape(query)}” right now — try different words.",
            parse_mode="HTML",
        )
        return

    downloaded = []  # list of (bytes, item)
    for item in results:
        data = await media_search.download_image(item["url"])
        if data:
            downloaded.append((data, item))
    if not downloaded:
        await msg.reply_text(
            f"I found pictures of “{html.escape(query)}” but couldn't download any of them — please try again.",
            parse_mode="HTML",
        )
        return

    caption_lines = [f"Here are {len(downloaded)} picture(s) of “{query}”:"]
    caption_lines += [media_search.attribution_caption(item) for _, item in downloaded]
    caption = "\n".join(caption_lines)[:1000]
    media_group = [
        InputMediaPhoto(
            media=data,
            caption=html.escape(caption) if i == 0 else None,
            parse_mode="HTML" if i == 0 else None,
        )
        for i, (data, _item) in enumerate(downloaded)
    ]
    try:
        await context.bot.send_media_group(
            chat_id=chat.id, media=media_group,
            reply_to_message_id=msg.message_id,
        )
    except Exception as e:
        logger.error(f"Sending image group failed: {type(e).__name__}: {e}")
        await msg.reply_text(
            fmt_error("Found the pictures but couldn't send them. Please try again."),
            parse_mode="HTML",
        )


async def pic_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send pictures: /pic <query>, or reply to a message with /pic."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return

    if is_maintenance_active_for_user(user.id):
        await msg.reply_text(MAINTENANCE_NOTICE, parse_mode="HTML")
        return

    query = " ".join(context.args).strip() if context.args else ""
    if not query and msg.reply_to_message:
        query = (msg.reply_to_message.text or msg.reply_to_message.caption or "").strip()

    if not query:
        await msg.reply_text(
            fmt_card("🖼️ /pic — Send pictures",
                "  Send the top 3 openly-licensed pictures for a search:\n"
                "  <code>/pic dog with a red ball</code>\n"
                "  Or just say <code>redster send me a dog with a red ball</code>.\n"
                "  Or reply to a message with /pic to search for its text."
            ),
            parse_mode="HTML",
        )
        return

    await _handle_images(update, context, query, msg=msg)


async def _handle_music(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        query: str, msg=None, rate_checked: bool = False) -> None:
    """Search YouTube for `query` and send the song as an MP3 audio file.

    Search-and-send only — no AI generation. The query is resolved to
    a YouTube URL by music_search.search_song (top result, max 10
    minutes — longer results are rejected before any download), then
    the download + upload is done entirely by the EXISTING
    downloaders.execute_youtube_download with quality="audio"
    (pytubefix fast audio engine, yt-dlp FFmpegExtractAudio fallback,
    residential relay fallback, Telethon >49MB fallback, download
    queue semaphore, temp-dir cleanup, 50MB/2000MB size limits and
    its own error messages). This function adds no download code of
    its own. Rate-limited on the "chat" bucket unless the caller
    already consumed a hit (rate_checked=True). No explicit-content
    blocklist for music — song titles are not filtered the way image
    queries are.
    """
    user = update.effective_user
    chat = update.effective_chat
    msg = msg or update.effective_message
    if not user or not chat or not msg:
        return

    query = (query or "").strip()
    if not query:
        return

    if not _chat_enabled_for(chat):
        await msg.reply_text(
            fmt_warning("Chat is off in this group. A group admin can turn it on with <code>/chat on</code>."),
            parse_mode="HTML",
        )
        return

    if not rate_checked and not check_rate_limit(user.id, "chat"):
        remaining = rate_limit_remaining(user.id, "chat")
        await msg.reply_text(
            fmt_warning(
                f"You've hit the hourly chat limit — {remaining} chats left this hour "
                f"(20/hr). Please try again in a little while."
            ),
            parse_mode="HTML",
        )
        return

    await context.bot.send_chat_action(chat_id=chat.id, action="upload_document")
    status_msg = await msg.reply_text(
        f"🎵 Looking for “{html.escape(query)}”…",
        parse_mode="HTML",
    )
    try:
        result = await music_search.search_song(query)
    except Exception as e:
        logger.error(f"Song search failed: {type(e).__name__}: {e}")
        result = {"error": "search_unavailable"}
    if isinstance(result, dict) and result.get("error") == "too_long":
        mins = int(result.get("duration_seconds") or 0) // 60
        try:
            await status_msg.edit_text(
                fmt_warning(
                    f"“{html.escape(str(result.get('title') or query))}” is too long ({mins} min). "
                    f"I can only send songs up to 10 minutes."
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    # search_unavailable means yt-dlp search raised or timed out — the
    # live failure on a blocked datacenter IP — NOT "no such song".
    # None means the search ran and found nothing. Both get a second
    # chance through a web search for the YouTube video; only when
    # that fails too do we answer, and the message says which it was.
    search_unavailable = isinstance(result, dict) and result.get("error") == "search_unavailable"
    found_but_no_link = isinstance(result, dict) and bool(result.get("video_id")) and not str(result.get("url") or "").strip()
    url = str((result or {}).get("url") or "").strip() if isinstance(result, dict) else ""
    if not url:
        try:
            url = (await music_search.find_song_via_web(query) or "").strip()
        except Exception as e:
            logger.error(f"Song web fallback failed: {type(e).__name__}: {e}")
            url = ""
        if url:
            # The 10-minute cap still applies on the fallback path:
            # yt-dlp search normally rejects long videos before any
            # download, and a web result carries no duration. When the
            # probe itself is blocked we let the downloader try — it
            # enforces its own 30-minute cap and size limits.
            try:
                duration = await music_search.probe_video_duration(url)
            except Exception:
                duration = None
            if duration and duration > music_search.MAX_SONG_SECONDS:
                mins = int(duration) // 60
                try:
                    await status_msg.edit_text(
                        fmt_warning(
                            f"“{html.escape(query)}” is too long ({mins} min). "
                            f"I can only send songs up to 10 minutes."
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                return
        else:
            if found_but_no_link:
                try:
                    await status_msg.edit_text(
                        fmt_error("I found the song but couldn't get a link for it. Please try again."),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            elif search_unavailable:
                try:
                    await status_msg.edit_text(
                        f"YouTube search isn't answering right now, so I couldn't look up “{html.escape(query)}” — please try again in a bit.",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            else:
                try:
                    await status_msg.edit_text(
                        f"I couldn't find “{html.escape(query)}” as a song right now — try the artist and song name, or /song &lt;song&gt; by &lt;artist&gt;.",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            return

    # The existing downloader owns the rest: queue slot, download,
    # size/duration checks, upload as audio, status updates and its
    # own error messages. It deletes/edits the status message itself.
    # Belt-and-braces: if it ever raises instead (it should not), the
    # user still gets an answer instead of a stuck "Looking for…".
    try:
        await execute_youtube_download(msg, url, context, status_msg=status_msg, quality="audio")
    except Exception as e:
        logger.error(f"Song download failed for {query}: {type(e).__name__}: {e}")
        try:
            await status_msg.edit_text(
                fmt_error("I found the song but couldn't send it. Please try again."),
                parse_mode="HTML",
            )
        except Exception:
            pass


async def song_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a song as MP3: /song <song> by <artist>, or reply with /song."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return

    if is_maintenance_active_for_user(user.id):
        await msg.reply_text(MAINTENANCE_NOTICE, parse_mode="HTML")
        return

    query = " ".join(context.args).strip() if context.args else ""
    if not query and msg.reply_to_message:
        query = (msg.reply_to_message.text or msg.reply_to_message.caption or "").strip()

    if not query:
        # Bare /song must always answer with usage, never silence: the
        # reply is retried as plain text if the formatted send fails
        # (e.g. the box is saturated), so a failed send cannot leave
        # the user with no visible reply.
        usage_html = fmt_card("🎵 /song — Send a song",
            "  Send a song as an MP3 audio file:\n"
            "  <code>/song pound the alarm by nicki minaj</code>\n"
            "  Or just say <code>redster send me pound the alarm by nicki minaj</code>.\n"
            "  Or reply to a message with /song to send that song.\n"
            "  1 song per request, max 10 minutes."
        )
        usage_plain = (
            "🎵 /song — Send a song\n"
            "Send a song as an MP3 audio file:\n"
            "/song pound the alarm by nicki minaj\n"
            "Or just say: redster send me pound the alarm by nicki minaj\n"
            "Or reply to a message with /song to send that song.\n"
            "1 song per request, max 10 minutes."
        )
        try:
            await msg.reply_text(usage_html, parse_mode="HTML")
        except Exception:
            try:
                await msg.reply_text(usage_plain)
            except Exception:
                logger.error("Bare /song usage reply failed twice")
        return

    await _handle_music(update, context, query, msg=msg)


async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Chat with the bot: /ask <text>, or reply to a message with /ask."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return

    if is_maintenance_active_for_user(user.id):
        await msg.reply_text(MAINTENANCE_NOTICE, parse_mode="HTML")
        return

    text = " ".join(context.args).strip() if context.args else ""
    if not text and msg.reply_to_message:
        text = (msg.reply_to_message.text or msg.reply_to_message.caption or "").strip()

    if not text:
        await msg.reply_text(
            fmt_card("💬 /ask — Chat",
                "  Ask me anything:\n"
                "  <code>/ask what is the capital of France?</code>\n"
                "  Or reply to a message with /ask to ask about it.\n"
                "  In DMs you can also just say <code>redster ...</code>.\n"
                "  In groups, mention me or reply to one of my messages."
            ),
            parse_mode="HTML",
        )
        return

    await _do_chat_reply(update, context, text, msg=msg)


async def chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle interactive chat for a group: /chat on|off (group admins only).

    In DMs chat is always on, so /chat there just explains how to chat.
    """
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

    if chat.type == "private":
        await msg.reply_text(
            fmt_card("💬 Chat",
                "  Chat is always on in DMs.\n"
                "  • <code>/ask &lt;question&gt;</code>\n"
                "  • Say <code>redster ...</code> — e.g. <code>redster hello</code>\n"
                "  • Reply to one of my messages"
            ),
            parse_mode="HTML",
        )
        return

    is_admin = user.id in ADMIN_USER_IDS
    if not is_admin:
        try:
            member = await context.bot.get_chat_member(chat_id=chat.id, user_id=user.id)
            is_admin = member.status in ("administrator", "creator")
        except Exception as e:
            logger.error(f"Admin check error in /chat: {e}")
            await msg.reply_text(
                fmt_error("Failed to verify your admin privileges. Please try again later."),
                parse_mode="HTML"
            )
            return
    if not is_admin:
        await msg.reply_text(
            fmt_error("Only group admins can change the chat setting."),
            parse_mode="HTML"
        )
        return

    arg = (context.args[0].lower() if context.args else "")
    cfg = get_chat_config(chat.id)
    if arg in ("on", "enable", "true", "yes"):
        cfg["chat"] = True
        save_chat_configs()
        await msg.reply_text(
            fmt_success("Chat is now <b>on</b> in this group. Mention me or reply to my messages to chat, or use <code>/ask</code>."),
            parse_mode="HTML"
        )
    elif arg in ("off", "disable", "false", "no"):
        cfg["chat"] = False
        save_chat_configs()
        await msg.reply_text(
            fmt_success("Chat is now <b>off</b> in this group."),
            parse_mode="HTML"
        )
    else:
        state = "on" if cfg.get("chat", False) else "off"
        await msg.reply_text(
            fmt_card("💬 Chat Settings",
                f"  Chat is currently <b>{state}</b> in this group.\n\n"
                f"  <code>/chat on</code> — enable\n"
                f"  <code>/chat off</code> — disable"
            ),
            parse_mode="HTML",
        )


_MENTION_RE = re.compile(r"@(?:r3dster_bot|redster_bot|redsterbot|redster|r3dster)\b", re.IGNORECASE)


def _strip_bot_mention(text: str) -> Optional[str]:
    """If `text` mentions the bot, return it with the mention removed.

    Returns None when the message does not mention the bot. Catches
    mentions anywhere in the text ("hey @R3dSter_Bot ..."); a leading
    mention is already handled by redster.parse_redster_message().
    """
    if not text:
        return None
    m = _MENTION_RE.search(text)
    if not m:
        return None
    return (text[:m.start()] + text[m.end():]).strip(" \t,!:.")


def _is_bot_message(msg, context) -> bool:
    """True when `msg` was sent by this bot (by username, else by bot id)."""
    sender = getattr(msg, "from_user", None)
    if not sender or not getattr(sender, "is_bot", False):
        return False
    username = (getattr(sender, "username", "") or "").lower()
    if username in {"r3dster_bot", "redster_bot", "redsterbot", "r3dster"}:
        return True
    try:
        bot_id = getattr(context.bot, "id", None)
    except Exception:
        bot_id = None
    return bool(bot_id) and getattr(sender, "id", None) == bot_id


def _is_media_message(msg) -> bool:
    """True when a Telegram message carries media: photo, video, audio,
    document, animation, video note, voice note, or sticker. A caption
    does not make it text — a captioned photo/video is still media."""
    if not msg:
        return False
    return bool(
        getattr(msg, "photo", None)
        or getattr(msg, "video", None)
        or getattr(msg, "audio", None)
        or getattr(msg, "document", None)
        or getattr(msg, "animation", None)
        or getattr(msg, "video_note", None)
        or getattr(msg, "voice", None)
        or getattr(msg, "sticker", None)
    )


# ─── GIF Command (/gif) ──────────────────────────────────────────────────────

# Cache of last link or video sent in DM per chat (chat_id -> (url, video_obj, timestamp))
_last_dm_media: Dict[int, tuple] = {}
_MAX_DM_MEDIA_ENTRIES = 200
_DM_MEDIA_TTL_SECONDS = 300  # 5 minutes, matching the freshness check in gif_command


def _track_dm_media(chat_id: int, url, vid) -> None:
    """Record the last DM media for a chat, evicting stale entries and
    capping the cache at 200 chats. Previously this dict grew forever —
    one entry per DM chat, never evicted."""
    now = time.time()
    for cid in [c for c, (_, _, ts) in _last_dm_media.items() if now - ts > _DM_MEDIA_TTL_SECONDS]:
        _last_dm_media.pop(cid, None)
    if len(_last_dm_media) >= _MAX_DM_MEDIA_ENTRIES:
        oldest = min(_last_dm_media, key=lambda c: _last_dm_media[c][2])
        _last_dm_media.pop(oldest, None)
    _last_dm_media[chat_id] = (url, vid, now)


# ─── Reply-chain tracker (for /q r) ─────────────────────────────────────────
# Why this exists: /q r shows what the quoted message was replying to.
# The live failure (2026-11-22) was: "/q r white" produced a white card
# but NO reply context and NO username, even though the colour parsing
# in the very same arg loop worked — so include_reply_context WAS set.
# The only remaining cause in handle_sticker_or_quote() is that
# target_msg.reply_to_message was empty in the update Telegram sent:
# Telegram does not reliably include the *nested* reply (the message
# the target was replying to) in a DM update, so the old code silently
# dropped the reply context. This tracker records recent messages
# (author + text + what they replied to) as they arrive, so /q r can
# resolve the chain from memory even when the update omits it.
# In-memory only, capped and TTL'd like _last_dm_media; lost on restart.
_reply_chain: Dict[tuple, dict] = {}
_MAX_REPLY_CHAIN_ENTRIES = 500
_REPLY_CHAIN_TTL_SECONDS = 3600  # 1 hour


def _track_message_for_quote(chat_id: int, msg) -> None:
    """Record one message's author/text/reply-target for later /q r lookup."""
    if not msg or getattr(msg, "message_id", None) is None:
        return
    text = (getattr(msg, "text", None) or getattr(msg, "caption", None) or "").strip()
    author = None
    author_user = getattr(msg, "from_user", None)
    if author_user:
        author = getattr(author_user, "first_name", None) or getattr(author_user, "username", None)
        if getattr(author_user, "last_name", None):
            author = f"{author} {author_user.last_name}"
    if not author:
        sender_chat = getattr(msg, "sender_chat", None)
        author = getattr(sender_chat, "title", None) if sender_chat else None
    author = (author or "User").strip()
    reply_to = getattr(msg, "reply_to_message", None)
    reply_to_id = getattr(reply_to, "message_id", None) if reply_to else None
    now = time.time()
    for key in [k for k, v in _reply_chain.items() if now - v.get("ts", 0) > _REPLY_CHAIN_TTL_SECONDS]:
        _reply_chain.pop(key, None)
    if len(_reply_chain) >= _MAX_REPLY_CHAIN_ENTRIES:
        oldest = min(_reply_chain, key=lambda k: _reply_chain[k].get("ts", 0))
        _reply_chain.pop(oldest, None)
    _reply_chain[(chat_id, msg.message_id)] = {
        "author": author,
        "text": text,
        "reply_to_id": reply_to_id,
        "ts": now,
    }


async def _track_reply_chain_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Group-1 passive handler: record every text/caption message's
    reply chain so /q r can find it later. Runs after group-0 handlers
    and never replies or interrupts anything."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    _track_message_for_quote(chat.id, msg)


async def _resolve_reply_context(bot, target_msg, chat_id: int):
    """Resolve (reply_username, reply_text, reply_avatar_bytes) for /q r.

    1. Preferred: target_msg.reply_to_message from the Telegram update.
    2. Fallback: the reply-chain tracker — what the target replied to,
       as recorded when that message arrived (covers DM updates where
       Telegram omits the nested reply).
    Returns (None, None, None) when neither source has a reply.

    Fall-through is deliberate: if the update's reply message exists
    but has no usable text (empty text/caption and no media label),
    we must still try the tracker instead of returning None at once —
    the previous code returned None here and /q r silently lost the
    reply even when the tracker knew it.
    """
    r_msg = getattr(target_msg, "reply_to_message", None)
    if r_msg:
        reply_text = getattr(r_msg, "text", None) or getattr(r_msg, "caption", None) or ""
        if not reply_text:
            if getattr(r_msg, "photo", None):
                reply_text = "📷 Photo"
            elif getattr(r_msg, "video", None) or getattr(r_msg, "animation", None) or getattr(r_msg, "video_note", None):
                reply_text = "🎥 Video"
            elif getattr(r_msg, "sticker", None):
                reply_text = f"{getattr(r_msg.sticker, 'emoji', None) or '💬'} Sticker"
            elif getattr(r_msg, "document", None):
                reply_text = "📄 File"
        if reply_text:
            reply_username, reply_avatar_bytes = await _extract_author_and_avatar(bot, r_msg)
            return reply_username, reply_text, reply_avatar_bytes
        logger.debug(
            "/q r: update reply message %s has no usable text; "
            "falling through to reply-chain tracker",
            getattr(r_msg, "message_id", None),
        )
    # Fallback: tracker lookup by (chat_id, target message_id).
    entry = _reply_chain.get((chat_id, getattr(target_msg, "message_id", None)))
    if entry and entry.get("reply_to_id") is not None:
        replied = _reply_chain.get((chat_id, entry["reply_to_id"]))
        if replied and replied.get("text"):
            return replied["author"], replied["text"], None
    logger.warning(
        "/q r: no reply context for message %s in chat %s "
        "(tracker entries=%d, tracker reply_to_id=%s, "
        "update reply_to_message present=%s)",
        getattr(target_msg, "message_id", None),
        chat_id,
        len(_reply_chain),
        entry.get("reply_to_id") if entry else None,
        r_msg is not None,
    )
    return None, None, None


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

    # Check inline keyboard buttons (e.g. 'View on X' buttons)
    if getattr(message, "reply_markup", None) and hasattr(message.reply_markup, "inline_keyboard"):
        for row in message.reply_markup.inline_keyboard:
            for btn in row:
                if getattr(btn, "url", None) and btn.url.startswith(("http://", "https://")):
                    return btn.url

    return None


async def gif_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Convert a replied video, attached video, or video link to a looping GIF animation.

    Usage:
      /gif                     – Convert replied/attached video or link to GIF (up to 10s)
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
        if arg.startswith(("http://", "https://")):
            if not url_from_args:
                url_from_args = arg
        else:
            time_args.append(arg)

    start_sec = 0.0
    duration = 10.0
    if len(time_args) >= 2:
        try:
            start_sec = max(0.0, float(time_args[0]))
            end_sec = float(time_args[1])
            if end_sec <= start_sec:
                await msg.reply_text(fmt_error("End time must be after start time."), parse_mode="HTML")
                return
            duration = min(end_sec - start_sec, 15.0)
        except ValueError:
            await msg.reply_text(fmt_error("Invalid time values. Use numbers like: <code>/gif 4 10</code>"), parse_mode="HTML")
            return
    elif len(time_args) == 1:
        try:
            duration = min(max(1.0, float(time_args[0])), 15.0)
        except ValueError:
            await msg.reply_text(fmt_error("Invalid duration. Use a number like: <code>/gif 5</code>"), parse_mode="HTML")
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

    # In private chats (DM), if no video/url found, check recently sent media in this DM
    if not video and not fallback_url and update.effective_chat.type == "private":
        recent = _last_dm_media.get(update.effective_chat.id)
        if recent:
            r_url, r_vid, r_ts = recent
            if (time.time() - r_ts) < 300:  # within last 5 minutes
                fallback_url = r_url
                video = r_vid

    if not video and not fallback_url:
        await msg.reply_text(
            fmt_card("🎞️ /gif — Video to GIF",
                "  Reply to a video or send a video/link with:\n"
                "  <code>/gif</code> — Convert to GIF (up to 10s)\n"
                "  <code>/gif 5</code> — First 5 seconds\n"
                "  <code>/gif 4 10</code> — Convert 4s to 10s\n"
                "  <code>/gif https://... 2 8</code> — Convert from link"
            ),
            parse_mode="HTML"
        )
        return

    status_msg = await msg.reply_text("🎞️ <i>Converting video to GIF...</i>", parse_mode="HTML")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_video")

    with tempfile.TemporaryDirectory() as tmp_dir:
        input_video_path = None

        # 1. In-memory cache check
        if video:
            cached_bytes = media_cache.get(f"vid:{video.file_id}")
            if not cached_bytes and target_msg:
                cached_bytes = media_cache.get(f"msg_vid:{target_msg.chat_id}:{target_msg.message_id}")
            if cached_bytes:
                input_video_path = os.path.join(tmp_dir, "input.mp4")
                with open(input_video_path, "wb") as f:
                    f.write(cached_bytes)

        # 2. Telegram download
        download_err_detail = None
        if not input_video_path and video:
            file_size = getattr(video, "file_size", None)

            # 2a. If under 20MB, try standard Bot API get_file first
            if not file_size or file_size <= 20 * 1024 * 1024:
                try:
                    tg_file = await asyncio.wait_for(context.bot.get_file(video.file_id), timeout=45.0)
                    input_video_path = os.path.join(tmp_dir, "input.mp4")
                    try:
                        await asyncio.wait_for(tg_file.download_to_drive(custom_path=input_video_path), timeout=60.0)
                    except Exception as d_err:
                        logger.warning(f"download_to_drive failed ({d_err}), trying download_as_bytearray fallback...")
                        b_data = bytes(await asyncio.wait_for(tg_file.download_as_bytearray(), timeout=60.0))
                        with open(input_video_path, "wb") as f:
                            f.write(b_data)

                    if os.path.exists(input_video_path) and os.path.getsize(input_video_path) > 0:
                        try:
                            with open(input_video_path, "rb") as f:
                                media_cache.set(f"vid:{video.file_id}", f.read())
                        except Exception:
                            pass
                    else:
                        input_video_path = None
                except Exception as e:
                    download_err_detail = f"{type(e).__name__}: {e}"
                    logger.warning(f"/gif Bot API download failed: {type(e).__name__}: {e} | file_size={file_size}")
                    input_video_path = None

            # 2b. Telethon MTProto download fallback (up to 2GB) when > 20MB or when Bot API download failed
            if not input_video_path and target_msg:
                if telethon_downloader.is_telethon_available():
                    try:
                        tt_path = os.path.join(tmp_dir, "input.mp4")
                        ok = await telethon_downloader.download_message_media(
                            chat_id=target_msg.chat_id,
                            message_id=target_msg.message_id,
                            output_path=tt_path,
                            timeout=90.0,
                        )
                        if ok and os.path.exists(tt_path) and os.path.getsize(tt_path) > 0:
                            input_video_path = tt_path
                            logger.info(f"/gif successfully retrieved {file_size or 'unknown'} byte video via Telethon MTProto!")
                        else:
                            download_err_detail = "Telethon MTProto download did not produce a valid file"
                    except Exception as t_err:
                        download_err_detail = f"Telethon download error: {t_err}"
                        logger.warning(f"/gif Telethon download failed: {t_err}")
                elif file_size and file_size > 20 * 1024 * 1024:
                    download_err_detail = f"Video is {file_size / (1024 * 1024):.1f} MB (exceeds Telegram Bot API's 20 MB limit)"

        # 3. Fallback URL download (Twitter, Instagram, TikTok, Reddit, YouTube, Direct)
        if not input_video_path and fallback_url:
            try:
                input_video_path = await asyncio.wait_for(
                    download_video_source(fallback_url, tmp_dir),
                    timeout=60.0
                )
            except Exception as e:
                download_err_detail = f"URL download failed: {e}"
                logger.error(f"URL video download failed for /gif ({fallback_url}): {e}")
                input_video_path = None

        if not input_video_path or not os.path.exists(input_video_path):
            if download_err_detail and "exceeds" in download_err_detail:
                err_msg = (
                    f"<b>Video is too large:</b> {download_err_detail}.\n\n"
                    "Telegram's official Bot API server enforces a strict <b>20 MB</b> download limit for bots.\n"
                    "💡 <i>Tip: Send the video with Telegram compression enabled, or use a web link (Twitter, Instagram, etc.).</i>"
                )
            elif download_err_detail:
                err_msg = f"Failed to retrieve video: <i>{html.escape(download_err_detail)}</i>"
            else:
                err_msg = "Failed to retrieve video. Ensure the video is under 20MB or the link is publicly accessible."
            try:
                await status_msg.edit_text(fmt_error(err_msg), parse_mode="HTML")
            except Exception:
                await msg.reply_text(fmt_error(err_msg), parse_mode="HTML")
            return

        # 4. Convert video to Animation (clean H.264 silent MP4 / GIF)
        output_anim_path = os.path.join(tmp_dir, "animation.mp4")

        err_detail = None
        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_video")
            success, err_detail = await quote_sticker.convert_video_to_animation(
                input_video_path,
                output_anim_path,
                start_sec=start_sec,
                duration=duration,
            )
        except Exception as e:
            logger.error(f"Animation conversion error: {e}", exc_info=True)
            success = False
            err_detail = str(e)

        if not success or not os.path.exists(output_anim_path) or os.path.getsize(output_anim_path) == 0:
            err_snippet = f":\n<code>{html.escape(err_detail[-250:])}</code>" if err_detail else ""
            try:
                await status_msg.edit_text(
                    fmt_error(f"Failed to convert video to GIF{err_snippet}"),
                    parse_mode="HTML"
                )
            except Exception:
                pass
            return

        # 5. Send as native Telegram Animation (plays muted with GIF badge)
        anim_sent = False
        try:
            with open(output_anim_path, "rb") as anim_file:
                await msg.reply_animation(
                    animation=anim_file,
                    caption="Here is your GIF clip!",
                    reply_to_message_id=msg.message_id,
                )
            anim_sent = True
        except Exception as e:
            logger.warning(f"reply_animation failed ({e}), falling back to video/document")
            try:
                with open(output_anim_path, "rb") as anim_file:
                    await msg.reply_video(
                        video=anim_file,
                        caption="Here is your GIF clip!",
                        reply_to_message_id=msg.message_id,
                        supports_streaming=True,
                    )
                anim_sent = True
            except Exception as e2:
                try:
                    with open(output_anim_path, "rb") as anim_file:
                        await msg.reply_document(
                            document=anim_file,
                            caption="Here is your GIF clip!",
                            reply_to_message_id=msg.message_id,
                        )
                    anim_sent = True
                except Exception as e3:
                    logger.error(f"Failed to send GIF: {e3}")
                    try:
                        await status_msg.edit_text(fmt_error(f"Failed to send GIF: {str(e3)[:120]}"), parse_mode="HTML")
                    except Exception:
                        pass
                    return

        if anim_sent:
            try:
                await status_msg.delete()
            except Exception:
                pass

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plain (non-command) text messages.

    Plain text is NOT auto-translated — not in DMs, not anywhere.
    Translation is explicit only, via /tr (see tr_command).
    Plain text does trigger, in order: (1) the "redster ..." keyword
    (quote or chat), (2) an @mention of the bot, (3) a reply to one of
    the bot's own PLAIN-TEXT messages — all via chat_agent (see
    _do_chat_reply) — and (4) a bare music request ("send me the song
    hello by adele") or a bare image request ("send me pictures of
    cats"), routed to music / images by _do_chat_reply. A reply to a
    media message the bot sent (photo/video/song/sticker) does NOT
    auto-chat: users reply to those to tag friends or talk among
    themselves (2026-11-25). Anything else only tracks URLs sent
    in a private chat, so /gif can reuse the last DM link or video
    (via _track_dm_media / handle_dm_media_tracker).
    """
    user = update.effective_user
    if not user or not update.message or not update.message.text:
        return

    # Track the reply chain HERE, in group 0 — not only in the group-1
    # _track_reply_chain_handler. PTB runs only ONE handler per group,
    # and in DMs the group-1 DM-media tracker matches plain text first,
    # so the group-1 reply tracker never runs there (verified against
    # PTB 21.11: DM text -> group 1 runs handle_dm_media_tracker only).
    # handle_message is the group-0 TEXT handler, so it runs for every
    # plain text message in every chat. Track before the maintenance /
    # should_skip early returns, so those never stop tracking.
    _track_message_for_quote(update.effective_chat.id, update.message)

    if is_maintenance_active_for_user(user.id):
        await update.message.reply_text(MAINTENANCE_NOTICE, parse_mode="HTML")
        return

    if should_skip_message(update.message):
        return

    if update.message.text.startswith("/"):
        return

    text = update.message.text

    # 1. "redster ..." keyword — quote takes priority over chat.
    #    (Also handles a leading "@R3dSter_Bot ..." mention.)
    parsed = redster.parse_redster_message(text)
    if parsed is not None:
        if parsed["intent"] == "quote":
            await handle_redster_quote(update, context, parsed)
        else:
            await _do_chat_reply(update, context, parsed.get("chat_text") or "", msg=update.message, allow_broad_image=True)
        return

    # 2. @mention of the bot anywhere else in the text chats with the remainder.
    mention_text = _strip_bot_mention(text)
    if mention_text is not None:
        await _do_chat_reply(update, context, mention_text, msg=update.message, allow_broad_image=True)
        return

    # 3. Replying to one of the bot's own messages chats with the text —
    #    UNLESS the bot message is media. Users reply to a photo/video/
    #    song/sticker the bot sent to tag a friend or chat among
    #    themselves; the bot jumping in with the AI there is noise, so
    #    those replies stay silent (2026-11-25, Red: "only stop the
    #    auto-reply"). Replies to the bot's plain-text messages still
    #    chat, and "redster ..." / @mentions (rules 1–2 above) still
    #    chat even on media replies.
    replied = update.message.reply_to_message
    if replied and _is_bot_message(replied, context) and not _is_media_message(replied):
        await _do_chat_reply(update, context, text, msg=update.message, allow_broad_image=True)
        return

    # 4. Bare music request ("send me the song hello by adele") or
    #    bare image request ("send me pictures of cats") — triggers in
    #    themselves, so they chat (and _do_chat_reply routes them to
    #    music / images). Music is checked first: a song request must
    #    never fall through to the image parser. Plain text with no
    #    trigger still does nothing below this.
    if music_search.parse_music_request(text):
        await _do_chat_reply(update, context, text, msg=update.message)
        return

    if media_search.parse_image_request(text):
        await _do_chat_reply(update, context, text, msg=update.message)
        return

    # Track URLs sent in a private chat for /gif, then stop — no translation.
    text_lower = update.message.text.lower()
    if "http://" in text_lower or "https://" in text_lower:
        if update.effective_chat.type == "private":
            extracted_url = _extract_url_from_message(update.message)
            if extracted_url:
                _track_dm_media(update.effective_chat.id, extracted_url, None)
        return

    # Plain text in DMs (and elsewhere) does nothing. Use /tr to translate.
    return


async def handle_dm_media_tracker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Track videos, animations, and media in private chats so /gif commands can be sent without replying."""
    if not update.effective_message or update.effective_chat.type != "private":
        return
    msg = update.effective_message
    # Also track the reply chain here: this handler WINS group 1 in DMs
    # (PTB runs only one handler per group, and this one is registered
    # before _track_reply_chain_handler), so it is the only group-1
    # handler that runs for DM text/captions. Without this call, /q r
    # in DMs never saw any tracked messages.
    _track_message_for_quote(update.effective_chat.id, msg)
    vid = _extract_video_media(msg)
    url = _extract_url_from_message(msg)
    if vid or url:
        _track_dm_media(update.effective_chat.id, url, vid)


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
            InlineKeyboardButton(btn_txt("linkedin", "LinkedIn"), callback_data="dltog:linkedin"),
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
        ("LinkedIn", cfg.get("linkedin", True)),
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
            await update.message.reply_text(
                fmt_error("Failed to verify your admin privileges. Please try again later."),
                parse_mode="HTML"
            )
            return

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

    # Allowlist: a crafted dltog:<anything> callback must not create
    # arbitrary keys in the persisted chat config.
    _ALLOWED_DL_ACTIONS = {
        "youtube", "twitter", "twitch", "tiktok", "instagram", "threads",
        "reddit", "linkedin", "mode", "reset", "close", "menu",
    }
    if action not in _ALLOWED_DL_ACTIONS:
        await query.answer("❌ Unknown setting.", show_alert=True)
        return

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
                f"• Translate with /tr — reply to a message or use /tr &lt;text&gt;\n"
                f"• Downloads video/media from <b>YouTube, Twitter, Twitch Clips, TikTok, Instagram, Threads, Reddit, and LinkedIn</b>\n\n"
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
        ("tr", "Translate text or reply"),
        ("target", "Set translation target language"),
        ("sticker", "Add image/sticker to your pack (or /q)"),
        ("q", "Quote a message as a sticker"),
        ("gif", "Convert a video to GIF"),
        ("voice", "Amharic Voice Assistant"),
        ("status", "Show settings and status"),
        ("help", "Full help guide"),
        ("report", "Report a problem to admins"),
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
        "read_timeout": 90.0,
        "write_timeout": 90.0,
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
    application.add_handler(CommandHandler("warn", warn_command))
    application.add_handler(CommandHandler("warns", warns_command))
    application.add_handler(CommandHandler(["unwarn", "rmwarn"], unwarn_command))
    application.add_handler(CommandHandler(["clearwarns", "resetwarns"], clearwarns_command))
    application.add_handler(CommandHandler(["setwarnlimit", "warnlimit", "setwarns"], set_warn_limit_command))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("promote", promote_command))
    application.add_handler(CommandHandler("demote", demote_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler(["maintenance", "admin"], maintenance_command))
    application.add_handler(CommandHandler("q", q_command, block=False))
    application.add_handler(CommandHandler(["sticker", "addsticker", "kang"], sticker_command, block=False))
    application.add_handler(MessageHandler(filters.CaptionRegex(r"^/q(?:\s|$)"), q_command, block=False))
    application.add_handler(MessageHandler(filters.CaptionRegex(r"^/(?:sticker|addsticker|kang)(?:\s|$)"), sticker_command, block=False))
    application.add_handler(CommandHandler("gif", gif_command, block=False))
    application.add_handler(MessageHandler(filters.CaptionRegex(r"^/gif(?:\s|$)"), gif_command, block=False))
    application.add_handler(CommandHandler("voice", voice_command))
    application.add_handler(CommandHandler("ask", ask_command))
    application.add_handler(CommandHandler("chat", chat_command))
    application.add_handler(CommandHandler("pic", pic_command, block=False))
    application.add_handler(CommandHandler("song", song_command, block=False))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice_message, block=False))

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

    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(LINKEDIN_URL_PATTERN),
        handle_linkedin_message,
        block=False
    ))

    # Register text message handler
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message, block=False))

    # Register DM media tracker in group 1 (passively tracks media/links in private chats for /gif without interrupting group 0 handlers)
    application.add_handler(
        MessageHandler(
            (filters.VIDEO | filters.ANIMATION | filters.ATTACHMENT | filters.TEXT) & filters.ChatType.PRIVATE,
            handle_dm_media_tracker,
            block=False
        ),
        group=1
    )

    # Register reply-chain tracker in group 1 (passively records every
    # text/caption message's author + reply target in all chats, so
    # /q r can resolve reply context even when Telegram's update omits
    # the nested reply_to_message — see _resolve_reply_context).
    # NOTE: do NOT rely on this handler alone. PTB runs only ONE
    # handler per group, and in DMs handle_dm_media_tracker (above)
    # matches plain text first, so this handler never runs there.
    # Reply-chain tracking therefore also happens inside
    # handle_message (group 0) and handle_dm_media_tracker, which
    # are the handlers that actually win for DM text.
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            _track_reply_chain_handler,
            block=False
        ),
        group=1
    )

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
