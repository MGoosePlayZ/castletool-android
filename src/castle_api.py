"""Direct Castle API client. Port of castle-cli's src/api.ts (+ login flow
from src/index.ts). No CLI, no subprocess, no Node.

Blocking (requests). Call from a worker thread / asyncio.to_thread in the UI.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Optional

import requests

API_HOST = "https://api.castle.xyz/graphql"

USER_FIELDS = """
  userId
  username
  token
  isAnonymous
  photo {
    url
    avatarUrl
    smallAvatarUrl
  }
  photoFrame {
    frameUrl
  }
"""

DECK_QUERY = """
query($deckId: ID!) {
  deck(deckId: $deckId) {
    deckId
    title
    caption
    visibility
    lastModified
    playCount
    variables
    initialCard {
      cardId
      title
      sceneDataUrl
      backgroundColor
      backgroundImage { url smallUrl largeCardUrl }
    }
    cards {
      cardId
      title
      sceneDataUrl
      backgroundColor
      backgroundImage { url smallUrl largeCardUrl }
    }
  }
}
"""

# Same flags castle-cli sends on every save-deck.
UNLISTED_TEST_CONTENT_FLAGS = {
    "bloodOrGuns": False,
    "profanity": False,
    "sexuallySuggestive": False,
    "horrorOrFear": False,
    "alcoholOrDrugs": False,
    "loudSounds": False,
    "flashingLights": False,
}


class CastleAPIError(Exception):
    pass


# Castle's uploads go through AWS API Gateway, which refuses anything over 10 MB.
UPLOAD_LIMIT_BYTES = 10 * 1024 * 1024
TOO_LARGE_MESSAGE = "This card is over Castle's 10 MB upload limit. Remove some media and try again."


class UploadTooLarge(CastleAPIError):
    """A card (or image) is bigger than Castle accepts in one upload."""


IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def http_error_message(status: int, body: str = "") -> str:
    """A readable message for a failed HTTP request (servers answer with whole HTML/XML pages)."""
    text = (body or "").strip()
    if status == 413 or "EntityTooLarge" in text or "Request Entity Too Large" in text:
        return TOO_LARGE_MESSAGE
    if not text or text.startswith("<") or "<html" in text[:200].lower():
        return f"Castle returned an error (HTTP {status})."
    return f"Castle returned HTTP {status}: {text[:200]}"


class DeckDeleted(CastleAPIError):
    """Castle says we can't view the deck. For a deck we already know, that means it was deleted."""


DECK_GONE_MESSAGE = "permission to view this deck"


