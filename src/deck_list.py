"""Port of castle-cli `castle list`: the signed-in user's most recently edited decks."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from castle_api import CastleAPI, CastleAPIError

DEFAULT_LIMIT = 20       # `castle list`'s default (the app asks for pages of its own)


def _format_date(value) -> str:
    if not value:
        return "unknown"
    try:
        # lastModified may be an ISO string or epoch millis.
        if isinstance(value, (int, float)) or str(value).isdigit():
            dt = datetime.fromtimestamp(float(value) / 1000)
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, OverflowError):
        return str(value)


def _one_line(value: Optional[str], max_len: int) -> str:
    line = " ".join((value or "").split())
    return line if len(line) <= max_len else line[: max(0, max_len - 3)] + "..."


def to_listed_deck(deck: dict) -> dict:
    cards = deck.get("cards") if isinstance(deck.get("cards"), list) else []
    initial = deck.get("initialCard") or {}
    return {
        "deckId": deck["deckId"],
        "title": deck.get("title") or "(untitled)",
        "visibility": deck.get("visibility"),
        "caption": deck.get("caption") or "",
        "lastModified": deck.get("lastModified"),
        "playCount": deck.get("playCount") or 0,
        "playTime": deck.get("playTime") or 0,
        "cardCount": len(cards),
        "initialCardId": initial.get("cardId"),
        "initialCardTitle": initial.get("title") or "",
        "bg": initial.get("backgroundColor"),
        "cover": ((initial.get("backgroundImage") or {}).get("smallUrl") or "") if isinstance(initial.get("backgroundImage"), dict) else "",
    }


def list_recent_decks(api: CastleAPI, limit: int = DEFAULT_LIMIT, user_id: str = None) -> list[dict]:
    if not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    if not user_id:
        user = api.me()
        if not user or not user.get("userId"):
            raise CastleAPIError("Unable to load current user.")
        user_id = user["userId"]
    return [to_listed_deck(d) for d in api.decks_for_user(user_id, limit)]


def summary_line(deck: dict) -> str:
    n = deck["cardCount"]
    p = deck["playCount"]
    return (
        f"{deck['deckId']} | {deck['visibility']} | {n} card{'s' if n != 1 else ''}"
        f" | updated {_format_date(deck['lastModified'])} | {p} play{'s' if p != 1 else ''}"
    )


def caption_line(deck: dict) -> str:
    return _one_line(deck["caption"], 120)
