"""TMDB for TV: searching and matching shows (tvshow.nfo IDs first), and a store of show and episode details."""
import datetime
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import core, tvlib
from .core import TmdbError, UserError, norm_title
from .tmdbapi import image_url, tmdb, year_of

REFRESH_AFTER = 7 * 24 * 3600  # show and episode details are re-fetched weekly


def brief(show: dict) -> dict:
    return {"tmdb_id": show["id"], "title": show.get("name") or show.get("original_name") or "",
            "original_title": show.get("original_name") or "", "year": year_of(show.get("first_air_date")),
            "overview": (show.get("overview") or "")[:400], "poster": image_url(show.get("poster_path"))}


def make_match(show: dict, status: str) -> dict:
    return {"tmdb_id": show["tmdb_id"], "title": show["title"], "year": show["year"], "poster": show["poster"],
            "status": status, "name": core.jellyfin_name(show["title"], show["year"])}


def search(query: str, year=None) -> list:
    data = tmdb("/search/tv", query=query, first_air_date_year=year, include_adult="false")
    return [brief(s) for s in data.get("results", [])[:12]]


def match_from_nfo(show_dir: Path):
    """tvshow.nfo (from tinyMediaManager, Jellyfin or this app) names the exact show."""
    from . import nfo
    path = show_dir / "tvshow.nfo" if show_dir else None
    if not path or not path.is_file():
        return None
    root = nfo.parse_xml(path.read_bytes(), tag="tvshow")
    if root is None:
        return None
    ids = {u.get("type", "").lower(): (u.text or "").strip() for u in root.findall("uniqueid")}
    tmdb_id = ids.get("tmdb") or nfo.text(root, "tmdbid")
    if str(tmdb_id).isdigit():
        try:
            return {**make_match(brief(tmdb(f"/tv/{tmdb_id}")), "auto"), "via": "nfo"}
        except TmdbError as err:
            if "no movie with that ID" not in str(err):
                raise
    for source, value in (("tvdb_id", ids.get("tvdb") or nfo.text(root, "tvdbid")), ("imdb_id", ids.get("imdb"))):
        if value:
            found = tmdb(f"/find/{value}", external_source=source).get("tv_results") or []
            if found:
                return {**make_match(brief(found[0]), "auto"), "via": "nfo"}
    return None


def auto_match(show: dict, root: Path) -> dict:
    show_dir = root / show["show_dir"] if show.get("show_dir") else None
    found = match_from_nfo(show_dir)
    if found:
        return found
    title, year = tvlib.show_title_year(show_dir.name) if show_dir else (show["series"], None)
    results = search(title, year) or (search(title) if year else [])
    if not results:
        return {"status": "none"}
    want = norm_title(title)
    same = [r for r in results if want in (norm_title(r["title"]), norm_title(r["original_title"]))]
    exact = [r for r in same if not year or r["year"] == year]
    if len(exact) == 1 or (exact and exact[0] is results[0]):
        return make_match(exact[0], "auto")
    return make_match((exact or same or results)[0], "review")


def match_all(job: dict, todo: list, root: Path) -> dict:
    errors, results = [], {}

    def one(show):
        if job["cancel"]:
            return
        try:
            results[show["key"]] = auto_match(show, root)
        except TmdbError as err:
            errors.append(str(err))
        finally:
            core.tick(job)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(one, todo))
    with core.lock:
        matches = core.load_json(core.TV_MATCHES, {})
        matches.update(results)
        core.save_json(core.TV_MATCHES, matches)
    counts = {s: sum(1 for r in results.values() if r["status"] == s) for s in ("auto", "review", "none")}
    counts["nfo"] = sum(1 for r in results.values() if r.get("via") == "nfo")
    # fetch episode details for the newly matched shows, so titles and missing episodes show up
    for r in results.values():
        if r["status"] in core.MATCHED and not job["cancel"]:
            core.set_detail(job, f"{r['name']}: episode list")
            try:
                show_data(r["tmdb_id"])
            except TmdbError:
                pass
    from . import tvlib as tl
    return {**tl.current(), "counts": counts, "checked": len(todo), "errors": len(errors),
            "error": errors[0] if errors else None}


