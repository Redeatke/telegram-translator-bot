"""
Amharic Voice AI Agent & Audio Processing Pipeline
──────────────────────────────────────────────────
Phase 4: Speech-to-Text (STT), Amharic AI reasoning, and Text-to-Speech (TTS)
voice message delivery for Telegram.
"""
from __future__ import annotations

import os
import re
import html
import base64
import asyncio
import tempfile
import logging
from typing import Optional, Tuple

import httpx
from gtts import gTTS
from telegram import Update
from telegram.ext import ContextTypes

from config import (
    OPENROUTER_API_KEY,
    OPENROUTER_MODEL,
    ADMIN_USER_IDS,
    is_maintenance_active_for_user,
    MAINTENANCE_NOTICE,
    logger,
    fmt_card,
    fmt_success,
    fmt_error,
    fmt_warning,
)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

VOICE_SYSTEM_PROMPT = (
    "You are a friendly, polite, and culturally intelligent Amharic AI assistant named 'አጋዥ' (Agazh). "
    "Respond naturally, concisely, and helpfully in standard Amharic (using proper Ge'ez script). "
    "Since your answer will be spoken out loud via text-to-speech, keep it conversational, direct, and under 3-5 sentences. "
    "Avoid asterisks, hashtags, URLs, or long bulleted lists that sound awkward when read aloud."
)


# ─── Audio File Conversion Utilities ─────────────────────────────────────────

async def convert_ogg_to_wav(ogg_path: str, wav_path: str) -> bool:
    """Convert Telegram voice note (.oga/.ogg) to 16kHz mono WAV for STT processing."""
    cmd = [
        "ffmpeg", "-y",
        "-i", ogg_path,
        "-ar", "16000",
        "-ac", "1",
        "-c:a", "pcm_s16le",
        wav_path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            logger.error(f"ffmpeg ogg->wav conversion failed: {stderr.decode(errors='ignore')}")
            return False
        return os.path.exists(wav_path) and os.path.getsize(wav_path) > 0
    except Exception as e:
        logger.error(f"Error converting ogg to wav: {e}")
        return False


async def convert_mp3_to_voice_ogg(mp3_path: str, ogg_path: str) -> bool:
    """Convert generated MP3 speech to Telegram voice note standard (OGG Opus)."""
    cmd = [
        "ffmpeg", "-y",
        "-i", mp3_path,
        "-c:a", "libopus",
        "-b:a", "32k",
        "-vbr", "on",
        ogg_path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            logger.error(f"ffmpeg mp3->ogg conversion failed: {stderr.decode(errors='ignore')}")
            return False
        return os.path.exists(ogg_path) and os.path.getsize(ogg_path) > 0
    except Exception as e:
        logger.error(f"Error converting mp3 to ogg: {e}")
        return False


# ─── Speech-to-Text (STT) ─────────────────────────────────────────────────────

async def transcribe_audio_whisper_api(wav_path: str) -> Optional[str]:
    """Transcribe audio using OpenAI or Groq Whisper API if configured."""
    api_key = GROQ_API_KEY or OPENAI_API_KEY
    if not api_key:
        return None

    url = (
        "https://api.groq.com/openai/v1/audio/transcriptions"
        if GROQ_API_KEY
        else "https://api.openai.com/v1/audio/transcriptions"
    )
    model = "whisper-large-v3" if GROQ_API_KEY else "whisper-1"

    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            with open(wav_path, "rb") as f:
                files = {"file": ("audio.wav", f, "audio/wav")}
                data = {
                    "model": model,
                    "language": "am",
                    "response_format": "json",
                }
                resp = await client.post(url, headers=headers, files=files, data=data)
                if resp.status_code == 200:
                    result = resp.json()
                    return result.get("text", "").strip()
                logger.warning(f"Whisper API error ({resp.status_code}): {resp.text}")
    except Exception as e:
        logger.warning(f"Whisper API transcription exception: {e}")
    return None


async def transcribe_audio_gemini(wav_path: str) -> Optional[str]:
    """Transcribe audio using OpenRouter Gemini 2.5 Flash native audio understanding."""
    if not OPENROUTER_API_KEY:
        return None

    try:
        with open(wav_path, "rb") as f:
            audio_base64 = base64.b64encode(f.read()).decode("utf-8")

        payload = {
            "model": OPENROUTER_MODEL or "google/gemini-2.5-flash",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Listen to this audio and transcribe exactly what is said. "
                                "If the audio is in Amharic, transcribe it into Amharic Ge'ez script. "
                                "If in English, transcribe in English. "
                                "Output ONLY the transcript text with no explanations or metadata."
                            ),
                        },
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": audio_base64,
                                "format": "wav",
                            },
                        },
                    ],
                }
            ],
            "temperature": 0.1,
            "max_tokens": 1000,
        }

        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "HTTP-Referer": "https://telegram.org",
            "X-Title": "Telegram Translator Voice Bot",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                json=payload,
                headers=headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                return content.strip() if content else None
            logger.warning(f"OpenRouter audio transcription failed ({resp.status_code}): {resp.text}")
    except Exception as e:
        logger.error(f"Gemini audio transcription error: {e}")
    return None


