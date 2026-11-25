"""TMDB: searching, matching movies, and fetching their details."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import core, library
from .core import TmdbError, norm_title

TMDB_API = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p/"


def tmdb(path: str, key: str = None, **params) -> dict:
    cfg = core.load_config()
    key = (key or cfg.get("tmdb_key") or "").strip()
    if not key:
        raise TmdbError("Add your TMDB API key in Settings first.")
    params.setdefault("language", cfg.get("language") or "en-US")
    headers = {"Accept": "application/json", "User-Agent": core.USER_AGENT}
    if len(key) > 40:  # a v4 "read access token"
        headers["Authorization"] = "Bearer " + key
    else:              # a v3 API key
        params["api_key"] = key
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    req = urllib.request.Request(f"{TMDB_API}{path}?{query}", headers=headers)
    waits = [2, 4, 8, 15, 30]  # about a minute in total, enough to ride out a short TMDB outage
    for attempt in range(len(waits) + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as err:
            # 429 = slow down; 500/502/503/504 = TMDB's servers are briefly overloaded or down
            if err.code in (429, 500, 502, 503, 504) and attempt < len(waits):
                time.sleep(waits[attempt])
                continue
            if err.code == 401:
                raise TmdbError("TMDB rejected the API key. Check it in Settings.") from None
            if err.code == 404:
                raise TmdbError("TMDB has no movie with that ID.") from None
            raise TmdbError(f"TMDB returned error {err.code}. Try again in a minute.") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt < 2:
                time.sleep(waits[attempt])
                continue
            raise TmdbError("Couldn't reach TMDB. Check your internet connection.") from None
    raise TmdbError("TMDB is busy. Try again in a minute.")


def year_of(date: str):
    return int(date[:4]) if date and date[:4].isdigit() else None


def image_url(path, size="w185"):
    return f"{TMDB_IMG}{size}{path}" if path else None


def brief(movie: dict) -> dict:
    return {
        "tmdb_id": movie["id"],
        "title": movie.get("title") or movie.get("original_title") or "",
        "original_title": movie.get("original_title") or "",
        "year": year_of(movie.get("release_date")),
        "overview": (movie.get("overview") or "")[:400],
        "poster": image_url(movie.get("poster_path")),
    }


def search(query: str, year=None) -> list:
    data = tmdb("/search/movie", query=query, year=year, include_adult="false")
    return [brief(m) for m in data.get("results", [])[:12]]


def make_match(movie: dict, status: str) -> dict:
    return {
        "tmdb_id": movie["tmdb_id"], "title": movie["title"], "year": movie["year"],
        "poster": movie["poster"], "status": status,
        "name": core.jellyfin_name(movie["title"], movie["year"]),
    }


def match_from_nfo(item: dict):
    """An .nfo next to the file (from tinyMediaManager, Jellyfin or this app) usually names the exact movie.
    Use its TMDB or IMDb ID instead of guessing from the file name."""
    from . import nfo  # (nfo uses this module too)
    root = core.state["root"]
    if root is None:
        return None
    path = nfo.nfo_path(Path(root, item["path"]))
    info = nfo.read_nfo(path) if path else None
    if not info:
        return None
    if info["tmdb_id"]:
        try:
            movie = brief(tmdb(f"/movie/{info['tmdb_id']}"))
        except TmdbError as err:
            if "no movie with that ID" not in str(err):
                raise
            movie = None
        if movie:
            return {**make_match(movie, "auto"), "via": "nfo"}
    if info["imdb_id"]:
        found = tmdb(f"/find/{info['imdb_id']}", external_source="imdb_id").get("movie_results") or []
        if found:
            return {**make_match(brief(found[0]), "auto"), "via": "nfo"}
    return None


def auto_match(item: dict) -> dict:
    """Use the IDs in an existing .nfo when there is one. Otherwise accept a search result only when the
    title and year both match exactly, and flag anything less certain for review."""
    from_nfo = match_from_nfo(item)
    if from_nfo:
        return from_nfo
    title, year = item["title"], item["year"]
    results = search(title, year)
    if year and not results:
        results = search(title)
    if not results:
        return {"status": "none"}
    want = norm_title(title)
    same = [r for r in results if want in (norm_title(r["title"]), norm_title(r["original_title"]))]
    exact = [r for r in same if year and r["year"] == year]
    # One exact hit, or several where TMDB's top result is one of them (e.g. two films called "Alien" in 1979)
    if len(exact) == 1 or (exact and exact[0] is results[0]):
        return make_match(exact[0], "auto")
    close = [r for r in same if year and r["year"] and abs(r["year"] - year) <= 1]
    best = (exact or close or same or results)[0]
    return make_match(best, "review")


def match_all(job: dict, todo: list) -> dict:
    errors, results = [], {}

    def one(it):
        if job["cancel"]:
            return
        try:
            results[it["id"]] = auto_match(it)
        except TmdbError as err:
            errors.append(str(err))
        finally:
            core.tick(job)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(one, todo))
    with core.lock:
        matches = core.load_json(core.MATCHES, {})
        matches.update(results)
        core.save_json(core.MATCHES, matches)
        items = library.current_items()
    counts = {s: sum(1 for r in results.values() if r["status"] == s) for s in ("auto", "review", "none")}
    counts["nfo"] = sum(1 for r in results.values() if r.get("via") == "nfo")
    return {"items": items, "counts": counts, "checked": len(todo),
            "errors": len(errors), "error": errors[0] if errors else None}


def set_match(item_id: str, data: dict) -> dict:
    if not item_id:
        raise core.UserError("That movie is no longer in the list. Scan again.")
    with core.lock:
        matches = core.load_json(core.MATCHES, {})
        if data.get("clear"):
            matches.pop(item_id, None)
        elif data.get("accept"):  # confirm a "needs review" guess as-is
            if item_id not in matches:
                raise core.UserError("That movie has no match to accept.")
            matches[item_id]["status"] = "manual"
        else:
            if not str(data.get("tmdb_id") or "").isdigit():
                raise core.UserError("Pick a movie from the search results.")
            movie = brief(tmdb(f"/movie/{int(data['tmdb_id'])}"))
            matches[item_id] = make_match(movie, "manual")
        core.save_json(core.MATCHES, matches)
        items = library.current_items()
    return next((it for it in items if it["id"] == item_id), None)
