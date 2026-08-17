import io
import math
import re
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

def format_count(num):
    if not num:
        return "0"
    if num >= 1_000_000:
        return f"{num / 1_000_000:.1f}M"
    if num >= 1_000:
        return f"{num / 1_000:.1f}K"
    return str(num)

def format_twitter_date(raw_date_str: str, created_timestamp: int = None) -> str:
    """Format Twitter date into standard readable format: 9:50 PM · Mar 21, 2006"""
    if created_timestamp:
        try:
            dt = datetime.fromtimestamp(created_timestamp)
            time_part = dt.strftime("%I:%M %p").lstrip("0")
            date_part = dt.strftime("%b %d, %Y")
            return f"{time_part} · {date_part}"
        except Exception:
            pass

    if raw_date_str:
        # Try parsing 'Tue Mar 21 20:50:14 +0000 2006'
        try:
            dt = datetime.strptime(raw_date_str, "%a %b %d %H:%M:%S %z %Y")
            time_part = dt.strftime("%I:%M %p").lstrip("0")
            date_part = dt.strftime("%b %d, %Y")
            return f"{time_part} · {date_part}"
        except Exception:
            pass
        return raw_date_str

    return ""

def wrap_text(text, font, max_width, draw):
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

def create_circular_avatar(avatar_img, size):
    avatar_img = avatar_img.resize((size, size), Image.Resampling.LANCZOS).convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    draw_mask = ImageDraw.Draw(mask)
    draw_mask.ellipse((0, 0, size, size), fill=255)
    
    circular = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    circular.paste(avatar_img, (0, 0), mask=mask)
    return circular

def draw_x_logo(draw: ImageDraw.ImageDraw, x: int, y: int, size: int = 20, color=(240, 243, 244)):
    """Draw vector X / Twitter logo reliably without font glyph dependency."""
    # Top-left to bottom-right diagonal thick line
    # Top-right to bottom-left thin line
    draw.line([(x, y), (x + size, y + size)], fill=color, width=3)
    draw.line([(x + size, y), (x, y + size)], fill=color, width=3)

def draw_verified_badge(draw: ImageDraw.ImageDraw, x: int, y: int, radius: int = 9):
    """Draw Twitter blue checkmark badge."""
    ACCENT_BLUE = (29, 155, 240)
    draw.ellipse([(x - radius, y - radius), (x + radius, y + radius)], fill=ACCENT_BLUE)
    # Checkmark polyline
    check_coords = [
        (x - 4, y),
        (x - 1, y + 3),
        (x + 4, y - 3)
    ]
    draw.line(check_coords, fill=(255, 255, 255), width=2)

