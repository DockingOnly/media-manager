"""Artwork: recognising what exists, choosing the best image, downloading, replacing, and the picker endpoints."""
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mm import artwork, core, tmdbapi, web  # noqa: E402

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="mm-test-"))
lib, st, binf = S / "libA", S / "stA", S / "binA"
for d in (lib, st, binf):
    shutil.rmtree(d, ignore_errors=True)
st.mkdir()
binf.mkdir()
core.CONFIG, core.MATCHES, core.RENAME_LOG, core.LIBRARY, core.EDITS = (st / n for n in ("c.json", "m.json", "l.json", "lib.json", "e.json"))
core.save_json(core.CONFIG, {"tmdb_key": "x", "folder": str(lib), "language": "en-US"})
fake = types.ModuleType("send2trash")
fake.send2trash = lambda p: shutil.move(p, binf / (Path(p).name + f".{time.time_ns()}"))
sys.modules["send2trash"] = fake
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)

# a movie in its own folder with tinyMediaManager-style artwork, and two movies sharing a folder
own = lib / "Heat (1995)"
own.mkdir(parents=True)
(own / "Heat (1995).mkv").write_bytes(b"x")
(own / "poster.jpg").write_bytes(b"old poster" * 20)
(own / "logo.png").write_bytes(b"old logo" * 20)
shared = lib / "Misc"
shared.mkdir()
(shared / "Alien (1979).mkv").write_bytes(b"a")
(shared / "Rocky (1976).mkv").write_bytes(b"r")

# stand-ins: TMDB images, fanart.tv, downloads
TMDB_IMAGES = {
    "posters": [{"file_path": "/de.jpg", "iso_639_1": "de", "vote_average": 9, "width": 2000, "height": 3000},
                {"file_path": "/en.jpg", "iso_639_1": "en", "vote_average": 5, "width": 2000, "height": 3000}],
    "backdrops": [{"file_path": "/text.jpg", "iso_639_1": "en", "vote_average": 9, "width": 3840, "height": 2160},
                  {"file_path": "/clean.jpg", "iso_639_1": None, "vote_average": 4, "width": 1920, "height": 1080}],
    "logos": [{"file_path": "/logo.svg", "iso_639_1": "en", "vote_average": 5, "width": 800, "height": 300}],
}
real_tmdb = tmdbapi.tmdb
tmdbapi.tmdb = lambda path, **k: TMDB_IMAGES if path.endswith("/images") else ({"id": int(path.split("/")[2]), "title": "T", "release_date": "1995-01-01"} if path.startswith("/movie/") else {})
FANART = {"hdmovielogo": [{"url": "https://assets.fanart.tv/fanart/movies/1/hdmovielogo/x.png", "lang": "en", "likes": "4"}],
          "moviebanner": [{"url": "https://assets.fanart.tv/fanart/movies/1/moviebanner/b.jpg", "lang": "en", "likes": "1"}],
          "moviedisc": [{"url": "https://assets.fanart.tv/fanart/movies/1/moviedisc/d.png", "lang": "en", "likes": "2"}]}
fanart_calls = []
artwork.fanart_request = lambda tid, key=None: (fanart_calls.append(tid), FANART)[1]
downloads = []
artwork.download_to = lambda url, dst: (downloads.append(url), dst.write_bytes(b"new image " * 20))

# 1. choosing the best image
opts = artwork.options(1, "poster")
ok(opts[0]["url"].endswith("/en.jpg"), "posters: your language beats a better-rated poster in another language")
ok(artwork.options(1, "fanart")[0]["url"].endswith("/clean.jpg"), "backdrops: a clean one without text comes first")
ok([o["url"].rsplit("/", 1)[1] for o in artwork.options(1, "landscape")] == ["text.jpg"], "thumbnails: only backdrops with the title on them")
ok(artwork.options(1, "logo")[0]["url"] == "https://image.tmdb.org/t/p/original/logo.png" and artwork.options(1, "logo")[0]["source"] == "TMDB",
   "logos: without a fanart.tv key, TMDB's logo, converted from SVG to PNG")
core.save_config(fanart_key="k")
ok(artwork.options(1, "logo")[0]["source"] == "fanart.tv" and len(artwork.options(1, "logo")) == 2, "logos: with a fanart.tv key, its HD clear logo first, TMDB's as a choice")
ok(artwork.options(1, "banner")[0]["preview"].endswith("/preview/movies/1/moviebanner/b.jpg"), "banners from fanart.tv, with small previews for the picker")

