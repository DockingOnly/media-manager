"""Converting to H.265 (with real ffmpeg on small generated clips), smart collections (against a stand-in Jellyfin),
poster badges, and the filter language on the server. Nothing here touches your library, Jellyfin, Recycle Bin or
credentials: the Recycle Bin is a folder in the test's own temp folder."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mm import badges, convert, core, jellyfin, query, smart, tmdbapi, web  # noqa: E402

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="mm-test-"))
lib, st = S / "libC", S / "stC"
for d in (lib, st):
    shutil.rmtree(d, ignore_errors=True)
st.mkdir(parents=True)
for name in ("CONFIG", "MATCHES", "RENAME_LOG", "LIBRARY", "EDITS", "TV_LIBRARY", "TV_MATCHES", "TV_CACHE", "TV_RENAME_LOG", "PROBES",
             "HISTORY", "CONVERT", "SMART", "BADGES", "MOVIE_CACHE", "COLLECTIONS"):
    setattr(core, name, st / f"{name.lower()}.json")
core.BACKUPS = st / "backups"
core.recycle_capacity = lambda p: None
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)
c = web.app.test_client()

# a Recycle Bin of our own
binned = []


def fake_discard(path):
    dst = S / "bin" / f"{len(binned)}-{path.name}"
    dst.parent.mkdir(exist_ok=True)
    os.replace(path, dst)
    binned.append((path, dst))
    core.note("recycled", path=path, binned=dst)


core.discard = fake_discard

# ---------------------------------------------------------------- the filter language, on the server
now = time.time()
rows = [
    {"id": "a", "title": "Heat", "year": 1995, "path": "Heat (1995)\\Heat.mkv", "resolution": "2160p", "codec": "H.265", "size": 30e9,
     "mtime": now - 3 * 86400, "match": {"status": "manual", "tmdb_id": 1, "name": "Heat (1995)", "year": 1995},
     "files": {"subs": ["en"], "subs_in": []}, "info": {"hdr": "HDR10", "audio": "TrueHD Atmos 7.1", "genres": ["Crime", "Drama"], "rating": 8.3, "runtime": 170}},
    {"id": "b", "title": "Ran", "year": 1985, "path": "Ran (1985)\\Ran.avi", "resolution": "480p", "codec": "MPEG-4", "size": 0.7e9,
     "mtime": now - 400 * 86400, "match": {"status": "auto", "tmdb_id": 2, "name": "Ran (1985)", "year": 1985},
     "files": {"subs": [], "subs_in": []}, "info": {"genres": ["War"], "rating": 8.2, "runtime": 162}},
    {"id": "c", "title": "Heat", "year": 1995, "path": "Heat (1995)\\Heat.1080p.mp4", "resolution": "1080p", "codec": "H.264", "size": 9e9,
     "mtime": now - 3 * 86400, "match": {"status": "manual", "tmdb_id": 1, "name": "Heat (1995)", "year": 1995},
     "files": {"subs": [], "subs_in": ["en"]}, "info": {"genres": ["Crime"], "rating": 8.3, "runtime": 170}},
]
sel = lambda q: [r["id"] for r in query.select(rows, q)]
ok(sel("res:4k") == ["a"] and sel("res:sd") == ["b"] and sel("res:>=1080") == ["a", "c"], "res: 4k, sd and at-least")
ok(sel("codec:hevc") == ["a"] and sel("codec:xvid") == ["b"] and sel("-codec:hevc -codec:av1") == ["b", "c"], "codec names and their nicknames")
ok(sel("year:1980-1989") == ["b"] and sel("year:<1990") == ["b"] and sel("size:>20gb") == ["a"] and sel("size:<1gb") == ["b"], "years and sizes")
ok(sel("genre:crime -genre:drama") == ["c"] and sel("sub:none") == ["b"] and sel("sub:en") == ["a", "c"], "genres (with not) and subtitles in or beside the file")
ok(sel("added:7d") == ["a", "c"] and sel("added:>30d") == ["b"] and sel("runtime:>2h30") == ["a", "b", "c"], "dates and running times")
ok(sel("hdr:yes audio:atmos rating:8") == ["a"] and sel("dup:yes") == ["a", "c"] and sel('heat "1080p"') == ["c"], "HDR, audio, rating, duplicates and plain words")
ok(query.parse("color:red", "movies")["unknown"] == ["color"], "an unknown field is reported")

# ---------------------------------------------------------------- estimating what converting would win
FF = core.ffmpeg_exe()


def text(codec, w, h, kbps, minutes=120, hdr=""):
    return (f"Input #0, matroska,webm, from 'x.mkv':\n  Duration: {minutes // 60:02d}:{minutes % 60:02d}:00.00, start: 0.000000, bitrate: {kbps + 640} kb/s\n"
            f"  Stream #0:0(eng): Video: {codec} (High), yuv420p(tv, {hdr or 'bt709'}, progressive), {w}x{h} [SAR 1:1 DAR 16:9], 23.98 fps\n"
            f"  Stream #0:1(eng): Audio: ac3, 48000 Hz, 5.1(side), fltp, 640 kb/s\n")


def est(t, size):
    return convert.estimate(Path("x.mkv"), size, "balanced", "hevc", t)


big = est(text("h264", 1920, 1080, 10000), int(10640 * 1000 / 8 * 7200))
ok(big["worth"] and big["target_kbps"] == 5000 and 0.4 < big["saved"] / int(10640 * 1000 / 8 * 7200) < 0.6, f"a 10 Mb/s 1080p H.264 file is worth converting, to about half: {big}")
small = est(text("h264", 1920, 1080, 2300), int(2940 * 1000 / 8 * 7200))
ok(not small["worth"] and small["reason"] == "Already small for its picture size", "a 2.3 Mb/s 1080p file is already small: left alone")
ok(est(text("hevc", 1920, 1080, 9000), 9e9)["reason"] == "Already H.265", "H.265 files are left alone")
ok(est(text("h264", 3840, 2160, 40000, hdr="smpte2084"), 40e9)["reason"].startswith("HDR"), "HDR files are left alone")
ok(convert.res_class(1920, 800) == 1080 and convert.res_class(720, 480) == 480, "letterboxed 1080p counts as 1080p")
argv = convert.plan(Path("D:/m/Old.avi"), {"interlaced": True, "language": "eng"}, "hevc_nvenc", "balanced", 5000)
ok(argv["final"].name == "Old.mkv" and argv["tmp"].name == "Old.mm-converting.part" and "bwdif=mode=send_frame" in argv["cmd"]
   and "-maxrate" in argv["cmd"] and "0:V:0" in argv["cmd"], "old containers become MKV, interlaced pictures are deinterlaced, the bitrate is capped")
mp4 = convert.plan(Path("D:/m/New.mp4"), {}, "hevc_nvenc", "high", 5000)["cmd"]
ok("hvc1" in mp4 and "+faststart" in mp4 and "0:t?" not in mp4 and mp4[mp4.index("-f", mp4.index("-maxrate")) + 1] == "mp4", "MP4s stay MP4 (tagged so Apple devices play them)")

# ---------------------------------------------------------------- a small library of real (tiny) videos
lib.mkdir()
srt = S / "s.srt"
srt.write_text("1\n00:00:01,000 --> 00:00:04,000\nHello\n", encoding="utf-8")


def make(path: Path, *args):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=24:d=30",
                    "-f", "lavfi", "-i", "sine=f=440:d=30", *args, str(path)], check=True, creationflags=core.NO_WINDOW)


heat = lib / "Heat (1995)" / "Heat (1995).mkv"
make(heat, "-i", str(srt), "-map", "0", "-map", "1", "-map", "2", "-c:v", "libx264", "-b:v", "3000k", "-c:a", "aac", "-c:s", "srt",
     "-metadata:s:a:0", "language=eng")
ran = lib / "Ran (1985)" / "Ran (1985).avi"
make(ran, "-c:v", "mpeg4", "-b:v", "2500k", "-c:a", "libmp3lame")
low = lib / "Low (2000)" / "Low (2000).mkv"
make(low, "-c:v", "libx264", "-b:v", "300k", "-c:a", "aac")
big_ = lib / "Big (1988)" / "Big (1988).mkv"
make(big_, "-c:v", "libx265", "-x265-params", "log-level=error", "-c:a", "aac")
(heat.parent / "Heat (1995).nfo").write_text(
    "<?xml version='1.0'?><movie><title>Heat</title><year>1995</year><ratings><rating name='imdb'><value>8.3</value></rating></ratings>"
    "<fileinfo><streamdetails><video><codec>h264</codec></video></streamdetails></fileinfo></movie>", encoding="utf-8")
from PIL import Image  # noqa: E402
Image.new("RGB", (300, 450), (200, 40, 40)).save(heat.parent / "poster.jpg", quality=95)
Image.new("RGB", (300, 450), (40, 40, 200)).save(ran.parent / "poster.png")
core.save_json(core.MATCHES, {core.key_of(heat): {"tmdb_id": 1, "title": "Heat", "year": 1995, "status": "manual", "name": "Heat (1995)"},
                              core.key_of(ran): {"tmdb_id": 2, "title": "Ran", "year": 1985, "status": "manual", "name": "Ran (1985)"},
                              core.key_of(big_): {"tmdb_id": 3, "title": "Big", "year": 1988, "status": "manual", "name": "Big (1988)"}})
core.save_json(core.CONFIG, {"tmdb_key": "k", "folder": str(lib), "folders": [str(lib)], "language": "en-US",
                             "jellyfin_url": "http://jf.local:8096", "jellyfin_key": "jf-test"})


def scan():
    j = c.post("/api/scan", json={"folder": str(lib)}).get_json()
    while not (r := c.get(f"/api/job/{j['job']}").get_json())["finished"]:
        time.sleep(0.05)
    return r["result"]["items"]


items = {Path(i["path"]).stem: i for i in scan()}
ok(items["Heat (1995)"]["resolution"] == "240p" and items["Heat (1995)"]["info"]["rating"] == 8.3, "the test library scans (with ratings from the .nfo)")

# ---------------------------------------------------------------- smart collections, against a stand-in Jellyfin
jf = {"sets": {"x9": "Heist Films"}, "members": {"x9": set()}, "images": [], "deleted": []}


def jf_items():
    out = {}
    for p in lib.rglob("*"):
        if p.suffix in (".mkv", ".avi"):
            out[str(p)] = "i" + str(abs(hash(p.stem)) % 10**6)
    return out


def fake_request(method, path, body=None, raw=None, content_type=None, **kw):
    u = urllib.parse.urlsplit(path)
    q = dict(urllib.parse.parse_qsl(u.query))
    route = u.path
    if route == "/Users":
        return [{"Id": "u1", "Policy": {"IsAdministrator": True}}]
    if route == "/Users/u1/Items":
        if q.get("IncludeItemTypes") == "Movie":
            return {"Items": [{"Id": i, "Path": p} for p, i in jf_items().items()]}
        if q.get("IncludeItemTypes") == "BoxSet":
            return {"Items": [{"Id": i, "Name": n} for i, n in jf["sets"].items()]}
        if "ParentId" in q:
            return {"Items": [{"Id": i} for i in jf["members"][q["ParentId"]]]}
    if route == "/Collections" and method == "POST":
        cid = f"c{len(jf['sets'])}"
        jf["sets"][cid] = q["Name"]
        jf["members"][cid] = set(filter(None, q.get("Ids", "").split(",")))
        return {"Id": cid}
    m = re.fullmatch(r"/Collections/(\w+)/Items", route)
    if m:
        ids = set(q["Ids"].split(","))
        jf["members"][m[1]].update(ids) if method == "POST" else jf["members"][m[1]].difference_update(ids)
        return None
    m = re.fullmatch(r"/Items/(\w+)/Images/Primary", route)
    if m:
        jf["images"].append((m[1], len(raw or b""), content_type))
        return None
    m = re.fullmatch(r"/Items/(\w+)", route)
    if m and method == "DELETE":
        if m[1] not in jf["sets"]:
            raise jellyfin.NotFound("gone")
        jf["sets"].pop(m[1])
        jf["members"].pop(m[1])
        jf["deleted"].append(m[1])
        return None
    raise AssertionError(f"unexpected Jellyfin call {method} {path}")


jellyfin.request = fake_request
ids = jf_items()
r = c.post("/api/smart/preview", json={"kind": "movies", "query": "year:<1990"}).get_json()
ok(r["count"] == 2 and set(r["names"]) == {"Ran (1985)", "Big (1988)"}, f"the preview lists what a rule picks: {r}")
r = c.post("/api/smart/save", json={"name": "Old favourites", "kind": "movies", "query": "year:<1990"}).get_json()
cid = r["collection"]["jellyfin_id"]
ok(jf["sets"].get(cid) == "Old favourites" and jf["members"][cid] == {ids[str(ran)], ids[str(big_)]} and r["result"]["in"] == 2,
   f"saving creates the collection in Jellyfin with what the rule picks: {r.get('result') or r}")
ok(jf["images"] and jf["images"][0][0] == cid and jf["images"][0][2] == "image/jpeg", "it gets a poster made from its members' posters")
r = c.post("/api/smart/save", json={"id": r["collection"]["id"], "name": "Old favourites", "kind": "movies", "query": "year:<1987"}).get_json()
ok(jf["members"][cid] == {ids[str(ran)]} and r["result"]["removed"] == 1 and len(jf["sets"]) == 2, "changing the rule updates the same collection (Big leaves)")
r = c.post("/api/smart/save", json={"name": "Heist Films", "kind": "movies", "query": "title:heat"})
ok(r.status_code == 400 and "wasn't made here" in r.get_json()["error"] and "Heist Films" in jf["sets"].values() and len(smart.listing()) == 1,
   "a collection Jellyfin already has (from .nfo files, say) is never taken over")
r = c.post("/api/smart/save", json={"name": "Odd", "kind": "movies", "query": "colour:red"})
ok(r.status_code == 400 and "isn't a filter field" in r.get_json()["error"], "a rule with an unknown field is refused")
tmdbapi.tmdb = lambda path, **kw: {"items": [{"id": 1, "media_type": "movie"}, {"id": 99, "media_type": "tv"}], "total_pages": 1}
r = c.post("/api/smart/save", json={"name": "On my list", "kind": "movies", "query": "", "tmdb_list": "https://www.themoviedb.org/list/8136-top"}).get_json()
ok(r["collection"]["tmdb_list"] == "8136" and jf["members"][r["collection"]["jellyfin_id"]] == {ids[str(heat)]}, "a TMDB list picks just the movies on it")
jf["members"][cid].add(ids[str(big_)])  # someone added one by hand in Jellyfin: the rule decides
jf["sets"].pop(r["collection"]["jellyfin_id"])  # and someone deleted the other in Jellyfin
r = c.post("/api/smart/sync", json={}).get_json()
ok(jf["members"][cid] == {ids[str(ran)]} and "On my list" in jf["sets"].values(), "Update all puts each collection right, making it again if it was deleted in Jellyfin")
gone = smart.listing()[0]
r = c.post("/api/smart/delete", json={"id": gone["id"]}).get_json()
ok(gone["jellyfin_id"] in jf["deleted"] and len(r["collections"]) == 1 and all(p.exists() for p in (heat, ran, big_)),
   "removing one removes the Jellyfin collection, and no movie")
smart.last.update(time=time.time(), waiting=False)
calls = []
smart.sync_all = lambda only_auto=False: calls.append(only_auto)
smart.maybe_sync([{"added": ["x"]}])
ok(not calls and smart.last["waiting"], "after the watcher adds files, collections wait for Jellyfin to have them")
smart.last["time"] -= 300
smart.maybe_sync([])
ok(calls == [True], "then the automatic ones are brought up to date")

# ---------------------------------------------------------------- poster badges
hp, rp = heat.parent / "poster.jpg", ran.parent / "poster.png"
orig_h, orig_r = hp.read_bytes(), rp.read_bytes()
r = c.post("/api/badges/settings", json={"on": True, "show": {"resolution": True, "rating": True}, "position": "top"}).get_json()
ok(r["settings"]["show"] == {"resolution": True, "hdr": False, "audio": False, "rating": True}, "badge settings are saved")
pv = c.get(f"/api/badges/preview?id={urllib.parse.quote(items['Heat (1995)']['id'])}&resolution=1&rating=1&position=top")
ok(pv.status_code == 200 and pv.mimetype == "image/jpeg" and pv.data != orig_h and hp.read_bytes() == orig_h, "the preview draws badges without touching the poster")


def run_job(url):
    j = c.post(url, json={}).get_json()
    while not (r := c.get(f"/api/job/{j['job']}").get_json())["finished"]:
        time.sleep(0.05)
    return r["result"] or r


r = run_job("/api/badges/apply")
ok(r["changed"] == 2 and hp.read_bytes() != orig_h and rp.read_bytes() != orig_r and Image.open(rp).format == "PNG",
   f"badges go on every poster, PNGs staying PNG: {r}")
ok(badges.original_of(hp).read_bytes() == orig_h, "the original poster is kept, byte for byte")
r = run_job("/api/badges/apply")
ok(r["changed"] == 0 and r["same"] == 2, "running it again changes nothing")
c.post("/api/badges/settings", json={"on": True, "show": {"resolution": True}, "position": "bottom"})
once = hp.read_bytes()
run_job("/api/badges/apply")
ok(hp.read_bytes() != once and badges.original_of(hp).read_bytes() == orig_h, "new settings redraw from the original (badges never pile up)")
time.sleep(1.1)
Image.new("RGB", (300, 450), (10, 160, 60)).save(hp, quality=95)  # new artwork chosen
new_h = hp.read_bytes()
run_job("/api/badges/apply")
ok(badges.original_of(hp).read_bytes() == new_h and hp.read_bytes() != new_h, "a replaced poster becomes the new original")
r = run_job("/api/badges/remove")
ok(r["restored"] == 2 and hp.read_bytes() == new_h and rp.read_bytes() == orig_r and badges.count() == 0
   and not any(badges.store().iterdir()), "Remove all badges puts every original back exactly, and tidies up")

# ---------------------------------------------------------------- converting, for real (tiny clips, on the processor)
convert._encoders = {"hevc": ["libx265"], "av1": []}
convert.MIN_SAVED = 0  # (the clips are tiny)
r = c.get("/api/convert/candidates?kind=movies").get_json()
by = {Path(f["path"]).stem: f for f in r["files"]}
ok(by["Heat (1995)"]["worth"] and by["Ran (1985)"]["worth"] and not by["Low (2000)"]["worth"] and by["Big (1988)"]["reason"] == "Already H.265",
   f"candidates: the files with bitrate to spare: { {k: (v['worth'], v['reason']) for k, v in by.items()} }")
created = convert.created_time(heat)
time.sleep(1.1)
c.post("/api/convert/settings", json={"codec": "hevc", "quality": "small", "encoder": "auto", "check": True})
r = c.post("/api/convert/queue", json={"paths": [str(heat)], "kind": "movies", "start": True}).get_json()
ok(r["running"] and r["queue"][0]["name"] == "Heat (1995)", "converting starts in the background")
ok(c.post("/api/ping").get_json()["convert"]["running"], "the page is told something is converting")
deadline = time.time() + 240
while convert.busy() and time.time() < deadline:
    time.sleep(0.3)
st_ = convert.status()
res = st_["results"][0] if st_["results"] else {}
ok(res.get("ok") and res.get("vmaf") is not None and res["after"] < res["before"], f"Heat converted and passed the quality check: {res}")
info = core.ffmpeg_info(heat)
ok(heat.exists() and "Video: hevc" in info and "Audio: aac" in info and "(eng): Audio" in info and "Subtitle: subrip" in info,
   "the copy is H.265 under the same name, with its audio (and language) and subtitles")
ok(binned and binned[0][0] == heat and binned[0][1].exists(), "the original went to the Recycle Bin")
ok(not list(heat.parent.glob("*.part")), "nothing half-done is left behind")
ok(os.name != "nt" or abs(convert.created_time(heat) - created) < 2, "the copy keeps the original's 'date created' (so Jellyfin doesn't call it new)")
ok("<codec>hevc</codec>" in (heat.parent / "Heat (1995).nfo").read_text(encoding="utf-8"), "the .nfo's technical details now say H.265")
ok(not convert.status()["queue"] and not convert.busy(), "the list is empty afterwards")

res = convert.convert_one(ran, "movies", {"codec": "hevc", "quality": "balanced", "encoder": "cpu", "check": False}, lambda *a: None)
mkv = ran.with_suffix(".mkv")
ok(res["ok"] and mkv.exists() and not ran.exists() and res["path"] == str(mkv), "an AVI becomes an MKV")
ok(core.key_of(mkv) in core.load_json(core.MATCHES, {}) and core.key_of(ran) not in core.load_json(core.MATCHES, {}), "its TMDB match moves with it")

real_args = convert.encoder_args
convert.encoder_args = lambda enc, q, k: ["-c:v", "libx265", "-x265-params", "lossless=1:log-level=error"]  # a copy bigger than the original
convert.add([str(low)], "movies")
real_estimate = convert.estimate
convert.estimate = lambda *a, **k: {**real_estimate(*a, **k), "worth": True, "reason": ""}
convert.start()
deadline = time.time() + 120
while convert.busy() and time.time() < deadline:
    time.sleep(0.3)
convert.estimate, convert.encoder_args = real_estimate, real_args
res = convert.status()["results"][0]
ok(not res["ok"] and res.get("kept") and "smaller" in res["reason"] and low.exists() and len(binned) == 2,
   f"a copy that isn't smaller is thrown away and the original kept: {res}")
ok(convert.tried(low) and "smaller" in convert.tried(low), "and that file isn't offered again (until it changes)")
r = c.post("/api/convert/queue", json={"paths": ["C:\\not\\in\\library.mkv"], "kind": "movies"})
ok(r.status_code == 400, "only files in the library can be queued")

shutil.rmtree(S, ignore_errors=True)
print("done")
