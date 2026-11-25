"""Duplicates: copies of one movie or episode side by side; the weaker ticked only when clearly safe; the last
copy never recycled; a copy's own subtitles/.nfo/artwork go with it, shared ones stay."""
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mm import core, web  # noqa: E402

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="mm-test-"))
lib, tv, st, binf = S / "libD", S / "tvD", S / "stD", S / "binD"
for d in (lib, tv, st, binf):
    shutil.rmtree(d, ignore_errors=True)
st.mkdir(parents=True)
binf.mkdir()
for name in ("CONFIG", "MATCHES", "RENAME_LOG", "LIBRARY", "EDITS", "TV_LIBRARY", "TV_MATCHES", "TV_CACHE", "TV_RENAME_LOG"):
    setattr(core, name, st / f"{name.lower()}.json")
core.save_json(core.CONFIG, {"tmdb_key": "x", "folder": str(lib), "tv_folder": str(tv)})
fake = types.ModuleType("send2trash")
fake.send2trash = lambda p: shutil.move(p, binf / (Path(p).name + f".{time.time_ns()}"))
sys.modules["send2trash"] = fake
core.recycle_capacity = lambda p: None
core.ffmpeg_info = lambda p, timeout=60: Path(p).read_text()  # each test "video" describes itself, as ffmpeg would
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)


def vid(minutes, w=1920, h=1080, codec="h264"):
    return (f"Duration: {minutes // 60:02d}:{minutes % 60:02d}:00.00, start: 0\n"
            f"  Stream #0:0: Video: {codec} (High), yuv420p, {w}x{h}, 24 fps\n"
            "  Stream #0:1(eng): Audio: ac3, 48000 Hz, 5.1(side), fltp\n")


def w(base, rel, data="x"):
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data)


w(lib, "Heat (1995)/Heat (1995).mkv", vid(170))
w(lib, "Heat.1995.720p/Heat.1995.720p.mkv", vid(170, 1280, 720) + " " * 50)
w(lib, "Heat.1995.720p/Heat.1995.720p.en.srt")
w(lib, "Heat.1995.720p/Heat.1995.720p-poster.jpg")
w(lib, "Aliens (1986)/Aliens (1986).mkv", vid(137))
w(lib, "Aliens (1986)/Aliens.1986.Special.Edition.mkv", vid(154))
w(lib, "Blade Runner (1982)/Blade Runner (1982).mkv", vid(117))
w(lib, "Blade Runner 1982 720p/Blade Runner 1982 720p.mkv", vid(97, 1280, 720))
w(lib, "Kill Bill (2003)/Kill Bill (2003)-part1.mkv", vid(60))
w(lib, "Kill Bill (2003)/Kill Bill (2003)-part2.mkv", vid(51))
w(lib, "Kill Bill 2003/Kill Bill 2003.mkv", vid(111, 1280, 720))
w(lib, "Up (2009)/Up (2009).mkv", vid(96))
w(lib, "Up (2009)/Up (2009).avi", vid(96, 720, 480, "mpeg4"))
w(lib, "Up (2009)/Up (2009).nfo")                    # shared by both copies: stays
w(lib, "Unsure (2001)/Unsure (2001).mkv", vid(90))
w(lib, "Unsure 2001/Unsure 2001.mkv", vid(90))       # match not confirmed: not compared

c = web.app.test_client()


def run(url, data):
    r = c.post(url, json=data).get_json()
    while not (j := c.get(f"/api/job/{r['job']}").get_json())["finished"]:
        time.sleep(0.05)
    assert not j["error"], j["error"]
    return j["result"]


items = run("/api/scan", {"folder": str(lib)})["items"]
T = {"Heat": (949, "Heat", 1995), "Aliens": (679, "Aliens", 1986), "Blade": (78, "Blade Runner", 1982),
     "Kill": (24, "Kill Bill: Vol. 1", 2003), "Up": (14160, "Up", 2009), "Unsure": (5, "Unsure", 2001)}
matches = {}
for it in items:
    t, title, year = next(v for k, v in T.items() if it["path"].startswith(k))
    matches[it["id"]] = {"tmdb_id": t, "title": title, "year": year, "status": "review" if title == "Unsure" else "manual"}
core.save_json(core.MATCHES, matches)

