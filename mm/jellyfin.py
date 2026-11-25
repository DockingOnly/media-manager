"""Telling Jellyfin about changes, so new names, .nfo files and artwork show up without waiting for its next scan.

After the app writes, moves or recycles files (core.changed), and things have been quiet for a few seconds, the
folders involved are reported to Jellyfin (POST /Library/Media/Updated). Jellyfin then refreshes just those
items, after its own short delay (about a minute by default). Many folders at once become one library scan.

Settings: the server address, an API key (Jellyfin: Dashboard -> API Keys), and, only when Jellyfin sees the
files under other paths (Docker, another computer), path mappings such as  D:\\Media => /media.
"""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import core
from .core import UserError

FULL_SCAN_ABOVE = 40   # this many folders or more: one library scan instead
QUIET_FOR = 5          # seconds without changes before telling Jellyfin
status = {"time": None, "folders": 0, "full": False, "error": None}


def settings(cfg: dict = None) -> dict:
    cfg = core.load_config() if cfg is None else cfg
    return {"url": (cfg.get("jellyfin_url") or "").rstrip("/"), "key": cfg.get("jellyfin_key") or "",
            "map": cfg.get("jellyfin_map") or "", "auto": cfg.get("jellyfin_auto", True)}


def configured(cfg: dict = None) -> bool:
    s = settings(cfg)
    return bool(s["url"] and s["key"])


def check_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise UserError("Enter Jellyfin's address, such as http://localhost:8096")
    return url


def request(method: str, path: str, body=None, url: str = None, key: str = None, timeout: int = 15):
    s = settings()
    url, key = (url or s["url"]), (key or s["key"])
    if not (url and key):
        raise UserError("Add your Jellyfin address and API key in Settings first.")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method, headers={
        "Authorization": f'MediaBrowser Client="Media manager", Token="{key}"',
        "Content-Type": "application/json", "Accept": "application/json", "User-Agent": core.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as err:
        if err.code in (401, 403):
            raise UserError("Jellyfin refused the API key. Create one in Jellyfin under Dashboard → API Keys.")
        raise UserError(f"Jellyfin answered with an error ({err.code}).")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise UserError(f"Couldn't reach Jellyfin at {url}. Check the address, and that Jellyfin is running.")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


# ---------------------------------------------------------------- paths as Jellyfin sees them

def mappings(text: str) -> list:
    """'D:\\Media => /media' lines as [(from, to)]."""
    out = []
    for line in (text or "").splitlines():
        if "=>" in line:
            a, b = (x.strip().strip('"') for x in line.split("=>", 1))
            if a and b:
                out.append((a, b))
    return out


def to_jellyfin(path, text: str = None) -> str:
    """This computer's path as Jellyfin knows it (unchanged when no mapping applies)."""
    p = str(path)
    for a, b in mappings(settings()["map"] if text is None else text):
        if p.lower() == a.lower().rstrip("\\/") or p.lower().startswith(a.lower().rstrip("\\/") + "\\") \
                or p.lower().startswith(a.lower().rstrip("\\/") + "/"):
            rest = p[len(a.rstrip("\\/")):].lstrip("\\/")
            if b.startswith("/"):  # Linux / Docker
                return str(PurePosixPath(b, *PureWindowsPath(rest).parts)) if rest else b
            return str(PureWindowsPath(b, rest)) if rest else b
    return p


def _inside(path: str, location: str) -> bool:
    norm = lambda x: x.replace("\\", "/").rstrip("/").lower()
    return norm(path) == norm(location) or norm(path).startswith(norm(location) + "/")


# ---------------------------------------------------------------- checking the connection

def check(url: str = None, key: str = None, map_text: str = None) -> dict:
    """Server name and version, its libraries, and whether your library folders are inside one of them."""
    info = request("GET", "/System/Info", url=url, key=key) or {}
    folders = request("GET", "/Library/VirtualFolders", url=url, key=key) or []
    libs = [{"name": f.get("Name"), "type": f.get("CollectionType") or "mixed", "locations": f.get("Locations") or []}
            for f in folders]
    cfg = core.load_config()
    warnings = []
    for kind, label in (("folders", "movie"), ("tv_folders", "TV")):
        for folder in cfg.get(kind) or ([cfg[kind[:-1]]] if cfg.get(kind[:-1]) else []):
            seen = to_jellyfin(folder, map_text)
            if not any(_inside(seen, loc) or _inside(loc, seen) for lib in libs for loc in lib["locations"]):
                warnings.append(f"Your {label} folder {folder}" + (f" (seen as {seen})" if seen != folder else "")
                                + " isn't in any Jellyfin library. If Jellyfin sees it under another path, add a path mapping.")
    return {"server": info.get("ServerName") or "Jellyfin", "version": info.get("Version") or "",
            "libraries": libs, "warnings": warnings}


# ---------------------------------------------------------------- telling Jellyfin

def folders_for(paths) -> list:
    """The folders to report: each changed file's folder, without folders already inside another reported one."""
    found = sorted({str(Path(p).parent) for p in paths}, key=len)
    out = []
    for f in found:
        if not any(_inside(f, o) for o in out):
            out.append(f)
    return out


def notify(paths) -> dict:
    folders = folders_for(paths)
    if not folders:
        return status
    try:
        if len(folders) >= FULL_SCAN_ABOVE:
            request("POST", "/Library/Refresh")
            status.update(full=True)
        else:
            request("POST", "/Library/Media/Updated",
                    {"Updates": [{"Path": to_jellyfin(f), "UpdateType": "Modified"} for f in folders]})
            status.update(full=False)
        status.update(time=time.time(), folders=len(folders), error=None)
    except UserError as err:
        status.update(time=time.time(), error=str(err))
    return status


def refresh_all() -> dict:
    request("POST", "/Library/Refresh")
    status.update(time=time.time(), folders=0, full=True, error=None)
    return status


def flush_once() -> None:
    """Report what changed, once things have been quiet for a moment and no task is running."""
    if core.job_running():
        return
    paths = core.take_changes(QUIET_FOR)
    if paths and configured() and settings()["auto"]:
        notify(paths)


def start() -> None:
    def loop():
        while True:
            time.sleep(2)
            try:
                flush_once()
            except Exception:  # never let this stop the app
                pass
    threading.Thread(target=loop, daemon=True, name="jellyfin").start()