def set_match(key: str, data: dict) -> dict:
    if not key:
        raise UserError("That show is no longer in the list. Scan again.")
    with core.lock:
        matches = core.load_json(core.TV_MATCHES, {})
        if data.get("clear"):
            matches.pop(key, None)
        elif data.get("accept"):
            if key not in matches:
                raise UserError("That show has no match to accept.")
            matches[key]["status"] = "manual"
        else:
            if not str(data.get("tmdb_id") or "").isdigit():
                raise UserError("Pick a show from the search results.")
            matches[key] = make_match(brief(tmdb(f"/tv/{int(data['tmdb_id'])}")), "manual")
        core.save_json(core.TV_MATCHES, matches)
    m = matches.get(key)
    if m and m.get("status") in core.MATCHED:
        try:
            show_data(m["tmdb_id"])
        except TmdbError:
            pass
    return m


# ---------------------------------------------------------------- show and episode details (cached)

def cached(tmdb_id):
    return core.load_json(core.TV_CACHE, {}).get(str(tmdb_id))


def show_data(tmdb_id, refresh: bool = False) -> dict:
    """Everything the app needs about a show: details, cast, seasons and every episode."""
    cache = core.load_json(core.TV_CACHE, {})
    entry = cache.get(str(tmdb_id))
    if entry and not refresh and time.time() - entry.get("fetched", 0) < REFRESH_AFTER:
        return entry
    d = tmdb(f"/tv/{tmdb_id}", append_to_response="external_ids,content_ratings,aggregate_credits,videos")
    rating = next((r.get("rating") for r in d.get("content_ratings", {}).get("results", []) if r.get("iso_3166_1") == "US"), "")
    show = {
        "id": d["id"], "name": d.get("name", ""), "original_name": d.get("original_name", ""),
        "first_air_date": d.get("first_air_date") or "", "overview": d.get("overview") or "", "tagline": d.get("tagline") or "",
        "status": d.get("status") or "", "genres": [g["name"] for g in d.get("genres", [])],
        "networks": [n["name"] for n in d.get("networks", [])][:3], "mpaa": rating or "",
        "runtime": (d.get("episode_run_time") or [None])[0], "vote_average": d.get("vote_average"),
        "poster_path": d.get("poster_path"), "backdrop_path": d.get("backdrop_path"),
        "tvdb_id": d.get("external_ids", {}).get("tvdb_id"), "imdb_id": d.get("external_ids", {}).get("imdb_id"),
        "cast": [{"name": c.get("name"), "role": (c.get("roles") or [{}])[0].get("character", ""),
                  "thumb": image_url(c.get("profile_path"), "w300")} for c in d.get("aggregate_credits", {}).get("cast", [])[:15]],
        "videos": [v for v in d.get("videos", {}).get("results", []) if v.get("site") == "YouTube"],
        "seasons": [{"season": s["season_number"], "name": s.get("name") or "", "overview": s.get("overview") or "",
                     "air_date": s.get("air_date") or "", "poster_path": s.get("poster_path"),
                     "episode_count": s.get("episode_count") or 0} for s in d.get("seasons", [])],
    }
    episodes = {}
    for s in show["seasons"]:
        sd = tmdb(f"/tv/{tmdb_id}/season/{s['season']}")
        episodes[str(s["season"])] = [{
            "episode": e["episode_number"], "id": e.get("id"), "name": e.get("name") or "",
            "air_date": e.get("air_date") or "", "overview": e.get("overview") or "", "runtime": e.get("runtime"),
            "still_path": e.get("still_path"), "rating": e.get("vote_average"),
            "directors": [c["name"] for c in e.get("crew", []) if c.get("job") == "Director"],
            "writers": [c["name"] for c in e.get("crew", []) if c.get("department") == "Writing"],
        } for e in sd.get("episodes", [])]
    entry = {"fetched": time.time(), "show": show, "episodes": episodes}
    with core.lock:
        cache = core.load_json(core.TV_CACHE, {})
        cache[str(tmdb_id)] = entry
        core.save_json(core.TV_CACHE, cache)
    return entry


def episode(data: dict, season: int, number: int):
    return next((e for e in (data or {}).get("episodes", {}).get(str(season), []) if e["episode"] == number), None)


def missing_episodes(data: dict, have: set) -> list:
    """Aired episodes (not specials) that have no file."""
    today = datetime.date.today().isoformat()
    out = []
    for season, eps in (data or {}).get("episodes", {}).items():
        if season == "0":
            continue
        for e in eps:
            if e["air_date"] and e["air_date"] <= today and (int(season), e["episode"]) not in have:
                out.append({"season": int(season), "episode": e["episode"], "name": e["name"], "air_date": e["air_date"]})
    return sorted(out, key=lambda x: (x["season"], x["episode"]))
