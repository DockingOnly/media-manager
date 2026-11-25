"""Collections: found from .nfo files or TMDB, with the films you have and the ones you're missing."""
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mm import core, nfo, tmdbapi, web  # noqa: E402

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="mm-test-"))
lib, st = S / "libC", S / "stC"
for d in (lib, st):
    shutil.rmtree(d, ignore_errors=True)
st.mkdir(parents=True)
for name in ("CONFIG", "MATCHES", "RENAME_LOG", "LIBRARY", "EDITS", "MOVIE_CACHE", "COLLECTIONS"):
    setattr(core, name, st / f"{name.lower()}.json")
core.save_json(core.CONFIG, {"tmdb_key": "x", "folder": str(lib)})
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)


def movie(folder, nfo_xml=None):
    d = lib / folder
    d.mkdir(parents=True)
    (d / f"{folder}.mkv").write_bytes(b"x")
    if nfo_xml:
        (d / f"{folder}.nfo").write_text(f'<?xml version="1.0"?><movie><title>{folder}</title>{nfo_xml}</movie>', encoding="utf-8")


movie("Alien (1979)", '<set><name>Alien Collection</name></set><uniqueid type="tmdbSet">8091</uniqueid>')   # id in the .nfo
movie("Aliens (1986)", "<set><name>Alien Collection</name></set>")                                            # name only
movie("Heat (1995)")                                                                                           # no .nfo: ask TMDB
movie("Rogue One (2016)", '<set><name>Star Wars Anthology</name></set><uniqueid type="tmdbSet">999</uniqueid>')  # stale id

calls = []


def fake_tmdb(path, key=None, **params):
    calls.append(path)
    if path == "/configuration":
        return {}
    if path == "/collection/8091":
        return {"id": 8091, "name": "Alien Collection", "overview": "Xenomorphs.", "poster_path": "/a.jpg", "parts": [
            {"id": 348, "title": "Alien", "release_date": "1979-05-25"}, {"id": 679, "title": "Aliens", "release_date": "1986-07-18"},
            {"id": 8077, "title": "Alien 3", "release_date": "1992-05-22"}, {"id": 999999, "title": "Alien 9", "release_date": "2099-01-01"}]}
    if path == "/collection/999":
        raise core.TmdbError("TMDB has no movie with that ID.")
    if path == "/search/collection":
        return {"results": [{"id": 8091, "name": "Alien Collection"}] if "Alien" in params["query"] else []}
    if path == "/movie/949":
        return {"id": 949, "belongs_to_collection": None}
    if path == "/movie/330459":
        return {"id": 330459, "belongs_to_collection": None}
    raise AssertionError(path)


tmdbapi.tmdb = fake_tmdb
c = web.app.test_client()


def job(url, data):
    r = c.post(url, json=data).get_json()
    while not (j := c.get(f"/api/job/{r['job']}").get_json())["finished"]:
        time.sleep(0.05)
    assert not j["error"], j["error"]
    return j["result"]


items = job("/api/scan", {"folder": str(lib)})["items"]
ids = {Path(i["path"]).parent.name: i["id"] for i in items}
M = lambda tid, name: {"tmdb_id": tid, "status": "auto", "name": name, "title": name[:-7], "year": int(name[-5:-1])}
core.save_json(core.MATCHES, {ids["Alien (1979)"]: M(348, "Alien (1979)"), ids["Aliens (1986)"]: M(679, "Aliens (1986)"),
                              ids["Heat (1995)"]: M(949, "Heat (1995)"), ids["Rogue One (2016)"]: M(330459, "Rogue One (2016)")})
r = job("/api/collections", {})
ok(len(r["collections"]) == 1 and not r["failed"], f"one collection found, nothing failed ({[x['name'] for x in r['collections']]})")
coll = r["collections"][0]
ok([h["title"] for h in coll["have"]] == ["Alien", "Aliens"], "both Alien films counted: one by the id in its .nfo, one by the collection's name")
ok([(m["title"], m["released"]) for m in coll["missing"]] == [("Alien 3", True), ("Alien 9", False)], "missing: Alien 3 (released) and Alien 9 (not released yet)")
ok("/movie/949" in calls, "Heat has no .nfo, so TMDB was asked (it's in no collection)")
ok("/movie/330459" in calls, "Rogue One's stale collection id: TMDB asked again, and it's no longer in a collection, so it's skipped quietly")
calls.clear()
job("/api/collections", {})
ok(calls == ["/configuration"], "a second check uses what it remembered (no TMDB calls)")
built = nfo.build_nfo({"id": 348, "title": "Alien", "belongs_to_collection": {"id": 8091, "name": "Alien Collection"}}).decode()
ok('<set tmdbcolid="8091">' in built and 'type="tmdbSet">8091<' in built, "new .nfo files carry the collection's TMDB id for Jellyfin")

shutil.rmtree(S, ignore_errors=True)
print("done")
