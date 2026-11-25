"""Renaming TV for Jellyfin: Show (Year)\\Season 1\\Show - S01E02 - Title.mkv, with preview and undo.

Files that are already structured right (right folder, starting 'Show - S01E02') keep their names, even when
TMDB words the episode title differently, so a library named by another tool isn't churned for nothing.
"""
import os
import time
from pathlib import Path

from . import core, nfo, tvlib, tvmeta
from .core import key_of, rel, same_file

MAX_TITLE = 90  # keeps paths well inside Windows' limits


def episode_stem(title: str, season: int, episodes: list, data) -> str:
    names = [((tvmeta.episode(data, season, n) or {}).get("name") or "").strip() for n in episodes]
    names = [n for n in names if n]
    ep_title = core.safe_name(" & ".join(dict.fromkeys(names))) if names else ""
    if len(ep_title) > MAX_TITLE:
        ep_title = ep_title[:MAX_TITLE].rsplit(" ", 1)[0].rstrip(" -&,")
    stem = f"{title} - {tvlib.episode_code(season, episodes)}"
    return f"{stem} - {ep_title}" if ep_title else stem


def plan() -> dict:
    root = core.require_tv_scan()
    items = tvlib.annotate(tvlib.with_matches(core.tv_state["items"], root), root)
    plans, claimed, left_only = [], {}, []
    for key, eps in tvlib.group(items).items():
        m = eps[0]["match"]
        if not tvlib.confirmed(m):
            continue
        edits = nfo.load_edits(f"tv:{m['tmdb_id']}")
        folder_name, title = tvlib.show_name_parts(m, edits)
        cur = root / eps[0]["show_dir"] if eps[0].get("show_dir") else None
        rename_show = cur is not None and not tvlib.same_name(cur.name, folder_name)
        target = cur if cur is not None and not rename_show else root / folder_name
        p = {"id": key, "name": folder_name, "ops": [], "reason": None, "renamed_show": None, "left_alone": []}
        if rename_show:
            if target.exists() and not same_file(target, cur):
                p["reason"] = f"A folder named “{folder_name}” already exists."
            p["ops"].append({"kind": "folder", "src": str(cur), "dst": str(target)})
            p["renamed_show"] = [str(cur), str(target)]

        def here(path: Path) -> Path:  # where a file sits once the show folder has its new name
            return target / path.relative_to(cur) if rename_show else path

        data = None
        used = {}
        for it in sorted(eps, key=lambda i: i["path"]):
            if it.get("mismatch"):
                if not it["named"]:
                    p["left_alone"].append(Path(it["path"]).name)
                continue
            if it["kind"] != "episode" or it["named"]:
                if it["kind"] == "episode":
                    used.setdefault(key_of(Path(root, it["path"]).parent), set()).add(Path(it["path"]).stem.lower())
                continue
            video = Path(root, it["path"])
            season_dir = target / tvlib.season_folder(it["season"])
            moved = here(video)
            if key_of(moved.parent) == key_of(season_dir) and tvlib.starts_right(moved.stem, title, it["season"], it["episodes"]):
                used.setdefault(key_of(season_dir), set()).add(moved.stem.lower())
                continue  # only the show folder's name was wrong; this file is fine inside it
            if data is None:
                data = tvmeta.show_data(m["tmdb_id"])
            stem = episode_stem(title, it["season"], it["episodes"], data)
            taken = used.setdefault(key_of(season_dir), set())
            if stem.lower() in taken:  # two files of the same episode
                n = 2
                while f"{stem} ({n})".lower() in taken:
                    n += 1
                stem = f"{stem} ({n})"
            taken.add(stem.lower())
            for f in [video] + tvlib.sidecars(video):
                src = here(f)
                dst = season_dir / (stem + f.name[len(video.stem):])
                if key_of(src) == key_of(dst):
                    continue
                if dst.exists() and not same_file(dst, src if src.exists() else f):
                    p["reason"] = p["reason"] or f"“{rel(dst, root)}” already exists."
                if key_of(dst) in claimed:
                    p["reason"] = f"Two files would both become “{rel(dst, root)}”."
                claimed[key_of(dst)] = True
                p["ops"].append({"kind": "file", "src": str(src), "dst": str(dst)})
        if p["ops"]:
            plans.append(p)
        elif p["left_alone"]:
            left_only.append({"name": folder_name, "left_alone": p["left_alone"]})
    view = [{"id": p["id"], "name": p["name"], "reason": p["reason"], "left_alone": p["left_alone"],
             "changes": [{"kind": op["kind"], "from": rel(Path(op["src"]), root), "to": rel(Path(op["dst"]), root)}
                         for op in p["ops"]]} for p in plans]
    already = sum(1 for it in items if it["kind"] == "episode" and it["named"])
    unmatched = len({it["key"] for it in items if not tvlib.confirmed(it["match"])})
    return {"plans": plans, "view": view, "already": already, "unmatched": unmatched, "left_only": left_only}


