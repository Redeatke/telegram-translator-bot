"""
Named sticker library
─────────────────────
Red (2026-11-26): send the bot a sticker, say "this is 'angry cat'",
later "redster send 'angry cat' sticker" sends it — with "x3" / "*3" /
"3 times" for repeats, clamped to 5 copies so the bot can't spam.

Pure functions + JSON storage — no Telegram imports — so the parsing
and the library are unit-testable without a bot. The async handlers
that call into this module live in bot.py.

Storage: sticker_library.json in DATA_DIR (same pattern as
user_configs.json / chat_configs.json), per-user libraries keyed by
user id, atomic writes via config._atomic_write_json. A missing or
corrupt file loads as an empty library, never raises.

Routing (Red, 2026-11-26 — explicit keywords so the intents never
clash):
  redster play <music name>         -> music (music_search.py)
  redster send an image of <thing>  -> image (media_search.py)
  redster send "<name>" sticker     -> this library (send action)
A sticker SEND is only claimed when the request contains the word
"sticker" — "send angry cat sticker", "send 'angry cat' sticker",
"send the angry cat sticker", with xN / *N / "N times" counts. A bare
"redster send <name>" is NEVER claimed by the library, even when
<name> exactly matches a saved sticker: it falls through to
music/image/chat exactly as before the library existed, so the broad
music parser claims bare sends as songs and there is no clash. (An
earlier build let an exact saved-name match win over music; that
precedence was removed 2026-11-26 — a sticker saved under a song's
title no longer steals the song, because the song goes through
"redster play ..." or /song anyway.) The unknown-name reply (list +
"did you mean ...?" suggestion, never a guess-send) likewise only
fires when the request has the "sticker" word.
"""
from __future__ import annotations

import difflib
import html
import json
import logging
import os
import re
import threading
from typing import Dict, List, Optional

from config import _atomic_write_json

logger = logging.getLogger(__name__)

# ─── Tunables ────────────────────────────────────────────────────────────────
MAX_COPIES = 5               # "x3" sends 3; anything over 5 sends 5 + a note
MAX_NAME_LENGTH = 40         # sticker names are 1–40 chars after normalizing
MAX_STICKERS_PER_USER = 50   # per-user cap, so the JSON file can't grow forever
SUGGEST_CUTOFF = 0.8         # difflib cutoff for "did you mean ...?"

STICKER_LIBRARY_FILE = os.path.join(
    os.environ.get("DATA_DIR", os.path.dirname(__file__)), "sticker_library.json"
)

# user_id -> {normalized name -> {"file_id", "is_video", "is_animated"}}
_library: Dict[int, Dict[str, dict]] = {}
_library_lock = threading.Lock()

# A name is letters/digits with single spaces or hyphens between words.
_NAME_RE = re.compile(r"^[a-z0-9]+(?:[ \-][a-z0-9]+)*$")


# ─── Names ───────────────────────────────────────────────────────────────────

def normalize_name(raw: str) -> str:
    """Lowercase, collapse whitespace, strip quotes / trailing punctuation."""
    s = (raw or "").strip().lower()
    s = s.strip("\"'“”‘’")
    s = re.sub(r"\s+", " ", s)
    s = s.strip(".,!?:;()[]{}")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def validate_name(raw: str) -> Optional[str]:
    """Normalized name, or None when it is empty, too long, or has other chars."""
    norm = normalize_name(raw)
    if not norm or len(norm) > MAX_NAME_LENGTH:
        return None
    if not _NAME_RE.match(norm):
        return None
    return norm


# ─── Parsing (pure: remainder text after the "redster" wake word) ───────────

def _strip_leading_please(text: str) -> str:
    s = (text or "").strip()
    while s.lower().startswith("please "):
        s = s[len("please "):].strip()
    return s


