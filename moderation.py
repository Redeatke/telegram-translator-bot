"""
Group Moderation & Bot Administration Commands
──────────────────────────────────────────────
Handles group admin commands:
- /ban, /promote, /demote
- /report (sends issue reports to configured admins)
- /setcookies (loads YouTube cookies via chat)
- /maintenance (interactive maintenance toggle)
"""
from __future__ import annotations

import os
import re
import json
import html
import tempfile
import logging
from typing import Optional, Dict, Any, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import (
    ADMIN_USER_IDS,
    get_maintenance_mode,
    set_maintenance_mode,
    set_youtube_cookies_file,
    safe_answer_query,
    logger,
    fmt_card,
    fmt_success,
    fmt_error,
    fmt_warning,
)


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

    # 3. Check target user status
    try:
        target_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=target_user_id)
        if target_member.status in ["administrator", "creator"]:
            await update.message.reply_text(fmt_error("This user is already an administrator or owner."), parse_mode="HTML")
            return
    except Exception as e:
        logger.error(f"Error checking target member status: {e}")

    # 4. Check bot permissions
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

    # 5. Perform promotion
    try:
        await context.bot.promote_chat_member(
            chat_id=chat.id,
            user_id=target_user_id,
            can_change_info=True,
            can_delete_messages=True,
            can_invite_users=True,
            can_restrict_members=True,
            can_pin_messages=True,
            can_manage_chat=True,
            can_manage_video_chats=True,
        )
        name_str = f"<b>{target_user_name}</b> (<code>{target_user_id}</code>)" if target_user_name else f"<code>{target_user_id}</code>"
        await update.message.reply_text(
            fmt_success(f"{name_str} has been promoted to administrator!"),
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
                "  Type /report followed by a description.\n\n"
                "  <i>Example:</i>\n"
                "  <code>/report Spanish translations show weird characters</code>"
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
        f"  <b>User ID:</b> <code>{user.id}</code>\n\n"
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


async def setcookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Update YouTube cookies by sending a cookies.txt file or pasting content."""
    user = update.effective_user
    if not user:
        return

    if update.effective_chat.type != "private":
        await update.message.reply_text(
            fmt_error("This command can only be used in private chat for security."),
            parse_mode="HTML"
        )
        return

    cookie_content = None

    doc = update.message.document or (update.message.reply_to_message.document if update.message.reply_to_message else None)
    if doc:
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

    elif context.args:
        cookie_content = " ".join(context.args)

    if not cookie_content:
        await update.message.reply_text(
            fmt_card("🍪 Update YouTube Cookies",
                "Send your <code>cookies.txt</code> file to this chat,\n"
                "then reply to it with /setcookies\n\n"
                "Or paste cookie content directly:\n"
                "<code>/setcookies &lt;paste here&gt;</code>\n\n"
                "<i>Use the 'Get cookies.txt LOCALLY' browser\n"
                "extension, or run refresh_cookies.py locally.</i>"
            ),
            parse_mode="HTML"
        )
        return

    try:
        tmp_cookie_path = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
        with open(tmp_cookie_path, "w", encoding="utf-8") as f:
            f.write(cookie_content.strip())
        set_youtube_cookies_file(tmp_cookie_path)

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


async def maintenance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to toggle maintenance mode with interactive controls."""
    user = update.effective_user
    if not user or user.id not in ADMIN_USER_IDS:
        return

    if context.args:
        arg = context.args[0].lower().strip()
        if arg in ["on", "true", "enable", "1"]:
            set_maintenance_mode(True)
        elif arg in ["off", "false", "disable", "0"]:
            set_maintenance_mode(False)
        elif arg in ["toggle"]:
            set_maintenance_mode(not get_maintenance_mode())

    maint = get_maintenance_mode()
    status_badge = "🔴 ON (Maintenance Active)" if maint else "🟢 OFF (Bot is Live)"
    btn_text = "🟢 Resume Bot (Turn OFF Maintenance)" if maint else "🔴 Pause Bot (Turn ON Maintenance)"
    toggle_val = "off" if maint else "on"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(btn_text, callback_data=f"maint:toggle:{toggle_val}")],
        [InlineKeyboardButton("🔄 Refresh Status", callback_data="maint:refresh")],
    ])

    body = (
        f"  Status: <b>{status_badge}</b>\n\n"
        f"  • When <b>ON</b>: All users in groups and DMs see the maintenance/patching notice on any message or button click.\n"
        f"  • When <b>OFF</b>: Normal operation for all chats.\n"
        f"  • Admins can always use all bot features even during maintenance."
    )

    if update.callback_query:
        await safe_answer_query(update.callback_query, f"Maintenance: {status_badge}")
        try:
            await update.callback_query.edit_message_text(
                fmt_card("🛠️ Maintenance Control Panel", body),
                parse_mode="HTML",
                reply_markup=kb
            )
        except Exception:
            pass
    else:
        await update.message.reply_text(
            fmt_card("🛠️ Maintenance Control Panel", body),
            parse_mode="HTML",
            reply_markup=kb
        )
    logger.info(f"Maintenance mode status checked/set to {maint} by admin {user.id}")


