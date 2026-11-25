"""Leftovers: folders left with only artwork/.nfo/tracker files, and tracker junk files, to the Recycle Bin."""
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mm import core, web  # noqa: E402

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="mm-test-"))
lib, st = S / "libL", S / "stL"
for d in (lib, st):
    shutil.rmtree(d, ignore_errors=True)
st.mkdir(parents=True)
for name in ("CONFIG", "MATCHES", "RENAME_LOG", "LIBRARY", "EDITS"):
    setattr(core, name, st / f"{name.lower()}.json")
core.save_json(core.CONFIG, {"tmdb_key": "x", "folder": str(lib)})
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)
binned = []
core.discard = lambda p: (binned.append(p.relative_to(lib).as_posix()), shutil.rmtree(p) if p.is_dir() else p.unlink())


def w(rel, data=b"x"):
    p = lib / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


w("Heat (1995)/Heat (1995).mkv")
w("Heat (1995)/extrathumbs/thumb1.jpg")            # the movie's own: never listed
w("Heat (1995)/Subs/English.srt")                  # the movie's own: never listed
w("Heat (1995)/[TGx]Downloaded from torrentgalaxy.to .txt")
w("Predator.1987.1080p.BluRay.x264.AC3-ETRG/Torrent Downloaded From ExtraTorrent.cc.txt")
w("Predator.1987.1080p.BluRay.x264.AC3-ETRG/Screenshots/1.jpg")
w("Predator.1987.1080p.BluRay.x264.AC3-ETRG/Predator.Sample.mp4")
w("Comedy/desktop.ini")
w("Old Empire (1980)/Old Empire (1980) 360p AAC-trailer.mkv")
w("Old Empire (1980)/Old Empire (1980) 360p AAC-trailer.trickplay/320 - 10x10/0.jpg")
w("Docs/manual.pdf")                               # another kind of file: never listed
w("Keepers/Movie (2001).mkv")                      # a real film inside: never listed
w(".deletedByTMM/Old/poster.jpg")                  # hidden: never listed
w("Alien (1979)/Alien (1979).mkv")
w("Alien (1979)/extras/poster.jpg")                # Jellyfin extras folder: never listed

c = web.app.test_client()
r = c.post("/api/scan", json={"folder": str(lib)}).get_json()
while not (j := c.get(f"/api/job/{r['job']}").get_json())["finished"]:
    time.sleep(0.05)
d = c.get("/api/leftovers").get_json()
folders = sorted(f["path"].replace("\\", "/") for f in d["folders"])
ok(folders == ["Comedy", "Old Empire (1980)", "Predator.1987.1080p.BluRay.x264.AC3-ETRG"],
   f"folders with only artwork/.nfo/tracker files/samples/trailers are leftovers: {folders}")
ok([j["path"].replace("\\", "/") for j in d["junk"]] == ["Heat (1995)/[TGx]Downloaded from torrentgalaxy.to .txt"],
   "tracker junk inside a real movie folder is listed on its own")
emp = next(f for f in d["folders"] if f["path"].startswith("Old Empire"))
ok("trailer/sample" in emp["inside"] and "Jellyfin preview set" in emp["inside"], f"what's inside is described: {emp['inside']}")

(lib / "Comedy/Funny (1990).mkv").write_bytes(b"film")  # changed since the list was made
r = c.post("/api/leftovers/recycle", json={"paths": [f["path"] for f in d["folders"]] + [d["junk"][0]["path"]]}).get_json()
ok(sorted(binned) == ["Heat (1995)/[TGx]Downloaded from torrentgalaxy.to .txt", "Old Empire (1980)", "Predator.1987.1080p.BluRay.x264.AC3-ETRG"]
   and len(r["failed"]) == 1 and "No longer a leftover" in r["failed"][0]["error"],
   "chosen leftovers recycled; a folder that gained a video since is left alone")
ok((lib / "Heat (1995)/Heat (1995).mkv").exists() and (lib / "Docs/manual.pdf").exists() and (lib / "Keepers/Movie (2001).mkv").exists()
   and (lib / "Heat (1995)/extrathumbs/thumb1.jpg").exists(), "films, their own folders and other files untouched")
shutil.rmtree(S, ignore_errors=True)
print("done")
