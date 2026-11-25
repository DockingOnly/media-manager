"""Trailers: find the best one on TMDB (or by YouTube search), download it, keep it named for Jellyfin."""
import glob
import os
import re
import subprocess
from pathlib import Path

from . import core, library, tmdbapi
from .core import UserError, key_of, norm_title, short_error

QUALITY_WIDTH = {1080: 1920, 720: 1280, 480: 854}
NEEDS_SIGNIN = re.compile(r"sign in|confirm your age|age-restricted|inappropriate for some users", re.I)
LOGIN_BROWSERS = ("firefox", "chrome", "edge", "brave", "opera", "vivaldi", "chromium")
YT_LOGIN_COOKIES = {"SAPISID", "__Secure-3PAPISID", "LOGIN_INFO", "__Secure-1PSID", "SID"}
TAG_RE = re.compile(r"media-manager tmdb=(\d+)")
NEED_LIBS = "Trailer downloads need new libraries. Close the app and run run.bat again to install them."
tag_cache = {}


# ---------------------------------------------------------------- choosing and placing trailers

def youtube_trailers(videos: list) -> list:
    """Order TMDB's YouTube videos best first: official trailers in your language, then teasers."""
    lang = (core.load_config().get("language") or "en-US")[:2]
    vids = [v for v in videos if v.get("site") == "YouTube" and v.get("type") in ("Trailer", "Teaser") and v.get("key")]
    vids.sort(key=lambda v: (v["type"] == "Trailer", bool(v.get("official")), v.get("iso_639_1") == lang,
                             "official trailer" in (v.get("name") or "").lower(), v.get("size") or 0,
                             v.get("published_at") or ""), reverse=True)
    return [v["key"] for v in vids]


def trailer_base(video: Path, name: str, own: bool) -> Path:
    """Where Jellyfin looks: a 'trailers' folder inside the movie's own folder, or '<movie>-trailer.ext' beside it."""
    if own:
        return video.parent / "trailers" / f"{name} - Trailer"
    return video.parent / f"{video.stem}-trailer"


def flat_trailers(folder: Path) -> list:
    """'<anything>-trailer.mkv' style trailers sitting directly in a folder."""
    try:
        return [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in core.VIDEO_EXTS
                and f.stem.lower().endswith("-trailer") and ".tagging" not in f.name]
    except OSError:
        return []


def tag_trailer(path: Path, tmdb_id, key: str) -> None:
    """Write the movie's TMDB ID inside the trailer, so it's recognised even after the file is renamed."""
    tmp = path.with_name(path.stem + ".tagging" + path.suffix)
    try:
        subprocess.run([core.ffmpeg_exe(), "-y", "-v", "error", "-i", str(path), "-map", "0", "-c", "copy",
                        "-metadata", f"comment=media-manager tmdb={tmdb_id} youtube={key}", str(tmp)],
                       check=True, capture_output=True, timeout=120, creationflags=core.NO_WINDOW)
        os.replace(tmp, path)
        core.changed(path)
    except Exception:
        tmp.unlink(missing_ok=True)  # an untagged trailer still works fine


def trailer_tag(path: Path):
    """The TMDB ID written into a trailer, or None for trailers from elsewhere."""
    try:
        st = path.stat()
    except OSError:
        return None
    cache_key = (key_of(path), st.st_size, int(st.st_mtime))
    if cache_key not in tag_cache:
        m = TAG_RE.search(core.ffmpeg_info(path, timeout=30))
        tag_cache[cache_key] = int(m.group(1)) if m else None
    return tag_cache[cache_key]


# ---------------------------------------------------------------- YouTube login (age-restricted trailers)

def youtube_login_opts(cfg: dict = None) -> dict:
    """yt-dlp options that use your YouTube login, from a browser or an exported cookies.txt file."""
    cfg = cfg or core.load_config()
    source = cfg.get("yt_login") or ""
    if source in LOGIN_BROWSERS:
        return {"cookiesfrombrowser": (source,)}
    if source == "file" and cfg.get("yt_cookie_file"):
        return {"cookiefile": cfg["yt_cookie_file"]}
    return {}


