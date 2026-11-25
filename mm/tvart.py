"""TV artwork: show artwork in the show folder, season posters beside it, episode thumbnails beside each episode.

Names match Jellyfin and what tinyMediaManager writes: poster.jpg, fanart.jpg, clearlogo.png, landscape.jpg,
banner.jpg, clearart.png; season01-poster.jpg, season-specials-poster.jpg; 'Show - S01E02 - Title-thumb.jpg'.
"""
import os
import urllib.error
from pathlib import Path

from . import artwork, core, tvlib, tvmeta
from .core import UserError
from .tmdbapi import image_url, tmdb

SHOW_KINDS = {k: artwork.KINDS[k] for k in ("poster", "fanart", "logo", "landscape", "banner", "clearart")}
SHOW_KINDS["fanart"] = ("Backdrop", "fanart", ".jpg", ("fanart.jpg", "backdrop.jpg", "backdrop1.jpg", "background.jpg"))
FANART_TV = {"poster": ("tvposter",), "fanart": ("showbackground",), "logo": ("hdtvlogo", "clearlogo"),
             "landscape": ("tvthumb",), "banner": ("tvbanner",), "clearart": ("hdclearart", "clearart"),
             "season": ("seasonposter",)}
ALL_KINDS = list(SHOW_KINDS) + ["season", "thumb"]  # 'season' = season posters, 'thumb' = episode thumbnails
LABELS = {**{k: v[0] for k, v in SHOW_KINDS.items()}, "season": "Season posters", "thumb": "Episode thumbnails"}


# ---------------------------------------------------------------- where artwork lives

def show_existing(show_dir: Path, kind: str):
    for n in SHOW_KINDS[kind][3]:
        if (show_dir / n).is_file():
            return show_dir / n
    return None


def show_target(show_dir: Path, kind: str, ext: str = None) -> Path:
    _, name, default_ext, _ = SHOW_KINDS[kind]
    return show_dir / f"{name}{ext or default_ext}"


def season_name(season: int) -> str:
    return "season-specials-poster" if season == 0 else f"season{season:02d}-poster"


def season_existing(show_dir: Path, season: int):
    for p in (show_dir / f"{season_name(season)}.jpg", show_dir / f"{season_name(season)}.png",
              show_dir / tvlib.season_folder(season) / "poster.jpg", show_dir / tvlib.season_folder(season) / "folder.jpg"):
        if p.is_file():
            return p
    return None


def thumb_path(video: Path) -> Path:
    return video.with_name(video.stem + "-thumb.jpg")


# ---------------------------------------------------------------- sources

def fanart_tv(tvdb_id) -> dict:
    if not tvdb_id or not artwork.fanart_key():
        return {}
    key = ("fanart-tv", tvdb_id)
    if key not in artwork._cache:
        artwork._cache[key] = artwork.fanart_request(tvdb_id, section="tv")
    return artwork._cache[key]


def tmdb_images(tmdb_id, season: int = None) -> dict:
    key = ("tmdb-tv", tmdb_id, season)
    if key not in artwork._cache:
        lang = (core.load_config().get("language") or "en-US")[:2]
        path = f"/tv/{tmdb_id}/images" if season is None else f"/tv/{tmdb_id}/season/{season}/images"
        artwork._cache[key] = tmdb(path, language=None, include_image_language=f"{lang},en,null")
    return artwork._cache[key]


def options(data: dict, kind: str, season: int = None) -> list:
    """Every image for a show artwork kind (or a season's poster), best first."""
    show = data["show"]
    lang = (core.load_config().get("language") or "en-US")[:2]
    out = []

    def lang_rank(code, textless_first=False):
        code = (code or "").lower()
        if textless_first:
            return {"": 3, "00": 3, "xx": 3, lang: 2, "en": 1}.get(code, 0)
        return {lang: 3, "en": 2, "": 1, "00": 1, "xx": 1}.get(code, 0)

    for i, ftype in enumerate(FANART_TV[kind]):
        for img in fanart_tv(show.get("tvdb_id")).get(ftype, []):
            if kind == "season" and str(img.get("season")) not in (str(season), "all"):
                continue
            url = img.get("url", "")
            if url:
                out.append({"url": url, "preview": url.replace("/fanart/", "/preview/"), "source": "fanart.tv",
                            "lang": img.get("lang", ""), "size": "HD" if ftype.startswith("hd") else "",
                            "score": (lang_rank(img.get("lang"), kind == "fanart"), 1 - i, int(img.get("likes") or 0), 0)})
    if kind in ("poster", "fanart", "landscape", "logo", "season"):
        images = tmdb_images(show["id"], season if kind == "season" else None)
        pool = {"poster": "posters", "season": "posters", "fanart": "backdrops", "landscape": "backdrops", "logo": "logos"}[kind]
        small, large = artwork.TMDB_SIZES.get(kind, ("w342", "w780"))
        for img in images.get(pool, []):
            code = img.get("iso_639_1")
            if kind == "landscape" and not code:
                continue
            path = img["file_path"]
            if kind == "logo" and path.endswith(".svg"):
                path = path[:-4] + ".png"
            out.append({"url": image_url(path, large), "preview": image_url(path, small), "source": "TMDB",
                        "lang": code or "", "size": f"{img.get('width')}×{img.get('height')}",
                        "score": (lang_rank(code, kind == "fanart"), 0, int((img.get("vote_average") or 0) * 10), img.get("width") or 0)})
    prefer_fanart = kind not in ("poster", "fanart", "season")
    out.sort(key=lambda o: (o["score"][0], (o["source"] == "fanart.tv") == prefer_fanart) + o["score"][1:], reverse=True)
    for o in out:
        o.pop("score")
    return out


