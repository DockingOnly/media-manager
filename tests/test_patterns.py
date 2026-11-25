"""Rename patterns: checking them, filling them in, and both renamers following them."""
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mm import core, tvmeta, web  # noqa: E402

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="mm-test-"))
lib, tv, st = S / "libP", S / "tvP", S / "stP"
for d in (lib, tv, st):
    shutil.rmtree(d, ignore_errors=True)
st.mkdir(parents=True)
for name in ("CONFIG", "MATCHES", "RENAME_LOG", "LIBRARY", "EDITS", "TV_LIBRARY", "TV_MATCHES", "TV_CACHE", "TV_RENAME_LOG"):
    setattr(core, name, st / f"{name.lower()}.json")
core.save_json(core.CONFIG, {"tmdb_key": "x", "folder": str(lib), "tv_folder": str(tv)})
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)


def refused(name, text, needle):
    try:
        core.check_pattern(name, text)
        return False
    except core.UserError as err:
        return needle in str(err)


# 1. checking and filling in
ok(refused("movie_file", "{year}", "{title}") and refused("tv_episode_file", "{show} - {episode_title}", "{code}")
   and refused("movie_folder", "{title} {foo}", "{foo}") and refused("movie_folder", "{title}: {year}", "can't contain"),
   "patterns without {title} or {code}, with unknown placeholders, or with forbidden characters are refused")
ok(core.render("{title} ({year})", {"title": "Heat", "year": None}) == "Heat", "an empty year takes its brackets with it")
ok(core.render("{show} - {code} - {episode_title}", {"show": "Loki", "code": "S01E01", "episode_title": ""}) == "Loki - S01E01",
   "an empty episode title takes its dash with it")
ok(core.render("Season {season:02}", {"season": 3}) == "Season 03" and core.render("{title}", {"title": "Mission: Impossible"}) == "Mission - Impossible",
   "leading zeros, and names made safe for Windows")

# 2. both renamers follow custom patterns
core.save_config(patterns={"movie_folder": "{title} [{year}]", "movie_file": "{title} ({year}) {resolution}",
                           "tv_show_folder": "{title}", "tv_season_folder": "S{season:02}", "tv_episode_file": "{show}.{code}"})
lib.mkdir(parents=True)
(lib / "heat.1995.1080p.mkv").write_bytes(b"x")
(tv / "Loki").mkdir(parents=True)
(tv / "Loki/Season 1").mkdir()
(tv / "Loki/Season 1/Loki.S01E01.mkv").write_bytes(b"x")
tvmeta.tmdb = lambda path, key=None, **p: (
    {"id": 1, "name": "Loki", "first_air_date": "2021-06-09", "seasons": [{"season_number": 1, "episode_count": 1}]} if path == "/tv/1" else
    {"episodes": [{"episode_number": 1, "name": "Glorious Purpose", "air_date": "2021-06-09"}]} if path == "/tv/1/season/1" else {})
c = web.app.test_client()


def job(url, data):
    r = c.post(url, json=data).get_json()
    while not (j := c.get(f"/api/job/{r['job']}").get_json())["finished"]:
        time.sleep(0.05)
    assert not j["error"], j["error"]
    return j["result"]


items = job("/api/scan", {"folder": str(lib)})["items"]
core.save_json(core.MATCHES, {items[0]["id"]: {"tmdb_id": 949, "status": "manual", "name": "Heat (1995)", "title": "Heat", "year": 1995}})
pv = c.get("/api/rename/preview").get_json()
ok(any(ch["to"] == "Heat [1995]\\Heat (1995) 1080p.mkv" for ch in pv["plans"][0]["changes"]), f"movie renamed by the pattern: {[ch['to'] for ch in pv['plans'][0]['changes']]}")
r = job("/api/rename/apply", {"ids": [pv["plans"][0]["id"]]})
ok(r["items"][0]["named"], "afterwards it counts as named (the Jellyfin column agrees with the renamer)")

shows = job("/api/tv/scan", {"folder": str(tv)})["shows"]
c.post("/api/tv/match", json={"key": shows[0]["key"], "tmdb_id": 1})
pv = c.get("/api/tv/rename/preview").get_json()
to = [ch["to"].replace("\\", "/") for ch in pv["plans"][0]["changes"]]
ok("Loki/S01/Loki.S01E01.mkv" in to, f"episode renamed by the patterns: {to}")
r = job("/api/tv/rename/apply", {"ids": [pv["plans"][0]["id"]]})
ok(r["shows"][0]["named"] and (tv / "Loki/S01/Loki.S01E01.mkv").exists(), "afterwards the show counts as named")

ok(c.post("/api/settings", json={"patterns": {"tv_episode_file": "{show} - {episode_title}"}}).status_code == 400, "Settings refuses an episode pattern without {code}")
shutil.rmtree(S, ignore_errors=True)
print("done")
