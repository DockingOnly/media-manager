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


# ---------------------------------------------------------------- finding a movie or show in Jellyfin

def find_item(path: str, title: str, kind: str = "Movie") -> dict:
    """Jellyfin's page for a movie file or a show folder: {'url', 'name'}. Found by path, so the right copy opens."""
    users = request("GET", "/Users") or []
    user = next((u for u in users if (u.get("Policy") or {}).get("IsAdministrator")), users[0] if users else None)
    if not user:
        raise UserError("Jellyfin has no users to look this up with.")
    q = urllib.parse.urlencode({"Recursive": "true", "IncludeItemTypes": kind, "Fields": "Path", "Limit": 50,
                                "SearchTerm": (title or "")[:60]})
    found = (request("GET", f"/Users/{user['Id']}/Items?{q}") or {}).get("Items") or []
    want = to_jellyfin(path)
    norm = lambda x: (x or "").replace("\\", "/").rstrip("/").lower()
    item = next((i for i in found if norm(i.get("Path")) == norm(want)), None)
    if item is None and kind == "Series":
        item = next((i for i in found if _inside(want, i.get("Path") or "") or _inside(i.get("Path") or "", want)), None)
    if item is None:
        raise UserError("Jellyfin doesn't have this yet (or knows it under another name). If it's new, give Jellyfin a minute to scan it.")
    base = settings()["url"]
    return {"url": f"{base}/web/#/details?id={item['Id']}" + (f"&serverId={item['ServerId']}" if item.get("ServerId") else ""),
            "name": item.get("Name") or title}


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
                if not core.job_running():
                    from . import mediainfo
                    mediainfo.save()  # keep what ffmpeg said about new files (only the running app does this)
            except Exception:  # never let this stop the app
                pass
    threading.Thread(target=loop, daemon=True, name="background").start()


# ---------------------------------------------------------------- Jellyfin's settings that fight this app's files

def _libraries_for_us(folders: list) -> list:
    """Jellyfin's libraries, each with which of your folders (movies / TV) it holds."""
    cfg = core.load_config()
    mine = {"movies": cfg.get("folders") or ([cfg["folder"]] if cfg.get("folder") else []),
            "tv": cfg.get("tv_folders") or ([cfg["tv_folder"]] if cfg.get("tv_folder") else [])}
    out = []
    for f in folders:
        locs = f.get("Locations") or []
        holds = {kind: [x for x in paths if any(_inside(to_jellyfin(x), loc) or _inside(loc, to_jellyfin(x)) for loc in locs)]
                 for kind, paths in mine.items()}
        out.append({"lib": f, "holds": {k: v for k, v in holds.items() if v}})
    return out


def settings_check() -> dict:
    """Problems (this app's work won't show), warnings (it can be overwritten) and notes, with fixes where safe."""
    libs = _libraries_for_us(request("GET", "/Library/VirtualFolders") or [])
    issues = []

    def add(level, lib, text, fix=None):
        issues.append({"level": level, "library": lib.get("Name") if lib else "", "library_id": lib.get("ItemId") if lib else "",
                       "text": text, "fix": fix})

    ours = [x for x in libs if x["holds"]]
    if not ours:
        add("problem", None, "None of Jellyfin's libraries holds your movie or TV folders. Add them in Jellyfin (Dashboard → Libraries), "
                             "or, if Jellyfin sees them under other paths, add a path mapping in Settings.")
    for x in ours:
        lib, opts = x["lib"], x["lib"].get("LibraryOptions") or {}
        ctype = lib.get("CollectionType") or "mixed"
        if "movies" in x["holds"] and ctype == "tvshows":
            add("problem", lib, "This is a TV library, but it holds your movie folder, so Jellyfin will treat the movies as shows.")
        if "tv" in x["holds"] and ctype == "movies":
            add("problem", lib, "This is a movie library, but it holds your TV folder, so Jellyfin will treat episodes as movies.")
        if "Nfo" in (opts.get("DisabledLocalMetadataReaders") or []):
            add("problem", lib, "Jellyfin is set to ignore .nfo files here, so the details and edits this app writes never reach it.", "enable_nfo")
        if "Nfo" in (opts.get("MetadataSavers") or []):
            add("warning", lib, "Jellyfin writes its own .nfo files here too, and can overwrite this app's details and your edits.", "no_nfo_saver")
        fetchers = sorted({f for t in opts.get("TypeOptions") or [] for f in t.get("MetadataFetchers") or []})
        if fetchers:
            add("note", lib, f"Jellyfin also looks details up online ({', '.join(fetchers)}). The .nfo files come first; a "
                             "“Replace all metadata” refresh in Jellyfin can still overwrite your edits.")
        if (opts.get("AutomaticRefreshIntervalDays") or 0) > 0:
            add("note", lib, f"Jellyfin refreshes details from the internet every {opts['AutomaticRefreshIntervalDays']} days.")
    try:
        nfo_cfg = request("GET", "/System/Configuration/xbmcmetadata") or {}
    except UserError:
        nfo_cfg = None
    if nfo_cfg is not None and not nfo_cfg.get("UserId"):
        users = request("GET", "/Users") or []
        add("warning", None, "No Jellyfin user is chosen for watched status in .nfo files (Dashboard → Display → NFO settings), "
                             "so the watched status in .nfo files (from Trakt, say) is ignored.",
            "nfo_user" if len(users) == 1 else None)
    order = {"problem": 0, "warning": 1, "note": 2}
    issues.sort(key=lambda i: order[i["level"]])
    return {"issues": issues, "libraries": [{"name": x["lib"].get("Name"), "type": x["lib"].get("CollectionType") or "mixed",
                                             "yours": sorted(x["holds"])} for x in libs]}


def fix(what: str, library_id: str = "") -> dict:
    """Change one Jellyfin setting (only the ones offered above), then check again."""
    if what in ("enable_nfo", "no_nfo_saver"):
        lib = next((f for f in request("GET", "/Library/VirtualFolders") or [] if f.get("ItemId") == library_id), None)
        if not lib:
            raise UserError("That Jellyfin library is no longer there. Check again.")
        opts = dict(lib.get("LibraryOptions") or {})
        if what == "enable_nfo":
            opts["DisabledLocalMetadataReaders"] = [r for r in opts.get("DisabledLocalMetadataReaders") or [] if r != "Nfo"]
        else:
            opts["MetadataSavers"] = [s for s in opts.get("MetadataSavers") or [] if s != "Nfo"]
        request("POST", "/Library/VirtualFolders/LibraryOptions", {"Id": library_id, "LibraryOptions": opts})
    elif what == "nfo_user":
        users = request("GET", "/Users") or []
        if len(users) != 1:
            raise UserError("Choose the user in Jellyfin: Dashboard → Display → NFO settings.")
        cfg = request("GET", "/System/Configuration/xbmcmetadata") or {}
        cfg["UserId"] = users[0]["Id"]
        request("POST", "/System/Configuration/xbmcmetadata", cfg)
    else:
        raise UserError("That can't be fixed from here.")
    return settings_check()
