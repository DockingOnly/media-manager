"""Subtitles from OpenSubtitles.com, picked to match your exact file and named for Jellyfin."""
import json
import re
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import core, library
from .core import QuotaError, SubError, UserError

OS_HOST = "api.opensubtitles.com"
OS_AGENT = "MediaManager v0.8"
os_session = {"token": None, "base": None, "user": None, "creds": None}


def os_request(method: str, path: str, params: dict = None, data: dict = None, key: str = None, host: str = None):
    cfg = core.load_config()
    key = (key or cfg.get("os_key") or "").strip()
    if not key:
        raise UserError("Add your OpenSubtitles API key in Settings first.")
    headers = {"Api-Key": key, "User-Agent": OS_AGENT, "Accept": "application/json",
               "Content-Type": "application/json"}
    if os_session["token"] and path != "/login":
        headers["Authorization"] = "Bearer " + os_session["token"]
    url = f"https://{host or os_session['base'] or OS_HOST}/api/v1{path}"
    if params:  # OpenSubtitles wants sorted, lower-case parameters
        url += "?" + urllib.parse.urlencode(sorted((k.lower(), str(v).lower()) for k, v in params.items()
                                                   if v not in (None, "")))
    payload = json.dumps(data).encode() if data is not None else None
    for attempt in range(4):
        req = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as err:
            try:
                info = json.loads(err.read() or b"{}")
            except ValueError:
                info = {}
            msg = info.get("message") or (info.get("errors") or [""])[0] or ""
            if err.code in (429, 500, 502, 503, 504) and attempt < 3:  # busy or briefly down: wait and retry
                time.sleep(2 + attempt * 3)
                continue
            if err.code == 406 or ("download" in msg.lower() and "limit" in msg.lower()):
                reset = info.get("reset_time") or ""
                raise QuotaError(f"OpenSubtitles daily download limit reached{f' (resets in {reset})' if reset else ''}.")
            if err.code in (401, 403):
                raise SubError(msg or "OpenSubtitles rejected the API key, username or password. Check them in Settings.")
            raise SubError(f"OpenSubtitles error {err.code}{': ' + msg if msg else ''}")
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt < 1:
                continue
            raise SubError("Couldn't reach OpenSubtitles. Check your internet connection.")
    raise SubError("OpenSubtitles is busy. Try again in a minute.")


def os_login(force: bool = False, key: str = None, user: str = None, password: str = None) -> dict:
    """Log in when a username is set, so downloads count against your account's daily allowance."""
    cfg = core.load_config()
    user = user if user is not None else cfg.get("os_user", "")
    password = password if password is not None else cfg.get("os_pass", "")
    creds = (key or cfg.get("os_key"), user, password)
    if not user or not password:
        os_session.update(token=None, base=None, user=None, creds=creds)
        return {}
    if os_session["token"] and os_session["creds"] == creds and not force:
        return os_session["user"] or {}
    os_session.update(token=None, base=None)
    r = os_request("POST", "/login", data={"username": user, "password": password}, key=key, host=OS_HOST)
    base = (r.get("base_url") or OS_HOST).replace("https://", "").strip("/")
    os_session.update(token=r.get("token"), base=base, user=r.get("user") or {}, creds=creds)
    return os_session["user"]


def check_key(key: str = None) -> None:
    """A request that fails on a bad key (some OpenSubtitles requests work without one)."""
    os_request("GET", "/discover/popular", {"type": "movie"}, key=key)


def movie_hash(path: Path):
    """OpenSubtitles' file fingerprint: finds subtitles made for this exact copy, so they're in sync."""
    try:
        size = path.stat().st_size
        if size < 131072:
            return None
        h = size
        with open(path, "rb") as f:
            for offset in (0, size - 65536):
                f.seek(offset)
                for (q,) in struct.iter_unpack("<Q", f.read(65536)):
                    h = (h + q) & 0xFFFFFFFFFFFFFFFF
        return f"{h:016x}"
    except OSError:
        return None


def sub_langs(text: str) -> list:
    langs = []
    for part in re.split(r"[,\s]+", (text or "").strip().lower()):
        if part and part not in langs:
            if not re.fullmatch(r"[a-z]{2}(-[a-z]{2})?", part):
                raise UserError(f"“{part}” isn't a language code. Use two-letter codes like en, es, fr or pt-br.")
            langs.append(part)
    return langs


def lang_names(code: str) -> set:
    """Every way a language may be written in a file name or a video's own subtitle tracks."""
    names = {code, code.split("-")[0]}
    try:
        from babelfish import Language  # comes with guessit
        lang = Language.fromietf(code)
        names |= {lang.alpha3, getattr(lang, "alpha3b", lang.alpha3), lang.name.lower()}
    except Exception:
        pass
    return {n.lower() for n in names if n}


def embedded_sub_langs(video: Path) -> set:
    """Languages of the subtitle tracks built into the video (not forced ones, which only cover foreign lines),
    as both two- and three-letter codes. Read with ffmpeg once, then remembered."""
    from . import mediainfo
    info = mediainfo.probe(video) or {}
    raw = {s["language"].lower() for s in info.get("subtitles", []) if s["language"] and not s["forced"]}
    return raw | {mediainfo.LANG2[x] for x in raw if x in mediainfo.LANG2}


