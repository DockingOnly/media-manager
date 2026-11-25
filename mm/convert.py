"""Converting videos in older formats (H.264, MPEG-4, MPEG-2, VC-1...) to H.265 or AV1, but only where that pays off.

Most of the space a library could win back is in files with plenty of bitrate for their picture size. A 1080p
H.264 file at 10 Mb/s becomes about 5 Mb/s in H.265 and looks the same; a 1080p file already squeezed to 2 Mb/s
(common for small web releases) has nothing left to give, and converting it again only loses detail. So each file
gets an estimate from its own bitrate, and files where converting saves less than a quarter are left alone.

For each file:
  1. ffmpeg writes the new copy next to the original as "<name>.mm-converting.part" (Jellyfin and this app ignore
     .part files), keeping every audio and subtitle track, chapters and fonts as they are. Only the picture is
     re-encoded, on the graphics card when it can (NVIDIA, Intel or AMD), otherwise on the processor.
  2. The copy is checked: same length, same audio and subtitle tracks, the new format, smaller by enough. With
     the quality check on, three 8-second samples are compared with the original using VMAF (Netflix's measure of
     how alike two videos look); a copy that scores below the bar is thrown away and the original kept.
  3. The original goes to the Recycle Bin (checked first, as everywhere in this app), and the copy takes its
     place under the same name (MKV for old containers such as AVI), with the original's "date created", so
     Jellyfin doesn't list it as newly added. The .nfo's technical details are updated, and Jellyfin is told.

Conversions run one file at a time in the background, separately from the app's other tasks, and survive the
page being closed. The PC is kept awake while they run. History lists each file, and Restore there brings back
the original (the converted copy goes to the Recycle Bin instead).
"""
import ctypes
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import core
from .core import UserError, key_of

OLD = {"h264", "mpeg4", "mpeg2video", "mpeg1video", "vc1", "wmv3", "wmv2", "wmv1", "msmpeg4v3", "msmpeg4v2", "h263",
       "flv1", "vp8", "theora", "rv40", "rv30"}
CODECS = {"hevc": "H.265", "av1": "AV1"}
QUALITY = ("high", "balanced", "small")
KEEP_EXT = {".mkv": "matroska", ".mp4": "mp4", ".m4v": "mp4", ".mov": "mov"}  # anything else becomes .mkv
FLOOR = {2160: 9000, 1080: 2200, 720: 1300, 480: 700}      # kb/s an H.265 copy is never asked to go below
TYPICAL = {2160: 16000, 1080: 5000, 720: 2800, 480: 1400}  # kb/s a good H.265 copy usually needs
FACTOR = {"high": 0.8, "balanced": 0.65, "small": 0.5}     # of the original's bitrate, before those limits
AV1_SHARE = 0.8                                            # AV1 needs about a fifth less than H.265
VMAF_BAR = {"high": 94, "balanced": 92, "small": 89}       # the samples must score this or better (out of 100)
MIN_SAVING, MIN_SAVED = 0.25, 150e6                       # worth it: a quarter smaller, and at least 150 MB
AUDIO_KBPS = {"aac": 80, "ac3": 96, "eac3": 107, "dts": 252, "truehd": 600, "flac": 350, "mp3": 96, "opus": 64,
              "vorbis": 80, "mp2": 96, "wmav2": 64, "pcm_s16le": 768, "pcm_s24le": 1152}  # per channel, when not stated
SAMPLES, SAMPLE_SECONDS = (0.2, 0.5, 0.8), 8
TMP_SUFFIX = ".mm-converting.part"
STATS_TAGS = ("BPS", "DURATION", "NUMBER_OF_FRAMES", "NUMBER_OF_BYTES", "_STATISTICS_WRITING_APP", "_STATISTICS_WRITING_DATE_UTC",
              "_STATISTICS_TAGS")
BELOW_NORMAL = 0x00004000

_lock = threading.Lock()      # the saved queue
_enc_lock = threading.Lock()  # what the computer can encode with
_encoders = None
state = {"running": False, "stop": None, "current": None, "proc": None, "started": None}


# ---------------------------------------------------------------- what the computer can encode with

