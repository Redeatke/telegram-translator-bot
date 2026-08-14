#!/usr/bin/env python3
"""
YouTube OAuth Setup — Run this locally ONCE to authenticate yt-dlp.

This uses YouTube's TV/device OAuth flow (no headless browser, no cookies).
The refresh token lasts for months and is automatically renewed by yt-dlp
on every download — no manual cookie refreshing ever needed.

Usage:
    pip install yt-dlp
    python setup_oauth.py

After authenticating, paste the printed YOUTUBE_OAUTH_DATA value into
your Render environment variables and redeploy.
"""

import sys
import json
import base64
import subprocess

# ── Check yt-dlp is installed ────────────────────────────────────────────────
try:
    import yt_dlp
except ImportError:
    print("❌ yt-dlp is not installed.")
    print("   Run: pip install yt-dlp")
    sys.exit(1)


def main():
    print()
    print("=" * 58)
    print("  🔐  YouTube OAuth Setup — Telegram Translator Bot")
    print("=" * 58)
    print()
    print("  How this works:")
    print("  ─────────────────────────────────────────────────────")
    print("  yt-dlp will display a short URL and a code.")
    print("  Open the URL in any browser, enter the code,")
    print("  and sign in with your dedicated YouTube account.")
    print()
    print("  ⚠️  Use a DEDICATED Google account — not your personal one.")
    print("  ⚠️  Disable 2FA on that account before continuing.")
    print()
    print("  Starting authentication flow...")
    print("  ─────────────────────────────────────────────────────")
    print()

    # Trigger the OAuth device code flow.
    # --skip-download means yt-dlp authenticates but doesn't download anything.
    # The token is stored in yt-dlp's local cache automatically.
    result = subprocess.run(
        [
            sys.executable, "-m", "yt_dlp",
            "--username", "oauth2",
            "--password", "",
            "--skip-download",
            "--no-playlist",
            "https://www.youtube.com/watch?v=BaW_jenozKc",
        ]
    )

    print()

    if result.returncode != 0:
        print("⚠️  yt-dlp exited with an error, but the token may still have been saved.")
        print("   Continuing to check for a cached token...\n")

    # Read the token from yt-dlp's cache using its own API.
    # This is platform-agnostic — yt-dlp knows the right path for each OS.
    try:
        with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
            token_data = ydl.cache.load("youtube", "oauth_access_token_info")
    except Exception as e:
        print(f"❌ Failed to read yt-dlp cache: {e}")
        sys.exit(1)

    if not token_data:
        print("❌ No OAuth token found in yt-dlp cache.")
        print("   Make sure you completed the browser login step and try again.")
        sys.exit(1)

    # Encode the token as base64 JSON for safe storage in an env var
    encoded = base64.b64encode(json.dumps(token_data).encode("utf-8")).decode("utf-8")

    print("✅  Authentication successful!")
    print()
    print("─" * 58)
    print("  Add the following to Render → Environment Variables:")
    print("─" * 58)
    print()
    print(f"  Key:   YOUTUBE_OAUTH_DATA")
    print(f"  Value: {encoded}")
    print()
    print("─" * 58)
    print("  Next steps:")
    print("  1. Copy the Value above (the long base64 string)")
    print("  2. Go to Render → your service → Environment")
    print("  3. Add YOUTUBE_OAUTH_DATA = <paste value>")
    print("  4. Save → Render will redeploy automatically")
    print()
    print("  The token refresh is handled automatically by yt-dlp.")
    print("  You should not need to run this script again for months.")
    print("─" * 58)
    print()

    # Also save a local copy in case you need it later
    with open("oauth_data.txt", "w", encoding="utf-8") as f:
        f.write(f"YOUTUBE_OAUTH_DATA={encoded}\n")
    print(f"  (Also saved to oauth_data.txt in this directory)")
    print()


if __name__ == "__main__":
    main()
