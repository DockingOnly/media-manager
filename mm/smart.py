"""Smart collections: Jellyfin collections that follow a rule, such as "res:4k hdr:yes" or "rating:8 genre:crime", and
optionally a TMDB list (a top 250, a director's films...). The rule uses the same words and fields as the filter box.

The app creates each collection in Jellyfin and keeps what's in it matching the rule: when you save it, when you
choose Update now, and after the folder watcher has handled new files (once Jellyfin has them). Only collections
made here are ever changed; the collections Jellyfin makes from .nfo files (a movie's TMDB collection) are left
alone. Removing a smart collection removes the collection in Jellyfin, never any of the movies or shows in it.
Each gets a poster made from the posters of its first four movies or shows.
"""
import base64
import io
import re
import threading
import time
import urllib.parse
from pathlib import Path

from . import core, jellyfin, query
from .core import UserError, key_of

PRESETS = [
    {"name": "4K HDR", "kind": "movies", "query": "res:4k hdr:yes"},
    {"name": "Dolby Atmos", "kind": "movies", "query": "audio:atmos"},
    {"name": "Highly rated", "kind": "movies", "query": "rating:8"},
    {"name": "Under 90 minutes", "kind": "movies", "query": "runtime:<90"},
    {"name": "Added this month", "kind": "movies", "query": "added:30d"},
    {"name": "Not watched yet", "kind": "movies", "query": "watched:no"},
    {"name": "The 80s", "kind": "movies", "query": "year:1980-1989"},
    {"name": "Animated shows", "kind": "tv", "query": "genre:animation"},
]
CHUNK = 40          # ids per request (they go in the address)
RESYNC_AFTER = 6 * 3600
_lock = threading.Lock()
last = {"time": 0.0, "waiting": False, "error": None}


def _load() -> list:
    return core.load_json(core.SMART, [])


def _save(items: list) -> None:
    core.save_json(core.SMART, items)


def listing() -> list:
    return _load()


# ---------------------------------------------------------------- what the library holds

def rows(kind: str) -> list:
    """Every movie (or show) in all your library folders of that kind, as the table shows them, each with 'full'
    (its file or show folder) for finding it in Jellyfin."""
    cfg = core.load_config()
    tv = kind == "tv"
    folders = cfg.get("tv_folders" if tv else "folders") or ([cfg.get("tv_folder" if tv else "folder")] if cfg.get("tv_folder" if tv else "folder") else [])
    out = []
    for folder in folders:
        root = Path(folder)
        if not root.is_dir():
            continue
        with core.lock:
            st = core.tv_state if tv else core.state
            items = list(st["items"]) if st["root"] is not None and key_of(st["root"]) == key_of(root) else core.load_scan(kind, root).get("items", [])
            items = [dict(i) for i in items]
            if tv:
                from . import tvlib
                for r in tvlib.summaries(tvlib.annotate(tvlib.with_matches(items, root), root), root):
                    if r["folder"] != "(loose files)":
                        r["full"] = str(root / r["folder"])
                        out.append(r)
            else:
                from . import library
                for r in library.annotate(library.with_matches(items), root):
                    r["full"] = str(root / r["path"])
                    r["root"] = str(root)
                    out.append(r)
    return out


def tmdb_list_id(text: str) -> str:
    """'https://www.themoviedb.org/list/8136-imdb-top-250' or '8136' -> '8136'."""
    text = (text or "").strip()
    if not text:
        return ""
    m = re.search(r"(?:/list/)?(\d+)", text)
    if not m:
        raise UserError("Paste the address of a TMDB list, such as https://www.themoviedb.org/list/8136.")
    return m.group(1)


def tmdb_list(list_id: str, kind: str) -> set:
    from . import tmdbapi
    ids, page, pages = set(), 1, 1
    while page <= min(pages, 30):
        d = tmdbapi.tmdb(f"/list/{list_id}", page=page)
        for it in d.get("items") or []:
            if (it.get("media_type") or "movie") == ("tv" if kind == "tv" else "movie"):
                ids.add(it.get("id"))
        pages = d.get("total_pages") or 1
        page += 1
    return ids