def _try(ff: str, enc: str) -> bool:
    try:
        return subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=black:s=320x240:d=0.2",
                               "-frames:v", "3", "-pix_fmt", "yuv420p", "-c:v", enc, "-f", "null", "-"], capture_output=True,
                              timeout=30, creationflags=core.NO_WINDOW).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def encoders(refresh: bool = False) -> dict:
    """{'hevc': [encoders, best first], 'av1': [...]}: the graphics card's when it has one, the processor's last."""
    global _encoders
    with _enc_lock:
        if _encoders is not None and not refresh:
            return _encoders
    try:
        ff = core.ffmpeg_exe()
    except Exception:
        return {"hevc": [], "av1": []}
    found = {"hevc": [e for e in ("hevc_nvenc", "hevc_qsv", "hevc_amf", "libx265") if _try(ff, e)],
             "av1": [e for e in ("av1_nvenc", "av1_qsv", "av1_amf") if _try(ff, e)]}
    with _enc_lock:
        _encoders = found
    return found


VENDOR = {"nvenc": "your NVIDIA graphics card", "qsv": "your Intel graphics", "amf": "your AMD graphics card", "libx265": "the processor"}


def encoder_label(enc: str) -> str:
    return next((v for k, v in VENDOR.items() if k in enc), enc)


def pick_encoder(codec: str, prefer: str = "auto") -> str:
    found = encoders().get(codec) or []
    if prefer == "cpu":
        found = [e for e in found if e.startswith("lib")]
    elif prefer == "gpu":
        found = [e for e in found if not e.startswith("lib")]
    if not found:
        if codec == "av1":
            raise UserError("AV1 needs a recent graphics card (NVIDIA RTX 40, Intel Arc or AMD RX 7000 and newer). Choose H.265 instead.")
        raise UserError("No H.265 encoder could be used on this computer." if prefer != "gpu" else
                        "The graphics card can't encode H.265 here. Choose Automatic or Processor.")
    return found[0]


# ---------------------------------------------------------------- reading a file and estimating

def res_class(width, height) -> int:
    w, h = width or 0, height or 0
    if w >= 3200 or h >= 1800:
        return 2160
    if w >= 1800 or h >= 1000:
        return 1080
    if w >= 1200 or h >= 700:
        return 720
    return 480


def facts(text: str) -> dict:
    """What converting needs to know from ffmpeg's description of a file."""
    from . import mediainfo
    info = mediainfo.parse(text or "")
    if not info or not info.get("video"):
        return {}
    v = info["video"]
    vline = next((ln for ln in text.splitlines() if re.search(r"Stream #\S+.*: Video: ", ln) and "attached pic" not in ln
                  and not re.search(r"Video: (mjpeg|png|bmp)", ln)), "")
    audio_kbps = 0
    for ln in text.splitlines():
        m = re.search(r": Audio: (\w+)", ln)
        if m:
            k = re.search(r"(\d+) kb/s", ln)
            if k:
                audio_kbps += int(k.group(1))
            else:
                a = next((x for x in info["audio"] if x["codec"] == m.group(1)), {"channels": 2})
                audio_kbps += AUDIO_KBPS.get(m.group(1), 96) * (a.get("channels") or 2)
    vk = re.search(r"(\d+) kb/s", vline)
    total = info.get("bitrate")
    video_kbps = int(vk.group(1)) if vk else (max(total - audio_kbps, 0) if total else None)
    count = lambda kind: sum(1 for ln in text.splitlines() if re.search(rf"Stream #\S+.*: {kind}: ", ln))
    return {"codec": v["codec"], "width": v["width"], "height": v["height"], "hdr": v.get("hdr") or "",
            "interlaced": bool(re.search(r"(top|bottom)( coded)? first|interlaced", vline)), "duration": info.get("duration"),
            "video_kbps": video_kbps, "audio_kbps": audio_kbps, "audio": count("Audio"), "subs": count("Subtitle"),
            "fonts": count("Attachment"), "language": next((m.group(1) for m in [re.search(r"Stream #\d+:\d+(?:\[\w+\])?\((\w+)\)", vline)] if m), "")}


def target_kbps(f: dict, quality: str, codec: str) -> int:
    rc = res_class(f["width"], f["height"])
    share = AV1_SHARE if codec == "av1" else 1.0
    want = f["video_kbps"] * FACTOR[quality]
    return int(max(FLOOR[rc] * share, min(want, TYPICAL[rc] * share)))


