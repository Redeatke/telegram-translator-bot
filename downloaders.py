"""
Media Downloaders Module
────────────────────────
Handles extraction, queuing, downloading, and uploading of media from:
- YouTube (direct yt-dlp + residential relay API fallback + pytubefix)
- Twitch clips
- Instagram (photos, reels, carousels)
- Threads (videos, photos)
- TikTok
- Reddit (videos, galleries)
- Generic yt-dlp supported platforms
"""
from __future__ import annotations

import os
import re
import html
import asyncio
import tempfile
import uuid
import time
import logging
from typing import Optional, List, Dict, Any, Tuple

import httpx
import yt_dlp
import yt_dlp.plugins
yt_dlp.plugins.load_plugins = lambda *a, **k: None
from yt_dlp.extractor.instagram import InstagramBaseIE
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo, InputFile
from telegram.ext import ContextTypes

from config import (
    IS_TERMUX,
    YOUTUBE_COOKIES_FILE,
    YOUTUBE_PROXY,
    YOUTUBE_RELAY_URL,
    LOCAL_BOT_API_URL,
    MAX_UPLOAD_SIZE_MB,
    MAX_VIDEO_DURATION,
    should_skip_message,
    safe_answer_query,
    is_maintenance_active_for_user,
    MAINTENANCE_NOTICE,
    get_chat_config,
    is_downloader_enabled,
    get_download_mode,
    pending_downloads,
    store_pending_download,
    fmt_card,
    fmt_error,
    fmt_warning,
    fmt_success,
    classify_media_error,
    YOUTUBE_URL_PATTERN,
    TWITCH_CLIP_PATTERN,
    TIKTOK_URL_PATTERN,
    INSTAGRAM_URL_PATTERN,
    THREADS_URL_PATTERN,
    THREADS_CANONICAL_PATTERN,
    REDDIT_URL_PATTERN,
    logger,
)

# Patch Instagram extractor for photo carousels
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