def cookie_error(err: Exception) -> str:
    """Turn yt-dlp's cookie errors into something actionable."""
    chain, cause = [], err
    while cause is not None and len(chain) < 5:  # yt-dlp wraps the real reason ("failed to load cookies")
        chain.append(short_error(cause))
        cause = cause.__cause__ or cause.__context__
    text = chain[-1] if len(chain) > 1 else chain[0]
    low = " ".join(chain).lower()
    if "could not copy" in low or "database is locked" in low or "permission denied" in low:
        return "the browser is holding its cookies. Close the browser completely and try again."
    if "dpapi" in low or "decrypt" in low or "app-bound" in low:
        return ("this browser encrypts its logins so other programs can't read them. Use Firefox, or export a "
                "cookies.txt file with a browser extension and choose “cookies.txt file”.")
    if "could not find" in low and "cookies" in low:
        return "no browser profile with cookies was found. Is that browser installed and have you used it?"
    if NEEDS_SIGNIN.search(low):
        return "YouTube still asks to sign in. Make sure you're signed in to YouTube in that browser."
    return text


def check_youtube_login(cfg: dict) -> str:
    """Load the cookies and confirm a YouTube login is among them."""
    login = youtube_login_opts(cfg)
    if not login:
        return "YouTube login is turned off."
    try:
        import yt_dlp
        from yt_dlp.cookies import load_cookies
    except ImportError:
        raise UserError(NEED_LIBS)
    if "cookiefile" in login and not Path(login["cookiefile"]).is_file():
        raise UserError(f"Couldn't find the file {login['cookiefile']}.")
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            jar = load_cookies(login.get("cookiefile"), login.get("cookiesfrombrowser"), ydl)
    except Exception as err:
        raise UserError("Couldn't read the YouTube login: " + cookie_error(err))
    names = {c.name for c in jar if c.domain.lstrip(".").endswith("youtube.com")}
    if not names & YT_LOGIN_COOKIES:
        raise UserError("Found cookies, but no YouTube login among them. Sign in to youtube.com in that browser"
                        " (or export the file while signed in), then try again.")
    where = "the cookies.txt file" if "cookiefile" in login else login["cookiesfrombrowser"][0].title()
    return f"Found your YouTube login in {where}. It will only be used for trailers that need a sign-in."


# ---------------------------------------------------------------- downloading

def download_trailer(key: str, base: Path, quality: int, on_progress) -> Path:
    try:
        import yt_dlp
        import imageio_ffmpeg
    except ImportError:
        raise UserError(NEED_LIBS)
    opts = {
        # best video up to the chosen size plus best audio, merged into one .mkv
        "format": f"bv*[height<=?{quality}][width<=?{QUALITY_WIDTH[quality]}]+ba/b[height<=?{quality}]/b",
        "merge_output_format": "mkv",
        "outtmpl": str(base) + ".%(ext)s",
        "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
        "noplaylist": True, "quiet": True, "no_warnings": True, "noprogress": True,
        "retries": 3, "overwrites": True,
        "progress_hooks": [on_progress],
    }
    try:
        import deno  # lets yt-dlp unlock YouTube's HD formats
        opts["js_runtimes"] = {"deno": {"path": deno.find_deno_bin()}}
    except Exception:
        pass
    base.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={key}"
    try:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as err:
            # Only when YouTube insists on a sign-in (age-restricted videos) do we use the browser's login
            login = youtube_login_opts()
            if not login or not NEEDS_SIGNIN.search(str(err)):
                raise
            try:
                with yt_dlp.YoutubeDL({**opts, **login}) as ydl:
                    info = ydl.extract_info(url, download=True)
            except Exception as err2:
                raise Exception(f"Needs a YouTube sign-in, and using your login failed: {cookie_error(err2)}") from None
        return Path(info["requested_downloads"][0]["filepath"])
    except Exception:
        for leftover in base.parent.glob(glob.escape(base.name) + ".*"):  # half-finished pieces
            try:
                leftover.unlink()
            except OSError:
                pass
        try:
            base.parent.rmdir()  # only removes an empty "trailers" folder we just made
        except OSError:
            pass
        raise


def search_youtube_trailer(title: str, year) -> list:
    """Fallback when TMDB's trailers are missing or blocked: search YouTube for '<title> <year> official trailer'
    and keep only results that look like a trailer for this movie (title words present, 30 s to 6 min long)."""
    try:
        import yt_dlp
    except ImportError:
        return []
    query = f"{title} {year or ''} official trailer".replace("  ", " ")
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}) as ydl:
            found = ydl.extract_info(f"ytsearch8:{query}", download=False).get("entries") or []
    except Exception:
        return []
    want = norm_title(title)
    keys = []
    for e in found:
        name = norm_title(e.get("title") or "")
        if e.get("id") and "trailer" in name and want in name and 30 <= (e.get("duration") or 0) <= 360:
            keys.append(e["id"])
    return keys[:3]


