import _path  # noqa: F401  (puts src/ on sys.path)
"""Offline test of the local serve server against a cloned fake deck."""
import tempfile, threading, time, json
from pathlib import Path
import requests
import deck_store as ds
from serve import LocalServe
from test_roundtrip import Fake  # also runs roundtrip asserts on import

logs = []
with tempfile.TemporaryDirectory() as t:
    d = ds.clone_deck(Fake(), "abc123", Path(t), log=lambda m: None)
    s = LocalServe(d, username="goose", cache_dir=Path(t) / "cache", on_log=logs.append, port=0)
    base = s.start().rstrip("/")
    r = requests.get(base + "/")
    assert r.status_code == 200 and "castle-core.js" in r.text and '"abc123"' in r.text and '"c1"' in r.text
    assert "__" not in r.text.replace("__proto__", ""), "unreplaced placeholder"
    assert r.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    sd = requests.get(base + "/scene-data").json()
    assert sd["snapshot"]["actors"][0]["actorId"] == "1"
    v = requests.get(base + "/variables").json()
    assert v["variables"][0]["name"] == "score" and v["cards"][0]["cardId"] == "c1", v
    w = requests.get(base + "/player/nothread/castle-core.wasm")
    assert w.headers["Content-Type"] == "application/wasm" and len(w.content) > 1_000_000
    assert requests.get(base + "/player/main/castle-core.worker.js").status_code == 200
    assert requests.get(base + "/player/main/..%2f..%2fserve.py").status_code == 404
    assert requests.get(base + "/player/node/castle-core-node.js").status_code == 404
    assert requests.get(base + "/scene-data?cardId=nope").status_code == 404
    assert requests.get(base + "/coreviews").status_code == 200
    # log endpoint
    requests.post(base + "/log", json={"log": "hi", "level": "warn", "blueprintTitle": "Hero"})
    assert logs[-1] == "WARN Hero: hi", logs
    # long-poll version: bumps when a file changes
    cur = requests.get(base + "/version?version=0&returnImmediate=true").json()["version"]
    out = {}
    th = threading.Thread(target=lambda: out.update(requests.get(f"{base}/version?version={cur}", timeout=40).json()))
    th.start(); time.sleep(0.5)
    p = d / "cards/c1/scene/blueprints/hero-guy.json"
    p.write_text(p.read_text() + " ")
    th.join(10)
    assert out.get("version", 0) > cur, out
    s.stop()
print("serve ok")
