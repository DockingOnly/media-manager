"""Watching for new files: every few minutes, look in your movie and TV folders for video files the app hasn't
seen. Once a new file has stopped growing (finished copying or downloading), run the safe steps on it: scan,
match on TMDB, write .nfo files, download artwork (and subtitles or trailers, if you chose them). Every step
only fills in what's missing, so nothing you already have is changed. Renaming and moving always wait for you:
the page says what arrived and what needs renaming.

Runs inside the app while it's open (Settings: keep watching after the page closes), or on its own with
    .venv\\Scripts\\python app.py watch
"""
import os
import threading
import time
from pathlib import Path

from . import core, library, tvlib

STEPS = ("match", "nfo", "artwork", "subtitles", "trailers")
DEFAULT_STEPS = ["match", "nfo", "artwork"]
SETTLE = 60  # seconds a new file must stay the same size before it's touched
ART = {"movies": ["poster", "fanart"], "tv": ["poster", "fanart", "season", "thumb"]}
state = {"last_check": None, "busy": False, "version": 0, "last": None, "error": None, "waiting": []}
_seen = {"movies": {}, "tv": {}}  # per library: path -> (size, mtime) from the previous look


def settings(cfg: dict = None) -> dict:
    cfg = core.load_config() if cfg is None else cfg
    steps = [s for s in (cfg["watch_steps"] if "watch_steps" in cfg else DEFAULT_STEPS) if s in STEPS]  # [] = scan only
    return {"on": bool(cfg.get("watch_on")), "minutes": max(2, int(cfg.get("watch_minutes") or 10)),
            "steps": steps, "keep_alive": bool(cfg.get("watch_keep_alive"))}


def videos(kind: str, root: Path) -> dict:
    """Every video file the scanner would pick up: {id: (path, size, mtime)}."""
    out = {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not library.skip_dir(d) and (kind != "tv" or d.lower() not in tvlib.SKIP_DIRS)]
        for name in files:
            p = Path(dirpath, name)
            if p.suffix.lower() in core.VIDEO_EXTS and not library.SKIP_FILE.search(p.stem):
                try:
                    st = p.stat()
                except OSError:
                    continue
                out[core.key_of(p)] = (p, st.st_size, st.st_mtime)
    return out


def look(kind: str, root: Path, now: float = None) -> dict:
    """What changed since the last scan: new files ready to be handled, new files still arriving, files gone."""
    now = time.time() if now is None else now
    known = {it["id"] for it in core.load_scan(kind, root).get("items", [])}
    found = videos(kind, root)
    ready, arriving = [], []
    seen = _seen.setdefault(kind, {})
    for k, (p, size, mtime) in found.items():
        if k in known:
            continue
        before = seen.get(k)
        if before == (size, mtime) and now - mtime >= SETTLE:
            ready.append(p)
        else:
            arriving.append(p)
        seen[k] = (size, mtime)
    for k in [k for k in seen if k not in found]:
        del seen[k]
    return {"ready": ready, "arriving": arriving, "gone": len(known - set(found))}


