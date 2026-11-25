"""The local web app: the page, its API, and shutting down when the browser tab closes."""
import os
import re
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, request

from . import artwork, collections, core, library, movie, nfo, renamer, subtitles, tmdbapi, trailers
from .core import UserError

app = Flask(__name__, static_folder=str(core.BASE / "static"), static_url_path="")
# Changes whenever the app's code or page changes, so an open tab knows to reload itself after an update
APP_VERSION = str(int(max(f.stat().st_mtime for f in [core.BASE / "static" / "index.html",
                                                     *Path(__file__).parent.glob("*.py")])))
LOCAL_HOSTS = {"127.0.0.1", "localhost"}


@app.before_request
def only_this_computer():
    """Answer only requests addressed to this app. Stops a web page from reaching it through a trick
    called DNS rebinding, or posting to it from another site, which could otherwise rename or delete files."""
    if (request.host or "").rsplit(":", 1)[0].strip("[]") not in LOCAL_HOSTS:
        return "Forbidden", 403
    origin = request.headers.get("Origin")
    if request.method != "GET" and origin and (urlsplit(origin).hostname or "") not in LOCAL_HOSTS:
        return "Forbidden", 403


@app.errorhandler(core.TmdbError)
@app.errorhandler(core.SubError)
@app.errorhandler(UserError)
def user_error(err):
    return jsonify(error=str(err)), 400


def body() -> dict:
    return request.get_json(silent=True) or {}


def job_response(kind: str, label: str, total: int, work):
    return jsonify(job=core.start_job(kind, label, total, work))


@app.get("/")
def index():
    resp = app.send_static_file("index.html")
    resp.headers["Cache-Control"] = "no-cache"  # always check for a newer page
    return resp


# ---------------------------------------------------------------- settings

def settings_view(cfg: dict) -> dict:
    key = cfg.get("tmdb_key") or ""
    os_key = cfg.get("os_key") or ""
    return {"folder": cfg.get("folder", ""), "tv_folder": cfg.get("tv_folder", ""), "has_key": bool(key), "key_hint": key[-4:] if key else "",
            "language": cfg.get("language") or "en-US",
            "has_os_key": bool(os_key), "os_key_hint": os_key[-4:] if os_key else "",
            "os_user": cfg.get("os_user", ""), "has_os_pass": bool(cfg.get("os_pass")),
            "sub_langs": cfg.get("sub_langs") or "en", "sub_hearing": cfg.get("sub_hearing") or "normal",
            "yt_login": cfg.get("yt_login") or "", "yt_cookie_file": cfg.get("yt_cookie_file") or "",
            "has_fanart_key": bool(cfg.get("fanart_key")), "fanart_key_hint": (cfg.get("fanart_key") or "")[-4:]}


@app.get("/api/config")
def get_config():
    return jsonify(settings_view(core.load_config()))


