"""The TV half of the web app's API (/api/tv/...). Mirrors the movie endpoints."""
import time
from pathlib import Path

from flask import jsonify, request, send_file

from . import core, subtitles, trailers, tvart, tvextras, tvlib, tvmeta, tvnfo, tvrename, tvshow
from .core import UserError
from .web import app, body, job_response


@app.post("/api/tv/scan")
def tv_scan():
    data = body()
    folder = (data.get("folder") or "").strip().strip('"')
    root = Path(folder)
    if not folder or not root.is_dir():
        return jsonify(error=f"Folder not found: {folder or '(empty)'}. Check the path and try again."), 400
    core.save_config(tv_folder=folder)
    refresh = bool(data.get("refresh"))

    def work(job):
        started = time.time()
        with core.lock:
            same = core.tv_state["root"] is not None and core.key_of(core.tv_state["root"]) == core.key_of(root)
            if same:
                before = {it["id"] for it in core.tv_state["items"]}
            else:
                cache = core.load_json(core.TV_LIBRARY, {})
                before = {it["id"] for it in cache.get("items", [])} if cache.get("root") == core.key_of(root) else None
            core.tv_state["root"] = root
            tvlib.rescan(job, "Checking for changes" if refresh else "Reading episode names")
            after = {it["id"] for it in core.tv_state["items"]}
        changes = None if before is None else {"added": len(after - before), "removed": len(before - after)}
        return {**tvlib.current(), "seconds": round(time.time() - started, 1), "changes": changes}

    return job_response("tvscan", "Checking for changes" if refresh else "Finding episodes", 0, work)


@app.get("/api/tv/library")
def tv_library():
    folder = core.load_config().get("tv_folder") or ""
    if not folder:
        return jsonify(shows=None)
    root = Path(folder)
    if not root.is_dir():
        return jsonify(shows=None, folder=folder, missing=True)
    with core.lock:
        if core.tv_state["root"] is None or core.key_of(core.tv_state["root"]) != core.key_of(root):
            cache = core.load_json(core.TV_LIBRARY, {})
            if cache.get("root") != core.key_of(root) or cache.get("v") != tvlib.TV_VERSION:
                return jsonify(shows=None, folder=folder)
            core.tv_state["root"] = root
            core.tv_state["items"] = cache.get("items", [])
        return jsonify(**tvlib.current(), folder=folder)


@app.post("/api/tv/match-all")
def tv_match_all():
    root = core.require_tv_scan()
    redo = bool(body().get("redo"))
    with core.lock:
        items = tvlib.with_matches(core.tv_state["items"], root)
    shows = {}
    for it in items:
        shows.setdefault(it["key"], it)
    todo = [dict(s) for s in shows.values()
            if not s["match"] or s["match"]["status"] == "none" or (redo and s["match"]["status"] != "manual")]
    if todo:
        tvmeta.tmdb("/configuration")
    return job_response("tvmatch", "Matching on TMDB", len(todo), lambda job: tvmeta.match_all(job, todo, root))


@app.get("/api/tv/search")
def tv_search():
    q = request.args.get("q", "").strip()
    if not q:
        raise UserError("Type a title to search for.")
    year = request.args.get("year", "").strip()
    return jsonify(results=tvmeta.search(q, int(year) if year.isdigit() else None))


@app.post("/api/tv/match")
def tv_match():
    data = body()
    tvmeta.set_match(data.get("key", ""), data)
    with core.lock:
        return jsonify(tvlib.current())


@app.get("/api/tv/show")
def tv_show():
    return jsonify(tvshow.detail(request.args.get("key", "")))


@app.post("/api/tv/refresh")
def tv_refresh():
    """Re-fetch a show's episode list from TMDB now (it's otherwise refreshed weekly)."""
    key = body().get("key", "")
    with core.lock:
        eps, _, _ = tvshow.find(key)
    m = eps[0]["match"]
    if not tvlib.confirmed(m):
        raise UserError("Match this show first.")
    tvmeta.data_for(m, refresh=True)
    return jsonify(tvshow.detail(key))


@app.get("/api/tv/orders")
def tv_orders():
    """The episode orders this show can follow, and how well each fits your files."""
    return jsonify(tvshow.order_fit(request.args.get("key", "")))