def estimate(path: Path, size: int, quality: str = "balanced", codec: str = "hevc", text: str = None) -> dict:
    """Whether converting this file pays off, and roughly how big the copy would be."""
    from . import mediainfo
    if text is None:
        text = mediainfo.text_of(path, probe_missing=False)
    f = facts(text or "")
    out = {"codec": (f.get("codec") or "").lower(), "worth": False, "after": None, "saved": 0, "reason": ""}
    if not f:
        out["reason"] = "Not read yet" if text is None else "Couldn't be read"
        return out
    out.update(width=f["width"], height=f["height"], video_kbps=f["video_kbps"], duration=f["duration"])
    if f["codec"] not in OLD:
        out["reason"] = "Already " + {"hevc": "H.265", "av1": "AV1", "vp9": "VP9"}.get(f["codec"], f["codec"].upper())
        return out
    if f["hdr"]:
        out["reason"] = "HDR: left as it is"
        return out
    if not f["duration"] or not f["video_kbps"]:
        out["reason"] = "Its length or bitrate can't be read"
        return out
    kbps = target_kbps(f, quality, codec)
    after = int((kbps + f["audio_kbps"]) * 1000 / 8 * f["duration"] * 1.01)
    saved = size - after
    out.update(after=after, saved=max(saved, 0), target_kbps=kbps)
    if saved < size * MIN_SAVING or saved < MIN_SAVED:
        out["reason"] = "Already small for its picture size" if saved < size * MIN_SAVING else "Too small to be worth it"
        return out
    out["worth"] = True
    return out


# ---------------------------------------------------------------- the saved queue and what was tried

def _load() -> dict:
    return core.load_json(core.CONVERT, {"queue": [], "tried": {}, "results": []})


def _save(d: dict) -> None:
    d["results"] = d.get("results", [])[-300:]
    core.save_json(core.CONVERT, d)


def tried(path: Path):
    """Why a file was left as it is last time (a copy that wasn't smaller, or didn't look as good), if it hasn't changed."""
    t = _load()["tried"].get(key_of(path))
    if not t:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    return t["reason"] if t.get("size") == st.st_size and t.get("mtime") == int(st.st_mtime) else None


# ---------------------------------------------------------------- the files of a library

def library_files(kind: str) -> list:
    """[(path, display name, size)] for the current movie or TV library."""
    out = []
    if kind == "tv":
        from . import tvlib
        root = core.tv_state["root"]
        if root is None:
            raise UserError("Scan your TV shows folder first.")
        with core.lock:
            items = tvlib.with_matches(list(core.tv_state["items"]), root)
        for it in items:
            if it.get("kind") != "episode":
                continue
            show = (it.get("match") or {}).get("name") or it.get("series") or ""
            code = tvlib.episode_code(it["season"], it["episodes"]) if it.get("episodes") else ""
            out.append((Path(root, it["path"]), f"{show} {code}".strip() or Path(it["path"]).stem, it["size"]))
    else:
        from . import library
        root = core.require_scan()
        with core.lock:
            items = library.with_matches(list(core.state["items"]))
        for it in items:
            m = it.get("match") or {}
            name = m.get("name") if m.get("status") in core.MATCHED else None
            out.append((Path(root, it["path"]), name or f"{it['title']}{f' ({it['year']})' if it.get('year') else ''}", it["size"]))
    return out


def candidates(kind: str, quality: str = "balanced", codec: str = "hevc") -> dict:
    files = []
    queued = {key_of(q["path"]) for q in _load()["queue"]}
    for path, name, size in library_files(kind):
        e = estimate(path, size, quality, codec)
        why = tried(path)
        if why and e["worth"]:
            e.update(worth=False, reason=why)
        files.append({"path": str(path), "name": name, "size": size, "queued": key_of(path) in queued, **e})
    files.sort(key=lambda f: (-f["worth"], -(f["saved"] or 0), -f["size"]))
    worth = [f for f in files if f["worth"]]
    old = [f for f in files if f["codec"] in OLD]
    return {"files": files, "worth": len(worth), "size": sum(f["size"] for f in worth),
            "after": sum(f["after"] for f in worth), "saved": sum(f["saved"] for f in worth),
            "old": len(old), "old_size": sum(f["size"] for f in old)}


# ---------------------------------------------------------------- converting one file

