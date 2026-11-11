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
import json
import html
import time
import asyncio
import tempfile
import uuid
from typing import Optional
from urllib.parse import urljoin
import card

import httpx
import yt_dlp
import yt_dlp.plugins
# Disable yt-dlp plugin auto-loading (bgutil spawns deno/node and stalls/OOMs on constrained container CPUs).
yt_dlp.plugins.load_plugins = lambda *a, **k: None
from yt_dlp.extractor.instagram import InstagramBaseIE
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo, InputFile
from telegram.ext import ContextTypes

from config import (
    IS_TERMUX,
    YOUTUBE_COOKIES_FILE,
    INSTAGRAM_COOKIES_FILE,
    INSTAGRAM_PROXY,
    get_instagram_proxy,
    INSTAGRAM_MAX_CONCURRENT,
    INSTAGRAM_REQUEST_DELAY,
    YOUTUBE_PROXY,
    get_youtube_proxy,
    YOUTUBE_RELAY_URL,
    MAX_UPLOAD_SIZE_MB,
    MAX_VIDEO_DURATION,
    should_skip_message,
    safe_answer_query,
    is_maintenance_active_for_user,
    MAINTENANCE_NOTICE,
    is_downloader_enabled,
    get_download_mode,
    pending_downloads,
    store_pending_download,
    fmt_error,
    fmt_warning,
    classify_media_error,
    send_media_error_dm,
    YOUTUBE_URL_PATTERN,
    TWITCH_CLIP_PATTERN,
    TIKTOK_URL_PATTERN,
    INSTAGRAM_URL_PATTERN,
    THREADS_URL_PATTERN,
    THREADS_CANONICAL_PATTERN,
    REDDIT_URL_PATTERN,
    TWITTER_URL_PATTERN,
    media_cache,
    url_media_cache,
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


def sanitize_instagram_url(url: str) -> str:
    """Normalize Instagram post/reel URL to a clean canonical format without tracking queries."""
    if not url:
        return ""
    m = INSTAGRAM_URL_PATTERN.search(url.strip())
    if m:
        shortcode = m.group(1)
        post_type = "reel" if "/reel/" in url.lower() or "/reels/" in url.lower() else "p"
        return f"https://www.instagram.com/{post_type}/{shortcode}/"
    return url.split("?")[0].split("#")[0]


# ─── Instagram Throttling & Rate-Limit Concurrency Guards ─────────────────────

_ig_semaphore: Optional[asyncio.Semaphore] = None
_ig_throttle_lock: Optional[asyncio.Lock] = None
_last_ig_request_time: float = 0.0

def _get_ig_semaphore() -> asyncio.Semaphore:
    global _ig_semaphore
    if _ig_semaphore is None:
        _ig_semaphore = asyncio.Semaphore(INSTAGRAM_MAX_CONCURRENT)
    return _ig_semaphore

def _get_ig_throttle_lock() -> asyncio.Lock:
    global _ig_throttle_lock
    if _ig_throttle_lock is None:
        _ig_throttle_lock = asyncio.Lock()
    return _ig_throttle_lock

async def _wait_for_instagram_slot() -> None:
    """Throttle Instagram requests to avoid hitting rate limits and 429 bans."""
    global _last_ig_request_time
    lock = _get_ig_throttle_lock()
    async with lock:
        now = time.time()
        elapsed = now - _last_ig_request_time
        if elapsed < INSTAGRAM_REQUEST_DELAY:
            delay = INSTAGRAM_REQUEST_DELAY - elapsed
            logger.info(f"Throttling Instagram request: waiting {delay:.2f}s...")
            await asyncio.sleep(delay)
        _last_ig_request_time = time.time()


# ─── URL to Telegram file_id Media Cache Helpers ──────────────────────────────

def get_canonical_media_cache_key(url: str, platform_name: str = "") -> Optional[str]:
    """Generate a stable, query-stripped cache key for media deduplication."""
    if not url:
        return None
    url_str = url.strip()

    # 1. Instagram: extract post/reel shortcode
    ig_match = INSTAGRAM_URL_PATTERN.search(url_str)
    if ig_match:
        return f"fid:ig:{ig_match.group(1)}"

    # 2. TikTok: extract video or short ID
    tt_match = TIKTOK_URL_PATTERN.search(url_str)
    if tt_match:
        return f"fid:tt:{tt_match.group(1)}"

    # 3. Twitch: extract clip slug
    tw_match = TWITCH_CLIP_PATTERN.search(url_str)
    if tw_match:
        return f"fid:twitch:{tw_match.group(1)}"

    # 4. Twitter/X: extract tweet ID
    x_match = TWITTER_URL_PATTERN.search(url_str)
    if x_match:
        return f"fid:tw:{x_match.group(2)}"

    # 5. Threads: extract post slug
    th_match = THREADS_CANONICAL_PATTERN.search(url_str)
    if th_match:
        return f"fid:threads:{th_match.group(2)}"

    # 6. Fallback (e.g. Reddit or generic): strip query params and trailing slash
    clean = url_str.split("?")[0].split("#")[0].rstrip("/").lower()
    prefix = platform_name.lower().replace(" ", "_") if platform_name else "gen"
    return f"fid:{prefix}:{clean}"


async def send_cached_media(target_message, cached: dict, original_url: str, platform_name: str, status_msg=None) -> bool:
    """Send previously uploaded media using Telegram file_id. Returns True if sent successfully, False otherwise."""
    if not cached or not target_message:
        return False
    try:
        title = cached.get('title') or f"{platform_name} Media"
        uploader = cached.get('uploader') or platform_name
        caption = (
            f"<b>{html.escape(title)}</b>\n"
            f"👤 <i>Source: {html.escape(uploader)}</i>\n\n"
            f"🔗 <a href='{original_url}'>{platform_name} Link</a>"
        )
        if len(caption) > 1024:
            caption = caption[:1020] + "…"

        media_type = cached.get("type", "video")
        if media_type == "video":
            await target_message.reply_video(
                video=cached["file_id"],
                caption=caption,
                parse_mode="HTML",
                duration=cached.get("duration", 0),
                supports_streaming=True,
                reply_to_message_id=target_message.message_id
            )
        elif media_type == "photo":
            await target_message.reply_photo(
                photo=cached["file_id"],
                caption=caption,
                parse_mode="HTML",
                reply_to_message_id=target_message.message_id
            )
        elif media_type == "media_group":
            items = cached.get("items", [])
            if not items:
                return False
            media_group = []
            for i, it in enumerate(items):
                kw = {'caption': caption, 'parse_mode': 'HTML'} if i == 0 else {}
                if it.get("type") == "video":
                    media_group.append(InputMediaVideo(media=it["file_id"], **kw))
                else:
                    media_group.append(InputMediaPhoto(media=it["file_id"], **kw))
            await target_message.reply_media_group(media=media_group, reply_to_message_id=target_message.message_id)
        else:
            return False

        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass
        logger.info(f"Instant cache hit for {platform_name} media: {original_url}")
        return True
    except Exception as e:
        logger.warning(f"Failed to send cached media for {original_url} ({e}); falling back to fresh download.")
        return False


def cache_uploaded_media(cache_key: Optional[str], media_type: str, sent_message_or_list, title: str = "", uploader: str = "", duration: int = 0) -> None:
    """Store Telegram file_id in url_media_cache for instantaneous retrieval in future requests."""
    if not cache_key or not sent_message_or_list:
        return
    try:
        if media_type == "media_group":
            items = []
            msgs = sent_message_or_list if isinstance(sent_message_or_list, list) else [sent_message_or_list]
            for m in msgs:
                if getattr(m, 'video', None):
                    items.append({"type": "video", "file_id": m.video.file_id})
                elif getattr(m, 'photo', None):
                    items.append({"type": "photo", "file_id": m.photo[-1].file_id})
            if items:
                url_media_cache.set(cache_key, {
                    "type": "media_group",
                    "items": items,
                    "title": title,
                    "uploader": uploader,
                })
        elif media_type == "video":
            msg = sent_message_or_list
            if getattr(msg, 'video', None):
                url_media_cache.set(cache_key, {
                    "type": "video",
                    "file_id": msg.video.file_id,
                    "title": title,
                    "uploader": uploader,
                    "duration": duration,
                })
        elif media_type == "photo":
            msg = sent_message_or_list
            if getattr(msg, 'photo', None):
                url_media_cache.set(cache_key, {
                    "type": "photo",
                    "file_id": msg.photo[-1].file_id,
                    "title": title,
                    "uploader": uploader,
                })
    except Exception as e:
        logger.warning(f"Failed to cache uploaded media for {cache_key}: {e}")

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

        # Use a dedicated thread with a non-blocking timeout guard (never wait on a hung thread)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        fut = pool.submit(_inner)
        try:
            res = fut.result(timeout=_strategy_timeout)
            pool.shutdown(wait=False)
            return res
        except concurrent.futures.TimeoutError:
            if fut.done() and not fut.exception():
                res = fut.result()
                pool.shutdown(wait=False)
                if res:
                    return res
            pool.shutdown(wait=False, cancel_futures=True)
            logger.warning(f"yt-dlp strategy {strategy_num} timed out after {_strategy_timeout}s for {url}")
            raise Exception(f"Strategy {strategy_num} timed out after {_strategy_timeout}s")
        except Exception:
            pool.shutdown(wait=False, cancel_futures=True)
            raise

    def _download():
        logger.info(f"Downloading YouTube media: {url} (quality={quality})...")
        if IS_TERMUX:
            logger.info("Termux environment detected — using lightweight download settings.")

        is_audio = str(quality).lower() in ("audio", "mp3")
        yt_proxy = get_youtube_proxy()
        pytube_proxies = {"http": yt_proxy, "https": yt_proxy} if yt_proxy else None

        # ─── OPTION A: MP3 Audio Download (Dedicated Single Strategy) ───
        if is_audio:
            logger.info(f"Downloading YouTube audio: {url}...")
            # Pytubefix fast audio engine (strictly selects original/default audio track)
            if has_pytubefix:
                try:
                    logger.info(f"Using pytubefix fast audio engine for {url}...")
                    yt = PytubeFixYouTube(url, proxies=pytube_proxies) if pytube_proxies else PytubeFixYouTube(url)
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

            def _make_audio_opts(use_fallback_clients: bool = False) -> dict:
                opts = {
                    'outtmpl': output_template,
                    'format': 'bestaudio[format_note*=original]/bestaudio[language=original]/bestaudio[language=en]/bestaudio/best',
                    'format_sort': ['lang:original', 'ext:m4a:10', 'quality'],
                    'http_headers': {'Accept-Language': 'en-US,en;q=0.9'},
                    'source_address': '0.0.0.0',
                    'noplaylist': True,
                    'compat_opts': ['no-plugins'],
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '192',
                    }],
                    'concurrent_fragment_downloads': _concurrent_frags,
                    'socket_timeout': _sock_timeout,
                    'retries': 1,
                    'extractor_retries': 0,
                    'quiet': not os.getenv('YT_DEBUG'),
                    'nocheckcertificate': True,
                }
                if not use_fallback_clients:
                    opts['extractor_args'] = {'youtube': {'player_client': ['android', 'ios']}}
                if yt_proxy:
                    opts['proxy'] = yt_proxy
                if YOUTUBE_COOKIES_FILE and os.path.exists(YOUTUBE_COOKIES_FILE):
                    opts['cookiefile'] = YOUTUBE_COOKIES_FILE
                return opts

            last_audio_err = None
            for idx, fallback_mode in enumerate((False, True), start=1):
                try:
                    result = _run_single_strategy(_make_audio_opts(fallback_mode), url, idx)
                    if result:
                        return result
                except Exception as ae:
                    last_audio_err = ae
                    logger.warning(f"yt-dlp audio strategy {idx} failed: {ae}")

            raise Exception(f"Failed to download YouTube audio: {last_audio_err or 'unknown error'}")

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
        def _make_video_opts(use_fallback_clients: bool = False) -> dict:
            opts = {
                'outtmpl': output_template,
                'merge_output_format': 'mp4',
                'format': fast_format,
                'format_sort': ['lang:original', 'res', 'fps'],
                'http_headers': {'Accept-Language': 'en-US,en;q=0.9'},
                'source_address': '0.0.0.0',
                'noplaylist': True,
                'compat_opts': ['no-plugins'],
                'postprocessor_args': {'merger': ['-c:v', 'copy', '-c:a', 'aac']},
                'concurrent_fragment_downloads': _concurrent_frags,
                'socket_timeout': _sock_timeout,
                'retries': 1,
                'extractor_retries': 0,
                'quiet': not os.getenv('YT_DEBUG'),
                'verbose': bool(os.getenv('YT_DEBUG')),
                'nocheckcertificate': True,
            }
            if not use_fallback_clients:
                opts['extractor_args'] = {'youtube': {'player_client': ['android', 'ios']}}
            if yt_proxy:
                opts['proxy'] = yt_proxy
            if YOUTUBE_COOKIES_FILE and os.path.exists(YOUTUBE_COOKIES_FILE):
                opts['cookiefile'] = YOUTUBE_COOKIES_FILE
            return opts

        last_err = None
        for idx, fallback_mode in enumerate((False, True), start=1):
            try:
                result = _run_single_strategy(_make_video_opts(fallback_mode), url, idx)
                if result:
                    return result
            except Exception as e:
                last_err = e
                logger.warning(f"yt-dlp video strategy {idx} failed: {e}")

        # Fallback to pytubefix for video (progressive only to ensure sound)
        if has_pytubefix:
            try:
                logger.info(f"Trying pytubefix video fallback for {url}...")
                yt = PytubeFixYouTube(url, proxies=pytube_proxies) if pytube_proxies else PytubeFixYouTube(url)
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
                if not last_err:
                    last_err = pe

        raise Exception(str(last_err) if last_err else "Failed to download YouTube video after trying available engines.")

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
    global _queued_downloads_count
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
        elif 'sign in to confirm' in error_lower or 'not a bot' in error_lower:
            await status_msg.edit_text(fmt_warning("YouTube blocked this server's request (bot verification required)."))
        elif 'private' in error_lower or 'unavailable' in error_lower:
            await status_msg.edit_text(fmt_error("This video is private or unavailable."))
        elif 'age' in error_lower:
            await status_msg.edit_text(fmt_error("This video is age-restricted and cannot be downloaded."))
        else:
            await status_msg.edit_text(fmt_error("Couldn't download this video."))
        await send_media_error_dm(context, target_message, yt_url, "YouTube", error_str)
    except Exception as e:
        error_str = str(e)
        logger.error(f"YouTube download failed for {yt_url}: {type(e).__name__}: {e}")
        error_lower = error_str.lower()
        if 'too large' in error_lower or '413' in error_lower or 'file is too big' in error_lower:
            try:
                await status_msg.edit_text(fmt_warning("This file exceeds Telegram's upload limits for this server."))
            except Exception:
                pass
        elif 'sign in to confirm' in error_lower or 'not a bot' in error_lower:
            try:
                await status_msg.edit_text(fmt_warning("YouTube blocked this server's request (bot verification required)."))
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
        await send_media_error_dm(context, target_message, yt_url, "YouTube", error_str)
    finally:
        _active_yt_downloads.discard(key)