async def transcribe_amharic_speech(wav_path: str) -> Optional[str]:
    """High-level STT dispatcher: tries Whisper API first, falls back to Gemini Flash."""
    transcript = await transcribe_audio_whisper_api(wav_path)
    if not transcript:
        transcript = await transcribe_audio_gemini(wav_path)
    return transcript.strip() if transcript else None


# ─── Amharic AI Reasoning ────────────────────────────────────────────────────

async def generate_amharic_response(user_query: str) -> str:
    """Generate intelligent Amharic response using OpenRouter LLM."""
    if not OPENROUTER_API_KEY:
        return "ይቅርታ፣ የ AI አገልግሎት በአሁኑ ሰዓት አልተገናኘም። እባክዎ ትንሽ ቆይተው እንደገና ይሞክሩ።"

    payload = {
        "model": OPENROUTER_MODEL or "google/gemini-2.5-flash",
        "messages": [
            {"role": "system", "content": VOICE_SYSTEM_PROMPT},
            {"role": "user", "content": user_query},
        ],
        "temperature": 0.6,
        "max_tokens": 500,
    }

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": "https://telegram.org",
        "X-Title": "Telegram Translator Voice Bot",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                json=payload,
                headers=headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                answer = data["choices"][0]["message"]["content"]
                return answer.strip() if answer else "ይቅርታ፣ ጥያቄዎን መመለስ አልቻልኩም።"
            logger.error(f"OpenRouter LLM error ({resp.status_code}): {resp.text}")
    except Exception as e:
        logger.error(f"Error querying OpenRouter LLM: {e}")

    return "ይቅርታ፣ በግንኙነት ችግር ምክንያት ምላሽ መስጠት አልተቻለም።"


# ─── Text-to-Speech (TTS) ────────────────────────────────────────────────────

def _clean_text_for_speech(text: str) -> str:
    """Clean markdown and symbols to make speech sound natural."""
    clean = re.sub(r'[*_`#~\[\]()>]', '', text)
    clean = re.sub(r'https?://\S+', '', clean)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean


async def synthesize_amharic_voice(text: str) -> Optional[bytes]:
    """Synthesize Amharic speech using gTTS and encode into Telegram OGG Opus format."""
    clean_text = _clean_text_for_speech(text)
    if not clean_text:
        return None

    loop = asyncio.get_running_loop()

    with tempfile.TemporaryDirectory() as tmp_dir:
        mp3_path = os.path.join(tmp_dir, "speech.mp3")
        ogg_path = os.path.join(tmp_dir, "speech.ogg")

        def _run_gtts():
            tts = gTTS(text=clean_text, lang="am", slow=False)
            tts.save(mp3_path)

        try:
            await loop.run_in_executor(None, _run_gtts)
        except Exception as e:
            logger.error(f"gTTS speech synthesis error: {e}")
            return None

        if not os.path.exists(mp3_path):
            return None

        # Convert to OGG Opus for native Telegram voice playback
        converted = await convert_mp3_to_voice_ogg(mp3_path, ogg_path)
        if converted and os.path.exists(ogg_path):
            with open(ogg_path, "rb") as f:
                return f.read()

    return None


# ─── Telegram Voice Handler & Command ─────────────────────────────────────────

