"""The library: finding video files, reading their names, what each movie has, and the shared folder rules."""
import os
import re
from pathlib import Path

from guessit import guessit

from . import core
from .core import key_of, norm_title

SKIP_FILE = re.compile(r"(^|[\s._-])(sample|trailer)([\s._-]|$)", re.I)
# Jellyfin treats these folder names as extras, not movies
SKIP_DIRS = {"extras", "trailers", "featurettes", "behind the scenes", "deleted scenes",
             "interviews", "scenes", "shorts", "other", "sample"}
# Hidden, system and trash folders: tinyMediaManager's ".deletedByTMM", "$RECYCLE.BIN", NAS "@eaDir", ...
SKIP_PREFIXES = (".", "$", "@", "#")
SKIP_SYSTEM = {"system volume information", "lost+found"}


def skip_dir(name: str) -> bool:
    low = name.lower()
    return low in SKIP_DIRS or low in SKIP_SYSTEM or name.startswith(SKIP_PREFIXES)


# Files next to a movie that share its name and belong to it
SIDECAR_EXTS = core.SUB_EXTS | {".nfo", ".jpg", ".jpeg", ".png", ".webp"}

PARSE_VERSION = 3  # bump when parsing changes, so saved scans are re-read
GUESS_OPTS = {"type": "movie", "excludes": ["part"]}  # "Part 2" in a title is a sequel, not disc 2
SPLIT_PART = re.compile(r"(?P<sep>\s-\s|-|[._ ])(?:cd|dvd|disc|disk|part|pt)[ ._-]?(?P<n>\d{1,2})$", re.I)
VIDEO_STREAM = re.compile(r"Stream #\d+:\d+[^\n]*?: Video: (\w+)[^\n]*?, (\d{2,5})x(\d{2,5})")
CODEC_NAMES = {"h264": "H.264", "hevc": "H.265", "av1": "AV1", "vp9": "VP9", "mpeg4": "MPEG-4",
               "mpeg2video": "MPEG-2", "vc1": "VC-1", "msmpeg4v3": "DivX", "wmv3": "WMV"}


# ---------------------------------------------------------------- reading names

def split_part(stem: str):
    """Disc/part number of a movie split over several files: 'Movie (2010)-part1', 'Movie.2010.cd2'.
    Needs a hyphen before it or a year earlier in the name, so 'Friday the 13th Part 2' stays a title."""
    m = SPLIT_PART.search(stem)
    if m and (m.group("sep").strip() == "-" or re.search(r"(19|20)\d{2}", stem[:m.start()])):
        return int(m.group("n"))
    return None


def parse(path: Path, root: Path) -> dict:
    guess = guessit(path.name, GUESS_OPTS)
    title, year = core.first(guess.get("title")), core.first(guess.get("year"))
    if not (title and year) and path.parent != root:  # fall back to the folder name
        folder = guessit(path.parent.name, GUESS_OPTS)
        title = title or core.first(folder.get("title"))
        year = year or core.first(folder.get("year"))
    part = core.first(guess.get("cd")) or split_part(path.stem)
    return {
        "title": str(title) if title else path.stem,
        "year": int(year) if year else None,
        "resolution": str(core.first(guess.get("screen_size")) or ""),
        "source": str(core.first(guess.get("source")) or ""),
        "codec": str(core.first(guess.get("video_codec")) or ""),
        "part": int(part) if isinstance(part, int) or str(part).isdigit() else None,
    }


def resolution_of(width: int, height: int) -> str:
    """Name a picture size the usual way; width counts too, so letterboxed 1920x800 is still 1080p."""
    if width >= 3200 or height >= 1800:
        return "2160p"
    if width >= 1800 or height >= 1000:
        return "1080p"
    if width >= 1200 or height >= 700:
        return "720p"
    if width >= 960 or height >= 540:
        return "576p"
    if width >= 600 or height >= 400:
        return "480p"
    return f"{height}p"


def probe_video(path: Path) -> dict:
    """Read the real resolution and codec from the video itself (for files whose names don't say)."""
    m = VIDEO_STREAM.search(core.ffmpeg_info(path, timeout=30))
    if not m:
        return {}
    codec, width, height = m.group(1), int(m.group(2)), int(m.group(3))
    return {"resolution": resolution_of(width, height), "codec": CODEC_NAMES.get(codec, codec.upper())}


