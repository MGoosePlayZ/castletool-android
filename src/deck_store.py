"""Clone / push Castle decks straight against the API.

Port of castle-cli get-deck.ts + save-deck.ts + the card read/write parts of
utils/project.ts, minus the WASM engine.

The CLI converts each card's scene data to YAML/Lua via a WASM engine
(internal <-> "external" values). castletool never touches that layer: it only
edits scene/blueprints/*.json (the raw internal library entries), card.json and
deck.json. So here we keep everything in the internal format and round-trip it
losslessly. On-disk layout stays castletool-compatible:

  <deckId>/deck.json
  <deckId>/cards/<cardId>/card.json
  <deckId>/cards/<cardId>/scene/blueprints/<slug>.json   (internal entries)
  <deckId>/cards/<cardId>/scene/actors.internal.json     (NEW: raw actors)
  <deckId>/cards/<cardId>/scene/variables.json           (NEW: raw variables)
  <deckId>/cards/<cardId>/scene/library-other.json       (NEW: non-blueprint entries)
  <deckId>/cards/<cardId>/.castle/slug-map.json

No actors.yaml / *.yaml / scripts/*.lua are written or read.
"""
from __future__ import annotations

import copy
import json
import re
import secrets
import shutil
from pathlib import Path
from typing import Callable, Optional

import tasks
from castle_api import (CastleAPI, CastleAPIError, UNLISTED_TEST_CONTENT_FLAGS, UPLOAD_LIMIT_BYTES,
                        UploadTooLarge)

Log = Callable[[str], None]

_ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_"


def make_id() -> str:
    return "".join(_ID_ALPHABET[b & 63] for b in secrets.token_bytes(12))


_DECK_ID = re.compile(r"^[A-Za-z0-9_-]{10,14}$")


def looks_like_deck_id(text: str) -> bool:
    return bool(_DECK_ID.match(text.strip()))


def deck_id_from_url(text: str) -> Optional[str]:
    t = text.strip()
    if looks_like_deck_id(t):
        return t
    m = re.search(r"castle\.xyz/(?:d|deck|play)/([A-Za-z0-9_-]{8,20})", t, re.I)
    return m.group(1) if m else None


def parse_deck_id(text: str) -> str:
    """Accept a bare id or a castle.xyz/d/<id> style URL (short s.castle.xyz links
    need resolve_deck_ref, which follows the redirect)."""
    return deck_id_from_url(text) or text.strip()


def resolve_deck_ref(text: str) -> Optional[str]:
    """Any deck reference -> deckId. Follows s.castle.xyz share-link redirects."""
    direct = deck_id_from_url(text)
    if direct:
        return direct
    m = re.match(r"^(?:https?://)?s\.castle\.xyz/([A-Za-z0-9_-]+)", text.strip(), re.I)
    if not m:
        return None
    import requests
    try:
        r = requests.get(f"https://s.castle.xyz/{m.group(1)}", timeout=10, allow_redirects=True)
    except requests.RequestException:
        return None
    return deck_id_from_url(r.url) or deck_id_from_url(r.text)


