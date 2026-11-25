"""IMDb, Rotten Tomatoes and Metacritic ratings from OMDb, written into .nfo files."""
import shutil
import sys
import tempfile
import time
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mm import artwork, core, ratings, tmdbapi, web  # noqa: E402

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="mm-test-"))
lib, st = S / "libR", S / "stR"
for d in (lib, st):
    shutil.rmtree(d, ignore_errors=True)
st.mkdir(parents=True)
for name in ("CONFIG", "MATCHES", "RENAME_LOG", "LIBRARY", "EDITS"):
    setattr(core, name, st / f"{name.lower()}.json")
core.save_json(core.CONFIG, {"tmdb_key": "x", "folder": str(lib)})
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)
for name in ("Heat (1995)", "Alien (1979)"):
    (lib / name).mkdir(parents=True)
    (lib / name / f"{name}.mkv").write_bytes(b"x")
# Alien already has tinyMediaManager's ratings
(lib / "Alien (1979)/Alien (1979).nfo").write_text('<?xml version="1.0"?><movie><title>Alien</title><ratings><rating name="imdb" default="true"><value>8.5</value></rating></ratings></movie>', encoding="utf-8")

omdb_calls = []
OMDB = {"Response": "True", "imdbRating": "8.3", "imdbVotes": "712,345",
        "Ratings": [{"Source": "Internet Movie Database", "Value": "8.3/10"}, {"Source": "Rotten Tomatoes", "Value": "88%"}, {"Source": "Metacritic", "Value": "76/100"}]}
ratings.request = lambda params, key=None: (omdb_calls.append(params["i"]), OMDB)[1]
tmdbapi.tmdb = lambda path, **k: {"id": int(path.split("/")[2]), "title": "T", "imdb_id": "tt0113277", "vote_average": 7.9, "vote_count": 7000} if path.startswith("/movie/") else {}
artwork.options = lambda *a: []
c = web.app.test_client()


def job(url, data):
    r = c.post(url, json=data).get_json()
    while not (j := c.get(f"/api/job/{r['job']}").get_json())["finished"]:
        time.sleep(0.05)
    assert not j["error"], j["error"]
    return j["result"]


def nfo(name):
    return ET.fromstring((lib / name / f"{name}.nfo").read_text(encoding="utf-8").split("?>", 1)[1])


items = job("/api/scan", {"folder": str(lib)})["items"]
core.save_json(core.MATCHES, {i["id"]: {"tmdb_id": 949 if "Heat" in i["path"] else 348, "status": "auto", "name": Path(i["path"]).parent.name,
                                        "title": "x", "year": 1} for i in items})

# without a key nothing changes: tinyMediaManager's ratings stay
job("/api/nfo", {"artwork": False, "overwrite": True})
ok(nfo("Alien (1979)").findtext("ratings/rating/value") == "8.5" and not omdb_calls, "without an OMDb key, existing ratings are kept and OMDb isn't asked")

core.save_config(omdb_key="k")
r = job("/api/nfo", {"artwork": False, "overwrite": True})
h = nfo("Heat (1995)")
names = {x.get("name"): (x.findtext("value"), x.findtext("votes"), x.get("default")) for x in h.findall("ratings/rating")}
ok(names.get("imdb") == ("8.3", "712345", "true") and names.get("tomatometerallcritics", (None,))[0] == "88" and names.get("metacritic", (None,))[0] == "76",
   f"IMDb (the default, with votes), Rotten Tomatoes and Metacritic written: {names}")
ok(h.findtext("criticrating") == "88" and h.findtext("rating") == "8.3" and names.get("themoviedb", (None,))[0] == "7.9",
   "Jellyfin's critic rating is the Rotten Tomatoes score; TMDB's rating is kept alongside")
ok(len(nfo("Alien (1979)").findall("ratings")) == 1, "the old ratings block is replaced, not doubled")
calls = len(omdb_calls)
job("/api/nfo", {"artwork": False, "overwrite": True})
ok(len(omdb_calls) == calls, "ratings are remembered for a week (no second lookup)")


def limit(params, key=None):
    raise core.UserError("OMDb's daily limit is used up. It resets at midnight (US time).")


ratings.request = limit
ratings.cache_path().unlink()
r = job("/api/nfo", {"artwork": False, "overwrite": True})
ok(r["nfo"] == 2 and not r["failed"] and "daily limit" in r["ratings_note"], ".nfo files still written when OMDb's limit is used up, with a note why ratings were left out")
shutil.rmtree(S, ignore_errors=True)
print("done")
