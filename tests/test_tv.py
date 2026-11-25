"""TV end to end, with stand-ins for TMDB, YouTube, OpenSubtitles and the Recycle Bin."""
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mm import artwork, core, subtitles, trailers, tvart, tvmeta, web  # noqa: E402

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="mm-test-"))
tv, st, binf = S / "tvT", S / "stT", S / "binT"
for d in (tv, st, binf):
    shutil.rmtree(d, ignore_errors=True)
st.mkdir()
binf.mkdir()
for name in ("CONFIG", "MATCHES", "RENAME_LOG", "LIBRARY", "EDITS", "TV_LIBRARY", "TV_MATCHES", "TV_CACHE", "TV_RENAME_LOG"):
    setattr(core, name, st / f"{name.lower()}.json")
core.save_json(core.CONFIG, {"tmdb_key": "x", "os_key": "y", "tv_folder": str(tv), "language": "en-US"})
fake = types.ModuleType("send2trash")
fake.send2trash = lambda p: shutil.move(p, binf / (Path(p).name + f".{time.time_ns()}"))
sys.modules["send2trash"] = fake
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)


def w(rel, data=b"x"):
    p = tv / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if isinstance(data, bytes) else data.encode())
    return p


def show_nfo(tmdb_id):
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<!--created by tinyMediaManager-->\n<tvshow><title>X</title><uniqueid type="tmdb">{tmdb_id}</uniqueid><userrating>7</userrating><watched>true</watched></tvshow>'


def ep_nfo(title, watched=True):
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<episodedetails><title>{title}</title><season>1</season><episode>1</episode><watched>{str(watched).lower()}</watched><playcount>3</playcount></episodedetails>'


# Seinfeld: tidy, plus a two-episode file and an extras folder
w("Seinfeld (1989)/tvshow.nfo", show_nfo(1400)); w("Seinfeld (1989)/poster.jpg"); w("Seinfeld (1989)/season01-poster.jpg")
w("Seinfeld (1989)/Season 1/Seinfeld - S01E01 - The Seinfeld Chronicles.mkv", b"s1")
w("Seinfeld (1989)/Season 1/Seinfeld - S01E01 - The Seinfeld Chronicles.nfo", ep_nfo("The Seinfeld Chronicles"))
w("Seinfeld (1989)/Season 1/Seinfeld - S01E02 - The Stake Out.mkv", b"s2")
w("Seinfeld (1989)/Season 1/Seinfeld - S01E02 - The Stake Out.nfo", ep_nfo("A TVDB title for another episode"))
w("Seinfeld (1989)/Season 7/Seinfeld - S07E14 S07E15 - The Cadillac (Part 1) - The Cadillac (Part 2).mkv", b"cad")
w("Seinfeld (1989)/Season 7/Seinfeld - S07E14 S07E15 - The Cadillac (Part 1) - The Cadillac (Part 2).en.srt", b"sub")
w("Seinfeld (1989)/Specials/Seinfeld - S00E01 - Notes about Nothing.mkv", b"sp")
w("Seinfeld (1989)/Seinfeld Xtras/Bloopers.mkv", b"x")
# Loki: no year on the folder, seasons buried in odd folders
w("Loki/tvshow.nfo", show_nfo(84958))
w("Loki/Loki (2021)/Season 1/Loki.S01E01.1080p.mkv", b"l1")
w("Loki/Loki (2021) Season 2 S02 (1080p DSNP WEB-DL)/Season 2/Loki.S02E01.1080p.mkv", b"l2")
# Maul: only the year is missing; its episode is otherwise named right
w("Maul/tvshow.nfo", show_nfo(289219)); w("Maul/Season 1/Maul - S01E01 - Chapter One.mkv", b"m1")
# Aqua Teen: the misleading name
w("Aqua Teen Hunger Force (2000)/tvshow.nfo", show_nfo(251))
w("Aqua Teen Hunger Force (2000)/Season 1/Aqua Teen Hunger Force S01 S11 2000 + Movie - S01E02 - Carl's Pissed Boston Freakin' Won.mp4", b"a2")

SHOWS = {1400: ("Seinfeld", "1989-07-05", {1: ["The Seinfeld Chronicles", "The Stake Out", "The Robbery"], 7: {14: "The Cadillac (1)", 15: "The Cadillac (2)"}, 0: ["Notes about Nothing"]}),
         84958: ("Loki", "2021-06-09", {1: ["Glorious Purpose"], 2: ["Ouroboros", "Breaking Brad"]}),
         289219: ("Maul", "2025-03-01", {1: ["Chapter One", "Chapter Two"]}),
         251: ("Aqua Teen Hunger Force", "2000-12-30", {1: ["Rabbot", "Carl's Pissed Boston Freakin' Won"]})}