_SAVE_RES = [
    (re.compile(r"^save\s+this\s+sticker\s+as\s+(.+)$", re.IGNORECASE), "save"),
    (re.compile(r"^save\s+sticker\s+as\s+(.+)$", re.IGNORECASE), "save"),
    (re.compile(r"^save\s+this\s+as\s+(.+)$", re.IGNORECASE), "save"),
    (re.compile(r"^save\s+as\s+(.+)$", re.IGNORECASE), "save"),
    (re.compile(r"^this\s+is\s+(.+)$", re.IGNORECASE), "this_is"),
]


def parse_save_request(remainder: str) -> Optional[dict]:
    """Match a save request. Returns {"kind": "save"|"this_is", "name": raw}
    or None. "this is <name>" is only a save when the user is replying
    to a sticker (see resolve_library_request) — without one it is
    plain chat ("redster this is amazing")."""
    text = _strip_leading_please(remainder)
    for rx, kind in _SAVE_RES:
        m = rx.match(text)
        if m:
            return {"kind": kind, "name": m.group(1).strip()}
    return None


_COUNT_RE = re.compile(
    r"(?:^|\s)(?:x\s*(\d+)|\*\s*(\d+)|(\d+)\s+times?)\s*$", re.IGNORECASE
)


def parse_send_request(remainder: str) -> Optional[dict]:
    """Match "send [me] [the] <name> sticker [xN|*N|N times]". Returns
    {"name": <name>, "count": clamped 1..5, "requested": raw count,
    "sticker_word": True} or None when it isn't a library send.

    The word "sticker" is REQUIRED (Red, 2026-11-26): a bare
    "send <name>" is never a library send — it falls through to
    music/image/chat. The name is the words between "send" (and
    optional "me"/"the") and the "sticker" marker, with surrounding
    quotes stripped by validate_name later. Only ONE trailing marker
    is removed, so a sticker literally named "angry cat sticker" is
    still sendable ("send angry cat sticker sticker"). A leading
    "sticker" is also accepted ("send sticker angry cat"). A bare
    trailing number is part of the name ("send cat 3 sticker" sends
    the sticker named "cat 3"); only xN / *N / "N times" count as a
    repeat request, and a missing/invalid count means 1.
    """
    text = _strip_leading_please(remainder)
    m = re.match(r"^send\s+(.+)$", text, re.IGNORECASE)
    if not m:
        return None
    rest = m.group(1).strip()
    if not rest:
        return None

    requested = 1
    cm = _COUNT_RE.search(rest)
    if cm:
        requested = int(next(g for g in cm.groups() if g is not None))
        rest = rest[:cm.start()].strip()
        if not rest:
            return None
    count = max(1, min(requested, MAX_COPIES))

    tokens = rest.split()
    while tokens and tokens[0].lower() in ("me", "the"):
        tokens.pop(0)
    if not tokens:
        return None
    if tokens[-1].lower() == "sticker":
        tokens = tokens[:-1]
    elif tokens[0].lower() == "sticker":
        tokens = tokens[1:]
    else:
        # No "sticker" word: not a library send — fall through to
        # image/chat (a bare "send <x>" is never a song request
        # either — music is only "redster play" or /song).
        return None
    if not tokens:
        return None
    name = " ".join(tokens)
    return {
        "name": name,
        "count": count,
        "requested": requested,
        "sticker_word": True,
    }


_LIST_PHRASES = {
    "my stickers", "my sticker",
    "list stickers", "list sticker", "list my stickers",
    "show my stickers", "show stickers",
}


def parse_list_request(remainder: str) -> bool:
    """True for "my stickers" / "list stickers" / "show my stickers" etc."""
    clean = re.sub(r"\s+", " ", (remainder or "").strip().lower())
    return clean in _LIST_PHRASES


_DELETE_RE = re.compile(
    r"^(?:forget|delete|remove)\s+(?:the\s+)?(?:sticker\s+)?(.+)$", re.IGNORECASE
)


