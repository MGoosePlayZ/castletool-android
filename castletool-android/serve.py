"""Port of castle-cli `castle serve` (src/commands/serve.ts) for the Flet app.

Runs a small threaded HTTP server on localhost that hosts the Castle WASM
player (bundled in ./player) and feeds it a local deck. Point a WebView (or any
browser) at LocalServe.url.

Differences from the CLI:
  * no screenshot / preview-image endpoints, no IPC registry, no detach
  * scene data comes straight from the internal-format card files
    (deck_store.materialize_card) - no WASM engine needed server side
  * file watching is a simple mtime poll (no chokidar)
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

import requests

from deck_store import is_project_card_dir, materialize_card

CASTLE_WWW = "https://castle.xyz"
PLAYER_DIR = Path(__file__).resolve().parent / "player"
PREFERRED_PORT = 8765  # stable origin => browser can cache the 10 MB wasm

CONTENT_TYPES = {
    ".js": "application/javascript; charset=utf-8",
    ".wasm": "application/wasm",
    ".data": "application/octet-stream",
    ".json": "application/json; charset=utf-8",
    ".html": "text/html; charset=utf-8",
}


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _merge_variables(*groups) -> list:
    result: dict = {}
    for group in groups:
        for var in group:
            key = (var or {}).get("variableId") or (var or {}).get("name")
            if key:
                result[key] = var
    return list(result.values())


class LocalDeck:
    """Reads deck.json + cards/ the same way readLocalDeck() does (project cards only)."""

    def __init__(self, directory: Path):
        self.dir = Path(directory).resolve()
        self.deck_id: Optional[str] = None
        self.title: Optional[str] = None
        self.initial_card_id: Optional[str] = None
        self.variables: list = []
        self.cards: dict[str, dict] = {}  # cardId -> {cardId, title, dir}

        dj = self.dir / "deck.json"
        if dj.exists():
            deck = _read_json(dj)
            self.deck_id = deck.get("deckId")
            self.title = deck.get("title")
            self.initial_card_id = (deck.get("initialCard") or {}).get("cardId")
            self.variables = deck.get("variables") if isinstance(deck.get("variables"), list) else []
            for card in deck.get("cards") or []:
                cid = card.get("cardId")
                cdir = self.dir / "cards" / (cid or "")
                if cid and is_project_card_dir(cdir):
                    self.cards[cid] = {"cardId": cid, "title": card.get("title"), "dir": cdir}

        cards_dir = self.dir / "cards"
        if cards_dir.is_dir():
            for cdir in sorted(cards_dir.iterdir()):
                if cdir.is_dir() and cdir.name not in self.cards and is_project_card_dir(cdir):
                    title = None
                    try:
                        title = _read_json(cdir / "card.json").get("title")
                    except Exception:
                        pass
                    self.cards[cdir.name] = {"cardId": cdir.name, "title": title, "dir": cdir}

        if not self.initial_card_id and self.cards:
            self.initial_card_id = next(iter(self.cards))

    def card(self, card_id: Optional[str]) -> Optional[dict]:
        return self.cards.get(card_id or self.initial_card_id or "")

    def scene_data_text(self, card: dict) -> str:
        return json.dumps(materialize_card(card["dir"]), separators=(",", ":"), ensure_ascii=False)

    def card_variables(self, card: dict) -> list:
        vars_ = materialize_card(card["dir"]).get("snapshot", {}).get("variables")
        return vars_ if isinstance(vars_, list) else []


def get_html(deck: LocalDeck, card: Optional[dict], username: str, version: int) -> str:
    j = json.dumps
    return (
        _HTML
        .replace("__DECK_ID__", j(deck.deck_id or ""))
        .replace("__CARD_ID__", j((card or {}).get("cardId") or ""))
        .replace("__CARD_TITLE__", j((card or {}).get("title") or ""))
        .replace("__USERNAME__", j(username or ""))
        .replace("__FEATURE_FLAGS__", j(j({"scriptDraw": True})))
        .replace("__VERSION__", j(version))
    )


class LocalServe:
    def __init__(
        self,
        deck_dir: Path,
        username: str = "",
        cache_dir: Optional[Path] = None,
        on_log: Callable[[str], None] = print,
        port: int = PREFERRED_PORT,
        card_id: Optional[str] = None,
    ):
        self.deck = LocalDeck(deck_dir)
        if card_id:
            if card_id not in self.deck.cards:
                raise ValueError(f"Card {card_id} has no project files.")
            self.deck.initial_card_id = card_id
        self.username = username
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.on_log = on_log
        self.version = 1
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._core_views: Optional[str] = None
        self._missing_player = [
            f for f in ("main/castle-core.js", "main/castle-core.wasm", "nothread/castle-core.js", "nothread/castle-core.wasm")
            if not (PLAYER_DIR / f).exists()
        ]
        if self._missing_player:
            raise FileNotFoundError(f"Missing player files in {PLAYER_DIR}: {', '.join(self._missing_player)}")
        if not self.deck.card(None):
            raise ValueError("No project-format cards found. Clone the deck in this app first.")

        handler = self._make_handler()
        try:
            self._httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError:
            self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self.url = f"http://localhost:{self.port}/"
        self._threads = [
            threading.Thread(target=self._httpd.serve_forever, daemon=True),
            threading.Thread(target=self._watch, daemon=True),
        ]

    # ── lifecycle ──
    def start(self) -> str:
        for t in self._threads:
            t.start()
        return self.url

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        self._httpd.shutdown()
        self._httpd.server_close()

    # ── file watching: bump version when anything in the deck dir changes ──
    def _snapshot(self) -> dict:
        snap = {}
        for p in self.deck.dir.rglob("*"):
            if p.is_file() and ".castle" not in p.parts:
                try:
                    st = p.stat()
                    snap[str(p)] = (st.st_mtime_ns, st.st_size)
                except OSError:
                    pass
        return snap

    def _watch(self) -> None:
        last = self._snapshot()
        while not self._stop.wait(1.0):
            cur = self._snapshot()
            if cur != last:
                last = cur
                self.bump()

    def bump(self) -> None:
        with self._cond:
            self.version += 1
            self._cond.notify_all()

    # ── helpers ──
    def core_views(self) -> str:
        if self._core_views is not None:
            return self._core_views
        cache = self.cache_dir / "coreviews.json" if self.cache_dir else None
        try:
            r = requests.get(f"{CASTLE_WWW}/api/coreviews", timeout=3)
            if r.ok:
                self._core_views = r.text
                if cache:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(r.text, encoding="utf-8")
                return self._core_views
        except requests.RequestException:
            pass
        if cache and cache.exists():
            self._core_views = cache.read_text(encoding="utf-8")
        else:
            self._core_views = "{}"
        return self._core_views

    def _make_handler(self):
        serve = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # silence
                pass

            def _send(self, status: int, body: bytes, ctype: str, extra: Optional[dict] = None):
                self.send_response(status)
                # Same isolation headers as the CLI so the threaded player build can use SharedArrayBuffer.
                self.send_header("Cross-Origin-Opener-Policy", "same-origin")
                self.send_header("Cross-Origin-Embedder-Policy", "credentialless")
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _json(self, status: int, value):
                self._send(status, json.dumps(value).encode("utf-8"), CONTENT_TYPES[".json"])

            def do_GET(self):
                try:
                    u = urlparse(self.path)
                    path, q = u.path, parse_qs(u.query)
                    deck = serve.deck

                    if path == "/":
                        html = get_html(deck, deck.card(None), serve.username, serve.version)
                        return self._send(200, html.encode("utf-8"), CONTENT_TYPES[".html"], {"Cache-Control": "no-store"})
                    if path == "/favicon.ico":
                        return self._send(204, b"", "image/x-icon")
                    if path == "/scene-data":
                        card = deck.card((q.get("cardId") or [None])[0])
                        if not card:
                            return self._json(404, {"error": "Card not found."})
                        return self._send(200, serve.deck.scene_data_text(card).encode("utf-8"),
                                          CONTENT_TYPES[".json"], {"Cache-Control": "no-store"})
                    if path == "/variables":
                        card = deck.card(None)
                        return self._json(200, {
                            "variables": _merge_variables(deck.variables, deck.card_variables(card) if card else []),
                            "passes": [],
                            "cards": [{"cardId": c["cardId"], "title": c["title"]} for c in deck.cards.values()],
                        })
                    if path == "/coreviews":
                        return self._send(200, serve.core_views().encode("utf-8"), CONTENT_TYPES[".json"])
                    if path == "/version":
                        immediate = (q.get("returnImmediate") or ["false"])[0] == "true"
                        client = int((q.get("version") or ["0"])[0] or 0)
                        with serve._cond:
                            if not immediate and client >= serve.version:
                                serve._cond.wait(timeout=30)
                            version = serve.version
                        return self._json(200, {"version": version})
                    if path.startswith("/player/"):
                        parts = path.split("/")  # ['', 'player', variant, file]
                        if len(parts) == 4 and parts[2] in ("main", "nothread") and parts[3] and "/" not in parts[3] \
                                and parts[3] not in (".", ".."):
                            fp = PLAYER_DIR / parts[2] / parts[3]
                            if fp.is_file():
                                return self._send(200, fp.read_bytes(),
                                                  CONTENT_TYPES.get(fp.suffix, "application/octet-stream"),
                                                  {"Cache-Control": "public, max-age=86400"})
                        return self._send(404, b"Not found", "text/plain; charset=utf-8")
                    return self._send(404, b"Not found", "text/plain; charset=utf-8")
                except Exception as ex:  # surface to the app log, 500 to the page
                    serve.on_log(f"[serve] error: {ex}")
                    self._json(500, {"error": str(ex)})

            def do_POST(self):
                try:
                    path = urlparse(self.path).path
                    n = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
                    if path == "/log":
                        try:
                            entry = json.loads(raw)
                            prefix = f"{entry['blueprintTitle']}: " if entry.get("blueprintTitle") else ""
                            lvl = str(entry.get("level") or "")
                            tag = "ERROR " if "error" in lvl else "WARN " if "warn" in lvl else ""
                            serve.on_log(f"{tag}{prefix}{entry.get('log', '')}")
                        except (ValueError, AttributeError):
                            if raw.strip():
                                serve.on_log(raw.strip())
                        return self._json(200, {"ok": True})
                    if path == "/preview-ready":
                        return self._json(200, {"ok": True})
                    return self._send(404, b"Not found", "text/plain; charset=utf-8")
                except Exception as ex:
                    serve.on_log(f"[serve] error: {ex}")
                    self._json(500, {"error": str(ex)})

        return Handler


# Trimmed port of getHTML() from serve.ts: same Castle bridge object, minus the
# screenshot/preview-client plumbing and the user-info footer.
_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Castle Serve</title>
<style>
  html, body { margin: 0; padding: 0; height: 100%; background: #000; }
  body { display: flex; align-items: flex-start; justify-content: center; overflow: hidden; }
  #player-frame { position: relative; border-radius: 4% / calc(4% * (5 / 7)); overflow: hidden; background: #000; visibility: hidden; }
  #canvas { width: 100%; height: 100%; display: block; border-radius: inherit; outline: none; touch-action: none; }
  #message { position: absolute; color: white; bottom: 20px; left: 20px; font-family: sans-serif; }
</style>
</head>
<body>
<div id="player-frame">
  <canvas id="canvas" tabindex="0" width="450" height="630"></canvas>
  <div id="message"></div>
</div>
<script>
function postLocalLog(level, args) {
  try {
    fetch('/log', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ level: level, log: Array.prototype.slice.call(args).map(function(a) {
        if (a instanceof Error) return a.stack || a.message;
        if (typeof a === 'string') return a;
        try { return JSON.stringify(a); } catch (e) { return String(a); }
      }).join(' ') }) }).catch(function() {});
  } catch (e) {}
}
['log', 'warn', 'error'].forEach(function(level) {
  var original = console[level].bind(console);
  console[level] = function() { original.apply(console, arguments); postLocalLog('browser-' + level, arguments); };
});
window.addEventListener('error', function(e) { postLocalLog('browser-error', [e.error || e.message]); });
window.addEventListener('unhandledrejection', function(e) { postLocalLog('browser-error', [e.reason]); });

var hasSetCurrentVersion = false;
var currentVersion = __VERSION__;

async function getVersion() {
  var r = await fetch('/version?version=' + currentVersion + '&returnImmediate=' + (hasSetCurrentVersion ? 'false' : 'true'));
  return (await r.json()).version;
}
async function checkForUpdate() {
  try {
    var v = await getVersion();
    if (hasSetCurrentVersion && v > currentVersion) { location.reload(); return; }
    currentVersion = v;
    hasSetCurrentVersion = true;
  } catch (e) { setTimeout(checkForUpdate, 2000); return; }
  setTimeout(checkForUpdate, 100);
}

async function loadDeck() {
  var sceneRes = await fetch('/scene-data');
  var varsRes = await fetch('/variables');
  var coreViewsRes = await fetch('/coreviews');
  if (!sceneRes.ok) {
    var msg = document.getElementById('message');
    msg.style.cssText = 'position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:white;font-family:sans-serif;text-align:center;padding:20px;';
    msg.textContent = 'No local scene data found.';
    return;
  }
  var sceneText = await sceneRes.text();
  var varsJson = await varsRes.json();
  var coreViewsText = await coreViewsRes.text();
  var canvas = document.getElementById('canvas');

  window.Castle = {
    hasInitialDeck: true,
    isPlayableMultiplayer: false,
    creatorUsername: __USERNAME__,
    deckId: __DECK_ID__,
    cardId: __CARD_ID__,
    cardTitle: __CARD_TITLE__,
    featureFlags: __FEATURE_FLAGS__,
    nextCardSceneData: sceneText,
    variables: JSON.stringify(varsJson),
    cardCache: {},
    isFocused: true,
    coreViews: coreViewsText,
    authToken: '',
    scriptLog: function(log, level, blueprintTitle) {
      fetch('/log', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ log: log, level: level, blueprintTitle: blueprintTitle }) }).catch(function() {});
    },
    navigateToCardId: async function(nextCardId) {
      var res = await fetch('/scene-data?cardId=' + encodeURIComponent(nextCardId));
      window.Castle.nextCardSceneData = await res.text();
    },
    navigateToDeckId: function() {},
    openDownloadAppLink: function() {},
    bridgeEvent: function(eventString) {},
    dataRequest: async function(requestId, url) {
      var arrayBuffer = new ArrayBuffer(0);
      var success = 1;
      try { var resp = await fetch(url); arrayBuffer = await resp.arrayBuffer(); } catch (e) { success = 0; }
      window.Module.ccall('jsDataRequestCompleted', 'void', ['number', 'number', 'array', 'number'],
        [requestId, success, new Uint8Array(arrayBuffer), arrayBuffer.byteLength]);
    },
    graphqlPostRequest: async function(requestId, body) {
      window.Module.ccall('jsGraphQLPostRequestComplete', 'void', ['number', 'number', 'string'], [requestId, 0, '']);
    },
  };

  var useThread = typeof Atomics !== 'undefined' && typeof SharedArrayBuffer !== 'undefined';
  var variant = useThread ? 'main' : 'nothread';
  window.Module = {
    canvas: canvas,
    locateFile: function(filePath) { return '/player/' + variant + '/' + filePath; },
    mainScriptUrlOrBlob: '/player/' + variant + '/castle-core.js',
  };
  window.Module.preinitializedWebGLContext = canvas.getContext('webgl');
  console.log('player variant: ' + variant);

  var script = document.createElement('script');
  script.src = '/player/' + variant + '/castle-core.js';
  document.head.appendChild(script);
  canvas.focus();
}

function resizeCard() {
  var padding = 0;
  var aw = window.innerWidth - padding * 2;
  var ah = window.innerHeight - padding * 2;
  var w, h;
  if (aw >= 450 && ah >= 630) { w = 450; h = 630; }
  else if (aw / ah < 5 / 7) { w = aw; h = w * (7 / 5); }
  else { h = ah; w = h * (5 / 7); }
  var frame = document.getElementById('player-frame');
  frame.style.width = w + 'px';
  frame.style.height = h + 'px';
  frame.style.visibility = 'visible';
}

resizeCard();
window.addEventListener('resize', resizeCard);
loadDeck();
setTimeout(checkForUpdate, 100);
</script>
</body>
</html>
"""
