"""Blocklist of Castle usernames that may not use Castletool.

The list is a public JSON file in the author's repo:
  {"message": "...$1...", "list": {"Username": "Reason"}}
A blocked person who signs in (and whom Castle authenticates) only sees the message and a close button.
No data about the person leaves the device: the file is downloaded and compared locally. If it can't be
downloaded, the last copy that was downloaded is used; with no copy at all nobody is blocked."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

import requests

URL = "https://raw.githubusercontent.com/MGoosePlayZ/screw-you/refs/heads/main/castle-xyz.json"
DEFAULT_MESSAGE = "You have been blocked from using my tools for reason: $1"
TTL = 300.0                          # seconds a download is reused

_mem: dict = {"t": 0.0, "data": None}
last_error: Optional[str] = None      # why the last download failed (for the log)


def parse(text: str):
    """JSON, forgiving a BOM and trailing commas (easy to leave in a hand-edited list)."""
    text = text.lstrip("\ufeff")
    try:
        return json.loads(text)
    except ValueError:
        return json.loads(re.sub(r",(\s*[}\]])", r"\1", text))


def _clean(data) -> Optional[dict]:
    if not isinstance(data, dict) or not isinstance(data.get("list"), dict):
        return None
    return {"message": str(data.get("message") or DEFAULT_MESSAGE),
            "list": {str(k): str(v) for k, v in data["list"].items()}}


def load(cache_file: Optional[Path] = None, url: str = URL, force: bool = False) -> Optional[dict]:
    """The blocklist (downloaded, else the cached copy, else None). Blocking: call from a thread."""
    now = time.monotonic()
    if not force and _mem["data"] is not None and now - _mem["t"] < TTL:
        return _mem["data"]
    global last_error
    data, last_error = None, None
    try:
        # raw.githubusercontent.com caches for ~5 minutes; a changing query string gets the current file
        r = requests.get(url, params={"cb": int(time.time())}, timeout=8, headers={"Cache-Control": "no-cache"})
        r.raise_for_status()
        data = _clean(parse(r.text))
        if data is None:
            last_error = "unexpected format (needs a \"list\" object)"
        if data is not None and cache_file:
            try:
                Path(cache_file).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
    except Exception as ex:
        data, last_error = None, str(ex) or ex.__class__.__name__
    if data is None and cache_file:
        try:
            data = _clean(json.loads(Path(cache_file).read_text(encoding="utf-8")))
        except Exception:
            data = None
    if data is not None:
        _mem.update(t=now, data=data)
    return data


def _norm(name: str) -> str:
    return (name or "").strip().lstrip("@").casefold()


def message_for(data: Optional[dict], username: Optional[str]) -> Optional[str]:
    """The text to show a blocked person, or None when they may use the app."""
    if not data or not _norm(username or ""):
        return None
    me = _norm(username or "")
    for name, reason in data["list"].items():
        if _norm(name) == me:
            return data["message"].replace("$1", reason)
    return None
