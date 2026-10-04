"""
Twitter / X Media & Card Handler
────────────────────────────────
Extracts Twitter/X posts via FxTwitter / VxTwitter APIs and yt-dlp.
Handles auto-playing videos, photos (single and media groups with captions),
and sleek dark cards for text-only tweets.
"""
from __future__ import annotations

import os
import re
import html
import asyncio
import tempfile
import logging
from typing import Optional, List, Dict, Any

import httpx
import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import ContextTypes

import card
from config import (
    TWITTER_URL_PATTERN,
    should_skip_message,
    is_downloader_enabled,
    is_maintenance_active_for_user,
    MAINTENANCE_NOTICE,
    media_cache,
    logger,
)


async def handle_twitter_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Detect and handle Twitter/X links."""
    if not update.message or not update.message.text:
        return

    user = update.effective_user
    if user and is_maintenance_active_for_user(user.id):
        await update.message.reply_text(MAINTENANCE_NOTICE, parse_mode="HTML")
        return

    text = update.message.text.strip()
    match = TWITTER_URL_PATTERN.search(text)
    if not match:
        return

    if should_skip_message(update.message):
        return

    chat_id = update.effective_chat.id
    if not is_downloader_enabled(chat_id, "twitter"):
        return

    username, tweet_id = match.groups()
    logger.info(f"Twitter/X link detected: @{username} status {tweet_id}")

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_video")
    except Exception:
        pass

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    tweet_data = None

    # 1. Query FxTwitter API
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"https://api.fxtwitter.com/{username}/status/{tweet_id}", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                raw_tweet = data.get("tweet")
                if raw_tweet:
                    author = raw_tweet.get("author") or {}
                    verification = author.get("verification") or {}
                    quote = raw_tweet.get("quote") or {}
                    media = raw_tweet.get("media") or {}
                    quote_media = quote.get("media") or {}

                    videos = (media.get("videos") or []) + (quote_media.get("videos") or [])
                    photos = (media.get("photos") or []) + (quote_media.get("photos") or [])

                    tweet_data = {
                        "author_name": author.get("name") or username,
                        "author_screen_name": author.get("screen_name") or username,
                        "author_avatar_url": author.get("avatar_url"),
                        "verified": verification.get("verified", False),
                        "text": raw_tweet.get("text", ""),
                        "created_at": raw_tweet.get("created_at"),
                        "created_timestamp": raw_tweet.get("created_timestamp"),
                        "retweets": raw_tweet.get("retweets", 0),
                        "likes": raw_tweet.get("likes", 0),
                        "replies": raw_tweet.get("replies", 0),
                        "views": raw_tweet.get("views"),
                        "url": raw_tweet.get("url") or f"https://x.com/{username}/status/{tweet_id}",
                        "quote": quote,
                        "videos": videos,
                        "photos": photos,
                    }
    except Exception as e:
        logger.warning(f"FxTwitter check failed for @{username}/{tweet_id}: {e}")

    # 2. Fallback to VxTwitter if FxTwitter did not resolve
    if not tweet_data:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"https://api.vxtwitter.com/{username}/status/{tweet_id}", headers=headers)
                if resp.status_code == 200:
                    raw_tweet = resp.json()
                    media_urls = raw_tweet.get("mediaURLs") or []
                    videos = []
                    photos = []
                    for m_url in media_urls:
                        if any(ext in m_url.lower() for ext in [".mp4", ".mov", ".m3u8", "video"]):
                            videos.append({"url": m_url})
                        else:
                            photos.append({"url": m_url})

                    tweet_data = {
                        "author_name": raw_tweet.get("user_name") or username,
                        "author_screen_name": raw_tweet.get("user_screen_name") or username,
                        "author_avatar_url": raw_tweet.get("user_profile_image_url"),
                        "verified": False,
                        "text": raw_tweet.get("text", ""),
                        "created_at": raw_tweet.get("date"),
                        "created_timestamp": raw_tweet.get("date_epoch"),
                        "retweets": raw_tweet.get("retweets", 0),
                        "likes": raw_tweet.get("likes", 0),
                        "replies": raw_tweet.get("replies", 0),
                        "views": None,
                        "url": raw_tweet.get("tweetURL") or f"https://x.com/{username}/status/{tweet_id}",
                        "quote": None,
                        "videos": videos,
                        "photos": photos,
                    }
        except Exception as e:
            logger.warning(f"VxTwitter fallback check failed for @{username}/{tweet_id}: {e}")

    if not tweet_data:
        logger.info(f"Ignoring Twitter link (failed to fetch metadata for @{username}/{tweet_id})")
        return

    author_name = tweet_data.get("author_name") or username
    screen_name = tweet_data.get("author_screen_name") or username
    main_text = tweet_data.get("text", "").strip()
    quote = tweet_data.get("quote")
    likes = tweet_data.get("likes", 0)
    retweets = tweet_data.get("retweets", 0)
    views = tweet_data.get("views")

    caption_parts = [f"𝕏 <b>{html.escape(author_name)}</b> (<code>@{html.escape(screen_name)}</code>)\n"]
    if main_text:
        caption_parts.append(html.escape(main_text))

    if quote:
        q_author = quote.get("author", {}).get("name") or "Quoted"
        q_screen = quote.get("author", {}).get("screen_name") or ""
        q_text = (quote.get("text") or "").strip()
        if q_text:
            caption_parts.append(
                f"\n💬 <b>Quoting {html.escape(q_author)} (@{html.escape(q_screen)}):</b>\n<i>{html.escape(q_text[:300])}</i>"
            )

    stats = []
    if likes:
        stats.append(f"❤️ {card.format_count(likes)}")
    if retweets:
        stats.append(f"🔁 {card.format_count(retweets)}")
    if views:
        stats.append(f"👁️ {card.format_count(views)}")

    if stats:
        caption_parts.append(f"\n{'  •  '.join(stats)}")

    caption = "\n".join(caption_parts)
    if len(caption) > 1024:
        caption = caption[:1020] + "…"

    tweet_url = tweet_data.get("url") or f"https://x.com/{username}/status/{tweet_id}"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("↗️ View on 𝕏", url=tweet_url)]])

    videos = tweet_data.get("videos") or []
    photos = tweet_data.get("photos") or []

    # ─── 1. If Video is present: send Video only (with text caption & button) ─────
    if videos:
        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_video")
        except Exception:
            pass

        v_obj = videos[0]
        video_url = v_obj.get("url")
        variants = v_obj.get("variants") or v_obj.get("formats") or []
        mp4_variants = [
            v for v in variants
            if v.get("content_type") == "video/mp4" or v.get("container") == "mp4" or ".mp4" in v.get("url", "")
        ]
        if mp4_variants:
            mp4_variants.sort(key=lambda x: x.get("bitrate", 0), reverse=True)
            video_url = mp4_variants[0].get("url") or video_url

        video_bytes = None
        if video_url:
            try:
                async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as v_client:
                    v_resp = await v_client.get(video_url, headers=headers)
                    if v_resp.status_code == 200 and len(v_resp.content) <= 50 * 1024 * 1024:
                        video_bytes = v_resp.content
            except Exception as e:
                logger.warning(f"Direct Twitter video download failed for @{username}/{tweet_id}: {e}")

        if not video_bytes:
            try:
                loop = asyncio.get_running_loop()
                def _yt_dlp_tw():
                    with tempfile.TemporaryDirectory() as tmp_dir:
                        out_template = os.path.join(tmp_dir, "%(id)s.%(ext)s")
                        ydl_opts = {
                            "outtmpl": out_template,
                            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                            "quiet": True,
                            "no_warnings": True,
                        }
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            ydl.extract_info(tweet_url, download=True)
                        for f in os.listdir(tmp_dir):
                            if f.endswith((".mp4", ".mkv", ".webm")):
                                with open(os.path.join(tmp_dir, f), "rb") as vf:
                                    return vf.read()
                        return None

                tw_b = await loop.run_in_executor(None, _yt_dlp_tw)
                if tw_b and len(tw_b) <= 50 * 1024 * 1024:
                    video_bytes = tw_b
            except Exception as e:
                logger.error(f"yt-dlp fallback failed for Twitter video @{username}/{tweet_id}: {e}")

        if video_bytes:
            try:
                await update.message.reply_video(
                    video=video_bytes,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    supports_streaming=True,
                    reply_to_message_id=update.message.message_id
                )
                logger.info(f"Sent auto-playing Twitter video for @{username}/status/{tweet_id}")
                return
            except Exception as e:
                logger.error(f"Failed to send video: {e}")

    # ─── 2. If Photos are present, send Photos with caption ─────────────────────────
    if photos:
        try:
            photo_bytes_list = []
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                for p in photos[:8]:
                    p_url = p.get("url")
                    if p_url:
                        try:
                            p_resp = await client.get(p_url, headers=headers)
                            if p_resp.status_code == 200:
                                photo_bytes_list.append(p_resp.content)
                        except Exception as p_err:
                            logger.warning(f"Failed to download photo {p_url}: {p_err}")

            if photo_bytes_list:
                if len(photo_bytes_list) == 1:
                    await update.message.reply_photo(
                        photo=photo_bytes_list[0],
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                        reply_to_message_id=update.message.message_id
                    )
                    logger.info(f"Sent single Twitter photo for @{username}/status/{tweet_id}")
                    return
                else:
                    # Multi-photo media group: Telegram sets caption on the first media item
                    media_caption = caption
                    if tweet_url and "View on 𝕏" not in media_caption:
                        media_caption += f'\n\n<a href="{html.escape(tweet_url)}">↗️ View on 𝕏</a>'
                    if len(media_caption) > 1024:
                        media_caption = media_caption[:1020] + "…"

                    media_group = [
                        InputMediaPhoto(
                            media=pb,
                            caption=media_caption if i == 0 else None,
                            parse_mode="HTML" if i == 0 else None
                        )
                        for i, pb in enumerate(photo_bytes_list)
                    ]
                    await update.message.reply_media_group(
                        media=media_group,
                        reply_to_message_id=update.message.message_id
                    )
                    logger.info(f"Sent Photo(s) media group ({len(photo_bytes_list)} photos) with caption for @{username}/status/{tweet_id}")
                    return
        except Exception as e:
            logger.error(f"Failed to send photo media group: {e}")

    # ─── 3. Text-Only (or Media Fallback): Send Dark Tweet Card ───────────────────
    card_data = dict(tweet_data)
    card_text = main_text
    if quote:
        q_author = quote.get("author", {}).get("name") or "Quoted"
        q_screen = quote.get("author", {}).get("screen_name") or ""
        q_text = (quote.get("text") or "").strip()
        if q_text:
            if card_text:
                card_text += f"\n\nQuoting {q_author} (@{q_screen}):\n{q_text}"
            else:
                card_text = f"Quoting {q_author} (@{q_screen}):\n{q_text}"
    card_data["text"] = card_text

    card_key = f"twitter:card:{tweet_id}"
    card_png = media_cache.get(card_key)
    if not card_png:
        av_key = f"avatar:{tweet_data.get('author_avatar_url')}"
        avatar_bytes = media_cache.get(av_key) if tweet_data.get("author_avatar_url") else None
        if not avatar_bytes and tweet_data.get("author_avatar_url"):
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    av_resp = await client.get(tweet_data["author_avatar_url"], headers=headers)
                    if av_resp.status_code == 200:
                        avatar_bytes = av_resp.content
                        media_cache.set(av_key, avatar_bytes)
            except Exception as e:
                logger.warning(f"Failed to fetch avatar for @{username}: {e}")

        try:
            loop = asyncio.get_running_loop()
            card_png = await loop.run_in_executor(
                None,
                lambda: card.generate_twitter_card(card_data, avatar_bytes)
            )
            if card_png:
                media_cache.set(card_key, card_png)
        except Exception as e:
            logger.error(f"Failed to render tweet card: {e}")

    if card_png:
        try:
            await update.message.reply_photo(
                photo=card_png,
                reply_markup=keyboard,
                reply_to_message_id=update.message.message_id
            )
            logger.info(f"Sent dark tweet card for @{username}/status/{tweet_id}")
            return
        except Exception as e:
            logger.error(f"Failed to send tweet card photo: {e}")

    # Final Text Fallback
    try:
        await update.message.reply_text(
            caption,
            parse_mode="HTML",
            reply_markup=keyboard,
            reply_to_message_id=update.message.message_id,
            disable_web_page_preview=True
        )
    except Exception:
        pass
