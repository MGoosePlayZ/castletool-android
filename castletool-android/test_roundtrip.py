"""Offline test: clone -> edit blueprint json like castletool -> push, with a fake API."""
import copy, json, tempfile
from pathlib import Path
import deck_store as ds
from castle_api import CastleAPI

SCENE = {"snapshot": {
    "library": {
        "bp1": {"entryId": "bp1", "entryType": "actorBlueprint", "title": "Hero Guy",
                "library": {"blueprintAssetId": "x"}, "actorBlueprint": {"components": {"Drawing2": {"drawData": "d"}}}},
        "bp2": {"entryId": "bp2", "entryType": "actorBlueprint", "title": "Hero Guy", "actorBlueprint": {"components": {}}},
        "snd": {"entryId": "snd", "entryType": "sound", "title": "boom"},
    },
    "actors": [{"actorId": "1", "parentEntryId": "bp1", "bp": {"components": {"Body": {"x": 1, "y": 2}}}}],
    "variables": [{"id": "v", "name": "score", "initialValue": 0}],
    "sceneProperties": {"backgroundColor": {"r": 1, "g": 0, "b": 0, "a": 0.3}, "clock": {"tempo": 120}},
    "actorBlueprintInherit": {"k": 1}, "linkTargetDeckIds": ["zz"],
}}

class Fake(CastleAPI):
    def __init__(self): super().__init__("tok"); self.saved = []; self.uploaded = {}
    def deck(self, did):
        c = {"cardId": "c1", "title": "Card 1", "sceneDataUrl": "u"}
        return {"deckId": did, "title": "T", "visibility": "unlisted", "variables": [], "initialCard": c, "cards": [c]}
    def download_scene_data(self, url): return copy.deepcopy(SCENE)
    def card(self, cid): raise RuntimeError("Unexpected error.")
    def upload_scene_data(self, cid, sd): self.uploaded[cid] = sd; return "up1"
    def update_card_and_deck_v2(self, d, c):
        self.saved.append((d, c)); return {"deck": {"deckId": d["deckId"]}, "card": {"cardId": c["cardId"]}}

api = Fake()
with tempfile.TemporaryDirectory() as t:
    d = ds.clone_deck(api, "https://castle.xyz/d/abc123", Path(t), log=lambda m: None)
    assert d.name == "abc123"
    bps = sorted(p.name for p in (d/"cards/c1/scene/blueprints").glob("*.json"))
    assert bps == ["hero-guy-2.json", "hero-guy.json"], bps
    ds.push_deck(api, d, log=lambda m: None)
    got = api.uploaded["c1"]["snapshot"]
    exp = copy.deepcopy(SCENE["snapshot"]); exp["sceneProperties"]["backgroundColor"]["a"] = 1
    assert got == exp, (got, exp)
    deck_in, card_in = api.saved[0]
    assert deck_in["initialCardId"] == "c1" and card_in["makeInitialCard"] and card_in["uploadId"] == "up1"
    # castletool-style edit: modify blueprint json, push again
    p = d/"cards/c1/scene/blueprints/hero-guy.json"
    e = json.loads(p.read_text()); e["actorBlueprint"]["components"]["Drawing2"]["drawData"] = "NEW"; p.write_text(json.dumps(e, separators=(",", ":")))
    ds.push_deck(api, d, log=lambda m: None)
    assert api.uploaded["c1"]["snapshot"]["library"]["bp1"]["actorBlueprint"]["components"]["Drawing2"]["drawData"] == "NEW"
    assert "sceneDataUrl" not in json.loads((d/"deck.json").read_text())["cards"][0]
print("ok")