def generate_tweet_card(tweet_data: dict, avatar_bytes: bytes = None) -> bytes:
    # Card settings (Twitter Dark Mode)
    CARD_WIDTH = 680
    PADDING = 30
    BG_COLOR = (0, 0, 0)             # Twitter Dark #000000
    BORDER_COLOR = (47, 51, 54)      # #2F3336
    TEXT_PRIMARY = (231, 233, 234)   # #E7E9EA
    TEXT_SECONDARY = (113, 118, 123) # #71767B
    
    # Load fonts with system fallbacks (works on Windows, Linux, Docker)
    def load_font(font_names, size, bold=False):
        for name in font_names:
            try:
                return ImageFont.truetype(name, size)
            except Exception:
                continue
        return ImageFont.load_default()

    font_name = load_font(["arialbd.ttf", "DejaVuSans-Bold.ttf", "segoeuib.ttf"], 20, bold=True)
    font_handle = load_font(["arial.ttf", "DejaVuSans.ttf", "segoeui.ttf"], 16)
    font_body = load_font(["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"], 22)
    font_meta = load_font(["arial.ttf", "DejaVuSans.ttf"], 15)
    font_stats = load_font(["arialbd.ttf", "DejaVuSans-Bold.ttf"], 16, bold=True)
    font_stats_label = load_font(["arial.ttf", "DejaVuSans.ttf"], 15)

    # Pre-calculate layout & dynamic height
    dummy_img = Image.new("RGB", (CARD_WIDTH, 500))
    dummy_draw = ImageDraw.Draw(dummy_img)

    tweet_text = tweet_data.get("text", "")
    content_width = CARD_WIDTH - (PADDING * 2)
    lines = wrap_text(tweet_text, font_body, content_width, dummy_draw)

    line_spacing = 8
    sample_bbox = dummy_draw.textbbox((0, 0), "Ag", font=font_body)
    line_height = (sample_bbox[3] - sample_bbox[1]) + line_spacing
    body_height = max(line_height, len(lines) * line_height)

    avatar_size = 52
    header_height = avatar_size

    y_header = PADDING
    y_body = y_header + header_height + 18
    y_date = y_body + body_height + 18
    y_divider1 = y_date + 24
    y_stats = y_divider1 + 14
    y_divider2 = y_stats + 26
    CARD_HEIGHT = y_divider2 + PADDING

    # Create canvas
    img = Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Rounded Dark Card Background
    corner_radius = 20
    draw.rounded_rectangle(
        [(0, 0), (CARD_WIDTH - 1, CARD_HEIGHT - 1)],
        radius=corner_radius,
        fill=BG_COLOR,
        outline=BORDER_COLOR,
        width=2
    )

    # 1. Avatar
    avatar_x = PADDING
    avatar_y = y_header
    if avatar_bytes:
        try:
            raw_avatar = Image.open(io.BytesIO(avatar_bytes))
            circ_avatar = create_circular_avatar(raw_avatar, avatar_size)
            img.paste(circ_avatar, (avatar_x, avatar_y), mask=circ_avatar)
        except Exception:
            avatar_bytes = None

    if not avatar_bytes:
        draw.ellipse(
            [(avatar_x, avatar_y), (avatar_x + avatar_size, avatar_y + avatar_size)],
            fill=(30, 39, 50),
            outline=BORDER_COLOR
        )
        initial = (tweet_data.get("author_name") or "U")[0].upper()
        draw.text((avatar_x + 18, avatar_y + 12), initial, font=font_name, fill=TEXT_PRIMARY)

    # 2. Author Name & Handle
    name_x = avatar_x + avatar_size + 14
    author_name = tweet_data.get("author_name") or "User"
    author_handle = f"@{tweet_data.get('author_screen_name') or 'user'}"

    draw.text((name_x, avatar_y + 2), author_name, font=font_name, fill=TEXT_PRIMARY)
    
    if tweet_data.get("verified"):
        name_bbox = draw.textbbox((name_x, avatar_y + 2), author_name, font=font_name)
        badge_x = name_bbox[2] + 16
        badge_y = avatar_y + 14
        draw_verified_badge(draw, badge_x, badge_y, radius=8)

    draw.text((name_x, avatar_y + 28), author_handle, font=font_handle, fill=TEXT_SECONDARY)

    # 3. Vector 𝕏 Logo in top right
    draw_x_logo(draw, CARD_WIDTH - PADDING - 22, y_header + 4, size=18)

    # 4. Tweet Body Text
    current_y = y_body
    for line in lines:
        draw.text((PADDING, current_y), line, font=font_body, fill=TEXT_PRIMARY)
        current_y += line_height

    # 5. Timestamp
    date_str = format_twitter_date(
        tweet_data.get("created_at"),
        tweet_data.get("created_timestamp")
    )
    draw.text((PADDING, y_date), date_str, font=font_meta, fill=TEXT_SECONDARY)

    # 6. Dividers
    draw.line([(PADDING, y_divider1), (CARD_WIDTH - PADDING, y_divider1)], fill=BORDER_COLOR, width=1)

    # 7. Metrics (Reposts, Likes, Replies)
    retweets = format_count(tweet_data.get("retweets", 0))
    likes = format_count(tweet_data.get("likes", 0))
    replies = format_count(tweet_data.get("replies", 0))

    stat_x = PADDING
    # Reposts
    draw.text((stat_x, y_stats), retweets, font=font_stats, fill=TEXT_PRIMARY)
    stat_bbox = draw.textbbox((stat_x, y_stats), retweets, font=font_stats)
    label_x = stat_bbox[2] + 6
    draw.text((label_x, y_stats + 1), "Reposts", font=font_stats_label, fill=TEXT_SECONDARY)
    
    # Likes
    stat_x = label_x + 75
    draw.text((stat_x, y_stats), likes, font=font_stats, fill=TEXT_PRIMARY)
    stat_bbox = draw.textbbox((stat_x, y_stats), likes, font=font_stats)
    label_x = stat_bbox[2] + 6
    draw.text((label_x, y_stats + 1), "Likes", font=font_stats_label, fill=TEXT_SECONDARY)

    # Replies
    stat_x = label_x + 75
    draw.text((stat_x, y_stats), replies, font=font_stats, fill=TEXT_PRIMARY)
    stat_bbox = draw.textbbox((stat_x, y_stats), replies, font=font_stats)
    label_x = stat_bbox[2] + 6
    draw.text((label_x, y_stats + 1), "Replies", font=font_stats_label, fill=TEXT_SECONDARY)

    # Final RGB Image
    final_img = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0))
    final_img.paste(img, (0, 0), mask=img.split()[3])

    buffer = io.BytesIO()
    final_img.save(buffer, format="PNG", quality=95)
    return buffer.getvalue()
