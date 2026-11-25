"""Trakt: bring your watched status (watched, play count, last watched) into the .nfo files.

Connecting uses Trakt's device login: the app shows a short code, you enter it at trakt.tv/activate, and no
password passes through the app. Trakt requires each app to be registered, so you create a free API app on
trakt.tv and paste its Client ID and Secret in Settings.
"""
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import core, library, nfo, tvlib, tvmeta
from .core import UserError

API = "https://api.trakt.tv"


def token_path() -> Path:
    return core.MATCHES.with_name("trakt.json")  # beside the other data files; never committed


def creds():
    cfg = core.load_config()
    cid, secret = (cfg.get("trakt_id") or "").strip(), (cfg.get("trakt_secret") or "").strip()
    if not cid or not secret:
        raise UserError("Add your Trakt app's Client ID and Secret in Settings first.")
    return cid, secret


def call(method: str, path: str, data: dict = None, auth: bool = True):
    cid, _ = creds()
    headers = {"Content-Type": "application/json", "trakt-api-version": "2", "trakt-api-key": cid, "User-Agent": core.USER_AGENT}
    if auth:
        headers["Authorization"] = "Bearer " + access_token()
    req = urllib.request.Request(API + path, data=json.dumps(data).encode() if data is not None else None, headers=headers, method=method)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
                return resp.status, (json.loads(body) if body else None)
        except urllib.error.HTTPError as err:
            if err.code == 429 and attempt < 2:
                time.sleep(int(err.headers.get("Retry-After") or 2))
                continue
            return err.code, None
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt < 1:
                continue
            raise UserError("Couldn't reach Trakt. Check your internet connection.") from None
    return 429, None


# ---------------------------------------------------------------- connecting (device login)

def start_connect() -> dict:
    cid, _ = creds()
    status, d = call("POST", "/oauth/device/code", {"client_id": cid}, auth=False)
    if status != 200 or not d:
        raise UserError("Trakt didn't accept the Client ID. Check it in Settings.")
    core.save_json(token_path(), {"pending": {"device_code": d["device_code"], "interval": d.get("interval", 5),
                                              "expires": time.time() + d.get("expires_in", 600)}})
    return {"code": d["user_code"], "url": d.get("verification_url") or "https://trakt.tv/activate", "interval": d.get("interval", 5)}


def poll_connect() -> dict:
    """'waiting' until you've entered the code, then 'connected' (or 'expired' / 'denied')."""
    cid, secret = creds()
    saved = core.load_json(token_path(), {})
    pending = saved.get("pending")
    if saved.get("access_token"):
        return {"state": "connected"}
    if not pending:
        return {"state": "none"}
    if time.time() > pending["expires"]:
        return {"state": "expired"}
    status, d = call("POST", "/oauth/device/token", {"code": pending["device_code"], "client_id": cid, "client_secret": secret}, auth=False)
    if status == 200 and d:
        core.save_json(token_path(), {"access_token": d["access_token"], "refresh_token": d["refresh_token"],
                                      "expires": d.get("created_at", time.time()) + d.get("expires_in", 7776000)})
        return {"state": "connected"}
    if status in (400, 429):
        return {"state": "waiting"}
    if status == 418:
        return {"state": "denied"}
    if status in (404, 409, 410):
        return {"state": "expired"}
    if status == 401:
        raise UserError("Trakt didn't accept the Client Secret. Check it in Settings.")
    return {"state": "waiting"}


def access_token() -> str:
    saved = core.load_json(token_path(), {})
    if not saved.get("access_token"):
        raise UserError("Connect to Trakt in Settings first.")
    if time.time() > saved.get("expires", 0) - 86400:  # renew a day before it runs out
        cid, secret = creds()
        req = urllib.request.Request(API + "/oauth/token", method="POST", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"refresh_token": saved["refresh_token"], "client_id": cid, "client_secret": secret,
                                                      "redirect_uri": "urn:ietf:wg:oauth:2.0:oob", "grant_type": "refresh_token"}).encode())
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                d = json.load(resp)
        except (urllib.error.URLError, OSError):
            raise UserError("Trakt's login has run out. Connect to Trakt again in Settings.") from None
        saved = {"access_token": d["access_token"], "refresh_token": d["refresh_token"],
                 "expires": d.get("created_at", time.time()) + d.get("expires_in", 7776000)}
        core.save_json(token_path(), saved)
    return saved["access_token"]


