"""Duplicates: the same movie, or the same episode, more than once in the library.

Each copy is read with ffmpeg (resolution, HDR, length, audio) and shown side by side. The weaker copies are
ticked for the Recycle Bin only when that is clearly safe: the copies run the same length, none is labelled
as a version you keep on purpose (Theatrical, Extended, ...), the match is confirmed and, for episodes, every
copy holds exactly the same episodes. Otherwise nothing in that group is ticked and the reason is shown.
The last copy of anything is never recycled.
"""
import re
from pathlib import Path

from . import core, library, mediainfo, tvlib
from .core import UserError, key_of

RES_RANK = {"2160p": 5, "1080p": 4, "720p": 3, "576p": 2, "480p": 1}
CUT_WORDS = re.compile(r"(?i)\b(extended|director'?s[ ._-]?cut|unrated|uncut|theatrical|special[ ._-]edition|"
                       r"remastered|imax|ultimate[ ._-]cut|final[ ._-]cut|redux|despecialized)\b")
PLACEHOLDER = re.compile(r"(?i)(?:\d{3,4}[pi]|4k|uhd|[hs]d|version|other|copy|alt|\d+)(?: \(\d+\))?")
SAME_LENGTH = 300  # seconds: copies further apart than 5 minutes may be different cuts (or different films)


# ---------------------------------------------------------------- grouping (cheap: names and matches only)

def _movie_groups(root: Path) -> list:
    """[(title, [copy, ...])] for every confirmed TMDB match with more than one copy. A copy is one file, or all
    the parts of a split movie in one folder."""
    by_id = {}
    for it in library.with_matches(core.state["items"]):
        m = it.get("match") or {}
        if m.get("status") in core.MATCHED and m.get("tmdb_id"):
            by_id.setdefault(m["tmdb_id"], []).append(it)
    out = []
    for files in by_id.values():
        copies = {}
        for it in sorted(files, key=lambda x: (x["path"].lower(), x.get("part") or 0)):
            folder = key_of(Path(root, it["path"]).parent)
            copies.setdefault((folder, "parts") if it.get("part") else it["id"], []).append(it)
        if len(copies) > 1:
            m = files[0]["match"]
            title = m.get("name") or (f"{m['title']} ({m['year']})" if m.get("year") else m.get("title")) or files[0]["title"]
            out.append((title, list(copies.values())))
    return out


