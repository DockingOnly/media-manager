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
                data_for(r)
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
            matches[key] = make_match(brief(tmdb(f"/tv/{int(data['tmdb_id'])}")), "manual")  # (a new show: TMDB order)
        core.save_json(core.TV_MATCHES, matches)
    m = matches.get(key)
    if m and m.get("status") in core.MATCHED:
        try:
            data_for(m)
        except (TmdbError, UserError):
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


# ---------------------------------------------------------------- episode orders (TVmaze, TMDB's alternatives)

GROUP_TYPES = {1: "Original air date", 2: "Absolute", 3: "DVD", 4: "Digital", 5: "Story arc", 6: "Production", 7: "TV"}
TVMAZE = "https://api.tvmaze.com"


def _get_json(url: str):
    import json
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": core.USER_AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return None
            if err.code == 429 and attempt < 2:  # TVmaze allows about 20 requests per 10 seconds
                time.sleep(3 + attempt * 3)
                continue
            raise UserError(f"TVmaze returned error {err.code}.") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt < 1:
                continue
            raise UserError("Couldn't reach TVmaze. Check your internet connection.") from None
    return None


def _strip_html(text: str) -> str:
    import html
    import re
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _by_air_date(base: dict) -> dict:
    """TMDB's own (season, episode) for each air date, where the date is unique, to translate other orders."""
    seen, out = {}, {}
    for season, eps in base.get("episodes", {}).items():
        for e in eps:
            if e["air_date"]:
                seen.setdefault(e["air_date"], []).append((int(season), e["episode"]))
    for date, nums in seen.items():
        if len(nums) == 1:
            out[date] = nums[0]
    return out


def _tvmaze_episodes(base: dict) -> dict:
    s = base["show"]
    show = None
    if s.get("tvdb_id"):
        show = _get_json(f"{TVMAZE}/lookup/shows?thetvdb={s['tvdb_id']}")
    if not show and s.get("imdb_id"):
        show = _get_json(f"{TVMAZE}/lookup/shows?imdb={s['imdb_id']}")
    if not show:
        raise UserError("TVmaze doesn't know this show.")
    eps = _get_json(f"{TVMAZE}/shows/{show['id']}/episodes?specials=1") or []
    dates, out, special_no = _by_air_date(base), {}, {}
    for e in eps:
        season = e.get("season") or 0
        number = e.get("number")
        if number is None:  # TVmaze lists specials without numbers: number them in airing order
            special_no[season] = special_no.get(season, 0) + 1
            number = special_no[season]
        tmdb_no = dates.get(e.get("airdate") or "")
        out.setdefault(str(season), []).append({
            "episode": number, "id": None, "name": e.get("name") or "", "air_date": e.get("airdate") or "",
            "overview": _strip_html(e.get("summary")), "runtime": e.get("runtime"), "still_path": None,
            "still_url": ((e.get("image") or {}).get("original") or "").replace("http://", "https://") or None,
            "rating": (e.get("rating") or {}).get("average"), "directors": [], "writers": [],
            "tmdb_season": tmdb_no[0] if tmdb_no else None, "tmdb_episode": tmdb_no[1] if tmdb_no else None})
    return out


def _group_episodes(base: dict, group_id: str) -> dict:
    import re
    data = tmdb(f"/tv/episode_group/{group_id}", language=None)
    out = {}
    for g in sorted(data.get("groups", []), key=lambda g: g.get("order", 0)):
        name = g.get("name") or ""
        if re.search(r"special", name, re.I):
            season = 0
        else:
            m = re.search(r"\d+", name)
            season = int(m.group()) if m else g.get("order", 0) or 1
        for e in sorted(g.get("episodes", []), key=lambda e: e.get("order", 0)):
            orig = episode(base, e.get("season_number"), e.get("episode_number")) or {}
            out.setdefault(str(season), []).append({
                **orig, "episode": e.get("order", 0) + 1, "name": e.get("name") or orig.get("name", ""),
                "air_date": e.get("air_date") or orig.get("air_date", ""), "still_path": e.get("still_path") or orig.get("still_path"),
                "tmdb_season": e.get("season_number"), "tmdb_episode": e.get("episode_number")})
    return out


def order_options(tmdb_id) -> list:
    """The episode orders a show can follow."""
    opts = [{"id": None, "name": "TMDB (as aired on TMDB)"}, {"id": "tvmaze", "name": "TVmaze (original airing; matches TVDB for most shows)"}]
    for g in tmdb(f"/tv/{tmdb_id}/episode_groups", language=None).get("results", []):
        if g.get("episode_count"):
            opts.append({"id": f"group:{g['id']}", "name": f"TMDB {GROUP_TYPES.get(g['type'], 'alternative')} order: {g['name']}",
                         "episodes": g["episode_count"]})
    return opts


def _ordered(tmdb_id, order, refresh: bool = False, offline: bool = False):
    """The whole show in one episode order (None = TMDB's own). offline: only what's already stored."""
    base = cached(tmdb_id) if offline else show_data(tmdb_id, refresh=refresh)
    if not base or not order:
        return base
    key = f"{tmdb_id}|{order}"
    entry = core.load_json(core.TV_CACHE, {}).get(key)
    if offline:
        return combine(base, entry) if entry else None
    if not entry or refresh or time.time() - entry.get("fetched", 0) >= REFRESH_AFTER:
        episodes = _tvmaze_episodes(base) if order == "tvmaze" else _group_episodes(base, order.split(":", 1)[1])
        entry = {"fetched": time.time(), "episodes": episodes}
        with core.lock:
            cache = core.load_json(core.TV_CACHE, {})
            cache[key] = entry
            core.save_json(core.TV_CACHE, cache)
    return combine(base, entry)


def _for(match: dict, refresh: bool, offline: bool):
    data = _ordered(match["tmdb_id"], match.get("order"), refresh, offline)
    season_orders = match.get("season_orders") or {}
    if not data or not season_orders:
        return data
    episodes = dict(data["episodes"])
    for season, order in season_orders.items():  # seasons that follow a different order than the rest
        other = _ordered(match["tmdb_id"], order, refresh, offline)
        if other is None:
            return None if offline else data
        if str(season) in other["episodes"]:
            episodes[str(season)] = other["episodes"][str(season)]
    base = cached(match["tmdb_id"]) if offline else show_data(match["tmdb_id"])
    return combine(base, {"fetched": data["fetched"], "episodes": episodes})


def data_for(match: dict, refresh: bool = False) -> dict:
    """The show's details with its episodes in the order you chose for it (TMDB's by default),
    season by season when some seasons follow a different order."""
    return _for(match, refresh, offline=False)


def cached_for(match: dict):
    """Like data_for, but only from what's already stored (no network)."""
    return _for(match, False, offline=True)


def combine(base: dict, ordered: dict) -> dict:
    seasons = []
    for season, eps in ordered["episodes"].items():
        info = next((s for s in base["show"]["seasons"] if s["season"] == int(season)), {})
        seasons.append({**info, "season": int(season), "episode_count": len(eps),
                        "name": info.get("name") or ("Specials" if season == "0" else f"Season {season}")})
    return {"fetched": ordered["fetched"], "show": {**base["show"], "seasons": sorted(seasons, key=lambda s: s["season"])},
            "episodes": ordered["episodes"]}


def tmdb_numbers(data: dict, season: int, number: int):
    """TMDB's own season and episode for an episode in any order (what OpenSubtitles searches by)."""
    e = episode(data, season, number)
    if not e:
        return None
    s, n = e.get("tmdb_season", season), e.get("tmdb_episode", number)
    return (s, n) if s is not None and n is not None else None


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