d = run("/api/dupes", {"kind": "movies"})
g = {x["title"].split(" (")[0]: x for x in d["groups"]}
ok(sorted(g) == ["Aliens", "Blade Runner", "Heat", "Kill Bill: Vol. 1", "Up"], f"confirmed matches with two copies are listed: {sorted(g)}")
heat = g["Heat"]
ok([cp["suggest"] for cp in heat["copies"]] == [False, True] and heat["copies"][0]["keep"] and not heat["note"]
   and heat["copies"][1]["resolution"] == "720p" and heat["copies"][0]["duration"] == 170 * 60,
   "same length: the 720p copy is ticked, the 1080p kept")
ok(not any(cp["suggest"] for cp in g["Aliens"]["copies"]) and "Special Edition" in g["Aliens"]["note"], f"a labelled version is never ticked: {g['Aliens']['note']}")
ok(not any(cp["suggest"] for cp in g["Blade Runner"]["copies"]) and "Lengths differ" in g["Blade Runner"]["note"], "different lengths: nothing ticked")
kb = g["Kill Bill: Vol. 1"]["copies"]
parted = next(cp for cp in kb if cp["parts"])
ok(len(kb) == 2 and parted["parts"] == 2 and parted["duration"] == 111 * 60 and len(parted["ids"]) == 2,
   "a movie split into parts counts as one copy, its length the parts together")

# Recycling
low = heat["copies"][1]
every_up = [cp["id"] for cp in g["Up"]["copies"]]
r = c.post("/api/dupes/recycle", json={"kind": "movies", "ids": [low["id"]] + every_up}).get_json()
ok(r["recycled"] == [low["paths"][0]] and len(r["failed"]) == 2 and all("one is always kept" in f["error"] for f in r["failed"]),
   "the chosen copy is recycled; choosing every copy of a movie is refused")
gone = sorted(p.name.rsplit(".", 1)[0] for p in binf.iterdir())
ok(gone == ["Heat.1995.720p-poster.jpg", "Heat.1995.720p.en.srt", "Heat.1995.720p.mkv"] and (lib / "Heat (1995)/Heat (1995).mkv").exists(),
   f"its own subtitles and artwork went with it; the kept copy is untouched: {gone}")
ok(len(r["items"]) == len(items) - 1, "the library list comes back updated")
avi = next(cp["id"] for cp in g["Up"]["copies"] if cp["paths"][0].endswith(".avi"))
r = c.post("/api/dupes/recycle", json={"kind": "movies", "ids": [avi]}).get_json()
ok(r["recycled"] == [str(Path("Up (2009)/Up (2009).avi"))] and (lib / "Up (2009)/Up (2009).nfo").exists(),
   "two copies with the same name share their .nfo, so it stays")
r = c.post("/api/dupes/recycle", json={"kind": "movies", "ids": [avi]}).get_json()
ok(not r["recycled"] and "No longer a duplicate" in r["failed"][0]["error"], "a copy that's gone or changed is left alone")

# TV
w(tv, "Show (2001)/Season 1/Show - S01E01 - Pilot.mkv", vid(22))
w(tv, "Show (2001)/Season 1/Show.S01E01.720p.mkv", vid(22, 1280, 720))
w(tv, "Show (2001)/Season 1/Show - S01E02 - Two.mkv", vid(22))
w(tv, "Show (2001)/Season 1/Show - S01E02-E03 - Two and Three.mkv", vid(44))
w(tv, "Show (2001)/Season 1/Show - S01E04 - Four.mkv", vid(22))
run("/api/tv/scan", {"folder": str(tv)})
d = run("/api/dupes", {"kind": "tv"})
t = {x["title"]: x for x in d["groups"]}
ok(sorted(t) == ["Show S01E01", "Show S01E02, S01E03"], f"episodes held twice are listed, overlaps included: {sorted(t)}")
ok(not any(cp["suggest"] for cp in t["Show S01E02, S01E03"]["copies"]) and "different episodes" in t["Show S01E02, S01E03"]["note"],
   "a file holding more episodes than its twin is never ticked")
ok(not any(cp["suggest"] for cp in t["Show S01E01"]["copies"]) and "matched" in t["Show S01E01"]["note"],
   "an unmatched show: nothing ticked, but you can tick a copy yourself")
lo = next(cp["id"] for cp in t["Show S01E01"]["copies"] if cp["resolution"] == "720p")
r = c.post("/api/dupes/recycle", json={"kind": "tv", "ids": [lo]}).get_json()
ok(len(r["recycled"]) == 1 and not (tv / "Show (2001)/Season 1/Show.S01E01.720p.mkv").exists()
   and (tv / "Show (2001)/Season 1/Show - S01E01 - Pilot.mkv").exists() and r["shows"], "an episode copy you tick is recycled")
shutil.rmtree(S, ignore_errors=True)
print("done")
