"""Renaming TV for Jellyfin: Show (Year)\\Season 1\\Show - S01E02 - Title.mkv, with preview and undo.

Files that are already structured right (right folder, starting 'Show - S01E02') keep their names, even when
TMDB words the episode title differently, so a library named by another tool isn't churned for nothing.
"""
import os
import time
from pathlib import Path

from . import core, nfo, tvlib, tvmeta, tvsort
from .core import key_of, rel, same_file

MAX_TITLE = 90  # keeps paths well inside Windows' limits


def episode_stem(title: str, season: int, episodes: list, data, resolution: str = "") -> str:
    """The episode's new file name (without extension), from the episode pattern."""
    names = [((tvmeta.episode(data, season, n) or {}).get("name") or "").strip() for n in episodes]
    names = [n for n in names if n]
    ep_title = core.safe_name(" & ".join(dict.fromkeys(names))) if names else ""
    if len(ep_title) > MAX_TITLE:
        ep_title = ep_title[:MAX_TITLE].rsplit(" ", 1)[0].rstrip(" -&,")
    return core.render(core.pattern("tv_episode_file"), tvlib.episode_values(title, season, episodes, ep_title, resolution))


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
        sorted_ids = sort_extras(eps, m, title, target, here, root, claimed, p, used)
        for it in sorted(eps, key=lambda i: i["path"]):
            if it["id"] in sorted_ids:
                continue
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
            if key_of(moved.parent) == key_of(season_dir) and tvlib.starts_right(moved.stem, title, it["season"], it["episodes"], it.get("resolution", "")):
                used.setdefault(key_of(season_dir), set()).add(moved.stem.lower())
                continue  # only the show folder's name was wrong; this file is fine inside it
            if data is None:
                data = tvmeta.data_for(m)
            stem = episode_stem(title, it["season"], it["episodes"], data, it.get("resolution", ""))
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
             "changes": [{"kind": op["kind"], "from": rel(Path(op["src"]), root),
                          "to": "Recycle Bin (made for the wrong episode)" if op["kind"] == "bin" else rel(Path(op["dst"]), root)}
                         for op in p["ops"]]} for p in plans]
    already = sum(1 for it in items if it["kind"] == "episode" and it["named"])
    unmatched = len({it["key"] for it in items if not tvlib.confirmed(it["match"])})
    return {"plans": plans, "view": view, "already": already, "unmatched": unmatched, "left_only": left_only}


def sort_extras(eps, m, title, target, here, root, claimed, p, used) -> set:
    """Specials and extras (see tvsort): adds their moves to the show's plan; returns the files it took care of.

    Only files that are clearly not episodes are touched: those whose title is one of TMDB's specials, those in
    an extras folder ('ATHF Xtras'), and those numbered like an episode that's already here under its right
    title (an extra that was given 'S01E03' when the real S01E03 sits beside it). Episodes that are simply
    numbered differently (TVDB vs TMDB) are still left alone."""
    agree = {(e["season"], n) for e in eps if e["kind"] == "episode" and not e.get("mismatch") for n in e["episodes"]}
    picked = [it for it in eps if (it["kind"] == "episode" and it.get("mismatch"))
              or (it["kind"] == "extra" and tvsort.in_extras_folder(Path(it["path"]).parts))]
    if not picked:
        return set()
    data = tvmeta.data_for(m)
    candidates = []
    for it in sorted(picked, key=lambda i: i["path"]):
        parts = Path(it["path"]).parts
        if it["kind"] == "episode":
            text = it.get("ep_title") or Path(it["path"]).stem
            # numbered like an episode that's already here under its right title, and its own title isn't
            # that episode's (so not a second copy of the episode, or a two-episode file like S07E01 S07E12)
            dup = all((it["season"], n) in agree for n in it["episodes"]) and not (it.get("ep_title") and tvlib.title_agrees(it, data))
        else:
            text, dup = Path(it["path"]).stem, True
        candidates.append((it, text, tvsort.extra_title(text, title), dup or tvsort.in_extras_folder(parts)))
    regular = {tvsort.compact(e["name"]) for s, es in (data or {}).get("episodes", {}).items() if s != "0" for e in es}
    done, extras = set(), []
    for it, text, name, sure in candidates:
        special = tvsort.special_for([name, tvsort.bare(text, title)], data) if tvsort.compact(name) not in regular else None
        if special:
            stem = episode_stem(title, 0, [special["episode"]], data, it.get("resolution", ""))
            if add_moves(it, target / tvlib.season_folder(0), stem, here, root, claimed, p, used):
                done.add(it["id"])
        elif sure and name:
            extras.append((it, name))
    for (it, name), (folder, file_title) in zip(extras, tvsort.place([n for _, n in extras])):
        stem = core.safe_name(file_title)[:MAX_TITLE].rstrip(" -&,") or "Extra"
        if add_moves(it, target / folder, stem, here, root, claimed, p, used):
            done.add(it["id"])
    return done


def add_moves(it, folder: Path, stem: str, here, root, claimed, p, used) -> bool:
    """Move a file (and its subtitles, .nfo, thumbnail and trailer) to folder/stem; a second file wanting the
    same name gets ' (2)'."""
    video = Path(root, it["path"])
    taken = used.setdefault(key_of(folder), set())
    base, n = stem, 2
    while stem.lower() in taken:
        stem, n = f"{base} ({n})", n + 1
    taken.add(stem.lower())
    for f in [video] + tvlib.sidecars(video):
        src = here(f)
        tail = f.name[len(video.stem):].lower()
        if f.is_file() and (tail == ".nfo" or tail.startswith("-thumb.")):
            # written for the episode number the file wrongly had (S01E03's title and picture): not this file's
            p["ops"].append({"kind": "bin", "src": str(src)})
            continue
        dst = folder / (stem + f.name[len(video.stem):])
        if key_of(src) == key_of(dst):
            continue
        if dst.exists() and not same_file(dst, src if src.exists() else f):
            p["reason"] = p["reason"] or f"“{rel(dst, root)}” already exists."
        if key_of(dst) in claimed:
            p["reason"] = f"Two files would both become “{rel(dst, root)}”."
        claimed[key_of(dst)] = True
        p["ops"].append({"kind": "file", "src": str(src), "dst": str(dst)})
    return True


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
                    touched.append(str(Path(op["src"]).parent))
                    if op["kind"] == "bin":  # (restorable from the Recycle Bin; the app writes fresh ones)
                        core.discard(Path(op["src"]))
                        continue
                    core.move(Path(op["src"]), Path(op["dst"]))
                    done.append(op)
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
