"""
Translation Engine & Language Utilities
───────────────────────────────────────
Handles language detection, resolution, keyboard UI for language selection,
and multiple translation engines:
- Free engine: deep-translator -> direct Google Translate API -> MyMemory fallback
- AI engine: OpenRouter (Gemini) standard, word-aligned, and OCR vision image translation
"""
from __future__ import annotations

import os
import re
import json
import base64
import asyncio
from typing import Optional, List, Dict, Any, Tuple

import httpx
from deep_translator import GoogleTranslator
from langdetect import detect
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import (
    COMMON_LANGUAGES,
    LANG_FLAGS,
    DEFAULT_ENGINE,
    DEFAULT_TARGET_LANG,
    user_configs,
    ADMIN_USER_IDS,
    has_ai,
    ai_client,
    OPENROUTER_MODEL,
    logger,
    get_flag,
    fmt_card,
)


def resolve_language_code(query: str) -> Optional[str]:
    """
    Resolve an ISO code or language name (case-insensitive) to an ISO language code.
    Supports codes (e.g. 'nl', 'de', 'es'), full names (e.g. 'dutch', 'german'),
    and prefix matches (e.g. 'span', 'amhar').
    """
    if not query:
        return None
    q = query.lower().strip()
    if q in COMMON_LANGUAGES:
        return q
    for code, name in COMMON_LANGUAGES.items():
        if name.lower() == q:
            return code
    if len(q) >= 3:
        for code, name in COMMON_LANGUAGES.items():
            if name.lower().startswith(q):
                return code
    try:
        GoogleTranslator(source="auto", target=q)
        return q
    except Exception:
        pass
    return None


LANGS_PER_PAGE = 8