def connected() -> bool:
    return bool(core.load_json(token_path(), {}).get("access_token"))


def disconnect() -> None:
    try:
        token_path().unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- writing watched status into .nfo files

def stamp(when: str) -> str:
    """Trakt's '2024-05-01T20:15:00.000Z' as Kodi's '2024-05-01 20:15:00'."""
    return (when or "").replace("T", " ")[:19]


def set_watched(root, plays: int, last: str, clear: bool) -> bool:
    """Returns True when the .nfo entry changed."""
    if plays:
        want = {"watched": "true", "playcount": str(plays), "lastplayed": stamp(last)}
    elif clear:
        want = {"watched": "false", "playcount": "0"}
    else:
        return False
    changed = False
    for tag, value in want.items():
        el = root.find(tag)
        if el is None:
            el = nfo.ET.SubElement(root, tag)
        if (el.text or "") != value:
            el.text = value
            changed = True
    return changed


def sync_movies(job: dict, root: Path, clear: bool) -> dict:
    status, watched = call("GET", "/sync/watched/movies")
    if status != 200:
        raise UserError("Trakt didn't send your watched movies. Connect to Trakt again in Settings.")
    plays = {w["movie"]["ids"].get("tmdb"): (w.get("plays", 1), w.get("last_watched_at")) for w in watched or [] if w["movie"]["ids"].get("tmdb")}
    with core.lock:
        items = library.rescan(job, "Checking for changes")
    folders = library.by_folder(items, root)
    todo = [it for it in items if it.get("match") and it["match"]["status"] in core.MATCHED]
    core.job_step(job, "Updating watched status", len(todo))
    res = {"updated": 0, "watched": 0, "no_nfo": 0, "on_trakt": len(plays)}
    for it in todo:
        core.tick(job)
        p, last = plays.get(it["match"]["tmdb_id"], (0, None))
        res["watched"] += bool(p)
        path = nfo.nfo_path(Path(root, it["path"]), library.own_folder(it, folders, root))
        if not path:
            res["no_nfo"] += 1
            continue
        r = nfo.parse_xml(path.read_bytes())
        if r is not None and set_watched(r, p, last, clear):
            path.write_bytes(nfo.to_bytes(r))
            res["updated"] += 1
    with core.lock:
        res["items"] = library.current_items()
    return res


def sync_tv(job: dict, root: Path, clear: bool) -> dict:
    status, watched = call("GET", "/sync/watched/shows")
    if status != 200:
        raise UserError("Trakt didn't send your watched shows. Connect to Trakt again in Settings.")
    by_show = {}
    for w in watched or []:
        tid = w["show"]["ids"].get("tmdb")
        if tid:
            by_show[tid] = {(s["number"], e["number"]): (e.get("plays", 1), e.get("last_watched_at"))
                            for s in w.get("seasons", []) for e in s.get("episodes", [])}
    with core.lock:
        items = tvlib.rescan(job, "Checking for changes")
    todo = [it for it in items if it["kind"] == "episode" and tvlib.confirmed(it["match"])]
    core.job_step(job, "Updating watched status", len(todo))
    res = {"updated": 0, "watched": 0, "no_nfo": 0, "on_trakt": sum(len(v) for v in by_show.values())}
    for it in todo:
        core.tick(job)
        m = it["match"]
        eps = by_show.get(m["tmdb_id"], {})
        data = tvmeta.cached_for(m) if m.get("order") or m.get("season_orders") else None
        path = Path(root, it["path"]).with_suffix(".nfo")
        if not path.is_file():
            res["no_nfo"] += 1
            continue
        roots = nfo.parse_all(path.read_bytes())
        changed = False
        for r, n in zip(roots, it["episodes"]):
            nums = tvmeta.tmdb_numbers(data, it["season"], n) if data else (it["season"], n)  # Trakt goes by TMDB's numbers
            p, last = eps.get(nums, (0, None)) if nums else (0, None)
            res["watched"] += bool(p)
            changed |= set_watched(r, p, last, clear)
        if changed:
            path.write_bytes(nfo.to_bytes_all(roots))
            res["updated"] += 1
    res.update(tvlib.current())
    return res
