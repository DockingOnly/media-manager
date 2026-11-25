"""Several library folders: each remembered with its own saved scan, so switching between them is instant."""
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mm import core, library, web  # noqa: E402

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="mm-test-"))
a, b, st = S / "MoviesA", S / "MoviesB", S / "stF"
for d in (a, b, st):
    shutil.rmtree(d, ignore_errors=True)
st.mkdir(parents=True)
for name in ("CONFIG", "MATCHES", "RENAME_LOG", "LIBRARY", "EDITS", "TV_LIBRARY", "TV_MATCHES"):
    setattr(core, name, st / f"{name.lower()}.json")
core.save_json(core.CONFIG, {})
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)
a.mkdir(); b.mkdir()
(a / "Heat.1995.mkv").write_bytes(b"x"); (a / "Alien.1979.mkv").write_bytes(b"x")
(b / "Rocky.1976.mkv").write_bytes(b"x")
parsed = []
real_parse = library.parse
library.parse = lambda path, root: (parsed.append(path.name), real_parse(path, root))[1]
c = web.app.test_client()


def job(url, data):
    r = c.post(url, json=data).get_json()
    while not (j := c.get(f"/api/job/{r['job']}").get_json())["finished"]:
        time.sleep(0.05)
    assert not j["error"], j["error"]
    return j["result"]


job("/api/scan", {"folder": str(a)})
job("/api/scan", {"folder": str(b)})
cfg = c.get("/api/config").get_json()
ok(cfg["folders"] == [str(b), str(a)] and cfg["folder"] == str(b), "both folders remembered, most recent first; the last one scanned is current")
parsed.clear()
r = job("/api/scan", {"folder": str(a), "refresh": True})
ok(len(r["items"]) == 2 and not parsed and r["changes"] == {"added": 0, "removed": 0},
   "switching back to the first folder reuses its own saved scan: no names re-read, no changes reported")
ok(c.get("/api/library").get_json()["folder"] == str(a), "it's now the current folder, and opens first next time")
cfg = c.post("/api/folders/forget", json={"folder": str(b), "kind": "movies"}).get_json()
ok(cfg["folders"] == [str(a)], "forgetting a folder removes it from the list only")
ok(b.exists() and (b / "Rocky.1976.mkv").exists(), "...and nothing on disk changes")

# an older version's single saved-scan file is picked up, so upgrading doesn't force a rescan
never = S / "MoviesC"  # a folder only an older version of the app scanned
old = {"root": core.key_of(never), "folder": str(never), "items": [{"id": "x"}], "v": library.PARSE_VERSION}
core.save_json(core.LIBRARY, old)
ok(core.load_scan("movies", never) == old and len(core.load_scan("movies", a)["items"]) == 2,
   "the old single saved scan still loads for its folder; other folders keep their own")

shutil.rmtree(S, ignore_errors=True)
print("done")