calls = []


def fake_tmdb(path, key=None, **params):
    calls.append(path)
    parts = path.strip("/").split("/")
    if path == "/configuration":
        return {}
    if parts[0] == "tv" and len(parts) == 2:
        name, first, seasons = SHOWS[int(parts[1])]
        return {"id": int(parts[1]), "name": name, "original_name": name, "first_air_date": first, "overview": f"{name} plot",
                "genres": [{"name": "Comedy"}], "networks": [{"name": "NBC"}], "status": "Ended", "episode_run_time": [22],
                "external_ids": {"tvdb_id": 1, "imdb_id": "tt1"}, "content_ratings": {"results": [{"iso_3166_1": "US", "rating": "TV-PG"}]},
                "aggregate_credits": {"cast": [{"name": "Jerry", "roles": [{"character": "Himself"}]}]},
                "videos": {"results": [{"site": "YouTube", "type": "Trailer", "key": "yt1", "official": True}]},
                "seasons": [{"season_number": n, "name": "Specials" if n == 0 else f"Season {n}", "episode_count": len(e)} for n, e in seasons.items()],
                "poster_path": "/p.jpg"}
    if parts[0] == "tv" and parts[2] == "season" and len(parts) == 4:
        eps = SHOWS[int(parts[1])][2][int(parts[3])]
        items = eps.items() if isinstance(eps, dict) else enumerate(eps, 1)
        return {"episodes": [{"episode_number": n, "name": t, "id": n * 100, "air_date": "2000-01-01" if t != "Breaking Brad" else "2099-01-01",
                              "overview": f"{t} plot", "still_path": f"/still{n}.jpg", "crew": []} for n, t in items]}
    if path.endswith("/images"):
        return {"posters": [{"file_path": "/poster.jpg", "iso_639_1": "en", "width": 1000, "height": 1500}],
                "backdrops": [{"file_path": "/back.jpg", "iso_639_1": None, "width": 1920, "height": 1080}],
                "logos": [{"file_path": "/logo.svg", "iso_639_1": "en", "width": 800, "height": 300}]}
    raise AssertionError("unexpected TMDB call " + path)


tvmeta.tmdb = fake_tmdb
tvart.tmdb = fake_tmdb
artwork.download_to = lambda url, dst: dst.write_bytes(b"image" * 30)
c = web.app.test_client()


def job(url, data):
    r = c.post(url, json=data).get_json()
    assert "job" in r, r
    while not (j := c.get(f"/api/job/{r['job']}").get_json())["finished"]:
        time.sleep(0.05)
    assert not j["error"], j["error"]
    return j["result"]


def tree():
    return sorted(str(p.relative_to(tv)).replace("\\", "/") for p in tv.rglob("*") if p.is_file())


# 1. scan and match
r = job("/api/tv/scan", {"folder": str(tv)})
shows = {s["folder"]: s for s in r["shows"]}
ok(len(shows) == 4 and shows["Seinfeld (1989)"]["files"] == 4 and shows["Seinfeld (1989)"]["extras"] == 1, "4 shows; Seinfeld has 4 episode files and 1 extra")
r = job("/api/tv/match-all", {})
ok(r["counts"]["nfo"] == 4 and not any(p.startswith("/search") for p in calls), f"all 4 shows matched from tvshow.nfo, no name searches ({r['counts']})")
shows = {s["folder"]: s for s in r["shows"]}
ok(shows["Seinfeld (1989)"]["episodes"] == 5 and shows["Seinfeld (1989)"]["missing"] == 1, "Seinfeld: 5 episodes on disk (one file holds two), 1 aired episode missing (The Robbery)")
ok(shows["Loki"]["missing"] == 0, "Loki: an episode that hasn't aired yet doesn't count as missing")
ok(shows["Seinfeld (1989)"]["to_rename"] == 1 and shows["Maul"]["to_rename"] == 1 and shows["Loki"]["to_rename"] == 2, "files needing renaming: Seinfeld 1 (the two-episode file), Maul 1, Loki 2")