def encoder_args(enc: str, quality: str, kbps: int) -> list:
    q = QUALITY.index(quality)
    cap = ["-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k"]
    if enc == "hevc_nvenc":
        return ["-c:v", enc, "-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", str((21, 24, 27)[q]), "-b:v", "0", *cap,
                "-spatial-aq", "1", "-temporal-aq", "1", "-rc-lookahead", "32", "-profile:v", "main10", "-pix_fmt", "p010le"]
    if enc == "av1_nvenc":
        return ["-c:v", enc, "-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", str((27, 31, 35)[q]), "-b:v", "0", *cap,
                "-spatial-aq", "1", "-temporal-aq", "1", "-rc-lookahead", "32", "-pix_fmt", "p010le"]
    if enc in ("hevc_qsv", "av1_qsv"):
        return ["-c:v", enc, "-preset", "slower", "-global_quality", str((22, 25, 28)[q] + (5 if "av1" in enc else 0)), *cap,
                "-pix_fmt", "p010le"]
    if enc in ("hevc_amf", "av1_amf"):
        return ["-c:v", enc, "-quality", "quality", "-rc", "qvbr", "-qvbr_quality_level", str((26, 23, 20)[q]), *cap]
    return ["-c:v", "libx265", "-preset", "medium", "-crf", str((20, 22, 24)[q]), "-pix_fmt", "yuv420p10le",
            "-profile:v", "main10", "-x265-params", f"vbv-maxrate={kbps}:vbv-bufsize={kbps * 2}:log-level=error"]


def plan(src: Path, f: dict, enc: str, quality: str, kbps: int) -> dict:
    ext = src.suffix.lower()
    fmt = KEEP_EXT.get(ext, "matroska")
    final = src if ext in KEEP_EXT else src.with_suffix(".mkv")
    tmp = src.with_name(src.stem + TMP_SUFFIX)
    cmd = [core.ffmpeg_exe(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
           "-i", str(src), "-map", "0:V:0", "-map", "0:a?", "-map", "0:s?"]
    if fmt == "matroska":
        cmd += ["-map", "0:t?"]
    cmd += ["-map_metadata", "0", "-map_chapters", "0", "-c", "copy"]
    for tag in STATS_TAGS:  # mkvmerge's statistics describe the old picture (Jellyfin reads BPS as the bitrate)
        cmd += ["-metadata:s:v:0", f"{tag}=", "-metadata:s:v:0", f"{tag}-eng="]
    if f.get("interlaced"):
        cmd += ["-vf", "bwdif=mode=send_frame"]
    cmd += encoder_args(enc, quality, kbps)
    if fmt in ("mp4", "mov"):
        cmd += ["-movflags", "+faststart"] + (["-tag:v", "hvc1"] if enc.startswith(("hevc", "libx265")) else [])
    cmd += ["-max_muxing_queue_size", "9999", "-f", fmt, str(tmp)]
    return {"cmd": cmd, "tmp": tmp, "final": final}


def _run(cmd: list, duration: float, progress) -> None:
    """Run ffmpeg, reporting (fraction done, speed). Raises UserError when it fails or is stopped."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            creationflags=core.NO_WINDOW | (BELOW_NORMAL if os.name == "nt" else 0))
    state["proc"] = proc
    errors = []
    t = threading.Thread(target=lambda: errors.extend(proc.stderr.read().decode("utf-8", "replace").splitlines()[-12:]), daemon=True)
    t.start()
    speed = None
    try:
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("speed="):
                speed = num(line[6:].rstrip("x"))
            elif line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                us = num(line.split("=", 1)[1])
                if us is not None and duration:
                    progress(min(us / 1e6 / duration, 1.0), speed)
        proc.wait()
    finally:
        state["proc"] = None
        t.join(5)
    if state["stop"] == "now":
        raise UserError("Stopped.")
    if proc.returncode != 0:
        msg = next((e for e in reversed(errors) if e.strip()), f"ffmpeg stopped with code {proc.returncode}")
        raise UserError(f"ffmpeg couldn't convert it: {msg[:200]}")


def num(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def vmaf_score(copy: Path, original: Path, duration: float, interlaced: bool) -> float:
    """VMAF of three short samples (0 to 100; 93 and up looks the same to most people). None if it can't be measured."""
    ff, scores = core.ffmpeg_exe(), []
    for frac in SAMPLES:
        start = max(0.0, duration * frac - SAMPLE_SECONDS / 2)
        ref = ("bwdif=mode=send_frame," if interlaced else "") + "setpts=PTS-STARTPTS,format=yuv420p"
        graph = f"[0:v]setpts=PTS-STARTPTS,format=yuv420p[d];[1:v]{ref}[r];[d][r]libvmaf=n_threads={min(8, os.cpu_count() or 2)}"
        cmd = [ff, "-hide_banner", "-nostdin", "-ss", f"{start:.2f}", "-t", str(SAMPLE_SECONDS), "-i", str(copy),
               "-ss", f"{start:.2f}", "-t", str(SAMPLE_SECONDS), "-i", str(original), "-map", "0:V:0", "-map", "1:V:0",
               "-lavfi", graph, "-f", "null", "-"]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=600, creationflags=core.NO_WINDOW | (BELOW_NORMAL if os.name == "nt" else 0))
        except (OSError, subprocess.SubprocessError):
            return None
        m = re.search(r"VMAF score: ([\d.]+)", r.stderr.decode("utf-8", "replace"))
        if not m:
            return None
        scores.append(float(m.group(1)))
        if state["stop"] == "now":
            raise UserError("Stopped.")
    return round(min(scores), 1) if scores else None


