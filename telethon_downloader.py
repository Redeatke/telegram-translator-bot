"""
Telethon Downloader Module
─────────────────────────
Allows downloading media up to 2GB directly from Telegram's MTProto servers,
bypassing the standard Telegram Bot API 20MB getFile limit.
"""

import os
import logging
import asyncio
import tempfile
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_client = None
_client_lock = asyncio.Lock()


def get_telethon_credentials() -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """Retrieve API_ID, API_HASH, and BOT_TOKEN from environment variables."""
    api_id_str = (
        os.getenv("TELEGRAM_API_ID")
        or os.getenv("API_ID")
        or os.getenv("TG_API_ID")
    )
    api_hash = (
        os.getenv("TELEGRAM_API_HASH")
        or os.getenv("API_HASH")
        or os.getenv("TG_API_HASH")
    )
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")

    api_id = None
    if api_id_str:
        try:
            api_id = int(str(api_id_str).strip())
        except ValueError:
            logger.warning(f"Invalid TELEGRAM_API_ID format: {api_id_str}")

    return (
        api_id,
        (str(api_hash).strip() if api_hash else None),
        (str(bot_token).strip() if bot_token else None),
    )


def is_telethon_available() -> bool:
    """Check if Telethon credentials (api_id, api_hash, bot_token) are configured."""
    api_id, api_hash, bot_token = get_telethon_credentials()
    return bool(api_id and api_hash and bot_token)


async def get_telethon_client():
    """Lazily initialize and connect the Telethon MTProto bot client."""
    global _client
    if _client is not None and _client.is_connected():
        return _client

    async with _client_lock:
        if _client is not None and _client.is_connected():
            return _client

        api_id, api_hash, bot_token = get_telethon_credentials()
        if not api_id or not api_hash or not bot_token:
            return None

        try:
            from telethon import TelegramClient

            session_path = os.path.join(tempfile.gettempdir(), "r3dster_telethon_bot.session")
            client = TelegramClient(session_path, api_id, api_hash)
            await client.start(bot_token=bot_token)
            _client = client
            logger.info("Telethon MTProto client connected successfully for high-capacity downloads (up to 2GB).")
            return _client
        except Exception as e:
            logger.error(f"Failed to start Telethon client: {type(e).__name__}: {e}", exc_info=True)
            return None


async def download_message_media(chat_id: int, message_id: int, output_path: str, timeout: float = 90.0) -> bool:
    """
    Download media from a Telegram message using Telethon MTProto.
    Bypasses the Bot API 20MB limit and supports files up to 2GB.
    """
    try:
        client = await get_telethon_client()
        if not client:
            logger.warning("Telethon client unavailable for download_message_media.")
            return False

        msg = await client.get_messages(chat_id, ids=message_id)
        if not msg or not msg.media:
            logger.warning(f"Telethon: no media found in message {chat_id}:{message_id}")
            return False

        res = await asyncio.wait_for(
            client.download_media(msg, file=output_path),
            timeout=timeout,
        )

        return bool(res and os.path.exists(output_path) and os.path.getsize(output_path) > 0)
    except Exception as e:
        logger.error(f"Telethon download_media failed for {chat_id}:{message_id}: {type(e).__name__}: {e}")
        return False
