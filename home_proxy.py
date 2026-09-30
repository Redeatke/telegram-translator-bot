#!/usr/bin/env python3
"""
YouTube Download Relay — runs on your home PC behind your residential IP.

The Render-hosted bot sends YouTube URLs to this API, which downloads them
locally (through your home IP that YouTube doesn't block) and streams the
file back to the bot.

Setup:
  1. Install dependencies:
       pip install flask yt-dlp

  2. Run this script:
       python home_proxy.py

  3. In another terminal, start ngrok:
       ngrok http 8899

  4. Copy the ngrok HTTPS URL (e.g. https://abc123.ngrok-free.app)
     and set it in Render's environment variables:
       YOUTUBE_RELAY_URL=https://abc123.ngrok-free.app

The bot will automatically use your home PC's residential IP for YouTube
downloads, bypassing datacenter IP blocks.
"""

import os
import sys
import json
import uuid
import time
import shutil
import logging
import tempfile
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

try:
    from pytubefix import YouTube as PytubeFixYouTube
    has_pytubefix = True
except ImportError:
    has_pytubefix = False

try:
    import yt_dlp
    import yt_dlp.plugins
    yt_dlp.plugins.load_plugins = lambda *a, **k: None
except ImportError:
    pass

# ─── Configuration ────────────────────────────────────────────────────────────

HOST = "0.0.0.0"
PORT = int(os.getenv("RELAY_PORT", "8899"))
AUTH_TOKEN = os.getenv("RELAY_AUTH", "")  # Optional: set to require auth
MAX_DURATION = 1800  # 30 min max video
CLEANUP_AFTER = 300  # Delete temp files after 5 min

# Detect Termux environment for resource-constrained settings
IS_TERMUX = bool(os.getenv("TERMUX_VERSION") or (os.getenv("PREFIX", "").startswith("/data/data/com.termux")))

CONCURRENT_FRAGMENTS = int(os.getenv("CONCURRENT_FRAGMENTS", "2" if IS_TERMUX else "4"))

# Relay concurrency limit: prevent CPU/RAM crashes or 429 IP bans on Termux/home PC
MAX_CONCURRENT_RELAY_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "1" if IS_TERMUX else "2"))
_relay_download_semaphore = threading.Semaphore(MAX_CONCURRENT_RELAY_DOWNLOADS)

# Per-strategy timeout: allow enough time for real download + ffmpeg muxing
STRATEGY_TIMEOUT = int(os.getenv("STRATEGY_TIMEOUT", "120" if IS_TERMUX else "180"))

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("yt_relay")

# Note: yt-dlp external script plugins are disabled via compat_opts=['no-plugins'] to prevent slow Deno/Node subprocess hangs

# ─── Temp file cleanup ───────────────────────────────────────────────────────

_temp_files = {}  # {filepath: created_timestamp}

def cleanup_old_files():
    """Periodically remove downloaded temp files."""
    while True:
        time.sleep(60)
        now = time.time()
        to_remove = [f for f, t in _temp_files.items() if now - t > CLEANUP_AFTER]
        for f in to_remove:
            try:
                if os.path.exists(f):
                    os.remove(f)
                _temp_files.pop(f, None)
                logger.info(f"Cleaned up temp file: {f}")
            except Exception as e:
                logger.warning(f"Failed to clean up {f}: {e}")

threading.Thread(target=cleanup_old_files, daemon=True).start()

# ─── HTTP Request Handler ────────────────────────────────────────────────────

class RelayHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        logger.info(f"{self.client_address[0]} - {format % args}")

    def _send_json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self):
        if not AUTH_TOKEN:
            return True
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {AUTH_TOKEN}" or auth == AUTH_TOKEN:
            return True
        self._send_json(401, {"error": "Unauthorized"})
        return False

    def do_GET(self):
        parsed = urlparse(self.path)

        # Health check
        if parsed.path == "/" or parsed.path == "/health":
            self._send_json(200, {"status": "ok", "service": "yt-relay"})
            return

        # File download (stream back to bot)
        if parsed.path.startswith("/file/"):
            if not self._check_auth():
                return
            filename = parsed.path[6:]  # strip /file/
            filepath = os.path.join(tempfile.gettempdir(), filename)
            if not os.path.exists(filepath):
                filepath = os.path.join(tempfile.gettempdir(), f"ytrelay_{filename}")
            if not os.path.exists(filepath):
                self._send_json(404, {"error": "File not found"})
                return

            file_size = os.path.getsize(filepath)

            # Determine content type based on extension
            _, ext = os.path.splitext(filepath)
            content_type = "audio/mpeg" if ext.lower() == ".mp3" else "video/mp4"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(file_size))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()

            with open(filepath, "rb") as f:
                shutil.copyfileobj(f, self.wfile, length=128 * 1024)
            return

        self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/download":
            if not self._check_auth():
                return

            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len)

            try:
                data = json.loads(body)
            except Exception:
                self._send_json(400, {"error": "Invalid JSON"})
                return

            url = data.get("url", "").strip()
            quality = data.get("quality", 720)

            if not url:
                self._send_json(400, {"error": "Missing 'url' field"})
                return

            logger.info(f"Download request: {url} (quality={quality})")

            # Queue downloads so concurrent requests don't crash Termux or get IP banned
            acquired = _relay_download_semaphore.acquire(timeout=600)
            if not acquired:
                self._send_json(503, {"error": "Relay queue timeout: server is busy with other downloads."})
                return

            try:
                result = self._download_video(url, quality)
                self._send_json(200, result)
            except Exception as e:
                logger.error(f"Download failed: {e}")
                self._send_json(500, {"error": str(e)})
            finally:
                _relay_download_semaphore.release()
            return

        self._send_json(404, {"error": "Not found"})

    def _download_video(self, url, quality=720):
        """Download a YouTube video or audio using yt-dlp and return metadata + file reference."""
        try:
            import yt_dlp
        except ImportError:
            raise RuntimeError("yt-dlp is not installed. Run: pip install yt-dlp")

        import concurrent.futures

        file_id = uuid.uuid4().hex
        tmp_dir = tempfile.gettempdir()
        output_template = os.path.join(tmp_dir, f"ytrelay_{file_id}.%(ext)s")

        is_audio = str(quality).lower() in ("audio", "mp3")

        # On Termux, show yt-dlp output so errors are visible
        _quiet = not IS_TERMUX
        _sock_timeout = 15 if IS_TERMUX else 20

        def _run_strategy_with_timeout(ydl_opts):
            def _inner():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    filepath = ydl.prepare_filename(info)

                    if not os.path.exists(filepath):
                        base = os.path.splitext(filepath)[0]
                        for ext in [".mp3", ".m4a", ".mp4", ".webm", ".mkv"]:
                            if os.path.exists(base + ext):
                                filepath = base + ext
                                break

                    if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                        filename = os.path.basename(filepath)
                        file_size = os.path.getsize(filepath)
                        _temp_files[filepath] = time.time()
                        logger.info(f"Downloaded: {info.get('title', 'unknown')} ({file_size} bytes)")
                        return {
                            "title": info.get("title", "Media"),
                            "duration": info.get("duration", 0),
                            "filename": filename,
                            "file_size": file_size,
                            "download_url": f"/file/{filename}",
                        }
                return None

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(_inner)
                try:
                    return fut.result(timeout=STRATEGY_TIMEOUT)
                except concurrent.futures.TimeoutError:
                    if fut.done() and not fut.exception():
                        return fut.result()
                    logger.warning(f"yt-dlp download timed out after {STRATEGY_TIMEOUT}s for {url}")
                    return None
                except Exception as e:
                    logger.warning(f"yt-dlp strategy error: {e}")
                    return None

        if is_audio:
            logger.info(f"Downloading YouTube audio: {url}...")
            # Pytubefix fast audio engine (strictly selects original/default audio track)
            if has_pytubefix:
                try:
                    logger.info(f"Trying pytubefix fast audio engine for {url}...")
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
                        out_name = f"ytrelay_{file_id}.mp3"
                        fp = stream.download(output_path=tmp_dir, filename=out_name)
                        if os.path.exists(fp) and os.path.getsize(fp) > 0:
                            file_size = os.path.getsize(fp)
                            filename = os.path.basename(fp)
                            _temp_files[fp] = time.time()
                            logger.info(f"Downloaded audio via pytubefix: {yt.title} ({file_size} bytes)")
                            return {
                                "title": yt.title or "Audio",
                                "duration": yt.length or 0,
                                "filename": filename,
                                "file_size": file_size,
                                "download_url": f"/file/{filename}",
                            }
                except Exception as pe:
                    logger.warning(f"pytubefix audio failed: {pe}, using yt-dlp...")

            # Dedicated single yt-dlp audio strategy (strictly selects original audio track)
            ydl_opts = {
                "outtmpl": output_template,
                "format": "bestaudio[format_note*=original]/bestaudio[language=original]/bestaudio[language=en]/bestaudio/best",
                "format_sort": ["lang:original", "ext:m4a:10", "quality"],
                "http_headers": {"Accept-Language": "en-US,en;q=0.9"},
                "compat_opts": ["no-plugins"],
                "source_address": "0.0.0.0",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
                "extractor_args": {"youtube": {"player_client": ["android", "ios"]}},
                "concurrent_fragment_downloads": CONCURRENT_FRAGMENTS,
                "socket_timeout": _sock_timeout,
                "retries": 1,
                "extractor_retries": 0,
                "quiet": _quiet,
                "nocheckcertificate": True,
            }
            res = _run_strategy_with_timeout(ydl_opts)
            if res:
                return res
            raise RuntimeError("Failed to download YouTube audio.")

        # Video (720p / Shorts / 1080p)
        try:
            q_val = int(quality)
        except (ValueError, TypeError):
            q_val = 720

        logger.info(f"Downloading YouTube video ({q_val}p): {url}...")
        fast_format = (
            f"bestvideo[height<={q_val}][vcodec^=avc]+bestaudio[format_note*=original][acodec^=mp4a]/"
            f"bestvideo[height<={q_val}][vcodec^=avc]+bestaudio[acodec^=mp4a]/"
            f"bestvideo[height<={q_val}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={q_val}]+bestaudio/"
            f"bestvideo[width<={q_val}]+bestaudio/"
            f"best[height<={q_val}][ext=mp4]/"
            f"best[height<={q_val}]/"
            f"best"
        )
        ydl_opts = {
            "outtmpl": output_template,
            "merge_output_format": "mp4",
            "format": fast_format,
            "format_sort": ["lang:original", "res", "fps"],
            "http_headers": {"Accept-Language": "en-US,en;q=0.9"},
            "compat_opts": ["no-plugins"],
            "source_address": "0.0.0.0",
            "postprocessor_args": {"merger": ["-c:v", "copy", "-c:a", "aac"]},
            "extractor_args": {"youtube": {"player_client": ["android", "ios"]}},
            "concurrent_fragment_downloads": CONCURRENT_FRAGMENTS,
            "socket_timeout": _sock_timeout,
            "retries": 1,
            "extractor_retries": 0,
            "quiet": _quiet,
            "nocheckcertificate": True,
        }

        try:
            res = _run_strategy_with_timeout(ydl_opts)
            if res:
                return res
        except Exception as e:
            logger.warning(f"yt-dlp video failed: {e}")

        # Fallback to pytubefix for video (progressive only to ensure sound)
        if has_pytubefix:
            try:
                logger.info(f"Trying pytubefix video fallback for {url}...")
                yt = PytubeFixYouTube(url)
                stream = yt.streams.filter(progressive=True, file_extension='mp4').get_highest_resolution() or yt.streams.filter(progressive=True).get_highest_resolution()
                if stream:
                    out_name = f"ytrelay_{file_id}.mp4"
                    fp = stream.download(output_path=tmp_dir, filename=out_name)
                    if os.path.exists(fp) and os.path.getsize(fp) > 0:
                        file_size = os.path.getsize(fp)
                        filename = os.path.basename(fp)
                        _temp_files[fp] = time.time()
                        logger.info(f"Downloaded video via pytubefix: {yt.title} ({file_size} bytes)")
                        return {
                            "title": yt.title or "Video",
                            "duration": yt.length or 0,
                            "filename": filename,
                            "file_size": file_size,
                            "download_url": f"/file/{filename}",
                        }
            except Exception as pe:
                logger.warning(f"pytubefix video fallback failed: {pe}")

        raise RuntimeError("Failed to download YouTube video.")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((HOST, PORT), RelayHandler)

    print()
    print("  =================================================")
    print("  YouTube Download Relay")
    print(f"  Running on http://{HOST}:{PORT}")
    if IS_TERMUX:
        print("  Environment: Termux (lightweight mode)")
        print(f"  Concurrent fragments: {CONCURRENT_FRAGMENTS}")
        print(f"  Per-strategy timeout: {STRATEGY_TIMEOUT}s")
    print("  =================================================")
    print()
    if not IS_TERMUX:
        print("  Next steps:")
        print(f"    1. Open another terminal and run:")
        print(f"       ngrok http {PORT}")
        print(f"    2. Copy the Forwarding HTTPS URL from ngrok")
        print(f"    3. Set YOUTUBE_RELAY_URL in your deployment:")
        print(f"       YOUTUBE_RELAY_URL=https://<ngrok-url>")
        print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down relay...")
        server.server_close()


if __name__ == "__main__":
    main()