async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming Telegram voice notes (Milestone 1).
    
    1. Downloads incoming voice note.
    2. Converts to WAV audio.
    3. Transcribes spoken Amharic (STT).
    4. Generates AI response.
    5. Converts answer to Amharic voice note (TTS) and replies.
    """
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not msg or not user or not chat or not msg.voice:
        return

    # In group chats, only respond if voice note replies to the bot or mentions the bot
    is_private = chat.type == "private"
    is_reply_to_bot = (
        msg.reply_to_message
        and msg.reply_to_message.from_user
        and msg.reply_to_message.from_user.id == context.bot.id
    )

    if not is_private and not is_reply_to_bot:
        return

    if is_maintenance_active_for_user(user.id):
        await msg.reply_text(MAINTENANCE_NOTICE, parse_mode="HTML")
        return

    # Send recording audio action
    await context.bot.send_chat_action(chat_id=chat.id, action="record_voice")

    with tempfile.TemporaryDirectory() as tmp_dir:
        input_ogg = os.path.join(tmp_dir, "input.oga")
        input_wav = os.path.join(tmp_dir, "input.wav")

        # 1. Download voice note
        try:
            tg_file = await context.bot.get_file(msg.voice.file_id)
            await tg_file.download_to_drive(input_ogg)
        except Exception as e:
            logger.error(f"Failed to download voice note: {e}")
            await msg.reply_text(
                fmt_error(f"ድምፁን ማውረድ አልተቻለም (Failed to download voice): {str(e)[:100]}"),
                parse_mode="HTML"
            )
            return

        # 2. Convert to WAV
        converted = await convert_ogg_to_wav(input_ogg, input_wav)
        if not converted:
            await msg.reply_text(
                fmt_error("የድምፅ ፋይሉን መለወጥ አልተቻለም (Audio conversion failed)."),
                parse_mode="HTML"
            )
            return

        # 3. Transcribe audio (STT)
        await context.bot.send_chat_action(chat_id=chat.id, action="typing")
        transcript = await transcribe_amharic_speech(input_wav)

        if not transcript:
            await msg.reply_text(
                "🎙️ <b>ድምጽዎ አልተሰማኝም።</b>\n\n"
                "<i>እባክዎ ወደ ማይክሮፎኑ ቀርበው በግልፅ እንደገና ይናገሩ።</i>\n"
                "(Could not detect clear speech. Please try speaking closer to your microphone.)",
                parse_mode="HTML"
            )
            return

        # 4. Generate AI response
        ai_reply = await generate_amharic_response(transcript)

        # 5. Synthesize Amharic speech (TTS)
        await context.bot.send_chat_action(chat_id=chat.id, action="record_voice")
        voice_bytes = await synthesize_amharic_voice(ai_reply)

        caption_text = (
            f"🎙️ <b>የተናገሩት (You said):</b>\n<i>{html.escape(transcript)}</i>\n\n"
            f"💡 <b>ምላሽ (Response):</b>\n{html.escape(ai_reply)}"
        )
        if len(caption_text) > 1024:
            caption_text = caption_text[:1020] + "…"

        # 6. Delivery: Send Voice Note (or text fallback if TTS fails)
        if voice_bytes:
            try:
                await msg.reply_voice(
                    voice=voice_bytes,
                    caption=caption_text,
                    parse_mode="HTML",
                    reply_to_message_id=msg.message_id,
                )
                return
            except Exception as e:
                logger.warning(f"reply_voice failed ({e}), falling back to text reply.")

        # Fallback to text reply
        await msg.reply_text(
            caption_text,
            parse_mode="HTML",
            reply_to_message_id=msg.message_id,
        )


async def voice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Informational command explaining the Amharic Voice Agent."""
    msg = update.effective_message
    if not msg:
        return

    body = (
        "  🎙️ <b>የአማርኛ ድምፅ ረዳት (Amharic Voice Assistant)</b>\n\n"
        "  • <b>እንዴት እንደሚሰራ (How it works):</b>\n"
        "    በማንኛውም ጊዜ የድምፅ መልዕክት (Voice Note) ወደዚህ ቦት ይላኩ!\n"
        "    ቦቱ ንግግርዎን በማድመጥ በአማርኛ ድምፅ እና በፅሁፍ ይመልሳል!\n\n"
        "  • <b>ባህሪያት (Features):</b>\n"
        "    ✓ ፈጣን የንግግር መለያ (Speech-to-Text)\n"
        "    ✓ ብልህ የአማርኛ AI አዕምሮ (Gemini 2.5 Flash)\n"
        "    ✓ ተፈጥሯዊ የአማርኛ ድምፅ መልስ (Amharic TTS)\n\n"
        "  <i>አሁኑኑ የድምፅ መልዕክት ልከው ይሞክሩት! 🎤</i>"
    )

    await msg.reply_text(
        fmt_card("🎙️ Amharic Voice AI", body),
        parse_mode="HTML"
    )
