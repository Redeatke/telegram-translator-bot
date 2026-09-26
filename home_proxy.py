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

# ─── Configuration ────────────────────────────────────────────────────────────

HOST = "0.0.0.0"
PORT = int(os.getenv("RELAY_PORT", "8899"))
AUTH_TOKEN = os.getenv("RELAY_AUTH", "")  # Optional: set to require auth
MAX_DURATION = 1800  # 30 min max video
CLEANUP_AFTER = 300  # Delete temp files after 5 min

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
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(file_size))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()

            with open(filepath, "rb") as f:
                shutil.copyfileobj(f, self.wfile)
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
        """Download a YouTube video using yt-dlp and return metadata + file reference."""
        try:
            import yt_dlp
        except ImportError:
            raise RuntimeError("yt-dlp is not installed. Run: pip install yt-dlp")

        file_id = uuid.uuid4().hex
        tmp_dir = tempfile.gettempdir()
        output_template = os.path.join(tmp_dir, f"ytrelay_{file_id}.%(ext)s")

        fast_format = (
            f"best[ext=mp4][height<={quality}]/"
            f"bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/"
            f"best[height<={quality}]/best"
        )

        ydl_opts = {
            "outtmpl": output_template,
            "merge_output_format": "mp4",
            "format": fast_format,
            "socket_timeout": 15,
            "retries": 3,
            "quiet": True,
            "nocheckcertificate": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)

            # Find the actual file (ext may differ after merge)
            if not os.path.exists(filepath):
                base = os.path.splitext(filepath)[0]
                for ext in [".mp4", ".webm", ".mkv"]:
                    if os.path.exists(base + ext):
                        filepath = base + ext
                        break

            if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
                raise RuntimeError("Download produced no output file")

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


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
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