def members(c: dict, all_rows: list = None) -> list:
    """The movies or shows the rule picks, best rated first."""
    rs = rows(c["kind"]) if all_rows is None else all_rows
    picked = query.select(rs, c.get("query") or "", c["kind"]) if (c.get("query") or "").strip() else list(rs)
    if c.get("tmdb_list"):
        wanted = tmdb_list(c["tmdb_list"], c["kind"])
        picked = [r for r in picked if query._ok(r.get("match")) and r["match"].get("tmdb_id") in wanted]
    elif not (c.get("query") or "").strip():
        raise UserError("Write a rule (such as res:4k hdr:yes) or add a TMDB list.")
    if c["kind"] != "tv":
        seen, unique = set(), []
        for r in picked:  # one entry per movie, even with several copies or parts
            k = (r.get("match") or {}).get("tmdb_id") if query._ok(r.get("match")) else r["id"]
            if k not in seen:
                seen.add(k)
                unique.append(r)
        picked = unique
    picked.sort(key=lambda r: (-((r.get("info") or {}).get("rating") or 0), str(r.get("title"))))
    return picked


def name_of(r: dict) -> str:
    m = r.get("match") or {}
    if query._ok(m) and m.get("name"):
        return m["name"]
    return f"{r.get('title')} ({r['year']})" if r.get("year") else str(r.get("title"))


def preview(kind: str, rule: str, list_text: str = "") -> dict:
    if kind not in ("movies", "tv"):
        raise UserError("Choose movies or shows.")
    c = {"kind": kind, "query": rule or "", "tmdb_list": tmdb_list_id(list_text)}
    got = members(c)
    qs = query.parse(rule or "", kind)
    return {"count": len(got), "names": [name_of(r) for r in got[:40]], "unknown": qs["unknown"]}


# ---------------------------------------------------------------- Jellyfin

def _norm(p) -> str:
    return (p or "").replace("\\", "/").rstrip("/").lower()


def jellyfin_items(kind: str, user: dict) -> dict:
    """Jellyfin's movies (or shows): their path -> id."""
    q = urllib.parse.urlencode({"Recursive": "true", "IncludeItemTypes": "Series" if kind == "tv" else "Movie", "Fields": "Path",
                                "CollapseBoxSetItems": "false"})  # (or movies in a collection come back as the collection)
    items = (jellyfin.request("GET", f"/Users/{user['Id']}/Items?{q}", timeout=60) or {}).get("Items") or []
    return {_norm(i.get("Path")): i["Id"] for i in items if i.get("Path")}


def jellyfin_id(r: dict, kind: str, paths: dict):
    want = _norm(jellyfin.to_jellyfin(r["full"]))
    if want in paths:
        return paths[want]
    if kind != "tv":  # another version or part of a movie Jellyfin lists under its first file
        folder = want.rsplit("/", 1)[0]
        same = [i for p, i in paths.items() if p.rsplit("/", 1)[0] == folder]
        if len(set(same)) == 1:
            return same[0]
    return None


def _boxsets(user: dict) -> dict:
    q = urllib.parse.urlencode({"Recursive": "true", "IncludeItemTypes": "BoxSet"})
    return {i["Id"]: i.get("Name") or "" for i in (jellyfin.request("GET", f"/Users/{user['Id']}/Items?{q}") or {}).get("Items") or []}


def _children(user: dict, coll_id: str) -> set:
    q = urllib.parse.urlencode({"ParentId": coll_id})
    return {i["Id"] for i in (jellyfin.request("GET", f"/Users/{user['Id']}/Items?{q}", timeout=60) or {}).get("Items") or []}


def _in_chunks(ids):
    ids = sorted(ids)
    for i in range(0, len(ids), CHUNK):
        yield ",".join(ids[i:i + CHUNK])


