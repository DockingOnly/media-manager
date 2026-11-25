"""Renaming never pulls a film out of its own folder, never writes into hidden folders, and keeps your version labels.

(From a real case: a sequel matched as the first film got moved into the first film's folder, and a copy in
tinyMediaManager's '.deletedByTMM' trash folder pulled the live copy in there with it.)"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mm import core, renamer, web  # noqa: E402

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="mm-test-"))
lib, st = S / "libS", S / "stS"
for d in (lib, st):
    shutil.rmtree(d, ignore_errors=True)
st.mkdir(parents=True)
for name in ("CONFIG", "MATCHES", "RENAME_LOG", "LIBRARY", "EDITS"):
    setattr(core, name, st / f"{name.lower()}.json")
core.save_json(core.CONFIG, {"tmdb_key": "x", "folder": str(lib)})
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)


def w(rel, data=b"x"):
    p = lib / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


w("Halloween (1978)/Halloween (1978).mp4", b"one")
w("Halloween - Resurrection (2002)/Halloween - Resurrection (2002) 720p.mp4", b"two")  # wrongly matched as Halloween (1978)
w("Star Wars (1977)/Star Wars (1977) - Theatrical.mp4", b"t")
w("Star Wars (1977)/Star Wars (1977) - Special Edition.mp4", b"se")
w("Predator - Badlands (2025)/Predator Badlands 2025 1080p.mkv", b"live")
w(".deletedByTMM/Predator Badlands123/Predator - Badlands (2025)/Predator - Badlands (2025).mkv", b"trash")
c = web.app.test_client()
import time  # noqa: E402

r = c.post("/api/scan", json={"folder": str(lib)}).get_json()
while not (j := c.get(f"/api/job/{r['job']}").get_json())["finished"]:
    time.sleep(0.05)
items = j["result"]["items"]
ok(not any(".deletedByTMM" in it["path"] for it in items), "tinyMediaManager's trash folder isn't scanned")
ids = {it["path"]: it["id"] for it in items}
M = {"Halloween": (948, "Halloween", 1978), "Star Wars": (11, "Star Wars", 1977), "Predator": (1242898, "Predator: Badlands", 2025)}
matches = {}
for path, i in ids.items():
    key = "Halloween" if "Halloween" in path else "Star Wars" if "Star Wars" in path else "Predator"
    t, title, year = M[key]
    matches[i] = {"tmdb_id": t, "title": title, "year": year, "status": "manual"}
core.save_json(core.MATCHES, matches)

pv = c.get("/api/rename/preview").get_json()
plans = {p["name"]: p for p in pv["plans"]}
h = plans.get("Halloween (1978)")
ok(h and h["reason"] and "different folders" in h["reason"], f"two folders matched to one film are held, with the reason: {h and h['reason']}")
ok("Star Wars (1977)" not in plans, "your version labels ('- Theatrical', '- Special Edition') are kept")
p = plans.get("Predator - Badlands (2025)")
ok(p and not p["reason"] and all(".deletedByTMM" not in ch["to"] for ch in p["changes"]), "the live Predator copy is renamed in its own folder only")

r = c.post("/api/rename/apply", json={"ids": [x["id"] for x in pv["plans"]]}).get_json()
while not (j := c.get(f"/api/job/{r['job']}").get_json())["finished"]:
    time.sleep(0.05)
ok((lib / "Halloween - Resurrection (2002)/Halloween - Resurrection (2002) 720p.mp4").read_bytes() == b"two"
   and (lib / "Halloween (1978)/Halloween (1978).mp4").read_bytes() == b"one", "applying leaves both Halloween films where they are")
ok((lib / ".deletedByTMM/Predator Badlands123/Predator - Badlands (2025)/Predator - Badlands (2025).mkv").read_bytes() == b"trash"
   and (lib / "Predator - Badlands (2025)/Predator - Badlands (2025).mkv").read_bytes() == b"live", "nothing goes into or comes out of the trash folder")

# a version label is kept, but a plain resolution suffix on a single file is still tidied
stems = renamer.version_stems([{"part": None, "resolution": "480p"}, {"part": None, "resolution": "480p"}],
                              [Path("X (2000) - Director's Cut.mkv"), Path("X (2000) 480p.mkv")], "X (2000)")
ok(stems == ["X (2000) - Director's Cut", "X (2000) - 480p"], f"a labelled version keeps its label; the other gets one: {stems}")
shutil.rmtree(S, ignore_errors=True)
print("done")
