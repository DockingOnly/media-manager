"""End-to-end check of the reorganised app with stand-ins for TMDB, YouTube, OpenSubtitles and the Recycle Bin."""
import shutil
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mm import core, subtitles, tmdbapi, trailers, web  # noqa: E402

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="mm-test-"))
lib, st, binf = S / "libT", S / "stT", S / "binT"
for d in (lib, st, binf):
    shutil.rmtree(d, ignore_errors=True)
st.mkdir()
binf.mkdir()
core.CONFIG, core.MATCHES, core.RENAME_LOG, core.LIBRARY = st / "c.json", st / "m.json", st / "log.json", st / "lib.json"
core.save_json(core.CONFIG, {"tmdb_key": "x", "os_key": "y", "folder": str(lib)})
fake = types.ModuleType("send2trash")
fake.send2trash = lambda p: shutil.move(p, binf / (Path(p).name + f".{time.time_ns()}"))
sys.modules["send2trash"] = fake
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)


def w(rel, data=b"x"):
    p = lib / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def video(rel, size):  # a real one-second video, so its resolution can be read
    p = lib / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([core.ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:duration=1",
                    "-c:v", "mpeg4", str(p)], check=True)


# --- a messy library
w("Heat (1995)/Heat (1995).mkv"); w("Heat (1995)/Heat (1995).nfo"); w("Heat (1995)/poster.jpg")
w("Heat (1995)/trailers/Heat (1995) - Trailer.mkv"); w("Heat (1995)/Heat (1995).en.srt")
video("Halloween (1978)/Halloween (1978) - version.mp4", "1280x720")
video("Halloween (1978)/Halloween (1978) - other.mp4", "1920x816")
w("Nightmare (1984)/Nightmare (1984)/Nightmare (1984).mkv", b"n"); w("Nightmare (1984)/Nightmare (1984)/poster.jpg", b"new")
w("Nightmare (1984)/poster.jpg", b"old"); w("Nightmare (1984)/clearlogo.png")
w("Rocky (1976)/Rocky (1976).mkv", b"r1"); w("Rocky (1976)/Alien.1979.mkv", b"a1")   # shared folder, already named for Rocky
w("Loose.Movie.2001.mkv", b"l")

c = web.app.test_client()


def job(url, data):
    r = c.post(url, json=data).get_json()
    assert "job" in r, r
    while not (j := c.get(f"/api/job/{r['job']}").get_json())["finished"]:
        time.sleep(0.05)
    assert not j["error"], j["error"]
    return j["result"]


# 1. security
ok(c.get("/api/config", headers={"Host": "evil.example:8765"}).status_code == 403, "requests addressed to another host are refused")
ok(c.post("/api/ping", headers={"Origin": "https://evil.example"}).status_code == 403, "posts from another website are refused")
ok(c.get("/api/config").status_code == 200 and c.post("/api/ping").status_code == 200, "the app's own requests work")

# 2. scan, real resolution, file status
items = job("/api/scan", {"folder": str(lib)})["items"]
by = {Path(i["path"]).name: i for i in items}
ok(len(items) == 7, f"found 7 movies (got {len(items)})")
ok(by["Halloween (1978) - version.mp4"]["resolution"] == "720p" and by["Halloween (1978) - other.mp4"]["resolution"] == "1080p",
   "real resolution read from the videos: " + by["Halloween (1978) - version.mp4"]["resolution"] + " / " + by["Halloween (1978) - other.mp4"]["resolution"])
h = by["Heat (1995).mkv"]["files"]
ok(h["nfo"] and h["art"] and h["trailer"] and h["subs"] == ["en"], f"Heat's files detected: {h}")
ok(not any(by["Loose.Movie.2001.mkv"]["files"][k] for k in ("nfo", "art", "trailer")), "a bare movie shows nothing present")

# 3. matches (set directly, no TMDB)
ids = {Path(i["path"]).name: i["id"] for i in items}
M = lambda tid, name: {"tmdb_id": tid, "status": "manual", "name": name, "title": name.split(" (")[0], "year": int(name[-5:-1])}
core.save_json(core.MATCHES, {ids["Heat (1995).mkv"]: M(949, "Heat (1995)"),
    ids["Halloween (1978) - version.mp4"]: M(948, "Halloween (1978)"), ids["Halloween (1978) - other.mp4"]: M(948, "Halloween (1978)"),
    ids["Nightmare (1984).mkv"]: M(377, "Nightmare (1984)"), ids["Rocky (1976).mkv"]: M(1366, "Rocky (1976)"),
    ids["Alien.1979.mkv"]: M(348, "Alien (1979)"), ids["Loose.Movie.2001.mkv"]: M(5, "Loose Movie (2001)")})
items = c.get("/api/library").get_json()["items"]
ok(next(i for i in items if "Heat" in i["path"])["named"], "Heat counts as named for Jellyfin")

# 4. rename preview: versions, no nesting, flattening
pv = c.get("/api/rename/preview").get_json()
plans = {p["name"]: p for p in pv["plans"]}
hal = [ch["to"] for ch in plans["Halloween (1978)"]["changes"]]
ok(any(t.endswith("Halloween (1978) - 720p.mp4") for t in hal) and any(t.endswith("Halloween (1978) - 1080p.mp4") for t in hal),
   f"versions get real resolutions: {hal}")