def plays(copy: Path, duration: float) -> bool:
    """Quick check that the copy can be read at three points."""
    ff = core.ffmpeg_exe()
    for frac in SAMPLES:
        r = subprocess.run([ff, "-hide_banner", "-nostdin", "-v", "error", "-ss", f"{duration * frac:.2f}", "-t", "2", "-i", str(copy),
                            "-map", "0:V:0", "-f", "null", "-"], capture_output=True, timeout=300, creationflags=core.NO_WINDOW)
        if r.returncode != 0 or r.stderr.strip():
            return False
    return True


def created_time(path: Path):
    st = path.stat()
    return getattr(st, "st_birthtime", None) or (st.st_ctime if os.name == "nt" else None)


def set_created(path: Path, when: float) -> None:
    """Give the converted copy the original's 'date created' (what Jellyfin's 'date added' comes from)."""
    if os.name != "nt" or not when:
        return
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = wintypes.HANDLE
    h = k32.CreateFileW(str(path), 0x100, 0x7, None, 3, 0x80, None)  # FILE_WRITE_ATTRIBUTES, share all, OPEN_EXISTING
    if h in (None, wintypes.HANDLE(-1).value):
        return
    try:
        t = int((when + 11644473600) * 1e7)
        ft = ctypes.c_ulonglong(t)
        k32.SetFileTime(wintypes.HANDLE(h), ctypes.byref(ft), None, None)
    finally:
        k32.CloseHandle(wintypes.HANDLE(h))


def refresh_nfo(video: Path, kind: str) -> None:
    """The .nfo's technical details (codec, bitrate) now describe the new copy."""
    from . import mediainfo, nfo
    spots = [video.with_suffix(".nfo")]
    if kind != "tv":
        try:
            if sum(1 for p in video.parent.iterdir() if p.suffix.lower() in core.VIDEO_EXTS) == 1:
                spots.append(video.parent / "movie.nfo")
        except OSError:
            pass
    for p in spots:
        if not p.is_file():
            continue
        try:
            data = p.read_bytes()
            if kind == "tv":
                roots = nfo.parse_all(data)
                if not roots or not all(mediainfo.add_fileinfo(r, video) for r in roots):
                    continue
                out = nfo.to_bytes_all(roots)
            else:
                root = nfo.parse_xml(data, "movie")
                if root is None or not mediainfo.add_fileinfo(root, video):
                    continue
                out = nfo.to_bytes(root)
            tmp = p.with_name(p.name + ".new")
            tmp.write_bytes(out)
            os.replace(tmp, p)
            core.changed(p)
        except OSError:
            pass


