"""TV .nfo files: tvshow.nfo, season.nfo and one per episode file (two entries for a two-episode file).

Same rules as movies: missing files are created; 'update' merges fresh TMDB data and keeps everything else
(watched status, play counts, tinyMediaManager's extras) plus your edits. Episodes get one more safeguard:
tinyMediaManager took its episode data from TVDB, and TMDB sometimes numbers seasons differently, so an
existing episode .nfo whose title disagrees with TMDB's is left alone rather than given the wrong episode.
"""
from pathlib import Path
from xml.etree import ElementTree as ET

from . import core, nfo, tvlib, tvmeta
from .nfo import sub
from .tmdbapi import image_url

SHOW_MANAGED = {"title", "originaltitle", "showtitle", "plot", "outline", "tagline", "year", "premiered", "status",
                "mpaa", "rating", "uniqueid", "tmdbid", "tvdbid", "imdbid", "genre", "studio", "actor", "runtime"}
EPISODE_MANAGED = {"title", "showtitle", "season", "episode", "plot", "aired", "premiered", "runtime", "rating",
                   "uniqueid", "director", "credits", "thumb"}
CARRY_OVER = ("watched", "playcount", "lastplayed", "userrating", "dateadded", "fileinfo")


def build_show(data: dict):
    s = data["show"]
    root = ET.Element("tvshow")
    sub(root, "title", s["name"])
    sub(root, "originaltitle", s["original_name"])
    sub(root, "showtitle", s["name"])
    sub(root, "plot", s["overview"])
    sub(root, "outline", s["overview"])
    sub(root, "tagline", s["tagline"])
    sub(root, "year", (s["first_air_date"] or "")[:4])
    sub(root, "premiered", s["first_air_date"])
    sub(root, "status", s["status"])
    sub(root, "mpaa", s["mpaa"])
    sub(root, "runtime", s["runtime"])
    if s.get("vote_average"):
        sub(root, "rating", round(s["vote_average"], 1))
    sub(root, "uniqueid", s["id"], type="tmdb", default="true")
    sub(root, "tmdbid", s["id"])
    if s.get("tvdb_id"):
        sub(root, "uniqueid", s["tvdb_id"], type="tvdb")
        sub(root, "tvdbid", s["tvdb_id"])
    if s.get("imdb_id"):
        sub(root, "uniqueid", s["imdb_id"], type="imdb")
        sub(root, "imdbid", s["imdb_id"])
    for g in s["genres"]:
        sub(root, "genre", g)
    for n in s["networks"]:
        sub(root, "studio", n)
    for i, a in enumerate(s["cast"]):
        actor = sub(root, "actor")
        sub(actor, "name", a["name"])
        sub(actor, "role", a["role"])
        sub(actor, "order", i)
        sub(actor, "thumb", a["thumb"])
    return root


def build_season(data: dict, season: int):
    info = next((x for x in data["show"]["seasons"] if x["season"] == season), {})
    root = ET.Element("season")
    sub(root, "seasonnumber", season)
    sub(root, "title", info.get("name") or ("Specials" if season == 0 else f"Season {season}"))
    sub(root, "plot", info.get("overview"))
    sub(root, "premiered", info.get("air_date"))
    sub(root, "year", (info.get("air_date") or "")[:4])
    return root


def build_episode(data: dict, season: int, number: int):
    e = tvmeta.episode(data, season, number)
    if not e:
        return None
    root = ET.Element("episodedetails")
    sub(root, "title", e["name"])
    sub(root, "showtitle", data["show"]["name"])
    sub(root, "season", season)
    sub(root, "episode", number)
    sub(root, "plot", e["overview"])
    sub(root, "aired", e["air_date"])
    sub(root, "premiered", e["air_date"])
    sub(root, "runtime", e["runtime"])
    if e.get("rating"):
        sub(root, "rating", round(e["rating"], 1))
    if e.get("id"):
        sub(root, "uniqueid", e["id"], type="tmdb", default="true")
    for d in e["directors"]:
        sub(root, "director", d)
    for w in dict.fromkeys(e["writers"]):
        sub(root, "credits", w)
    sub(root, "thumb", e.get("still_url") or image_url(e.get("still_path"), "w780"))
    return root


def merge_into(existing, fresh, managed: set) -> None:
    for el in [e for e in list(existing) if e.tag in managed]:
        existing.remove(el)
    for el in list(fresh):
        existing.append(el)


def same_episode(existing_title: str, tmdb_title: str) -> bool:
    return tvlib.titles_agree(existing_title, tmdb_title)


