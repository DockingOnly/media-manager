"""Detail view, editing, merging and matching from .nfo, using copies of real tinyMediaManager .nfo files."""
import shutil
import sys
import tempfile
import time
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mm import core, nfo, tmdbapi, web  # noqa: E402

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="mm-test-"))
lib, st = S / "libD", S / "stD"
for d in (lib, st):
    shutil.rmtree(d, ignore_errors=True)
st.mkdir()
core.CONFIG, core.MATCHES, core.RENAME_LOG, core.LIBRARY, core.EDITS = (st / n for n in ("c.json", "m.json", "l.json", "lib.json", "e.json"))
core.save_json(core.CONFIG, {"tmdb_key": "x", "folder": str(lib)})
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)

# copies of real .nfo files (the originals are only read)
REAL = Path("D:/Media/Movies")
if not REAL.is_dir():
    print("SKIP: needs the real movie library at D:/Media/Movies")
    sys.exit(0)
picked = []
for folder in sorted(REAL.iterdir()):
    if folder.name.startswith(".") or not folder.is_dir():
        continue
    nfos = list(folder.glob("*.nfo"))
    if nfos and "tinyMediaManager" in nfos[0].read_text(encoding="utf-8", errors="ignore"):
        picked.append(nfos[0])
    if len(picked) == 3:
        break
for src in picked:
    dst = lib / src.parent.name
    dst.mkdir(parents=True)
    shutil.copy(src, dst / src.name)
    (dst / (src.stem + ".mkv")).write_bytes(b"x")
    (dst / "poster.jpg").write_bytes(b"\xff\xd8poster")
print("using copies of:", [p.parent.name for p in picked])

# 1. reading tinyMediaManager .nfo files
infos = [nfo.read_nfo(lib / p.parent.name / p.name) for p in picked]
ok(all(i and i["title"] and i["tmdb_id"] and i["imdb_id"] and i["by_tmm"] for i in infos),
   "read title, TMDB and IMDb ids from all three: " + ", ".join(f"{i['title']} ({i['tmdb_id']})" for i in infos))
ok(all(i["genres"] and i["cast"] and i["ratings"] for i in infos), f"genres, cast and ratings found, e.g. {infos[0]['ratings']}")

# 2. matching from the .nfo instead of guessing (stand-in TMDB)
calls = []
def fake_tmdb(path, key=None, **params):
    calls.append(path)
    if path.startswith("/movie/"):
        mid = int(path.split("/")[2])
        return {"id": mid, "title": f"TMDB title {mid}", "release_date": "1999-01-01", "overview": "fresh plot",
                "genres": [{"name": "Fresh"}], "poster_path": "/p.jpg", "credits": {"cast": [], "crew": []}, "videos": {"results": []}}
    if path == "/configuration":
        return {}
    raise AssertionError("searched by name: " + path)
tmdbapi.tmdb = fake_tmdb
c = web.app.test_client()


def job(url, data):
    r = c.post(url, json=data).get_json()
    while not (j := c.get(f"/api/job/{r['job']}").get_json())["finished"]:
        time.sleep(0.05)
    assert not j["error"], j["error"]
    return j["result"]


job("/api/scan", {"folder": str(lib)})
r = job("/api/match-all", {})
ok(r["counts"]["auto"] == 3 and r["counts"]["nfo"] == 3 and not any(p.startswith("/search") for p in calls),
   f"all 3 matched from their .nfo IDs, no name searches ({r['counts']})")
ids = {Path(i["path"]).parent.name: i["id"] for i in r["items"]}
first = picked[0].parent.name
nfo_file = lib / first / picked[0].name
before = nfo_file.read_text(encoding="utf-8")

# 3. detail view
d = c.get("/api/movie", query_string={"id": ids[first]}).get_json()
ok(d["source"] == "nfo" and d["info"]["title"] == infos[0]["title"] and d["editable"], "detail reads the .nfo and is editable")
ok(d["images"].get("poster", "").startswith("/api/image") and any(f["kind"] == "nfo" for f in d["files"]), "local poster and the movie's files listed")
img = c.get(d["images"]["poster"])
ok(img.status_code == 200 and img.data.startswith(b"\xff\xd8"), "the poster is served from the movie's folder")
ok(c.get("/api/image", query_string={"id": ids[first], "kind": "../../c.json"}).status_code == 400, "artwork requests can't reach other files")

# 4. editing in place
d = c.post("/api/movie/edit", json={"id": ids[first], "fields": {"title": "My Title", "year": "2001", "genres": "Horror, Cult",
                                                               "plot": "My plot.", "set": "My Set"}}).get_json()
after = nfo_file.read_text(encoding="utf-8")
root = ET.fromstring(after.split("?>", 1)[1])
keep = ["watched", "playcount", "tag", "fileinfo", "ratings", "dateadded"]
ok(root.findtext("title") == "My Title" and root.findtext("year") == "2001" and [g.text for g in root.findall("genre")] == ["Horror", "Cult"]
   and root.findtext("plot") == "My plot." and root.findtext("set/name") == "My Set" and root.findtext("lockdata") == "true",
   "edits written into the .nfo, with lockdata so Jellyfin keeps them")
ok(all((root.find(k) is not None) == (f"<{k}" in before) for k in keep) and "tinyMediaManager" in after,
   "everything else kept: " + ", ".join(k for k in keep if root.find(k) is not None) + " and the tinyMediaManager note")
m = core.load_json(core.MATCHES, {})[ids[first]]
ok(m["name"] == "My Title (2001)", f"the renamer will use the edited name: {m['name']}")
ok(c.post("/api/movie/edit", json={"id": ids[first], "fields": {"year": "19"}}).status_code == 400, "a bad year is refused")

# 5. updating .nfo files from TMDB keeps the edits and tinyMediaManager's extras
r = job("/api/nfo", {"artwork": False, "overwrite": True})
root = ET.fromstring(nfo_file.read_text(encoding="utf-8").split("?>", 1)[1])
other = ET.fromstring((lib / picked[1].parent.name / picked[1].name).read_text(encoding="utf-8").split("?>", 1)[1])
ok(root.findtext("title") == "My Title" and root.findtext("plot") == "My plot." and root.find("watched") is not None,
   "after 'Update existing .nfo files': your edits and watched status survive")
ok(other.findtext("plot") == "fresh plot" and other.find("watched") is not None and other.find("fileinfo") is not None,
   "an unedited .nfo gets fresh TMDB data but keeps watched status and file details")

# 6. reset to TMDB
d = c.post("/api/movie/edit", json={"id": ids[first], "reset": True}).get_json()
root = ET.fromstring(nfo_file.read_text(encoding="utf-8").split("?>", 1)[1])
ok(not d["edits"] and root.findtext("title").startswith("TMDB title") and root.find("lockdata") is None
   and core.load_json(core.MATCHES, {})[ids[first]]["name"].startswith("TMDB title"), "reset goes back to TMDB and unlocks")

for d in (lib, st):
    shutil.rmtree(d, ignore_errors=True)
print("done")
