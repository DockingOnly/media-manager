"""History: what the app changed, task by task and click by click, newest first, with ways back.

Every move, every file written and everything sent to the Recycle Bin is noted while a task runs (core.note), and
each task or click that changed something becomes one entry. From the History window you can:
  * Restore what went to the Recycle Bin (while it's still there), to where it was;
  * Move back what a task moved (renames), if every file is still where the app put it and nothing is in the way.
Files written (.nfo, artwork, subtitles) are listed; they're replaced by running that task again, not undone here.
"""
import os
import threading
import time
from pathlib import Path

from . import core, recyclebin
from .core import UserError, key_of

KEEP = 1000
enabled = False  # the app and the command line switch it on (tests don't, unless they point HISTORY elsewhere)
_lock = threading.Lock()

JOB_TITLES = {"rename": "Renamed movies", "tvrename": "Renamed TV episodes", "nfo": "Wrote movie .nfo files",
              "tvnfo": "Wrote TV .nfo files", "artwork": "Downloaded movie artwork", "tvartwork": "Downloaded TV artwork",
              "trailers": "Downloaded movie trailers", "tvtrailers": "Downloaded TV trailers",
              "subtitles": "Downloaded movie subtitles", "tvsubtitles": "Downloaded TV subtitles",
              "bulk": "Edited several at once", "trakt": "Watched status from Trakt", "match": "Matched movies",
              "tvmatch": "Matched shows", "scan": "Scanned movies", "tvscan": "Scanned TV"}
PATH_TITLES = {"/api/delete": "Deleted a movie", "/api/tv/delete": "Deleted from a show", "/api/dupes/recycle": "Recycled duplicates",
               "/api/leftovers/recycle": "Tidied leftovers", "/api/rename/undo": "Undid a movie rename",
               "/api/tv/rename/undo": "Undid a TV rename", "/api/movie/edit": "Edited a movie", "/api/tv/edit": "Edited a show",
               "/api/artwork/set": "Changed a movie's artwork", "/api/tv/artwork/set": "Changed a show's artwork",
               "/api/history/move-back": "Moved back", "/api/history/reveal": "", "/api/history/restore": "Restored from the Recycle Bin",
               "/api/match": "Matched a movie", "/api/tv/match": "Matched a show"}


def job_title(kind: str) -> str:
    return JOB_TITLES.get(kind, kind.capitalize())


def path_title(path: str) -> str:
    return PATH_TITLES.get(path, "Changed files")


def _load() -> list:
    return core.load_json(core.HISTORY, [])


def add(entry: dict) -> None:
    if not enabled:
        return
    with _lock:
        log = _load()
        entry = {"id": f"h{time.time_ns()}", **entry}
        log.append(entry)
        core.save_json(core.HISTORY, log[-KEEP:])


def _summary(e: dict) -> dict:
    counts = {}
    for it in e["items"]:
        counts[it["do"]] = counts.get(it["do"], 0) + 1
    moved = [it for it in e["items"] if it["do"] == "moved"]
    binned = [it for it in e["items"] if it["do"] == "recycled"]
    return {**e, "counts": counts,
            "can_move_back": bool(moved) and not e.get("moved_back"),
            "restorable": sum(1 for it in binned if not it.get("restored"))}


def entries(limit: int = 100, before: str = None) -> dict:
    log = list(reversed(_load()))
    if before:
        ids = [e["id"] for e in log]
        log = log[ids.index(before) + 1:] if before in ids else []
    page = log[:limit]
    return {"entries": [_summary(e) for e in page], "more": len(log) > limit}


def _find(log: list, entry_id: str) -> dict:
    e = next((x for x in log if x["id"] == entry_id), None)
    if not e:
        raise UserError("That history entry is no longer there.")
    return e


def library_folders() -> list:
    cfg = core.load_config()
    out = []
    for many, one in (("folders", "folder"), ("tv_folders", "tv_folder")):
        out += cfg.get(many) or ([cfg[one]] if cfg.get(one) else [])
    return out


def reveal(path: str) -> None:
    """Open Explorer at a file (selected), or at the nearest folder that still exists. Only inside your libraries."""
    import subprocess
    p = Path(path)
    if not any(core.key_of(p) == core.key_of(f) or core.key_of(p).startswith(core.key_of(f).rstrip("\\/") + os.sep)
               for f in library_folders()):
        raise UserError("That's outside your library folders.")
    if os.name != "nt":
        raise UserError("Opening folders only works on Windows.")
    if p.exists():
        subprocess.Popen(["explorer", "/select,", str(p)])
        return
    while not p.exists() and p.parent != p:
        p = p.parent
    os.startfile(str(p))