async def download_youtube_via_relay(url: str, output_dir: str, quality=720) -> dict:
    """Download a YouTube video via the home relay API. The relay runs on a
    residential IP and streams the file back to us."""
    relay_auth = os.getenv("RELAY_AUTH", "").strip()
    headers = {"Content-Type": "application/json"}
    if relay_auth:
        headers["Authorization"] = f"Bearer {relay_auth}"

    # ngrok free tier requires this header to skip the browser warning page
    headers["ngrok-skip-browser-warning"] = "true"

    logger.info(f"Requesting YouTube relay download: {url} via {YOUTUBE_RELAY_URL} (quality={quality})")

    async with httpx.AsyncClient(timeout=300) as client:
        # Step 1: Ask the relay to download the video
        resp = await client.post(
            f"{YOUTUBE_RELAY_URL}/download",
            json={"url": url, "quality": quality},
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            raise Exception(f"Relay error: {data['error']}")

        download_path = data["download_url"]
        title = data.get("title", "Video")
        duration = data.get("duration", 0)

        # Step 2: Stream the file from the relay to local disk in 128KB chunks (zero RAM overhead)
        logger.info(f"Streaming file from relay: {download_path} ({data.get('file_size', 0)} bytes)")
        local_path = os.path.join(output_dir, data["filename"])

        async with client.stream("GET", f"{YOUTUBE_RELAY_URL}{download_path}", headers=headers) as file_resp:
            file_resp.raise_for_status()
            with open(local_path, "wb") as f:
                async for chunk in file_resp.aiter_bytes(chunk_size=128 * 1024):
                    f.write(chunk)

        if not os.path.exists(local_path) or os.path.getsize(local_path) == 0:
            raise Exception("Relay returned empty file")

        logger.info(f"Relay download complete: {title} -> {local_path}")
        return {
            "filepath": local_path,
            "title": title,
            "duration": duration,
        }


async def _download_youtube_local(url: str, output_dir: str, quality=720) -> dict:
    """Download a YouTube video directly on the local host with optimized mobile clients."""
    filename = f"{uuid.uuid4().hex}"
    output_template = os.path.join(output_dir, f"{filename}.%(ext)s")

    loop = asyncio.get_running_loop()

    # Adapt resource usage to the environment
    _concurrent_frags = 2 if IS_TERMUX else 4
    _sock_timeout = 15 if IS_TERMUX else 20
    _strategy_timeout = 60 if IS_TERMUX else 120

    def _run_single_strategy(opts, url, strategy_num):
        """Run a single yt-dlp strategy with a hard per-strategy timeout."""
        import concurrent.futures
        def _inner():
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filepath = ydl.prepare_filename(info)
                if not os.path.exists(filepath):
                    base = os.path.splitext(filepath)[0]
                    for ext in ['.mp3', '.m4a', '.mp4', '.webm', '.mkv']:
                        if os.path.exists(base + ext):
                            filepath = base + ext
                            break
                if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                    return {
                        'filepath': filepath,
                        'title': info.get('title', 'Video'),
                        'duration': info.get('duration', 0),
                    }
                return None

        # Use a dedicated thread with a timeout guard
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_inner)
            try:
                return fut.result(timeout=_strategy_timeout)
            except concurrent.futures.TimeoutError:
                if fut.done() and not fut.exception():
                    res = fut.result()
                    if res:
                        return res
                logger.warning(f"yt-dlp strategy {strategy_num} timed out after {_strategy_timeout}s for {url}")
                raise Exception(f"Strategy {strategy_num} timed out after {_strategy_timeout}s")

    def _download():
        logger.info(f"Downloading YouTube media: {url} (quality={quality})...")
        if IS_TERMUX:
            logger.info("Termux environment detected — using lightweight download settings.")

        is_audio = str(quality).lower() in ("audio", "mp3")

        # ─── OPTION A: MP3 Audio Download (Dedicated Single Strategy) ───
        if is_audio:
            logger.info(f"Downloading YouTube audio: {url}...")
            # Pytubefix fast audio engine (strictly selects original/default audio track)
            if has_pytubefix:
                try:
                    logger.info(f"Using pytubefix fast audio engine for {url}...")
                    yt = PytubeFixYouTube(url)
                    audio_streams = yt.streams.filter(only_audio=True)
                    stream = None
                    if audio_streams:
                        orig_candidates = [s for s in audio_streams if getattr(s, 'is_default_audio_track', False)]
                        if not orig_candidates:
                            orig_candidates = [s for s in audio_streams if 'original' in str(getattr(s, 'audio_track_name_regionalized', '') or '').lower()]
                        if not orig_candidates:
                            orig_candidates = [s for s in audio_streams if str(getattr(s, 'audio_track_language_id', '') or '').lower().startswith('en')]
                        candidates = orig_candidates if orig_candidates else list(audio_streams)
                        candidates.sort(key=lambda s: int(str(s.abr or 0).replace('kbps', '') or 0) if str(s.abr or 0).replace('kbps', '').isdigit() else 0, reverse=True)
                        stream = candidates[0]
                    if stream:
                        fp = stream.download(output_path=output_dir, filename=f"{filename}.mp3")
                        if os.path.exists(fp) and os.path.getsize(fp) > 0:
                            return {
                                'filepath': fp,
                                'title': yt.title or 'Audio',
                                'duration': yt.length or 0,
                            }
                except Exception as pe:
                    logger.warning(f"pytubefix audio engine failed: {pe}, falling back to yt-dlp...")

            # Fallback yt-dlp audio strategy (clean, mobile client, strictly selects original audio)
            ydl_opts = {
                'outtmpl': output_template,
                'format': 'bestaudio[format_note*=original]/bestaudio[language=original]/bestaudio[language=en]/bestaudio/best',
                'format_sort': ['lang:original', 'ext:m4a:10', 'quality'],
                'http_headers': {'Accept-Language': 'en-US,en;q=0.9'},
                'compat_opts': ['no-plugins'],
                'source_address': '0.0.0.0',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'extractor_args': {'youtube': {'player_client': ['android', 'ios']}},
                'concurrent_fragment_downloads': _concurrent_frags,
                'socket_timeout': _sock_timeout,
                'retries': 1,
                'extractor_retries': 0,
                'quiet': not os.getenv('YT_DEBUG'),
                'nocheckcertificate': True,
            }
            if YOUTUBE_PROXY:
                ydl_opts['proxy'] = YOUTUBE_PROXY
            if YOUTUBE_COOKIES_FILE and os.path.exists(YOUTUBE_COOKIES_FILE):
                ydl_opts['cookiefile'] = YOUTUBE_COOKIES_FILE

            result = _run_single_strategy(ydl_opts, url, 1)
            if result:
                return result
            raise Exception("Failed to download YouTube audio.")

        # ─── OPTION B: Video Download (Dedicated Single Strategy) ───
        try:
            q_val = int(quality)
        except (ValueError, TypeError):
            q_val = 720

        logger.info(f"Downloading YouTube video ({q_val}p): {url}...")
        # Prioritize H.264 video + AAC audio; automatically muxes to standard AAC so audio plays on iOS/Android/Telegram
        fast_format = (
            f'bestvideo[height<={q_val}][vcodec^=avc]+bestaudio[format_note*=original][acodec^=mp4a]/'
            f'bestvideo[height<={q_val}][vcodec^=avc]+bestaudio[acodec^=mp4a]/'
            f'bestvideo[height<={q_val}][ext=mp4]+bestaudio[ext=m4a]/'
            f'bestvideo[height<={q_val}]+bestaudio/'
            f'bestvideo[width<={q_val}]+bestaudio/'
            f'best[height<={q_val}][ext=mp4]/'
            f'best[height<={q_val}]/'
            f'best'
        )
        ydl_opts = {
            'outtmpl': output_template,
            'merge_output_format': 'mp4',
            'format': fast_format,
            'format_sort': ['lang:original', 'res', 'fps'],
            'http_headers': {'Accept-Language': 'en-US,en;q=0.9'},
            'compat_opts': ['no-plugins'],
            'source_address': '0.0.0.0',
            'postprocessor_args': {'merger': ['-c:v', 'copy', '-c:a', 'aac']},
            'extractor_args': {'youtube': {'player_client': ['android', 'ios']}},
            'concurrent_fragment_downloads': _concurrent_frags,
            'socket_timeout': _sock_timeout,
            'retries': 1,
            'extractor_retries': 0,
            'quiet': not os.getenv('YT_DEBUG'),
            'verbose': bool(os.getenv('YT_DEBUG')),
            'nocheckcertificate': True,
        }
        if YOUTUBE_PROXY:
            ydl_opts['proxy'] = YOUTUBE_PROXY
        if YOUTUBE_COOKIES_FILE and os.path.exists(YOUTUBE_COOKIES_FILE):
            ydl_opts['cookiefile'] = YOUTUBE_COOKIES_FILE

        try:
            result = _run_single_strategy(ydl_opts, url, 1)
            if result:
                return result
        except Exception as e:
            logger.warning(f"yt-dlp video download failed: {e}")

        # Fallback to pytubefix for video (progressive only to ensure sound)
        if has_pytubefix:
            try:
                logger.info(f"Trying pytubefix video fallback for {url}...")
                yt = PytubeFixYouTube(url)
                stream = yt.streams.filter(progressive=True, file_extension='mp4').get_highest_resolution() or yt.streams.filter(progressive=True).get_highest_resolution()
                if stream:
                    fp = stream.download(output_path=output_dir, filename=f"{filename}.mp4")
                    return {
                        'filepath': fp,
                        'title': yt.title or 'Video',
                        'duration': yt.length or 0,
                    }
            except Exception as pe:
                logger.warning(f"pytubefix video fallback failed: {pe}")

        raise Exception("Failed to download YouTube video after trying available engines.")

    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _download),
            timeout=300  # 5 minute hard cap on any single download
        )
    except asyncio.TimeoutError:
        raise Exception("YouTube download timed out after 5 minutes.")