@app.post("/api/settings")
def api_settings():
    data = body()
    changes = {}
    lang = (data.get("language") or "").strip()
    if lang:
        if not re.fullmatch(r"[a-z]{2}(-[A-Z]{2})?", lang):
            raise UserError("Language should look like en-US or de-DE.")
        changes["language"] = lang
    key = (data.get("tmdb_key") or "").strip()
    if key:
        tmdbapi.tmdb("/configuration", key=key)  # raises if TMDB rejects it
        changes["tmdb_key"] = key
    if "yt_login" in data:
        source = data.get("yt_login") or ""
        if source not in trailers.LOGIN_BROWSERS + ("", "file"):
            raise UserError("Choose a browser for the YouTube login.")
        changes["yt_login"] = source
        changes["yt_cookie_file"] = (data.get("yt_cookie_file") or "").strip().strip('"')
        if source == "file" and not changes["yt_cookie_file"]:
            raise UserError("Enter the full path of your cookies.txt file.")
    fanart_key = (data.get("fanart_key") or "").strip()
    if fanart_key:
        artwork.check_fanart_key(fanart_key)  # raises if fanart.tv refuses it
        changes["fanart_key"] = fanart_key
    if data.get("fanart_key_clear"):
        changes["fanart_key"] = ""
    if "sub_langs" in data:
        changes["sub_langs"] = ", ".join(subtitles.sub_langs(data.get("sub_langs"))) or "en"
    # OpenSubtitles: only check when something changed; a blank password keeps the saved one
    cfg = core.load_config()
    os_key = (data.get("os_key") or "").strip() or cfg.get("os_key", "")
    os_user = (data.get("os_user") if "os_user" in data else cfg.get("os_user", "")).strip()
    os_pass = data.get("os_pass") or cfg.get("os_pass", "")
    os_info = None
    if (os_key, os_user, os_pass) != (cfg.get("os_key", ""), cfg.get("os_user", ""), cfg.get("os_pass", "")):
        if os_user and not os_pass:
            raise UserError("Enter your OpenSubtitles password too, or clear the username.")
        if os_key:
            if os_user:
                user = subtitles.os_login(force=True, key=os_key, user=os_user, password=os_pass)
                os_info = f"Logged in to OpenSubtitles: {user.get('allowed_downloads', '?')} downloads a day."
            else:
                subtitles.os_session.update(token=None, base=None, user=None, creds=None)
                subtitles.check_key(os_key)
                os_info = "OpenSubtitles accepted the API key."
        changes.update(os_key=os_key, os_user=os_user, os_pass=os_pass if os_user else "")
    cfg = core.save_config(**changes)
    return jsonify({**settings_view(cfg), "verified": bool(key), "os_info": os_info,
                    "fanart_info": "fanart.tv accepted the key." if fanart_key else None})


@app.post("/api/youtube-login/test")
def api_youtube_login_test():
    data = body()
    cfg = {"yt_login": data.get("yt_login") or "",
           "yt_cookie_file": (data.get("yt_cookie_file") or "").strip().strip('"')}
    return jsonify(message=trailers.check_youtube_login(cfg))


# ---------------------------------------------------------------- library

@app.post("/api/scan")
def api_scan():
    data = body()
    folder = (data.get("folder") or "").strip().strip('"')
    root = Path(folder)
    if not folder or not root.is_dir():
        return jsonify(error=f"Folder not found: {folder or '(empty)'}. Check the path and try again."), 400
    core.save_config(folder=folder)
    refresh = bool(data.get("refresh"))

    def work(job):
        started = time.time()
        with core.lock:
            if core.state["root"] is not None and core.key_of(core.state["root"]) == core.key_of(root):
                before = {it["id"] for it in core.state["items"]}
            else:
                cache = core.load_json(core.LIBRARY, {})
                before = ({it["id"] for it in cache.get("items", [])}
                          if cache.get("root") == core.key_of(root) else None)
            core.state["root"] = root
            items = [dict(i) for i in library.rescan(job, "Checking for changes" if refresh else "Reading file names")]
        after = {it["id"] for it in items}
        changes = None if before is None else {"added": len(after - before), "removed": len(before - after)}
        return {"items": items, "seconds": round(time.time() - started, 1), "changes": changes}

    return job_response("scan", "Checking for changes" if refresh else "Finding video files", 0, work)


@app.get("/api/library")
def api_library():
    """The last library, straight from the saved scan, so the page can show it without waiting."""
    folder = core.load_config().get("folder") or ""
    if not folder:
        return jsonify(items=None)
    root = Path(folder)
    if not root.is_dir():
        return jsonify(items=None, folder=folder, missing=True)
    with core.lock:
        if core.state["root"] is None or core.key_of(core.state["root"]) != core.key_of(root):
            cache = core.load_json(core.LIBRARY, {})
            if cache.get("root") != core.key_of(root):
                return jsonify(items=None, folder=folder)  # nothing saved yet: the page will scan
            core.state["root"] = root
            core.state["items"] = cache.get("items", [])
        items = library.current_items()
    return jsonify(items=items, folder=folder)


