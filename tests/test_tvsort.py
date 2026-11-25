"""DVD extras and specials in a show folder: matched to TMDB's specials, or moved into Jellyfin's extras folders."""
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mm import core, tvmeta, tvsort, web  # noqa: E402

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="mm-test-"))
tv, st = S / "tvX", S / "stX"
for d in (tv, st):
    shutil.rmtree(d, ignore_errors=True)
st.mkdir(parents=True)
for name in ("CONFIG", "MATCHES", "RENAME_LOG", "LIBRARY", "EDITS", "TV_LIBRARY", "TV_MATCHES", "TV_CACHE", "TV_RENAME_LOG"):
    setattr(core, name, st / f"{name.lower()}.json")
core.save_json(core.CONFIG, {"tmdb_key": "x", "tv_folder": str(tv)})
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)
binned = []
core.discard = lambda p: (binned.append(p.name), p.unlink())

# unit checks
ok(tvsort.initials("Aqua Teen Hunger Force") == "ATHF", "show initials")
ok(tvsort.extra_title("ATHF S04M01X03 Deleted Scene Answering Machine Fight", "Aqua Teen Hunger Force") == "Deleted Scene Answering Machine Fight",
   "disc codes and the show's initials are dropped from an extra's title")
ok([f for f, _ in tvsort.place(["Deleted Scene Cameo", "Fake Ending 1", "Movie Promo", "Carl's Pissed Giants Win", "Carl's Pissed Red Losers",
                                 "Carl's Pissed Roid Rage", "Carl on Health"])] ==
   ["deleted scenes", "other", "trailers", "shorts", "shorts", "shorts", "extras"], "extras sorted into Jellyfin's folders")
ok(tvsort.place(["Deleted Scene Cameo"])[0][1] == "Cameo", "'Deleted Scene' isn't repeated in the deleted scenes folder")


def w(rel, data=b"x"):
    p = tv / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


show = "Brak Hour (2000)"
w(f"{show}/tvshow.nfo", b'<?xml version="1.0"?><tvshow><title>Brak Hour</title><uniqueid type="tmdb">7</uniqueid></tvshow>')
w(f"{show}/Season 1/Brak Hour - S01E01 - Alpha.mkv")
w(f"{show}/Season 1/Brak Hour - S01E02 - Beta.mkv")
w(f"{show}/Season 2/Brak Hour - S02E01 - Zeta.mkv")  # numbered differently from TMDB (TMDB says Eta): a real episode
# extras given episode numbers that the real episodes already have
w(f"{show}/Season 1/Brak Hour - S01E01 - BH S01M01X01 Deleted Scene Cameo.mkv")
w(f"{show}/Season 1/Brak Hour - S01E01 - BH S01M01X01 Deleted Scene Cameo.nfo", b"<episodedetails><title>Alpha</title></episodedetails>")
w(f"{show}/Season 1/Brak Hour - S01E01 - BH S01M01X01 Deleted Scene Cameo-thumb.jpg")
w(f"{show}/Season 1/Brak Hour - S01E01 - BH S01M01X01 Deleted Scene Cameo.en.srt")
w(f"{show}/Season 1/Brak Hour Pack - S01E01 - Carl's Pissed Joe.mkv")
w(f"{show}/Season 1/Brak Hour Pack - S01E02 - Carl's Pissed Giants Win.mkv")
w(f"{show}/Season 1/Brak Hour Pack 2 - S01E02 - Carl's Pissed Giants Win.mkv")
# a special numbered as episode 3 of season 1 (TMDB's season 1 has 2), with its trailer
w(f"{show}/Season 1/Brak Hour - S01E03 - BH S01M01X19 Brak Hour Colon Movie Film for Television.mkv")
w(f"{show}/Season 1/Brak Hour - S01E03 - BH S01M01X19 Brak Hour Colon Movie Film for Television-trailer.mkv")
# a folder of extras
w(f"{show}/BH Xtras/BH X01 Carl on Health.mkv")

SPECIALS = [{"episode_number": 1, "name": "Brak Hour Colon Movie Film for Television", "air_date": "2007-04-01", "id": 900}]
EPS = {1: ["Alpha", "Beta"], 2: ["Eta"]}


