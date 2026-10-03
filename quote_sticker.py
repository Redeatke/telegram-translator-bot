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
MAX_GIF_DURATION = 30       # seconds — reasonable limit for GIFs

# ─── Card Design Colors & Fonts ────────────────────────────────────────────────
# Design 1: Glassmorphism (default — user can choose)

CARD_BG_PRIMARY = (26, 27, 46)       # #1a1b2e
CARD_BG_SECONDARY = (13, 14, 26)     # #0d0e1a
CARD_BORDER = (60, 65, 90)           # Subtle border
CARD_TEXT_PRIMARY = (240, 242, 245)   # White text
CARD_TEXT_SECONDARY = (140, 150, 170) # Muted text
CARD_ACCENT_CYAN = (0, 220, 220)     # Cyan glow
CARD_ACCENT_PURPLE = (140, 80, 220)  # Purple accent
CARD_REPLY_BG = (35, 38, 55)         # Reply container background

# ─── Font Loading ──────────────────────────────────────────────────────────────

def _load_font(font_names: list, size: int) -> ImageFont.FreeTypeFont:
    """Try to load a font from a list of candidates, falling back to default."""
    for name in font_names:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


FONT_USERNAME = lambda size=22: _load_font(["arialbd.ttf", "DejaVuSans-Bold.ttf", "segoeuib.ttf"], size)
FONT_BODY = lambda size=20: _load_font(["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"], size)
FONT_META = lambda size=14: _load_font(["arial.ttf", "DejaVuSans.ttf", "segoeui.ttf"], size)
FONT_REPLY_NAME = lambda size=16: _load_font(["arialbd.ttf", "DejaVuSans-Bold.ttf", "segoeuib.ttf"], size)
FONT_REPLY_TEXT = lambda size=15: _load_font(["arial.ttf", "DejaVuSans.ttf", "segoeui.ttf"], size)


# ─── Text Wrapping ─────────────────────────────────────────────────────────────

def _wrap_text(text: str, font, max_width: int, draw: ImageDraw.ImageDraw) -> List[str]:
    """Wrap text to fit within max_width pixels."""
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
            bbox = draw.textbbox((0, 0), test_line, font=font)
            width = bbox[2] - bbox[0]
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