def convert_one(src: Path, kind: str, opts: dict, progress) -> dict:
    """Convert one file, check it, and swap it in. Returns what happened; raises UserError to leave the original."""
    from . import mediainfo
    st = src.stat()
    sig = (st.st_size, int(st.st_mtime))
    text = mediainfo.text_of(src)
    f = facts(text or "")
    e = estimate(src, st.st_size, opts["quality"], opts["codec"], text)
    if not e["worth"] and not opts.get("force"):
        raise UserError(e["reason"] or "Not worth converting")
    if not f or f["codec"] not in OLD or f["hdr"]:
        raise UserError(e["reason"] or "Not a format this converts")
    enc = pick_encoder(opts["codec"], opts.get("encoder", "auto"))
    p = plan(src, f, enc, opts["quality"], e.get("target_kbps") or target_kbps(f, opts["quality"], opts["codec"]))
    tmp, final = p["tmp"], p["final"]
    free = shutil.disk_usage(src.parent).free
    if free < (e["after"] or st.st_size) * 1.2 + 1e9:
        raise UserError(f"Not enough free space on the drive ({free / 1e9:.1f} GB free).")
    if final != src and final.exists():
        raise UserError(f"“{final.name}” already exists next to it.")
    try:
        tmp.unlink(missing_ok=True)  # (a copy left half-done by an earlier run)
        progress("converting", 0, None)
        _run(p["cmd"], f["duration"], lambda frac, speed: progress("converting", frac, speed))
        progress("checking", 1, None)
        got = facts(core.ffmpeg_info(tmp))
        if not got or got["codec"] != opts["codec"]:
            raise UserError("The converted copy couldn't be read back.")
        if abs((got["duration"] or 0) - f["duration"]) > max(2, f["duration"] * 0.005):
            raise UserError(f"The converted copy is {got['duration']} s long instead of {f['duration']} s.")
        if (got["audio"], got["subs"]) != (f["audio"], f["subs"]):
            raise UserError(f"The converted copy has {got['audio']} audio and {got['subs']} subtitle tracks instead of {f['audio']} and {f['subs']}.")
        new_size = tmp.stat().st_size
        if new_size > st.st_size * (1 - MIN_SAVING * 0.6):
            pct = 100 - new_size * 100 // st.st_size
            raise _Kept("The converted copy wasn't smaller, so the original is kept" if pct <= 0 else f"Only {pct}% smaller, so the original is kept")
        score = None
        if opts.get("check", True):
            progress("quality", 1, None)
            score = vmaf_score(tmp, src, f["duration"], f["interlaced"])
            if score is not None and score < VMAF_BAR[opts["quality"]]:
                raise _Kept(f"Quality check scored {score} (needs {VMAF_BAR[opts['quality']]}), so the original is kept")
        if score is None and not plays(tmp, f["duration"]):
            raise UserError("The converted copy didn't play back cleanly.")
        progress("swapping", 1, None)
        with core.lock:
            now = src.stat()
            if (now.st_size, int(now.st_mtime)) != sig:
                raise UserError("The original changed while it was being converted.")
            created = created_time(src)
            core.discard(src)  # Recycle Bin (refuses if Windows would delete it for good)
            core.last_note(replaced_by=str(final))
            try:
                os.replace(tmp, final)
            except OSError:
                from . import recyclebin
                recyclebin.restore(str(src))  # put the original back where it was
                raise
            set_created(final, created)
            if final != src and kind != "tv":
                from . import library
                library.remap_matches([(src, final)])
        core.changed(final)
        refresh_nfo(final, kind)
        return {"ok": True, "before": st.st_size, "after": new_size, "saved": st.st_size - new_size, "vmaf": score,
                "encoder": enc, "path": str(final)}
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


class _Kept(UserError):
    """The copy worked but wasn't worth keeping (remembered, so the file isn't offered again)."""


# ---------------------------------------------------------------- the queue, in the background

def settings(cfg: dict = None) -> dict:
    cfg = core.load_config() if cfg is None else cfg
    return {"codec": cfg.get("convert_codec") if cfg.get("convert_codec") in CODECS else "hevc",
            "quality": cfg.get("convert_quality") if cfg.get("convert_quality") in QUALITY else "balanced",
            "encoder": cfg.get("convert_encoder") if cfg.get("convert_encoder") in ("auto", "gpu", "cpu") else "auto",
            "check": cfg.get("convert_check", True)}


def busy() -> bool:
    return state["running"]


def status() -> dict:
    d = _load()
    return {"running": state["running"], "stop": state["stop"], "current": state["current"],
            "queue": [{"path": q["path"], "name": q["name"]} for q in d["queue"]], "results": d["results"][-50:][::-1]}


def _awake(on: bool) -> None:
    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if on else 0))  # keep the PC from sleeping
        except (AttributeError, OSError):
            pass