@app.post("/api/tv/order")
def tv_order():
    d = body()
    return jsonify(tvshow.set_order(d.get("key", ""), d.get("order") or None, d.get("season_orders") or {}))


@app.post("/api/tv/edit")
def tv_edit():
    data = body()
    return jsonify(tvshow.edit(data.get("key", ""), data.get("fields") or {}, reset=bool(data.get("reset"))))


@app.get("/api/tv/image")
def tv_image():
    a = request.args
    path, mimetype = tvshow.image(a.get("key", ""), a.get("kind", ""), a.get("season"), a.get("id"))
    return send_file(path, mimetype=mimetype, max_age=300)


@app.get("/api/tv/artwork/options")
def tv_artwork_options():
    a = request.args
    return jsonify(options=tvshow.art_options(a.get("key", ""), a.get("kind", ""), a.get("season")))


@app.post("/api/tv/artwork/set")
def tv_artwork_set():
    d = body()
    return jsonify(tvshow.set_art(d.get("key", ""), d.get("kind", ""), d.get("url", ""), d.get("season")))


@app.post("/api/tv/open-folder")
def tv_open_folder():
    tvshow.open_folder(body().get("key", ""))
    return jsonify(ok=True)


@app.post("/api/tv/delete/preview")
def tv_delete_preview():
    d = body()
    return jsonify(tvshow.delete_preview(d.get("key", ""), d.get("what", ""), d.get("season"), d.get("id")))


@app.post("/api/tv/delete")
def tv_delete():
    d = body()
    return jsonify(tvshow.delete(d.get("key", ""), d.get("what", ""), d.get("season"), d.get("id")))


@app.get("/api/tv/rename/preview")
def tv_rename_preview():
    return jsonify(tvrename.preview())


@app.post("/api/tv/rename/apply")
def tv_rename_apply():
    wanted = set(map(str, body().get("ids") or []))
    root = core.require_tv_scan()
    if not wanted:
        raise UserError("Pick at least one show to rename.")
    return job_response("tvrename", "Checking files", 0, lambda job: tvrename.apply(job, wanted, root))


@app.post("/api/tv/rename/undo")
def tv_rename_undo():
    return jsonify(tvrename.undo())


@app.post("/api/tv/nfo")
def tv_nfo():
    overwrite = bool(body().get("overwrite"))
    root = core.require_tv_scan()
    tvmeta.tmdb("/configuration")
    return job_response("tvnfo", "Checking for changes", 0, lambda job: tvnfo.write_all(job, root, overwrite))


@app.post("/api/tv/artwork")
def tv_artwork():
    d = body()
    kinds = [k for k in (d.get("kinds") or []) if k in tvart.ALL_KINDS]
    if not kinds:
        raise UserError("Choose at least one kind of artwork.")
    root = core.require_tv_scan()
    tvmeta.tmdb("/configuration")
    return job_response("tvartwork", "Checking for changes", 0, lambda job: tvart.run(job, root, kinds, bool(d.get("replace"))))


@app.post("/api/tv/trailers")
def tv_trailers():
    d = body()
    quality = int(d.get("quality") or 1080)
    if quality not in trailers.QUALITY_WIDTH:
        raise UserError("Pick 1080p, 720p or 480p.")
    root = core.require_tv_scan()
    tvmeta.tmdb("/configuration")
    return job_response("tvtrailers", "Checking for changes", 0,
                        lambda job: tvextras.trailers_run(job, root, quality, bool(d.get("force"))))


@app.post("/api/tv/subtitles")
def tv_subtitles():
    d = body()
    langs = subtitles.sub_langs(d.get("languages") or core.load_config().get("sub_langs") or "en")
    if not langs:
        raise UserError("Choose at least one subtitle language.")
    hearing = d.get("hearing") if d.get("hearing") in ("normal", "prefer") else "normal"
    root = core.require_tv_scan()
    if not subtitles.os_login():
        subtitles.check_key()
    core.save_config(sub_langs=", ".join(langs), sub_hearing=hearing)
    return job_response("tvsubtitles", "Checking for changes", 0,
                        lambda job: tvextras.subtitles_run(job, root, langs, hearing, d.get("skip_embedded", True) is not False))