@app.get("/api/job/<job_id>")
def api_job(job_id):
    with core.job_lock:
        job = core.jobs.get(job_id)
        if not job:
            return jsonify(error="That task is no longer available. Scan again."), 404
        return jsonify({**job, "elapsed": round(time.time() - job["started"], 1)})


@app.post("/api/job/<job_id>/cancel")
def api_job_cancel(job_id):
    with core.job_lock:
        job = core.jobs.get(job_id)
        if job and not job["finished"]:
            job["cancel"] = True
            job["detail"] = "stopping after the current item"
    return jsonify(ok=True)


# ---------------------------------------------------------------- matching

@app.post("/api/match-all")
def api_match_all():
    core.require_scan()
    redo = bool(body().get("redo"))
    with core.lock:
        items = library.with_matches(core.state["items"])
    todo = [dict(it) for it in items if redo and (it["match"] or {}).get("status") != "manual"
            or not it["match"] or it["match"]["status"] == "none"]
    if todo:
        tmdbapi.tmdb("/configuration")  # fail fast on a missing or bad key
    return job_response("match", "Matching on TMDB", len(todo), lambda job: tmdbapi.match_all(job, todo))


@app.get("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        raise UserError("Type a title to search for.")
    year = request.args.get("year", "").strip()
    return jsonify(results=tmdbapi.search(q, int(year) if year.isdigit() else None))


@app.post("/api/match")
def api_match():
    data = body()
    return jsonify(item=tmdbapi.set_match(data.get("id"), data))


# ---------------------------------------------------------------- one movie: details, edits, artwork, folder

@app.get("/api/movie")
def api_movie():
    return jsonify(movie.detail(request.args.get("id", "")))


@app.post("/api/movie/edit")
def api_movie_edit():
    data = body()
    return jsonify(movie.edit(data.get("id", ""), data.get("fields") or {}, reset=bool(data.get("reset"))))


@app.get("/api/image")
def api_image():
    from flask import send_file
    path, mimetype = movie.image(request.args.get("id", ""), request.args.get("kind", ""))
    return send_file(path, mimetype=mimetype, max_age=300)


@app.get("/api/artwork/options")
def api_artwork_options():
    return jsonify(options=movie.art_options(request.args.get("id", ""), request.args.get("kind", "")))


@app.post("/api/artwork/set")
def api_artwork_set():
    data = body()
    return jsonify(movie.set_art(data.get("id", ""), data.get("kind", ""), data.get("url", "")))


@app.post("/api/artwork")
def api_artwork():
    data = body()
    kinds = [k for k in (data.get("kinds") or []) if k in artwork.KINDS]
    if not kinds:
        raise UserError("Choose at least one kind of artwork.")
    replace = bool(data.get("replace"))
    root = core.require_scan()
    tmdbapi.tmdb("/configuration")  # fail fast on a missing or bad key
    return job_response("artwork", "Checking for changes", 0, lambda job: artwork.run(job, root, kinds, replace))


@app.post("/api/collections")
def api_collections():
    root = core.require_scan()
    tmdbapi.tmdb("/configuration")
    return job_response("collections", "Checking for changes", 0, lambda job: collections.run(job, root))


@app.post("/api/open-folder")
def api_open_folder():
    movie.open_folder(body().get("id", ""))
    return jsonify(ok=True)


# ---------------------------------------------------------------- renaming, .nfo, deleting

@app.get("/api/rename/preview")
def api_rename_preview():
    return jsonify(renamer.preview())


@app.post("/api/rename/apply")
def api_rename_apply():
    wanted = set(map(str, body().get("ids") or []))
    root = core.require_scan()
    if not wanted:
        raise UserError("Pick at least one movie to rename.")
    return job_response("rename", "Checking files", 0, lambda job: renamer.apply(job, wanted, root))


@app.post("/api/rename/undo")
def api_rename_undo():
    return jsonify(renamer.undo())


@app.post("/api/nfo")
def api_nfo():
    data = body()
    overwrite, artwork = bool(data.get("overwrite")), data.get("artwork", True) is not False
    root = core.require_scan()
    tmdbapi.tmdb("/configuration")  # fail fast on a missing or bad key
    return job_response("nfo", "Checking for changes", 0, lambda job: nfo.write_all(job, root, overwrite, artwork))


@app.post("/api/delete/preview")
def api_delete_preview():
    with core.lock:
        root = core.require_scan()
        p = library.delete_plan(body().get("id"))
    others = [x["path"] for x in p["siblings"] if x["id"] != p["item"]["id"]]
    return jsonify(
        name=p["name"], own=p["own"], permanent=core.is_network_path(p["video"]),
        files=[core.rel(f, root) for f in p["files"]], files_size=library.folder_size(p["files"]),
        folder=core.rel(p["folder"], root) if p["own"] else None,
        folder_files=[core.rel(f, p["folder"]) for f in p["folder_files"]][:200],
        folder_count=len(p["folder_files"]), folder_size=library.folder_size(p["folder_files"]),
        other_videos=others if p["own"] else [],
        suggested="folder" if p["own"] and not others else "files",
    )


@app.post("/api/delete")
def api_delete():
    data = body()
    return jsonify(library.delete(data.get("id"), data.get("mode")))


# ---------------------------------------------------------------- trailers and subtitles

@app.post("/api/trailers")
def api_trailers():
    data = body()
    quality = int(data.get("quality") or 1080)
    if quality not in trailers.QUALITY_WIDTH:
        raise UserError("Pick 1080p, 720p or 480p.")
    force = bool(data.get("force"))
    root = core.require_scan()
    tmdbapi.tmdb("/configuration")  # fail fast on a missing or bad key
    return job_response("trailers", "Checking for changes", 0, lambda job: trailers.run(job, root, quality, force))


@app.post("/api/subtitles")
def api_subtitles():
    data = body()
    langs = subtitles.sub_langs(data.get("languages") or core.load_config().get("sub_langs") or "en")
    if not langs:
        raise UserError("Choose at least one subtitle language.")
    hearing = data.get("hearing") if data.get("hearing") in ("normal", "prefer") else "normal"
    skip_embedded = data.get("skip_embedded", True) is not False
    root = core.require_scan()
    if not subtitles.os_login():  # logged in: the login itself checked the key, username and password
        subtitles.check_key()
    core.save_config(sub_langs=", ".join(langs), sub_hearing=hearing)
    return job_response("subtitles", "Checking for changes", 0,
                        lambda job: subtitles.run(job, root, langs, hearing, skip_embedded))


# ---------------------------------------------------------------- quit when the browser tab closes

LOST_AFTER = 100   # no check-in for this long = tab gone (hidden tabs may only run timers once a minute)
BYE_GRACE = 6      # after a tab says goodbye, wait this long in case it was just a reload
alive = {"last": None, "bye": None}


@app.post("/api/ping")
def api_ping():
    alive["last"], alive["bye"] = time.time(), None
    return jsonify(ok=True, version=APP_VERSION)


@app.post("/api/bye")
def api_bye():
    alive["bye"] = time.time()
    return ("", 204)


def watchdog() -> None:
    while True:
        time.sleep(2)
        if alive["last"] is None or core.job_running():  # never opened yet, or busy: keep going
            continue
        now = time.time()
        if (alive["bye"] and now - alive["bye"] > BYE_GRACE) or now - alive["last"] > LOST_AFTER:
            print("\nThe browser tab was closed, so the media manager is shutting down.")
            os._exit(0)


@app.post("/api/quit")
def api_quit():
    """A newer copy of the app is starting and asks this one to step aside (only this computer can ask)."""
    if request.headers.get("X-Media-Manager") != "restart":
        return "Forbidden", 403
    print("\nA new copy of the media manager is starting, so this one is closing.")
    threading.Timer(0.3, lambda: os._exit(0)).start()  # exit code 0 lets run.bat close this window
    return jsonify(ok=True)


# ---------------------------------------------------------------- starting up

def port_in_use(port: int) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_until_free(port: int, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if not port_in_use(port):
            return True
        time.sleep(0.25)
    return not port_in_use(port)


def process_info(pid: int) -> dict:
    """Name, command line and parent of a Windows process."""
    import json as _json
    import subprocess
    cmd = (f"Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}' | "
           "Select-Object Name,CommandLine,ParentProcessId | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                             timeout=15, creationflags=core.NO_WINDOW).stdout.strip()
        return _json.loads(out) if out else {}
    except Exception:
        return {}


def listening_pid(port: int):
    import subprocess
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=15,
                             creationflags=core.NO_WINDOW).stdout
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
            return int(parts[4])
    return None


def close_old_copy(port: int) -> bool:
    """Make room for this copy: ask the old one to quit, and if it doesn't answer (an older version of the app,
    or a stuck one), end it. Only a Python process started from this app's folder is ever ended."""
    import subprocess
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/quit", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json", "X-Media-Manager": "restart"})
    try:
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass
    if wait_until_free(port, 6):
        return True
    pid = listening_pid(port)
    info = process_info(pid) if pid else {}
    if not info or info.get("Name", "").lower() not in ("python.exe", "pythonw.exe"):
        return False  # not a Python program: certainly not ours
    # Walk up to the window it runs in: Python (and the .venv launcher), then the run.bat console
    top, parent, from_our_bat = pid, info.get("ParentProcessId"), False
    for _ in range(3):
        p = process_info(parent) if parent else {}
        name, line = p.get("Name", "").lower(), (p.get("CommandLine") or "").lower()
        if name in ("python.exe", "pythonw.exe"):
            top, parent = parent, p.get("ParentProcessId")
        elif name == "cmd.exe" and str(core.BASE / "run.bat").lower() in line:
            top, from_our_bat = parent, True
            break
        else:
            break
    # Only ever end it when it is verifiably this app: it serves the "Movie library" page (every version does),
    # or it is app.py started by this folder's run.bat. Anything else using the port is left alone.
    if not (serves_our_page(port) or (from_our_bat and "app.py" in (info.get("CommandLine") or "").lower())):
        return False
    subprocess.run(["taskkill", "/PID", str(top), "/T", "/F"], capture_output=True, creationflags=core.NO_WINDOW)
    return wait_until_free(port, 8)


def serves_our_page(port: int) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as resp:
            return b"<title>Movie library</title>" in resp.read(4096)
    except Exception:
        return False


def open_browser_unless_reconnected(port: int) -> None:
    """An already-open tab reconnects within a few seconds; only open a new tab when none does."""
    if alive["last"] is None:
        webbrowser.open(f"http://127.0.0.1:{port}")


from . import tvweb  # noqa: E402  (importing it registers the /api/tv/... endpoints)

__all__ = ["app", "main", "tvweb"]


def main() -> None:
    port = core.PORT
    if port_in_use(port):
        print("An earlier copy of the media manager is still running. Closing it...")
        if not close_old_copy(port):
            print(f"\nSomething else is using port {port} and couldn't be closed.")
            print("Close the other media manager window (or its python.exe in Task Manager), then run this again.\n")
            raise SystemExit(1)
        print("Closed it.")
    threading.Thread(target=watchdog, daemon=True).start()
    threading.Timer(3.0, open_browser_unless_reconnected, args=(port,)).start()
    print(f"Media manager is running at http://127.0.0.1:{port}")
    print("Closing the browser tab shuts it down. You can also close this window.")
    app.run(host="127.0.0.1", port=port, threaded=True)