def parse_delete_request(remainder: str) -> Optional[dict]:
    """Match "forget/delete/remove [the] [sticker] <name>". Returns
    {"name": full, "name_stripped": full minus a trailing "sticker"}
    or None. The resolver tries the full name first, then the stripped
    one, so a sticker literally named "... sticker" can be deleted."""
    text = _strip_leading_please(remainder)
    m = _DELETE_RE.match(text)
    if not m:
        return None
    full = m.group(1).strip()
    if not full:
        return None
    tokens = full.split()
    while tokens and tokens[-1].lower() == "sticker":
        tokens.pop()
    stripped = " ".join(tokens) if tokens else full
    return {"name": full, "name_stripped": stripped}


def _candidates(parsed: dict) -> List[str]:
    """Name candidates in try order: full first, then marker-stripped."""
    out = []
    for key in ("name", "name_stripped"):
        cand = parsed.get(key)
        if cand and cand not in out:
            out.append(cand)
    return out


# ─── Storage ─────────────────────────────────────────────────────────────────

def load_sticker_library() -> None:
    """Load sticker_library.json into memory. Missing or corrupt file
    loads as an empty library — never raises."""
    global _library
    if not os.path.exists(STICKER_LIBRARY_FILE):
        _library = {}
        return
    try:
        with open(STICKER_LIBRARY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        loaded: Dict[int, Dict[str, dict]] = {}
        if isinstance(data, dict):
            for k, v in data.items():
                if not isinstance(v, dict):
                    continue
                entries: Dict[str, dict] = {}
                for name, entry in v.items():
                    if not isinstance(entry, dict) or not isinstance(entry.get("file_id"), str):
                        continue
                    norm = validate_name(name)
                    if not norm:
                        continue
                    entries[norm] = {
                        "file_id": entry["file_id"],
                        "is_video": bool(entry.get("is_video", False)),
                        "is_animated": bool(entry.get("is_animated", False)),
                    }
                loaded[int(k)] = entries
        _library = loaded
        logger.info(f"Loaded sticker libraries for {len(_library)} users.")
    except Exception as e:
        logger.error(f"Failed to load sticker_library.json: {e}")
        _library = {}


def _save_locked() -> None:
    try:
        _atomic_write_json(
            STICKER_LIBRARY_FILE, {str(k): v for k, v in _library.items()}
        )
    except Exception as e:
        logger.error(f"Failed to save sticker_library.json: {e}")


def save_sticker(user_id: int, raw_name: str, file_id: str,
                 is_video: bool = False, is_animated: bool = False):
    """Save (or replace) a sticker under `raw_name` for one user.
    Returns (ok, replaced, error): error is "invalid" or "full"."""
    norm = validate_name(raw_name)
    if norm is None:
        return False, False, "invalid"
    with _library_lock:
        user_lib = _library.setdefault(user_id, {})
        if norm not in user_lib and len(user_lib) >= MAX_STICKERS_PER_USER:
            return False, False, "full"
        replaced = norm in user_lib
        user_lib[norm] = {
            "file_id": file_id,
            "is_video": bool(is_video),
            "is_animated": bool(is_animated),
        }
        _save_locked()
    return True, replaced, None


def get_sticker(user_id: int, raw_name: str) -> Optional[dict]:
    """The saved entry for `raw_name`, or None."""
    norm = validate_name(raw_name)
    if norm is None:
        return None
    return _library.get(user_id, {}).get(norm)


def delete_sticker(user_id: int, raw_name: str) -> bool:
    """Remove a saved sticker. True when something was deleted."""
    norm = validate_name(raw_name)
    if norm is None:
        return False
    with _library_lock:
        user_lib = _library.get(user_id, {})
        if norm not in user_lib:
            return False
        del user_lib[norm]
        _save_locked()
    return True


def list_names(user_id: int) -> List[str]:
    """Saved sticker names for one user, in saved order."""
    return list(_library.get(user_id, {}).keys())


def suggest_name(user_id: int, raw_name: str) -> Optional[str]:
    """One close saved name for a mistyped request, or None. Never
    guess-sends: the caller only shows the suggestion."""
    norm = validate_name(raw_name)
    names = list_names(user_id)
    if not norm or not names:
        return None
    matches = difflib.get_close_matches(norm, names, n=1, cutoff=SUGGEST_CUTOFF)
    return matches[0] if matches else None


# ─── Reply text ──────────────────────────────────────────────────────────────

def library_list_text(user_id: int) -> str:
    """List reply for /mystickers and "redster my stickers"."""
    names = list_names(user_id)
    if not names:
        return (
            "You haven't saved any stickers yet.\n"
            "Reply to a sticker with “redster save this as <name>” to save one."
        )
    lines = ["Your stickers:"]
    lines += [f"• {name}" for name in names]
    lines.append(f"\n{len(names)} saved. Send one with “redster send '<name>' sticker”.")
    return "\n".join(lines)


def unknown_name_text(user_id: int, raw_name: str, suggestion: Optional[str] = None) -> str:
    """Reply for a send/delete of a name that isn't saved: the name,
    a "did you mean ...?" when one saved name is clearly close, and
    the user's saved names (or the save hint when the library is empty)."""
    display = html.escape(normalize_name(raw_name) or "that")
    text = f"❌ I don't have a sticker called “{display}”."
    if suggestion:
        text += f"\nDid you mean “{html.escape(suggestion)}”?"
    names = list_names(user_id)
    if names:
        text += "\nYour stickers: " + ", ".join(names)
    else:
        text += ("\nYou haven't saved any stickers yet — reply to a sticker "
                 "with “redster save this as <name>”.")
    return text


# ─── Routing decision (called from bot.py handle_message) ────────────────────

def resolve_library_request(user_id: int, remainder: str,
                            replied_sticker: Optional[dict] = None) -> Optional[dict]:
    """Decide whether a "redster ..." remainder is a sticker-library
    request. `replied_sticker` is {"file_id","is_video","is_animated"}
    when the message replies to a sticker, else None.

    Returns an action dict, or None to fall through to music/image/chat:
      {"action": "list"}
      {"action": "delete", "name": <normalized>}
      {"action": "save", "name": <normalized>, "sticker": <dict|None>}
      {"action": "save_invalid"}
      {"action": "send", "name": <normalized>, "count": int, "requested": int}
      {"action": "unknown", "name": <display>, "suggestion": <str|None>}

    "save ... as <name>" claims the message even with no sticker
    replied to (the handler answers with the "reply to a sticker"
    hint). "this is <name>" only claims when a sticker IS replied to
    — without one it is plain chat and must fall through.
    """
    if not remainder or not isinstance(remainder, str):
        return None
    text = remainder.strip()
    if not text:
        return None

    if parse_list_request(text):
        return {"action": "list"}

    dele = parse_delete_request(text)
    if dele is not None:
        for cand in _candidates(dele):
            norm = validate_name(cand)
            if norm and get_sticker(user_id, norm) is not None:
                return {"action": "delete", "name": norm}
        display = validate_name(dele["name"]) or normalize_name(dele["name"])
        return {
            "action": "unknown",
            "name": display,
            "suggestion": suggest_name(user_id, display),
        }

    save = parse_save_request(text)
    if save is not None:
        if save["kind"] == "this_is" and replied_sticker is None:
            return None
        norm = validate_name(save["name"])
        if norm is None:
            return {"action": "save_invalid"}
        return {"action": "save", "name": norm, "sticker": replied_sticker}

    send = parse_send_request(text)
    if send is not None:
        # parse_send_request only matches when the word "sticker" is
        # present, so a bare "send <x>" never reaches here — it falls
        # through to music/image/chat. A name that isn't saved gets
        # the unknown-name reply (list + suggestion), never a guess.
        norm = validate_name(send["name"])
        if norm and get_sticker(user_id, norm) is not None:
            return {
                "action": "send",
                "name": norm,
                "count": send["count"],
                "requested": send["requested"],
            }
        display = norm or normalize_name(send["name"])
        return {
            "action": "unknown",
            "name": display,
            "suggestion": suggest_name(user_id, display),
        }

    return None


load_sticker_library()