async def handle_maintenance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle maintenance control panel inline button clicks."""
    query = update.callback_query
    if not query:
        return
    user = query.from_user
    if not user or user.id not in ADMIN_USER_IDS:
        await query.answer("❌ Only bot administrators can toggle maintenance.", show_alert=True)
        return

    data = query.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "toggle":
        target = parts[2] if len(parts) > 2 else ""
        if target == "on":
            set_maintenance_mode(True)
        elif target == "off":
            set_maintenance_mode(False)
        else:
            set_maintenance_mode(not get_maintenance_mode())

    await maintenance_command(update, context)


# ─── Group Warning & Lives System (/warn, /warns, /unwarn, /clearwarns) ──────

WARNS_FILE = os.path.join(os.environ.get("DATA_DIR", os.path.dirname(__file__)), "chat_warns.json")
chat_warns: Dict[str, Dict[str, Any]] = {}
DEFAULT_WARN_LIMIT = 3  # Default 3 lives (configurable between 3 and 5)


def load_chat_warns() -> None:
    """Load warnings data from local JSON file."""
    global chat_warns
    if os.path.exists(WARNS_FILE):
        try:
            with open(WARNS_FILE, "r", encoding="utf-8") as f:
                chat_warns = json.load(f)
                logger.info(f"Loaded warnings data for {len(chat_warns)} chats.")
        except Exception as e:
            logger.error(f"Failed to load chat_warns.json: {e}")
            chat_warns = {}


def save_chat_warns() -> None:
    """Save warnings data to local JSON file."""
    try:
        with open(WARNS_FILE, "w", encoding="utf-8") as f:
            json.dump(chat_warns, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save chat_warns.json: {e}")


# Initialize on import
load_chat_warns()


def get_chat_warn_limit(chat_id: int) -> int:
    """Get the warning/lives limit for a chat (between 3 and 5)."""
    cid = str(chat_id)
    return chat_warns.get(cid, {}).get("limit", DEFAULT_WARN_LIMIT)


def set_chat_warn_limit(chat_id: int, limit: int) -> None:
    """Set the warning limit for a chat (clamped between 3 and 5)."""
    cid = str(chat_id)
    if cid not in chat_warns:
        chat_warns[cid] = {"limit": limit, "users": {}}
    else:
        chat_warns[cid]["limit"] = limit
    save_chat_warns()


def get_user_warn_data(chat_id: int, user_id: int) -> dict:
    """Get warning record for a specific user in a chat."""
    cid = str(chat_id)
    uid = str(user_id)
    return chat_warns.get(cid, {}).get("users", {}).get(uid, {"count": 0, "reasons": []})


def add_user_warn(chat_id: int, user_id: int, reason: str = "") -> Tuple[int, int]:
    """Add a warning to a user. Returns (current_warn_count, max_limit)."""
    cid = str(chat_id)
    uid = str(user_id)
    if cid not in chat_warns:
        chat_warns[cid] = {"limit": DEFAULT_WARN_LIMIT, "users": {}}
    if "users" not in chat_warns[cid]:
        chat_warns[cid]["users"] = {}
    if uid not in chat_warns[cid]["users"]:
        chat_warns[cid]["users"][uid] = {"count": 0, "reasons": []}

    chat_warns[cid]["users"][uid]["count"] += 1
    if reason:
        chat_warns[cid]["users"][uid]["reasons"].append(reason)
    save_chat_warns()
    return chat_warns[cid]["users"][uid]["count"], chat_warns[cid].get("limit", DEFAULT_WARN_LIMIT)


def remove_user_warn(chat_id: int, user_id: int) -> Tuple[int, int]:
    """Remove 1 warning from a user. Returns (new_count, max_limit)."""
    cid = str(chat_id)
    uid = str(user_id)
    limit = get_chat_warn_limit(chat_id)
    if cid in chat_warns and "users" in chat_warns[cid] and uid in chat_warns[cid]["users"]:
        count = chat_warns[cid]["users"][uid].get("count", 0)
        new_count = max(0, count - 1)
        chat_warns[cid]["users"][uid]["count"] = new_count
        if new_count == 0 and "reasons" in chat_warns[cid]["users"][uid]:
            chat_warns[cid]["users"][uid]["reasons"] = []
        save_chat_warns()
        return new_count, limit
    return 0, limit


def reset_user_warns(chat_id: int, user_id: int) -> None:
    """Clear all warnings for a user in a chat."""
    cid = str(chat_id)
    uid = str(user_id)
    if cid in chat_warns and "users" in chat_warns[cid] and uid in chat_warns[cid]["users"]:
        del chat_warns[cid]["users"][uid]
        save_chat_warns()


def format_lives_bar(warns: int, max_warns: int) -> str:
    """Format visual hearts: 💔 for lost lives, ❤️ for remaining lives."""
    lost = min(warns, max_warns)
    remaining = max(0, max_warns - warns)
    return "💔 " * lost + "❤️ " * remaining


async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Warn a user. If warnings reach the group limit (3-5 lives), the user is banned.
    
    Usage:
      Reply to a message with: /warn [reason]
      Or run: /warn <user_id> [reason]
    """
    chat = update.effective_chat
    caller = update.effective_user

    if not chat or chat.type not in ["group", "supergroup"]:
        await update.message.reply_text(fmt_error("This command can only be used in groups."), parse_mode="HTML")
        return

    if not caller:
        return

    # 1. Verify caller is group admin
    try:
        caller_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=caller.id)
        if caller_member.status not in ["administrator", "creator"] and caller.id not in ADMIN_USER_IDS:
            await update.message.reply_text(fmt_error("You must be a group admin to issue warnings."), parse_mode="HTML")
            return
    except Exception as e:
        logger.error(f"Error checking caller admin status: {e}")
        await update.message.reply_text(fmt_error("Failed to verify your admin privileges."), parse_mode="HTML")
        return

    # 2. Get target user and reason
    target_user_id = None
    target_user_name = None
    reason = ""

    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        if target_user:
            target_user_id = target_user.id
            target_user_name = target_user.first_name
        if context.args:
            reason = " ".join(context.args)
    elif context.args:
        arg = context.args[0]
        if arg.isdigit():
            target_user_id = int(arg)
            if len(context.args) > 1:
                reason = " ".join(context.args[1:])
        elif arg.startswith("@"):
            await update.message.reply_text(
                fmt_error("Can't resolve usernames directly. Reply to their message or use a numeric user ID."),
                parse_mode="HTML"
            )
            return

    if not target_user_id:
        await update.message.reply_text(
            fmt_card("⚠️ /warn — Warning & Lives System",
                "  <b>Usage:</b>\n"
                "  • Reply to a user's message with <code>/warn [reason]</code>\n"
                "  • Or run <code>/warn &lt;user_id&gt; [reason]</code>\n\n"
                "  <i>Users lose 1 life per warning. Reaching maximum warnings (3-5 lives) results in an automatic ban.</i>"
            ),
            parse_mode="HTML"
        )
        return

    # 3. Prevent self-warn or bot-warn
    if target_user_id == caller.id:
        await update.message.reply_text(fmt_error("You cannot warn yourself."), parse_mode="HTML")
        return
    if target_user_id == context.bot.id:
        await update.message.reply_text(fmt_error("You cannot warn the bot."), parse_mode="HTML")
        return

    # 4. Check if target is admin/creator
    try:
        target_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=target_user_id)
        if target_member.status in ["administrator", "creator"]:
            await update.message.reply_text(fmt_error("Cannot warn administrators or group owners."), parse_mode="HTML")
            return
        if not target_user_name and target_member.user:
            target_user_name = target_member.user.first_name
    except Exception as e:
        logger.error(f"Error checking target member status: {e}")

    # 5. Add warning
    current_warns, max_warns = add_user_warn(chat.id, target_user_id, reason)
    lives_remaining = max(0, max_warns - current_warns)
    lives_bar = format_lives_bar(current_warns, max_warns)
    name_str = f"<b>{html.escape(target_user_name)}</b>" if target_user_name else f"<code>{target_user_id}</code>"
    reason_str = html.escape(reason) if reason else "No reason specified"

    # 6. Check if warn limit reached -> Ban user
    if current_warns >= max_warns:
        # Check bot ban permissions
        try:
            bot_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=context.bot.id)
            if bot_member.status not in ["administrator", "creator"] or not bot_member.can_restrict_members:
                await update.message.reply_text(
                    fmt_error("Warning limit reached, but I need <b>Restrict Members</b> permission to ban."),
                    parse_mode="HTML"
                )
                return
        except Exception as e:
            logger.error(f"Error checking bot permissions: {e}")

        try:
            await context.bot.ban_chat_member(chat_id=chat.id, user_id=target_user_id)
            reset_user_warns(chat.id, target_user_id)
            ban_body = (
                f"  👤  <b>User:</b> {name_str} (<code>{target_user_id}</code>)\n"
                f"  ⚠️  <b>Warns:</b> <b>{current_warns}/{max_warns}</b>\n"
                f"  ❤️  <b>Lives:</b> {lives_bar} (0 remaining)\n"
                f"  📝  <b>Reason:</b> <i>{reason_str}</i>\n\n"
                f"  🚫 <b>Action:</b> User has exhausted all lives and was <b>BANNED</b> from the group."
            )
            await update.message.reply_text(
                fmt_card("🚨 Warning Limit Exceeded — User Banned", ban_body),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to ban user {target_user_id} after warns: {e}")
            await update.message.reply_text(
                fmt_error(f"Failed to ban user after reaching warning limit: <code>{str(e)}</code>"),
                parse_mode="HTML"
            )
    else:
        warn_body = (
            f"  👤  <b>User:</b> {name_str} (<code>{target_user_id}</code>)\n"
            f"  ⚠️  <b>Warns:</b> <b>{current_warns}/{max_warns}</b>\n"
            f"  ❤️  <b>Lives:</b> {lives_bar} (<b>{lives_remaining}</b> remaining)\n"
            f"  📝  <b>Reason:</b> <i>{reason_str}</i>\n\n"
            f"  <i>Warning {current_warns} of {max_warns}. Reaching {max_warns} warnings will result in a permanent ban.</i>"
        )
        await update.message.reply_text(
            fmt_card("⚠️ Warning Issued", warn_body),
            parse_mode="HTML"
        )


async def warns_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check how many warnings and lives a user has."""
    chat = update.effective_chat
    caller = update.effective_user

    if not chat or chat.type not in ["group", "supergroup"]:
        await update.message.reply_text(fmt_error("This command can only be used in groups."), parse_mode="HTML")
        return

    if not caller:
        return

    target_user_id = caller.id
    target_user_name = caller.first_name

    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        if target_user:
            target_user_id = target_user.id
            target_user_name = target_user.first_name
    elif context.args and context.args[0].isdigit():
        target_user_id = int(context.args[0])
        target_user_name = None

    data = get_user_warn_data(chat.id, target_user_id)
    count = data.get("count", 0)
    reasons = data.get("reasons", [])
    max_warns = get_chat_warn_limit(chat.id)
    lives_remaining = max(0, max_warns - count)
    lives_bar = format_lives_bar(count, max_warns)
    name_str = f"<b>{html.escape(target_user_name)}</b>" if target_user_name else f"<code>{target_user_id}</code>"

    body = (
        f"  👤  <b>User:</b> {name_str} (<code>{target_user_id}</code>)\n"
        f"  ⚠️  <b>Warns:</b> <b>{count}/{max_warns}</b>\n"
        f"  ❤️  <b>Lives:</b> {lives_bar} (<b>{lives_remaining}</b> left)\n"
    )
    if reasons:
        reasons_list = "\n".join([f"    • <i>{html.escape(r)}</i>" for r in reasons[-5:]])
        body += f"\n  📝  <b>Recent Reasons:</b>\n{reasons_list}\n"

    await update.message.reply_text(
        fmt_card("🛡️ Warning Status", body),
        parse_mode="HTML"
    )


async def unwarn_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove 1 warning (restore 1 life) from a user."""
    chat = update.effective_chat
    caller = update.effective_user

    if not chat or chat.type not in ["group", "supergroup"]:
        await update.message.reply_text(fmt_error("This command can only be used in groups."), parse_mode="HTML")
        return

    if not caller:
        return

    # Verify caller is admin
    try:
        caller_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=caller.id)
        if caller_member.status not in ["administrator", "creator"] and caller.id not in ADMIN_USER_IDS:
            await update.message.reply_text(fmt_error("You must be a group admin to remove warnings."), parse_mode="HTML")
            return
    except Exception as e:
        logger.error(f"Error checking caller admin status: {e}")
        await update.message.reply_text(fmt_error("Failed to verify your admin privileges."), parse_mode="HTML")
        return

    target_user_id = None
    target_user_name = None

    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        if target_user:
            target_user_id = target_user.id
            target_user_name = target_user.first_name
    elif context.args and context.args[0].isdigit():
        target_user_id = int(context.args[0])

    if not target_user_id:
        await update.message.reply_text(
            fmt_warning("Reply to a user's message with /unwarn or use <code>/unwarn &lt;user_id&gt;</code>"),
            parse_mode="HTML"
        )
        return

    new_count, max_warns = remove_user_warn(chat.id, target_user_id)
    lives_remaining = max(0, max_warns - new_count)
    lives_bar = format_lives_bar(new_count, max_warns)
    name_str = f"<b>{html.escape(target_user_name)}</b>" if target_user_name else f"<code>{target_user_id}</code>"

    body = (
        f"  👤  <b>User:</b> {name_str} (<code>{target_user_id}</code>)\n"
        f"  ⚠️  <b>Warns:</b> <b>{new_count}/{max_warns}</b>\n"
        f"  ❤️  <b>Lives:</b> {lives_bar} (<b>{lives_remaining}</b> left)\n\n"
        f"  ✅ <i>1 warning removed (+1 life restored).</i>"
    )
    await update.message.reply_text(
        fmt_card("💚 Warning Removed", body),
        parse_mode="HTML"
    )