def _slugify(title: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return slug or fallback


def _unique_slug(base: str, used: set[str]) -> str:
    slug, i = base, 2
    while slug in used:
        slug = f"{base}-{i}"
        i += 1
    used.add(slug)
    return slug


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _scene_properties_for_project(props):
    # CLI forces backgroundColor.a = 1 on both write and materialize.
    if isinstance(props, dict):
        props = copy.deepcopy(props)
        if isinstance(props.get("backgroundColor"), dict):
            props["backgroundColor"]["a"] = 1
    return props


# ── cauldron decks ───────────────────────────────────────────────────────
# A "cauldron" deck runs on the experimentalWeb engine: its scene data holds
# {"experimentalWeb": {"enabled": true, ...}, ...}. Its cards (if any) can't be edited here.

def scene_uses_cauldron(data) -> bool:
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            ew = node.get("experimentalWeb")
            if isinstance(ew, dict) and ew.get("enabled") is True:
                return True
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(v for v in node if isinstance(v, (dict, list)))
    return False


def _top_card(deck_dir: Path) -> tuple[str, str]:
    """(cardId, sceneDataUrl) of the deck's top card, as stored in deck.json."""
    try:
        card = _read_json(Path(deck_dir) / "deck.json").get("initialCard") or {}
    except (OSError, ValueError):
        return "", ""
    return card.get("cardId") or "", card.get("sceneDataUrl") or ""


def _read_cauldron_cache(cache_file: Path) -> dict:
    try:
        data = _read_json(Path(cache_file))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def cauldron_status(cache_file: Path, deck_dir: Path) -> tuple[Optional[bool], bool]:
    """(known answer or None, whether it still has to be fetched) without touching the network."""
    card_id, url = _top_card(deck_dir)
    entry = _read_cauldron_cache(cache_file).get(card_id) if card_id else None
    if isinstance(entry, dict) and "cauldron" in entry and (not url or entry.get("url") == url):
        return bool(entry["cauldron"]), False
    return None, bool(card_id and url)


def detect_cauldron(api: CastleAPI, deck_dir: Path, cache_file: Path) -> bool:
    """Does this deck use cauldron? Only the top card's scene data is looked at; the answer is
    cached per card (and re-fetched when the card's scene data URL changes). Blocks; raises
    on network errors (nothing is cached then)."""
    known, needs_fetch = cauldron_status(cache_file, deck_dir)
    if known is not None or not needs_fetch:
        return bool(known)
    card_id, url = _top_card(deck_dir)
    result = scene_uses_cauldron(api.download_scene_data(url))
    cache = _read_cauldron_cache(cache_file)
    cache[card_id] = {"url": url, "cauldron": result}
    _write_json(Path(cache_file), cache)
    return result


def list_local_decks(root: Path) -> list[Path]:
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir() if d.is_dir() and (d / "deck.json").exists())


# ── clone ────────────────────────────────────────────────────────────────

def _write_card(card_dir: Path, card: dict, scene_data: dict) -> None:
    snapshot = scene_data.get("snapshot") or {}
    library = snapshot.get("library") or {}

    # replace=True behavior: wipe what we own, leave anything else alone.
    shutil.rmtree(card_dir / "scene" / "blueprints", ignore_errors=True)
    for stale in ("actors.internal.json", "variables.json", "library-other.json"):
        (card_dir / "scene" / stale).unlink(missing_ok=True)
    (card_dir / ".castle" / "slug-map.json").unlink(missing_ok=True)
    (card_dir / "scene" / "blueprints").mkdir(parents=True, exist_ok=True)

    used: set[str] = set()
    slug_to_entry: dict[str, str] = {}
    other: dict[str, dict] = {}
    for entry_id, entry in library.items():
        if entry.get("entryType") and entry["entryType"] != "actorBlueprint":
            other[entry_id] = entry  # CLI drops these; we keep them so push is lossless
            continue
        slug = _unique_slug(_slugify(entry.get("title") or "", entry_id), used)
        slug_to_entry[slug] = entry_id
        _write_json(card_dir / "scene" / "blueprints" / f"{slug}.json", entry)

    _write_json(card_dir / "scene" / "actors.internal.json", snapshot.get("actors") or [])
    _write_json(card_dir / "scene" / "variables.json", snapshot.get("variables") or [])
    if other:
        _write_json(card_dir / "scene" / "library-other.json", other)

    _write_json(card_dir / "card.json", {
        **card,
        "sceneProperties": _scene_properties_for_project(snapshot.get("sceneProperties")),
        "actorBlueprintInherit": snapshot.get("actorBlueprintInherit"),
        "linkTargetDeckIds": snapshot.get("linkTargetDeckIds") or [],
    })
    _write_json(card_dir / ".castle" / "slug-map.json", slug_to_entry)


