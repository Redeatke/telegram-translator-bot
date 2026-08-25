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

def clean_text_for_card(text: str) -> str:
    """Filter emojis and unsupported symbols that render as boxed missing glyphs in standard PIL fonts."""
    if not text:
        return ""
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # Emoticons
        "\U0001F300-\U0001F5FF"  # Misc Symbols and Pictographs
        "\U0001F680-\U0001F6FF"  # Transport and Map Symbols
        "\U0001F1E0-\U0001F1FF"  # Flags / Regional Indicator Symbols
        "\U0001F900-\U0001F9FF"  # Supplemental Symbols and Pictographs
        "\U0001FA70-\U0001FAFF"  # Symbols and Pictographs Extended-A
        "\u2600-\u27BF"          # Misc Symbols / Dingbats
        "\u200d"                 # Zero width joiner
        "\ufe00-\ufe0f"          # Variation selectors
        "\u200e-\u200f"          # Directional marks
        "]+",
        flags=re.UNICODE
    )
    cleaned = emoji_pattern.sub("", text)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    lines = [line.strip() for line in cleaned.split("\n")]
    return "\n".join(lines).strip()

def load_font(font_names, size):
    if isinstance(font_names, str):
        font_names = [font_names]
    for name in font_names:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()

def generate_tweet_card(tweet_data: dict, avatar_bytes: bytes = None) -> bytes:
    # Card settings (Twitter Dark Mode)
    CARD_WIDTH = 680
    PADDING = 30
    BG_COLOR = (0, 0, 0)             # Twitter Dark #000000
    BORDER_COLOR = (47, 51, 54)      # #2F3336
    TEXT_PRIMARY = (231, 233, 234)   # #E7E9EA
    TEXT_SECONDARY = (113, 118, 123) # #71767B

    font_name = load_font(["arialbd.ttf", "DejaVuSans-Bold.ttf", "segoeuib.ttf"], 20)
    font_handle = load_font(["arial.ttf", "DejaVuSans.ttf", "segoeui.ttf"], 16)
    font_body = load_font(["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"], 22)
    font_meta = load_font(["arial.ttf", "DejaVuSans.ttf"], 15)
    font_stats = load_font(["arialbd.ttf", "DejaVuSans-Bold.ttf"], 16)
    font_stats_label = load_font(["arial.ttf", "DejaVuSans.ttf"], 15)
    font_stats = load_font(["arialbd.ttf", "DejaVuSans-Bold.ttf"], 16, bold=True)
    font_stats_label = load_font(["arial.ttf", "DejaVuSans.ttf"], 15)

    # Pre-calculate layout & dynamic height
    dummy_img = Image.new("RGB", (CARD_WIDTH, 500))
    dummy_draw = ImageDraw.Draw(dummy_img)

    tweet_text = clean_text_for_card(tweet_data.get("text", ""))
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
    author_name = clean_text_for_card(tweet_data.get("author_name") or "User")
    if not author_name:
        author_name = tweet_data.get("author_screen_name") or "User"
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


def generate_reddit_card(reddit_data: dict) -> bytes:
    """Generate a sleek dark card PNG image for a Reddit post."""
    dummy_img = Image.new("RGBA", (1, 1))
    dummy_draw = ImageDraw.Draw(dummy_img)

    subreddit = f"{reddit_data.get('subreddit', 'r/reddit')}"
    if not subreddit.startswith("r/"):
        subreddit = f"r/{subreddit}"
    author = f"u/{reddit_data.get('author', 'user')}"
    title = clean_text_for_card(reddit_data.get("title", ""))
    body = clean_text_for_card(reddit_data.get("body", ""))

    content_text = title
    if body:
        content_text += f"\n\n{body}"

    CARD_WIDTH = 650
    PADDING = 30
    content_width = CARD_WIDTH - (PADDING * 2)

    font_sub = load_font(["arialbd.ttf", "DejaVuSans-Bold.ttf", "segoeuib.ttf"], 20)
    font_author = load_font(["arial.ttf", "DejaVuSans.ttf", "segoeui.ttf"], 15)
    font_body = load_font(["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"], 18)
    font_stats = load_font(["arialbd.ttf", "DejaVuSans-Bold.ttf"], 16)

    lines = wrap_text(content_text, font_body, content_width, dummy_draw)
    if len(lines) > 14:
        lines = lines[:13] + ["... (open link to read full post)"]

    line_spacing = 8
    sample_bbox = dummy_draw.textbbox((0, 0), "Ag", font=font_body)
    line_height = (sample_bbox[3] - sample_bbox[1]) + line_spacing
    body_height = max(line_height, len(lines) * line_height)

    header_height = 45
    y_header = PADDING
    y_body = y_header + header_height + 15
    y_divider = y_body + body_height + 18
    y_stats = y_divider + 14
    CARD_HEIGHT = y_stats + 35

    img = Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Dark Reddit Card Background (Reddit dark mode style #1A1A1B)
    draw.rounded_rectangle(
        [(0, 0), (CARD_WIDTH - 1, CARD_HEIGHT - 1)],
        radius=20,
        fill=(26, 26, 27, 255),
        outline=(52, 53, 54, 255),
        width=2
    )

    # Reddit Orange Circle Logo on Header
    orange_color = (255, 69, 0, 255)
    draw.ellipse([(PADDING, y_header), (PADDING + 36, y_header + 36)], fill=orange_color)
    draw.text((PADDING + 8, y_header + 6), "r/", font=font_sub, fill=(255, 255, 255))

    # Subreddit & Author Header Text
    name_x = PADDING + 48
    draw.text((name_x, y_header + 1), subreddit, font=font_sub, fill=(255, 255, 255))
    draw.text((name_x, y_header + 24), f"Posted by {author}", font=font_author, fill=(129, 131, 132))

    # Body Text
    current_y = y_body
    for line in lines:
        draw.text((PADDING, current_y), line, font=font_body, fill=(215, 218, 220))
        current_y += line_height

    # Divider
    draw.line([(PADDING, y_divider), (CARD_WIDTH - PADDING, y_divider)], fill=(52, 53, 54), width=1)

    # Stats: Upvotes & Comments
    score_str = format_count(reddit_data.get("score", 0))
    comments_str = format_count(reddit_data.get("num_comments", 0))

    # Upvotes
    draw.text((PADDING, y_stats), f"⬆️  {score_str}", font=font_stats, fill=(255, 69, 0))
    score_bbox = draw.textbbox((PADDING, y_stats), f"⬆️  {score_str}", font=font_stats)

    # Comments
    comments_x = score_bbox[2] + 30
    draw.text((comments_x, y_stats), f"💬  {comments_str} Comments", font=font_stats, fill=(129, 131, 132))

    # Final RGB Image
    final_img = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0))
    final_img.paste(img, (0, 0), mask=img.split()[3])

    buffer = io.BytesIO()
    final_img.save(buffer, format="PNG", quality=95)
    return buffer.getvalue()