async def download_youtube_video(url: str, output_dir: str, quality=720) -> dict:
    """Download a YouTube video. Tries high-speed direct download on the hosting server first;
    falls back to residential home relay if blocked."""
    # Step 1: Direct fast download on host server (e.g. Northflank 1Gbps connection)
    try:
        logger.info(f"Attempting high-speed direct YouTube download on host: {url} (quality={quality})")
        return await _download_youtube_local(url, output_dir, quality=quality)
    except Exception as direct_err:
        logger.warning(f"Direct download attempt failed ({direct_err}).")

        # Step 2: Fallback to residential relay (Termux / Home PC) if configured
        if YOUTUBE_RELAY_URL:
            try:
                logger.info(f"Falling back to home relay: {url} via {YOUTUBE_RELAY_URL}")
                return await download_youtube_via_relay(url, output_dir, quality)
            except Exception as relay_err:
                logger.error(f"Home relay fallback also failed: {relay_err}")
                raise Exception(f"Download failed directly ({direct_err}) and via relay ({relay_err})")

        raise direct_err


# Download queue manager: limits concurrent heavy downloads to prevent server overload
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))
_download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
_queued_downloads_count = 0
_active_yt_downloads = set()


async def execute_youtube_download(target_message, yt_url: str, context: ContextTypes.DEFAULT_TYPE, status_msg=None, quality=720) -> None:
    """Execute download and upload for YouTube video or audio with queue management and memory streaming."""
    global _queued_downloads_count, _active_yt_downloads
    is_audio = str(quality).lower() in ("audio", "mp3")
    label = "audio (MP3)" if is_audio else f"{quality}p"
    key = (target_message.chat_id, yt_url, str(quality))

    if key in _active_yt_downloads:
        return
    _active_yt_downloads.add(key)

    try:
        # If all download slots are currently occupied, notify user of queue position
        if _download_semaphore.locked():
            _queued_downloads_count += 1
            pos = _queued_downloads_count
            q_text = f"⏳ In download queue (position #{pos}). Waiting for other downloads to finish..."
            if not status_msg:
                status_msg = await target_message.reply_text(q_text)
            else:
                try:
                    await status_msg.edit_text(q_text)
                except Exception:
                    pass

        async with _download_semaphore:
            if _queued_downloads_count > 0:
                _queued_downloads_count = max(0, _queued_downloads_count - 1)

            if not status_msg:
                status_msg = await target_message.reply_text(f"⏳ Downloading YouTube {label}...")
            else:
                try:
                    await status_msg.edit_text(f"⏳ Downloading YouTube {label}...")
                except Exception:
                    pass

            with tempfile.TemporaryDirectory() as tmp_dir:
                result = await download_youtube_video(yt_url, tmp_dir, quality=quality)
                filepath = result['filepath']
                title = result['title']
                duration = result.get('duration', 0)

                # Check duration limit (30 mins = 1800s)
                if duration and duration > MAX_VIDEO_DURATION:
                    mins = duration // 60
                    secs = duration % 60
                    await status_msg.edit_text(
                        fmt_warning(f"Media is too long ({mins}m {secs}s). Max allowed duration is 30 minutes.")
                    )
                    return

                # Check file size limit (Standard Telegram limit: 50 MB, Local Bot API: up to 2000 MB)
                file_size = os.path.getsize(filepath)
                mb_size = file_size / (1024 * 1024)
                max_bytes = MAX_UPLOAD_SIZE_MB * 1024 * 1024
                if file_size > max_bytes:
                    await status_msg.edit_text(
                        fmt_warning(f"File is too large for Telegram ({mb_size:.1f} MB). Max limit is {MAX_UPLOAD_SIZE_MB} MB.")
                    )
                    return

                upload_note = " (large file, may take a moment)" if mb_size > 40 else ""
                await status_msg.edit_text(f"📤 Uploading to Telegram ({mb_size:.1f} MB){upload_note}...")

                if is_audio:
                    await context.bot.send_chat_action(
                        chat_id=target_message.chat_id, action="upload_document"
                    )
                    try:
                        with open(filepath, 'rb') as audio_file:
                            input_audio = InputFile(audio_file, filename=os.path.basename(filepath), read_file_handle=False)
                            await target_message.reply_audio(
                                audio=input_audio,
                                title=title,
                                caption=f"🎵 {title}",
                                duration=int(duration) if duration else None,
                                read_timeout=300,
                                write_timeout=300,
                            )
                    except Exception as upload_err:
                        logger.warning(f"reply_audio failed ({upload_err}), attempting reply_document fallback...")
                        with open(filepath, 'rb') as audio_file:
                            input_doc = InputFile(audio_file, filename=os.path.basename(filepath), read_file_handle=False)
                            await target_message.reply_document(
                                document=input_doc,
                                caption=f"🎵 {title}",
                                read_timeout=300,
                                write_timeout=300,
                            )
                else:
                    await context.bot.send_chat_action(
                        chat_id=target_message.chat_id, action="upload_video"
                    )
                    try:
                        with open(filepath, 'rb') as video_file:
                            input_video = InputFile(video_file, filename=os.path.basename(filepath), read_file_handle=False)
                            await target_message.reply_video(
                                video=input_video,
                                caption=f"📹 {title}",
                                supports_streaming=True,
                                read_timeout=300,
                                write_timeout=300,
                            )
                    except Exception as upload_err:
                        logger.warning(f"reply_video failed ({upload_err}), attempting reply_document fallback...")
                        with open(filepath, 'rb') as video_file:
                            input_doc = InputFile(video_file, filename=os.path.basename(filepath), read_file_handle=False)
                            await target_message.reply_document(
                                document=input_doc,
                                caption=f"📹 {title}",
                                read_timeout=300,
                                write_timeout=300,
                            )

                try:
                    await status_msg.delete()
                except Exception:
                    pass

                logger.info(f"YouTube {'audio' if is_audio else 'video'} sent successfully: {title}")

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
        error_str = str(e)
        logger.error(f"YouTube download failed for {yt_url}: {type(e).__name__}: {e}")
        error_lower = error_str.lower()
        if 'too large' in error_lower or '413' in error_lower or 'file is too big' in error_lower:
            try:
                await status_msg.edit_text(fmt_warning(f"This file exceeds Telegram's upload limits for this server."))
            except Exception:
                pass
        elif 'timed out' in error_lower:
            try:
                await status_msg.edit_text(fmt_warning("The request or upload timed out. Please try again."))
            except Exception:
                pass
        else:
            try:
                await status_msg.edit_text(fmt_error("Something went wrong processing this video."))
            except Exception:
                pass
    finally:
        _active_yt_downloads.discard(key)


