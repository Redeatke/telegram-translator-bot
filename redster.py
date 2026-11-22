"""
"redster ..." keyword triggers
──────────────────────────────
Natural-language alternative to /q and /ask, e.g. replying to a
message with::

    redster quote this
    redster quote and add a background color of yellow
    redster quote and add color red
    redster quote this in yello
    redster quote this with reply in red
    redster hello, how are you?

Pure functions — no Telegram imports — so they are unit-testable
without a bot. Fuzzy matching (difflib) tolerates typos in the wake
word ("redstr"), in "quote" ("qoute") and in colour names ("yello",
"purpl", "blu", "gren").

Design notes / trade-offs:
- Colour fuzzy cutoff is 0.8, stricter than quote (0.75) and wake
  (0.7): "blurple" is 0.77-similar to "purple" and must stay an
  *unknown* colour (friendly error) rather than silently becoming
  purple, while "yello" (0.91), "blu" (0.86), "gren" (0.89) and
  "purpl" (0.91) still resolve.
- Colour words are only looked for AFTER the wake word. A normal
  sentence merely containing "quote" ("I love that quote") returns
  None — no wake word, no trigger.
- Translate-by-keyword ("redster translate this ...") is NOT built
  yet — brick by brick. The parser is extensible: add a new intent
  branch where the quote branch is, the same way.
"""
from __future__ import annotations

import difflib
import re
from typing import Dict, Optional

import quote_sticker

# ─── Tunables ────────────────────────────────────────────────────────────────
WAKE_CUTOFF = 0.7    # "redstr" (0.92), "redstar" (0.86) still match "redster"
QUOTE_CUTOFF = 0.75  # "qoute" is exactly 0.80-similar to "quote"
COLOR_CUTOFF = 0.8   # see module docstring — "blurple" (0.77) must NOT match
REPLY_CUTOFF = 0.75  # "replay" (0.91) still matches "reply"