# ---------------------------------------------------------------- scanning

def scan(root: Path, job: dict = None, label: str = "Reading file names") -> list:
    videos = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not skip_dir(d)]
        for name in files:
            path = Path(dirpath) / name
            if path.suffix.lower() in core.VIDEO_EXTS and not SKIP_FILE.search(path.stem):
                videos.append(path)
    if job:
        core.job_step(job, label, len(videos))
    # Reuse what we worked out last time for files that haven't changed; only new or changed files are read
    cache = core.load_scan("movies", root)
    known = {it["id"]: it for it in cache.get("items", [])} if cache.get("root") == key_of(root) else {}
    if cache.get("v") != PARSE_VERSION:  # parsing improved: re-read every name once (matches are kept)
        known = {k: {kk: vv for kk, vv in v.items() if kk in ("size", "mtime")} for k, v in known.items()}
    items = []
    for path in videos:
        try:
            st = path.stat()
        except OSError:
            continue
        finally:
            if job:
                core.tick(job)
        old = known.get(key_of(path))
        if old and "title" in old and old.get("size") == st.st_size and old.get("mtime") == int(st.st_mtime):
            item = {k: v for k, v in old.items() if k not in ("match", "files", "named")}
        else:
            item = parse(path, root)
        if not item.get("probed"):  # ask the video itself (once per file): release names can be wrong
            info = probe_video(path)  # ('Star Wars 360p' that is really 640x272)
            if info.get("resolution"):
                if item["resolution"] and item["resolution"] != info["resolution"]:
                    item["name_resolution"] = item["resolution"]
                item["resolution"] = info["resolution"]
            item["codec"] = info.get("codec") or item["codec"]
            item["probed"] = True
        item["size"] = st.st_size
        item["mtime"] = int(st.st_mtime)
        item["path"] = str(path.relative_to(root))
        item["id"] = key_of(path)
        items.append(item)
    # Safeguard: a file is only "part N" of a split movie when the same folder holds at least two parts
    # of that movie. A lone "part 2" is a sequel or a stray name, and must never be renamed as a part.
    parts = {}
    for it in items:
        if it["part"]:
            parent = key_of(Path(root, it["path"]).parent)
            parts.setdefault((parent, norm_title(it["title"]), it["year"]), []).append(it)
    for group in parts.values():
        if len(group) < 2:
            for it in group:
                it["part"] = None
    follow_renames(known, items)
    try:
        core.save_scan("movies", root, {"root": key_of(root), "folder": str(root), "items": items, "v": PARSE_VERSION})
    except OSError:
        pass
    return [dict(it) for it in items]


def follow_renames(known: dict, items: list) -> None:
    """A file that vanished and one that appeared with the same size and timestamp were renamed or moved
    (in Explorer, say). Carry the TMDB match over so the movie stays matched."""
    now = {it["id"] for it in items}
    gone, new = {}, {}
    for old_id, old in known.items():
        if old_id not in now:
            gone.setdefault((old.get("size"), old.get("mtime")), []).append(old_id)
    for it in items:
        if it["id"] not in known:
            new.setdefault((it["size"], it["mtime"]), []).append(it["id"])
    pairs = [(gone[k][0], ids[0]) for k, ids in new.items() if len(ids) == 1 and len(gone.get(k, [])) == 1]
    if pairs:
        with core.lock:
            matches = core.load_json(core.MATCHES, {})
            moved = [(o, n) for o, n in pairs if o in matches and n not in matches]
            for o, n in moved:
                matches[n] = matches.pop(o)
            if moved:
                core.save_json(core.MATCHES, matches)


def with_matches(items: list) -> list:
    matches = core.load_json(core.MATCHES, {})
    for item in items:
        item["match"] = matches.get(item["id"])
    return items


def remap_matches(pairs) -> None:
    matches = core.load_json(core.MATCHES, {})
    for old, new in pairs:
        if key_of(old) in matches:
            matches[key_of(new)] = matches.pop(key_of(old))
    core.save_json(core.MATCHES, matches)


def rescan(job: dict = None, label: str = "Reading file names") -> list:
    """Scan the current library folder again, then attach matches and what each movie has."""
    root = core.state["root"]
    if root is None:
        return []
    core.state["items"] = scan(root, job, label)
    return annotate(with_matches(core.state["items"]), root)