def _tv_groups(root: Path) -> list:
    """[(title, [copy, ...])]: files of one show that hold the same episode (S01E02 twice, or S01E02 and S01E01-E02)."""
    items = [it for it in tvlib.with_matches(core.tv_state["items"], root) if it["kind"] == "episode"]
    parent = {it["id"]: it["id"] for it in items}

    def top(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    first = {}
    for it in items:
        for n in it["episodes"]:
            k = (it["key"], it["season"], n)
            if k in first:
                parent[top(it["id"])] = top(first[k])
            else:
                first[k] = it["id"]
    sets = {}
    for it in items:
        sets.setdefault(top(it["id"]), []).append(it)
    out = []
    for files in sets.values():
        if len(files) < 2:
            continue
        files.sort(key=lambda x: x["path"].lower())
        show = files[0]["series"] or files[0].get("show_dir") or "?"
        codes = [tvlib.episode_code(s, [n]) for s, n in sorted({(f["season"], n) for f in files for n in f["episodes"]})]
        out.append((f"{show} {', '.join(codes[:4])}{' …' if len(codes) > 4 else ''}", [[f] for f in files]))
    return out


def _groups(kind: str):
    if kind == "tv":
        root = core.require_tv_scan()
        return root, _tv_groups(root)
    root = core.require_scan()
    return root, _movie_groups(root)


# ---------------------------------------------------------------- what each copy is

def _label(kind: str, it: dict, video: Path) -> str:
    """A version you keep on purpose: 'Star Wars (1977) - Theatrical', 'Aliens.1986.Extended.1080p'."""
    cut = CUT_WORDS.search(video.stem)
    if cut:
        return re.sub(r"[._]", " ", cut.group(1)).title()
    if kind == "movies":
        try:
            _, file_name = library.movie_names(it["match"], it)
        except Exception:
            return ""
        m = re.fullmatch(re.escape(file_name) + r" - (?!part\d)(.+)", video.stem, re.I)
        if m and not PLACEHOLDER.fullmatch(m.group(1).strip()):
            return m.group(1).strip()
    return ""


def _describe(kind: str, root: Path, files: list) -> dict:
    videos = [Path(root, f["path"]) for f in files]
    infos = [mediainfo.probe(v) for v in videos]
    info = infos[0] or {}
    v = info.get("video") or {}
    resolution = library.resolution_of(v["width"], v["height"]) if v.get("width") and v.get("height") else files[0].get("resolution", "")
    durations = [i.get("duration") for i in infos if i]
    duration = sum(durations) if len(durations) == len(infos) and all(durations) else None
    summary = mediainfo.summary(info) if info else {}
    return {
        "id": files[0]["id"], "ids": [f["id"] for f in files], "paths": [core.rel(p, root) for p in videos],
        "size": sum(f["size"] for f in files), "resolution": resolution,
        "codec": (v.get("codec") or files[0].get("codec") or "").upper(),
        "hdr": {"hdr10": "HDR10", "hlg": "HLG", "dolbyvision": "Dolby Vision"}.get(v.get("hdr"), ""),
        "duration": duration, "audio": summary.get("audio", ""), "readable": all(infos),
        "label": _label(kind, files[0], videos[0]), "parts": len(files) if files[0].get("part") else 0,
        "episodes": tvlib.episode_code(files[0]["season"], files[0]["episodes"]) if kind == "tv" else "",
        "mismatch": bool(files[0].get("mismatch")),
    }


def _rank(c: dict):
    return (RES_RANK.get(c["resolution"], 0), bool(c["hdr"]), c["size"])


def _suggest(kind: str, copies: list, confirmed: bool) -> str:
    """Tick the weaker copies when it's clearly safe; otherwise return why nothing is ticked."""
    for c in copies:
        c["keep"], c["suggest"] = False, False
    why = ""
    labels = [c["label"] for c in copies if c["label"]]
    lengths = [c["duration"] for c in copies]
    if not all(c["readable"] for c in copies):
        why = "A copy couldn't be read, so they can't be compared. Check them yourself."
    elif labels:
        why = f"Labelled as versions you may keep on purpose ({', '.join(labels)})."
    elif kind == "tv" and len({c["episodes"] for c in copies}) > 1:
        why = "The files hold different episodes, so recycling one could lose an episode. Check them yourself."
    elif None in lengths:
        why = "The length of a copy is unknown. Check them yourself."
    elif max(lengths) - min(lengths) > SAME_LENGTH:
        why = ("Lengths differ by more than 5 minutes: they may be different cuts, or not the same "
               f"{'episode' if kind == 'tv' else 'film'}. Check them yourself.")
    elif kind == "tv" and any(c["mismatch"] for c in copies):
        why = "A file's name doesn't fit TMDB's episode list, so it may be a different episode or an extra."
    elif not confirmed:
        why = "The show isn't matched on TMDB (confirmed) yet."
    if not why:  # the best copy stays; the rest are ticked
        best = max(copies, key=_rank)
        best["keep"] = True
        for c in copies:
            c["suggest"] = c is not best
    return why


def find(job: dict, kind: str) -> dict:
    root, groups = _groups(kind)
    if kind == "tv":  # names that don't fit TMDB's list
        tvlib.annotate([f for _, copies in groups for c in copies for f in c], root)
    core.job_step(job, "Reading the copies", sum(len(c) for _, copies in groups for c in copies))
    out = []
    for title, copies in groups:
        core.set_detail(job, title)
        described = []
        for c in copies:
            described.append(_describe(kind, root, c))
            core.tick(job, len(c))
        confirmed = kind == "movies" or tvlib.confirmed(copies[0][0].get("match"))
        why = _suggest(kind, described, confirmed)
        out.append({"title": title, "copies": sorted(described, key=_rank, reverse=True), "note": why})
    out.sort(key=lambda g: g["title"].lower())
    return {"kind": kind, "groups": out}


# ---------------------------------------------------------------- recycling

def _own_files(kind: str, video: Path) -> list:
    """The video and its own subtitles, .nfo, artwork and previews. When another copy in the same folder has the
    same name ('Movie.mkv' and 'Movie.avi'), those files are shared, so only the video goes."""
    try:
        twins = [e for e in video.parent.iterdir() if e != video and e.is_file() and e.stem == video.stem
                 and e.suffix.lower() in core.VIDEO_EXTS]
    except OSError:
        twins = []
    if twins:
        return [video]
    return [video] + (tvlib.sidecars(video) if kind == "tv" else library.sidecars(video))


def recycle(kind: str, copy_ids: list) -> dict:
    """Send the chosen copies to the Recycle Bin. Each must still be a duplicate, and every group keeps a copy."""
    if core.job_running():
        raise UserError("Wait for the current task to finish first.")
    chosen = set(copy_ids)
    with core.lock:
        root, groups = _groups(kind)
        copies = {c[0]["id"]: (title, c, group) for title, group in groups for c in group}
        done, failed, gone = [], [], []
        for cid in copy_ids:
            if cid not in copies:
                failed.append({"path": cid, "error": "No longer a duplicate (it changed since the list was made); left alone."})
                continue
            title, files, group = copies[cid]
            if all(c[0]["id"] in chosen for c in group):
                failed.append({"path": core.rel(Path(root, files[0]["path"]), root),
                               "error": f"That's every copy of {title}; one is always kept."})
                continue
            for f in files:
                video = Path(root, f["path"])
                try:
                    if not video.exists():
                        raise UserError("The file is no longer there.")
                    for target in _own_files(kind, video):
                        core.discard(target)
                    done.append(f["path"])
                    gone.append(f["id"])
                except (OSError, UserError) as err:
                    failed.append({"path": f["path"], "error": getattr(err, "strerror", None) or str(err)})
        if kind == "tv":
            tvlib.rescan()
            library_now = tvlib.current()
        else:
            if gone:
                matches = core.load_json(core.MATCHES, {})
                for gid in gone:
                    matches.pop(gid, None)
                core.save_json(core.MATCHES, matches)
            library.rescan()
            library_now = {"items": library.current_items()}
    return {"recycled": done, "failed": failed, **library_now}