async def is_youtube_live(url: str) -> bool:
    """Check whether a YouTube URL is a currently-active live stream via a plain page fetch (fast, no yt-dlp/GetPOT involved)."""
    try:
        yt_proxy = get_youtube_proxy()
        client_kwargs = {"timeout": 8.0, "follow_redirects": True}
        if yt_proxy:
            client_kwargs["proxy"] = yt_proxy
        async with httpx.AsyncClient(**client_kwargs) as client:
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

    cache_key = get_canonical_media_cache_key(clip_url, "Twitch")
    if cache_key:
        cached = url_media_cache.get(cache_key)
        if cached and await send_cached_media(update.message, cached, clip_url, "Twitch"):
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
                sent_msg = await update.message.reply_video(
                    video=input_video,
                    caption=caption,
                    parse_mode="HTML",
                    duration=duration,
                    supports_streaming=True,
                    reply_to_message_id=update.message.message_id
                )
            cache_uploaded_media(cache_key, "video", sent_msg, title, uploader, duration)

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
            await status_msg.edit_text(fmt_error(classify_media_error(error_msg)), parse_mode="HTML")
        await send_media_error_dm(context, update.message, clip_url, "Twitch", error_msg)


# ─── Universal Generic Downloader (TikTok, Instagram, Reddit) ─────────────────

