"""TV subtitles (per episode, from OpenSubtitles.com) and trailers (per show, into its 'trailers' folder)."""
import time
import urllib.error
from pathlib import Path

from . import core, subtitles, trailers, tvlib, tvmeta
from .core import QuotaError, SubError, UserError


def subtitles_run(job: dict, root: Path, langs: list, hearing: str, skip_embedded: bool) -> dict:
    with core.lock:
        items = tvlib.annotate(tvlib.rescan(job, "Checking for changes"), root)
    todo = [it for it in items if it["kind"] == "episode" and tvlib.confirmed(it["match"])]
    if not todo:
        raise UserError("Match some shows on TMDB first. Only confirmed matches get subtitles.")
    core.job_step(job, "Downloading subtitles", len(todo))
    res = {"downloaded": 0, "synced": 0, "had": 0, "none": 0, "numbering": 0, "failed": [], "remaining": None, "quota": None}
    for it in todo:
        if job["cancel"] or res["quota"]:
            break
        video = Path(root, it["path"])
        label = f"{it['match']['title']} {tvlib.episode_code(it['season'], it['episodes'])}"
        try:
            needed, embedded = [], None
            for lang in langs:
                names = subtitles.lang_names(lang)
                if subtitles.has_sub_file(video, names):
                    res["had"] += 1
                    continue
                if skip_embedded:
                    embedded = subtitles.embedded_sub_langs(video) if embedded is None else embedded
                    if embedded & names:
                        res["had"] += 1
                        continue
                needed.append(lang)
            if not needed:
                continue
            core.set_detail(job, f"{label}: searching")
            params = {"parent_tmdb_id": it["match"]["tmdb_id"], "moviehash": subtitles.movie_hash(video),
                      "languages": ",".join(needed), "order_by": "download_count", "ai_translated": "exclude"}
            nums = tvmeta.tmdb_numbers(tvmeta.cached_for(it["match"]) or {}, it["season"], it["episodes"][0]) \
                if it["match"].get("order") else (it["season"], it["episodes"][0])
            if nums:  # OpenSubtitles goes by TMDB's numbering
                params.update(season_number=nums[0], episode_number=nums[1])
            found = subtitles.os_request("GET", "/subtitles", params=params).get("data", [])
            time.sleep(0.3)
            for lang in needed:
                pick = subtitles.pick_subtitle(found, lang, None, hearing)
                if pick and it.get("mismatch") and not pick["synced"]:
                    # TMDB numbers this episode differently, so a subtitle found by its number may be for another
                    # episode; only one made for this exact file (found by its fingerprint) is safe
                    res["numbering"] += 1
                    continue
                if not pick:
                    res["none"] += 1
                    continue
                core.set_detail(job, f"{label}: downloading {lang}")
                content, ext, remaining = subtitles.fetch_subtitle(pick["file_id"])
                code = lang if "-" not in lang else lang.split("-")[0] + "-" + lang.split("-")[1].upper()
                video.with_name(f"{video.stem}.{code}{'.sdh' if pick['hi'] else ''}{ext}").write_bytes(content)
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
        tvlib.rescan()
    return {**tvlib.current(), **res}


def show_trailers(show_dir: Path) -> list:
    found = trailers.flat_trailers(show_dir)
    folder = show_dir / "trailers"
    if folder.is_dir():
        found += [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in core.VIDEO_EXTS and ".tagging" not in f.name]
    return found


def trailers_run(job: dict, root: Path, quality: int, force: bool) -> dict:
    with core.lock:
        items = tvlib.rescan(job, "Checking for changes")
    shows = {k: v for k, v in tvlib.group(items).items() if tvlib.confirmed(v[0]["match"]) and v[0].get("show_dir")}
    if not shows:
        raise UserError("Match some shows on TMDB first. Only confirmed matches get trailers.")
    core.job_step(job, "Downloading trailers", len(shows))
    res = {"downloaded": 0, "had": 0, "fixed": 0, "replaced": 0, "none": 0, "searched": 0, "failed": [], "bytes": 0}
    for key, eps in shows.items():
        if job["cancel"]:
            break
        m = eps[0]["match"]
        show_dir = root / eps[0]["show_dir"]
        base = show_dir / "trailers" / f"{m['name']} - Trailer"
        try:
            found = show_trailers(show_dir)
            if found and not force:
                res["had"] += 1
                continue
            for f in found:
                core.discard(f)
            core.set_detail(job, f"{m['name']}: finding trailer")
            data = tvmeta.show_data(m["tmdb_id"])
            keys = trailers.youtube_trailers(data["show"].get("videos", []))[:5]
            searched, last_err, got = False, None, False
            while True:
                if not keys and not searched:
                    searched = True
                    keys = trailers.search_youtube_trailer(m["title"], m.get("year"))
                if not keys:
                    break
                k = keys.pop(0)

                def hook(d, name=m["name"]):
                    if d.get("status") == "downloading":
                        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                        pct = f" {d['downloaded_bytes'] * 100 // total}%" if total else ""
                        core.set_detail(job, f"{name}: downloading{pct}")
                try:
                    path = trailers.download_trailer(k, base, quality, hook)
                    trailers.tag_trailer(path, m["tmdb_id"], k)
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
                    res["failed"].append({"name": m["name"], "error": core.short_error(last_err)})
                else:
                    res["none"] += 1
        except core.TmdbError as err:
            res["failed"].append({"name": m["name"], "error": str(err)})
        except OSError as err:
            res["failed"].append({"name": m["name"], "error": err.strerror or str(err)})
        finally:
            core.tick(job)
    res["cancelled"] = job["cancel"]
    with core.lock:
        tvlib.rescan()
    return {**tvlib.current(), **res}