def write_show(show_dir: Path, data: dict, tmdb_id, overwrite: bool) -> bool:
    path = show_dir / "tvshow.nfo"
    existing = nfo.parse_xml(path.read_bytes(), "tvshow") if path.exists() else None
    if existing is not None and not overwrite:
        return False
    fresh = build_show(data)
    root = existing if existing is not None else fresh
    if existing is not None:
        merge_into(existing, fresh, SHOW_MANAGED)
    nfo.apply_edits(root, nfo.load_edits(f"tv:{tmdb_id}"))
    path.write_bytes(nfo.to_bytes(root))
    return True


def write_season(season_dir: Path, data: dict, season: int, overwrite: bool) -> bool:
    path = season_dir / "season.nfo"
    existing = nfo.parse_xml(path.read_bytes(), "season") if path.exists() else None
    if existing is not None and not overwrite:
        return False
    fresh = build_season(data, season)
    if existing is not None:
        merge_into(existing, fresh, {"seasonnumber", "title", "plot", "premiered", "year"})
        fresh = existing
    path.write_bytes(nfo.to_bytes(fresh))
    return True


def write_episode(video: Path, data: dict, season: int, numbers: list, overwrite: bool) -> str:
    """Returns 'written', 'skipped' (exists), 'kept' (titles disagree with TMDB) or 'unknown' (not on TMDB)."""
    path = video.with_suffix(".nfo")
    existing = nfo.parse_all(path.read_bytes()) if path.exists() else []
    if existing and not overwrite:
        return "skipped"
    fresh = [build_episode(data, season, n) for n in numbers]
    if any(f is None for f in fresh):
        return "unknown"
    if existing:
        for old, new in zip(existing, fresh):
            if not same_episode(old.findtext("title") or "", new.findtext("title") or ""):
                return "kept"
    roots = []
    for i, new in enumerate(fresh):
        if i < len(existing):  # update in place, keeping watched status, play count and the rest
            merge_into(existing[i], new, EPISODE_MANAGED)
            roots.append(existing[i])
        else:
            if existing:  # carry the first entry's watched status over to an added one
                for tag in CARRY_OVER:
                    el = existing[0].find(tag)
                    if el is not None and new.find(tag) is None:
                        new.append(el)
            roots.append(new)
    from . import mediainfo
    for r in roots:  # each entry carries the file's technical details, as Kodi and Jellyfin expect
        mediainfo.add_fileinfo(r, video)
    path.write_bytes(nfo.to_bytes_all(roots))
    return "written"


def write_all(job: dict, root: Path, overwrite: bool) -> dict:
    with core.lock:
        items = tvlib.annotate(tvlib.rescan(job, "Checking for changes"), root)
    shows = {k: v for k, v in tvlib.group(items).items() if tvlib.confirmed(v[0]["match"])}
    if not shows:
        raise core.UserError("Match some shows on TMDB first. Only confirmed matches get .nfo files.")
    core.job_step(job, "Writing .nfo files", len(shows))
    res = {"show_nfos": 0, "seasons": 0, "episodes": 0, "skipped": 0, "kept": 0, "unknown": 0, "failed": []}
    for key, eps in shows.items():
        if job["cancel"]:
            break
        m = eps[0]["match"]
        try:
            core.set_detail(job, m["name"])
            data = tvmeta.data_for(m)
            show_dir = root / eps[0]["show_dir"] if eps[0].get("show_dir") else None
            if show_dir and write_show(show_dir, data, m["tmdb_id"], overwrite):
                res["show_nfos"] += 1
            seasons = {}
            for it in eps:
                if it["kind"] != "episode":
                    continue
                video = Path(root, it["path"])
                if show_dir and video.parent != show_dir and video.parent.name == tvlib.season_folder(it["season"]):
                    seasons[video.parent] = it["season"]
                if it.get("mismatch") and not video.with_suffix(".nfo").exists():
                    res["kept"] += 1  # TMDB numbers it differently: its details would be another episode's
                    continue
                outcome = write_episode(video, data, it["season"], it["episodes"], overwrite)
                res["episodes" if outcome == "written" else outcome] += 1
            for season_dir, season in seasons.items():
                if write_season(season_dir, data, season, overwrite):
                    res["seasons"] += 1
        except (core.TmdbError, OSError) as err:
            res["failed"].append({"name": m["name"], "error": getattr(err, "strerror", None) or str(err)})
        finally:
            core.tick(job)
    res["cancelled"] = job["cancel"]
    with core.lock:
        tvlib.rescan()
    return {**tvlib.current(), **res}