def clone_deck(api: CastleAPI, deck_id: str, root: Path, log: Log = print) -> Path:
    deck_id = parse_deck_id(deck_id)
    if not deck_id:
        raise ValueError("No deck id given.")

    deck = api.deck(deck_id)
    if not deck:
        raise CastleAPIError(f"Deck not found: {deck_id}")

    deck_dir = Path(root) / deck["deckId"]
    deck_dir.mkdir(parents=True, exist_ok=True)

    cards = deck.get("cards") or ([deck["initialCard"]] if deck.get("initialCard") else [])
    _write_json(deck_dir / "deck.json", {
        "deckId": deck["deckId"],
        "title": deck.get("title") or "",
        "caption": deck.get("caption") or "",
        "visibility": deck.get("visibility"),
        "lastModified": deck.get("lastModified"),
        "playCount": deck.get("playCount") or 0,
        "variables": deck.get("variables") or [],
        "initialCard": deck.get("initialCard"),
        "cards": cards,
    })

    for ci, card in enumerate(cards):
        if not card.get("cardId") or not card.get("sceneDataUrl"):
            continue
        tasks.step(ci, len(cards), f"Downloading card {ci + 1}/{len(cards)}")
        log(f"getting {card['cardId']}...")
        scene_data = api.download_scene_data(card["sceneDataUrl"])
        _write_card(deck_dir / "cards" / card["cardId"], card, scene_data)

    log(f"Got {deck['deckId']}")
    return deck_dir


# ── push ─────────────────────────────────────────────────────────────────

def is_project_card_dir(card_dir: Path) -> bool:
    return (card_dir / "scene" / "blueprints").exists()


def materialize_card(card_dir: Path) -> dict:
    """Rebuild the scene data upload payload from the on-disk card."""
    bp_dir = card_dir / "scene" / "blueprints"
    if not bp_dir.exists():
        raise FileNotFoundError(f"Missing project blueprints directory: {bp_dir}")

    actors_path = card_dir / "scene" / "actors.internal.json"
    if not actors_path.exists():
        raise FileNotFoundError(
            f"{actors_path} not found. This card was made by the Castle CLI "
            "(YAML format). Re-clone the deck in this app to push it."
        )

    card_json = _read_json(card_dir / "card.json") if (card_dir / "card.json").exists() else {}

    library: dict[str, dict] = {}
    for f in sorted(bp_dir.glob("*.json")):
        entry = _read_json(f)
        if isinstance(entry, dict) and entry.get("entryId"):
            library[entry["entryId"]] = entry
    other_path = card_dir / "scene" / "library-other.json"
    if other_path.exists():
        for entry_id, entry in _read_json(other_path).items():
            library.setdefault(entry_id, entry)

    variables_path = card_dir / "scene" / "variables.json"
    snapshot = {
        "library": library,
        "actors": _read_json(actors_path),
        "variables": _read_json(variables_path) if variables_path.exists() else [],
        "sceneProperties": _scene_properties_for_project(card_json.get("sceneProperties")),
        "linkTargetDeckIds": card_json.get("linkTargetDeckIds") or [],
    }
    if card_json.get("actorBlueprintInherit") is not None:
        snapshot["actorBlueprintInherit"] = card_json["actorBlueprintInherit"]
    if snapshot["sceneProperties"] is None:
        del snapshot["sceneProperties"]
    return {"snapshot": snapshot}


def _payload_size(payload: dict) -> int:
    return len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def card_upload_size(card_dir: Path) -> int:
    """Bytes of ONE card's scene data when uploaded. Each card is its own upload, so this is
    the size Castle's limit applies to."""
    return _payload_size(materialize_card(Path(card_dir)))


def deck_upload_size(deck_dir: Path) -> int:
    """Bytes all of the deck's scene data comes to (informational: cards upload one by one)."""
    deck_dir = Path(deck_dir)
    deck = _read_json(deck_dir / "deck.json")
    cards = deck.get("cards") or ([deck["initialCard"]] if deck.get("initialCard") else [])
    total = 0
    for card in cards:
        card_dir = deck_dir / "cards" / (card.get("cardId") or "")
        if not card.get("cardId") or not is_project_card_dir(card_dir):
            continue
        try:
            total += card_upload_size(card_dir)
        except FileNotFoundError:
            continue
    return total


