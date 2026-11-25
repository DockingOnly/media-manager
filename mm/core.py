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


def move(src: Path, dst: Path) -> None:
    if dst.exists() and not same_file(src, dst):
        raise OSError(f"“{dst.name}” already exists")
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dst)  # same drive, so this is instant and keeps the file intact


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
    if is_network_path(path):
        shutil.rmtree(path) if path.is_dir() else path.unlink()
        return
    try:
        from send2trash import send2trash
    except ImportError:
        raise UserError("Deleting needs a new library. Close the app and run run.bat again to install it.")
    send2trash(str(path))


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
            job["finished"] = True

    threading.Thread(target=runner, daemon=True).start()
    return job["id"]


def require_tv_scan() -> Path:
    if tv_state["root"] is None or not tv_state["root"].is_dir():
        raise UserError("Scan your TV shows folder first.")
    return tv_state["root"]


def require_scan() -> Path:
    if state["root"] is None or not state["root"].is_dir():
        raise UserError("Scan your movie folder first.")
    return state["root"]