def current_items() -> list:
    """The library as last scanned, with matches and file status, as copies safe to send to the page."""
    root = core.state["root"]
    return [dict(it) for it in annotate(with_matches(core.state["items"]), root)] if root else []


# ---------------------------------------------------------------- the shared folder rules

def by_folder(items: list, root: Path) -> dict:
    out = {}
    for it in items:
        out.setdefault(key_of(Path(root, it["path"]).parent), []).append(it)
    return out


def same_movie(a: dict, b: dict, confirmed: bool = False) -> bool:
    """Are two files the same movie (another version, or another part)?"""
    if a["id"] == b["id"]:
        return True
    ma, mb = a.get("match") or {}, b.get("match") or {}
    if confirmed:  # the renamer only moves files whose matches you've confirmed
        return (ma.get("status") in core.MATCHED and mb.get("status") in core.MATCHED
                and ma.get("tmdb_id") == mb.get("tmdb_id"))
    if ma.get("tmdb_id") or mb.get("tmdb_id"):
        return ma.get("tmdb_id") == mb.get("tmdb_id")
    return norm_title(a["title"]) == norm_title(b["title"]) and a["year"] == b["year"]  # both unmatched


def own_folder(item: dict, folders: dict, root: Path, confirmed: bool = False) -> bool:
    """Does this movie have a folder to itself? Only then may the folder be renamed, deleted as a whole,
    or hold plain 'poster.jpg' and 'trailers' (which would otherwise be claimed by several movies)."""
    folder = Path(root, item["path"]).parent
    return folder != root and all(same_movie(item, s, confirmed) for s in folders.get(key_of(folder), []))


def sidecars(video: Path) -> list:
    """Files in the same folder named like the video, such as 'Movie.en.srt' or 'Movie-poster.jpg', and Jellyfin's
    'Movie.trickplay' folder."""
    out = []
    try:
        entries = list(video.parent.iterdir())
    except OSError:
        return out
    # Another version whose name starts with this one ("Movie - 1080p (2)" vs "Movie - 1080p") owns its own files
    longer = [e.stem for e in entries if e != video and e.is_file() and e.suffix.lower() in core.VIDEO_EXTS
              and len(e.stem) > len(video.stem) and e.stem.startswith(video.stem) and not SKIP_FILE.search(e.stem)]
    for f in entries:
        if f.is_dir() and f.name.lower() == f"{video.stem}.trickplay".lower():
            out.append(f)  # Jellyfin's seek-bar previews for this video: they move with it, or Jellyfin rebuilds them
            continue
        if not (f.is_file() and f != video and f.name.startswith(video.stem) and f.name[len(video.stem)] in ".-"):
            continue
        if any(f.name.startswith(o) for o in longer):
            continue
        rest = f.name[len(video.stem):].lower()
        # subtitles, .nfo and images, plus a downloaded "<movie>-trailer.mkv"
        if f.suffix.lower() in SIDECAR_EXTS or (f.suffix.lower() in core.VIDEO_EXTS and rest.startswith("-trailer")):
            out.append(f)
    return out


# ---------------------------------------------------------------- what each movie has

def has_videos(folder: Path) -> bool:
    try:
        return any(e.is_file() and Path(e.name).suffix.lower() in core.VIDEO_EXTS for e in os.scandir(folder))
    except OSError:
        return False


def has_trailer(folder: Path, names: dict, stem: str, own: bool) -> bool:
    """'<movie>-trailer.mkv' beside it; or, in its own folder, any '-trailer' file or a 'trailers' folder with a video."""
    flat = tuple(f"-trailer{e}" for e in core.VIDEO_EXTS)
    if any(n.startswith(f"{stem}-trailer.") and n.endswith(flat) for n in names):
        return True
    if not own:
        return False
    return any(n.endswith(flat) for n in names) or (names.get("trailers") is True and has_videos(folder / "trailers"))


def sub_languages(names: dict, stem: str) -> list:
    """'Movie (2001).en.srt' -> 'en'; 'Movie (2001).srt' (no language given) -> '?'. Only what follows the
    movie's own name counts, so a dot in the title ('Dr. Strangelove') isn't mistaken for a language."""
    langs = set()
    for n in names:
        if n.startswith(stem + ".") and Path(n).suffix in core.SUB_EXTS:
            rest = n[len(stem) + 1:]
            langs.add(rest.split(".")[0] if "." in rest else "?")
    return sorted(langs)


