"""Shared foundation: where data lives, settings, background tasks with progress, and safe file helpers."""
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # the app folder
CONFIG = BASE / "config.json"          # folder, API keys, language (keep this file private)
MATCHES = BASE / "matches.json"        # file path -> TMDB match
RENAME_LOG = BASE / "rename_log.json"  # rename batches, for undo
LIBRARY = BASE / "library.json"        # the last scan, so the library opens instantly next time
EDITS = BASE / "edits.json"            # your own metadata edits, by TMDB id (they survive .nfo rewrites)
MOVIE_CACHE = BASE / "movie_cache.json"  # which collection each movie belongs to (from TMDB)
COLLECTIONS = BASE / "collections.json"  # collection details from TMDB
TV_LIBRARY = BASE / "tv_library.json"  # the last TV scan
TV_MATCHES = BASE / "tv_matches.json"  # show folder -> TMDB show
TV_CACHE = BASE / "tv_cache.json"      # TMDB show and episode details, refreshed weekly
TV_RENAME_LOG = BASE / "tv_rename_log.json"
HISTORY = BASE / "history.json"       # what the app changed, newest last (the History window)
PROBES = BASE / "probe_cache.json"      # what ffmpeg said about each video (so it's read once, not every time)
PORT = 8765
USER_AGENT = "media-manager/0.8"

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts", ".mpg", ".mpeg", ".webm"}
SUB_EXTS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt"}
MATCHED = {"auto", "manual"}  # statuses that count as a confirmed match
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # don't flash a console window for ffmpeg

lock = threading.RLock()            # guards the library and the JSON files
state = {"root": None, "items": []}  # the last movie scan
tv_state = {"root": None, "items": []}  # the last TV scan


class UserError(Exception):
    """Something the person can fix; shown as-is."""


class TmdbError(Exception):
    pass


class SubError(Exception):
    pass


class QuotaError(SubError):
    pass


# ---------------------------------------------------------------- storage

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


_read_cache = {}


def read_json(path: Path, default):
    """load_json for read-only use: parsed once, and again only when the file changes. Callers must not
    modify what it returns (the TV cache is read hundreds of times while the TV list is built)."""
    try:
        st = path.stat()
    except OSError:
        return default
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _read_cache.get(str(path))
    if hit and hit[0] == stamp:
        return hit[1]
    data = load_json(path, default)
    _read_cache[str(path)] = (stamp, data)
    return data


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_config() -> dict:
    return load_json(CONFIG, {})


def save_config(**changes) -> dict:
    with lock:
        cfg = load_config()
        cfg.update(changes)
        save_json(CONFIG, cfg)
        return cfg


# ---------------------------------------------------------------- names and paths

def key_of(path) -> str:
    """How a file is identified: its full path, case-insensitive on Windows."""
    return os.path.normcase(str(path))


def rel(path: Path, root: Path) -> str:
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return str(path)


def first(value):
    return value[0] if isinstance(value, list) and value else value