def preview() -> dict:
    with core.lock:
        tvlib.rescan()
        p = plan()
    return {"plans": p["view"], "already": p["already"], "unmatched": p["unmatched"], "left_only": p["left_only"],
            "can_undo": bool(core.load_json(core.TV_RENAME_LOG, []))}


def remove_empty_up(paths, stop: Path) -> None:
    """Remove folders left empty, and their parents up to (not including) the TV folder."""
    for p in sorted({Path(x) for x in paths}, key=lambda x: len(str(x)), reverse=True):
        while p != stop and stop in p.parents:
            try:
                if p.is_dir() and not any(p.iterdir()):
                    p.rmdir()
                    p = p.parent
                    continue
            except OSError:
                pass
            break


def remap_shows(pairs) -> None:
    matches = core.load_json(core.TV_MATCHES, {})
    for old, new in pairs:
        if key_of(old) in matches:
            matches[key_of(new)] = matches.pop(key_of(old))
    core.save_json(core.TV_MATCHES, matches)


def apply(job: dict, wanted: set, root: Path) -> dict:
    with core.lock:
        tvlib.rescan(job, "Checking files")
        plans = [p for p in plan()["plans"] if p["id"] in wanted and not p["reason"]]
        core.job_step(job, "Renaming", len(plans))
        done, failed, touched, shows = [], [], [], []
        for p in plans:
            core.tick(job)
            core.set_detail(job, p["name"])
            undo_point = len(done)
            try:
                for op in p["ops"]:
                    core.move(Path(op["src"]), Path(op["dst"]))
                    done.append(op)
                    touched.append(str(Path(op["src"]).parent))
                if p["renamed_show"]:
                    shows.append(p["renamed_show"])
            except OSError as err:
                for op in reversed(done[undo_point:]):
                    try:
                        os.rename(op["dst"], op["src"])
                    except OSError:
                        pass
                del done[undo_point:]
                failed.append({"name": p["name"], "error": getattr(err, "strerror", None) or str(err)})
        remap_shows(shows)
        remove_empty_up(touched, root)
        if done:
            log = core.load_json(core.TV_RENAME_LOG, [])
            log.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "ops": done, "shows": shows})
            core.save_json(core.TV_RENAME_LOG, log[-20:])
        tvlib.rescan(job, "Refreshing the list")
    return {**tvlib.current(), "renamed": len(plans) - len(failed), "failed": failed}


def undo() -> dict:
    with core.lock:
        root = core.require_tv_scan()
        log = core.load_json(core.TV_RENAME_LOG, [])
        if not log:
            raise core.UserError("There is no rename to undo.")
        batch = log.pop()
        failed, touched = 0, []
        for op in reversed(batch["ops"]):
            try:
                core.move(Path(op["dst"]), Path(op["src"]))
                touched.append(str(Path(op["dst"]).parent))
            except OSError:
                failed += 1
        remap_shows([(new, old) for old, new in batch["shows"]])
        remove_empty_up(touched, root)
        core.save_json(core.TV_RENAME_LOG, log)
        tvlib.rescan()
    return {**tvlib.current(), "undone": len(batch["ops"]) - failed, "failed": failed, "can_undo": bool(log)}
