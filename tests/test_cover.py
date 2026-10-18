import _path  # noqa: F401  (puts src/ on sys.path)
"""Offline test: cover image upload (multipart), pending cover in card.json, push, deck.json links."""
import copy, json, tempfile
from pathlib import Path
import deck_store as ds
import castle_api as ca
from castle_api import CastleAPI
from session import CardSession

OLD = {"url": "https://image.castle.xyz/old?h=400&format=png8&ar=5:7",
       "smallUrl": "https://image.castle.xyz/old?h=280&format=png8&ar=5:7",
       "largeCardUrl": "https://image.castle.xyz/old?h=800&format=png8&ar=5:7"}
SCENE = {"snapshot": {"library": {}, "actors": [], "variables": []}}


class Fake(CastleAPI):
    def __init__(self):
        super().__init__("tok"); self.saved = []
    def deck(self, did):
        a = {"cardId": "c1", "title": "One", "sceneDataUrl": "u", "backgroundImage": copy.deepcopy(OLD)}
        b = {"cardId": "c2", "title": "Two", "sceneDataUrl": "u", "backgroundImage": copy.deepcopy(OLD)}
        return {"deckId": did, "title": "T", "visibility": "unlisted", "variables": [],
                "initialCard": copy.deepcopy(a), "cards": [a, b]}
    def download_scene_data(self, url): return copy.deepcopy(SCENE)
    def card(self, cid): return None
    def upload_scene_data(self, cid, sd): return "up-" + cid
    def update_card_and_deck_v2(self, d, c):
        self.saved.append(c); return {"deck": {"deckId": d["deckId"]}, "card": {"cardId": c["cardId"]}}


# ---- cover_urls
u = ds.cover_urls("https://image.castle.xyz/abc123")
assert u["smallUrl"] == "https://image.castle.xyz/abc123?h=280&format=png8&ar=5:7" and u["largeCardUrl"].endswith("h=800&format=png8&ar=5:7")
assert ds.cover_urls("https://image.castle.xyz/abc123?h=50")["url"] == "https://image.castle.xyz/abc123?h=400&format=png8&ar=5:7"
assert ds.cover_urls("https://cdn.example/x.png") == {"url": "https://cdn.example/x.png", "smallUrl": "https://cdn.example/x.png", "largeCardUrl": "https://cdn.example/x.png"}

# ---- upload_file: multipart request spec (operations + map + file part), no JSON Content-Type
class Resp:
    status_code = 200
    def json(self): return {"data": {"uploadFile": {"fileId": "F1", "url": "https://image.castle.xyz/F1"}}}
class Sess:
    def post(self, url, **kw): self.kw = kw; self.url = url; return Resp()
api = CastleAPI("tok"); api._session = Sess()
with tempfile.TemporaryDirectory() as t:
    t = Path(t)
    png = t / "cover.png"; png.write_bytes(b"\x89PNG-bytes")
    r = api.upload_file(png)
    kw = api._session.kw
    assert r == {"fileId": "F1", "url": "https://image.castle.xyz/F1"} and api._session.url == ca.API_HOST
    ops = json.loads(kw["data"]["operations"])
    assert "uploadFile(file: $file)" in ops["query"] and ops["variables"] == {"file": None}
    assert json.loads(kw["data"]["map"]) == {"0": ["variables.file"]}
    assert kw["files"]["0"] == ("cover.png", b"\x89PNG-bytes", "image/png")
    assert "Content-Type" not in kw["headers"] and kw["headers"]["X-Auth-Token"] == "tok"
    jpg = t / "c.JPG"; jpg.write_bytes(b"x"); api.upload_file(jpg); assert api._session.kw["files"]["0"][2] == "image/jpeg"
    gif = t / "c.gif"; gif.write_bytes(b"x")
    try: api.upload_file(gif); raise SystemExit("gif accepted")
    except ca.CastleAPIError: pass

    # ---- pending cover -> dirty session -> push_card sends the fileId -> deck.json links follow
    fake = Fake()
    d = ds.clone_deck(fake, "deck1", t / "decks", log=lambda m: None)
    c1 = d / "cards/c1"
    assert ds.card_cover_url(d, "c1") == OLD["smallUrl"]
    sess = CardSession(c1); assert not sess.dirty
    sess.checkpoint(); ds.set_pending_cover(c1, "F1", "https://image.castle.xyz/F1"); sess.refresh_dirty()
    assert sess.dirty and ds.card_cover_url(d, "c1").startswith("https://image.castle.xyz/F1")
    assert json.loads((d / "deck.json").read_text())["cards"][0]["backgroundImage"] == OLD      # deck.json waits for Save
    sess.undo(); assert not sess.dirty and ds.card_cover_url(d, "c1") == OLD["smallUrl"]        # undo restores the old cover
    sess.redo(); assert sess.dirty

    ds.push_card(fake, d, "c1", log=lambda m: None)
    assert fake.saved[-1]["backgroundImageFileId"] == "F1" and fake.saved[-1]["makeInitialCard"] is True
    deck = json.loads((d / "deck.json").read_text())
    new = ds.cover_urls("https://image.castle.xyz/F1")
    assert deck["cards"][0]["backgroundImage"] == new and deck["cards"][1]["backgroundImage"] == OLD
    assert deck["initialCard"]["backgroundImage"] == new                                         # initial card mirrors its source card
    assert "backgroundImageFileId" not in json.loads((c1 / "card.json").read_text())
    ds.push_card(fake, d, "c1", log=lambda m: None); assert "backgroundImageFileId" not in fake.saved[-1]   # sent once only

    # ---- a non-initial card, via push_deck
    c2 = d / "cards/c2"; ds.set_pending_cover(c2, "F2", "https://image.castle.xyz/F2")
    ds.push_deck(fake, d, log=lambda m: None)
    sent = {c["cardId"]: c for c in fake.saved[-2:]}
    assert sent["c2"]["backgroundImageFileId"] == "F2" and "backgroundImageFileId" not in sent["c1"]
    deck = json.loads((d / "deck.json").read_text())
    assert deck["cards"][1]["backgroundImage"] == ds.cover_urls("https://image.castle.xyz/F2")
    assert deck["initialCard"]["backgroundImage"] == new and deck["cards"][0]["backgroundImage"] == new

# ---- decksForUser asks for smallUrl; deck list carries it
assert "backgroundImage { url smallUrl }" in open(Path(__file__).parents[1] / "src/castle_api.py").read()
import deck_list, screens_library as lib
listed = deck_list.to_listed_deck({"deckId": "x", "initialCard": {"cardId": "c", "backgroundColor": "#112233", "backgroundImage": {"smallUrl": "https://s/1"}}})
assert listed["cover"] == "https://s/1" and deck_list.to_listed_deck({"deckId": "y"})["cover"] == ""
assert lib.remote_info(listed)["cover"] == "https://s/1"
assert lib.cover_of({"backgroundImage": {"smallUrl": "s", "url": "u"}}) == "s" and lib.cover_of({}) == ""
assert lib.swatch("#aabbcc", 36, "https://x/y.png").content is not None and lib.swatch("#aabbcc", 36).content is None
print("cover ok")
