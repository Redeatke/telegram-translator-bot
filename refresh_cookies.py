#!/usr/bin/env python3
"""
YouTube Cookie Refresher — Reads fresh cookies directly from your local browser
and automatically pushes them to your Render service as YOUTUBE_COOKIES env var,
then triggers a redeploy so the bot picks them up immediately.

Usage:
    python refresh_cookies.py                  # uses Chrome by default
    python refresh_cookies.py --browser firefox
    python refresh_cookies.py --browser edge
    python refresh_cookies.py --no-push        # export only, don't push to Render

Requirements:
    pip install yt-dlp requests python-dotenv

Render credentials (add to .env or set as env vars):
    RENDER_API_KEY=rnd_xxxxxxxxxxxx
    RENDER_SERVICE_ID=srv-xxxxxxxxxxxx

Schedule this script via Windows Task Scheduler to run every 5 days for
fully automated cookie refresh.
"""

import sys
import os
import argparse
import tempfile
from pathlib import Path

# ── Dependency checks ────────────────────────────────────────────────────────
missing = []
try:
    import yt_dlp
except ImportError:
    missing.append("yt-dlp")

try:
    import requests
except ImportError:
    missing.append("requests")

if missing:
    print(f"❌ Missing packages: {', '.join(missing)}")
    print(f"   Run: pip install {' '.join(missing)}")
    sys.exit(1)

# Load .env if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Configuration ─────────────────────────────────────────────────────────────
RENDER_API_KEY    = os.getenv("RENDER_API_KEY", "")
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "")

SUPPORTED_BROWSERS = ["chrome", "firefox", "edge", "brave", "chromium", "opera", "vivaldi"]


# ── Cookie extraction ─────────────────────────────────────────────────────────

def extract_cookies_from_browser(browser: str, output_path: str) -> bool:
    """
    Use yt-dlp to read cookies directly from the browser's local database.
    Works without opening the browser — yt-dlp copies the DB first to avoid
    file-lock issues on Windows.
    """
    print(f"  📂  Reading YouTube cookies from {browser.title()}...")

    ydl_opts = {
        "cookiefile": output_path,
        "cookiesfrombrowser": (browser, None, None, None),
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info("https://www.youtube.com", download=False)
    except Exception as e:
        err = str(e).lower()
        if "cookies" in err or "browser" in err or "profile" in err:
            print(f"  ❌  Browser error: {e}")
            return False
        # Other errors (network etc.) are fine — cookies may still be written
        pass

    if not os.path.exists(output_path) or os.path.getsize(output_path) < 100:
        return False

    return True


# ── Render API ────────────────────────────────────────────────────────────────

def push_cookies_to_render(cookie_content: str) -> bool:
    """Update YOUTUBE_COOKIES on Render and trigger a redeploy."""

    if not RENDER_API_KEY or not RENDER_SERVICE_ID:
        print()
        print("  ⚠️   Render credentials not set — skipping auto-push.")
        print("  Add these to your .env file:")
        print("    RENDER_API_KEY=rnd_xxxx")
        print("    RENDER_SERVICE_ID=srv-xxxx")
        print()
        print("  You can find your Service ID in the Render dashboard URL:")
        print("  https://dashboard.render.com/web/srv-XXXXXXXX")
        print()
        print("  And your API key at: https://dashboard.render.com/u/settings/api-keys")
        return False

    headers = {
        "Authorization": f"Bearer {RENDER_API_KEY}",
        "Content-Type": "application/json",
    }

    # Step 1: Update env var
    print("  📤  Pushing cookies to Render env vars...")
    resp = requests.put(
        f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars",
        headers=headers,
        json=[{"key": "YOUTUBE_COOKIES", "value": cookie_content}],
        timeout=15,
    )

    if resp.status_code not in (200, 201):
        print(f"  ❌  Failed to update env var ({resp.status_code}): {resp.text[:200]}")
        return False

    print("  ✅  YOUTUBE_COOKIES updated on Render!")

    # Step 2: Trigger redeploy so bot picks up the new cookies
    print("  🚀  Triggering Render redeploy...")
    resp = requests.post(
        f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/deploys",
        headers=headers,
        json={"clearCache": "do_not_clear"},
        timeout=15,
    )

    if resp.status_code in (200, 201):
        print("  ✅  Redeploy triggered! Bot will have fresh cookies in ~1-2 min.")
        return True
    else:
        print(f"  ⚠️   Redeploy trigger failed ({resp.status_code}) — but cookies were saved.")
        print("  Manually redeploy from the Render dashboard.")
        return True  # Cookies updated even if redeploy failed


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Refresh YouTube cookies and push to Render"
    )
    parser.add_argument(
        "--browser", default="chrome",
        choices=SUPPORTED_BROWSERS,
        help="Browser to extract cookies from (default: chrome)"
    )
    parser.add_argument(
        "--no-push", action="store_true",
        help="Extract cookies locally but don't push to Render"
    )
    args = parser.parse_args()

    print()
    print("=" * 55)
    print("  🍪  YouTube Cookie Refresher")
    print("=" * 55)
    print()
    print(f"  Browser : {args.browser.title()}")
    print(f"  Push    : {'Disabled (--no-push)' if args.no_push else 'Render API'}")
    print()

    # Make sure the user is logged into YouTube in the chosen browser
    print("  ──────────────────────────────────────────────────")
    print(f"  Make sure you are logged into YouTube in {args.browser.title()}.")
    print("  The browser does NOT need to be open.")
    print("  ──────────────────────────────────────────────────")
    print()

    # Extract cookies to a temp file, then read content
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tmp:
        tmp_path = tmp.name

    try:
        ok = extract_cookies_from_browser(args.browser, tmp_path)

        if not ok:
            print()
            print(f"  ❌  Could not extract cookies from {args.browser.title()}.")
            print("  Possible reasons:")
            print("   • You are not logged into YouTube in that browser")
            print("   • The browser profile path is non-standard")
            print(f"  Try a different browser: python refresh_cookies.py --browser firefox")
            sys.exit(1)

        cookie_content = Path(tmp_path).read_text(encoding="utf-8")
        cookie_lines = [
            l for l in cookie_content.splitlines()
            if l.strip() and not l.startswith("#")
        ]
        print(f"  ✅  Extracted {len(cookie_lines)} cookies.")
        print()

        # Save a local cookies.txt copy
        Path("cookies.txt").write_text(cookie_content, encoding="utf-8")
        print("  💾  Saved to cookies.txt")

        if args.no_push:
            print()
            print("  --no-push: Skipping Render push.")
            print("  Manually add the contents of cookies.txt to")
            print("  YOUTUBE_COOKIES in your Render environment variables.")
        else:
            print()
            push_cookies_to_render(cookie_content)

    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    print()
    print("─" * 55)
    print("  To schedule this automatically, run:")
    print("  python schedule_refresh.py")
    print("─" * 55)
    print()


if __name__ == "__main__":
    main()
