"""Bulk editing: change genres, tags, collection or age rating for many movies (or shows) at once.

Each change goes through the same path as editing one title: written into its .nfo (everything else kept),
locked for Jellyfin, and remembered so .nfo updates keep it.
"""
from . import core, movie, tvshow
from .core import UserError


def split(value) -> list:
    items = value if isinstance(value, list) else str(value or "").split(",")
    return [x.strip() for x in items if str(x).strip()]


def changed_list(current: list, mode: str, values: list) -> list:
    """'add' keeps what's there and adds; 'remove' takes away (ignoring case); 'set' replaces."""
    if mode == "set":
        return list(dict.fromkeys(values))
    low = {v.lower() for v in values}
    if mode == "remove":
        return [x for x in current if x.lower() not in low]
    have = {x.lower() for x in current}
    return current + [v for v in dict.fromkeys(values) if v.lower() not in have]


def plan(changes: dict) -> dict:
    """Clean up a bulk request: {genres: {mode, values}, tags: {...}, set: '...', mpaa: '...'}."""
    out = {}
    for field in ("genres", "tags"):
        c = changes.get(field) or {}
        values = split(c.get("values"))
        if c.get("mode") in ("add", "remove", "set") and (values or c.get("mode") == "set"):
            out[field] = (c["mode"], values)
    for field in ("set", "mpaa"):
        if field in changes and changes[field] is not None:
            out[field] = " ".join(str(changes[field]).split())
    if not out:
        raise UserError("Choose something to change.")
    return out


def run(job: dict, kind: str, ids: list, changes: dict) -> dict:
    todo = plan(changes)
    if kind == "tv":
        todo.pop("set", None)  # shows don't belong to collections
    core.job_step(job, "Editing", len(ids))
    res = {"edited": 0, "unchanged": 0, "skipped": [], "failed": []}
    for item_id in ids:
        if job["cancel"]:
            break
        try:
            d = tvshow.detail(item_id) if kind == "tv" else movie.detail(item_id)
            name = (d.get("info") or {}).get("title") or item_id
            core.set_detail(job, name)
            if not d.get("editable"):
                res["skipped"].append(name)
                continue
            info = d.get("info") or {}
            fields = {}
            for field, (mode, values) in ((k, v) for k, v in todo.items() if k in ("genres", "tags")):
                current = list(info.get(field) or [])
                new = changed_list(current, mode, values)
                if new != current:
                    fields[field] = new
            for field in ("set", "mpaa"):
                if field in todo and todo[field] != (info.get(field) or ""):
                    fields[field] = todo[field]
            if not fields:
                res["unchanged"] += 1
                continue
            if kind == "tv":
                tvshow.edit(item_id, fields)
            else:
                movie.edit(item_id, fields)
            res["edited"] += 1
        except (UserError, core.TmdbError, OSError) as err:
            res["failed"].append({"name": item_id, "error": getattr(err, "strerror", None) or str(err)})
        finally:
            core.tick(job)
    res["cancelled"] = job["cancel"]
    if kind == "tv":
        from . import tvlib
        res.update(tvlib.current())
    else:
        from . import library
        with core.lock:
            res["items"] = library.current_items()
    return res
