"""Artwork for Jellyfin: posters, backdrops, logos, thumbnails, banners, clear art and disc art.

TMDB (no extra key) supplies posters, backdrops and logos. fanart.tv (free key, optional) adds banners,
clear art, disc art and landscape thumbnails, and more choices for the rest.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import core, library, tmdbapi
from .core import UserError

# kind -> (label, Jellyfin file name, extension, names that already count as this kind)
KINDS = {
    "poster": ("Poster", "poster", ".jpg", ("poster.jpg", "poster.png", "folder.jpg", "cover.jpg")),
    "fanart": ("Backdrop", "fanart", ".jpg", ("fanart.jpg", "backdrop.jpg", "background.jpg", "fanart.png")),
    "logo": ("Logo", "clearlogo", ".png", ("clearlogo.png", "logo.png")),
    "landscape": ("Thumbnail", "landscape", ".jpg", ("landscape.jpg", "thumb.jpg")),
    "banner": ("Banner", "banner", ".jpg", ("banner.jpg",)),
    "clearart": ("Clear art", "clearart", ".png", ("clearart.png",)),
    "disc": ("Disc art", "disc", ".png", ("disc.png", "discart.png", "cdart.png")),
}
FANART_TYPES = {  # fanart.tv's names, best first
    "poster": ("movieposter",), "fanart": ("moviebackground",), "logo": ("hdmovielogo", "movielogo"),
    "landscape": ("moviethumb",), "banner": ("moviebanner",), "clearart": ("hdmovieclearart", "movieart"),
    "disc": ("moviedisc",),
}
TMDB_SIZES = {"poster": ("w342", "w780"), "fanart": ("w300", "w1280"), "landscape": ("w300", "w1280"),
              "logo": ("w300", "original")}
ALLOWED_HOSTS = {"image.tmdb.org", "assets.fanart.tv", "static.tvmaze.com"}
FANART_API = "https://webservice.fanart.tv/v3/"
_cache = {}  # (source, tmdb_id) -> data, for this session


# ---------------------------------------------------------------- where artwork lives

def existing(video: Path, own: bool, kind: str):
    """The file that already serves as this kind of artwork, if any."""
    names = [f"{video.stem}-{n}" for n in KINDS[kind][3]]
    if own:
        names += list(KINDS[kind][3])
    for n in names:
        p = video.parent / n
        if p.is_file():
            return p
    return None


def target(video: Path, own: bool, kind: str, ext: str = None) -> Path:
    _, name, default_ext, _ = KINDS[kind]
    ext = ext or default_ext
    return video.parent / (f"{name}{ext}" if own else f"{video.stem}-{name}{ext}")


# ---------------------------------------------------------------- sources

def fanart_key() -> str:
    return (core.load_config().get("fanart_key") or "").strip()


def fanart_request(item_id, key: str = None, section: str = "movies") -> dict:
    """fanart.tv accepts a project key or a personal key; try it as either. Movies go by TMDB id, shows by TVDB id."""
    key = key or fanart_key()
    if not key:
        return {}
    for param in ("api_key", "client_key"):
        url = f"{FANART_API}{section}/{int(item_id)}?{param}={urllib.parse.quote(key)}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": core.USER_AGENT}),
                                        timeout=20) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as err:
            if err.code == 404:  # fanart.tv has nothing for this movie
                return {}
            if err.code in (401, 403) and param == "api_key":
                continue
            if err.code in (401, 403):
                raise UserError("fanart.tv rejected the API key. Check it in Settings.") from None
            raise UserError(f"fanart.tv returned error {err.code}. Try again later.") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise UserError("Couldn't reach fanart.tv. Check your internet connection.") from None
    return {}


def check_fanart_key(key: str) -> None:
    fanart_request(603, key)  # The Matrix: raises if the key is refused


def tmdb_images(tmdb_id) -> dict:
    if ("tmdb", tmdb_id) not in _cache:
        lang = (core.load_config().get("language") or "en-US")[:2]
        _cache[("tmdb", tmdb_id)] = tmdbapi.tmdb(f"/movie/{tmdb_id}/images", language=None,
                                                 include_image_language=f"{lang},en,null")
    return _cache[("tmdb", tmdb_id)]


def fanart_images(tmdb_id) -> dict:
    if not fanart_key():
        return {}
    if ("fanart", tmdb_id) not in _cache:
        _cache[("fanart", tmdb_id)] = fanart_request(tmdb_id)
    return _cache[("fanart", tmdb_id)]


def options(tmdb_id, kind: str) -> list:
    """Every image available for one kind, best first: {url, preview, source, lang, score, size}."""
    lang = (core.load_config().get("language") or "en-US")[:2]
    out = []

    def lang_rank(code, textless_first=False):
        code = (code or "").lower()
        if textless_first:  # backdrops look best without text
            return {"": 3, "00": 3, "xx": 3, lang: 2, "en": 1}.get(code, 0)
        return {lang: 3, "en": 2, "": 1, "00": 1, "xx": 1}.get(code, 0)

    # fanart.tv
    for i, ftype in enumerate(FANART_TYPES[kind]):
        for img in fanart_images(tmdb_id).get(ftype, []):
            url = img.get("url", "")
            if not url:
                continue
            out.append({"url": url, "preview": url.replace("/fanart/", "/preview/"), "source": "fanart.tv",
                        "lang": img.get("lang", ""), "size": "HD" if ftype.startswith("hd") else "",
                        "score": (lang_rank(img.get("lang"), kind == "fanart"), 1 - i, int(img.get("likes") or 0), 0)})
    # TMDB
    if kind in ("poster", "fanart", "landscape", "logo"):
        data = tmdb_images(tmdb_id)
        pool = {"poster": "posters", "fanart": "backdrops", "landscape": "backdrops", "logo": "logos"}[kind]
        small, large = TMDB_SIZES[kind]
        for img in data.get(pool, []):
            code = img.get("iso_639_1")
            if kind == "landscape" and not code:
                continue  # a thumbnail should show the title, so only backdrops with text on them
            path = img["file_path"]
            if kind == "logo" and path.endswith(".svg"):
                path = path[:-4] + ".png"  # TMDB renders SVG logos as PNG
            out.append({"url": tmdbapi.image_url(path, large), "preview": tmdbapi.image_url(path, small),
                        "source": "TMDB", "lang": code or "", "size": f"{img.get('width')}×{img.get('height')}",
                        "score": (lang_rank(code, kind == "fanart"), 0, int((img.get("vote_average") or 0) * 10),
                                  img.get("width") or 0)})
    # fanart.tv's dedicated art first for logos, clear art etc.; TMDB's well-rated posters/backdrops first
    prefer_fanart = kind not in ("poster", "fanart")
    out.sort(key=lambda o: (o["score"][0], (o["source"] == "fanart.tv") == prefer_fanart) + o["score"][1:], reverse=True)
    for o in out:
        o.pop("score")
    return out


def check_source(url: str) -> None:
    parts = urllib.parse.urlsplit(url or "")
    if parts.scheme != "https" or (parts.hostname or "") not in ALLOWED_HOSTS:
        raise UserError("Artwork can only come from TMDB or fanart.tv.")


def download_to(url: str, dst: Path) -> None:
    check_source(url)
    req = urllib.request.Request(url, headers={"User-Agent": core.USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    if len(data) < 100:
        raise UserError("That image came back empty.")
    tmp = dst.with_name(dst.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, dst)


def save(video: Path, own: bool, kind: str, url: str, replace: bool) -> Path:
    """Download one image to its Jellyfin name. With replace, the old file goes to the Recycle Bin."""
    check_source(url)  # before anything else happens
    ext =Path(urllib.parse.urlsplit(url).path).suffix.lower()
    ext = ext if ext in (".jpg", ".jpeg", ".png") else KINDS[kind][2]
    dst = target(video, own, kind, ".jpg" if ext == ".jpeg" else ext)
    old = existing(video, own, kind)
    tmp = dst.with_name(dst.name + ".new")
    download_to(url, tmp)
    if replace and old is not None:
        core.discard(old)
    if dst.exists():  # (an old file of the same name, when replacing)
        core.discard(dst)
    os.replace(tmp, dst)
    return dst


# ---------------------------------------------------------------- the task

def run(job: dict, root: Path, kinds: list, replace: bool) -> dict:
    with core.lock:
        items = library.rescan(job, "Checking for changes")
    folders = library.by_folder(items, root)
    groups = {}
    for it in items:
        if it["match"] and it["match"]["status"] in core.MATCHED:
            groups.setdefault(it["match"]["tmdb_id"], []).append(it)
    if not groups:
        raise UserError("Match some movies on TMDB first. Only confirmed matches get artwork.")
    core.job_step(job, "Downloading artwork", len(groups))
    res = {"downloaded": 0, "had": 0, "none": 0, "failed": [], "by_kind": {k: 0 for k in kinds}}
    for tmdb_id, group in groups.items():
        if job["cancel"]:
            break
        it = group[0]
        name = it["match"]["name"]
        video = Path(root, it["path"])
        own = library.own_folder(it, folders, root)
        try:
            for kind in kinds:
                if existing(video, own, kind) and not replace:
                    res["had"] += 1
                    continue
                core.set_detail(job, f"{name}: {KINDS[kind][0].lower()}")
                opts = options(tmdb_id, kind)
                if not opts:
                    res["none"] += 1
                    continue
                save(video, own, kind, opts[0]["url"], replace)
                res["downloaded"] += 1
                res["by_kind"][kind] += 1
        except (UserError, core.TmdbError) as err:
            res["failed"].append({"name": name, "error": str(err)})
            if "rejected the API key" in str(err):
                break
        except (OSError, urllib.error.URLError) as err:
            res["failed"].append({"name": name, "error": getattr(err, "strerror", None) or str(err)})
        finally:
            for key in [k for k in _cache if k[1] == tmdb_id]:
                _cache.pop(key, None)
            core.tick(job)
    res["cancelled"] = job["cancel"]
    with core.lock:
        res["items"] = [dict(i) for i in library.rescan()]
    return res