def save_to(dst: Path, url: str, old: Path = None) -> Path:
    """Download to dst; whatever it replaces goes to the Recycle Bin."""
    artwork.check_source(url)
    tmp = dst.with_name(dst.name + ".new")
    artwork.download_to(url, tmp)
    for f in {old, dst} - {None}:
        if f.exists():
            core.discard(f)
    os.replace(tmp, dst)
    core.changed(dst)
    return dst


def ext_of(url: str, default: str) -> str:
    ext = Path(url.split("?")[0]).suffix.lower()
    return ".jpg" if ext == ".jpeg" else (ext if ext in (".jpg", ".png") else default)


# ---------------------------------------------------------------- the task and single choices

def run(job: dict, root: Path, kinds: list, replace: bool) -> dict:
    with core.lock:
        items = tvlib.annotate(tvlib.rescan(job, "Checking for changes"), root)
    shows = {k: v for k, v in tvlib.group(items).items() if tvlib.confirmed(v[0]["match"]) and v[0].get("show_dir")}
    if not shows:
        raise UserError("Match some shows on TMDB first. Only confirmed matches get artwork.")
    core.job_step(job, "Downloading artwork", len(shows))
    res = {"downloaded": 0, "had": 0, "none": 0, "numbering": 0, "failed": [], "by_kind": {k: 0 for k in kinds}}
    for key, eps in shows.items():
        if job["cancel"]:
            break
        m = eps[0]["match"]
        show_dir = root / eps[0]["show_dir"]
        try:
            data = tvmeta.data_for(m)
            for kind in [k for k in kinds if k in SHOW_KINDS]:
                old = show_existing(show_dir, kind)
                if old and not replace:
                    res["had"] += 1
                    continue
                core.set_detail(job, f"{m['name']}: {LABELS[kind].lower()}")
                opts = options(data, kind)
                if not opts:
                    res["none"] += 1
                    continue
                save_to(show_target(show_dir, kind, ext_of(opts[0]["url"], SHOW_KINDS[kind][2])), opts[0]["url"], old)
                res["downloaded"] += 1
                res["by_kind"][kind] += 1
            if "season" in kinds:
                for season in sorted({e["season"] for e in eps if e["kind"] == "episode"}):
                    old = season_existing(show_dir, season)
                    if old and not replace:
                        res["had"] += 1
                        continue
                    core.set_detail(job, f"{m['name']}: {'specials' if season == 0 else f'season {season}'} poster")
                    opts = options(data, "season", season)
                    if not opts:
                        res["none"] += 1
                        continue
                    save_to(show_dir / f"{season_name(season)}{ext_of(opts[0]['url'], '.jpg')}", opts[0]["url"],
                            old if old and old.parent == show_dir else None)
                    res["downloaded"] += 1
                    res["by_kind"]["season"] += 1
            if "thumb" in kinds:
                for it in eps:
                    if it["kind"] != "episode":
                        continue
                    video = Path(root, it["path"])
                    dst = thumb_path(video)
                    if dst.exists() and not replace:
                        res["had"] += 1
                        continue
                    if it.get("mismatch"):  # TMDB's still for this number would show a different episode
                        res["numbering"] += 1
                        continue
                    ep = tvmeta.episode(data, it["season"], it["episodes"][0])
                    if not ep or not (ep.get("still_url") or ep.get("still_path")):
                        res["none"] += 1
                        continue
                    core.set_detail(job, f"{m['name']}: {tvlib.episode_code(it['season'], it['episodes'])} thumbnail")
                    save_to(dst, ep.get("still_url") or image_url(ep["still_path"], "w780"))
                    res["downloaded"] += 1
                    res["by_kind"]["thumb"] += 1
        except (UserError, core.TmdbError) as err:
            res["failed"].append({"name": m["name"], "error": str(err)})
            if "rejected the API key" in str(err):
                break
        except (OSError, urllib.error.URLError) as err:
            res["failed"].append({"name": m["name"], "error": getattr(err, "strerror", None) or str(err)})
        finally:
            for k in [k for k in artwork._cache if k[0] in ("tmdb-tv", "fanart-tv")]:  # one show at a time
                artwork._cache.pop(k, None)
            core.tick(job)
    res["cancelled"] = job["cancel"]
    with core.lock:
        tvlib.rescan()
    return {**tvlib.current(), **res}


def choose(show_dir: Path, data: dict, kind: str, url: str, season: int = None) -> None:
    """Use one picked image: show artwork or a season poster. The old file goes to the Recycle Bin."""
    if kind == "season":
        old = season_existing(show_dir, season)
        save_to(show_dir / f"{season_name(season)}{ext_of(url, '.jpg')}", url, old if old and old.parent == show_dir else None)
    elif kind in SHOW_KINDS:
        save_to(show_target(show_dir, kind, ext_of(url, SHOW_KINDS[kind][2])), url, show_existing(show_dir, kind))
    else:
        raise UserError("Unknown kind of artwork.")