def assert_card_fits(card_dir: Path, limit: int = UPLOAD_LIMIT_BYTES) -> None:
    """Refuse to upload a card Castle would reject."""
    size = card_upload_size(card_dir)
    if size > limit:
        raise UploadTooLarge(f"Card {Path(card_dir).name} is {size / 1048576:.1f} MB, over Castle's "
                             f"{limit // 1048576} MB upload limit. Remove some media and try again.")


def assert_deck_fits(deck_dir: Path, limit: int = UPLOAD_LIMIT_BYTES) -> None:
    """Check every card of the deck against the limit, one by one (the limit is per card,
    not per deck)."""
    deck_dir = Path(deck_dir)
    deck = _read_json(deck_dir / "deck.json")
    cards = deck.get("cards") or ([deck["initialCard"]] if deck.get("initialCard") else [])
    for card in cards:
        card_dir = deck_dir / "cards" / (card.get("cardId") or "")
        if card.get("cardId") and is_project_card_dir(card_dir):
            try:
                assert_card_fits(card_dir, limit)
            except FileNotFoundError:
                continue


# ── cover images ─────────────────────────────────────────────────────────
# A card's cover is `backgroundImage` {url, smallUrl, largeCardUrl} in card.json and deck.json.
# Picking a new image uploads it right away and parks the result in card.json
# (backgroundImageFileId + the new links), so the card turns dirty and undo / discard work.
# Save sends the fileId; only then do the links move into deck.json (initial card = its source card).

_CASTLE_IMAGE_HOST = "image.castle.xyz"


def cover_urls(url: str) -> dict:
    """Display links for an uploaded image. Castle's image host serves resized variants via query
    params (same ones deck.json already carries); any other url is used as-is for all three."""
    base = (url or "").split("?", 1)[0]
    if _CASTLE_IMAGE_HOST in base:
        return {"url": f"{base}?h=400&format=png8&ar=5:7",
                "smallUrl": f"{base}?h=280&format=png8&ar=5:7",
                "largeCardUrl": f"{base}?h=800&format=png8&ar=5:7"}
    return {"url": url, "smallUrl": url, "largeCardUrl": url}


def set_pending_cover(card_dir: Path, file_id: str, url: str) -> None:
    card_json_path = Path(card_dir) / "card.json"
    card_json = _read_json(card_json_path) if card_json_path.exists() else {}
    card_json["backgroundImageFileId"] = file_id
    card_json["backgroundImage"] = cover_urls(url)
    _write_json(card_json_path, card_json)


def card_cover_url(deck_dir: Path, card_id: str) -> str:
    """Small cover link for a card tile: card.json first (it may hold an unsaved new cover), then deck.json."""
    deck_dir = Path(deck_dir)
    try:
        cj = _read_json(deck_dir / "cards" / card_id / "card.json")
    except (OSError, ValueError):
        cj = {}
    img = cj.get("backgroundImage") if isinstance(cj, dict) else None
    if not (isinstance(img, dict) and img.get("smallUrl")):
        try:
            deck = _read_json(deck_dir / "deck.json")
        except (OSError, ValueError):
            return ""
        img = next((c.get("backgroundImage") for c in (deck.get("cards") or []) if c.get("cardId") == card_id), None)
    return (img.get("smallUrl") or "") if isinstance(img, dict) else ""


def _set_cover_in_deck(deck: dict, card_id: str, image: Optional[dict]) -> None:
    for c in list(deck.get("cards") or []) + [deck.get("initialCard")]:
        if isinstance(c, dict) and c.get("cardId") == card_id:
            c["backgroundImage"] = copy.deepcopy(image)


def _sync_initial_cover(deck: dict) -> None:
    """The initial card's links are always its source card's links."""
    initial = deck.get("initialCard")
    if not isinstance(initial, dict):
        return
    src = next((c for c in deck.get("cards") or [] if c.get("cardId") == initial.get("cardId")), None)
    if src is not None and "backgroundImage" in src:
        initial["backgroundImage"] = copy.deepcopy(src["backgroundImage"])


