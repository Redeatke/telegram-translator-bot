"""
Quote Sticker & GIF Module
──────────────────────────
Generates quote card stickers from chat messages and manages per-user sticker packs.
Also handles /gif command for converting videos to GIFs.

Commands handled:
  /q          – Quote a replied message as a static sticker (card for text, raw for image)
  /q r        – Quote with reply context (shows both users)
  /q N        – Quote last N messages as individual stickers
  /q 2 5      – For video replies: clip from 2s to 5s as a video sticker
  /gif        – Convert a replied video to GIF
  /gif 4 10   – Convert video clip from 4s to 10s to GIF
"""

import io
import os
import re
import logging
import asyncio
import tempfile
import uuid
import subprocess
from typing import Optional, List, Tuple

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# ─── Telegram Sticker Constants ────────────────────────────────────────────────

STICKER_SIZE = 512          # One side must be exactly 512px
MAX_STICKERS_PER_PACK = 120  # Telegram limit
MAX_VIDEO_STICKER_DURATION = 3  # seconds
MAX_VIDEO_STICKER_FPS = 30
MAX_VIDEO_STICKER_SIZE_KB = 256
DEFAULT_GIF_DURATION = 6.0   # seconds — ideal default length for crisp, fast-loading GIFs
MAX_GIF_DURATION = 15.0       # seconds — max ceiling for GIF duration to prevent CPU freeze / oversized files

# ─── Card Design Colors & Fonts ────────────────────────────────────────────────
# Design: Glassmorphism dark

CARD_BG_PRIMARY = (26, 27, 46)       # #1a1b2e
CARD_BG_SECONDARY = (13, 14, 26)     # #0d0e1a
CARD_BORDER = (60, 65, 90)           # Subtle border
CARD_TEXT_PRIMARY = (240, 242, 245)   # White text
CARD_TEXT_SECONDARY = (140, 150, 170) # Muted text
CARD_ACCENT_CYAN = (0, 220, 220)     # Cyan glow
CARD_ACCENT_PURPLE = (140, 80, 220)  # Purple accent
CARD_REPLY_BG = (35, 38, 55)         # Reply container background

# Avatar color palette — vibrant, varied colors for initial-letter placeholder avatars
AVATAR_COLORS = [
    (0, 170, 190),    # Teal
    (140, 80, 220),   # Purple
    (220, 80, 100),   # Crimson
    (50, 170, 100),   # Emerald
    (230, 140, 40),   # Amber
    (70, 130, 220),   # Azure
    (200, 70, 160),   # Magenta
    (100, 180, 60),   # Lime
    (200, 100, 50),   # Burnt orange
    (100, 70, 200),   # Indigo
]

# ─── Font Loading ──────────────────────────────────────────────────────────────

FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")


def _load_font(font_names: list, size: int) -> ImageFont.FreeTypeFont:
    """Try to load a font from bundled fonts or system candidates, falling back to default."""
    # 1. Check bundled fonts in assets/fonts/
    for name in font_names:
        base_name = os.path.basename(name)
        bundled_path = os.path.join(FONTS_DIR, base_name)
        if os.path.isfile(bundled_path):
            try:
                return ImageFont.truetype(bundled_path, size)
            except Exception:
                pass

    # 2. Check system font paths
    for name in font_names:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue

    # 3. Fallback to Pillow's load_default (with size if supported)
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


