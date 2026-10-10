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
from typing import Optional, List

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
    Generate a glassmorphism-style quote card as a 512px WEBP sticker.

    Features:
      - Dynamic card width: short text produces compact cards, not wide 512px strips
      - Emoji support: renders emoji characters using NotoEmoji font
      - Vibrant per-user avatar colors for placeholder initials & emoji avatars
      - Reply context shown above the main quote

    Returns:
        WEBP image bytes suitable for Telegram sticker upload
    """
    text = _clean_text(text) or "(no text)"
    username = _clean_text(username) or "User"

    # Layout constants
    PADDING = 20
    AVATAR_SIZE = 40
    REPLY_AVATAR_SIZE = 30
    MAX_CONTENT_W = STICKER_SIZE - (PADDING * 2)

    # Standard crisp fonts
    font_user = FONT_USERNAME(22)
    font_body = FONT_BODY(20)
    font_meta = FONT_META(14)
    font_reply_name = FONT_REPLY_NAME(16)
    font_reply_text_font = FONT_REPLY_TEXT(15)
    emoji_font = FONT_EMOJI(20) if _emoji_font_available else None
    emoji_font_small = FONT_EMOJI(15) if _emoji_font_available else None
    emoji_font_reply_name = FONT_EMOJI(16) if _emoji_font_available else None

    dummy_img = Image.new("RGBA", (1, 1))
    dd = ImageDraw.Draw(dummy_img)

    # Wrap body text at maximum possible width
    body_lines = _wrap_text(text, font_body, MAX_CONTENT_W - 16, dd,
                            emoji_font=emoji_font)
    if len(body_lines) > 12:
        body_lines = body_lines[:11] + ["…"]

    # Line heights
    body_sample = dd.textbbox((0, 0), "Ag", font=font_body)
    line_height = (body_sample[3] - body_sample[1]) + 6

    # Measure widest body line
    max_line_w = 0
    for line in body_lines:
        w = _measure_text_width(line, font_body, emoji_font, dd)
        max_line_w = max(max_line_w, w)

    # Measure username width and timestamp width
    user_w = _measure_text_width(username, font_user, emoji_font, dd)
    time_w = _measure_text_width(timestamp, font_meta, emoji_font, dd) if timestamp else 0

    # Reply section measurement
    reply_lines = []
    reply_line_h = 0
    has_reply = bool(reply_username and reply_text)
    max_reply_line_w = 0
    if has_reply:
        reply_text_clean = _clean_text(reply_text)
        reply_lines = _wrap_text(reply_text_clean, font_reply_text_font,
                                 MAX_CONTENT_W - 30, dd, emoji_font=emoji_font)
        if len(reply_lines) > 3:
            reply_lines = reply_lines[:2] + ["…"]
        reply_sample = dd.textbbox((0, 0), "Ag", font=font_reply_text_font)
        reply_line_h = (reply_sample[3] - reply_sample[1]) + 4
        for rline in reply_lines:
            w = _measure_text_width(rline, font_reply_text_font, emoji_font, dd)
            max_reply_line_w = max(max_reply_line_w, w)

    # ── Calculate dynamic card box width ──
    # Content-based width calculation
    body_needed_w = PADDING + 16 + max_line_w + PADDING
    header_needed_w = PADDING + AVATAR_SIZE + 12 + max(user_w, time_w) + PADDING
    reply_needed_w = 0
    if has_reply:
        reply_name_w = _measure_text_width(
            _clean_text(reply_username), font_reply_name, emoji_font_reply_name, dd
        )
        reply_head_w = PADDING + 6 + REPLY_AVATAR_SIZE + 10 + reply_name_w + PADDING
        reply_body_w = PADDING + 22 + max_reply_line_w + PADDING
        reply_needed_w = max(reply_head_w, reply_body_w)

    raw_w = max(body_needed_w, header_needed_w, reply_needed_w, 240)
    CARD_BOX_WIDTH = min(raw_w, STICKER_SIZE)

    # Height: sum of sections
    header_h = AVATAR_SIZE + 6
    body_h = len(body_lines) * line_height + 4

    reply_section_h = 0
    if has_reply and reply_lines:
        reply_header_h = REPLY_AVATAR_SIZE + 4
        reply_body_h = len(reply_lines) * reply_line_h + 6
        reply_section_h = reply_header_h + reply_body_h + 10

    total_h = PADDING + reply_section_h + header_h + body_h + PADDING
    CARD_HEIGHT = min(max(total_h, 105), STICKER_SIZE)

    # ── Render on a 512px canvas (Telegram sticker requirement: one side = 512px) ──
    # The canvas width is STICKER_SIZE (512), with transparent background outside CARD_BOX_WIDTH
    img = Image.new("RGBA", (STICKER_SIZE, CARD_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background card with rounded corners — glassmorphism dark
    draw.rounded_rectangle(
        [(0, 0), (CARD_BOX_WIDTH - 1, CARD_HEIGHT - 1)],
        radius=18,
        fill=CARD_BG_PRIMARY,
        outline=CARD_BORDER,
        width=2
    )

    y = PADDING

    # ═══════════════════════════════════════════════════════════
    # SECTION 1 (TOP): Reply context
    # ═══════════════════════════════════════════════════════════
    if has_reply and reply_lines:
        reply_container_top = y
        reply_container_h = REPLY_AVATAR_SIZE + 4 + len(reply_lines) * reply_line_h + 12
        draw.rounded_rectangle(
            [(PADDING - 4, reply_container_top - 2),
             (CARD_BOX_WIDTH - PADDING + 4, reply_container_top + reply_container_h)],
            radius=10,
            fill=CARD_REPLY_BG,
        )

        # Reply avatar
        reply_avatar_x = PADDING + 6
        reply_avatar_y = y + 2
        reply_name_clean = _clean_text(reply_username)
        if reply_avatar_bytes:
            drawn = _draw_circular_avatar(img, reply_avatar_bytes, reply_avatar_x, reply_avatar_y, REPLY_AVATAR_SIZE)
            if not drawn:
                _draw_avatar_placeholder(draw, reply_avatar_x, reply_avatar_y, REPLY_AVATAR_SIZE,
                                         reply_name_clean, glow=False)
        else:
            _draw_avatar_placeholder(draw, reply_avatar_x, reply_avatar_y, REPLY_AVATAR_SIZE,
                                     reply_name_clean, glow=False)

        # Reply username
        reply_name_x = reply_avatar_x + REPLY_AVATAR_SIZE + 10
        _draw_text_with_emoji(draw, (reply_name_x, reply_avatar_y + 4),
                              reply_name_clean, font_reply_name, emoji_font_reply_name,
                              fill=CARD_TEXT_SECONDARY)

        # Reply accent bar
        y += REPLY_AVATAR_SIZE + 6
        bar_x_reply = PADDING + 12
        draw.line([(bar_x_reply, y), (bar_x_reply, y + len(reply_lines) * reply_line_h)],
                  fill=CARD_ACCENT_CYAN, width=2)

        # Reply text lines
        reply_text_x = PADDING + 22
        for rline in reply_lines:
            _draw_text_with_emoji(draw, (reply_text_x, y), rline,
                                  font_reply_text_font, emoji_font_small,
                                  fill=CARD_TEXT_SECONDARY)
            y += reply_line_h

        y += 12

    # ═══════════════════════════════════════════════════════════
    # SECTION 2 (BOTTOM): Main quoted message
    # ═══════════════════════════════════════════════════════════

    # Avatar + Username header
    avatar_x = PADDING
    avatar_y = y

    if avatar_bytes:
        drawn = _draw_circular_avatar(img, avatar_bytes, avatar_x, avatar_y, AVATAR_SIZE)
        if not drawn:
            _draw_avatar_placeholder(draw, avatar_x, avatar_y, AVATAR_SIZE, username)
    else:
        _draw_avatar_placeholder(draw, avatar_x, avatar_y, AVATAR_SIZE, username)

    name_x = avatar_x + AVATAR_SIZE + 10
    _draw_text_with_emoji(draw, (name_x, avatar_y + 2),
                          username, font_user, emoji_font, fill=CARD_TEXT_PRIMARY)

    if timestamp:
        draw.text((name_x, avatar_y + 24), timestamp,
                  font=font_meta, fill=CARD_TEXT_SECONDARY)

    y += header_h + 2

    # Accent bar (gradient cyan → purple)
    bar_x = PADDING
    bar_top = y
    bar_bottom = y + len(body_lines) * line_height
    bar_height = bar_bottom - bar_top
    if bar_height > 0:
        for i in range(bar_height):
            ratio = i / max(bar_height - 1, 1)
            r = int(CARD_ACCENT_CYAN[0] * (1 - ratio) + CARD_ACCENT_PURPLE[0] * ratio)
            g = int(CARD_ACCENT_CYAN[1] * (1 - ratio) + CARD_ACCENT_PURPLE[1] * ratio)
            b = int(CARD_ACCENT_CYAN[2] * (1 - ratio) + CARD_ACCENT_PURPLE[2] * ratio)
            draw.line([(bar_x, bar_top + i), (bar_x + 3, bar_top + i)], fill=(r, g, b))

    # Body text lines
    text_x = bar_x + 16
    for line in body_lines:
        _draw_text_with_emoji(draw, (text_x, y), line,
                              font_body, emoji_font, fill=CARD_TEXT_PRIMARY)
        y += line_height

    # ── Ensure one side is exactly 512px (Telegram sticker requirement) ──
    w, h = img.size
    if w != STICKER_SIZE and h != STICKER_SIZE:
        if w >= h:
            new_w = STICKER_SIZE
            new_h = int(h * (STICKER_SIZE / w))
        else:
            new_h = STICKER_SIZE
            new_w = int(w * (STICKER_SIZE / h))
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    buffer = io.BytesIO()
    img.save(buffer, format="WEBP", quality=95)
    return buffer.getvalue()


# ─── Image to Sticker ──────────────────────────────────────────────────────────

def image_to_sticker(image_bytes: bytes) -> bytes:
    """
    Convert an image to a 512px WEBP sticker without any card frame.
    Preserves aspect ratio — no stretching.
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    w, h = img.size

    # Scale so the longest side is 512px, preserving aspect ratio
    if w >= h:
        new_w = STICKER_SIZE
        new_h = int(h * (STICKER_SIZE / w))
    else:
        new_h = STICKER_SIZE
        new_w = int(w * (STICKER_SIZE / h))

    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    buffer = io.BytesIO()
    img.save(buffer, format="WEBP", quality=95)
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
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start_sec),
            "-i", video_path,
            "-t", str(duration),
            "-vf", f"scale='if(gt(iw,ih),{STICKER_SIZE},-2)':'if(gt(iw,ih),-2,{STICKER_SIZE})',fps={MAX_VIDEO_STICKER_FPS}",
            "-c:v", "libvpx-vp9",
            "-b:v", "200k",
            "-an",  # No audio
            "-pix_fmt", "yuva420p",
            output_path,
        ]

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
                "ffmpeg", "-y",
                "-i", output_path,
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