def _card_input(card: dict, card_json: dict, title: str, upload_id: str, is_initial: bool) -> dict:
    result = {"cardId": card["cardId"], "title": title, "blocks": [],
              "uploadId": upload_id, "makeInitialCard": is_initial}
    if card_json.get("backgroundImageFileId"):
        result["backgroundImageFileId"] = card_json["backgroundImageFileId"]
    return result


def _cover_saved(card_dir: Path, card_json_path: Path, deck: dict, card_id: str) -> bool:
    """After a successful save: the pending cover is now on Castle. Returns True if deck changed."""
    card_json = _read_json(card_json_path) if card_json_path.exists() else {}
    if not card_json.pop("backgroundImageFileId", None):
        return False
    _write_json(card_json_path, card_json)
    _set_cover_in_deck(deck, card_id, card_json.get("backgroundImage"))
    return True


def _assert_card_can_be_saved(api: CastleAPI, card_id: str, deck_id: str) -> None:
    # `card` resolver errors when the row doesn't exist (new deck): treat as absent.
    try:
        existing = api.card(card_id)
    except Exception:
        return
    if not existing or existing.get("deckId") == deck_id:
        return
    raise CastleAPIError(
        f"Refusing to save card {card_id} into deck {deck_id}: it already belongs to "
        f"deck {existing.get('deckId')}. (Deck dir copied without regenerating card ids?)"
    )


def push_deck(api: CastleAPI, deck_dir: Path, log: Log = print) -> str:
    deck_dir = Path(deck_dir)
    deck_json_path = deck_dir / "deck.json"
    if not deck_json_path.exists():
        raise FileNotFoundError(f"No deck.json found in {deck_dir}.")

    deck = _read_json(deck_json_path)
    was_new = not deck.get("deckId")
    deck["deckId"] = deck.get("deckId") or make_id()
    deck["title"] = deck.get("title") or deck_dir.name
    deck.setdefault("visibility", "unlisted")
    deck.setdefault("variables", [])

    cards = deck.get("cards") or ([deck["initialCard"]] if deck.get("initialCard") else [])
    if not cards:
        raise ValueError(f"No cards found in {deck_dir}.")
    # Castle's limit applies to each card's upload, not to the deck as a whole: check them one
    # by one up front so nothing is half-pushed.
    total = deck_upload_size(deck_dir)
    if total > UPLOAD_LIMIT_BYTES:
        log(f"Deck is {total / 1048576:.1f} MB in total; cards upload one at a time, so that's fine.")
    assert_deck_fits(deck_dir)
    for card in cards:
        card["cardId"] = card.get("cardId") or make_id()
        _assert_card_can_be_saved(api, card["cardId"], deck["deckId"])

    initial_card_id = (deck.get("initialCard") or {}).get("cardId") or cards[0]["cardId"]

    for index, card in enumerate(cards):
        is_initial = card["cardId"] == initial_card_id
        card_dir = deck_dir / "cards" / card["cardId"]
        if not is_project_card_dir(card_dir):
            raise FileNotFoundError(f"Card {card['cardId']} is not a project-format card.")

        card_json_path = card_dir / "card.json"
        card_json = _read_json(card_json_path) if card_json_path.exists() else {}
        card_json["cardId"] = card["cardId"]
        card_json["title"] = card.get("title") or card_json.get("title") or f"Card {index + 1}"
        _write_json(card_json_path, card_json)

        scene_data = materialize_card(card_dir)
        upload_id = api.upload_scene_data(card["cardId"], scene_data)

        deck_input = {
            "deckId": deck["deckId"],
            "title": deck["title"],
            "visibility": deck["visibility"],
            "contentFlags": UNLISTED_TEST_CONTENT_FLAGS,
        }
        if is_initial:
            deck_input["initialCardId"] = card["cardId"]
            deck_input["variables"] = deck.get("variables") or []
        if deck.get("multiplayerEnabledByCreator"):
            deck_input["multiplayerEnabledByCreator"] = True

        result = api.update_card_and_deck_v2(deck_input, _card_input(
            card, card_json, card.get("title") or f"Card {index + 1}", upload_id, is_initial))
        if result["deck"]["deckId"] != deck["deckId"]:
            raise CastleAPIError(f"Server returned unexpected deck id {result['deck']['deckId']}.")
        if result["card"]["cardId"] != card["cardId"]:
            raise CastleAPIError(f"Server returned unexpected card id {result['card']['cardId']}.")

        card.pop("sceneDataUrl", None)
        if is_initial:
            deck["initialCard"] = card
        _cover_saved(card_dir, card_json_path, {"cards": cards}, card["cardId"])
        log(f"Saved card {card['cardId']}.")

    deck["cards"] = cards
    _sync_initial_cover(deck)
    _write_json(deck_json_path, deck)
    url = f"https://castle.xyz/d/{deck['deckId']}"
    log(f"{'Created' if was_new else 'Updated'} {deck['visibility']} deck: {url}")
    return url


