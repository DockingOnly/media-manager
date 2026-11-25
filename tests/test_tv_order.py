"""TV episode orders: TVmaze, TMDB's alternative orders, a different order for some seasons, absolute (anime)."""
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mm import core, tvmeta, web  # noqa: E402

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="mm-test-"))
tv, st = S / "tvO", S / "stO"
for d in (tv, st):
    shutil.rmtree(d, ignore_errors=True)
st.mkdir(parents=True)
for name in ("CONFIG", "MATCHES", "RENAME_LOG", "LIBRARY", "EDITS", "TV_LIBRARY", "TV_MATCHES", "TV_CACHE", "TV_RENAME_LOG"):
    setattr(core, name, st / f"{name.lower()}.json")
core.save_json(core.CONFIG, {"tmdb_key": "x", "tv_folder": str(tv)})
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)


def w(rel, data=b"x"):
    p = tv / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def show_nfo(i):
    return f'<?xml version="1.0"?><tvshow><title>X</title><uniqueid type="tmdb">{i}</uniqueid></tvshow>'.encode()


# "Crypt": season 1 follows TMDB; season 2 follows TVDB/TVmaze (TMDB has 2x01 and 2x02 the other way round)
w("Crypt (1989)/tvshow.nfo", show_nfo(10))
w("Crypt (1989)/Season 1/Crypt - S01E01 - Alpha.mkv"); w("Crypt (1989)/Season 1/Crypt - S01E02 - Beta.mkv")
w("Crypt (1989)/Season 2/Crypt - S02E01 - Delta.mkv"); w("Crypt (1989)/Season 2/Crypt - S02E02 - Gamma.mkv")
# "Anime": absolute numbering, no seasons in the names
w("Anime (2010)/tvshow.nfo", show_nfo(20))
for n, t in [(1, "One"), (2, "Two"), (3, "Three"), (4, "Four")]:
    w(f"Anime (2010)/Anime - {n:03d}.mkv")

SHOWS = {10: ("Crypt", {1: ["Alpha", "Beta"], 2: ["Gamma", "Delta"]}), 20: ("Anime", {1: ["One", "Two"], 2: ["Three", "Four"]})}


def fake_tmdb(path, key=None, **params):
    parts = path.strip("/").split("/")
    if path == "/configuration":
        return {}
    if parts[:2] == ["tv", "episode_group"]:  # Anime's absolute order: one group, episodes 1-4
        return {"groups": [{"name": "Absolute", "order": 1, "episodes": [
            {"order": i, "season_number": s, "episode_number": e, "name": t}
            for i, (s, e, t) in enumerate([(1, 1, "One"), (1, 2, "Two"), (2, 1, "Three"), (2, 2, "Four")])]}]}
    if parts[0] == "tv" and len(parts) == 3 and parts[2] == "episode_groups":
        return {"results": [{"id": "absgroup", "type": 2, "name": "Absolute", "group_count": 1, "episode_count": 4}] if parts[1] == "20" else []}
    if parts[0] == "tv" and len(parts) == 2:
        name, seasons = SHOWS[int(parts[1])]
        return {"id": int(parts[1]), "name": name, "first_air_date": "1989-01-01" if name == "Crypt" else "2010-01-01",
                "external_ids": {"tvdb_id": 99}, "seasons": [{"season_number": n, "episode_count": len(e)} for n, e in seasons.items()]}
    if parts[0] == "tv" and parts[2] == "season":
        sid, season = int(parts[1]), int(parts[3])
        return {"episodes": [{"episode_number": i, "name": t, "air_date": f"19{89 + season}-0{i}-01", "id": sid * 100 + season * 10 + i}
                             for i, t in enumerate(SHOWS[sid][1][season], 1)]}
    raise AssertionError(path)


tvmeta.tmdb = fake_tmdb
# TVmaze has both seasons the other way round from TMDB: the files follow TMDB for season 1, TVmaze for season 2
TVMAZE_EPS = [{"season": 1, "number": 1, "name": "Beta", "airdate": "1990-02-01"}, {"season": 1, "number": 2, "name": "Alpha", "airdate": "1990-01-01"},
              {"season": 2, "number": 1, "name": "Delta", "airdate": "1991-02-01"}, {"season": 2, "number": 2, "name": "Gamma", "airdate": "1991-01-01"}]
tvmeta._get_json = lambda url: {"id": 5} if "lookup" in url else TVMAZE_EPS
c = web.app.test_client()


def job(url, data):
    r = c.post(url, json=data).get_json()
    while not (j := c.get(f"/api/job/{r['job']}").get_json())["finished"]:
        time.sleep(0.05)
    assert not j["error"], j["error"]
    return j["result"]


job("/api/tv/scan", {"folder": str(tv)})
r = job("/api/tv/match-all", {})
shows = {s["folder"]: s for s in r["shows"]}
crypt, anime = shows["Crypt (1989)"]["key"], shows["Anime (2010)"]["key"]
ok(shows["Crypt (1989)"]["mismatch"] == 2, "TMDB order: Crypt's season 2 files disagree with TMDB, so they're protected")

fit = c.get("/api/tv/orders", query_string={"key": crypt}).get_json()
ok(fit["mix"] and fit["mix"]["season_orders"] == {"2": "tvmaze"} and fit["mix"]["agree"] == 4,
   f"best fit: TMDB, except season 2 from TVmaze ({fit['mix'] and fit['mix']['description']})")
d = c.post("/api/tv/order", json={"key": crypt, "order": None, "season_orders": fit["mix"]["season_orders"]}).get_json()
s2 = next(s for s in d["seasons"] if s["season"] == 2)
ok([e["name"] for e in s2["episodes"]] == ["Delta", "Gamma"] and not any(e.get("mismatch") for e in s2["episodes"]), "season 2 now follows TVmaze: titles agree, nothing protected")
lib = c.get("/api/tv/library").get_json()
ok(next(s for s in lib["shows"] if s["key"] == crypt)["mismatch"] == 0, "the library table no longer flags Crypt")
data = tvmeta.cached_for(core.load_json(core.TV_MATCHES, {})[crypt])
ok(tvmeta.tmdb_numbers(data, 2, 1) == (2, 2), "subtitle searches translate TVmaze's 2x01 back to TMDB's 2x02 (by air date)")
ok(c.post("/api/tv/order", json={"key": crypt, "order": "group:nope"}).status_code == 400, "an order the show doesn't have is refused")

# anime: absolute numbers line up once the absolute order is chosen
fit = c.get("/api/tv/orders", query_string={"key": anime}).get_json()
absolute = next(o for o in fit["orders"] if o["id"] == "group:absgroup")
ok(absolute.get("best") or (fit["mix"] is None and absolute["agree"] >= 0), f"anime: the absolute order is offered ({absolute['name']})")
d = c.post("/api/tv/order", json={"key": anime, "order": "group:absgroup"}).get_json()
s1 = next(s for s in d["seasons"] if s["season"] == 1)
ok([(e["episode"], e["name"], bool(e.get("file"))) for e in s1["episodes"]] == [(1, "One", True), (2, "Two", True), (3, "Three", True), (4, "Four", True)],
   "anime: files 001-004 line up with absolute episodes 1-4, none missing")
pv = c.get("/api/tv/rename/preview").get_json()
plan = next((p for p in pv["plans"] if p["name"].startswith("Anime")), None)
ok(plan and any(ch["to"].endswith("Anime - S01E03 - Three.mkv") for ch in plan["changes"]), "anime: renamed with titles from the absolute order")

shutil.rmtree(S, ignore_errors=True)
print("done")