async def video_to_gif(
    video_path: str,
    start_sec: float = 0,
    end_sec: Optional[float] = None,
    max_width: int = 400,
    fps: int = 12,
) -> Optional[bytes]:
    """
    Convert a video (or portion of it) to an optimized GIF.
    Returns GIF bytes or None on failure.
    """
    start_sec = max(0.0, float(start_sec))
    if end_sec is not None:
        duration = float(end_sec) - start_sec
        if duration <= 0:
            return None
        if duration > MAX_GIF_DURATION:
            duration = MAX_GIF_DURATION
    else:
        duration = DEFAULT_GIF_DURATION

    with tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as tmp:
        output_path = tmp.name

    try:
        # Ultra-fast high-quality GIF generation with -nostdin and output -t
        filter_str = (
            f"fps={fps},scale=min({max_width}\\,iw):-2:flags=fast_bilinear,split[s0][s1];"
            f"[s0]palettegen=max_colors=128[p];"
            f"[s1][p]paletteuse=dither=bayer:bayer_scale=3"
        )

        cmd = [
            "ffmpeg", "-y",
            "-nostdin",
            "-ss", str(start_sec),
            "-i", video_path,
            "-t", str(duration),
            "-vf", filter_str,
            output_path,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=20.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            logger.error("ffmpeg GIF conversion timed out after 20s")
            return None

        if proc.returncode != 0:
            err_output = stderr.decode(errors='ignore').strip()
            logger.warning(
                f"ffmpeg palette filter failed: {err_output[:200]}, trying standard fallback"
            )
            # Fallback: standard GIF encoding without palette filter
            cmd_fallback = [
                "ffmpeg", "-y",
                "-nostdin",
                "-ss", str(start_sec),
                "-i", video_path,
                "-t", str(duration),
                "-vf", f"fps={fps},scale=min({max_width}\\,iw):-2",
                output_path,
            ]
            proc_fallback = await asyncio.create_subprocess_exec(
                *cmd_fallback,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr_fallback = await asyncio.wait_for(proc_fallback.communicate(), timeout=15.0)
            except asyncio.TimeoutError:
                try:
                    proc_fallback.kill()
                    await proc_fallback.wait()
                except Exception:
                    pass
                return None

            if proc_fallback.returncode != 0:
                logger.error(f"ffmpeg GIF error: {stderr_fallback.decode(errors='ignore').strip()[:200]}")
                return None

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            logger.error("ffmpeg produced empty GIF output")
            return None

        with open(output_path, "rb") as f:
            return f.read()
    except Exception as e:
        logger.error(f"GIF conversion error: {e}", exc_info=True)
        return None
    finally:
        try:
            if os.path.exists(output_path):
                os.unlink(output_path)
        except OSError:
            pass


# ─── Sticker Pack Naming ───────────────────────────────────────────────────────

def get_sticker_pack_name(user_first_name: str, user_id: int, bot_username: str, pack_number: int = 1) -> str:
    """
    Generate a Telegram sticker pack short name.
    Format: {sanitized_name}_quotes_by_{bot_username}
    For pack 2+: {sanitized_name}_quotes_2_by_{bot_username}
    """
    # Sanitize name: only alphanumeric and underscores, max 20 chars
    safe_name = re.sub(r"[^a-zA-Z0-9]", "_", user_first_name)[:20].strip("_").lower()
    if not safe_name:
        safe_name = f"user_{user_id}"

    if pack_number <= 1:
        return f"{safe_name}_quotes_by_{bot_username}"
    else:
        return f"{safe_name}_quotes_{pack_number}_by_{bot_username}"


def get_sticker_pack_title(user_first_name: str, pack_number: int = 1) -> str:
    """Generate human-readable sticker pack title."""
    name = user_first_name or "User"
    if pack_number <= 1:
        return f"{name}'s Sticker Pack"
    else:
        return f"{name}'s Sticker Pack {pack_number}"