# 2. rename preview
pv = c.get("/api/tv/rename/preview").get_json()
plans = {p["name"]: p for p in pv["plans"]}
ok("Seinfeld (1989)" in plans and len(plans["Seinfeld (1989)"]["changes"]) == 2, f"Seinfeld: only the two-episode file and its subtitle change: {[ch['to'] for ch in plans.get('Seinfeld (1989)', {}).get('changes', [])]}")
ok(any(ch["to"].endswith("Seinfeld - S07E14-E15 - The Cadillac (1) & The Cadillac (2).mkv") for ch in plans["Seinfeld (1989)"]["changes"]), "two-episode file named S07E14-E15 for Jellyfin")
maul = plans["Maul (2025)"]["changes"]
ok(len(maul) == 1 and maul[0]["kind"] == "folder", "Maul: just the folder gets its year; the episode inside is already right")
ok(any(ch["to"].replace("\\", "/") == "Loki (2021)/Season 2/Loki - S02E01 - Ouroboros.mkv" for ch in plans["Loki (2021)"]["changes"]), "Loki: buried episodes go to Loki (2021)/Season N with TMDB titles")
ok("Aqua Teen Hunger Force (2000)" in plans and plans["Aqua Teen Hunger Force (2000)"]["changes"][0]["to"].endswith("Aqua Teen Hunger Force - S01E02 - Carl's Pissed Boston Freakin' Won.mp4"), "Aqua Teen: the misleading name is fixed")
ok(not any("Xtras" in ch["from"] for p in pv["plans"] for ch in p["changes"]), "extras are left alone")
r = job("/api/tv/rename/apply", {"ids": [p["id"] for p in pv["plans"]]})
t = tree()
ok("Loki (2021)/Season 1/Loki - S01E01 - Glorious Purpose.mkv" in t and not any(x.startswith("Loki/") for x in t)
   and not (tv / "Loki (2021)" / "Loki (2021)").exists(), "Loki rearranged and the empty old folders removed")
ok("Maul (2025)/Season 1/Maul - S01E01 - Chapter One.mkv" in t and "Seinfeld (1989)/Season 7/Seinfeld - S07E14-E15 - The Cadillac (1) & The Cadillac (2).en.srt" in t, "Maul renamed; the subtitle moved with its episode")
ok(all(s["named"] for s in r["shows"]) and all(s["match"] for s in r["shows"]), "everything named, and every show still matched after its folder was renamed")
u = c.post("/api/tv/rename/undo").get_json()
ok(u["failed"] == 0 and (tv / "Loki/Loki (2021)/Season 1/Loki.S01E01.1080p.mkv").exists(), f"undo put {u['undone']} moves back")
job("/api/tv/rename/apply", {"ids": [p["id"] for p in c.get("/api/tv/rename/preview").get_json()["plans"]]})

# 3. .nfo files
r = job("/api/tv/nfo", {"overwrite": False})
ok(r["episodes"] >= 4 and r["seasons"] >= 3 and r["show_nfos"] == 0 and len(r["shows"]) == 4, f"created missing ones: {r['episodes']} episode, {r['seasons']} season .nfo files; existing tvshow.nfo files left alone")
multi = (tv / "Seinfeld (1989)/Season 7/Seinfeld - S07E14-E15 - The Cadillac (1) & The Cadillac (2).nfo").read_text(encoding="utf-8")
ok(multi.count("<episodedetails>") == 2, "the two-episode file's .nfo has two entries")
r = job("/api/tv/nfo", {"overwrite": True})
e1 = ET.fromstring((tv / "Seinfeld (1989)/Season 1/Seinfeld - S01E01 - The Seinfeld Chronicles.nfo").read_text(encoding="utf-8").split("?>", 1)[1])
ok(e1.findtext("plot") == "The Seinfeld Chronicles plot" and e1.findtext("watched") == "true" and e1.findtext("playcount") == "3", "update: fresh plot, watched status and play count kept")
e2 = (tv / "Seinfeld (1989)/Season 1/Seinfeld - S01E02 - The Stake Out.nfo").read_text(encoding="utf-8")
ok("A TVDB title for another episode" in e2 and r["kept"] >= 1, "an episode .nfo whose title disagrees with TMDB is left alone")
show = ET.fromstring((tv / "Seinfeld (1989)/tvshow.nfo").read_text(encoding="utf-8").split("?>", 1)[1])
ok(show.findtext("plot") == "Seinfeld plot" and show.findtext("watched") == "true" and show.findtext("userrating") == "7", "tvshow.nfo updated, its extras kept")

