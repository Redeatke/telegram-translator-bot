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
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

try:
    from pytubefix import YouTube as PytubeFixYouTube
    has_pytubefix = True
except ImportError:
    has_pytubefix = False

# ─── Configuration ────────────────────────────────────────────────────────────

HOST = "0.0.0.0"
PORT = int(os.getenv("RELAY_PORT", "8899"))
AUTH_TOKEN = os.getenv("RELAY_AUTH", "")  # Optional: set to require auth
MAX_DURATION = 1800  # 30 min max video
CLEANUP_AFTER = 300  # Delete temp files after 5 min
CONCURRENT_FRAGMENTS = int(os.getenv("CONCURRENT_FRAGMENTS", "5"))

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("yt_relay")

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

            try:
                result = self._download_video(url, quality)
                self._send_json(200, result)
            except Exception as e:
                logger.error(f"Download failed: {e}")
                self._send_json(500, {"error": str(e)})
            return

        self._send_json(404, {"error": "Not found"})

    def _download_video(self, url, quality=720):
        """Download a YouTube video or audio using yt-dlp and return metadata + file reference."""
        try:
            import yt_dlp
        except ImportError:
            raise RuntimeError("yt-dlp is not installed. Run: pip install yt-dlp")

        file_id = uuid.uuid4().hex
        tmp_dir = tempfile.gettempdir()
        output_template = os.path.join(tmp_dir, f"ytrelay_{file_id}.%(ext)s")

        is_audio = str(quality).lower() in ("audio", "mp3")

        if is_audio:
            ydl_opts_list = [
                {
                    "outtmpl": output_template,
                    "format": "bestaudio/best",
                    "postprocessors": [{
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }],
                    "extractor_args": {"youtube": {"player_client": ["ios", "mweb"]}},
                    "js_runtimes": {"node": {}, "deno": {}},
                    "concurrent_fragment_downloads": CONCURRENT_FRAGMENTS,
                    "socket_timeout": 20,
                    "retries": 3,
                    "quiet": True,
                    "nocheckcertificate": True,
                },
                {
                    "outtmpl": output_template,
                    "format": "bestaudio/best",
                    "postprocessors": [{
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }],
                    "extractor_args": {"youtube": {"player_client": ["android", "tv"]}},
                    "js_runtimes": {"node": {}, "deno": {}},
                    "concurrent_fragment_downloads": CONCURRENT_FRAGMENTS,
                    "socket_timeout": 20,
                    "retries": 3,
                    "quiet": True,
                    "nocheckcertificate": True,
                },
            ]
        else:
            try:
                q_val = int(quality)
            except (ValueError, TypeError):
                q_val = 720

            fast_format = (
                f"best[ext=mp4][height<={q_val}]/"
                f"bestvideo[height<={q_val}]+bestaudio/best[height<={q_val}]/best"
            )

            ydl_opts_list = [
                {
                    "outtmpl": output_template,
                    "merge_output_format": "mp4",
                    "format": fast_format,
                    "extractor_args": {"youtube": {"player_client": ["ios", "mweb"]}},
                    "js_runtimes": {"node": {}, "deno": {}},
                    "concurrent_fragment_downloads": CONCURRENT_FRAGMENTS,
                    "socket_timeout": 20,
                    "retries": 3,
                    "quiet": True,
                    "nocheckcertificate": True,
                },
                {
                    "outtmpl": output_template,
                    "merge_output_format": "mp4",
                    "format": fast_format,
                    "extractor_args": {"youtube": {"player_client": ["android", "tv"]}},
                    "js_runtimes": {"node": {}, "deno": {}},
                    "concurrent_fragment_downloads": CONCURRENT_FRAGMENTS,
                    "socket_timeout": 20,
                    "retries": 3,
                    "quiet": True,
                    "nocheckcertificate": True,
                },
                {
                    "outtmpl": output_template,
                    "merge_output_format": "mp4",
                    "format": f"best[height<={q_val}]/best",
                    "js_runtimes": {"node": {}, "deno": {}},
                    "concurrent_fragment_downloads": CONCURRENT_FRAGMENTS,
                    "socket_timeout": 20,
                    "retries": 3,
                    "quiet": True,
                    "nocheckcertificate": True,
                },
            ]

        last_err = None
        for i, ydl_opts in enumerate(ydl_opts_list, 1):
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    filepath = ydl.prepare_filename(info)

                    # Find the actual file (ext may differ after merge or audio extraction)
                    if not os.path.exists(filepath):
                        base = os.path.splitext(filepath)[0]
                        for ext in [".mp3", ".m4a", ".mp4", ".webm", ".mkv"]:
                            if os.path.exists(base + ext):
                                filepath = base + ext
                                break

                    if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                        filename = os.path.basename(filepath)
                        file_size = os.path.getsize(filepath)

                        # Track for cleanup
                        _temp_files[filepath] = time.time()

                        logger.info(f"Downloaded: {info.get('title', 'unknown')} ({file_size} bytes)")

                        return {
                            "title": info.get("title", "Video"),
                            "duration": info.get("duration", 0),
                            "filename": filename,
                            "file_size": file_size,
                            "download_url": f"/file/{filename}",
                        }
            except Exception as e:
                last_err = e
                logger.warning(f"Relay download strategy {i} failed: {e}")
                if i < len(ydl_opts_list):
                    time.sleep(1)

        # Fallback to pytubefix if available
        if has_pytubefix:
            try:
                logger.info(f"Trying pytubefix fallback for {url}...")
                yt = PytubeFixYouTube(url)
                if is_audio:
                    stream = yt.streams.filter(only_audio=True).first()
                else:
                    stream = yt.streams.filter(progressive=True, file_extension='mp4').get_highest_resolution() or yt.streams.filter(file_extension='mp4').first()
                if stream:
                    ext = "mp3" if is_audio else "mp4"
                    out_name = f"ytrelay_{file_id}.{ext}"
                    fp = stream.download(output_path=tmp_dir, filename=out_name)
                    if os.path.exists(fp) and os.path.getsize(fp) > 0:
                        file_size = os.path.getsize(fp)
                        filename = os.path.basename(fp)
                        _temp_files[fp] = time.time()
                        logger.info(f"Downloaded via pytubefix: {yt.title} ({file_size} bytes)")
                        return {
                            "title": yt.title or "Media",
                            "duration": yt.length or 0,
                            "filename": filename,
                            "file_size": file_size,
                            "download_url": f"/file/{filename}",
                        }
            except Exception as pe:
                logger.warning(f"pytubefix fallback failed: {pe}")

        raise RuntimeError(f"Relay download failed across all strategies: {last_err}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    HTTPServer.allow_reuse_address = True
    server = HTTPServer((HOST, PORT), RelayHandler)

    print()
    print("  =================================================")
    print("  YouTube Download Relay")
    print(f"  Running on http://{HOST}:{PORT}")
    print("  =================================================")
    print()
    print("  Next steps:")
    print(f"    1. Open another terminal and run:")
    print(f"       ngrok http {PORT}")
    print(f"    2. Copy the Forwarding HTTPS URL from ngrok")
    print(f"    3. Set YOUTUBE_RELAY_URL in Render environment:")
    print(f"       YOUTUBE_RELAY_URL=https://<ngrok-url>")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down relay...")
        server.server_close()


if __name__ == "__main__":
    main()