async def is_youtube_live(url: str) -> bool:
    """Check whether a YouTube URL is a currently-active live stream via a plain page fetch (fast, no yt-dlp/GetPOT involved)."""
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            resp = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept-Language": "en-US"},
            )
            if resp.status_code == 200:
                return '"isLive":true' in resp.text
    except Exception as e:
        logger.warning(f"Failed to check live status for {url}: {e}")
    return False


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

    if should_skip_message(update.message):
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

    # Live streams can't be downloaded — ignore the link entirely rather than
    # showing a prompt that will just fail once clicked.
    if await is_youtube_live(yt_url):
        logger.info(f"Ignoring live YouTube stream: {yt_url}")
        return

    # For standard videos, offer quality choices + MP3
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬇️ 720p", callback_data=f"ytdl:720:{yt_url}"),
            InlineKeyboardButton("⬇️ 1080p", callback_data=f"ytdl:1080:{yt_url}"),
            InlineKeyboardButton("🎵 MP3", callback_data=f"ytdl:audio:{yt_url}"),
        ]
    ])

    caption = (
        f"📹 <b>YouTube Link Detected</b>\n"
        f"🔗 {yt_url}\n\n"
        f"<i>Choose a format to download:</i>"
    )

    thumbnail_url = None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("https://www.youtube.com/oembed", params={"url": yt_url, "format": "json"})
            if resp.status_code == 200:
                thumbnail_url = resp.json().get("thumbnail_url")
    except Exception as e:
        logger.warning(f"Failed to fetch YouTube oEmbed thumbnail for {yt_url}: {e}")

    if thumbnail_url:
        try:
            await update.message.reply_photo(
                photo=thumbnail_url,
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
                reply_to_message_id=update.message.message_id
            )
            return
        except Exception as e:
            logger.warning(f"Failed to send YouTube thumbnail preview, falling back to text: {e}")

    await update.message.reply_text(
        caption,
        parse_mode="HTML",
        reply_markup=keyboard,
        reply_to_message_id=update.message.message_id,
        disable_web_page_preview=True
    )