def push_card(api: CastleAPI, deck_dir: Path, card_id: str, log: Log = print) -> None:
    """Save ONE card to Castle. The API call is per card (updateCardAndDeckV2 takes a
    deck input plus a single card), exactly as push_deck does it in a loop.
    Unlike push_deck this does NOT send contentFlags, so content warnings set on
    Castle are left alone."""
    deck_dir = Path(deck_dir)
    deck_json_path = deck_dir / "deck.json"
    if not deck_json_path.exists():
        raise FileNotFoundError(f"No deck.json found in {deck_dir}.")
    deck = _read_json(deck_json_path)
    if not deck.get("deckId"):
        raise ValueError("This deck has no deck ID; clone it from Castle first.")
    cards = deck.get("cards") or ([deck["initialCard"]] if deck.get("initialCard") else [])
    card = next((c for c in cards if c.get("cardId") == card_id), None)
    if card is None:
        raise ValueError(f"Card {card_id} isn't in this deck.")
    card_dir = deck_dir / "cards" / card_id
    if not is_project_card_dir(card_dir):
        raise FileNotFoundError(f"Card {card_id} has no project files.")

    initial_id = (deck.get("initialCard") or {}).get("cardId") or cards[0]["cardId"]
    is_initial = card_id == initial_id
    index = [c.get("cardId") for c in cards].index(card_id)

    card_json_path = card_dir / "card.json"
    card_json = _read_json(card_json_path) if card_json_path.exists() else {}
    card_json["cardId"] = card_id
    card_json["title"] = card.get("title") or card_json.get("title") or f"Card {index + 1}"
    _write_json(card_json_path, card_json)

    tasks.say("Checking size", 0.05)
    assert_card_fits(card_dir)                    # only this card: each card is its own upload
    tasks.say("Preparing card", 0.1)
    scene = materialize_card(card_dir)
    tasks.say("Uploading card", 0.3)
    upload_id = api.upload_scene_data(card_id, scene)
    tasks.say("Updating deck", 0.85)             # last point where Cancel still applies
    deck_input = {
        "deckId": deck["deckId"],
        "title": deck.get("title") or deck_dir.name,
        "visibility": deck.get("visibility") or "unlisted",
    }
    if is_initial:
        deck_input["initialCardId"] = card_id
        deck_input["variables"] = deck.get("variables") or []
    result = api.update_card_and_deck_v2(deck_input, _card_input(
        card, card_json, card.get("title") or card_json["title"], upload_id, is_initial))
    if result["deck"]["deckId"] != deck["deckId"] or result["card"]["cardId"] != card_id:
        raise CastleAPIError("Server returned unexpected ids.")
    if _cover_saved(card_dir, card_json_path, deck, card_id):
        _sync_initial_cover(deck)
        _write_json(deck_json_path, deck)
    log(f"Saved card {card_id}.")
