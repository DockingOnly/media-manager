"""Playback check: which files Jellyfin will have to convert (transcode) to play, and why, for the kind of device
you watch on. Converting video is heavy work for the server and can stutter; converting audio or repackaging the
file is light. Files ffmpeg can't read at all are listed first: they may be damaged.

The rules are the usual Jellyfin behaviour for each kind of player, not a guarantee for one particular device.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import core, library, mediainfo, tvlib

PROFILES = {
    "browser": "Web browser (Chrome, Edge)",
    "tv": "Smart TV or streaming stick (Android TV, Fire TV, Roku, LG, Samsung)",
    "app": "Jellyfin Media Player, Kodi or mpv on a computer",
}
HEAVY, LIGHT, SUBS, MAYBE = "heavy", "light", "subs", "maybe"
OLD_VIDEO = {"mpeg4": "MPEG-4 Part 2 (DivX/Xvid)", "msmpeg4v3": "DivX 3", "mpeg2video": "MPEG-2", "mpeg1video": "MPEG-1",
             "vc1": "VC-1", "wmv3": "Windows Media Video", "wmv2": "Windows Media Video", "rv40": "RealVideo",
             "h263": "H.263", "theora": "Theora", "mjpeg": "Motion JPEG"}
IMAGE_SUBS = {"hdmv_pgs_subtitle": "PGS (Blu-ray)", "dvd_subtitle": "VobSub (DVD)", "dvb_subtitle": "DVB"}
AUDIO_NAMES = {"dca": "DTS", "dts": "DTS", "truehd": "Dolby TrueHD", "mlp": "MLP", "ac3": "Dolby Digital (AC3)",
               "eac3": "Dolby Digital Plus (EAC3)"}
REMOTE_KBPS = 40000  # above this, watching away from home usually needs converting


def reasons(info, ext: str, profile: str) -> list:
    """[(severity, text)] for one file on one kind of player. An empty list: it plays directly."""
    if not info or not info.get("video"):
        return [("broken", "ffmpeg can't read a video track: the file may be damaged or incomplete")]
    v, out = info["video"], []
    codec, depth = v["codec"], v.get("bit_depth", 8)
    if codec in OLD_VIDEO and not (profile == "app" or (profile == "tv" and codec == "mpeg2video")):
        out.append((HEAVY, f"old video format ({OLD_VIDEO[codec]})"))
    elif codec == "h264" and depth > 8 and profile != "app":
        out.append((HEAVY, "10-bit H.264, which almost no hardware plays"))
    elif codec == "hevc" and profile == "browser":
        out.append((MAYBE, "H.265: plays in Chrome or Edge only where the computer's graphics can decode it; Firefox can't"))
    elif codec == "av1" and profile == "tv":
        out.append((MAYBE, "AV1: only newer TVs and sticks (about 2021 onwards) play it"))
    elif codec not in set(OLD_VIDEO) | {"h264", "hevc", "av1", "vp9", "vp8"} and profile != "app":
        out.append((HEAVY, f"uncommon video format ({codec})"))
    if v.get("dv_profile") == 5 and profile != "app":
        out.append((HEAVY, "Dolby Vision profile 5 has no HDR10 fallback: on anything without Dolby Vision it needs converting (or shows wrong colours)"))
    elif v.get("hdr") and profile == "browser":
        out.append((MAYBE, "HDR: converted to regular colours unless the browser and screen both do HDR"))
    audio = info.get("audio") or []
    if profile != "app" and audio:
        first = dict(audio[0])
        first["codec"] = "dts" if first["codec"] == "dca" else first["codec"]  # (newer ffmpeg calls DTS "dca")
        name = first.get("profile") if first.get("profile", "").startswith("DTS") else AUDIO_NAMES.get(first["codec"], first["codec"])
        if first["codec"] in ("truehd", "mlp") or (first["codec"] == "dts" and profile == "browser"):
            out.append((LIGHT, f"{name} audio is converted (or passed to a receiver that supports it)"))
        elif first["codec"] == "dts":
            out.append((MAYBE, f"{name} audio: many TVs can't play it, so it's converted unless a receiver handles it"))
        elif first["codec"] in ("ac3", "eac3") and profile == "browser":
            out.append((MAYBE, f"{name} audio: Edge plays it; Chrome and Firefox get it converted (light work)"))
        elif first["codec"] in ("pcm_s16le", "pcm_s24le", "pcm_bluray", "wmav2", "wmapro", "cook", "ra_288") and profile == "browser":
            out.append((LIGHT, f"{first['codec']} audio is converted"))
    fmt = info.get("format") or ""
    if ext in (".avi", ".wmv", ".asf", ".mpg", ".mpeg", ".ts", ".m2ts", ".vob", ".flv", ".rm", ".rmvb", ".divx") and profile != "app":
        out.append((LIGHT, f"the {ext.lstrip('.').upper()} container is repackaged on the fly"))
    elif "avi" in fmt and profile != "app":
        out.append((LIGHT, "the AVI container is repackaged on the fly"))
    image = sorted({IMAGE_SUBS[s["codec"]] for s in info.get("subtitles") or [] if s.get("codec") in IMAGE_SUBS})
    if image and profile == "browser":
        out.append((SUBS, f"picture subtitles ({', '.join(image)}) are burned into the picture when turned on"))
    if (info.get("bitrate") or 0) > REMOTE_KBPS:
        out.append(("remote", f"{info['bitrate'] / 1000:.0f} Mb/s: fine at home, but usually converted when watching away from home"))
    return out


def _items(kind: str):
    if kind == "tv":
        root = core.require_tv_scan()
        items = tvlib.with_matches(list(core.tv_state["items"]), root)
        name = lambda it: f"{(it.get('match') or {}).get('name') or it.get('series') or '?'} " + (
            tvlib.episode_code(it["season"], it["episodes"]) if it.get("episodes") else "(extra)")
    else:
        root = core.require_scan()
        items = library.with_matches(list(core.state["items"]))
        name = lambda it: (it.get("match") or {}).get("name") or (f"{it['title']} ({it['year']})" if it.get("year") else it["title"])
    return root, [(it, Path(root, it["path"]), name(it)) for it in items]


def check(job: dict, kind: str) -> dict:
    root, items = _items(kind)
    core.job_step(job, "Reading the files", len(items))

    def one(entry):
        it, path, name = entry
        if job.get("cancel"):
            return None
        info = mediainfo.probe(path)
        core.tick(job)
        core.set_detail(job, name)
        v = (info or {}).get("video") or {}
        summary = mediainfo.summary(info) if info else {}
        return {"id": it["id"], "name": name, "path": core.rel(path, root), "size": it["size"],
                "video": summary.get("video", ""), "audio": summary.get("audio", ""),
                "bitrate": (info or {}).get("bitrate"), "hdr": v.get("hdr") or "",
                "by_profile": {p: reasons(info, path.suffix.lower(), p) for p in PROFILES}}

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = [r for r in pool.map(one, items) if r]
    finally:
        mediainfo.save()  # next time, only new or changed files are read
    if job.get("cancel"):
        raise core.UserError("Stopped. What was read so far is kept, so the next check is quicker.")
    rows.sort(key=lambda r: r["name"].lower())
    return {"kind": kind, "profiles": PROFILES, "files": rows}