def has_sub_file(video: Path, names: set) -> bool:
    prefix = video.stem + "."
    try:
        for f in video.parent.iterdir():
            if f.suffix.lower() in core.SUB_EXTS and f.name.startswith(prefix):
                tags = {t.lower() for t in f.name[len(prefix):].split(".")[:-1]}
                if tags & names:
                    return True
    except OSError:
        pass
    return False


def pick_subtitle(results: list, lang: str, part, hearing: str):
    """Best match for one language: made for this exact file first, then your SDH preference, trust and popularity."""
    options = []
    for r in results:
        a = r.get("attributes", {})
        files = a.get("files") or []
        if (a.get("language") or "").lower() != lang or not files:
            continue
        if part:
            f = next((f for f in files if f.get("cd_number") == part), None)
            if not f or not a.get("moviehash_match"):
                continue  # parts need subtitles cut for exactly that part
        elif len(files) > 1:
            continue  # split across CDs; doesn't fit a single file
        else:
            f = files[0]
        hi = bool(a.get("hearing_impaired"))
        options.append(((bool(a.get("moviehash_match")), hi == (hearing == "prefer"), bool(a.get("from_trusted")),
                         a.get("download_count") or 0), f, a, hi))
    if not options:
        return None
    options.sort(key=lambda o: o[0], reverse=True)
    _, f, a, hi = options[0]
    return {"file_id": f["file_id"], "hi": hi, "synced": bool(a.get("moviehash_match")), "release": a.get("release")}


def fetch_subtitle(file_id) -> tuple:
    r = os_request("POST", "/download", data={"file_id": file_id})
    link = r.get("link")
    if not link:
        raise SubError(r.get("message") or "OpenSubtitles didn't return a download link.")
    req = urllib.request.Request(link, headers={"User-Agent": OS_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    ext = Path(r.get("file_name") or "x.srt").suffix.lower()
    return data, (ext if ext in core.SUB_EXTS else ".srt"), r.get("remaining")


def run(job: dict, root: Path, langs: list, hearing: str, skip_embedded: bool) -> dict:
    with core.lock:  # pick up renames made outside the app first
        items = library.rescan(job, "Checking for changes")
    todo = [it for it in items if it["match"] and it["match"]["status"] in core.MATCHED]
    if not todo:
        raise UserError("Match some movies on TMDB first. Only confirmed matches get subtitles.")
    core.job_step(job, "Downloading subtitles", len(todo))
    res = {"downloaded": 0, "synced": 0, "had": 0, "none": 0, "failed": [], "remaining": None, "quota": None}
    for it in todo:
        if job["cancel"] or res["quota"]:
            break
        video = Path(root, it["path"])
        label = it["match"]["name"] + (f" part {it['part']}" if it["part"] else "")
        try:
            core.set_detail(job, f"{label}: checking")
            needed, embedded = [], None
            for lang in langs:
                names = lang_names(lang)
                if has_sub_file(video, names):
                    res["had"] += 1
                    continue
                if skip_embedded:
                    if embedded is None:
                        embedded = embedded_sub_langs(video)
                    if embedded & names:
                        res["had"] += 1
                        continue
                needed.append(lang)
            if not needed:
                continue
            core.set_detail(job, f"{label}: searching")
            found = os_request("GET", "/subtitles", params={
                "tmdb_id": it["match"]["tmdb_id"], "moviehash": movie_hash(video),
                "languages": ",".join(needed), "order_by": "download_count",
                "ai_translated": "exclude"}).get("data", [])  # machine translations are excluded by default
            time.sleep(0.3)  # stay well inside OpenSubtitles' rate limit
            for lang in needed:
                pick = pick_subtitle(found, lang, it["part"], hearing)
                if not pick:
                    res["none"] += 1
                    continue
                core.set_detail(job, f"{label}: downloading {lang}")
                content, ext, remaining = fetch_subtitle(pick["file_id"])
                # Jellyfin naming: "Movie (Year).en.srt", hearing-impaired as "Movie (Year).en.sdh.srt"
                code = lang if "-" not in lang else lang.split("-")[0] + "-" + lang.split("-")[1].upper()
                video.with_name(f"{video.stem}.{code}{'.sdh' if pick['hi'] else ''}{ext}").write_bytes(content)
                core.changed(video)
                res["downloaded"] += 1
                res["synced"] += pick["synced"]
                if remaining is not None:
                    res["remaining"] = remaining
                time.sleep(0.3)
        except QuotaError as err:
            res["quota"] = str(err)
        except SubError as err:
            res["failed"].append({"name": label, "error": str(err)})
        except (OSError, urllib.error.URLError) as err:
            res["failed"].append({"name": label, "error": getattr(err, "strerror", None) or str(err)})
        finally:
            core.tick(job)
    res["cancelled"] = job["cancel"]
    with core.lock:
        res["items"] = [dict(i) for i in library.rescan()]  # refresh the subtitle checkmarks
    return res