async def clearwarns_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all warnings for a user."""
    chat = update.effective_chat
    caller = update.effective_user

    if not chat or chat.type not in ["group", "supergroup"]:
        await update.message.reply_text(fmt_error("This command can only be used in groups."), parse_mode="HTML")
        return

    if not caller:
        return

    # Verify caller is admin
    try:
        caller_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=caller.id)
        if caller_member.status not in ["administrator", "creator"] and caller.id not in ADMIN_USER_IDS:
            await update.message.reply_text(fmt_error("You must be a group admin to clear warnings."), parse_mode="HTML")
            return
    except Exception as e:
        logger.error(f"Error checking caller admin status: {e}")
        await update.message.reply_text(fmt_error("Failed to verify your admin privileges."), parse_mode="HTML")
        return

    target_user_id = None
    target_user_name = None

    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        if target_user:
            target_user_id = target_user.id
            target_user_name = target_user.first_name
    elif context.args and context.args[0].isdigit():
        target_user_id = int(context.args[0])

    if not target_user_id:
        await update.message.reply_text(
            fmt_warning("Reply to a user's message with /clearwarns or use <code>/clearwarns &lt;user_id&gt;</code>"),
            parse_mode="HTML"
        )
        return

    reset_user_warns(chat.id, target_user_id)
    max_warns = get_chat_warn_limit(chat.id)
    name_str = f"<b>{html.escape(target_user_name)}</b>" if target_user_name else f"<code>{target_user_id}</code>"

    await update.message.reply_text(
        fmt_success(f"All warnings cleared for {name_str}! All <b>{max_warns}/{max_warns}</b> lives have been restored."),
        parse_mode="HTML"
    )


async def set_warn_limit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the maximum warning limit (number of lives, 3 to 5) for the group."""
    chat = update.effective_chat
    caller = update.effective_user

    if not chat or chat.type not in ["group", "supergroup"]:
        await update.message.reply_text(fmt_error("This command can only be used in groups."), parse_mode="HTML")
        return

    if not caller:
        return

    # Verify caller is admin
    try:
        caller_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=caller.id)
        if caller_member.status not in ["administrator", "creator"] and caller.id not in ADMIN_USER_IDS:
            await update.message.reply_text(fmt_error("You must be a group admin to configure warning limits."), parse_mode="HTML")
            return
    except Exception as e:
        logger.error(f"Error checking caller admin status: {e}")
        await update.message.reply_text(fmt_error("Failed to verify your admin privileges."), parse_mode="HTML")
        return

    if not context.args or not context.args[0].isdigit():
        current_limit = get_chat_warn_limit(chat.id)
        await update.message.reply_text(
            fmt_card("⚙️ Set Warning Limit",
                f"  Current limit: <b>{current_limit} lives</b>\n\n"
                f"  <b>Usage:</b>\n"
                f"  <code>/setwarnlimit 3</code> — Set 3 lives (default)\n"
                f"  <code>/setwarnlimit 4</code> — Set 4 lives\n"
                f"  <code>/setwarnlimit 5</code> — Set 5 lives\n\n"
                f"  <i>Valid range: 3 to 5 lives.</i>"
            ),
            parse_mode="HTML"
        )
        return

    limit = int(context.args[0])
    if limit < 3 or limit > 5:
        await update.message.reply_text(
            fmt_error("Warning limit must be between <b>3</b> and <b>5</b> lives."),
            parse_mode="HTML"
        )
        return

    set_chat_warn_limit(chat.id, limit)
    await update.message.reply_text(
        fmt_success(f"Warning limit updated! Users now have <b>{limit} lives</b> before receiving an automatic ban."),
        parse_mode="HTML"
    )