FONT_USERNAME = lambda size=22: _load_font(
    ["DejaVuSans-Bold.ttf", "arialbd.ttf", "segoeuib.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    size
)
FONT_BODY = lambda size=20: _load_font(
    ["DejaVuSans.ttf", "segoeui.ttf", "arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    size
)
FONT_META = lambda size=14: _load_font(
    ["DejaVuSans.ttf", "arial.ttf", "segoeui.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    size
)
FONT_REPLY_NAME = lambda size=16: _load_font(
    ["DejaVuSans-Bold.ttf", "arialbd.ttf", "segoeuib.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    size
)
FONT_REPLY_TEXT = lambda size=15: _load_font(
    ["DejaVuSans.ttf", "arial.ttf", "segoeui.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    size
)
FONT_EMOJI = lambda size=20: _load_font(
    ["NotoEmoji.ttf", "NotoEmoji-Bold.ttf", "NotoEmoji-Regular.ttf",
     "Segoe UI Emoji.ttf",
     "/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf",
     "/usr/share/fonts/noto-emoji/NotoEmoji-Regular.ttf"],
    size
)

# Detect if a real emoji font is available (vs Pillow's fallback bitmap font)
_emoji_font_available = False
try:
    _test_emoji_font = FONT_EMOJI(20)
    _emoji_font_available = isinstance(_test_emoji_font, ImageFont.FreeTypeFont)
    if _emoji_font_available:
        logger.info("Emoji font loaded successfully for quote cards.")
except Exception:
    pass


# ─── Emoji Detection & Dual-Font Rendering ────────────────────────────────────

def _is_emoji_char(ch: str) -> bool:
    """Check if a single character is an emoji that needs the emoji font."""
    cp = ord(ch)
    return (
        0x1F600 <= cp <= 0x1F64F or  # Emoticons (😀-🙏)
        0x1F300 <= cp <= 0x1F5FF or  # Misc Symbols and Pictographs (🌀-🗿)
        0x1F680 <= cp <= 0x1F6FF or  # Transport and Map (🚀-🛿)
        0x1F900 <= cp <= 0x1F9FF or  # Supplemental Symbols (🤐-🧿)
        0x1FA00 <= cp <= 0x1FA6F or  # Chess Symbols
        0x1FA70 <= cp <= 0x1FAFF or  # Symbols Extended-A (🩰-🫿)
        0x2600 <= cp <= 0x26FF or    # Misc symbols (☀☂⚡ etc.)
        0x2700 <= cp <= 0x27BF or    # Dingbats (✂✈✉ etc.)
        0xFE00 <= cp <= 0xFE0F or    # Variation Selectors
        cp == 0x200D or              # Zero Width Joiner (emoji sequences)
        cp == 0x20E3 or              # Combining Enclosing Keycap
        0x1F1E0 <= cp <= 0x1F1FF or  # Regional Indicator Symbols (flags)
        0xE0020 <= cp <= 0xE007F or  # Tags (flag sub-regions)
        0x2300 <= cp <= 0x23FF or    # Misc Technical (⌚⏰ etc.)
        0x2B05 <= cp <= 0x2B07 or    # Arrows ⬅⬆⬇
        0x2B1B <= cp <= 0x2B1C or    # Squares ⬛⬜
        cp == 0x2B50 or              # Star ⭐
        cp == 0x2B55 or              # Circle ⭕
        0x2934 <= cp <= 0x2935 or    # Curved arrows ⤴⤵
        cp == 0x3030 or              # Wavy dash 〰
        cp == 0x303D or              # Part alternation mark 〽
        cp == 0x3297 or              # ㊗
        cp == 0x3299 or              # ㊙
        cp == 0x00A9 or              # ©
        cp == 0x00AE or              # ®
        cp == 0x2122 or              # ™
        0x231A <= cp <= 0x231B or    # ⌚⌛
        0x23E9 <= cp <= 0x23F3 or    # ⏩-⏳
        0x23F8 <= cp <= 0x23FA or    # ⏸-⏺
        0x25AA <= cp <= 0x25AB or    # ▪▫
        cp == 0x25B6 or              # ▶
        cp == 0x25C0 or              # ◀
        0x25FB <= cp <= 0x25FE or    # ◻◼◽◾
        cp == 0x2611 or              # ☑
        cp == 0x2614 or              # ☔
        cp == 0x2615 or              # ☕
        0x2648 <= cp <= 0x2653 or    # Zodiac ♈-♓
        cp == 0x267F or              # ♿
        cp == 0x2693 or              # ⚓
        cp == 0x26A1 or              # ⚡
        0x2702 <= cp <= 0x2704 or    # ✂-✄
        0x270A <= cp <= 0x270D or    # ✊-✍
        cp == 0x270F or              # ✏
        cp == 0x2712 or              # ✒
        cp == 0x2714 or              # ✔
        cp == 0x2716 or              # ✖
        cp == 0x271D or              # ✝
        cp == 0x2721 or              # ✡
        cp == 0x2728 or              # ✨
        cp == 0x2733 or              # ✳
        cp == 0x2734 or              # ✴
        cp == 0x2744 or              # ❄
        cp == 0x2747 or              # ❇
        cp == 0x274C or              # ❌
        cp == 0x274E or              # ❎
        0x2753 <= cp <= 0x2755 or    # ❓❔❕
        cp == 0x2757 or              # ❗
        0x2763 <= cp <= 0x2764 or    # ❣❤
        0x2795 <= cp <= 0x2797 or    # ➕➖➗
        cp == 0x27A1 or              # ➡
        cp == 0x27B0 or              # ➰
        cp == 0x27BF or              # ➿
        0x1F700 <= cp <= 0x1F77F or  # Alchemical Symbols
        0x1F780 <= cp <= 0x1F7FF or  # Geometric Shapes Extended
        0x1F800 <= cp <= 0x1F8FF     # Supplemental Arrows-C
    )


def _split_emoji_segments(text: str) -> List[tuple]:
    """Split text into runs of (text, is_emoji) for dual-font rendering."""
    if not text:
        return []
    segments = []
    current_chars = []
    current_is_emoji = _is_emoji_char(text[0])
    for ch in text:
        is_emoji = _is_emoji_char(ch)
        if is_emoji == current_is_emoji:
            current_chars.append(ch)
        else:
            segments.append(("".join(current_chars), current_is_emoji))
            current_chars = [ch]
            current_is_emoji = is_emoji
    if current_chars:
        segments.append(("".join(current_chars), current_is_emoji))
    return segments


def _measure_text_width(text: str, font, emoji_font, draw: ImageDraw.ImageDraw) -> int:
    """Measure text pixel width, using the correct font for each emoji/text segment."""
    if not text:
        return 0
    if not emoji_font or not _emoji_font_available:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]
    total = 0.0
    for seg_text, is_emoji in _split_emoji_segments(text):
        f = emoji_font if is_emoji else font
        try:
            total += f.getlength(seg_text)
        except AttributeError:
            bbox = draw.textbbox((0, 0), seg_text, font=f)
            total += bbox[2] - bbox[0]
    return int(total)


def _draw_text_with_emoji(draw: ImageDraw.ImageDraw, pos: tuple, text: str,
                          font, emoji_font, fill):
    """Draw text at `pos`, using emoji_font for emoji characters."""
    if not text:
        return
    if not emoji_font or not _emoji_font_available:
        draw.text(pos, text, font=font, fill=fill)
        return
    x, y = pos
    for seg_text, is_emoji in _split_emoji_segments(text):
        f = emoji_font if is_emoji else font
        draw.text((int(x), y), seg_text, font=f, fill=fill)
        try:
            x += f.getlength(seg_text)
        except AttributeError:
            bbox = draw.textbbox((int(x), y), seg_text, font=f)
            x = bbox[2]


# ─── Text Wrapping ─────────────────────────────────────────────────────────────

def _wrap_text(text: str, font, max_width: int, draw: ImageDraw.ImageDraw,
               emoji_font=None) -> List[str]:
    """Wrap text to fit within max_width pixels, with emoji-aware measurement."""
    lines = []
    paragraphs = text.split("\n")
    for p in paragraphs:
        if not p.strip():
            lines.append("")
            continue
        words = p.split(" ")
        current_line = []
        for word in words:
            test_line = " ".join(current_line + [word])
            width = _measure_text_width(test_line, font, emoji_font, draw)
            if width <= max_width:
                current_line.append(word)
            else:
                if current_line:
                    lines.append(" ".join(current_line))
                    current_line = [word]
                else:
                    lines.append(word)
                    current_line = []
        if current_line:
            lines.append(" ".join(current_line))
    return lines


def _clean_text(text: str) -> str:
    """Clean text for rendering (remove excessive whitespace)."""
    if not text:
        return ""
    cleaned = re.sub(r"[ \t]+", " ", text)
    lines = [line.strip() for line in cleaned.split("\n")]
    return "\n".join(lines).strip()


# ─── Avatar Drawing ────────────────────────────────────────────────────────────

def _get_avatar_color(name: str) -> tuple:
    """Get a consistent, vibrant avatar background color from a username."""
    h = sum(ord(c) for c in name) if name else 0
    return AVATAR_COLORS[h % len(AVATAR_COLORS)]


def _draw_circular_avatar(img: Image.Image, avatar_bytes: bytes, x: int, y: int, size: int = 44):
    """Draw a circular avatar onto the image at position (x, y)."""
    try:
        raw_avatar = Image.open(io.BytesIO(avatar_bytes)).resize(
            (size, size), Image.Resampling.LANCZOS
        ).convert("RGBA")
        mask = Image.new("L", (size, size), 0)
        draw_mask = ImageDraw.Draw(mask)
        draw_mask.ellipse((0, 0, size, size), fill=255)
        circular = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        circular.paste(raw_avatar, (0, 0), mask=mask)
        img.paste(circular, (x, y), mask=circular)
        return True
    except Exception:
        return False


def _draw_avatar_placeholder(draw: ImageDraw.ImageDraw, x: int, y: int, size: int,
                             name: str, glow: bool = True):
    """Draw a placeholder circular avatar with the user's initial and vibrant per-user color."""
    cx, cy = x + size // 2, y + size // 2
    radius = size // 2
    bg_color = _get_avatar_color(name)
    initial = (name.strip()[:1] if name else "?").upper()

    if glow:
        # Glow ring in the user's color
        for r_offset in range(3, 0, -1):
            draw.ellipse(
                [(cx - radius - r_offset, cy - radius - r_offset),
                 (cx + radius + r_offset, cy + radius + r_offset)],
                outline=bg_color, width=1
            )

    # Main circle with user-specific vibrant color
    draw.ellipse([(x, y), (x + size, y + size)], fill=bg_color, outline=None)

    # Initial letter / emoji (white on colored background)
    font_size = int(size * 0.45)
    if _is_emoji_char(initial) and _emoji_font_available:
        font = FONT_EMOJI(font_size)
    else:
        font = FONT_USERNAME(font_size)

    bbox = draw.textbbox((0, 0), initial, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text((cx - tw // 2, cy - th // 2 - 2), initial, font=font, fill=(255, 255, 255))


# ─── Quote Card Generation (for text messages) ────────────────────────────────

def generate_quote_card(
    username: str,
    text: str,
    avatar_bytes: Optional[bytes] = None,
    reply_username: Optional[str] = None,
    reply_text: Optional[str] = None,
    reply_avatar_bytes: Optional[bytes] = None,
    timestamp: Optional[str] = None,
) -> bytes:
    """
    Generate a Telegram/Quotly-style quote card as a 512px WEBP sticker.
    Matches Telegram's native message structure:
      - Quoted user's avatar on top-left
      - Quoted user's name on top (vibrant accent color)
      - If reply context: indented reply container with vertical accent bar
      - Quoted message body text below
      - Snug card bounding box scaled to 512px without excess empty canvas
    """
    text = _clean_text(text) or "(no text)"
    username = _clean_text(username) or "User"

    PADDING = 18
    AVATAR_SIZE = 48

    font_user = FONT_USERNAME(22)
    font_body = FONT_BODY(21)
    font_reply_name = FONT_REPLY_NAME(17)
    font_reply_text_font = FONT_REPLY_TEXT(16)

    emoji_font = FONT_EMOJI(21) if _emoji_font_available else None
    emoji_font_small = FONT_EMOJI(16) if _emoji_font_available else None

    dummy_img = Image.new("RGBA", (1, 1))
    dd = ImageDraw.Draw(dummy_img)

    content_x = PADDING + AVATAR_SIZE + 14
    max_content_w = 480 - content_x - PADDING

    # 1. Author username width
    user_w = _measure_text_width(username, font_user, emoji_font, dd)
    header_w = user_w

    # 2. Reply section measurement
    has_reply = bool(reply_username and reply_text)
    reply_lines = []
    reply_w = 0
    reply_h = 0
    rline_h = 20
    if has_reply:
        reply_clean = _clean_text(reply_text)
        reply_lines = _wrap_text(reply_clean, font_reply_text_font, max_content_w - 24, dd, emoji_font=emoji_font_small)
        if len(reply_lines) > 3:
            reply_lines = reply_lines[:2] + ["…"]
        rname_w = _measure_text_width(_clean_text(reply_username), font_reply_name, emoji_font_small, dd)
        rlines_max_w = max([_measure_text_width(l, font_reply_text_font, emoji_font_small, dd) for l in reply_lines] or [0])
        reply_w = max(rname_w, rlines_max_w) + 20
        reply_sample = dd.textbbox((0, 0), "Ag", font=font_reply_text_font)
        rline_h = (reply_sample[3] - reply_sample[1]) + 4
        reply_h = 22 + len(reply_lines) * rline_h + 8

    # 3. Main body text lines
    body_lines = _wrap_text(text, font_body, max_content_w, dd, emoji_font=emoji_font)
    if len(body_lines) > 12:
        body_lines = body_lines[:11] + ["…"]
    body_sample = dd.textbbox((0, 0), "Ag", font=font_body)
    line_h = (body_sample[3] - body_sample[1]) + 6
    body_max_w = max([_measure_text_width(l, font_body, emoji_font, dd) for l in body_lines] or [0])
    body_h = len(body_lines) * line_h

    # Calculate snug card dimensions
    needed_content_w = max(header_w, reply_w, body_max_w, 140)
    card_w = min(content_x + needed_content_w + PADDING, 512)
    card_w = max(card_w, 230)

    total_content_h = 30 + (reply_h + 8 if has_reply else 0) + body_h
    card_h = max(total_content_h + (PADDING * 2), AVATAR_SIZE + (PADDING * 2), 105)
    card_h = min(card_h, 512)

    img = Image.new("RGBA", (int(card_w), int(card_h)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background card: Sleek dark glassmorphism
    BG_COLOR = (26, 24, 34)
    BORDER_COLOR = (46, 43, 58)
    draw.rounded_rectangle(
        [(0, 0), (card_w - 1, card_h - 1)],
        radius=20,
        fill=BG_COLOR,
        outline=BORDER_COLOR,
        width=2,
    )

    # Draw Avatar top-left
    avatar_x = PADDING
    avatar_y = PADDING
    if avatar_bytes:
        drawn = _draw_circular_avatar(img, avatar_bytes, avatar_x, avatar_y, AVATAR_SIZE)
        if not drawn:
            _draw_avatar_placeholder(draw, avatar_x, avatar_y, AVATAR_SIZE, username, glow=True)
    else:
        _draw_avatar_placeholder(draw, avatar_x, avatar_y, AVATAR_SIZE, username, glow=True)

    # Draw author name (cyan/turquoise accent)
    USER_ACCENT = (42, 212, 183)  # #2AD4B7
    _draw_text_with_emoji(draw, (content_x, avatar_y + 2), username, font_user, emoji_font, fill=USER_ACCENT)

    cur_y = avatar_y + 32

    # Draw Reply Section if present
    if has_reply:
        REPLY_ACCENT = (224, 86, 151)  # #E05697 Pink bar and author
        bar_x = content_x
        bar_h = reply_h - 6
        draw.line([(bar_x, cur_y), (bar_x, cur_y + bar_h)], fill=REPLY_ACCENT, width=3)

        reply_name_clean = _clean_text(reply_username)
        _draw_text_with_emoji(draw, (bar_x + 10, cur_y), reply_name_clean, font_reply_name, emoji_font_small, fill=REPLY_ACCENT)

        ry = cur_y + 22
        for rline in reply_lines:
            _draw_text_with_emoji(draw, (bar_x + 10, ry), rline, font_reply_text_font, emoji_font_small, fill=(235, 238, 245))
            ry += rline_h

        cur_y += reply_h + 4

    # Draw Main Message text
    TEXT_COLOR = (245, 246, 250)
    for bline in body_lines:
        _draw_text_with_emoji(draw, (content_x, cur_y), bline, font_body, emoji_font, fill=TEXT_COLOR)
        cur_y += line_h

    # Ensure one side is exactly 512px with no stretched empty padding
    w, h = img.size
    if w >= h:
        new_w = STICKER_SIZE
        new_h = max(int(h * (STICKER_SIZE / w)), 1)
    else:
        new_h = STICKER_SIZE
        new_w = max(int(w * (STICKER_SIZE / h)), 1)
    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    for q in (95, 85, 75, 60, 45):
        buffer = io.BytesIO()
        img.save(buffer, format="WEBP", quality=q)
        if buffer.tell() <= 512 * 1024:
            return buffer.getvalue()
    return buffer.getvalue()


# ─── Image to Sticker ──────────────────────────────────────────────────────────

def image_to_sticker(image_bytes: bytes) -> bytes:
    """
    Convert an image to a 512px WEBP sticker without any card frame.
    Preserves aspect ratio — no stretching. Ensures output <= 512KB for Telegram.
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    w, h = img.size

    scale = STICKER_SIZE / max(w, h)
    new_w = max(1, round(w * scale))
    new_h = max(1, round(h * scale))

    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    for q in (95, 85, 75, 60, 45):
        buffer = io.BytesIO()
        img.save(buffer, format="WEBP", quality=q)
        if buffer.tell() <= 512 * 1024:
            return buffer.getvalue()

    return buffer.getvalue()


# ─── Video to Video Sticker ───────────────────────────────────────────────────

async def video_to_sticker(
    video_path: str,
    start_sec: float = 0,
    end_sec: float = 3,
) -> Optional[bytes]:
    """
    Convert a video clip to a WEBM video sticker (VP9, no audio, 512px, ≤3s, ≤30fps).
    Returns WEBM bytes or None on failure.
    """
    duration = end_sec - start_sec
    if duration <= 0:
        return None
    if duration > MAX_VIDEO_STICKER_DURATION:
        end_sec = start_sec + MAX_VIDEO_STICKER_DURATION
        duration = MAX_VIDEO_STICKER_DURATION

    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
        output_path = tmp.name

    try:
        scale_expr = f"scale=w={STICKER_SIZE}:h={STICKER_SIZE}:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2,fps={MAX_VIDEO_STICKER_FPS}"
        cmd = [
            "ffmpeg", "-y", "-nostdin",
            "-i", video_path,
        ]
        if start_sec > 0.05:
            cmd.extend(["-ss", str(start_sec)])
        cmd.extend([
            "-t", str(duration),
            "-map", "0:v:0",
            "-vf", scale_expr,
            "-c:v", "libvpx-vp9",
            "-b:v", "200k",
            "-an",  # No audio
            "-pix_fmt", "yuva420p",
            output_path,
        ])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)

        if proc.returncode != 0:
            logger.error(f"ffmpeg video sticker error: {stderr.decode()}")
            return None

        # Check file size
        file_size = os.path.getsize(output_path)
        if file_size > MAX_VIDEO_STICKER_SIZE_KB * 1024:
            # Re-encode with lower bitrate
            output_path_2 = output_path + "_small.webm"
            cmd2 = [
                "ffmpeg", "-y", "-nostdin",
                "-i", output_path,
                "-map", "0:v:0",
                "-c:v", "libvpx-vp9",
                "-b:v", "100k",
                "-an",
                "-pix_fmt", "yuva420p",
                output_path_2,
            ]
            proc2 = await asyncio.create_subprocess_exec(
                *cmd2,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc2.communicate(), timeout=60)
            if proc2.returncode == 0 and os.path.getsize(output_path_2) <= MAX_VIDEO_STICKER_SIZE_KB * 1024:
                os.replace(output_path_2, output_path)
            else:
                # Clean up and return None if still too large
                try:
                    os.unlink(output_path_2)
                except OSError:
                    pass
                logger.warning("Video sticker still exceeds 256KB after re-encoding")
                return None

        with open(output_path, "rb") as f:
            return f.read()
    except asyncio.TimeoutError:
        logger.error("ffmpeg video sticker conversion timed out")
        return None
    except Exception as e:
        logger.error(f"Video sticker conversion error: {e}")
        return None
    finally:
        try:
            os.unlink(output_path)
        except OSError:
            pass


# ─── Video to GIF ──────────────────────────────────────────────────────────────

def probe_has_audio(video_path: str) -> bool:
    """Quick check if input media contains an audio stream."""
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_type",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path,
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5.0)
        return res.returncode == 0 and b"audio" in res.stdout
    except Exception as e:
        logger.debug(f"probe_has_audio exception: {e}")
        return False


async def convert_video_to_animation(
    video_path: str,
    output_path: str,
    start_sec: float = 0.0,
    duration: float = 10.0,
    max_dim: int = 480,
) -> Tuple[bool, Optional[str]]:
    """
    Convert video to a fast, lightweight soundless H.264 animation for Telegram's reply_animation.
    Telegram natively renders silent H.264 MP4 videos as looping GIFs (with the GIF badge).
    Ultra-fast (<0.5s), tiny file size, 100% reliable across all devices and aspect ratios.
    Returns (success: bool, error_message: Optional[str]).
    """
    start_sec = max(0.0, float(start_sec))
    duration = min(float(duration), 15.0)

    loop = asyncio.get_running_loop()

    def _run_cmd(c):
        try:
            res = subprocess.run(c, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=25.0)
            stderr_text = res.stderr.decode(errors="ignore").strip()
            if res.returncode != 0:
                logger.error(f"FFmpeg animation failed (rc={res.returncode}):\nCmd: {' '.join(c)}\nStderr: {stderr_text}")
                return False, stderr_text
            return True, None
        except subprocess.TimeoutExpired:
            err = "FFmpeg conversion timed out after 25s"
            logger.error(err)
            return False, err
        except Exception as e:
            err = f"Execution error: {e}"
            logger.error(err, exc_info=True)
            return False, err

    # Extra network protocol flags only if reading from a remote/HLS playlist URL
    extra_input_flags = []
    if video_path.lower().startswith(("http://", "https://")) or ".m3u8" in video_path.lower():
        extra_input_flags.extend(["-protocol_whitelist", "file,http,https,tcp,tls,crypto,hls,applehttp"])
        if ".m3u8" in video_path.lower():
            extra_input_flags.extend(["-allowed_extensions", "ALL"])

    # 1. Primary: Fast H.264 silent MP4 with input seeking (-ss before -i)
    # -map 0:V:0? selects the primary moving video stream, ignoring cover/thumbnail images
    scale_expr = f"scale='min({max_dim},iw)':-2"
    cmd_mp4 = ["ffmpeg", "-y", "-nostdin"]
    if extra_input_flags:
        cmd_mp4.extend(extra_input_flags)
    if start_sec > 0.05:
        cmd_mp4.extend(["-ss", str(start_sec)])
    cmd_mp4.extend([
        "-i", video_path,
        "-t", str(duration),
        "-map", "0:V:0?",
        "-an",
        "-vf", scale_expr,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "26",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        output_path,
    ])

    ok, err_detail = await loop.run_in_executor(None, lambda: _run_cmd(cmd_mp4))
    if ok and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        return True, None

    # 2. Fallback: Direct GIF conversion if H.264 MP4 is unavailable
    logger.warning(f"H.264 animation failed, attempting GIF fallback. Primary error: {err_detail}")
    gif_path = os.path.splitext(output_path)[0] + ".gif"
    cmd_gif = ["ffmpeg", "-y", "-nostdin"]
    if extra_input_flags:
        cmd_gif.extend(extra_input_flags)
    if start_sec > 0.05:
        cmd_gif.extend(["-ss", str(start_sec)])
    cmd_gif.extend([
        "-i", video_path,
        "-t", str(duration),
        "-map", "0:V:0?",
        "-vf", "fps=12,scale='min(360,iw)':-2",
        "-loop", "0",
        gif_path,
    ])
    ok_gif, gif_err = await loop.run_in_executor(None, lambda: _run_cmd(cmd_gif))
    if ok_gif and os.path.exists(gif_path) and os.path.getsize(gif_path) > 0:
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
            os.rename(gif_path, output_path)
            return True, None
        except Exception:
            pass

    return False, err_detail or gif_err or "Unknown FFmpeg encoding failure"


async def extract_audio_from_video(
    video_path: str,
    output_audio_path: str,
    start_sec: float = 0.0,
    duration: float = 10.0,
) -> bool:
    """
    Extract audio track from video clip as MP3.
    Returns True if audio track was extracted, False if video has no audio or on error.
    """
    start_sec = max(0.0, float(start_sec))
    duration = min(float(duration), 15.0)

    cmd = ["ffmpeg", "-y", "-nostdin"]
    if start_sec > 0.05:
        cmd.extend(["-ss", str(start_sec)])
    cmd.extend([
        "-i", video_path,
        "-t", str(duration),
        "-vn",
        "-c:a", "libmp3lame",
        "-q:a", "2",
        output_audio_path,
    ])

    loop = asyncio.get_running_loop()
    def _run():
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=20.0)
        if res.returncode != 0:
            logger.warning(f"Audio extraction failed (rc={res.returncode}): {res.stderr.decode(errors='ignore')[-200:]}")
        return res.returncode == 0 and os.path.exists(output_audio_path) and os.path.getsize(output_audio_path) > 0

    try:
        return await loop.run_in_executor(None, _run)
    except Exception as e:
        logger.warning(f"Audio extraction error: {e}")
        return False


async def video_to_gif(
    video_path: str,
    start_sec: float = 0,
    end_sec: Optional[float] = None,
    max_width: int = 360,
    fps: int = 10,
) -> Optional[bytes]:
    """Compatibility wrapper returning animation bytes."""
    duration = (float(end_sec) - float(start_sec)) if end_sec else 10.0
    out = os.path.join(tempfile.gettempdir(), f"anim_{uuid.uuid4().hex[:10]}.mp4")
    try:
        ok, _ = await convert_video_to_animation(video_path, out, start_sec, duration, max_width)
        if ok and os.path.exists(out):
            with open(out, "rb") as f:
                return f.read()
    finally:
        if os.path.exists(out):
            try:
                os.remove(out)
            except OSError:
                pass
    return None


# ─── Sticker Pack Naming ───────────────────────────────────────────────────────

def get_sticker_pack_name(user_first_name: str, user_id: int, bot_username: str, pack_number: int = 1, is_video: bool = False) -> str:
    """
    Generate a Telegram sticker pack short name.
    Telegram rules:
      - Can contain only English letters, digits, and underscores.
      - Must begin with a letter.
      - Must end in _by_<bot_username>.
      - Length must be 1-64 characters.
      - Must be globally unique across Telegram.
    Includes user_id to ensure complete per-user isolation without conflicts.
    Separates static ('q') and video ('vid') packs because Telegram sets cannot mix formats.
    """
    safe_name = re.sub(r"[^a-zA-Z0-9]", "_", user_first_name or "")[:12].strip("_").lower()
    prefix = "vid" if is_video else "q"
    suffix = f"_by_{bot_username}"

    name_part = f"_{safe_name}" if safe_name else ""
    num_part = f"_{pack_number}" if pack_number > 1 else ""

    base = f"u{user_id}_{prefix}{name_part}{num_part}"
    max_base_len = 64 - len(suffix)
    if len(base) > max_base_len:
        base = base[:max_base_len].rstrip("_")

    return f"{base}{suffix}"


def get_sticker_pack_title(user_first_name: str, pack_number: int = 1, is_video: bool = False) -> str:
    """Generate human-readable sticker pack title."""
    name = (user_first_name or "User").strip()
    tag = "Video Stickers" if is_video else "Sticker Pack"
    if pack_number <= 1:
        return f"{name}'s {tag}"
    else:
        return f"{name}'s {tag} {pack_number}"
