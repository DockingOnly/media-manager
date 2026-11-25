"""Trakt: device login, then watched status (watched, play count, last watched) into movie and episode .nfo files."""
import shutil
import sys
import tempfile
import time
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mm import core, nfo as nfomod, trakt, tvmeta, web  # noqa: E402

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="mm-test-"))
lib, tv, st = S / "libK", S / "tvK", S / "stK"
for d in (lib, tv, st):
    shutil.rmtree(d, ignore_errors=True)
st.mkdir(parents=True)
for name in ("CONFIG", "MATCHES", "RENAME_LOG", "LIBRARY", "EDITS", "TV_LIBRARY", "TV_MATCHES", "TV_CACHE", "TV_RENAME_LOG"):
    setattr(core, name, st / f"{name.lower()}.json")
core.save_json(core.CONFIG, {"tmdb_key": "x", "folder": str(lib), "tv_folder": str(tv)})
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)


def w(p, data=b"x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if isinstance(data, bytes) else data.encode())


for name in ("Heat (1995)", "Alien (1979)"):
    w(lib / name / f"{name}.mkv")
    w(lib / name / f"{name}.nfo", f'<?xml version="1.0"?><movie><title>{name}</title><watched>true</watched><playcount>1</playcount></movie>')
w(tv / "Show (2001)/tvshow.nfo", '<?xml version="1.0"?><tvshow><title>Show</title><uniqueid type="tmdb">10</uniqueid></tvshow>')
w(tv / "Show (2001)/Season 1/Show - S01E01 - One.mkv")
w(tv / "Show (2001)/Season 1/Show - S01E02-E03 - Two.mkv")
w(tv / "Show (2001)/Season 1/Show - S01E01 - One.nfo", '<?xml version="1.0"?><episodedetails><title>One</title></episodedetails>')
w(tv / "Show (2001)/Season 1/Show - S01E02-E03 - Two.nfo",
  '<?xml version="1.0"?><episodedetails><title>Two</title></episodedetails>\n<episodedetails><title>Three</title></episodedetails>')


def fake_tmdb(path, key=None, **params):
    parts = path.strip("/").split("/")
    if path == "/configuration" or parts[-1] == "episode_groups":
        return {"results": []} if parts[-1] == "episode_groups" else {}
    if len(parts) == 2:
        return {"id": 10, "name": "Show", "first_air_date": "2001-01-01", "external_ids": {}, "seasons": [{"season_number": 1, "episode_count": 3}]}
    return {"episodes": [{"episode_number": i, "name": t, "air_date": f"2001-01-0{i}", "id": i} for i, t in enumerate(["One", "Two", "Three"], 1)]}


tvmeta.tmdb = fake_tmdb
tvmeta._get_json = lambda url: None
c = web.app.test_client()


def job(url, data):
    r = c.post(url, json=data).get_json()
    assert "job" in r, r
    while not (j := c.get(f"/api/job/{r['job']}").get_json())["finished"]:
        time.sleep(0.05)
    assert not j["error"], j["error"]
    return j["result"]


# ---- connecting
r = c.post("/api/trakt/connect", json={})
ok(r.status_code == 400 and "Client ID" in r.get_json()["error"], "without a Trakt app's ID and Secret, Connect says what to add")
r = c.post("/api/settings", json={"trakt_id": "abcd1234", "trakt_secret": "s3cret"}).get_json()
ok(r.get("trakt_id_hint") == "1234" and r.get("has_trakt_secret") and not r.get("trakt_connected"), "ID and Secret saved (only a hint shown back)")

polls = []


def fake_call(method, path, data=None, auth=True):
    if path == "/oauth/device/code":
        return 200, {"device_code": "dev", "user_code": "ABCD1234", "verification_url": "https://trakt.tv/activate", "expires_in": 600, "interval": 5}
    if path == "/oauth/device/token":
        polls.append(data)
        return (400, None) if len(polls) == 1 else (200, {"access_token": "tok", "refresh_token": "ref", "created_at": time.time(), "expires_in": 7776000})
    if path == "/sync/watched/movies":
        return 200, [{"plays": 3, "last_watched_at": "2024-05-01T20:15:00.000Z", "movie": {"ids": {"tmdb": 949}}}]
    if path == "/sync/watched/shows":
        return 200, [{"show": {"ids": {"tmdb": 10}}, "seasons": [{"number": 1, "episodes": [
            {"number": 1, "plays": 1, "last_watched_at": "2024-06-01T10:00:00.000Z"},
            {"number": 3, "plays": 2, "last_watched_at": "2024-06-02T10:00:00.000Z"}]}]}]
    raise AssertionError(path)


trakt.call = fake_call
d = c.post("/api/trakt/connect", json={}).get_json()
ok(d["code"] == "ABCD1234" and "trakt.tv/activate" in d["url"], "Connect shows the code to enter at trakt.tv/activate")
ok(c.get("/api/trakt/connect").get_json()["state"] == "waiting", "waiting until the code is entered")
ok(c.get("/api/trakt/connect").get_json()["state"] == "connected" and polls[-1]["client_secret"] == "s3cret", "then connected")
ok(c.get("/api/config").get_json()["trakt_connected"], "Settings shows Trakt as connected")

# ---- movies
items = job("/api/scan", {"folder": str(lib)})["items"]
core.save_json(core.MATCHES, {i["id"]: {"tmdb_id": 949 if "Heat" in i["path"] else 348, "status": "auto", "name": Path(i["path"]).parent.name,
                                        "title": "x", "year": 1} for i in items})


def nfo(p):
    return ET.fromstring(p.read_text(encoding="utf-8").split("?>", 1)[1])


r = job("/api/trakt/sync", {"kind": "movies", "clear": False})
heat, alien = nfo(lib / "Heat (1995)/Heat (1995).nfo"), nfo(lib / "Alien (1979)/Alien (1979).nfo")
ok((heat.findtext("watched"), heat.findtext("playcount"), heat.findtext("lastplayed")) == ("true", "3", "2024-05-01 20:15:00"),
   "Heat: watched, played 3 times, last watched date in Kodi's format")
ok(heat.findtext("title") == "Heat (1995)", "the rest of the .nfo is kept")
ok(alien.findtext("watched") == "true" and r["updated"] == 1 and r["watched"] == 1, "without 'clear', a movie Trakt doesn't list keeps its watched mark")
r = job("/api/trakt/sync", {"kind": "movies", "clear": True})
alien = nfo(lib / "Alien (1979)/Alien (1979).nfo")
ok((alien.findtext("watched"), alien.findtext("playcount")) == ("false", "0") and r["updated"] == 1, "with 'clear', it's marked unwatched (Heat unchanged)")

# ---- TV
job("/api/tv/scan", {"folder": str(tv)})
job("/api/tv/match-all", {})
r = job("/api/trakt/sync", {"kind": "tv", "clear": False})
one = nfo(tv / "Show (2001)/Season 1/Show - S01E01 - One.nfo")
raw = (tv / "Show (2001)/Season 1/Show - S01E02-E03 - Two.nfo").read_bytes()
both = nfomod.parse_all(raw)
ok(one.findtext("watched") == "true" and one.findtext("lastplayed") == "2024-06-01 10:00:00", "episode 1 marked watched")
ok(len(both) == 2 and both[0].find("watched") is None and both[1].findtext("playcount") == "2",
   "two-episode file: only the watched episode's entry (E03) is marked")
ok(r["watched"] == 2 and r["updated"] == 2 and "shows" in r, f"counts: {r['watched']} watched, {r['updated']} files updated")

# ---- disconnect
d = c.post("/api/trakt/disconnect", json={}).get_json()
ok(not d["trakt_connected"] and not trakt.token_path().exists(), "Disconnect forgets the Trakt login")
ok(c.post("/api/trakt/sync", json={"kind": "movies"}).status_code == 400, "syncing while disconnected is refused with a message")
shutil.rmtree(S, ignore_errors=True)
print("done")