def _draw_avatar_placeholder(draw: ImageDraw.ImageDraw, x: int, y: int, size: int, initial: str, glow: bool = True):
    """Draw a placeholder circular avatar with the user's initial and optional glow ring."""
    cx, cy = x + size // 2, y + size // 2
    radius = size // 2

    if glow:
        # Glow ring (cyan)
        for r_offset in range(4, 0, -1):
            glow_color = (CARD_ACCENT_CYAN[0], CARD_ACCENT_CYAN[1], CARD_ACCENT_CYAN[2])
            draw.ellipse(
                [(cx - radius - r_offset, cy - radius - r_offset),
                 (cx + radius + r_offset, cy + radius + r_offset)],
                outline=glow_color, width=1
            )

    # Main circle
    draw.ellipse([(x, y), (x + size, y + size)], fill=(30, 35, 55), outline=CARD_ACCENT_CYAN, width=2)

    # Initial letter
    font = FONT_USERNAME(size // 2)
    bbox = draw.textbbox((0, 0), initial, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text((cx - tw // 2, cy - th // 2 - 2), initial, font=font, fill=CARD_TEXT_PRIMARY)


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

    Layout (when reply context is present):
      ┌────────────────────────┐
      │  [reply avatar] reply  │  ← reply context on TOP
      │  │ reply message text  │
      │                        │
      │  [avatar] Username     │  ← main quote on BOTTOM
      │  │ Main quoted text    │
      └────────────────────────┘

    Args:
        username: Name of the person being quoted
        text: The quoted text
        avatar_bytes: Optional raw bytes of the quoted user's profile photo
        reply_username: Optional - who the quoted message was replying to
        reply_text: Optional - the text being replied to
        reply_avatar_bytes: Optional raw bytes of the reply user's profile photo
        timestamp: Optional timestamp string

    Returns:
        WEBP image bytes suitable for Telegram sticker upload
    """
    text = _clean_text(text) or "(no text)"
    username = _clean_text(username) or "User"

    PADDING = 24
    AVATAR_SIZE = 44
    REPLY_AVATAR_SIZE = 32
    CARD_WIDTH = STICKER_SIZE

    # ── Measure text to compute dynamic card height ──
    dummy_img = Image.new("RGBA", (1, 1))
    dummy_draw = ImageDraw.Draw(dummy_img)

    font_user = FONT_USERNAME()
    font_body = FONT_BODY()
    font_meta = FONT_META()
    font_reply_name = FONT_REPLY_NAME()
    font_reply_text_font = FONT_REPLY_TEXT()

    content_width = CARD_WIDTH - (PADDING * 2)

    # Measure main body text
    body_lines = _wrap_text(text, font_body, content_width - 16, dummy_draw)
    if len(body_lines) > 12:
        body_lines = body_lines[:11] + ["..."]

    sample_bbox = dummy_draw.textbbox((0, 0), "Ag", font=font_body)
    line_height = (sample_bbox[3] - sample_bbox[1]) + 6

    # Measure reply text (if present)
    reply_lines = []
    reply_line_h = 0
    has_reply = bool(reply_username and reply_text)
    if has_reply:
        reply_text_clean = _clean_text(reply_text)
        reply_lines = _wrap_text(reply_text_clean, font_reply_text_font, content_width - 30, dummy_draw)
        if len(reply_lines) > 3:
            reply_lines = reply_lines[:2] + ["..."]
        reply_sample = dummy_draw.textbbox((0, 0), "Ag", font=font_reply_text_font)
        reply_line_h = (reply_sample[3] - reply_sample[1]) + 4

    # Calculate section heights
    header_h = AVATAR_SIZE + 8
    body_h = len(body_lines) * line_height + 12

    reply_section_h = 0
    if has_reply:
        # Reply header (avatar + name) + reply text + spacing
        reply_header_h = REPLY_AVATAR_SIZE + 4
        reply_body_h = len(reply_lines) * reply_line_h + 8
        reply_section_h = reply_header_h + reply_body_h + 12  # 12px gap before main quote

    total_h = PADDING + reply_section_h + header_h + body_h + PADDING
    CARD_HEIGHT = max(total_h, 200)

    if CARD_HEIGHT > STICKER_SIZE:
        CARD_HEIGHT = STICKER_SIZE

    # ── Create the card ──
    img = Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background with rounded corners — glassmorphism dark
    draw.rounded_rectangle(
        [(0, 0), (CARD_WIDTH - 1, CARD_HEIGHT - 1)],
        radius=20,
        fill=CARD_BG_PRIMARY,
        outline=CARD_BORDER,
        width=2
    )


    y = PADDING

    # ═══════════════════════════════════════════════════════════
    # SECTION 1 (TOP): Reply context — who the quoted user was replying to
    # ═══════════════════════════════════════════════════════════
    if has_reply and reply_lines:
        # Reply container background
        reply_container_top = y
        reply_container_h = REPLY_AVATAR_SIZE + 4 + len(reply_lines) * reply_line_h + 12
        draw.rounded_rectangle(
            [(PADDING - 4, reply_container_top - 2),
             (CARD_WIDTH - PADDING + 4, reply_container_top + reply_container_h)],
            radius=10,
            fill=CARD_REPLY_BG,
        )

        # Reply avatar (smaller)
        reply_avatar_x = PADDING + 6
        reply_avatar_y = y + 2
        reply_name_clean = _clean_text(reply_username)
        if reply_avatar_bytes:
            drawn = _draw_circular_avatar(img, reply_avatar_bytes, reply_avatar_x, reply_avatar_y, REPLY_AVATAR_SIZE)
            if not drawn:
                _draw_avatar_placeholder(draw, reply_avatar_x, reply_avatar_y, REPLY_AVATAR_SIZE,
                                         reply_name_clean[0].upper() if reply_name_clean else "?", glow=False)
        else:
            _draw_avatar_placeholder(draw, reply_avatar_x, reply_avatar_y, REPLY_AVATAR_SIZE,
                                     reply_name_clean[0].upper() if reply_name_clean else "?", glow=False)

        # Reply username
        reply_name_x = reply_avatar_x + REPLY_AVATAR_SIZE + 10
        draw.text((reply_name_x, reply_avatar_y + 4), reply_name_clean,
                  font=font_reply_name, fill=CARD_TEXT_SECONDARY)

        # Reply accent bar
        y += REPLY_AVATAR_SIZE + 6
        draw.line([(PADDING + 12, y), (PADDING + 12, y + len(reply_lines) * reply_line_h)],
                  fill=CARD_ACCENT_CYAN, width=2)

        # Reply text
        for rline in reply_lines:
            draw.text((PADDING + 22, y), rline,
                      font=font_reply_text_font, fill=CARD_TEXT_SECONDARY)
            y += reply_line_h

        y += 12  # Gap between reply section and main quote

    # ═══════════════════════════════════════════════════════════
    # SECTION 2 (BOTTOM): Main quoted message — avatar + username + text
    # ═══════════════════════════════════════════════════════════

    # Avatar + Username header
    avatar_x = PADDING
    avatar_y = y

    if avatar_bytes:
        drawn = _draw_circular_avatar(img, avatar_bytes, avatar_x, avatar_y, AVATAR_SIZE)
        if not drawn:
            _draw_avatar_placeholder(draw, avatar_x, avatar_y, AVATAR_SIZE, username[0].upper())
    else:
        _draw_avatar_placeholder(draw, avatar_x, avatar_y, AVATAR_SIZE, username[0].upper())

    name_x = avatar_x + AVATAR_SIZE + 12
    draw.text((name_x, avatar_y + 2), username, font=font_user, fill=CARD_TEXT_PRIMARY)

    if timestamp:
        draw.text((name_x, avatar_y + 26), timestamp, font=font_meta, fill=CARD_TEXT_SECONDARY)

    y += header_h + 4

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

    # Body text
    text_x = bar_x + 16
    for line in body_lines:
        draw.text((text_x, y), line, font=font_body, fill=CARD_TEXT_PRIMARY)
        y += line_height

    # ── Convert to WEBP for Telegram sticker ──
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
    max_width: int = 480,
) -> Optional[bytes]:
    """
    Convert a video (or portion of it) to an optimized GIF.
    Returns GIF bytes or None on failure.
    """
    duration_args = []
    if end_sec is not None:
        duration = end_sec - start_sec
        if duration <= 0:
            return None
        if duration > MAX_GIF_DURATION:
            duration = MAX_GIF_DURATION
        duration_args = ["-t", str(duration)]

    with tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as tmp:
        output_path = tmp.name

    try:
        # Two-pass GIF: generate palette then apply it for quality
        palette_path = output_path + "_palette.png"

        # Pass 1: Generate palette
        cmd_palette = [
            "ffmpeg", "-y",
            "-ss", str(start_sec),
            "-i", video_path,
            *duration_args,
            "-vf", f"scale={max_width}:-1:flags=lanczos,fps=15,palettegen=stats_mode=diff",
            palette_path,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd_palette,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=60)

        if proc.returncode != 0:
            # Fallback: single-pass GIF
            cmd_simple = [
                "ffmpeg", "-y",
                "-ss", str(start_sec),
                "-i", video_path,
                *duration_args,
                "-vf", f"scale={max_width}:-1:flags=lanczos,fps=15",
                output_path,
            ]
            proc2 = await asyncio.create_subprocess_exec(
                *cmd_simple,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc2.communicate(), timeout=60)
            if proc2.returncode != 0:
                logger.error(f"ffmpeg GIF error: {stderr.decode()}")
                return None
        else:
            # Pass 2: Apply palette
            cmd_gif = [
                "ffmpeg", "-y",
                "-ss", str(start_sec),
                "-i", video_path,
                *duration_args,
                "-i", palette_path,
                "-lavfi", f"scale={max_width}:-1:flags=lanczos,fps=15 [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=5",
                output_path,
            ]
            proc3 = await asyncio.create_subprocess_exec(
                *cmd_gif,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc3.communicate(), timeout=60)
            if proc3.returncode != 0:
                logger.error(f"ffmpeg GIF pass 2 error: {stderr.decode()}")
                return None

        try:
            os.unlink(palette_path)
        except OSError:
            pass

        with open(output_path, "rb") as f:
            return f.read()
    except asyncio.TimeoutError:
        logger.error("ffmpeg GIF conversion timed out")
        return None
    except Exception as e:
        logger.error(f"GIF conversion error: {e}")
        return None
    finally:
        try:
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
