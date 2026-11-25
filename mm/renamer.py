"""Renaming for Jellyfin: 'Title (Year)\\Title (Year).mkv', with a preview of every change and undo."""
import os
import re
import time
from pathlib import Path

from . import core, library
from .core import key_of, rel, same_file


def version_stems(group: list, videos: list, names) -> list:
    """The new file name (without extension) for each file of one movie. Parts get '-part1'; several versions
    get ' - 1080p', ' - 720p'; two versions with the same resolution get ' (2)'. A file whose name already
    fits keeps it, so copies never swap names."""
    names = names if isinstance(names, list) else [names] * len(group)  # each file's name from the pattern
    by_resolution = "{resolution}" in core.pattern("movie_file")  # versions already differ by resolution
    unparted = [it for it in group if not it["part"]]
    bases = []
    for it, name in zip(group, names):
        if it["part"]:
            bases.append(f"{name}-part{it['part']}")
        elif len(unparted) > 1 and not by_resolution:
            bases.append(f"{name} - {it['resolution'] or 'version'}")
        else:
            bases.append(name)
    stems, taken = [None] * len(group), set()
    for i, video in enumerate(videos):  # first: keep names that already fit
        m = re.fullmatch(re.escape(bases[i]) + r"(?: \((\d+)\))?", video.stem, re.I)
        if m:
            stem = bases[i] + (f" ({m.group(1)})" if m.group(1) else "")
            if stem.lower() not in taken:
                stems[i] = stem
                taken.add(stem.lower())
        elif len(unparted) > 1 and not group[i]["part"] and not by_resolution:
            # your own label for a version ('Star Wars (1977) - Theatrical', '... - Special Edition'): kept
            label = re.fullmatch(re.escape(names[i]) + r" - (?!part\d)(.+)", video.stem, re.I)
            placeholder = label and re.fullmatch(r"(?i)(?:\d{3,4}[pi]|4k|uhd|[hs]d|version|other|copy|alt|\d+)(?: \(\d+\))?", label.group(1).strip())
            if label and not placeholder and video.stem.lower() not in taken:
                stems[i] = video.stem
                taken.add(video.stem.lower())
    for i in range(len(group)):  # then: everything else gets the next free name
        if stems[i] is None:
            stem, n = bases[i], 2
            while stem.lower() in taken:
                stem, n = f"{bases[i]} ({n})", n + 1
            stems[i] = stem
            taken.add(stem.lower())
    return stems