def poster(c: dict, picked: list) -> bytes:
    """A poster for the collection: the first four posters in a grid, with its name across the bottom."""
    from PIL import Image, ImageDraw
    from . import badges
    W, H = 1000, 1500
    img = Image.new("RGB", (W, H), (24, 26, 31))
    files = [p for p in (badges.poster_of(r, c["kind"]) for r in picked) if p][:4]
    if not files:
        return b""
    cells = [(0, 0, W, H)] if len(files) == 1 else [(0, 0, W // 2, H // 2), (W // 2, 0, W, H // 2), (0, H // 2, W // 2, H), (W // 2, H // 2, W, H)]
    for i, f in enumerate(files):
        x0, y0, x1, y1 = cells[i]
        try:
            with Image.open(f) as im:
                im = im.convert("RGB")
                scale = max((x1 - x0) / im.width, (y1 - y0) / im.height)
                im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
                left, top = (im.width - (x1 - x0)) // 2, (im.height - (y1 - y0)) // 2
                img.paste(im.crop((left, top, left + x1 - x0, top + y1 - y0)), (x0, y0))
        except OSError:
            continue
    if len(files) in (2, 3):  # fill the empty cells
        for i in range(len(files), 4):
            x0, y0, x1, y1 = cells[i]
            ImageDraw.Draw(img).rectangle((x0, y0, x1, y1), fill=(34, 37, 44))
    band = Image.new("RGBA", (W, 300), (0, 0, 0, 0))
    bd = ImageDraw.Draw(band)
    for y in range(300):
        bd.line((0, y, W, y), fill=(10, 11, 14, int(235 * min(1, y / 140))))
    img.paste(band, (0, H - 300), band)
    d = ImageDraw.Draw(img)
    name = c["name"]
    size = 104
    font = badges.font(size)
    while d.textlength(name, font=font) > W - 100 and size > 44:
        size -= 6
        font = badges.font(size)
    tw = d.textlength(name, font=font)
    d.text(((W - tw) / 2, H - 70), name, font=font, fill=(255, 255, 255), anchor="ls")
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)
    return out.getvalue()


def sync(c: dict, user: dict = None, picked: list = None) -> dict:
    """Make the Jellyfin collection hold exactly what the rule picks (of what Jellyfin has). Updates `c`."""
    user = user or jellyfin.admin_user()
    picked = members(c) if picked is None else picked
    paths = jellyfin_items(c["kind"], user)
    want, waiting = set(), []
    for r in picked:
        jid = jellyfin_id(r, c["kind"], paths)
        if jid:
            want.add(jid)
        else:
            waiting.append(name_of(r))
    sets = _boxsets(user)
    clash = next((i for i, n in sets.items() if n.lower() == c["name"].lower() and i != c.get("jellyfin_id")), None)
    added = removed = 0
    if not c.get("jellyfin_id") or c["jellyfin_id"] not in sets:
        if clash:
            raise UserError(f"Jellyfin already has a collection called “{c['name']}” that wasn't made here. Choose another name.")
        first = sorted(want)[:CHUNK]
        q = urllib.parse.urlencode({"Name": c["name"], "IsLocked": "true", **({"Ids": ",".join(first)} if first else {})})
        made = jellyfin.request("POST", f"/Collections?{q}") or {}
        if not made.get("Id"):
            raise UserError("Jellyfin didn't create the collection.")
        c["jellyfin_id"], c["poster_sig"] = made["Id"], None
        have = set(first)
        added = len(first)
    else:
        if sets[c["jellyfin_id"]] != c["name"]:
            if clash:
                raise UserError(f"Jellyfin already has a collection called “{c['name']}”. Choose another name.")
            item = jellyfin.request("GET", f"/Users/{user['Id']}/Items/{c['jellyfin_id']}") or {}
            if item:
                item["Name"] = c["name"]
                jellyfin.request("POST", f"/Items/{c['jellyfin_id']}", item)
        have = _children(user, c["jellyfin_id"])
    for ids in _in_chunks(want - have):
        jellyfin.request("POST", f"/Collections/{c['jellyfin_id']}/Items?Ids={ids}")
    for ids in _in_chunks(have - want):
        jellyfin.request("DELETE", f"/Collections/{c['jellyfin_id']}/Items?Ids={ids}")
    added += len(want - have)
    removed = len(have - want)
    if c.get("poster", True):
        sig = "|".join(name_of(r) for r in picked[:4])
        if sig and sig != c.get("poster_sig"):
            try:
                img = poster(c, picked)
                if img:
                    jellyfin.request("POST", f"/Items/{c['jellyfin_id']}/Images/Primary", raw=base64.b64encode(img), content_type="image/jpeg", timeout=60)
                    c["poster_sig"] = sig
            except (ImportError, OSError, UserError):
                pass
    c.update(synced=time.time(), count=len(want), waiting=len(waiting), error=None)
    return {"in": len(want), "added": added, "removed": removed, "waiting": waiting[:20], "waiting_count": len(waiting)}


# ---------------------------------------------------------------- your list of smart collections

def clean(data: dict) -> dict:
    name = re.sub(r"\s+", " ", (data.get("name") or "")).strip()
    if not name:
        raise UserError("Give the collection a name.")
    if len(name) > 80:
        raise UserError("Keep the name under 80 characters.")
    kind = data.get("kind")
    if kind not in ("movies", "tv"):
        raise UserError("Choose movies or shows.")
    rule = (data.get("query") or "").strip()
    if len(rule) > 500:
        raise UserError("That rule is too long.")
    unknown = query.parse(rule, kind)["unknown"]
    if unknown:
        raise UserError(f"“{unknown[0]}:” isn't a filter field. The ? beside the filter box lists them.")
    return {"name": name, "kind": kind, "query": rule, "tmdb_list": tmdb_list_id(data.get("tmdb_list") or ""),
            "auto": bool(data.get("auto", True)), "poster": bool(data.get("poster", True))}


def save(data: dict) -> dict:
    fields = clean(data)
    if not jellyfin.configured():
        raise UserError("Add your Jellyfin address and API key in Settings first.")
    with _lock:
        items = _load()
        cid = data.get("id")
        c = next((x for x in items if x["id"] == cid), None) if cid else None
        if cid and c is None:
            raise UserError("That smart collection is no longer there.")
        if any(x["name"].lower() == fields["name"].lower() and x is not c for x in items):
            raise UserError(f"You already have a smart collection called “{fields['name']}”.")
        if c is None:
            c = {"id": f"s{time.time_ns()}", "created": time.time()}
            items.append(c)
        elif (c.get("query"), c.get("tmdb_list")) != (fields["query"], fields["tmdb_list"]):
            c["poster_sig"] = None
        c.update(fields)
        result = sync(c)
        _save(items)
    return {"collection": c, "result": result, "collections": items}


def delete(cid: str) -> list:
    with _lock:
        items = _load()
        c = next((x for x in items if x["id"] == cid), None)
        if c is None:
            raise UserError("That smart collection is no longer there.")
        if c.get("jellyfin_id") and jellyfin.configured():
            try:
                jellyfin.request("DELETE", f"/Items/{c['jellyfin_id']}")
            except jellyfin.NotFound:
                pass  # already gone in Jellyfin
        items.remove(c)
        _save(items)
    return items


def sync_all(only_auto: bool = False) -> dict:
    if not jellyfin.configured():
        raise UserError("Add your Jellyfin address and API key in Settings first.")
    with _lock:
        items = _load()
        todo = [c for c in items if c.get("auto", True) or not only_auto]
        if not todo:
            return {"collections": items, "results": {}}
        user = jellyfin.admin_user()
        cache, results = {}, {}
        for c in todo:
            if c["kind"] not in cache:
                cache[c["kind"]] = rows(c["kind"])
            try:
                results[c["id"]] = sync(c, user, members(c, cache[c["kind"]]))
            except (UserError, core.TmdbError) as err:
                c["error"] = str(err)
                results[c["id"]] = {"error": str(err)}
        _save(items)
        last.update(time=time.time(), waiting=any(c.get("waiting") for c in todo), error=None)
    return {"collections": items, "results": results}


def maybe_sync(handled: list) -> None:
    """After the folder watcher: keep the automatic collections up to date (once Jellyfin has the new files)."""
    if not jellyfin.configured() or not any(c.get("auto", True) for c in _load()):
        return
    now = time.time()
    due = bool(handled) or (last["waiting"] and now - last["time"] > 120) or now - last["time"] > RESYNC_AFTER
    if not due:
        return
    if handled:
        last.update(waiting=True, time=now)  # Jellyfin takes a minute or so to add new files: look again next time
        return
    try:
        sync_all(only_auto=True)
    except (UserError, core.TmdbError, OSError) as err:
        last.update(time=now, error=str(err))
