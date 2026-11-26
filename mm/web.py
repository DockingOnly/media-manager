"""The local web app: the page, its API, and shutting down when the browser tab closes."""
import os
import re
import subprocess
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, g, jsonify, request

from . import (artwork, backup, badges, bulk, collections, convert, core, dupes, export, history, jellyfin, leftovers, library, logs,
               movie, nfo, notify, playback, renamer, smart, subtitles, tmdbapi, trailers, trakt, vault, watch)
from .core import UserError

app = Flask(__name__, static_folder=str(core.RES / "static"), static_url_path="")
# Changes whenever the app's code or page changes, so an open tab knows to reload itself after an update
APP_VERSION = str(int(max(f.stat().st_mtime for f in [core.RES / "static" / "index.html",
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


@app.errorhandler(OSError)
def file_error(err):
    """A file problem the page can show (not a server error page)."""
    if isinstance(err, PermissionError):
        return jsonify(error=f"Windows wouldn't let the app use {getattr(err, 'filename', None) or 'a file'} just now "
                             "(another program had it open). Try again in a moment."), 409
    return jsonify(error=f"File error: {getattr(err, 'strerror', None) or err}"), 500


def body() -> dict:
    return request.get_json(silent=True) or {}


def job_response(kind: str, label: str, total: int, work):
    return jsonify(job=core.start_job(kind, label, total, work))


@app.before_request
def collect_changes():
    """Each click that changes files becomes one History entry (background tasks collect their own)."""
    if request.method == "POST" and request.path.startswith("/api/") and not request.path.startswith("/api/job/"):
        g.collecting = core.begin_activity(request.path, history.path_title(request.path),
                                           "tv" if request.path.startswith("/api/tv/") else "")


@app.teardown_request
def save_changes(exc):
    if g.pop("collecting", False):
        core.end_activity()


@app.get("/api/history")
def api_history():
    return jsonify(history.entries(before=request.args.get("before") or None))


@app.post("/api/history/move-back")
def api_history_move_back():
    return jsonify(history.move_back(str(body().get("id") or "")))


@app.post("/api/history/reveal")
def api_history_reveal():
    history.reveal(str(body().get("path") or ""))
    return jsonify(ok=True)


@app.post("/api/history/restore")
def api_history_restore():
    d = body()
    index = d.get("index")
    return jsonify(history.restore(str(d.get("id") or ""), int(index) if index is not None else None))


@app.get("/")
def index():
    resp = app.send_static_file("index.html")
    resp.headers["Cache-Control"] = "no-cache"  # always check for a newer page
    return resp


# ---------------------------------------------------------------- settings

def settings_view(cfg: dict) -> dict:
    key = cfg.get("tmdb_key") or ""
    os_key = cfg.get("os_key") or ""
    return {"folder": cfg.get("folder", ""), "tv_folder": cfg.get("tv_folder", ""),
            "folders": cfg.get("folders") or ([cfg["folder"]] if cfg.get("folder") else []),
            "tv_folders": cfg.get("tv_folders") or ([cfg["tv_folder"]] if cfg.get("tv_folder") else []), "has_key": bool(key), "key_hint": key[-4:] if key else "",
            "language": cfg.get("language") or "en-US",
            "has_os_key": bool(os_key), "os_key_hint": os_key[-4:] if os_key else "",
            "os_user": cfg.get("os_user", ""), "has_os_pass": bool(cfg.get("os_pass")),
            "sub_langs": cfg.get("sub_langs") or "en", "sub_hearing": cfg.get("sub_hearing") or "normal",
            "yt_login": cfg.get("yt_login") or "", "yt_cookie_file": cfg.get("yt_cookie_file") or "",
            "patterns": {k: core.pattern(k, cfg) for k in core.PATTERNS},
            "pattern_defaults": {k: v[0] for k, v in core.PATTERNS.items()},
            "trakt_id_hint": (cfg.get("trakt_id") or "")[-4:], "has_trakt_secret": bool(cfg.get("trakt_secret")),
            "trakt_connected": trakt.connected(),
            "has_omdb_key": bool(cfg.get("omdb_key")), "omdb_key_hint": (cfg.get("omdb_key") or "")[-4:],
            "has_fanart_key": bool(cfg.get("fanart_key")), "fanart_key_hint": (cfg.get("fanart_key") or "")[-4:],
            "jellyfin_url": cfg.get("jellyfin_url") or "", "has_jellyfin_key": bool(cfg.get("jellyfin_key")),
            "jellyfin_key_hint": (cfg.get("jellyfin_key") or "")[-4:], "jellyfin_map": cfg.get("jellyfin_map") or "",
            "jellyfin_auto": cfg.get("jellyfin_auto", True), "watch": watch.settings(cfg),
            "keys_in_vault": vault.available(), "watch_notify": cfg.get("watch_notify", True)}


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
    if isinstance(data.get("patterns"), dict):
        changes["patterns"] = {k: core.check_pattern(k, data["patterns"].get(k, "")) for k in core.PATTERNS}
    for field in ("trakt_id", "trakt_secret"):
        value = (data.get(field) or "").strip()
        if value:
            changes[field] = value
            trakt.disconnect()  # a different app needs connecting again
    omdb_key = (data.get("omdb_key") or "").strip()
    if omdb_key:
        from . import ratings
        ratings.check_key(omdb_key)  # raises if OMDb refuses it
        changes["omdb_key"] = omdb_key
    fanart_key = (data.get("fanart_key") or "").strip()
    if fanart_key:
        artwork.check_fanart_key(fanart_key)  # raises if fanart.tv refuses it
        changes["fanart_key"] = fanart_key
    if data.get("fanart_key_clear"):
        changes["fanart_key"] = ""
    jf_info = None
    if "jellyfin_url" in data:
        jf_url = (data.get("jellyfin_url") or "").strip().rstrip("/")
        jf_key = (data.get("jellyfin_key") or "").strip()
        jf_map = (data.get("jellyfin_map") or "").strip()
        old = jellyfin.settings()
        if jf_url:
            jf_url = jellyfin.check_url(jf_url)
            if (jf_url, jf_key or old["key"], jf_map) != (old["url"], old["key"], old["map"]):
                info = jellyfin.check(jf_url, jf_key or old["key"], jf_map)  # raises if Jellyfin can't be reached
                jf_info = f"Connected to {info['server']} (Jellyfin {info['version']})." + (" " + " ".join(info["warnings"]) if info["warnings"] else "")
        changes.update(jellyfin_url=jf_url, jellyfin_map=jf_map, jellyfin_auto=bool(data.get("jellyfin_auto", True)))
        if jf_key:
            changes["jellyfin_key"] = jf_key
        if not jf_url:
            changes["jellyfin_key"] = ""
    if isinstance(data.get("watch"), dict):
        w = data["watch"]
        try:
            minutes = int(w.get("minutes") or 10)
        except (TypeError, ValueError):
            raise UserError("Enter how often to look for new files, in minutes.")
        if "notify" in w:
            changes["watch_notify"] = bool(w.get("notify"))
        changes.update(watch_on=bool(w.get("on")), watch_keep_alive=bool(w.get("keep_alive")),
                       watch_minutes=max(2, min(minutes, 24 * 60)),
                       watch_steps=[s for s in (w.get("steps") or []) if s in watch.STEPS])
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
                    "omdb_info": "OMDb accepted the key." if omdb_key else None,
                    "fanart_info": "fanart.tv accepted the key." if fanart_key else None, "jellyfin_info": jf_info})


@app.post("/api/jellyfin/test")
def api_jellyfin_test():
    d = body()
    s = jellyfin.settings()
    url = jellyfin.check_url(d.get("url") or s["url"])
    return jsonify(jellyfin.check(url, (d.get("key") or "").strip() or s["key"], d.get("map") if "map" in d else None))


@app.get("/api/jellyfin/check")
def api_jellyfin_check():
    return jsonify(jellyfin.settings_check())


@app.post("/api/jellyfin/fix")
def api_jellyfin_fix():
    d = body()
    return jsonify(jellyfin.fix(str(d.get("fix") or ""), str(d.get("library") or "")))


@app.post("/api/playback")
def api_playback():
    kind = "tv" if body().get("kind") == "tv" else "movies"
    leftover_root(kind)  # a scan is needed first
    return job_response("playback", "Reading the files", 0, lambda job: playback.check(job, kind))


@app.post("/api/jellyfin/refresh")
def api_jellyfin_refresh():
    return jsonify(jellyfin.refresh_all())


@app.post("/api/watch/now")
def api_watch_now():
    """Look for new files now, in the background (the page follows it like any other task)."""
    if watch.state["busy"]:
        raise UserError("Already looking for new files.")
    if core.job_running():
        raise UserError("Wait for the current task to finish first.")

    def run():
        try:
            watch.check_once(now=None)
        except Exception as err:
            watch.state["error"] = str(err)
    threading.Thread(target=run, daemon=True).start()
    return jsonify(ok=True)


@app.post("/api/trakt/connect")
def api_trakt_connect():
    return jsonify(trakt.start_connect())


@app.get("/api/trakt/connect")
def api_trakt_connect_status():
    return jsonify(trakt.poll_connect())


@app.post("/api/trakt/disconnect")
def api_trakt_disconnect():
    trakt.disconnect()
    return jsonify(settings_view(core.load_config()))


@app.post("/api/trakt/sync")
def api_trakt_sync():
    d = body()
    clear = bool(d.get("clear"))
    trakt.access_token()  # fail fast when not connected
    if d.get("kind") == "tv":
        root = core.require_tv_scan()
        return job_response("trakt", "Asking Trakt", 0, lambda job: trakt.sync_tv(job, root, clear))
    root = core.require_scan()
    return job_response("trakt", "Asking Trakt", 0, lambda job: trakt.sync_movies(job, root, clear))


def leftover_root(kind: str) -> Path:
    return core.require_tv_scan() if kind == "tv" else core.require_scan()


@app.get("/api/leftovers")
def api_leftovers():
    return jsonify(leftovers.find(leftover_root(request.args.get("kind", "movies"))))


@app.post("/api/leftovers/recycle")
def api_leftovers_recycle():
    d = body()
    root = leftover_root(d.get("kind", "movies"))
    with core.lock:
        return jsonify(leftovers.recycle(root, [str(p) for p in d.get("paths") or []]))


@app.post("/api/dupes")
def api_dupes():
    kind = "tv" if body().get("kind") == "tv" else "movies"
    leftover_root(kind)  # a scan is needed first
    return job_response("dupes", "Looking for duplicates", 0, lambda job: dupes.find(job, kind))


@app.post("/api/dupes/recycle")
def api_dupes_recycle():
    d = body()
    return jsonify(dupes.recycle("tv" if d.get("kind") == "tv" else "movies", [str(x) for x in d.get("ids") or []]))


@app.post("/api/folders/forget")
def api_forget_folder():
    d = body()
    cfg = core.forget_folder("tv" if d.get("kind") == "tv" else "movies", d.get("folder") or "")
    return jsonify(settings_view(cfg))


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
    core.remember_folder("movies", folder)
    refresh = bool(data.get("refresh"))

    def work(job):
        started = time.time()
        with core.lock:
            if core.state["root"] is not None and core.key_of(core.state["root"]) == core.key_of(root):
                before = {it["id"] for it in core.state["items"]}
            else:
                cache = core.load_scan("movies", root)
                before = {it["id"] for it in cache.get("items", [])} if cache else None
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
            cache = core.load_scan("movies", root)
            if not cache:
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


@app.post("/api/bulk-edit")
def api_bulk_edit():
    d = body()
    ids = [i for i in (d.get("ids") or []) if isinstance(i, str)]
    if not ids:
        raise UserError("Select some titles first.")
    kind = "tv" if d.get("kind") == "tv" else "movies"
    core.require_tv_scan() if kind == "tv" else core.require_scan()
    changes = d.get("changes") or {}
    bulk.plan(changes)  # refuse an empty request before starting
    return job_response("bulk", "Editing", len(ids), lambda job: bulk.run(job, kind, ids, changes))


@app.post("/api/collections")
def api_collections():
    root = core.require_scan()
    tmdbapi.tmdb("/configuration")
    def work(job):
        result = collections.run(job, root)
        core.state["collections"] = result  # kept for exporting
        return result

    return job_response("collections", "Checking for changes", 0, work)


@app.get("/api/export")
def api_export():
    kind, fmt = request.args.get("kind", "movies"), request.args.get("format", "csv")
    name, data, ctype = export.export(kind, fmt, core.state.get("collections"))
    return Response(data, mimetype=ctype.split(";")[0], headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/open-folder")
def api_open_folder():
    movie.open_folder(body().get("id", ""))
    return jsonify(ok=True)


@app.post("/api/play")
def api_play():
    """Play a movie, or a show's first episode, in your usual video player."""
    d = body()
    if d.get("kind") == "tv":
        root = core.require_tv_scan()
        from . import tvlib
        eps = sorted((i for i in tvlib.with_matches(list(core.tv_state["items"]), root) if i["key"] == d.get("id") and i["kind"] == "episode"),
                     key=lambda i: (i["season"] or 99, i["episodes"]))
        if not eps:
            raise UserError("That show has no episodes to play.")
        video = Path(root, eps[0]["path"])
    else:
        root = core.require_scan()
        it = next((i for i in core.state["items"] if i["id"] == d.get("id")), None)
        if not it:
            raise UserError("That file is no longer in the library. Scan again.")
        video = Path(root, it["path"])
    if not video.exists():
        raise UserError("That file no longer exists. Scan again to update the list.")
    if os.name != "nt":
        raise UserError("Playing files only works on Windows.")
    os.startfile(str(video))
    return jsonify(ok=True, name=video.name)


@app.post("/api/jellyfin/find")
def api_jellyfin_find():
    """The Jellyfin page of a movie or show, to open it there."""
    d = body()
    if d.get("kind") == "tv":
        root = core.require_tv_scan()
        from . import tvlib
        eps = [i for i in tvlib.with_matches(list(core.tv_state["items"]), root) if i["key"] == d.get("id")]
        if not eps or not eps[0].get("show_dir"):
            raise UserError("That show isn't in a show folder, so Jellyfin can't be asked about it.")
        m = eps[0].get("match") or {}
        return jsonify(jellyfin.find_item(str(root / eps[0]["show_dir"]), m.get("title") or eps[0].get("series") or "", "Series"))
    root = core.require_scan()
    items = library.with_matches([dict(i) for i in core.state["items"]])
    it = next((i for i in items if i["id"] == d.get("id")), None)
    if not it:
        raise UserError("That file is no longer in the library. Scan again.")
    m = it.get("match") or {}
    return jsonify(jellyfin.find_item(str(Path(root, it["path"])), m.get("title") or it["title"], "Movie"))


@app.get("/api/backups")
def api_backups():
    return jsonify(backups=backup.listing(), folder=str(backup.folder()), keep=backup.KEEP)


@app.post("/api/backups/make")
def api_backup_make():
    made = backup.make("manual")
    return jsonify(made=made, backups=backup.listing())


@app.post("/api/backups/restore")
def api_backup_restore():
    r = backup.restore(body().get("id") or "")
    return jsonify(**r, backups=backup.listing())


@app.post("/api/logs/open")
def api_logs_open():
    """Show the log file in Explorer (for when something went wrong)."""
    f = logs.log_file()
    if os.name != "nt":
        raise UserError("Opening folders only works on Windows.")
    if f.exists():
        subprocess.Popen(["explorer", "/select,", str(f)])
    else:
        logs.LOG_DIR.mkdir(exist_ok=True)
        os.startfile(str(logs.LOG_DIR))
    return jsonify(ok=True)


@app.post("/api/notify/test")
def api_notify_test():
    if not notify.show("Media manager", "Notifications are working. You'll see one like this when new files have been handled."):
        raise UserError("Windows notifications couldn't be shown here.")
    return jsonify(ok=True)


# ---------------------------------------------------------------- converting to H.265 / AV1

def _kind() -> str:
    return "tv" if (request.args.get("kind") or (request.get_json(silent=True) or {}).get("kind")) == "tv" else "movies"


@app.get("/api/convert")
def api_convert():
    found = convert.encoders()
    cfg = core.load_config()
    drives = {}
    for key in ("folder", "tv_folder"):
        f = cfg.get(key)
        if f and Path(f).is_dir():
            d = os.path.splitdrive(str(Path(f).resolve()))[0] or f
            if d not in drives:
                drives[d] = core.recycle_capacity(Path(f))
    return jsonify(settings=convert.settings(cfg), status=convert.status(),
                   encoders={c: [{"id": e, "label": convert.encoder_label(e)} for e in found.get(c, [])] for c in convert.CODECS},
                   bins=[{"drive": d, "capacity": cap} for d, cap in drives.items()])


@app.get("/api/convert/candidates")
def api_convert_candidates():
    s = convert.settings()
    return jsonify(convert.candidates(_kind(), s["quality"], s["codec"]))


@app.post("/api/convert/settings")
def api_convert_settings():
    d = body()
    changes = {}
    if d.get("codec") in convert.CODECS:
        changes["convert_codec"] = d["codec"]
    if d.get("quality") in convert.QUALITY:
        changes["convert_quality"] = d["quality"]
    if d.get("encoder") in ("auto", "gpu", "cpu"):
        changes["convert_encoder"] = d["encoder"]
    if "check" in d:
        changes["convert_check"] = bool(d["check"])
    cfg = core.save_config(**changes)
    return jsonify(settings=convert.settings(cfg))


@app.post("/api/convert/queue")
def api_convert_queue():
    d = body()
    paths = [p for p in (d.get("paths") or []) if isinstance(p, str)]
    if not paths:
        raise UserError("Choose some files to convert first.")
    convert.add(paths, _kind())
    return jsonify(convert.start() if d.get("start") else convert.status())


@app.post("/api/convert/remove")
def api_convert_remove():
    d = body()
    return jsonify(convert.remove([p for p in (d.get("paths") or []) if isinstance(p, str)] or None))


@app.post("/api/convert/start")
def api_convert_start():
    return jsonify(convert.start())


@app.post("/api/convert/stop")
def api_convert_stop():
    return jsonify(convert.stop(bool(body().get("now"))))


@app.get("/api/convert/status")
def api_convert_status():
    return jsonify(convert.status())


# ---------------------------------------------------------------- smart collections in Jellyfin

@app.get("/api/smart")
def api_smart():
    return jsonify(collections=smart.listing(), presets=smart.PRESETS, jellyfin=jellyfin.configured())


@app.post("/api/smart/preview")
def api_smart_preview():
    d = body()
    return jsonify(smart.preview(d.get("kind") or "movies", d.get("query") or "", d.get("tmdb_list") or ""))


@app.post("/api/smart/save")
def api_smart_save():
    return jsonify(smart.save(body()))


@app.post("/api/smart/delete")
def api_smart_delete():
    return jsonify(collections=smart.delete(body().get("id") or ""))


@app.post("/api/smart/sync")
def api_smart_sync():
    return jsonify(smart.sync_all())


# ---------------------------------------------------------------- poster badges

@app.get("/api/badges")
def api_badges():
    return jsonify(settings=badges.settings(), count=badges.count())


@app.post("/api/badges/settings")
def api_badges_settings():
    d = body()
    show = d.get("show") if isinstance(d.get("show"), dict) else {}
    cfg = core.save_config(badges_on=bool(d.get("on")), badge_show={k: bool(show.get(k)) for k in badges.KINDS},
                           badge_position="bottom" if d.get("position") == "bottom" else "top")
    return jsonify(settings=badges.settings(cfg), count=badges.count())


@app.get("/api/badges/preview")
def api_badges_preview():
    a = request.args
    show = {k: a.get(k) == "1" for k in badges.KINDS}
    data, mime = badges.preview(a.get("id", ""), show, a.get("position", "top"), plain=a.get("plain") == "1")
    return Response(data, mimetype=mime, headers={"Cache-Control": "no-store"})


@app.post("/api/badges/apply")
def api_badges_apply():
    root = core.require_scan()
    return job_response("badges", "Adding badges to posters", 0, lambda job: {**badges.apply_all(job, root=root), "count": badges.count()})


@app.post("/api/badges/remove")
def api_badges_remove():
    return job_response("badges-remove", "Removing badges", 0, lambda job: {**badges.remove_all(job), "count": badges.count()})


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
    with core.job_lock:
        running = next(({"id": j["id"], "label": j["label"]} for j in core.jobs.values() if not j["finished"]), None)
    w = watch.state
    cur = convert.state["current"]
    conv = {"running": True, "name": (cur or {}).get("name"), "percent": (cur or {}).get("percent"),
            "phase": (cur or {}).get("phase")} if convert.busy() else None
    return jsonify(ok=True, version=APP_VERSION, job=running, convert=conv,
                   watch={"version": w["version"], "busy": w["busy"], "message": watch.describe(w["last"]),
                          "kind": (w["last"] or {}).get("kind"), "waiting": w["waiting"], "error": w["error"],
                          "last_check": w["last_check"]},
                   jellyfin={**jellyfin.status, "configured": jellyfin.configured()})


@app.post("/api/bye")
def api_bye():
    alive["bye"] = time.time()
    return ("", 204)


def watchdog() -> None:
    while True:
        time.sleep(2)
        if alive["last"] is None or core.job_running() or watch.state["busy"] or convert.busy():  # never opened yet, or busy: keep going
            continue
        if watch.settings()["on"] and watch.settings()["keep_alive"]:  # watching for new files after the page closes
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
    if not info or info.get("Name", "").lower() not in ("python.exe", "pythonw.exe", "mediamanager.exe"):
        return False  # not Python or the packaged app: certainly not ours
    # Walk up to the window it runs in: Python (and the .venv launcher), then the run.bat console
    top, parent, from_our_bat = pid, info.get("ParentProcessId"), False
    for _ in range(3):
        p = process_info(parent) if parent else {}
        name, line = p.get("Name", "").lower(), (p.get("CommandLine") or "").lower()
        if name in ("python.exe", "pythonw.exe"):
            top, parent = parent, p.get("ParentProcessId")
        elif name == "cmd.exe" and str(core.RES / "run.bat").lower() in line:
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


def app_mode() -> None:
    """What only the real app (and its command line) does: keep History, keep keys in Credential Manager, make
    daily backups, write the log file, pop up notifications. Tests leave all of this off."""
    history.enabled = True
    if core.CONFIG != core.BASE / "config.json" or core.ISOLATED:
        return  # a test, or MM_ISOLATED: never touch your credentials, backups, log or screen
    vault.enabled = backup.enabled = notify.enabled = True
    logs.setup()
    try:
        core.save_config()  # moves any keys still in config.json into Credential Manager
    except OSError:
        pass
    try:
        backup.daily()
    except OSError:
        pass


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
    app_mode()
    jellyfin.start()
    watch.start()
    backup.start()
    if not os.environ.get("MM_NO_BROWSER"):
        threading.Timer(3.0, open_browser_unless_reconnected, args=(port,)).start()
    print(f"Media manager is running at http://127.0.0.1:{port}")
    print("Closing the browser tab shuts it down. You can also close this window.")
    app.run(host="127.0.0.1", port=port, threaded=True)