def add(paths: list, kind: str) -> dict:
    names = {key_of(p): n for p, n, _ in library_files(kind)}
    with _lock:
        d = _load()
        have = {key_of(q["path"]) for q in d["queue"]}
        for p in paths:
            k = key_of(p)
            if k not in names:
                raise UserError(f"{p} isn't in the library.")
            if k not in have:
                d["queue"].append({"path": str(p), "name": names[k], "kind": kind})
                have.add(k)
        _save(d)
    return status()


def remove(paths: list = None) -> dict:
    with _lock:
        d = _load()
        drop = {key_of(p) for p in paths} if paths else None
        current = (state["current"] or {}).get("path")
        d["queue"] = [q for q in d["queue"] if q["path"] == current or (drop is not None and key_of(q["path"]) not in drop)]
        _save(d)
    return status()


def start() -> dict:
    with _lock:
        if state["running"]:
            return status()
        if not _load()["queue"]:
            raise UserError("Choose some files to convert first.")
        opts = settings()
        pick_encoder(opts["codec"], opts["encoder"])  # says now if it can't work
        state.update(running=True, stop=None, current=None, started=time.time())
    threading.Thread(target=_worker, args=(opts,), daemon=True, name="convert").start()
    return status()


def stop(now: bool = False) -> dict:
    state["stop"] = "now" if now else "after"
    proc = state.get("proc")
    if now and proc is not None:
        try:
            proc.kill()
        except OSError:
            pass
    return status()


def _worker(opts: dict) -> None:
    _awake(True)
    try:
        while not state["stop"]:
            with _lock:
                d = _load()
                if not d["queue"]:
                    break
                q = d["queue"][0]
            src = Path(q["path"])
            began = time.time()
            state["current"] = {"path": q["path"], "name": q["name"], "phase": "starting", "percent": 0, "speed": None,
                                "eta": None, "started": began}

            def progress(phase, frac, speed, q=q, began=began):
                cur = state["current"] or {}
                elapsed = time.time() - began
                cur.update(phase=phase, percent=round(frac * 100, 1), speed=speed,
                           eta=round(elapsed / frac - elapsed) if phase == "converting" and frac > 0.02 else None)

            result = {"name": q["name"], "path": q["path"], "time": time.time()}
            from . import history
            collecting = core.begin_activity("convert", history.job_title("convert"), "tv" if q.get("kind") == "tv" else "")
            try:
                if not src.is_file():
                    raise UserError("It's no longer there (moved or renamed since).")
                result.update(convert_one(src, q.get("kind", "movies"), opts, progress))
            except _Kept as err:
                result.update(ok=False, kept=True, reason=str(err))
                try:
                    st = src.stat()
                    with _lock:
                        d = _load()
                        d["tried"][key_of(src)] = {"size": st.st_size, "mtime": int(st.st_mtime), "reason": str(err)}
                        _save(d)
                except OSError:
                    pass
            except UserError as err:
                result.update(ok=False, reason=str(err))
            except OSError as err:
                result.update(ok=False, reason=f"File error: {err.strerror or err}")
            except Exception as err:  # never leave the queue stuck
                import logging
                logging.getLogger("mm").exception("Converting failed")
                result.update(ok=False, reason=f"Unexpected error: {err}")
            finally:
                if collecting:
                    core.end_activity()
            result["seconds"] = round(time.time() - began)
            import logging
            logging.getLogger("mm").info("Convert %s: %s", q["name"], f"saved {result['saved'] / 1e9:.2f} GB" if result.get("ok") else result.get("reason"))
            with _lock:
                d = _load()
                if not (state["stop"] == "now" and result.get("reason") == "Stopped."):
                    d["queue"] = [x for x in d["queue"] if x["path"] != q["path"]]
                    d["results"].append(result)
                _save(d)
            state["current"] = None
    finally:
        state.update(running=False, current=None, proc=None)
        _awake(False)
        try:
            from . import notify
            d = _load()
            mine = [r for r in d["results"] if r.get("time", 0) >= (state["started"] or 0)]
            if mine and not state["stop"]:
                saved = sum(r.get("saved", 0) for r in mine if r.get("ok"))
                notify.show("Converting finished", f"{sum(1 for r in mine if r.get('ok'))} of {len(mine)} files converted, "
                                                   f"{saved / 1e9:.1f} GB smaller. Empty the Recycle Bin to get the space back.")
        except Exception:
            pass