# 4. artwork
r = job("/api/tv/artwork", {"kinds": ["poster", "logo", "season", "thumb"]})
t = tree()
ok("Seinfeld (1989)/clearlogo.png" in t and "Loki (2021)/poster.jpg" in t and "Loki (2021)/season02-poster.jpg" in t and "Seinfeld (1989)/season-specials-poster.jpg" in t,
   "show posters, logos and season posters (including specials) with Jellyfin's names")
ok("Seinfeld (1989)/Season 1/Seinfeld - S01E01 - The Seinfeld Chronicles-thumb.jpg" in t and r["had"] >= 2, f"episode thumbnails added; existing poster/season poster kept ({r['by_kind']})")

# 5. subtitles and trailers
subtitles.os_request = lambda m, p, params=None, data=None, key=None, host=None: (
    {"data": [{"attributes": {"language": "en", "files": [{"file_id": params["episode_number"]}], "moviehash_match": True}}]} if p == "/subtitles" else {})
subtitles.fetch_subtitle = lambda fid: (b"1\n00:00:01,000 --> 00:00:02,000\nhi\n", ".srt", 90)
r = job("/api/tv/subtitles", {"languages": "en"})
ok(r["downloaded"] == 7 and r["had"] == 1 and (tv / "Loki (2021)/Season 2/Loki - S02E01 - Ouroboros.en.srt").exists(), f"subtitles: {r['downloaded']} downloaded, {r['had']} already there")
trailers.download_trailer = lambda k, base, q, hook: (base.parent.mkdir(parents=True, exist_ok=True), (base.parent / (base.name + ".mkv")).write_bytes(b"t"), base.parent / (base.name + ".mkv"))[2]
trailers.tag_trailer = lambda *a: None
r = job("/api/tv/trailers", {"quality": 480})
ok(r["downloaded"] == 4 and (tv / "Maul (2025)/trailers/Maul (2025) - Trailer.mkv").exists(), "a trailer per show, in its trailers folder")

# 6. the show window, edits, deleting
key = next(s["key"] for s in r["shows"] if s["folder"] == "Seinfeld (1989)")
d = c.get("/api/tv/show", query_string={"key": key}).get_json()
s1 = next(s for s in d["seasons"] if s["season"] == 1)
ok([e.get("missing", False) for e in s1["episodes"]] == [False, False, True] and s1["missing"] == 1, "show window: season 1 lists The Robbery as missing")
ok(next(s for s in d["seasons"] if s["season"] == 7)["episodes"][1].get("same_file"), "show window: episode 15 shown as part of the two-episode file")
img = c.get(next(s["poster"] for s in d["seasons"] if s["season"] == 1))
ok(img.status_code == 200, "season poster served from the show folder")
d = c.post("/api/tv/edit", json={"key": key, "fields": {"title": "Seinfeld Edited", "genres": "Sitcom"}}).get_json()
show = (tv / "Seinfeld (1989)/tvshow.nfo").read_text(encoding="utf-8")
ok("<title>Seinfeld Edited</title>" in show and "<lockdata>true</lockdata>" in show and d["info"]["title"] == "Seinfeld Edited", "show edits saved into tvshow.nfo, locked for Jellyfin")
pv = c.get("/api/tv/rename/preview").get_json()
ok(any(p["name"] == "Seinfeld Edited (1989)" for p in pv["plans"]), "the renamer follows the edited title")
c.post("/api/tv/edit", json={"key": key, "reset": True})
ep_id = next(e["id"] for e in s1["episodes"] if e.get("file", "").startswith("Seinfeld - S01E02"))
pv = c.post("/api/tv/delete/preview", json={"key": key, "what": "episode", "id": ep_id}).get_json()
ok(pv["count"] >= 3, f"deleting an episode includes its subtitle, .nfo and thumbnail ({pv['count']} files)")
r = c.post("/api/tv/delete", json={"key": key, "what": "episode", "id": ep_id}).get_json()
ok(not (tv / "Seinfeld (1989)/Season 1/Seinfeld - S01E02 - The Stake Out.mkv").exists() and next(s for s in r["shows"] if s["folder"] == "Seinfeld (1989)")["missing"] == 2,
   "episode in the (fake) Recycle Bin; now 2 missing")
r = c.post("/api/tv/delete", json={"key": key, "what": "season", "season": 7}).get_json()
ok(not (tv / "Seinfeld (1989)/Season 7").exists(), "deleting a season removes its folder")

for d in (tv, st, binf):
    shutil.rmtree(d, ignore_errors=True)
print("done")