def _refresh() -> None:
    """Pick up what moved, in whichever libraries are open."""
    from . import library, tvlib
    if core.state["root"] is not None:
        library.rescan()
    if core.tv_state["root"] is not None:
        tvlib.rescan()


def move_back(entry_id: str) -> dict:
    """Undo an entry's moves, last first, after checking every one can be undone."""
    from . import library, tvrename
    if core.job_running():
        raise UserError("Wait for the current task to finish first.")
    with core.lock, _lock:
        log = _load()
        e = _find(log, entry_id)
        if e.get("moved_back"):
            raise UserError("That was moved back already.")
        moves = [it for it in e["items"] if it["do"] == "moved"]
        if not moves:
            raise UserError("Nothing in that entry was moved.")
        # Check first: every file still where the app put it, and nothing where it's going back to
        gone = [m["to"] for m in moves if not os.path.lexists(m["to"])]
        taken = [m["path"] for m in moves if os.path.lexists(m["path"]) and not core.same_file(Path(m["path"]), Path(m["to"]))]
        if gone or taken:
            what = (f"{len(gone)} {'file is' if len(gone) == 1 else 'files are'} no longer where the app put "
                    f"{'it' if len(gone) == 1 else 'them'} (e.g. {gone[0]})" if gone else
                    f"something is already at {taken[0]}")
            raise UserError(f"Can't move back safely: {what}. Nothing was changed.")
        failed, touched = [], []
        for m in reversed(moves):
            try:
                core.move(Path(m["to"]), Path(m["path"]))
                touched.append(str(Path(m["to"]).parent))
            except OSError as err:
                failed.append({"path": m["to"], "error": getattr(err, "strerror", None) or str(err)})
        pairs = [(m["to"], m["path"]) for m in moves]
        library.remap_matches(pairs)  # your TMDB matches follow the files
        tv_root = core.tv_state["root"]
        shows = list(pairs)
        if tv_root is not None:  # a show folder that was renamed: its match follows too
            for new, old in pairs:
                try:
                    a, b = Path(new).relative_to(tv_root).parts[0], Path(old).relative_to(tv_root).parts[0]
                except (ValueError, IndexError):
                    continue
                if a != b:
                    shows.append((str(tv_root / a), str(tv_root / b)))
        tvrename.remap_shows(shows)
        for t in touched:  # folders the moves left empty
            core.remove_empty_dirs([t], Path(os.path.splitdrive(t)[0] + os.sep))
        done = {(key_of(m["path"]), key_of(m["to"])) for m in moves}
        for rename_log in (core.RENAME_LOG, core.TV_RENAME_LOG):  # the renamer's own Undo no longer applies to these
            batches = core.load_json(rename_log, [])
            keep = [b for b in batches if not {(key_of(o["src"]), key_of(o["dst"])) for o in b["ops"]} <= done]
            if len(keep) != len(batches):
                core.save_json(rename_log, keep)
        e["moved_back"] = time.time()
        core.save_json(core.HISTORY, log)
        _refresh()
    return {"moved": len(moves) - len(failed), "failed": failed}


def restore(entry_id: str, index: int = None) -> dict:
    """Bring back from the Recycle Bin what this entry sent there (all of it, or item `index`)."""
    if core.job_running():
        raise UserError("Wait for the current task to finish first.")
    with core.lock, _lock:
        log = _load()
        e = _find(log, entry_id)
        todo = [(i, it) for i, it in enumerate(e["items"]) if it["do"] == "recycled" and not it.get("restored")
                and (index is None or i == index)]
        if not todo:
            raise UserError("Nothing left to restore from that entry.")
        done, failed = [], []
        for i, it in todo:
            try:
                where = recyclebin.restore(it.get("binned") or it["path"], to=it["path"], near=e["time"])
                it["restored"] = time.time()
                core.note("restored", path=where)
                core.changed(where, note_as=None)
                done.append(where)
            except (OSError, UserError) as err:
                failed.append({"path": it["path"], "error": getattr(err, "strerror", None) or str(err)})
        core.save_json(core.HISTORY, log)
        _refresh()
    return {"restored": done, "failed": failed}