def build_language_keyboard(current_lang: str, page: int = 0) -> InlineKeyboardMarkup:
    """Build an interactive paginated inline keyboard for choosing target language."""
    items = list(COMMON_LANGUAGES.items())
    total_pages = (len(items) + LANGS_PER_PAGE - 1) // LANGS_PER_PAGE
    page = max(0, min(page, total_pages - 1))

    start_idx = page * LANGS_PER_PAGE
    page_items = items[start_idx:start_idx + LANGS_PER_PAGE]

    keyboard = []
    row = []
    for code, name in page_items:
        flag = get_flag(code)
        is_selected = " ✓" if code == current_lang else ""
        button_text = f"{flag} {name}{is_selected}"
        row.append(InlineKeyboardButton(button_text, callback_data=f"lang:set:{code}:{page}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    # Navigation row
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀ Prev", callback_data=f"lang:page:{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="lang:noop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ▶", callback_data=f"lang:page:{page + 1}"))
    keyboard.append(nav_row)

    # Close button
    keyboard.append([InlineKeyboardButton("❌ Close", callback_data="lang:close")])
    return InlineKeyboardMarkup(keyboard)


def get_user_config(user_id: int) -> dict:
    """Retrieve or initialize configuration for a user."""
    if user_id not in user_configs:
        user_configs[user_id] = {
            "engine": DEFAULT_ENGINE,
            "target": DEFAULT_TARGET_LANG,
        }
    return user_configs[user_id]


def is_user_premium_or_admin(update: Update) -> bool:
    """Check if the user is a Telegram Premium subscriber or admin/whitelist."""
    user = update.effective_user
    if not user:
        return False
    if user.is_premium:
        return True
    if user.id in ADMIN_USER_IDS:
        return True
    return False


async def handle_lang_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle language picker inline button clicks."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = query.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    user = update.effective_user
    if not user:
        return

    config = get_user_config(user.id)

    if action == "close":
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if action == "noop":
        return

    if action == "page":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        current_lang = config["target"]
        lang_name = COMMON_LANGUAGES.get(current_lang, current_lang.upper())
        flag = get_flag(current_lang)
        reply_markup = build_language_keyboard(current_lang, page)
        try:
            await query.edit_message_text(
                fmt_card("🌐 Language Settings",
                    f"  Current Target: {flag} <b>{lang_name}</b> (<code>{current_lang}</code>)\n\n"
                    f"  Tap any language below to set it as your target.\n"
                    f"  Or type <code>/target &lt;name or code&gt;</code> (e.g. <code>/target dutch</code>)"
                ),
                parse_mode="HTML",
                reply_markup=reply_markup
            )
        except Exception:
            pass
        return

    if action == "set":
        new_lang = parts[2] if len(parts) > 2 else "en"
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        config["target"] = new_lang
        lang_name = COMMON_LANGUAGES.get(new_lang, new_lang.upper())
        flag = get_flag(new_lang)
        reply_markup = build_language_keyboard(new_lang, page)
        try:
            await query.edit_message_text(
                fmt_card("🌐 Language Settings",
                    f"  Current Target: {flag} <b>{lang_name}</b> (<code>{new_lang}</code>) ✅\n\n"
                    f"  Tap any language below to switch again.\n"
                    f"  Or type <code>/target &lt;name or code&gt;</code> (e.g. <code>/target dutch</code>)"
                ),
                parse_mode="HTML",
                reply_markup=reply_markup
            )
        except Exception:
            pass
        return


def _detect_by_unicode_script(text: str) -> str:
    """Detect language from dominant Unicode script as a fallback when langdetect fails."""
    script_counts = {}
    for ch in text:
        cp = ord(ch)
        if 0x1200 <= cp <= 0x137F or 0x1380 <= cp <= 0x139F or 0x2D80 <= cp <= 0x2DDF or 0xAB00 <= cp <= 0xAB2F:
            script_counts["am"] = script_counts.get("am", 0) + 1
        elif 0x0600 <= cp <= 0x06FF or 0x0750 <= cp <= 0x077F or 0xFB50 <= cp <= 0xFDFF or 0xFE70 <= cp <= 0xFEFF:
            script_counts["ar"] = script_counts.get("ar", 0) + 1
        elif 0x0900 <= cp <= 0x097F:
            script_counts["hi"] = script_counts.get("hi", 0) + 1
        elif 0x0980 <= cp <= 0x09FF:
            script_counts["bn"] = script_counts.get("bn", 0) + 1
        elif 0x0E00 <= cp <= 0x0E7F:
            script_counts["th"] = script_counts.get("th", 0) + 1
        elif 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
            script_counts["zh"] = script_counts.get("zh", 0) + 1
        elif 0x3040 <= cp <= 0x309F or 0x30A0 <= cp <= 0x30FF:
            script_counts["ja"] = script_counts.get("ja", 0) + 1
        elif 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF or 0x3130 <= cp <= 0x318F:
            script_counts["ko"] = script_counts.get("ko", 0) + 1
        elif 0x0400 <= cp <= 0x04FF:
            script_counts["ru"] = script_counts.get("ru", 0) + 1
        elif 0x0370 <= cp <= 0x03FF:
            script_counts["el"] = script_counts.get("el", 0) + 1
        elif 0x0590 <= cp <= 0x05FF:
            script_counts["he"] = script_counts.get("he", 0) + 1
    if not script_counts:
        return ""
    return max(script_counts, key=script_counts.get)


COMMON_ENGLISH_WORDS = {
    "hi", "hello", "hey", "how", "are", "you", "i", "im", "i'm", "me", "my",
    "the", "is", "it", "it's", "this", "that", "goat", "good", "great",
    "what", "why", "where", "who", "when", "yes", "no", "ok", "okay", "bro",
    "cool", "nice", "yeah", "yep", "nah", "love", "like", "lol", "lmao",
    "thanks", "thank", "please", "can", "could", "will", "would", "do", "did",
    "we", "us", "our", "he", "she", "they", "them", "their", "so", "much",
    "see", "seen", "saw", "go", "going", "went", "come", "came", "here", "there",
    "not", "all", "for", "with", "about", "just", "get", "got", "know", "think",
}


def detect_language_code(text: str) -> str:
    """Detect the language code of the text using Unicode scripts, common word matching, and langdetect."""
    if not text:
        return "auto"

    script_lang = _detect_by_unicode_script(text)
    if script_lang:
        return script_lang

    tokens = [re.sub(r'[^a-zA-Z]', '', w).lower() for w in text.split()]
    tokens = [w for w in tokens if w]
    if tokens:
        english_match_count = sum(1 for w in tokens if w in COMMON_ENGLISH_WORDS)
        if english_match_count >= max(1, (len(tokens) + 1) // 2):
            return "en"

    try:
        lang = detect(text)
        if lang:
            return lang.lower()
    except Exception:
        pass

    return "auto"


async def _translate_google_direct(text: str, target_lang: str) -> str:
    """Translate text by calling Google Translate's free web API directly via httpx."""
    url = "https://translate.googleapis.com/translate_a/single"
    params = {
        "client": "gtx",
        "sl": "auto",
        "tl": target_lang,
        "dt": "t",
        "q": text,
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        translated_parts = []
        if isinstance(data, list) and data and isinstance(data[0], list):
            for part in data[0]:
                if isinstance(part, list) and part:
                    translated_parts.append(str(part[0]))
        result = "".join(translated_parts)
        if not result:
            raise ValueError("Empty translation response from Google API")
        return result


async def _translate_mymemory(text: str, target_lang: str, src_lang: str = "auto") -> str:
    """Translate text using MyMemory free API."""
    if not src_lang or src_lang in ["auto", "??"]:
        src_lang = detect_language_code(text)
    if not src_lang or src_lang in ["auto", "??"]:
        src_lang = "en"

    if src_lang.lower() == target_lang.lower():
        return text

    url = "https://api.mymemory.translated.net/get"
    params = {
        "q": text[:500],
        "langpair": f"{src_lang.lower()}|{target_lang.lower()}",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        status = data.get("responseStatus")
        translated = data.get("responseData", {}).get("translatedText", "")
        if (
            status != 200
            or not translated
            or "INVALID SOURCE LANGUAGE" in translated.upper()
            or "MYMEMORY WARNING" in translated.upper()
            or translated.upper() == text.upper()
        ):
            raise ValueError(f"MyMemory returned invalid response: {translated}")
        return translated


async def translate_free(text: str, target_lang: str, src_lang: str = "auto") -> str:
    """Translate text using deep-translator first, falling back to direct Google API, then MyMemory."""
    loop = asyncio.get_running_loop()
    # 1. deep-translator
    try:
        translated = await loop.run_in_executor(
            None,
            lambda: GoogleTranslator(source="auto", target=target_lang).translate(text)
        )
        if translated:
            return translated
    except Exception as e:
        logger.warning(f"deep-translator failed, trying direct Google API: {e}")

    # 2. direct Google API
    try:
        translated = await _translate_google_direct(text, target_lang)
        return translated
    except Exception as e2:
        logger.warning(f"Direct Google Translate API also failed: {e2}. Trying MyMemory...")

    # 3. MyMemory
    try:
        translated = await _translate_mymemory(text, target_lang, src_lang=src_lang)
        return translated
    except Exception as e3:
        logger.error(f"All translation engines failed. Last error (MyMemory): {e3}")
        raise e3


async def translate_ai(text: str, target_lang: str) -> str:
    """Translate text using OpenRouter AI."""
    if not has_ai or not ai_client:
        raise ValueError("OpenRouter API key is not configured.")

    lang_name = COMMON_LANGUAGES.get(target_lang, target_lang.upper())
    prompt = (
        f"You are a professional translator. Translate the following text into {lang_name} "
        f"(ISO code: '{target_lang}'). Return ONLY the direct translation — no explanations, "
        f"no formatting notes, no introductory text.\n\n"
        f"Text to translate:\n{text}"
    )

    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: ai_client.chat.completions.create(
                model=OPENROUTER_MODEL,
                messages=[
                    {"role": "system", "content": "You are a professional translation engine. Output only the translated text, nothing else."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=4096,
            )
        )
        translation = response.choices[0].message.content.strip()
        if not translation:
            raise ValueError("Empty response received from AI.")
        return translation
    except Exception as e:
        logger.error(f"AI Translation Error: {e}")
        raise e


async def translate_ai_word_aligned(text: str, target_lang: str) -> dict:
    """Translate text and split it into word/phrase pairs in original order."""
    if not has_ai or not ai_client:
        raise ValueError("OpenRouter API key is not configured.")

    lang_name = COMMON_LANGUAGES.get(target_lang, target_lang.upper())
    prompt = (
        f"Translate the following text into {lang_name} (ISO code: '{target_lang}'). "
        f"Also split the source text into its natural words or short phrases, in their "
        f"original order, and give the translation for each one — this shows how word "
        f"order and grammar correspond between the two languages.\n\n"
        f"Respond with ONLY valid JSON, no markdown fences, no explanation, in exactly this shape:\n"
        f'{{"translation": "<full natural translation>", "pairs": [["<source word or phrase>", "<its translation>"], ...]}}\n\n'
        f"Text to translate:\n{text}"
    )

    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: ai_client.chat.completions.create(
                model=OPENROUTER_MODEL,
                messages=[
                    {"role": "system", "content": "You are a professional translation engine that also produces word-level alignments. Output only valid JSON, nothing else."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=1024,
            )
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        data = json.loads(raw)
        translation = (data.get("translation") or "").strip()
        pairs = [
            (str(p[0]), str(p[1])) for p in (data.get("pairs") or [])
            if isinstance(p, (list, tuple)) and len(p) == 2
        ]
        if not translation:
            raise ValueError("Empty translation in AI response.")
        return {"translation": translation, "pairs": pairs}
    except Exception as e:
        logger.error(f"AI Word-Aligned Translation Error: {e}")
        raise e


async def translate_image_ai(image_bytes: bytes, target_lang: str) -> str:
    """Read and translate any text found in an image using OpenRouter AI vision."""
    if not has_ai or not ai_client:
        raise ValueError("OpenRouter API key is not configured.")

    lang_name = COMMON_LANGUAGES.get(target_lang, target_lang.upper())
    b64_image = base64.b64encode(image_bytes).decode("ascii")
    prompt = (
        f"Read all text visible in this image, exactly as laid out (do not reorder "
        f"elements into a standard/memorized sequence such as alphabetical order — "
        f"transcribe them in the order and position they actually appear). Translate "
        f"it into {lang_name} (ISO code: '{target_lang}'). If an element is a single "
        f"isolated letter or character rather than a full word (e.g. an alphabet "
        f"chart or keyboard key), give its phonetic name/sound instead of leaving it "
        f"unchanged, since individual letters have no direct translation. Return ONLY "
        f"the translated text, preserving line breaks between separate text elements, "
        f"with no explanations or notes. If there is no readable text anywhere in the "
        f"image, respond with exactly: NO_TEXT_FOUND"
    )

    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: ai_client.chat.completions.create(
                model=OPENROUTER_MODEL,
                messages=[
                    {"role": "system", "content": "You are an OCR and translation engine. Output only the translated text, nothing else."},
                    {"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"}},
                    ]},
                ],
                temperature=0.1,
                max_tokens=2048,
            )
        )
        result = response.choices[0].message.content.strip()
        if not result:
            raise ValueError("Empty response received from AI.")
        return result
    except Exception as e:
        logger.error(f"AI Image Translation Error: {e}")
        raise e