class OfflineError(CastleAPIError):
    """Castle's servers couldn't be reached (no network / DNS / timeout)."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class CastleAPI:
    def __init__(self, token: Optional[str] = None, timeout: float = 60.0):
        self.token = token
        self.timeout = timeout
        self._session = requests.Session()

    # ── transport ────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        headers = {
            # Identify as the CLI: the server keys API behavior off these.
            "X-OS": "cli",
            "X-CLI-API-Version": "4",
            "X-Scene-Creator-Version": "latest",
            "Accept": "application/json",
        }
        if self.token:
            headers["X-Auth-Token"] = self.token
        return headers

    def _post(self, **kwargs) -> dict:
        try:
            resp = self._session.post(API_HOST, timeout=self.timeout, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as ex:
            raise OfflineError(str(ex)) from ex
        try:
            body = resp.json()
        except ValueError:
            raise CastleAPIError(http_error_message(resp.status_code, resp.text))

        # GraphQL errors can ride on non-2xx too; prefer their message.
        if isinstance(body, dict):
            if body.get("error"):
                raise CastleAPIError(body["error"].get("message", str(body["error"])))
            if body.get("errors"):
                raise CastleAPIError(body["errors"][0].get("message", str(body["errors"][0])))
        if resp.status_code >= 400:
            raise CastleAPIError(http_error_message(resp.status_code))
        return body["data"]

    def _call(self, query: str, variables: Optional[dict] = None) -> dict:
        headers = {**self._headers(), "Content-Type": "application/json"}
        return self._post(data=json.dumps({"query": query, "variables": variables or {}}), headers=headers)

    # ── auth ─────────────────────────────────────────────────────────────

    def me(self) -> Optional[dict]:
        """Current user. None if not signed in or the token is no longer valid.
        Raises OfflineError if Castle can't be reached."""
        if not self.token:
            return None
        try:
            user = self._call(f"query {{ me {{ {USER_FIELDS} }} }}")["me"]
        except OfflineError:
            raise
        except CastleAPIError:
            return None
        if not user or user.get("isAnonymous"):
            return None
        return user

    def login_password(self, username_or_email: str, password: str) -> dict:
        """In-app sign-in (no browser). Returns the user including .token."""
        data = self._call(
            "mutation($u: String!, $p: String!) { loginV2(usernameOrEmail: $u, password: $p) "
            f"{{ {USER_FIELDS} }} }}",
            {"u": username_or_email, "p": password},
        )
        user = data.get("loginV2")
        if not user or not user.get("token"):
            raise CastleAPIError("Sign-in didn't return a token.")
        self.token = user["token"]
        return user

    def download_bytes(self, url: str, timeout: float = 20.0) -> bytes:
        try:
            r = requests.get(url, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as ex:
            raise OfflineError(str(ex)) from ex
        r.raise_for_status()
        return r.content

    def start_login(self) -> dict:
        """Begin browser login (PKCE-style). Returns {pollToken, url, verifier}.
        Open `url` in a browser, then call poll_login until it returns a user."""
        verifier = _b64url(secrets.token_bytes(32))
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        data = self._call(
            "mutation($challenge: String!) { startCLILogin(challenge: $challenge) { pollToken url } }",
            {"challenge": challenge},
        )
        return {**data["startCLILogin"], "verifier": verifier}

    def poll_login(self, poll_token: str, verifier: str) -> Optional[dict]:
        """One poll attempt. Returns the user (with .token) once the browser
        step is done, None while still pending."""
        try:
            data = self._call(
                "query($pollToken: String!, $verifier: String!) "
                f"{{ pollForCLILogin(pollToken: $pollToken, verifier: $verifier) {{ {USER_FIELDS} }} }}",
                {"pollToken": poll_token, "verifier": verifier},
            )
        except (CastleAPIError, requests.RequestException):
            return None  # CLI treats any error as "keep polling"
        user = data.get("pollForCLILogin")
        if user and user.get("token"):
            self.token = user["token"]
            return user
        return None

    def wait_for_login(
        self,
        poll_token: str,
        verifier: str,
        timeout: float = 300.0,
        interval: float = 1.0,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> Optional[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not cancelled():
            time.sleep(interval)
            user = self.poll_login(poll_token, verifier)
            if user:
                return user
        return None

    # ── decks ────────────────────────────────────────────────────────────

    def deck(self, deck_id: str) -> Optional[dict]:
        try:
            return self._call(DECK_QUERY, {"deckId": deck_id})["deck"]
        except CastleAPIError as ex:
            if not isinstance(ex, (OfflineError, UploadTooLarge)) and DECK_GONE_MESSAGE in str(ex).lower():
                raise DeckDeleted(str(ex)) from ex
            raise

    def decks_for_user(self, user_id: str, limit: Optional[int] = None) -> list[dict]:
        data = self._call(
            """query($userId: ID!, $limit: Int) {
              decksForUser(userId: $userId, limit: $limit, filter: recent_edited) {
                deckId
                title
                visibility
                caption
                lastModified
                playCount
                playTime
                initialCard { cardId title backgroundColor backgroundImage { url smallUrl } }
                cards { cardId title }
              }
            }""",
            {"userId": user_id, "limit": limit},
        )
        return data["decksForUser"] or []

    def card(self, card_id: str) -> Optional[dict]:
        data = self._call(
            "query($cardId: ID!) { card(cardId: $cardId) { cardId deckId } }",
            {"cardId": card_id},
        )
        result = data.get("card")
        return result if result and result.get("cardId") == card_id else None

    def download_scene_data(self, scene_data_url: str) -> Any:
        # Plain GET to storage; no auth header.
        r = requests.get(scene_data_url, timeout=30)
        r.raise_for_status()
        return r.json()

    # ── saving ───────────────────────────────────────────────────────────

    def create_scene_data_upload_config(self, card_ids: list[str]) -> list[dict]:
        data = self._call(
            """mutation($cardIds: [ID!]!) {
              createSceneDataUploadConfig(cardIds: $cardIds) {
                cardId uploadId postUrl postFields
              }
            }""",
            {"cardIds": card_ids},
        )
        return data["createSceneDataUploadConfig"]

    def upload_scene_data(self, card_id: str, scene_data: Any) -> str:
        """Presigned S3 POST. Returns the uploadId for updateCardAndDeckV2."""
        configs = self.create_scene_data_upload_config([card_id])
        if not configs:
            raise CastleAPIError("Failed to get scene data upload config.")
        cfg = configs[0]

        # S3 requires policy fields BEFORE the file part; dicts keep order and
        # requests encodes `data` fields ahead of `files`.
        fields = {"Content-Type": "application/json"}
        fields.update({k: str(v) for k, v in cfg["postFields"].items()})
        payload = json.dumps(scene_data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        r = requests.post(
            cfg["postUrl"],
            data=fields,
            files={"file": ("blob", payload)},
            timeout=120,
        )
        if r.status_code >= 300:
            raise CastleAPIError(http_error_message(r.status_code, r.text))
        return cfg["uploadId"]

    def upload_file(self, path: Path) -> dict:
        """Upload an image (png/jpg) via the uploadFile mutation (GraphQL multipart request
        spec: operations + map + file part). Returns {"fileId", "url"}: pass fileId as
        `backgroundImageFileId` on the next updateCardAndDeckV2; url is a ready-to-show link."""
        path = Path(path)
        mime = IMAGE_MIME.get(path.suffix.lower())
        if not mime:
            raise CastleAPIError("Cover images must be PNG or JPEG.")
        data = path.read_bytes()
        if len(data) > UPLOAD_LIMIT_BYTES:
            raise UploadTooLarge(f"That image is {len(data) / 1048576:.1f} MB, over Castle's "
                                 f"{UPLOAD_LIMIT_BYTES // 1048576} MB upload limit.")
        operations = json.dumps({
            "query": "mutation UploadFile($file: Upload!) { uploadFile(file: $file) { fileId url } }",
            "variables": {"file": None},
        })
        result = self._post(
            data={"operations": operations, "map": json.dumps({"0": ["variables.file"]})},
            files={"0": (path.name, data, mime)},
            headers=self._headers(),          # no Content-Type: requests adds the multipart boundary
        ).get("uploadFile")
        if not result or not result.get("fileId"):
            raise CastleAPIError("Castle didn't accept the image.")
        return result

    def update_card_and_deck_v2(self, deck: dict, card: dict) -> dict:
        data = self._call(
            """mutation($deck: DeckInput!, $card: CardInput!) {
              updateCardAndDeckV2(deck: $deck, card: $card) {
                deck { deckId visibility }
                card { cardId }
              }
            }""",
            {"deck": deck, "card": card},
        )
        return data["updateCardAndDeckV2"]