rocky = plans.get("Rocky (1976)")
ok(rocky is None, "Rocky's file already sits in a folder with its name: no 'Rocky (1976)\\Rocky (1976)' nesting")
nm = plans["Nightmare (1984)"]["changes"]
ok(any(ch["kind"] == "bin" and ch["from"].endswith("Nightmare (1984)\\poster.jpg") for ch in nm)
   and not any("Nightmare (1984)\\Nightmare (1984)\\" in ch["to"] for ch in nm), "doubled folder is flattened; the older outer poster goes to the bin")
ok("Heat (1995)" not in plans and "Alien (1979)" in plans, "Heat is untouched; Alien gets its own folder out of Rocky's")
r = job("/api/rename/apply", {"ids": [p["id"] for p in pv["plans"] if not p["reason"]]})
ok(not r["failed"], f"renamed {r['renamed']} movies without failures")
files = sorted(str(p.relative_to(lib)) for p in lib.rglob("*") if p.is_file())
ok("Nightmare (1984)\\Nightmare (1984).mkv" in files and (lib / "Nightmare (1984)/poster.jpg").read_bytes() == b"new"
   and not (lib / "Nightmare (1984)/Nightmare (1984)").exists(), "Nightmare flattened; the newer poster kept")
ok("Rocky (1976)\\Alien (1979)\\Alien (1979).mkv" in files and "Rocky (1976)\\Rocky (1976).mkv" in files,
   "Alien moved into its own subfolder (shared-folder rule unchanged); Rocky untouched")
ok("Halloween (1978)\\Halloween (1978) - 720p.mp4" in files and "Halloween (1978)\\Halloween (1978) - 1080p.mp4" in files, "Halloween versions renamed")
u = c.post("/api/rename/undo").get_json()
ok(u["failed"] == 0 and (lib / "Halloween (1978)/Halloween (1978) - version.mp4").exists(), f"undo put {u['undone']} moves back")
job("/api/rename/apply", {"ids": [p["id"] for p in c.get("/api/rename/preview").get_json()["plans"] if not p["reason"]]})

# 5. .nfo + artwork (stand-in TMDB), refreshed checkmarks
tmdbapi.tmdb = lambda path, **k: {"id": int(path.split("/")[2]), "title": "T", "release_date": "2000-01-01", "poster_path": "/p.jpg",
                                  "backdrop_path": "/b.jpg", "videos": {"results": []}} if path.startswith("/movie/") else {}
from mm import artwork
artwork.options = lambda tid, kind: [{"url": f"https://image.tmdb.org/t/p/w780/{kind}.jpg"}]
artwork.download_to = lambda url, dst: dst.write_bytes(b"img" * 50)
r = job("/api/nfo", {"artwork": True})
loose = next(i for i in r["items"] if "Loose" in i["path"])
ok(r["nfo"] >= 6 and loose["files"]["nfo"] and loose["files"]["art"], f".nfo job: {r['nfo']} written, {r['images']} images; checkmarks refreshed")

# 6. trailers (stand-in downloads)
trailers.download_trailer = lambda key, base, q, hook: (base.parent.mkdir(parents=True, exist_ok=True), (base.parent / (base.name + ".mkv")).write_bytes(b"t"), base.parent / (base.name + ".mkv"))[2]
trailers.tag_trailer = lambda *a: None
tmdbapi.tmdb = lambda path, **k: {"results": [{"site": "YouTube", "type": "Trailer", "key": "k", "official": True}]}
r = job("/api/trailers", {"quality": 480})
ok(not r["failed"] and all(i["files"]["trailer"] for i in r["items"]), f"trailers: {r['downloaded']} downloaded, {r['had']} already there; all marked")

# 7. subtitles (stand-in OpenSubtitles)
subtitles.os_request = lambda m, p, params=None, data=None, key=None, host=None: (
    {"data": [{"attributes": {"language": "en", "files": [{"file_id": 1}], "moviehash_match": False}}]} if p == "/subtitles" else {"data": []})
subtitles.fetch_subtitle = lambda fid: (b"1\n00:00:01,000 --> 00:00:02,000\nhi\n", ".srt", 99)
r = job("/api/subtitles", {"languages": "en"})
ok(not r["failed"] and r["had"] >= 1 and all(i["files"]["subs"] == ["en"] for i in r["items"]), f"subtitles: {r['downloaded']} downloaded, {r['had']} already there")

# 8. delete (fake Recycle Bin) using the shared rule
items = c.get("/api/library").get_json()["items"]
hv = next(i for i in items if i["path"].endswith("720p.mp4"))
pv = c.post("/api/delete/preview", json={"id": hv["id"]}).get_json()
ok(pv["own"] and pv["suggested"] == "files" and len(pv["other_videos"]) == 1, "deleting one Halloween version suggests just that file")
d = c.post("/api/delete", json={"id": hv["id"], "mode": "files"}).get_json()
ok(d["removed"] >= 1 and not (lib / hv["path"]).exists() and any(binf.iterdir()), "version moved to the (fake) Recycle Bin")

for d in (lib, st, binf):
    shutil.rmtree(d, ignore_errors=True)
print("done")
