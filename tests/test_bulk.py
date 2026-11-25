"""Bulk editing: genres, tags, collection and age rating for many titles at once."""
import shutil
import sys
import tempfile
import time
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mm import core, tvmeta, web  # noqa: E402

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="mm-test-"))
lib, tv, st = S / "libB", S / "tvB", S / "stB"
for d in (lib, tv, st):
    shutil.rmtree(d, ignore_errors=True)
st.mkdir(parents=True)
for name in ("CONFIG", "MATCHES", "RENAME_LOG", "LIBRARY", "EDITS", "TV_LIBRARY", "TV_MATCHES", "TV_CACHE"):
    setattr(core, name, st / f"{name.lower()}.json")
core.save_json(core.CONFIG, {"tmdb_key": "x", "folder": str(lib), "tv_folder": str(tv)})
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)


def movie(name, genres, tags):
    d = lib / name
    d.mkdir(parents=True)
    (d / f"{name}.mkv").write_bytes(b"x")
    g = "".join(f"<genre>{x}</genre>" for x in genres)
    t = "".join(f"<tag>{x}</tag>" for x in tags)
    (d / f"{name}.nfo").write_text(f'<?xml version="1.0"?><movie><title>{name[:-7]}</title><year>{name[-5:-1]}</year>{g}{t}<watched>true</watched></movie>', encoding="utf-8")


movie("Alien (1979)", ["Horror", "Science Fiction"], ["space"])
movie("Heat (1995)", ["Crime", "Drama"], [])
movie("Unmatched (2000)", ["Drama"], [])
(tv / "Loki").mkdir(parents=True)
(tv / "Loki/Season 1").mkdir()
(tv / "Loki/Season 1/Loki - S01E01.mkv").write_bytes(b"x")
(tv / "Loki/tvshow.nfo").write_text('<?xml version="1.0"?><tvshow><title>Loki</title><genre>Drama</genre></tvshow>', encoding="utf-8")
tvmeta.tmdb = lambda path, key=None, **p: ({"id": 1, "name": "Loki", "first_air_date": "2021-06-09", "genres": [{"name": "Drama"}],
                                            "seasons": []} if path == "/tv/1" else {})
c = web.app.test_client()


def job(url, data):
    r = c.post(url, json=data).get_json()
    assert "job" in r, r
    while not (j := c.get(f"/api/job/{r['job']}").get_json())["finished"]:
        time.sleep(0.05)
    assert not j["error"], j["error"]
    return j["result"]


def nfo(name):
    return ET.fromstring((lib / name / f"{name}.nfo").read_text(encoding="utf-8").split("?>", 1)[1])


items = job("/api/scan", {"folder": str(lib)})["items"]
ids = {Path(i["path"]).parent.name: i["id"] for i in items}
M = lambda tid, name: {"tmdb_id": tid, "status": "manual", "name": name, "title": name[:-7], "year": int(name[-5:-1])}
core.save_json(core.MATCHES, {ids["Alien (1979)"]: M(348, "Alien (1979)"), ids["Heat (1995)"]: M(949, "Heat (1995)")})
all_ids = list(ids.values())

r = job("/api/bulk-edit", {"kind": "movies", "ids": all_ids, "changes": {"genres": {"mode": "add", "values": "Cult, horror"}, "tags": {"mode": "add", "values": "favourite"}}})
a, h = nfo("Alien (1979)"), nfo("Heat (1995)")
ok([g.text for g in a.findall("genre")] == ["Horror", "Science Fiction", "Cult"] and [g.text for g in h.findall("genre")] == ["Crime", "Drama", "Cult", "horror"],
   "add: new genres appended, existing ones (any case) not doubled")
ok([t.text for t in a.findall("tag")] == ["space", "favourite"] and a.findtext("watched") == "true" and a.findtext("lockdata") == "true",
   "tags added too; watched status kept; locked for Jellyfin")
ok(r["edited"] == 2 and r["skipped"] == ["Unmatched"], f"the unmatched movie is skipped ({r['skipped']})")
r = job("/api/bulk-edit", {"kind": "movies", "ids": all_ids, "changes": {"genres": {"mode": "remove", "values": "CULT"}, "mpaa": "R", "set": "My Set"}})
a = nfo("Alien (1979)")
ok([g.text for g in a.findall("genre")] == ["Horror", "Science Fiction"] and a.findtext("mpaa") == "R" and a.findtext("set/name") == "My Set",
   "remove (ignoring case), age rating and collection set in one go")
r = job("/api/bulk-edit", {"kind": "movies", "ids": all_ids, "changes": {"mpaa": "R"}})
ok(r["edited"] == 0 and r["unchanged"] == 2, "nothing rewritten when it's already that way")
r = job("/api/bulk-edit", {"kind": "movies", "ids": [ids["Heat (1995)"]], "changes": {"genres": {"mode": "set", "values": "Thriller"}, "set": ""}})
h = nfo("Heat (1995)")
ok([g.text for g in h.findall("genre")] == ["Thriller"] and h.find("set") is None, "replace genres, and an empty collection removes it")
ok(c.post("/api/bulk-edit", json={"kind": "movies", "ids": all_ids, "changes": {}}).status_code == 400, "an empty request is refused")

shows = job("/api/tv/scan", {"folder": str(tv)})["shows"]
c.post("/api/tv/match", json={"key": shows[0]["key"], "tmdb_id": 1})
r = job("/api/bulk-edit", {"kind": "tv", "ids": [shows[0]["key"]], "changes": {"genres": {"mode": "add", "values": "Sci-Fi"}, "set": "ignored"}})
show = (tv / "Loki/tvshow.nfo").read_text(encoding="utf-8")
ok(r["edited"] == 1 and "<genre>Sci-Fi</genre>" in show and "<set>" not in show, "TV shows: genres added to tvshow.nfo (collections don't apply)")

shutil.rmtree(S, ignore_errors=True)
print("done")