def handle(kind: str, folder: str, steps: list, client) -> dict:
    """Scan, then the chosen steps, through the app's own endpoints. Returns what happened."""
    tv = kind == "tv"
    pre = "/api/tv" if tv else "/api"

    def job(url, data):
        r = client.post(url, json=data)
        d = r.get_json(silent=True) or {}
        if r.status_code >= 400:
            raise core.UserError(d.get("error") or f"{url} failed")
        while True:
            j = client.get(f"/api/job/{d['job']}").get_json()
            if j["finished"]:
                if j["error"]:
                    raise core.UserError(j["error"])
                return j["result"]
            time.sleep(0.5)

    before = {it["id"] for it in core.load_scan(kind, Path(folder)).get("items", [])}
    job(f"{pre}/scan", {"folder": folder, "refresh": True})
    done, problem = ["scanned"], None
    for step in steps:
        try:
            if step == "match":
                job(f"{pre}/match-all", {})
            elif step == "nfo":
                job(f"{pre}/nfo", {"overwrite": False, "artwork": False})
            elif step == "artwork":
                job(f"{pre}/artwork", {"kinds": ART[kind], "replace": False})
            elif step == "subtitles":
                if not core.load_config().get("os_key"):
                    continue
                job(f"{pre}/subtitles", {})
            elif step == "trailers":
                job(f"{pre}/trailers", {})
        except core.UserError as err:  # the scan is in; the rest is tried again next time
            problem = f"{step} didn't finish ({err}); it's tried again next time."
            break
        done.append(step)
    with core.lock:
        if tv:
            items = tvlib.with_matches(list(core.tv_state["items"]), core.tv_state["root"])
        else:
            items = library.current_items()
    new = [it for it in items if it["id"] not in before]
    names = []
    for it in new:
        m = it.get("match") or {}
        if tv:
            code = tvlib.episode_code(it["season"], it["episodes"]) if it.get("episodes") else "extra"
            names.append(f"{(m.get('name') or it.get('series') or '?')} {code}")
        else:
            names.append(m.get("name") or (f"{m['title']} ({m['year']})" if m.get("title") else it["title"]))
    unmatched = sum(1 for it in new if (it.get("match") or {}).get("status") not in core.MATCHED)
    return {"kind": kind, "time": time.time(), "added": names, "unmatched": unmatched, "steps": done, "problem": problem}


def check_once(client=None, now: float = None) -> list:
    """One look at both libraries; handles whatever is ready. Returns the summaries of what was handled."""
    s = settings()
    cfg = core.load_config()
    results = []
    state["last_check"] = time.time()
    waiting = []
    for kind, key in (("movies", "folder"), ("tv", "tv_folder")):
        folder = cfg.get(key) or ""
        if not folder or not Path(folder).is_dir():
            continue
        seen = look(kind, Path(folder), now)
        waiting += [p.name for p in seen["arriving"]]
        if not (seen["ready"] or seen["gone"]) or seen["arriving"] or core.job_running():
            continue  # nothing new, or wait until everything new has finished arriving
        if client is None:
            from . import web
            client = web.app.test_client()
        state["busy"] = True
        core.automatic.set()
        try:
            r = handle(kind, folder, s["steps"], client)
            r["gone"] = seen["gone"]
            results.append(r)
            state.update(last=r, error=r["problem"], version=state["version"] + 1)
            from . import notify
            notify.watch_result(r)
        except core.UserError as err:  # e.g. you started something meanwhile: try again next time
            state["error"] = str(err)
        finally:
            core.automatic.clear()
            state["busy"] = False
    state["waiting"] = waiting
    return results


def describe(r: dict) -> str:
    if not r:
        return ""
    what = "movie file" if r["kind"] == "movies" else "episode file"
    parts = []
    if r["added"]:
        shown = ", ".join(r["added"][:5]) + (f" and {len(r['added']) - 5} more" if len(r["added"]) > 5 else "")
        n = len(r["added"])
        parts.append(f"{n} new {what}{'' if n == 1 else 's'}: {shown}. Done: {', '.join(r['steps'])}.")
        if r["unmatched"] and "match" in r["steps"]:
            parts.append(f"{r['unmatched']} couldn't be matched on TMDB by themselves; match them by hand.")
    if r.get("problem"):
        parts.append(r["problem"][0].upper() + r["problem"][1:])
    if r.get("gone"):
        parts.append(f"{r['gone']} {what}{'' if r['gone'] == 1 else 's'} no longer there.")
    return " ".join(parts)


def loop(say=None) -> None:
    """Check every few minutes (as set in Settings) while watching is on."""
    last = 0.0
    while True:
        time.sleep(15)
        s = settings()
        wait = SETTLE if state["waiting"] else s["minutes"] * 60  # something arriving: look again in a minute
        if not s["on"] or time.time() - last < wait:
            continue
        last = time.time()
        try:
            handled = check_once()
            for r in handled:
                if say:
                    say(describe(r))
            from . import smart
            smart.maybe_sync(handled)  # smart collections in Jellyfin follow what was added
        except Exception as err:  # never let this stop the app
            state["error"] = f"Watching stopped for a moment: {err}"


def start() -> None:
    threading.Thread(target=loop, daemon=True, name="watch").start()
