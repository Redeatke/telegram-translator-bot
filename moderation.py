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
import html
import tempfile
import logging
from typing import Optional

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