# ---------------------------------------------------------------- the task

def run(job: dict, root: Path, quality: int, force: bool) -> dict:
    with core.lock:  # pick up renames made outside the app first
        items = library.rescan(job, "Checking for changes")
    folders = library.by_folder(items, root)
    groups = {}
    for it in items:
        if it["match"] and it["match"]["status"] in core.MATCHED:
            groups.setdefault(it["match"]["tmdb_id"], []).append(it)
    if not groups:
        raise UserError("Match some movies on TMDB first. Only confirmed matches get trailers.")
    core.job_step(job, "Downloading trailers", len(groups))
    res = {"downloaded": 0, "had": 0, "fixed": 0, "replaced": 0, "none": 0, "searched": 0, "failed": [], "bytes": 0}
    lang = (core.load_config().get("language") or "en-US")[:2]

    for tmdb_id, group in groups.items():
        if job["cancel"]:
            break
        name = group[0]["match"]["name"]
        videos = [Path(root, it["path"]) for it in group]
        video, parent = videos[0], videos[0].parent
        own = library.own_folder(group[0], folders, root)
        base = trailer_base(video, name, own)
        try:
            core.set_detail(job, f"{name}: checking trailer")
            # Find this movie's trailers, wherever they are and whatever they are called now
            if own:
                found = flat_trailers(parent)
                if (parent / "trailers").is_dir():
                    found += [f for f in (parent / "trailers").iterdir()
                              if f.is_file() and f.suffix.lower() in core.VIDEO_EXTS and ".tagging" not in f.name]
            else:  # a shared folder: only trailers named after this movie's files, or tagged as this movie
                stems = {v.stem.lower() for v in videos}
                found = [f for f in flat_trailers(parent)
                         if f.stem[:-len("-trailer")].lower() in stems or trailer_tag(f) == tmdb_id]
            tags = {f: trailer_tag(f) for f in found}
            # A trailer tagged as a different movie (e.g. after re-matching) gets replaced
            for f in [f for f in found if tags[f] not in (None, tmdb_id)]:
                core.discard(f)
                res["replaced"] += 1
            mine = sorted((f for f in found if tags[f] in (None, tmdb_id)), key=lambda f: tags[f] != tmdb_id)
            if force:
                for f in mine:
                    core.discard(f)
                mine = []
            if mine:
                # Keep it, but give it the right Jellyfin name and place if the movie was renamed
                keep = mine[0]
                want = base.parent / (base.name + keep.suffix)
                if key_of(keep) != key_of(want):
                    old_dir = keep.parent
                    core.move(keep, want)
                    if old_dir.name.lower() == "trailers":
                        core.remove_empty_dirs([str(old_dir)], root)
                    res["fixed"] += 1
                else:
                    res["had"] += 1
                continue
            # Nothing usable: download the best trailer, then fall back to a YouTube search
            core.set_detail(job, f"{name}: finding trailer")
            keys = youtube_trailers(tmdbapi.tmdb(f"/movie/{tmdb_id}/videos",
                                                 include_video_language=f"{lang},en,null").get("results", []))[:5]
            searched, last_err, got = False, None, False
            while True:
                if not keys and not searched:
                    searched = True
                    core.set_detail(job, f"{name}: searching YouTube")
                    keys = search_youtube_trailer(group[0]["match"]["title"], group[0]["match"]["year"])
                if not keys:
                    break
                key = keys.pop(0)

                def hook(d, name=name):
                    if d.get("status") == "downloading":
                        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                        pct = f" {d['downloaded_bytes'] * 100 // total}%" if total else ""
                        core.set_detail(job, f"{name}: downloading{pct}")
                try:
                    path = download_trailer(key, base, quality, hook)
                    core.set_detail(job, f"{name}: saving")
                    tag_trailer(path, tmdb_id, key)
                    res["downloaded"] += 1
                    res["searched"] += searched
                    res["bytes"] += path.stat().st_size
                    got = True
                    break
                except UserError:
                    raise
                except Exception as err:
                    last_err = err
            if not got:
                if last_err:
                    res["failed"].append({"name": name, "error": short_error(last_err)})
                else:
                    res["none"] += 1
        except core.TmdbError as err:
            res["failed"].append({"name": name, "error": str(err)})
        except OSError as err:
            res["failed"].append({"name": name, "error": err.strerror or str(err)})
        finally:
            core.tick(job)
    res["cancelled"] = job["cancel"]
    with core.lock:
        res["items"] = [dict(i) for i in library.rescan()]  # refresh the trailer checkmarks
    return res