def safe_name(text: str) -> str:
    """Make a title safe for Windows file names: 'Mission: Impossible' -> 'Mission - Impossible'."""
    text = re.sub(r"\s*:\s*", " - ", text)
    text = re.sub(r"[\\/]", "-", text)
    text = re.sub(r'[<>"|?*\x00-\x1f]', "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.rstrip(". ") or "Untitled"


def jellyfin_name(title: str, year) -> str:
    return f"{safe_name(title)} ({year})" if year else safe_name(title)


# ---------------------------------------------------------------- rename patterns

PATTERNS = {  # setting -> (default, placeholders allowed, placeholders required)
    "movie_folder": ("{title} ({year})", {"title", "year", "resolution", "codec", "source", "tmdb"}, {"title"}),
    "movie_file": ("{title} ({year})", {"title", "year", "resolution", "codec", "source", "tmdb"}, {"title"}),
    "tv_show_folder": ("{title} ({year})", {"title", "year", "tmdb", "tvdb"}, {"title"}),
    "tv_season_folder": ("Season {season}", {"season"}, {"season"}),
    "tv_episode_file": ("{show} - {code} - {episode_title}", {"show", "code", "season", "episode", "episode_title", "resolution"}, {"code"}),
}
TOKEN = re.compile(r"\{(\w+)(?::(\d+))?\}")


def pattern(name: str, cfg: dict = None) -> str:
    cfg = load_config() if cfg is None else cfg
    return (cfg.get("patterns") or {}).get(name) or PATTERNS[name][0]


def check_pattern(name: str, text: str) -> str:
    """Refuse a pattern Jellyfin couldn't work with. Returns the cleaned-up pattern."""
    text = " ".join((text or "").split())
    if not text:
        return PATTERNS[name][0]
    _, allowed, required = PATTERNS[name]
    used = {m.group(1) for m in TOKEN.finditer(text)}
    unknown = used - allowed
    if unknown:
        raise UserError(f"“{{{sorted(unknown)[0]}}}” can't be used there. Use: " + ", ".join(f"{{{t}}}" for t in sorted(allowed)) + ".")
    missing = required - used
    if missing:
        raise UserError(f"That pattern needs {{{sorted(missing)[0]}}}" +
                        (" (the S01E02 part Jellyfin reads)." if "code" in missing else "."))
    if re.search(r'[<>:"/\\|?*]', TOKEN.sub("", text)):
        raise UserError('Patterns can\'t contain any of < > : " / \\ | ? *')
    return text


def render(text: str, values: dict) -> str:
    """Fill in a pattern, then tidy what an empty placeholder leaves behind ('Title ()', 'A -  - B')."""
    def one(m):
        v = values.get(m.group(1))
        if v in (None, ""):
            return ""
        if m.group(2) and str(v).isdigit():
            return str(int(v)).zfill(int(m.group(2)))
        return str(v)
    out = TOKEN.sub(one, text)
    out = re.sub(r"\(\s*\)|\[\s*\]|\{\s*\}", "", out)
    out = re.sub(r"(\s*-\s*){2,}", " - ", out)
    out = re.sub(r"\s+", " ", out).strip(" -._")
    return safe_name(out) if out else "Untitled"


def norm_title(text: str) -> str:
    import unicodedata
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return re.sub(r"^(the|a|an) ", "", text)


def short_error(err: Exception) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(err)).replace("ERROR: ", "")
    text = re.sub(r"^\[youtube\] [\w-]+: ", "", text)
    return text.split("\n")[0][:160]


# ---------------------------------------------------------------- file helpers

def same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


_changes = {}  # path -> when the app last changed it (for telling Jellyfin)
_changes_lock = threading.Lock()


def changed(*paths, note_as: str = "wrote") -> None:
    """Note that the app wrote, moved or recycled these files, so Jellyfin can be told to look again (and, for
    files written, so the History window lists them)."""
    now = time.time()
    with _changes_lock:
        for p in paths:
            _changes[str(p)] = now
    if note_as:
        for p in paths:
            note(note_as, path=p)


# ---------------------------------------------------------------- history: what each task or click changed

_act = threading.local()


def note(do: str, **data) -> None:
    """Add one change (moved, recycled, wrote, deleted, restored) to what the current task or click is doing."""
    a = getattr(_act, "cur", None)
    if a is not None:
        a["items"].append({"do": do, **{k: str(v) for k, v in data.items()}})


automatic = threading.Event()  # set while the folder watcher is working, so its History entries say so


def begin_activity(kind: str, title: str, library: str = "") -> bool:
    """Start collecting changes for one history entry (on this thread). False when one is already open."""
    if getattr(_act, "cur", None) is not None:
        return False
    _act.cur = {"time": time.time(), "kind": kind, "title": title, "library": library, "items": [],
                **({"by": "watch"} if automatic.is_set() else {})}
    return True


def end_activity() -> None:
    a, _act.cur = getattr(_act, "cur", None), None
    if a and a["items"]:
        from . import history
        try:
            history.add(a)
        except OSError:
            pass


def take_changes(quiet_for: float = 0) -> list:
    """The paths changed since last time, once nothing has changed for quiet_for seconds (else [])."""
    with _changes_lock:
        if not _changes or time.time() - max(_changes.values()) < quiet_for:
            return []
        out = list(_changes)
        _changes.clear()
        return out


