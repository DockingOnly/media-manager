"""Collections (movie sets): which you have, and which of their movies are missing.

Jellyfin builds its collections from the <set> in each movie's .nfo and fetches their artwork itself, so what
this adds is the overview: every collection in your library, how many of its films you have, and which are missing.
"""
import datetime
import time
from pathlib import Path

from . import core, library, nfo, tmdbapi
from .tmdbapi import image_url, year_of

REFRESH_AFTER = 7 * 24 * 3600


def collection_from_nfo(video: Path, own: bool):
    """The collection an .nfo names: (TMDB id or None, name)."""
    path = nfo.nfo_path(video, own)
    root = nfo.parse_xml(path.read_bytes()) if path else None
    if root is None:
        return None
    ids = {u.get("type", "").lower(): (u.text or "").strip() for u in root.findall("uniqueid")}
    set_el = root.find("set")
    name = ((set_el.findtext("name") if set_el is not None else "") or (set_el.text if set_el is not None else "") or "").strip()
    cid = ids.get("tmdbset") or (set_el.get("tmdbcolid") if set_el is not None else None) or nfo.text(root, "tmdbcolid")
    if not name and not cid:
        return None
    return (int(cid) if str(cid or "").isdigit() else None, name)


def movie_collection(tmdb_id) -> dict:
    """{id, name} of the collection a movie belongs to (or {} for none), from TMDB, remembered."""
    cache = core.load_json(core.MOVIE_CACHE, {})
    entry = cache.get(str(tmdb_id))
    if entry is None or time.time() - entry.get("fetched", 0) > REFRESH_AFTER:
        d = tmdbapi.tmdb(f"/movie/{tmdb_id}")
        c = d.get("belongs_to_collection") or {}
        entry = {"fetched": time.time(), "collection": {"id": c["id"], "name": c.get("name", "")} if c.get("id") else {}}
        with core.lock:
            cache = core.load_json(core.MOVIE_CACHE, {})
            cache[str(tmdb_id)] = entry
            core.save_json(core.MOVIE_CACHE, cache)
    return entry["collection"]


def collection_data(cid) -> dict:
    cache = core.load_json(core.COLLECTIONS, {})
    entry = cache.get(str(cid))
    if entry is not None and entry.get("gone") and time.time() - entry.get("fetched", 0) <= REFRESH_AFTER:
        raise core.TmdbError("TMDB has no movie with that ID.")  # remembered: this id is gone from TMDB
    if entry is None or time.time() - entry.get("fetched", 0) > REFRESH_AFTER:
        try:
            d = tmdbapi.tmdb(f"/collection/{cid}")
        except core.TmdbError as err:
            if "no movie with that ID" in str(err):
                with core.lock:
                    cache = core.load_json(core.COLLECTIONS, {})
                    cache[str(cid)] = {"fetched": time.time(), "gone": True}
                    core.save_json(core.COLLECTIONS, cache)
            raise
        entry = {"fetched": time.time(), "id": d["id"], "name": d.get("name", ""), "overview": d.get("overview") or "",
                 "poster": image_url(d.get("poster_path"), "w342"), "parts": [
                     {"tmdb_id": p["id"], "title": p.get("title") or "", "year": year_of(p.get("release_date")),
                      "release_date": p.get("release_date") or "", "poster": image_url(p.get("poster_path"), "w185")}
                     for p in d.get("parts", [])]}
        with core.lock:
            cache = core.load_json(core.COLLECTIONS, {})
            cache[str(cid)] = entry
            core.save_json(core.COLLECTIONS, cache)
    return entry


def find_name(name: str):
    """A collection's TMDB id from its name (for .nfo files that only name it), remembered."""
    cache = core.load_json(core.COLLECTIONS, {})
    known = cache.get("_names", {})
    if name in known:
        return known[name]
    found = tmdbapi.tmdb("/search/collection", query=name).get("results", [])
    want = core.norm_title(name)
    cid = next((r["id"] for r in found if core.norm_title(r.get("name", "")) == want), None)
    with core.lock:
        cache = core.load_json(core.COLLECTIONS, {})
        cache.setdefault("_names", {})[name] = cid
        core.save_json(core.COLLECTIONS, cache)
    return cid


def run(job: dict, root: Path) -> dict:
    with core.lock:
        items = library.rescan(job, "Checking for changes")
    folders = library.by_folder(items, root)
    movies = {}
    for it in items:
        m = it.get("match")
        if m and m["status"] in core.MATCHED:
            movies.setdefault(m["tmdb_id"], it)
    core.job_step(job, "Finding collections", len(movies))
    by_collection, names, cid_names, failed = {}, {}, {}, []
    for tmdb_id, it in movies.items():
        if job["cancel"]:
            break
        core.set_detail(job, it["match"]["name"])
        try:
            found = collection_from_nfo(Path(root, it["path"]), library.own_folder(it, folders, root))
            cid, name = found if found else (None, "")
            if found and not cid and name:
                cid = names.get(name) or find_name(name)
                names[name] = cid
            if not found:
                c = movie_collection(tmdb_id)
                cid, name = c.get("id"), c.get("name", "")
            if cid:
                by_collection.setdefault(cid, []).append(it)
                cid_names.setdefault(cid, name)
        except core.TmdbError as err:
            failed.append({"name": it["match"]["name"], "error": str(err)})
        finally:
            core.tick(job)
    core.job_step(job, "Fetching collections", len(by_collection))
    today = datetime.date.today().isoformat()
    out = []
    for cid, owned in by_collection.items():
        core.tick(job)
        try:
            data = collection_data(cid)
        except core.TmdbError as err:
            # an id stored in an older .nfo can disappear from TMDB (collections get merged): try the name
            gone = "no movie with that ID" in str(err)
            alt = (find_name(cid_names[cid]) if cid_names.get(cid) else None) if gone else None
            if gone and not alt:  # ask TMDB which collection the movie belongs to now
                alt = movie_collection(owned[0]["match"]["tmdb_id"]).get("id")
                if not alt:
                    continue  # TMDB no longer groups it into a collection: nothing to report
            if not alt:
                failed.append({"name": cid_names.get(cid) or f"collection {cid}",
                               "error": "no longer on TMDB" if "no movie with that ID" in str(err) else str(err)})
                continue
            data = collection_data(alt)
        have_ids = {it["match"]["tmdb_id"] for it in owned}
        parts = sorted(data["parts"], key=lambda p: (p["release_date"] or "9999"))
        out.append({
            "id": cid, "name": data["name"], "overview": data["overview"], "poster": data["poster"],
            "have": [{"tmdb_id": p["tmdb_id"], "title": p["title"], "year": p["year"],
                      "item_id": next(it["id"] for it in owned if it["match"]["tmdb_id"] == p["tmdb_id"])}
                     for p in parts if p["tmdb_id"] in have_ids],
            "missing": [{"tmdb_id": p["tmdb_id"], "title": p["title"], "year": p["year"], "poster": p["poster"],
                         "released": bool(p["release_date"]) and p["release_date"] <= today}
                        for p in parts if p["tmdb_id"] not in have_ids],
        })
    out.sort(key=lambda c: core.norm_title(c["name"]))
    return {"collections": out, "movies": len(movies), "failed": failed, "cancelled": job["cancel"]}