_WAKE_WORDS = {"redster", "redstr", "redstar", "redsterbot", "r3dster", "r3dster_bot", "red"}
_QUOTE_WORDS = {"quote", "qoute", "quoute", "qote", "quotee"}
_REPLY_WORDS = {"reply", "replay", "replies", "r"}
_FILLERS = {"hey", "please", "yo", "hi", "hello", "ok", "okay"}
_COLOR_KEYWORDS = {"color", "colour", "background", "backround", "bg"}
_PREPOSITIONS = {"in", "with", "on"}
# Words that are never a colour, even if they follow "color"/"in"/"with".
_NON_COLOR_WORDS = {
    "quote", "qoute", "quoute", "qote", "quotee",
    "reply", "replay", "replies", "r",
    "this", "that", "it", "and", "add", "a", "an", "the", "of", "in",
    "with", "on", "to", "is", "for", "me", "my",
    "color", "colour", "background", "backround", "bg",
    "please", "hey", "yo", "hi", "hello",
    "redster", "redstr", "redstar", "redsterbot", "r3dster", "r3dster_bot", "redsterbot",
}
_HEX_RE = re.compile(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b")
_BARE_HEX_RE = re.compile(r"^[0-9a-fA-F]{6}$")


def _clean(token: str) -> str:
    """Lowercase a token and strip surrounding punctuation."""
    return token.strip().strip(".,!?:;\"'()[]{}").lower()


def _fuzzy(word: str, candidates, cutoff: float) -> Optional[str]:
    """Best difflib match for `word` among `candidates`, or None."""
    matches = difflib.get_close_matches(word, list(candidates), n=1, cutoff=cutoff)
    return matches[0] if matches else None


def _norm_username(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def parse_redster_message(text: str, bot_username: str = "R3dSter_Bot") -> Optional[Dict]:
    """Parse a "redster ..." message.

    Returns None when this is NOT a redster invocation. Otherwise a
    dict: {"intent": "quote"|"chat", "color": RGB tuple|None,
    "color_raw": str|None, "unknown_color": str|None,
    "with_reply": bool, "chat_text": str|None, "wake": str}.

    "chat" intent means: wake word present, but no "quote" — the
    remainder is a chat message for the AI (see chat_agent.py).
    """
    if not text or not isinstance(text, str):
        return None
    tokens = text.strip().split()
    if not tokens:
        return None

    idx = 0
    wake = None

    # 1. Leading @mention of the bot counts as the wake word:
    #    "@R3dSter_Bot quote this" — but "@someone_else ..." does not.
    first_clean = _clean(tokens[0])
    if tokens[0].startswith("@"):
        mentioned = _norm_username(first_clean.lstrip("@"))
        bot_norm = _norm_username(bot_username)
        if mentioned == bot_norm or mentioned in {"redster", "r3dster", "redsterbot"} or \
                _fuzzy(mentioned, [bot_norm, "redster"], WAKE_CUTOFF):
            wake = tokens[0]
            idx = 1
            if idx >= len(tokens):
                # Just "@R3dSter_Bot" with nothing after — a chat nudge.
                return {
                    "intent": "chat", "color": None, "color_raw": None,
                    "unknown_color": None, "with_reply": False,
                    "chat_text": "", "wake": wake,
                }
        else:
            return None

    # 2. Leading fillers ("hey redster ...", "please redster ...").
    while idx < len(tokens) and _clean(tokens[idx]) in _FILLERS:
        idx += 1
    if idx >= len(tokens):
        return None

    # 3. Wake word — exact, then fuzzy ("redstr", "redstar", "red").
    if wake is None:
        w = _clean(tokens[idx])
        if w in _WAKE_WORDS:
            wake = tokens[idx]
        else:
            match = _fuzzy(w, ["redster", "r3dster"], WAKE_CUTOFF)
            if not match:
                return None
            wake = tokens[idx]
        idx += 1

    remainder = tokens[idx:]
    remainder_str = " ".join(remainder).strip()

    clean_tokens = [_clean(t) for t in remainder]

    # 4. Intent: any token (fuzzy-)matching "quote" -> quote, else chat.
    is_quote = any(
        t in _QUOTE_WORDS or _fuzzy(t, ["quote"], QUOTE_CUTOFF)
        for t in clean_tokens if t
    )
    if not is_quote:
        return {
            "intent": "chat", "color": None, "color_raw": None,
            "unknown_color": None, "with_reply": False,
            "chat_text": remainder_str, "wake": wake,
        }

    # 5. Quote: reply flag.
    with_reply = any(
        t in _REPLY_WORDS or _fuzzy(t, ["reply"], REPLY_CUTOFF)
        for t in clean_tokens if t
    )

    # 6. Quote: colour — hex first, then bigrams ("sky blue"), then singles.
    color = None
    color_raw = None

    hex_m = _HEX_RE.search(remainder_str)
    if hex_m:
        parsed = quote_sticker.parse_quote_color("#" + hex_m.group(1))
        if parsed:
            color, color_raw = parsed, "#" + hex_m.group(1)

    if color is None:
        for tok in tokens:
            t = _clean(tok)
            if _BARE_HEX_RE.match(t) and any(ch.isdigit() for ch in t):
                parsed = quote_sticker.parse_quote_color(t)
                if parsed:
                    color, color_raw = parsed, t
                    break

    if color is None:
        # Bigrams first, so "sky blue" wins over the "blue" inside it.
        for i in range(len(clean_tokens) - 1):
            a, b = clean_tokens[i], clean_tokens[i + 1]
            if not a or not b or a in _NON_COLOR_WORDS or b in _NON_COLOR_WORDS:
                continue
            joined = a + b
            if joined in quote_sticker.QUOTE_COLORS:
                color = quote_sticker.QUOTE_COLORS[joined]
                color_raw = joined
                break
            match = _fuzzy(joined, quote_sticker.QUOTE_COLORS.keys(), COLOR_CUTOFF)
            if match:
                color = quote_sticker.QUOTE_COLORS[match]
                color_raw = match
                break

    if color is None:
        for t in clean_tokens:
            if not t or t in _NON_COLOR_WORDS:
                continue
            if t in quote_sticker.QUOTE_COLORS:
                color = quote_sticker.QUOTE_COLORS[t]
                color_raw = t
                break
            match = _fuzzy(t, quote_sticker.QUOTE_COLORS.keys(), COLOR_CUTOFF)
            if match:
                color = quote_sticker.QUOTE_COLORS[match]
                color_raw = match
                break

    # 7. Unknown colour: a colour/background keyword (or "in"/"with")
    #    followed by a token that isn't a known colour — e.g. "add color
    #    blurple", "in blurple". Lets the bot give a friendly error
    #    naming the word it didn't understand, instead of silently
    #    quoting with the default colour.
    unknown_color = None
    if color is None:
        for i, t in enumerate(clean_tokens):
            if not t:
                continue
            introduces = (
                t in _COLOR_KEYWORDS
                or t in _PREPOSITIONS
                or _fuzzy(t, ["color", "colour", "background"], QUOTE_CUTOFF)
            )
            if not introduces:
                continue
            j = i + 1
            while j < len(clean_tokens) and clean_tokens[j] in {"of", "is", "to", "a", "the"}:
                j += 1
            if j >= len(clean_tokens):
                continue
            cand = clean_tokens[j]
            if cand and cand not in _NON_COLOR_WORDS and cand not in quote_sticker.QUOTE_COLORS:
                unknown_color = cand
                break

    return {
        "intent": "quote",
        "color": color,
        "color_raw": color_raw,
        "unknown_color": unknown_color,
        "with_reply": with_reply,
        "chat_text": None,
        "wake": wake,
    }