def move(src: Path, dst: Path) -> None:
    if dst.exists() and not same_file(src, dst):
        raise OSError(f"“{dst.name}” already exists")
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dst)  # same drive, so this is instant and keeps the file intact
    changed(src, dst, note_as=None)
    note("moved", path=src, to=dst)


def remove_empty_dirs(paths, root: Path) -> None:
    for p in sorted({Path(x) for x in paths}, key=lambda x: len(str(x)), reverse=True):
        try:
            if p != root and p.is_dir() and not any(p.iterdir()):
                p.rmdir()
        except OSError:
            pass


def is_network_path(path: Path) -> bool:
    """Network drives have no Recycle Bin, so deleting there is permanent."""
    text = str(path)
    if text.startswith("\\\\"):
        return True
    if os.name == "nt":
        import ctypes
        drive = os.path.splitdrive(text)[0] + "\\"
        return ctypes.windll.kernel32.GetDriveTypeW(drive) == 4  # DRIVE_REMOTE
    return False


def discard(path: Path) -> None:
    """Recycle Bin when there is one; network drives have none, so the file or folder is deleted."""
    changed(path, note_as=None)
    original = path
    if is_network_path(path):
        shutil.rmtree(path) if path.is_dir() else path.unlink()
        note("deleted", path=original)
        return
    try:
        from send2trash import send2trash
    except ImportError:
        raise UserError("Deleting needs a new library. Close the app and run run.bat again to install it.")
    # Windows deletes for good, without asking, anything the Recycle Bin can't take: paths over 260 characters,
    # or more than the bin holds. Check first, so "to the Recycle Bin" always means restorable.
    size, longest = tree_stats(path)
    cap = recycle_capacity(path)
    if cap is not None and size > cap:
        raise UserError(f"“{path.name}” is {size / 1e9:.1f} GB, more than this drive's Recycle Bin holds ({cap / 1e9:.1f} GB), "
                        "so Windows would delete it for good. Delete it in File Explorer if you're sure, or make the Recycle Bin bigger.")
    if longest >= MAX_BIN_PATH:  # move it up to a short name at the top of the same drive first (instant, same drive)
        short = Path(os.path.splitdrive(str(path.resolve()))[0] + os.sep) / f"~mm-recycle-{time.time_ns() % 10**9}{path.suffix}"
        if longest - len(str(path)) + len(str(short)) >= MAX_BIN_PATH:
            raise UserError(f"“{path.name}” has paths too long for the Recycle Bin, so Windows would delete it for good. "
                            "Delete it in File Explorer if you're sure.")
        os.rename(path, short)
        path = short
    send2trash(str(path))
    note("recycled", path=original, binned=path)


MAX_BIN_PATH = 259  # Windows' MAX_PATH (260) less the terminating character


def tree_stats(path: Path):
    """(total bytes, longest full path) of a file or a folder and everything inside it."""
    if not path.is_dir():
        return path.stat().st_size, len(str(path))
    size, longest = 0, len(str(path))
    for root, dirs, files in os.walk(path):
        for n in dirs + files:
            full = os.path.join(root, n)
            longest = max(longest, len(full))
            if n in files:
                try:
                    size += os.stat(full).st_size
                except OSError:
                    pass
    return size, longest


def recycle_capacity(path: Path):
    """Bytes this drive's Recycle Bin holds (Windows' per-drive setting), or None if unknown."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        import winreg
        buf = ctypes.create_unicode_buffer(64)
        drive = os.path.splitdrive(str(path.resolve()))[0] + "\\"
        if not ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(drive, buf, 64):
            return None
        guid = buf.value[buf.value.index("{"):buf.value.index("}") + 1]
        key = rf"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume\{guid}"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            if winreg.QueryValueEx(k, "NukeOnDelete")[0]:
                return 0  # the Recycle Bin is switched off for this drive
            return int(winreg.QueryValueEx(k, "MaxCapacity")[0]) * 1024 * 1024
    except (OSError, ValueError, ImportError):
        return None


def ffmpeg_exe() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def ffmpeg_info(path: Path, timeout: int = 60) -> str:
    """What ffmpeg says about a file (streams, tags), or '' if it can't be read."""
    import subprocess
    try:
        return subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True,
                              timeout=timeout, creationflags=NO_WINDOW).stderr.decode("utf-8", "replace")
    except Exception:
        return ""