def movie_names(match: dict, item: dict, cfg: dict = None):
    """A movie's folder name and file name (before any version or part suffix), from the rename patterns.
    Your edited title and year win over TMDB's."""
    from . import nfo
    cfg = core.load_config() if cfg is None else cfg
    edits = nfo.load_edits(match["tmdb_id"])
    values = {"title": edits.get("title") or match["title"], "year": edits.get("year") or match.get("year"),
              "resolution": item.get("resolution", ""), "codec": item.get("codec", ""), "source": item.get("source", ""),
              "tmdb": match["tmdb_id"]}
    return core.render(core.pattern("movie_folder", cfg), values), core.render(core.pattern("movie_file", cfg), values)


def annotate(items: list, root: Path) -> list:
    """Mark each file with what Jellyfin will find: .nfo, artwork, trailer, subtitles, and correct naming."""
    folders = by_folder(items, root)
    listings = {}
    cfg = core.load_config()
    for it in items:
        video = Path(root, it["path"])
        folder = video.parent
        names = listings.get(key_of(folder))
        if names is None:
            try:
                names = {e.name.lower(): e.is_dir() for e in os.scandir(folder)}
            except OSError:
                names = {}
            listings[key_of(folder)] = names
        own = own_folder(it, folders, root)
        stem = video.stem.lower()
        it["files"] = {
            "nfo": f"{stem}.nfo" in names or (own and "movie.nfo" in names),
            "art": any(n in names for n in (f"{stem}-poster.jpg", f"{stem}-poster.png"))
                   or (own and any(n in names for n in ("poster.jpg", "poster.png", "folder.jpg", "cover.jpg"))),
            "trailer": has_trailer(folder, names, stem, own),
            "subs": sub_languages(names, stem),
        }
        m = it.get("match") or {}
        it["named"] = False
        if m.get("status") in core.MATCHED:
            # Named for Jellyfin: in its folder ("Title (Year)" or your pattern), with a file name that starts right
            folder_name, file_name = movie_names(m, it, cfg)
            it["named"] = folder.name == folder_name and video.stem.startswith(file_name)
    return items


# ---------------------------------------------------------------- deleting

def folder_size(files) -> int:
    total = 0
    for f in files:
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total


def delete_plan(item_id: str) -> dict:
    root = core.require_scan()
    items = with_matches(core.state["items"])
    it = next((x for x in items if x["id"] == item_id), None)
    if not it:
        raise core.UserError("That file is no longer in the library. Scan again.")
    video = Path(root, it["path"])
    if not video.exists():
        raise core.UserError("That file no longer exists. Scan again to update the list.")
    folders = by_folder(items, root)
    own = own_folder(it, folders, root)  # the whole folder can go only when it holds nothing but this movie
    return {
        "item": it, "video": video, "files": [video] + sidecars(video), "own": own, "folder": video.parent,
        "folder_files": [f for f in video.parent.rglob("*") if f.is_file()] if own else [],
        "siblings": folders.get(key_of(video.parent), []), "name": (it["match"] or {}).get("name") or it["title"],
    }


def delete(item_id: str, mode: str) -> dict:
    if mode not in ("files", "folder"):
        raise core.UserError("Choose what to delete.")
    if core.job_running():
        raise core.UserError("Wait for the current task to finish before deleting.")
    with core.lock:
        p = delete_plan(item_id)
        if mode == "folder" and not p["own"]:
            raise core.UserError("This folder also holds other movies, so only the movie's own files can be deleted.")
        permanent = core.is_network_path(p["video"])
        targets = [p["folder"]] if mode == "folder" else p["files"]
        gone_ids = [x["id"] for x in p["siblings"]] if mode == "folder" else [p["item"]["id"]]
        failed = []
        for target in targets:
            try:
                core.discard(target)
            except core.UserError:
                raise
            except Exception as err:
                failed.append({"name": target.name, "error": getattr(err, "strerror", None) or str(err)})
        if not failed:
            matches = core.load_json(core.MATCHES, {})
            for gid in gone_ids:
                matches.pop(gid, None)
            core.save_json(core.MATCHES, matches)
        items = [dict(i) for i in rescan()]
    return {"items": items, "name": p["name"], "removed": len(targets) - len(failed), "failed": failed,
            "permanent": permanent, "mode": mode}