async def download_generic_media(url: str, output_dir: str, platform_name: str = "Media", cookiefile: str = None, proxy: str = None) -> dict:
    """Download video from TikTok, Instagram, Reddit, etc. using yt-dlp. Returns dict with filepath, title, duration, uploader."""
    filename = f"{uuid.uuid4().hex}"
    output_template = os.path.join(output_dir, f"{filename}.%(ext)s")

    loop = asyncio.get_running_loop()

    def _download():
        logger.info(f"Downloading {platform_name} video: {url}...")
        ydl_opts = {
            'outtmpl': output_template,
            # Prefer a direct progressive MP4 stream; fall back to merging best video+audio into MP4.
            # Explicitly avoid HLS-only selections that leave ffmpeg with a .m3u8 playlist file.
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/bestvideo+bestaudio/best',
            'merge_output_format': 'mp4',
            'hls_prefer_native': False,   # use ffmpeg for HLS muxing — produces a proper MP4 on disk
            'socket_timeout': 20,
            'retries': 3,
            'quiet': True,
            'nocheckcertificate': True,
        }
        if cookiefile and os.path.exists(cookiefile):
            ydl_opts['cookiefile'] = cookiefile
        if proxy:
            ydl_opts['proxy'] = proxy
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
    if clean_url.endswith((".mp4", ".mov", ".mkv", ".webm", ".gif", ".webp", ".m4v")):
        try:
            ext = os.path.splitext(clean_url)[1] or ".mp4"
            out_path = os.path.join(output_dir, f"video_{uuid.uuid4().hex[:8]}{ext}")
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                resp = await client.get(url)
                if resp.status_code == 200 and len(resp.content) > 0:
                    with open(out_path, "wb") as f:
                        f.write(resp.content)
                    return out_path
        except Exception as e:
            logger.warning(f"Direct video download failed ({e}), continuing fallback...")

    # 2. Twitter / X: Fast direct extraction of video MP4
    if TWITTER_URL_PATTERN.search(url):
        tw_match = TWITTER_URL_PATTERN.search(url)
        username, tweet_id = tw_match.groups()
        headers = {"User-Agent": "TelegramBot (like TwitterBot)"}
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(f"https://api.fxtwitter.com/{username}/status/{tweet_id}", headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    tweet = data.get("tweet") or {}
                    media = tweet.get("media") or {}
                    videos = media.get("videos") or []
                    if videos:
                        v_obj = videos[0]
                        v_url = v_obj.get("url")
                        variants = v_obj.get("variants") or []
                        mp4_variants = [v for v in variants if v.get("content_type") == "video/mp4" or ".mp4" in v.get("url", "")]
                        if mp4_variants:
                            mp4_variants.sort(key=lambda x: x.get("bitrate", 0), reverse=True)
                            v_url = mp4_variants[0].get("url") or v_url
                        if v_url:
                            v_resp = await client.get(v_url, timeout=25.0)
                            if v_resp.status_code == 200 and len(v_resp.content) > 0:
                                out_path = os.path.join(output_dir, f"tw_{uuid.uuid4().hex[:8]}.mp4")
                                with open(out_path, "wb") as f:
                                    f.write(v_resp.content)
                                return out_path
        except Exception as tw_err:
            logger.warning(f"Fast Twitter extraction failed for GIF ({tw_err}), falling back...")

    # 3. Instagram: Fast direct extraction of CDN video URL
    if INSTAGRAM_URL_PATTERN.search(url):
        clean_ig_url = sanitize_instagram_url(url)
        try:
            loop = asyncio.get_running_loop()
            ig_proxy = get_instagram_proxy()

            def _extract_ig():
                ydl_opts = {
                    'format': 'best',
                    'quiet': True,
                    'no_warnings': True,
                    'skip_download': True,
                    'socket_timeout': 20,
                }
                if ig_proxy:
                    ydl_opts['proxy'] = ig_proxy
                if INSTAGRAM_COOKIES_FILE and os.path.exists(INSTAGRAM_COOKIES_FILE):
                    ydl_opts['cookiefile'] = INSTAGRAM_COOKIES_FILE
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(clean_ig_url, download=False)
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

            video_cdn_url = await asyncio.wait_for(loop.run_in_executor(None, _extract_ig), timeout=35.0)
            if not video_cdn_url:
                fb_data = await fetch_instagram_via_embed_proxy(clean_ig_url)
                if fb_data and fb_data.get('items') and fb_data['items'][0].get('is_video'):
                    video_cdn_url = fb_data['items'][0]['url']
            if video_cdn_url:
                out_path = os.path.join(output_dir, f"ig_{uuid.uuid4().hex[:8]}.mp4")
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                client_kwargs = {"timeout": 25.0, "follow_redirects": True}
                if ig_proxy:
                    client_kwargs["proxy"] = ig_proxy
                async with httpx.AsyncClient(**client_kwargs) as client:
                    resp = await client.get(video_cdn_url, headers=headers)
                    if resp.status_code == 200 and len(resp.content) > 0:
                        with open(out_path, "wb") as f:
                            f.write(resp.content)
                        return out_path
        except Exception as ig_err:
            logger.warning(f"Fast Instagram extraction failed for GIF ({ig_err}), falling back to yt-dlp...")

    # 4. Generic media download with safe timeout
    try:
        is_ig = bool(INSTAGRAM_URL_PATTERN.search(url))
        cookiefile = INSTAGRAM_COOKIES_FILE if is_ig else None
        proxy = get_instagram_proxy() if is_ig else None
        info = await asyncio.wait_for(
            download_generic_media(url, output_dir, platform_name="Video", cookiefile=cookiefile, proxy=proxy),
            timeout=75.0
        )
        return info.get("filepath")
    except Exception as e:
        logger.error(f"Failed to download video source from {url}: {e}")
        return None


async def execute_generic_media_download(target_message, url: str, platform_name: str, context: ContextTypes.DEFAULT_TYPE, status_msg=None, cookiefile: str = None, proxy: str = None) -> None:
    """Execute download and upload for generic media (TikTok, Instagram, Reddit)."""
    cache_key = get_canonical_media_cache_key(url, platform_name)
    if cache_key:
        cached = url_media_cache.get(cache_key)
        if cached and await send_cached_media(target_message, cached, url, platform_name, status_msg):
            return

    if not status_msg:
        status_msg = await target_message.reply_text(f"⏳ Downloading {platform_name} media...", reply_to_message_id=target_message.message_id)
    else:
        try:
            await status_msg.edit_text(f"⏳ Downloading {platform_name} media...")
        except Exception:
            pass  # e.g. text is already identical to the current status message

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            info = await download_generic_media(url, tmp_dir, platform_name, cookiefile=cookiefile, proxy=proxy)
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
            sent_msg = None
            if is_image:
                with open(filepath, "rb") as photo_file:
                    sent_msg = await target_message.reply_photo(
                        photo=photo_file,
                        caption=caption,
                        parse_mode="HTML",
                        reply_to_message_id=target_message.message_id
                    )
                cache_uploaded_media(cache_key, "photo", sent_msg, title, uploader)
            else:
                with open(filepath, "rb") as video_file:
                    sent_msg = await target_message.reply_video(
                        video=video_file,
                        caption=caption,
                        parse_mode="HTML",
                        duration=duration,
                        supports_streaming=True,
                        reply_to_message_id=target_message.message_id
                    )
                cache_uploaded_media(cache_key, "video", sent_msg, title, uploader, duration)

            try:
                await status_msg.delete()
            except Exception:
                pass

    except Exception as e:
        error_msg = str(e)
        logger.error(f"{platform_name} download failed for {url}: {type(e).__name__}: {e}")
        try:
            await status_msg.edit_text(fmt_error(classify_media_error(error_msg)), parse_mode="HTML")
        except Exception:
            pass
        await send_media_error_dm(context, target_message, url, platform_name, error_msg)


async def fetch_instagram_via_embed_proxy(url: str) -> Optional[dict]:
    """Fallback extractor for Instagram reels and posts using embed fixers (ddinstagram, instagramez).
    Extracts direct CDN media streams and OpenGraph metadata when direct scraping gets 429'd or blocked.
    """
    m = INSTAGRAM_URL_PATTERN.search(url.strip())
    if not m:
        return None
    shortcode = m.group(1)
    is_reel = "/reel/" in url.lower() or "/reels/" in url.lower()
    path_type = "reel" if is_reel else "p"

    proxy_urls = [
        f"https://www.ddinstagram.com/{path_type}/{shortcode}/",
        f"https://instagramez.com/{path_type}/{shortcode}/",
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        for target_url in proxy_urls:
            try:
                resp = await client.get(target_url, headers=headers)
                if resp.status_code != 200:
                    continue
                html_text = resp.text

                # 1. Look for og:video / og:video:secure_url / twitter:player:stream
                video_match = (
                    re.search(r'<meta\s+(?:property|name)=["\'](?:og:video(?::secure_url)?|twitter:player:stream)["\']\s+content=["\']([^"\']+)["\']', html_text, re.IGNORECASE)
                    or re.search(r'<meta\s+content=["\']([^"\']+)["\']\s+(?:property|name)=["\'](?:og:video(?::secure_url)?|twitter:player:stream)["\']', html_text, re.IGNORECASE)
                )

                # 2. Look for og:image / twitter:image
                image_match = (
                    re.search(r'<meta\s+(?:property|name)=["\'](?:og:image|twitter:image)["\']\s+content=["\']([^"\']+)["\']', html_text, re.IGNORECASE)
                    or re.search(r'<meta\s+content=["\']([^"\']+)["\']\s+(?:property|name)=["\'](?:og:image|twitter:image)["\']', html_text, re.IGNORECASE)
                )

                # 3. Look for og:title and og:description
                title_match = (
                    re.search(r'<meta\s+(?:property|name)=["\']og:title["\']\s+content=["\']([^"\']+)["\']', html_text, re.IGNORECASE)
                    or re.search(r'<meta\s+content=["\']([^"\']+)["\']\s+(?:property|name)=["\']og:title["\']', html_text, re.IGNORECASE)
                )
                desc_match = (
                    re.search(r'<meta\s+(?:property|name)=["\'](?:og:description|description)["\']\s+content=["\']([^"\']+)["\']', html_text, re.IGNORECASE)
                    or re.search(r'<meta\s+content=["\']([^"\']+)["\']\s+(?:property|name)=["\'](?:og:description|description)["\']', html_text, re.IGNORECASE)
                )

                items = []
                base_url = str(resp.url)
                if video_match:
                    v_url = urljoin(base_url, html.unescape(video_match.group(1).strip()))
                    items.append({'url': v_url, 'is_video': True})
                elif image_match:
                    img_url = urljoin(base_url, html.unescape(image_match.group(1).strip()))
                    items.append({'url': img_url, 'is_video': False})
                else:
                    continue

                raw_title = html.unescape(title_match.group(1).strip()) if title_match else ""
                raw_desc = html.unescape(desc_match.group(1).strip()) if desc_match else ""
                title = raw_desc or raw_title or "Instagram Post"
                uploader = raw_title if (raw_title and raw_desc and raw_title != raw_desc) else "Instagram"

                logger.info(f"Embed proxy extracted media from {target_url} for {shortcode}")
                return {
                    'items': items,
                    'title': title,
                    'uploader': uploader,
                }
            except Exception as e:
                logger.warning(f"Error querying embed proxy {target_url}: {e}")
                continue

    return None


async def execute_instagram_download(target_message, url: str, context: ContextTypes.DEFAULT_TYPE, status_msg=None) -> None:
    """Download and upload Instagram media, including photo-only and multi-item carousel posts."""
    url = sanitize_instagram_url(url)
    cache_key = get_canonical_media_cache_key(url, "Instagram")
    if cache_key:
        cached = url_media_cache.get(cache_key)
        if cached and await send_cached_media(target_message, cached, url, "Instagram", status_msg):
            return

    if not status_msg:
        status_msg = await target_message.reply_text("⏳ Downloading Instagram media...", reply_to_message_id=target_message.message_id)
    else:
        try:
            await status_msg.edit_text("⏳ Downloading Instagram media...")
        except Exception:
            pass  # e.g. text is already identical to the current status message

    sem = _get_ig_semaphore()
    if sem.locked():
        try:
            await status_msg.edit_text("⏳ Queued for Instagram download...")
        except Exception:
            pass

    async with sem:
        await _wait_for_instagram_slot()
        try:
            await status_msg.edit_text("⏳ Downloading Instagram media...")
        except Exception:
            pass

        try:
            loop = asyncio.get_running_loop()

            def _extract(proxy: Optional[str]):
                ydl_opts = {
                    'format': 'best',
                    'quiet': True,
                    'no_warnings': True,
                    'skip_download': True,
                    'socket_timeout': 20,
                }
                if proxy:
                    ydl_opts['proxy'] = proxy
                if INSTAGRAM_COOKIES_FILE and os.path.exists(INSTAGRAM_COOKIES_FILE):
                    ydl_opts['cookiefile'] = INSTAGRAM_COOKIES_FILE
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

            data = None
            active_proxy = None
            max_attempts = 2
            for attempt in range(max_attempts):
                active_proxy = get_instagram_proxy()
                try:
                    data = await asyncio.wait_for(
                        loop.run_in_executor(None, lambda p=active_proxy: _extract(p)),
                        timeout=45
                    )
                    break
                except Exception as extract_err:
                    err_str = str(extract_err).lower()
                    is_rate_limited = "429" in err_str or "too many requests" in err_str or "timed out" in err_str or "timeout" in err_str
                    if is_rate_limited and attempt < max_attempts - 1:
                        backoff = 3.0 * (attempt + 1)
                        logger.warning(
                            f"Instagram rate limit or timeout on attempt {attempt + 1}. "
                            f"Backing off {backoff:.1f}s before retry..."
                        )
                        await asyncio.sleep(backoff)
                        continue

                    # Direct yt-dlp extraction failed (e.g. 429 datacenter IP block or restriction).
                    # Fall back to embed proxy (ddinstagram / instagramez)
                    logger.info(f"Direct Instagram extraction failed ({extract_err}). Trying embed proxy fallback for {url}...")
                    try:
                        fallback_data = await fetch_instagram_via_embed_proxy(url)
                        if fallback_data and fallback_data.get('items'):
                            logger.info(f"Successfully extracted media via embed proxy fallback for {url}")
                            data = fallback_data
                            break
                    except Exception as fb_err:
                        logger.warning(f"Embed proxy fallback error for {url}: {fb_err}")

                    raise extract_err

            if not data or not data.get('items'):
                raise Exception("No media found in this post.")

            items = data['items']

            title_escaped = html.escape(str(data.get('title') or 'Instagram Post'))
            uploader_escaped = html.escape(str(data.get('uploader') or 'Instagram'))
            caption = f"<b>{title_escaped}</b>\n👤 <i>Source: {uploader_escaped}</i>\n\n🔗 <a href='{url}'>Instagram Link</a>"
            if len(caption) > 1024:
                caption = caption[:1020] + "…"

            await status_msg.edit_text("📤 Uploading to Telegram...")

            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            client_kwargs = {"timeout": 60.0, "follow_redirects": True}
            if active_proxy:
                client_kwargs["proxy"] = active_proxy

            # ── Single Video Stream (direct CDN stream without re-invoking yt-dlp) ──
            if len(items) == 1 and items[0]['is_video']:
                v_url = items[0]['url']
                with tempfile.TemporaryDirectory() as tmp_dir:
                    tmp_video_path = os.path.join(tmp_dir, f"ig_{uuid.uuid4().hex[:8]}.mp4")
                    file_size = 0
                    async with httpx.AsyncClient(**client_kwargs) as client:
                        async with client.stream("GET", v_url, headers=headers) as resp:
                            resp.raise_for_status()
                            with open(tmp_video_path, "wb") as f:
                                async for chunk in resp.aiter_bytes(chunk_size=128 * 1024):
                                    f.write(chunk)
                                    file_size += len(chunk)
                                    if file_size > MAX_UPLOAD_SIZE_MB * 1024 * 1024:
                                        break

                    if file_size > MAX_UPLOAD_SIZE_MB * 1024 * 1024:
                        await status_msg.edit_text(
                            fmt_warning(f"This video exceeds Telegram's {MAX_UPLOAD_SIZE_MB}MB limit ({file_size / (1024*1024):.1f}MB).")
                        )
                        return

                    if not os.path.exists(tmp_video_path) or os.path.getsize(tmp_video_path) == 0:
                        raise Exception("Downloaded Instagram video file is empty.")

                    with open(tmp_video_path, "rb") as video_file:
                        sent_msg = await target_message.reply_video(
                            video=video_file, caption=caption, parse_mode="HTML",
                            supports_streaming=True, reply_to_message_id=target_message.message_id
                        )
                    cache_uploaded_media(cache_key, "video", sent_msg, data.get('title'), data.get('uploader'))

            # ── Photos or Multi-item Carousel ──
            else:
                media_items = []
                async with httpx.AsyncClient(**client_kwargs) as client:
                    for item in items[:10]:
                        try:
                            resp = await client.get(item['url'], headers=headers)
                            if resp.status_code == 200 and len(resp.content) <= MAX_UPLOAD_SIZE_MB * 1024 * 1024:
                                media_items.append({'bytes': resp.content, 'is_video': item['is_video']})
                        except Exception as fetch_err:
                            logger.warning(f"Failed to fetch Instagram media item: {fetch_err}")

                if not media_items:
                    raise Exception("Failed to download any media from this post.")

                if len(media_items) == 1:
                    m = media_items[0]
                    if m['is_video']:
                        sent_msg = await target_message.reply_video(
                            video=m['bytes'], caption=caption, parse_mode="HTML",
                            supports_streaming=True, reply_to_message_id=target_message.message_id
                        )
                        cache_uploaded_media(cache_key, "video", sent_msg, data.get('title'), data.get('uploader'))
                    else:
                        sent_msg = await target_message.reply_photo(
                            photo=m['bytes'], caption=caption, parse_mode="HTML",
                            reply_to_message_id=target_message.message_id
                        )
                        cache_uploaded_media(cache_key, "photo", sent_msg, data.get('title'), data.get('uploader'))
                else:
                    media_group = []
                    for i, m in enumerate(media_items):
                        kwargs = {'caption': caption, 'parse_mode': 'HTML'} if i == 0 else {}
                        if m['is_video']:
                            media_group.append(InputMediaVideo(media=m['bytes'], **kwargs))
                        else:
                            media_group.append(InputMediaPhoto(media=m['bytes'], **kwargs))
                    sent_msgs = await target_message.reply_media_group(media=media_group, reply_to_message_id=target_message.message_id)
                    cache_uploaded_media(cache_key, "media_group", sent_msgs, data.get('title'), data.get('uploader'))

            try:
                await status_msg.delete()
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Instagram download failed for {url}: {type(e).__name__}: {e}")
            try:
                await status_msg.edit_text(fmt_error(classify_media_error(str(e))), parse_mode="HTML")
            except Exception:
                pass
            await send_media_error_dm(context, target_message, url, "Instagram", str(e))

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
            await status_msg.edit_text(fmt_error(classify_media_error(str(e))), parse_mode="HTML")
        except Exception:
            pass
        await send_media_error_dm(context, target_message, url, "Threads", str(e))


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

    cache_key = get_canonical_media_cache_key(canonical_url, "Reddit")
    if cache_key:
        cached = url_media_cache.get(cache_key)
        if cached and await send_cached_media(update.message, cached, canonical_url, "Reddit"):
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
                        sent_msg = await update.message.reply_video(
                            video=video_file,
                            caption=caption,
                            parse_mode="HTML",
                            duration=duration,
                            supports_streaming=True,
                            reply_to_message_id=update.message.message_id
                        )
                    cache_uploaded_media(cache_key, "video", sent_msg, title, uploader, duration)
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
        await send_media_error_dm(context, update.message, canonical_url, "Reddit", str(e))


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