# ---------------------------------------------------------------- background tasks with progress

jobs = {}
job_lock = threading.Lock()


def job_step(job: dict, label: str, total: int) -> None:
    with job_lock:
        job.update(label=label, total=total, done=0)


def tick(job: dict, n: int = 1) -> None:
    with job_lock:
        job["done"] += n


def set_detail(job: dict, text: str) -> None:
    with job_lock:
        job["detail"] = text


def job_running() -> bool:
    with job_lock:
        return any(not j["finished"] for j in jobs.values())


def start_job(kind: str, label: str, total: int, work) -> str:
    """Run work(job) on a background thread; the page polls /api/job/<id> for progress. Returns the job id."""
    with job_lock:
        if any(not j["finished"] for j in jobs.values()):
            raise UserError("Another task is still running. Wait for it to finish.")
        job = {"id": f"{kind}-{time.time_ns()}", "kind": kind, "label": label, "done": 0, "total": total,
               "finished": False, "result": None, "error": None, "started": time.time(),
               "detail": "", "cancel": False}
        jobs.clear()
        jobs[job["id"]] = job

    def runner():
        from . import history
        collecting = begin_activity(kind, history.job_title(kind), "tv" if kind.startswith("tv") else "")
        try:
            job["result"] = work(job)
        except (TmdbError, SubError, UserError) as err:
            job["error"] = str(err)
        except OSError as err:
            job["error"] = f"File error: {err.strerror or err}"
        except Exception as err:  # keep the page from waiting forever
            import logging
            logging.getLogger(__name__).exception("Task failed")
            job["error"] = f"Unexpected error: {err}"
        finally:
            if collecting:
                end_activity()
            job["finished"] = True

    threading.Thread(target=runner, daemon=True).start()
    return job["id"]


# ---------------------------------------------------------------- several library folders

def _scan_file(base: Path, root) -> Path:
    import hashlib
    return base.with_name(f"{base.stem}-{hashlib.sha1(key_of(root).encode()).hexdigest()[:10]}{base.suffix}")


def load_scan(kind: str, root) -> dict:
    """The saved scan of one library folder (each folder has its own, so switching between them is instant).
    Falls back to the single file older versions kept."""
    base = TV_LIBRARY if kind == "tv" else LIBRARY
    data = load_json(_scan_file(base, root), {})
    if not data:
        old = load_json(base, {})
        data = old if old.get("root") == key_of(root) else {}
    return data


def save_scan(kind: str, root, data: dict) -> None:
    save_json(_scan_file(TV_LIBRARY if kind == "tv" else LIBRARY, root), data)


def remember_folder(kind: str, folder: str) -> dict:
    """Make a folder the current one, and remember it for the Library dropdown (most recent first)."""
    key = "tv_folders" if kind == "tv" else "folders"
    current = "tv_folder" if kind == "tv" else "folder"
    with lock:
        cfg = load_config()
        known = [f for f in (cfg.get(key) or ([cfg[current]] if cfg.get(current) else [])) if key_of(f) != key_of(folder)]
        return save_config(**{current: folder, key: [folder] + known[:11]})


def forget_folder(kind: str, folder: str) -> dict:
    key = "tv_folders" if kind == "tv" else "folders"
    with lock:
        cfg = load_config()
        return save_config(**{key: [f for f in cfg.get(key) or [] if key_of(f) != key_of(folder)]})


def require_tv_scan() -> Path:
    if tv_state["root"] is None or not tv_state["root"].is_dir():
        raise UserError("Scan your TV shows folder first.")
    return tv_state["root"]


def require_scan() -> Path:
    if state["root"] is None or not state["root"].is_dir():
        raise UserError("Scan your movie folder first.")
    return state["root"]