async def handle_youtube_download_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 'Download Video / Audio' inline keyboard button presses."""
    query = update.callback_query
    if not query:
        return

    if is_maintenance_active_for_user(query.from_user.id):
        await safe_answer_query(query, "🚧 Bot is currently in maintenance mode. Please try again shortly!", show_alert=True)
        return

    await safe_answer_query(query)

    data = query.data
    if not data or not data.startswith("ytdl:"):
        return

    quality_str, _, yt_url = data[5:].partition(":")
    if quality_str == "summary":
        await query.answer("AI Summary feature has been removed.", show_alert=True)
        return

    if quality_str in ("audio", "mp3"):
        quality = "audio"
        logger.info(f"User clicked YouTube download button (MP3 audio) for: {yt_url}")
    elif quality_str == "1080":
        quality = 1080
        logger.info(f"User clicked YouTube download button (1080p) for: {yt_url}")
    else:
        quality = 720
        logger.info(f"User clicked YouTube download button (720p) for: {yt_url}")

    key = (query.message.chat_id, yt_url, str(quality))
    if key in _active_yt_downloads:
        try:
            await query.answer("⏳ This download is already in progress! Please wait a moment...", show_alert=True)
        except Exception:
            pass
        return

    status_msg = await query.message.reply_text("⏳ Starting YouTube download...")
    await execute_youtube_download(query.message, yt_url, context, status_msg=status_msg, quality=quality)


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

    if should_skip_message(update.message):
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
                input_video = InputFile(video_file, filename=os.path.basename(filepath), read_file_handle=False)
                await update.message.reply_video(
                    video=input_video,
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


async def download_video_source(url: str, output_dir: str) -> Optional[str]:
    """Download video from URL (Instagram, TikTok, Reddit, YouTube, or direct MP4 link) for GIF conversion.
    Returns the absolute path to the downloaded video file, or None if failed.
    """
    if not url:
        return None

    # 1. Direct video file URL
    clean_url = url.split("?")[0].lower()
    if clean_url.endswith((".mp4", ".mov", ".mkv", ".webm")):
        try:
            out_path = os.path.join(output_dir, f"video_{uuid.uuid4().hex[:8]}.mp4")
            async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
                resp = await client.get(url)
                if resp.status_code == 200 and len(resp.content) > 0:
                    with open(out_path, "wb") as f:
                        f.write(resp.content)
                    return out_path
        except Exception as e:
            logger.warning(f"Direct video download failed ({e}), continuing fallback...")

    # 2. Instagram: Fast direct extraction of CDN video URL
    if INSTAGRAM_URL_PATTERN.search(url):
        try:
            loop = asyncio.get_running_loop()

            def _extract_ig():
                ydl_opts = {
                    'format': 'best',
                    'quiet': True,
                    'no_warnings': True,
                    'skip_download': True,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    entries = info.get('entries') if info.get('_type') == 'playlist' else [info]
                    for e in entries:
                        if not e:
                            continue
                        item_url = e.get('url')
                        if not item_url and e.get('formats'):
                            item_url = e['formats'][-1].get('url')
                        if item_url and e.get('ext') != 'jpg':
                            return item_url
                return None

            video_cdn_url = await loop.run_in_executor(None, _extract_ig)
            if video_cdn_url:
                out_path = os.path.join(output_dir, f"ig_{uuid.uuid4().hex[:8]}.mp4")
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                    resp = await client.get(video_cdn_url, headers=headers)
                    if resp.status_code == 200 and len(resp.content) > 0:
                        with open(out_path, "wb") as f:
                            f.write(resp.content)
                        return out_path
        except Exception as ig_err:
            logger.warning(f"Fast Instagram extraction failed for GIF ({ig_err}), falling back to yt-dlp...")

    # 3. Generic media download with reasonable 45-second timeout
    try:
        info = await asyncio.wait_for(
            download_generic_media(url, output_dir, platform_name="Video"),
            timeout=45.0
        )
        return info.get("filepath")
    except Exception as e:
        logger.error(f"Failed to download video source from {url}: {e}")
        return None


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
            await status_msg.edit_text(fmt_error(classify_media_error(error_msg)))
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
            await status_msg.edit_text(fmt_error(classify_media_error(str(e))))
        except Exception:
            pass


# ─── Threads Media Handler ─────────────────────────────────────────────────────
# Threads has no yt-dlp extractor and its post pages are login-walled for normal
# browsers, but they're still server-rendered (with the same private media schema
# Instagram uses) for the Googlebot crawler. We scrape that rendering directly.

_THREADS_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"


def _match_json_bracket(text: str, open_idx: int) -> int:
    """Given the index of an opening '{' or '[' in `text`, return the index just
    past its matching closing bracket, respecting JSON string/escape state."""
    open_ch = text[open_idx]
    close_ch = '}' if open_ch == '{' else ']'
    depth = 0
    in_string = False
    escape = False
    for i in range(open_idx, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return i + 1
    raise ValueError("No matching bracket found")


_THREADS_CODE_FIELD = re.compile(r'"code":"([^"]+)"')


def _find_in_post_scope(text: str, marker: str, idx_code: int, code: str, radius: int = 200000) -> int:
    """Return the occurrence of `marker` closest to idx_code that still belongs to
    *this* post, or -1 if none does.

    A video post's own cover-frame thumbnail (`image_versions2`) always sits a few
    characters from its "code" field, while the actual `video_versions` can be tens
    of thousands of characters away on the other side of an inlined DASH manifest --
    so nearest-by-distance alone would wrongly pick the thumbnail. Instead we require
    that no *other* post's "code" field falls between idx_code and the candidate,
    which keeps the search anchored to this post's own slice of the page no matter
    how far apart its fields land.
    """
    lo, hi = max(0, idx_code - radius), min(len(text), idx_code + radius)
    candidates = sorted(
        (m.start() + lo for m in re.finditer(re.escape(marker), text[lo:hi])),
        key=lambda p: abs(p - idx_code)
    )
    for pos in candidates:
        seg_start, seg_end = (idx_code, pos) if pos > idx_code else (pos, idx_code)
        if all(m.group(1) == code for m in _THREADS_CODE_FIELD.finditer(text, seg_start, seg_end)):
            return pos
    return -1


def _find_carousel_media_array(text: str, idx_code: int, radius: int = 200000):
    """Return (arr_start, arr_end) for this post's own `carousel_media` array, or
    None if it has none.

    Carousel items carry their own "code" fields (each is individually
    addressable), so `_find_in_post_scope`'s foreign-code check can't be used here
    -- it would reject the real array because of its own nested codes. Instead we
    rely on this schema's fixed field order: the array closes immediately before
    this post's own "code" field, separated only by a comma.
    """
    lo = max(0, idx_code - radius)
    candidates = sorted(
        (m.start() + lo for m in re.finditer(r'"carousel_media":\[', text[lo:idx_code])),
        key=lambda p: idx_code - p
    )
    for pos in candidates:
        arr_start = pos + len('"carousel_media":')
        try:
            arr_end = _match_json_bracket(text, arr_start)
        except ValueError:
            continue
        if text[arr_end:idx_code] == ',':
            return arr_start, arr_end
    return None


def _parse_threads_page(text: str, code: str) -> dict:
    """Pull the media items, caption, and author for one Threads post out of its
    Googlebot-rendered page HTML."""
    idx_code = text.find(f'"code":"{code}"')
    if idx_code == -1:
        raise Exception("This post isn't available (it may be private, deleted, or region-locked).")

    def _media_from_item(item: dict) -> dict | None:
        vv = item.get("video_versions")
        if vv:
            return {"url": vv[0]["url"], "is_video": True}
        iv2 = item.get("image_versions2")
        if iv2 and iv2.get("candidates"):
            return {"url": iv2["candidates"][0]["url"], "is_video": False}
        return None

    items = []
    carousel_bounds = _find_carousel_media_array(text, idx_code)
    if carousel_bounds:
        arr_start, arr_end = carousel_bounds
        carousel = json.loads(text[arr_start:arr_end])
        for entry in carousel:
            media = _media_from_item(entry)
            if media:
                items.append(media)
        caption_scope_end = arr_start
    else:
        # A video post always carries its own cover-frame image_versions2 too, so
        # video takes priority whenever both are present -- it's the real content.
        vid_pos = _find_in_post_scope(text, '"video_versions":[', idx_code, code)
        if vid_pos != -1:
            arr_start = vid_pos + len('"video_versions":')
            video_versions = json.loads(text[arr_start:_match_json_bracket(text, arr_start)])
            if video_versions:
                items.append({"url": video_versions[0]["url"], "is_video": True})
        else:
            img_pos = _find_in_post_scope(text, '"image_versions2":{', idx_code, code)
            if img_pos != -1:
                obj_start = img_pos + len('"image_versions2":')
                image_versions2 = json.loads(text[obj_start:_match_json_bracket(text, obj_start)])
                if image_versions2.get("candidates"):
                    items.append({"url": image_versions2["candidates"][0]["url"], "is_video": False})
        caption_scope_end = idx_code

    title = None
    cap_pos = _find_in_post_scope(text, '"caption":{"text":"', caption_scope_end, code)
    if cap_pos != -1:
        obj_start = cap_pos + len('"caption":')
        try:
            caption = json.loads(text[obj_start:_match_json_bracket(text, obj_start)])
            title = caption.get("text")
        except Exception:
            pass

    return {"items": items, "title": title}


async def download_threads_media(url: str) -> dict:
    """Fetch a Threads post's media items by scraping its Googlebot-rendered page.

    `url` may be a full post link or a short /share/ or /t/ link -- either way,
    the username and post code are read off the final redirected URL rather
    than the input, since share links carry no username of their own.
    """
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": _THREADS_UA})
    resolved_url = str(resp.url)
    if "error=invalid_post" in resolved_url or resp.status_code == 404:
        raise Exception("This post isn't available (it may be private, deleted, or region-locked).")
    canonical = THREADS_CANONICAL_PATTERN.search(resolved_url)
    if not canonical:
        raise Exception("This post isn't available (it may be private, deleted, or region-locked).")
    username, code = canonical.group(1), canonical.group(2)
    data = _parse_threads_page(resp.text, code)
    data["uploader"] = username
    data["url"] = resolved_url.split("?")[0]
    return data


async def execute_threads_download(target_message, url: str, context: ContextTypes.DEFAULT_TYPE, status_msg=None) -> None:
    """Download and upload Threads media, including photo-only and multi-item posts."""
    if not status_msg:
        status_msg = await target_message.reply_text("⏳ Downloading Threads media...", reply_to_message_id=target_message.message_id)
    else:
        try:
            await status_msg.edit_text("⏳ Downloading Threads media...")
        except Exception:
            pass

    try:
        data = await download_threads_media(url)
        items = data["items"]
        if not items:
            raise Exception("No media found in this post.")

        caption_text = html.escape(data["title"]) if data.get("title") else html.escape(data["uploader"])
        caption = (
            f"{caption_text}\n"
            f"👤 <i>Source: @{html.escape(data['uploader'])}</i>\n\n"
            f"🔗 <a href='{data['url']}'>Threads Link</a>"
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
                    logger.warning(f"Failed to fetch Threads media item: {fetch_err}")

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
        logger.error(f"Threads download failed for {url}: {type(e).__name__}: {e}")
        try:
            await status_msg.edit_text(fmt_error(classify_media_error(str(e))))
        except Exception:
            pass


async def handle_threads_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-detect Threads links."""
    if not update.message or not update.message.text: return
    user = update.effective_user
    if user and is_maintenance_active_for_user(user.id):
        await update.message.reply_text(MAINTENANCE_NOTICE, parse_mode="HTML"); return

    text = update.message.text.strip()
    match = THREADS_URL_PATTERN.search(text)
    if not match: return
    if should_skip_message(update.message): return
    url = match.group(0)

    chat_id = update.effective_chat.id
    if not is_downloader_enabled(chat_id, "threads"):
        return

    auto_dl = get_download_mode(chat_id)
    if not auto_dl:
        short_id = store_pending_download(url, "Threads")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ Download Threads Media", callback_data=f"dlmed:{short_id}")]])
        await update.message.reply_text("🧵 <b>Threads Link Detected</b>\n<i>Click below to download:</i>", parse_mode="HTML", reply_markup=kb, reply_to_message_id=update.message.message_id)
        return

    await execute_threads_download(update.message, url, context)


async def handle_tiktok_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-detect TikTok links."""
    if not update.message or not update.message.text: return
    user = update.effective_user
    if user and is_maintenance_active_for_user(user.id):
        await update.message.reply_text(MAINTENANCE_NOTICE, parse_mode="HTML"); return

    text = update.message.text.strip()
    match = TIKTOK_URL_PATTERN.search(text)
    if not match: return
    if should_skip_message(update.message): return
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
    if should_skip_message(update.message): return
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
    if should_skip_message(update.message): return
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

            card_key = f"reddit:card:{canonical_url}"
            card_png = media_cache.get(card_key)
            if not card_png:
                card_png = card.generate_reddit_card(reddit_data)
                media_cache.set(card_key, card_png)

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
    if not query:
        return

    if is_maintenance_active_for_user(query.from_user.id):
        await safe_answer_query(query, "🚧 Bot is currently in maintenance mode. Please try again shortly!", show_alert=True)
        return

    await safe_answer_query(query)
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
    elif platform == "Threads":
        await execute_threads_download(query.message, url, context, status_msg=status_msg)
    else:
        await execute_generic_media_download(query.message, url, platform, context, status_msg=status_msg)