def fake_tmdb(path, key=None, **params):
    parts = path.strip("/").split("/")
    if path == "/configuration":
        return {}
    if parts[-1] == "episode_groups":
        return {"results": []}
    if len(parts) == 2:
        return {"id": 7, "name": "Brak Hour", "first_air_date": "2000-01-01", "external_ids": {},
                "seasons": [{"season_number": 0, "episode_count": 1}] + [{"season_number": s, "episode_count": len(e)} for s, e in EPS.items()]}
    season = int(parts[3])
    if season == 0:
        return {"episodes": SPECIALS}
    return {"episodes": [{"episode_number": i, "name": t, "air_date": f"200{season}-01-0{i}", "id": season * 10 + i} for i, t in enumerate(EPS[season], 1)]}


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


job("/api/tv/scan", {"folder": str(tv)})
job("/api/tv/match-all", {})
pv = c.get("/api/tv/rename/preview").get_json()
plan = pv["plans"][0]
moves = {Path(ch["from"]).name: ch["to"] for ch in plan["changes"]}
sp = "Specials\\" if "\\" in next(iter(moves.values())) else "Specials/"
ok(any(v.endswith("Brak Hour - S00E01 - Brak Hour Colon Movie Film for Television.mkv") and sp in v for v in moves.values()),
   "a file whose title is one of TMDB's specials becomes that special, in Specials")
ok(any(v.endswith("S00E01 - Brak Hour Colon Movie Film for Television-trailer.mkv") for v in moves.values()), "its trailer goes with it")
ok(moves.get("Brak Hour - S01E01 - BH S01M01X01 Deleted Scene Cameo.mkv", "").replace("\\", "/").endswith("deleted scenes/Cameo.mkv"),
   "a deleted scene numbered as S01E01 (the real S01E01 is there) moves to 'deleted scenes'")
ok(moves.get("Brak Hour - S01E01 - BH S01M01X01 Deleted Scene Cameo.en.srt", "").endswith("Cameo.en.srt"), "its subtitles go with it")
ok(moves.get("Brak Hour - S01E01 - BH S01M01X01 Deleted Scene Cameo.nfo", "").startswith("Recycle Bin")
   and moves.get("Brak Hour - S01E01 - BH S01M01X01 Deleted Scene Cameo-thumb.jpg", "").startswith("Recycle Bin"),
   "the .nfo and thumbnail made for the wrong episode go to the Recycle Bin")
shorts = sorted(Path(v).name for v in moves.values() if "shorts" in v)
ok(shorts == ["Carl's Pissed Giants Win (2).mkv", "Carl's Pissed Giants Win.mkv", "Carl's Pissed Joe.mkv"],
   f"a run of three or more with the same first words are shorts; a repeated title gets (2): {shorts}")
ok(any(v.replace("\\", "/").endswith("extras/Carl on Health.mkv") for v in moves.values()), "files in an extras folder ('BH Xtras') move to 'extras'")
ok(not any("Zeta" in k for k in moves) and not plan["left_alone"],
   "an episode that's only numbered differently is still left alone")

r = job("/api/tv/rename/apply", {"ids": [plan["id"]]})
base = tv / show
ok((base / "deleted scenes/Cameo.mkv").is_file() and (base / "deleted scenes/Cameo.en.srt").is_file()
   and (base / "Specials/Brak Hour - S00E01 - Brak Hour Colon Movie Film for Television.mkv").is_file()
   and (base / "extras/Carl on Health.mkv").is_file() and not (base / "BH Xtras").exists(),
   "applied: files moved, the emptied extras folder removed")
ok(sorted(binned) == ["Brak Hour - S01E01 - BH S01M01X01 Deleted Scene Cameo-thumb.jpg", "Brak Hour - S01E01 - BH S01M01X01 Deleted Scene Cameo.nfo"],
   "the wrong .nfo and thumbnail went to the Recycle Bin")
lib = c.get("/api/tv/library").get_json()["shows"][0]
ok(lib["mismatch"] == 1 and lib["extras"] == 0, f"the show now has only the one left-alone episode flagged ({lib['mismatch']}); the extras are out of the episode list")
c.post("/api/tv/rename/undo", json={})
ok((base / "Season 1/Brak Hour - S01E01 - BH S01M01X01 Deleted Scene Cameo.mkv").is_file() and (base / "BH Xtras/BH X01 Carl on Health.mkv").is_file(),
   "undo puts the files back")
shutil.rmtree(S, ignore_errors=True)
print("done")
