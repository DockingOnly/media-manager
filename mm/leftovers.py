"""Leftovers: folders in the library that no longer hold a movie or episode (only artwork, .nfo, subtitles,
trailers, samples or tracker files, typically left behind by another tool's moves), and tracker junk files
('Torrent downloaded from ....txt'). Listed for you to check, then sent to the Recycle Bin.

Careful by design: a folder holding any video that isn't a trailer or sample, or any file of a kind not listed
below (an archive, a document, a disc image), is never offered.
"""
import os
import re
from pathlib import Path

from . import core, library

LEFTOVER_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tbn", ".nfo", ".txt", ".url", ".ini", ".db",
                 ".srt", ".sub", ".idx", ".ass", ".ssa", ".vtt", ".sfv", ".md5", ".nzb", ".torrent", ".log"}
JUNK_FILE = re.compile(r"(?i)^(?:torrent.*downloaded.*|.*\bdownloaded from\b.*|rarbg.*|.*\byts\b.*|www\..*|read ?this.*)\.(?:txt|url)$")
TRAILERISH = re.compile(r"(?i)(?:^|[\s._-])(?:trailer|sample)(?:[\s._-]|$)|^sample\b")


def _is_leftover_file(path: Path, rel_parts) -> bool:
    ext = path.suffix.lower()
    if ext in LEFTOVER_EXTS:
        return True
    if ext in core.VIDEO_EXTS:  # a trailer or sample clip, not a film
        return bool(TRAILERISH.search(path.stem)) or any(p.lower() in ("trailers", "samples", "sample") for p in rel_parts)
    return False


def _folder_report(folder: Path):
    """(size, what's inside) when everything in the folder is leftovers; None when it holds anything else."""
    size, kinds = 0, {}
    for dirpath, dirs, files in os.walk(folder):
        rel = Path(dirpath).relative_to(folder).parts
        if any(d.lower().endswith(".trickplay") for d in rel):  # Jellyfin's seek-bar previews: leftovers too
            for f in files:
                size += _size(Path(dirpath, f))
            if len(rel) == 1:
                kinds["Jellyfin preview set"] = kinds.get("Jellyfin preview set", 0) + 1
            continue
        for f in files:
            p = Path(dirpath, f)
            if not _is_leftover_file(p, rel):
                return None
            size += _size(p)
            kind = ("trailer/sample" if p.suffix.lower() in core.VIDEO_EXTS else "artwork" if p.suffix.lower() in
                    {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tbn"} else p.suffix.lower().lstrip("."))
            kinds[kind] = kinds.get(kind, 0) + 1
    return size, kinds


def _holds_video(folder: Path) -> bool:
    """Does the folder itself (not its sub-folders) hold a movie or episode?"""
    try:
        return any(e.is_file() and Path(e.name).suffix.lower() in core.VIDEO_EXTS and not TRAILERISH.search(Path(e.name).stem)
                   for e in os.scandir(folder))
    except OSError:
        return True


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def find(root: Path) -> dict:
    """Leftover folders (the highest one in each branch) and junk files outside them."""
    folders, junk = [], []
    taken = []

    def walk(d: Path):
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name.lower())
        except OSError:
            return
        for e in entries:
            if not e.is_dir(follow_symlinks=False) or e.name.startswith(library.SKIP_PREFIXES) or e.name.lower().endswith(".trickplay"):
                continue
            if e.name.lower() in library.SKIP_DIRS:  # Jellyfin's extras folders belong to the movie or show beside them
                continue
            p = Path(e.path)
            rep = _folder_report(p)
            if rep is not None:
                folders.append({"path": core.rel(p, root), "size": rep[0],
                                "inside": ", ".join(f"{n} {k}" for k, n in sorted(rep[1].items())) or "nothing (empty)"})
                taken.append(p)
            elif not _holds_video(p):  # a folder with a movie or episode in it: its sub-folders are its own
                walk(p)

    walk(root)
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if not x.startswith(library.SKIP_PREFIXES)
                   and not any(Path(dirpath, x) == t for t in taken)]
        for f in files:
            if JUNK_FILE.match(f):
                p = Path(dirpath, f)
                junk.append({"path": core.rel(p, root), "size": _size(p)})
    return {"folders": folders, "junk": junk}


def recycle(root: Path, paths: list) -> dict:
    """Send the chosen leftovers to the Recycle Bin, after checking each one is still a leftover."""
    now = find(root)
    allowed = {x["path"] for x in now["folders"]} | {x["path"] for x in now["junk"]}
    done, failed = [], []
    for rel in paths:
        if rel not in allowed:
            failed.append({"path": rel, "error": "No longer a leftover (it changed, or holds a video now); left alone."})
            continue
        try:
            core.discard(Path(root, rel))
            done.append(rel)
        except (OSError, core.UserError) as err:
            failed.append({"path": rel, "error": str(err)})
    return {"recycled": done, "failed": failed, **find(root)}
