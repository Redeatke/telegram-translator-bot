"""
Telethon Downloader Module
─────────────────────────
Allows downloading media up to 2GB directly from Telegram's MTProto servers,
bypassing the standard Telegram Bot API 20MB getFile limit.
"""

import os
import json
import logging
import asyncio
import tempfile
import subprocess
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_client = None
_client_lock: Optional[asyncio.Lock] = None  # lazily initialized once a loop is running


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
    global _client, _client_lock
    # Lazily create the lock the first time we're called inside a running loop
    if _client_lock is None:
        _client_lock = asyncio.Lock()
    # Narrow type for the static checker — the branch above guarantees it's set
    assert _client_lock is not None

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
            await client.connect()
            if not await client.is_user_authorized():
                await client.sign_in(bot_token=bot_token)
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
    global _client
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

    except asyncio.TimeoutError:
        # The download_media coroutine was cancelled by wait_for — the client
        # connection may be in an inconsistent state, so force a reconnect next time.
        logger.error(
            f"Telethon download_media timed out after {timeout}s for "
            f"{chat_id}:{message_id}. Resetting client for next call."
        )
        _client = None
        return False
    except Exception as e:
        logger.error(f"Telethon download_media failed for {chat_id}:{message_id}: {type(e).__name__}: {e}")
        return False


def _probe_video_metadata(file_path: str) -> Tuple[int, int, int]:
    """Return (duration_seconds, width, height) via ffprobe, or (0, 0, 0) on failure."""
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,duration:format=duration",
                "-of", "json",
                file_path,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0 or not proc.stdout:
            return 0, 0, 0
        data = json.loads(proc.stdout)
        streams = data.get("streams") or []
        fmt = data.get("format") or {}
        width = int(streams[0].get("width") or 0) if streams else 0
        height = int(streams[0].get("height") or 0) if streams else 0
        raw_dur = (streams[0].get("duration") if streams else None) or fmt.get("duration") or 0
        duration = int(float(raw_dur)) if raw_dur else 0
        return duration, width, height
    except Exception:
        return 0, 0, 0


async def upload_media_via_telethon(
    chat_id: int,
    file_path: str,
    caption: str = "",
    reply_to_message_id: Optional[int] = None,
    is_audio: bool = False,
    title: str = "",
    duration: int = 0,
    button_text: Optional[str] = None,
    button_url: Optional[str] = None,
    timeout: float = 600.0,
) -> bool:
    """Upload media up to 2GB directly via Telethon MTProto (bypassing Bot API 50MB limit)."""
    global _client
    client = await get_telethon_client()
    if not client:
        raise RuntimeError("Telethon MTProto client is unavailable for high-capacity upload.")

    from telethon import Button
    from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeVideo

    attributes = []
    if is_audio:
        attributes.append(
            DocumentAttributeAudio(
                duration=int(duration or 0),
                title=title or os.path.basename(file_path),
            )
        )
    elif file_path.lower().endswith((".mp4", ".mov", ".mkv", ".webm", ".m4v")):
        probed_dur, width, height = await asyncio.get_running_loop().run_in_executor(
            None, _probe_video_metadata, file_path
        )
        final_dur = int(duration or probed_dur or 0)
        if width > 0 and height > 0:
            attributes.append(
                DocumentAttributeVideo(
                    duration=final_dur,
                    w=width,
                    h=height,
                    supports_streaming=True,
                )
            )

    buttons = [[Button.url(button_text, button_url)]] if (button_text and button_url) else None

    try:
        entity = await client.get_input_entity(chat_id)
        await asyncio.wait_for(
            client.send_file(
                entity=entity,
                file=file_path,
                caption=caption or None,
                parse_mode="html",
                reply_to=reply_to_message_id,
                supports_streaming=not is_audio,
                attributes=attributes or None,
                buttons=buttons,
            ),
            timeout=timeout,
        )
        logger.info(
            f"Uploaded {os.path.getsize(file_path) / (1024 * 1024):.1f} MB via Telethon MTProto to chat {chat_id}"
        )
        return True
    except asyncio.TimeoutError:
        logger.error(f"Telethon upload timed out after {timeout}s for chat {chat_id}. Resetting client.")
        _client = None
        raise TimeoutError(f"Telegram MTProto upload timed out after {int(timeout)}s.")
    except Exception as e:
        logger.error(f"Telethon upload failed for chat {chat_id}: {type(e).__name__}: {e}")
        if not client.is_connected():
            _client = None
        raise