# 2. the download task
c = web.app.test_client()


def job(url, data):
    r = c.post(url, json=data).get_json()
    assert "job" in r, r
    while not (j := c.get(f"/api/job/{r['job']}").get_json())["finished"]:
        time.sleep(0.05)
    assert not j["error"], j["error"]
    return j["result"]


items = job("/api/scan", {"folder": str(lib)})["items"]
ids = {Path(i["path"]).stem: i["id"] for i in items}
core.save_json(core.MATCHES, {ids["Heat (1995)"]: {"tmdb_id": 949, "status": "manual", "name": "Heat (1995)", "title": "Heat", "year": 1995},
                              ids["Alien (1979)"]: {"tmdb_id": 348, "status": "manual", "name": "Alien (1979)", "title": "Alien", "year": 1979},
                              ids["Rocky (1976)"]: {"tmdb_id": 1366, "status": "manual", "name": "Rocky (1976)", "title": "Rocky", "year": 1976}})
r = job("/api/artwork", {"kinds": list(artwork.KINDS)})
heat = sorted(p.name for p in own.iterdir())
ok((own / "poster.jpg").read_bytes().startswith(b"old poster") and (own / "logo.png").read_bytes().startswith(b"old logo"),
   "existing tinyMediaManager poster.jpg and logo.png recognised and kept")
ok({"fanart.jpg", "landscape.jpg", "banner.jpg", "disc.png"} <= set(heat) and "clearlogo.png" not in heat,
   f"missing kinds added with Jellyfin's names: {[n for n in heat if not n.endswith('.mkv')]}")
misc = sorted(p.name for p in shared.iterdir())
ok("Alien (1979)-poster.jpg" in misc and "Rocky (1976)-clearlogo.png" in misc and "poster.jpg" not in misc,
   "shared folder: each movie gets 'Name-poster.jpg' style names, never a plain poster.jpg")
ok(r["had"] == 2 and r["none"] == 3 and not r["failed"], f"summary: {r['downloaded']} downloaded {r['by_kind']}, {r['had']} already there, {r['none']} not available (no clear art anywhere)")

# 3. replacing, the picker, and serving
r = job("/api/artwork", {"kinds": ["poster"], "replace": True})
ok((own / "poster.jpg").read_bytes().startswith(b"new image") and any(n.startswith("poster.jpg") for n in (p.name for p in binf.iterdir())),
   "replace: new poster in place, the old one in the (fake) Recycle Bin")
d = c.get("/api/movie", query_string={"id": ids["Heat (1995)"]}).get_json()
kinds = {a["kind"]: a["file"] for a in d["artwork"]}
ok(kinds["logo"] == "logo.png" and kinds["banner"] == "banner.jpg" and kinds["clearart"] is None, f"detail window lists every kind: {kinds}")
img = c.get(next(a["url"] for a in d["artwork"] if a["kind"] == "banner"))
ok(img.status_code == 200 and img.data.startswith(b"new image"), "banner served to the detail window")
o = c.get("/api/artwork/options", query_string={"id": ids["Heat (1995)"], "kind": "logo"}).get_json()["options"]
d = c.post("/api/artwork/set", json={"id": ids["Heat (1995)"], "kind": "logo", "url": o[1]["url"]}).get_json()
ok((own / "clearlogo.png").exists() and not (own / "logo.png").exists() and next(a for a in d["artwork"] if a["kind"] == "logo")["file"] == "clearlogo.png",
   "picking a logo saves clearlogo.png; the old logo.png goes to the Recycle Bin")
bad = c.post("/api/artwork/set", json={"id": ids["Heat (1995)"], "kind": "logo", "url": "https://evil.example/x.png"})
ok(bad.status_code == 400 and "TMDB or fanart.tv" in bad.get_json()["error"], "images can only come from TMDB or fanart.tv")

# 4. settings: a refused fanart.tv key
def refuse(key):
    raise core.UserError("fanart.tv rejected the API key. Check it in Settings.")
artwork.check_fanart_key = refuse
ok("rejected" in c.post("/api/settings", json={"fanart_key": "bad"}).get_json().get("error", ""), "a refused fanart.tv key isn't saved")

for d in (lib, st, binf):
    shutil.rmtree(d, ignore_errors=True)
print("done")
