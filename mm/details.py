"""The extra facts the library table can show and filter on: runtime, HDR, audio, bitrate (from what ffmpeg already
said about the file; nothing is read just for this), and rating, genres, collection and age rating (from the .nfo).
.nfo files are read once and remembered until they change."""
import os
import threading
from pathlib import Path

from . import mediainfo

_nfo_memo = {}   # path -> (mtime, size, facts)
_tech_memo = {}  # what ffmpeg said -> facts
_lock = threading.Lock()
AUDIO_NAMES = {"truehd": "TrueHD", "dts": "DTS", "dca": "DTS", "eac3": "E-AC3", "ac3": "AC3", "aac": "AAC", "flac": "FLAC",
               "opus": "Opus", "mp3": "MP3", "pcm_s16le": "PCM", "pcm_s24le": "PCM", "vorbis": "Vorbis", "mp2": "MP2"}
CH_NAMES = {1: "1.0", 2: "2.0", 3: "2.1", 6: "5.1", 7: "6.1", 8: "7.1"}
HDR_NAMES = {"hdr10": "HDR10", "hlg": "HLG", "dolbyvision": "Dolby Vision"}
RATING_ORDER = ("imdb", "themoviedb", "tmdb", "trakt", "rating")


def audio_name(a: dict) -> str:
    codec = AUDIO_NAMES.get(a.get("codec") or "", (a.get("codec") or "").upper())
    prof = (a.get("profile") or "").lower()
    if "atmos" in prof:
        codec += " Atmos"
    elif "dts-hd ma" in prof or "dts-hd master" in prof:
        codec = "DTS-HD MA"
    elif "dts:x" in prof:
        codec = "DTS:X"
    ch = CH_NAMES.get(a.get("channels"), f"{a['channels']}ch" if a.get("channels") else "")
    return " ".join(x for x in (codec, ch) if x)


def short_mpaa(m: str) -> str:
    """'US:R / US:Rated R' -> 'R'."""
    first = m.split("/")[0].strip()
    return first.split(":", 1)[1].strip() if ":" in first else first


def tech(video: Path) -> dict:
    """Runtime, HDR, audio, bitrate of a file ffmpeg has already read (empty if it hasn't)."""
    text = mediainfo.text_of(video, probe_missing=False)
    if not text:
        return {}
    with _lock:
        hit = _tech_memo.get(text)
    if hit is not None:
        return dict(hit)
    info = mediainfo.parse(text)
    if not info:
        return {}
    v = info.get("video") or {}
    audio = info.get("audio") or []
    best = max(audio, key=lambda a: (a.get("channels") or 0), default=None)
    from .library import CODEC_NAMES, resolution_of
    out = {"runtime": round(info["duration"] / 60) if info.get("duration") else None,
           "resolution": resolution_of(v["width"], v["height"]) if v.get("width") and v.get("height") else "",
           "codec": CODEC_NAMES.get(v.get("codec") or "", (v.get("codec") or "").upper()),
           "hdr": HDR_NAMES.get(v.get("hdr"), ""), "bit_depth": v.get("bit_depth"),
           "audio": audio_name(best) if best else "",
           "audio_langs": sorted({mediainfo.LANG2.get(a["language"], a["language"]) for a in audio if a.get("language")}),
           "bitrate": info.get("bitrate")}
    with _lock:
        _tech_memo[text] = out
    return dict(out)


def from_nfo(path: Path, tag: str = "movie") -> dict:
    """Rating, genres, collection, age rating, watched, from an .nfo file (remembered until it changes)."""
    try:
        st = os.stat(path)
    except OSError:
        return {}
    k = str(path).lower()
    with _lock:
        hit = _nfo_memo.get(k)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    from . import nfo  # (here, not at the top: nfo needs the library module, which needs this one)
    try:
        root = nfo.parse_xml(Path(path).read_bytes(), tag)
    except OSError:
        root = None
    i = nfo.read_nfo_root(root) if root is not None else {}
    r = i.get("ratings") or {}
    rating = None
    for name in RATING_ORDER + tuple(r):
        try:
            rating = round(float(r[name]), 1)
            break
        except (KeyError, TypeError, ValueError):
            continue
    facts = {"rating": rating, "genres": i.get("genres") or [], "set": i.get("set") or "", "mpaa": short_mpaa(i.get("mpaa") or ""),
             "watched": bool(i.get("watched")), "nfo_runtime": int(i["runtime"]) if str(i.get("runtime") or "").isdigit() else None,
             "tags": i.get("tags") or []}
    with _lock:
        _nfo_memo[k] = (st.st_mtime, st.st_size, facts)
    return facts


def for_movie(video: Path, nfo_file) -> dict:
    out = tech(video)
    if nfo_file:
        n = from_nfo(nfo_file)
        if not out.get("runtime") and n.get("nfo_runtime"):
            out["runtime"] = n["nfo_runtime"]
        out.update({k: v for k, v in n.items() if k != "nfo_runtime"})
    return out