def rename_plan() -> dict:
    """Work out every rename without touching any files."""
    root = core.require_scan()
    items = library.with_matches(core.state["items"])
    folders = library.by_folder(items, root)
    groups = {}
    for it in items:
        m = it["match"]
        if m and m["status"] in core.MATCHED:
            groups.setdefault(m["tmdb_id"], []).append(it)

    plans, claimed = [], {}
    cfg = core.load_config()
    for tmdb_id, group in groups.items():
        name = library.movie_names(group[0]["match"], group[0], cfg)[0]  # the folder name
        file_names = [library.movie_names(it["match"], it, cfg)[1] for it in group]
        videos = [Path(root, it["path"]) for it in group]
        parent = videos[0].parent
        own = (len({key_of(v.parent) for v in videos}) == 1
               and library.own_folder(group[0], folders, root, confirmed=True))
        plan = {"id": str(tmdb_id), "name": name, "ops": [], "videos": [], "reason": None}
        binned = set()
        outer = parent.parent

        if own and parent.name == name and outer != root and outer.name == name and not folders.get(key_of(outer)):
            # "Name\Name\movie.mkv" (left by an older version of this app): move everything up one level.
            # Older copies of the same files in the outer folder go to the Recycle Bin.
            target_dir = here_dir = outer
            for entry in sorted(parent.iterdir()):
                dst = outer / entry.name
                if dst.exists():
                    plan["ops"].append({"kind": "bin", "src": str(dst)})
                    binned.add(key_of(dst))
                plan["ops"].append({"kind": "entry", "src": str(entry), "dst": str(dst)})
        elif own:
            target_dir = here_dir = outer / name
            if parent.name != name:
                if target_dir.exists() and not same_file(target_dir, parent):
                    plan["reason"] = f"A folder named “{name}” already exists there."
                plan["ops"].append({"kind": "folder", "src": str(parent), "dst": str(target_dir)})
        else:
            # Shared folder: give the movie its own folder, unless this folder already has the right name
            target_dir = parent if parent.name == name else parent / name
            here_dir = None

        spread = sorted({rel(v.parent, root) or "(the library folder)" for v in videos})
        if len(spread) > 1:
            # Files matched to one movie but sitting in different folders: usually a wrong match (a sequel
            # matched as the first film). Moving them together would pull a film out of its own folder.
            plan["reason"] = ("Its files are in different folders (" + "; ".join(spread) + "). Check each one is really "
                              "this movie (open it and use Change match if not); if they are, put them in one folder first.")

        stems = version_stems(group, videos, file_names)
        for it, video, stem in zip(group, videos, stems):
            dst = target_dir / (stem + video.suffix)
            plan["videos"].append({"src": str(video), "dst": str(dst)})
            for f in [video] + library.sidecars(video):
                f_here = here_dir / f.name if here_dir else f  # where the file sits after any folder move
                f_dst = target_dir / (stem + f.name[len(video.stem):])
                if f_here.name == f_dst.name and key_of(f_here.parent) == key_of(f_dst.parent):
                    continue
                if any(library.skip_dir(part) for part in f_dst.relative_to(root).parts[:-1]):
                    plan["reason"] = f"“{rel(f_dst, root)}” is inside a hidden or extras folder."
                if key_of(f_dst) not in binned and f_dst.exists() and not same_file(f_dst, f_here if f_here.exists() else f):
                    plan["reason"] = plan["reason"] or f"“{rel(f_dst, root)}” already exists."
                plan["ops"].append({"kind": "file", "src": str(f_here), "dst": str(f_dst)})
            if key_of(dst) in claimed:
                plan["reason"] = f"Another file is already being renamed to “{rel(dst, root)}”."
                claimed[key_of(dst)]["reason"] = plan["reason"]
            claimed[key_of(dst)] = plan

        if plan["ops"]:
            plans.append(plan)

    view = []
    for p in plans:
        changes = []
        for op in p["ops"]:
            if op["kind"] == "bin":
                changes.append({"kind": "bin", "from": rel(Path(op["src"]), root), "to": "Recycle Bin (an older copy)"})
            else:
                changes.append({"kind": "folder" if op["kind"] in ("folder", "entry") and Path(op["src"]).is_dir() else "file",
                                "from": rel(Path(op["src"]), root), "to": rel(Path(op["dst"]), root)})
        view.append({"id": p["id"], "name": p["name"], "changes": changes, "reason": p["reason"]})
    unmatched = sum(1 for it in items if not (it["match"] and it["match"]["status"] in core.MATCHED))
    done = sum(len(g) for g in groups.values()) - sum(len(p["videos"]) for p in plans)
    return {"plans": plans, "view": view, "unmatched": unmatched, "already": max(done, 0)}


def preview() -> dict:
    with core.lock:
        library.rescan()
        plan = rename_plan()
    return {"plans": plan["view"], "unmatched": plan["unmatched"], "already": plan["already"],
            "can_undo": bool(core.load_json(core.RENAME_LOG, []))}


def apply(job: dict, wanted: set, root: Path) -> dict:
    with core.lock:
        library.rescan(job, "Checking files")
        plans = [p for p in rename_plan()["plans"] if p["id"] in wanted and not p["reason"]]
        core.job_step(job, "Renaming", len(plans))
        done_ops, moved, failed, touched = [], [], [], []
        for p in plans:
            core.tick(job)
            undo_point = len(done_ops)
            try:
                for op in p["ops"]:
                    if op["kind"] == "bin":
                        core.discard(Path(op["src"]))
                    else:
                        core.move(Path(op["src"]), Path(op["dst"]))
                        touched.append(str(Path(op["src"]).parent))
                    done_ops.append(op)
                moved.extend((v["src"], v["dst"]) for v in p["videos"])
            except OSError as err:
                for op in reversed(done_ops[undo_point:]):  # put this movie back the way it was
                    if op["kind"] != "bin":  # (anything sent to the Recycle Bin can be restored from there)
                        try:
                            os.rename(op["dst"], op["src"])
                        except OSError:
                            pass
                del done_ops[undo_point:]
                failed.append({"name": p["name"], "error": getattr(err, "strerror", None) or str(err)})
        library.remap_matches(moved)
        core.remove_empty_dirs(touched, root)
        moves = [op for op in done_ops if op["kind"] != "bin"]
        if moves:
            log = core.load_json(core.RENAME_LOG, [])
            log.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "ops": moves, "videos": moved})
            core.save_json(core.RENAME_LOG, log[-20:])
        items = [dict(i) for i in library.rescan(job, "Refreshing the list")]
    return {"items": items, "renamed": len(plans) - len(failed), "failed": failed}


def undo() -> dict:
    with core.lock:
        root = core.require_scan()
        log = core.load_json(core.RENAME_LOG, [])
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
        library.remap_matches([(new, old) for old, new in batch["videos"]])
        core.remove_empty_dirs(touched, root)
        core.save_json(core.RENAME_LOG, log)
        items = [dict(i) for i in library.rescan()]
    return {"items": items, "undone": len(batch["ops"]) - failed, "failed": failed, "can_undo": bool(log)}
