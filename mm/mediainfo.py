"""Technical details of a video file (codecs, resolution, HDR, audio channels, languages), read with ffmpeg,
for the <fileinfo><streamdetails> block Kodi and Jellyfin read from .nfo files."""
import re
import threading
from pathlib import Path
from xml.etree import ElementTree as ET

from . import core

CHANNELS = {"mono": 1, "stereo": 2, "2.1": 3, "quad": 4, "4.0": 4, "5.0": 5, "5.1": 6, "6.1": 7, "7.1": 8}
KEEP_LINE = re.compile(r"^\s*(Input #|Duration:|Stream #|DOVI configuration)")
_lock = threading.Lock()
_disk = None   # path -> {"size", "mtime", "text"}: the useful lines of what ffmpeg said, kept between runs
_dirty = False

# ffmpeg names languages with three letters (ISO 639-2); subtitle files use two (ISO 639-1)
LANG2 = {"eng": "en", "ger": "de", "deu": "de", "fre": "fr", "fra": "fr", "spa": "es", "ita": "it", "por": "pt", "rus": "ru",
         "jpn": "ja", "kor": "ko", "chi": "zh", "zho": "zh", "cze": "cs", "ces": "cs", "pol": "pl", "dut": "nl", "nld": "nl",
         "swe": "sv", "nor": "no", "nob": "no", "dan": "da", "fin": "fi", "gre": "el", "ell": "el", "tur": "tr", "ara": "ar",
         "heb": "he", "hun": "hu", "rum": "ro", "ron": "ro", "slo": "sk", "slk": "sk", "slv": "sl", "tha": "th", "ukr": "uk",
         "vie": "vi", "hin": "hi", "ind": "id", "may": "ms", "msa": "ms", "bul": "bg", "hrv": "hr", "scr": "hr", "srp": "sr",
         "est": "et", "lav": "lv", "lit": "lt", "ice": "is", "isl": "is", "per": "fa", "fas": "fa", "cat": "ca", "baq": "eu",
         "eus": "eu", "glg": "gl", "fil": "tl", "tgl": "tl", "ben": "bn", "tam": "ta", "tel": "te", "mal": "ml", "kan": "kn"}


def _store():
    global _disk
    if _disk is None:
        _disk = core.load_json(core.PROBES, {})
    return _disk


def text_of(path: Path, probe_missing: bool = True):
    """What ffmpeg says about a file (its useful lines), from the saved copy when the file hasn't changed."""
    global _dirty
    try:
        st = path.stat()
    except OSError:
        return None
    k = core.key_of(path)
    with _lock:
        hit = _store().get(k)
    if hit and hit["size"] == st.st_size and hit["mtime"] == int(st.st_mtime):
        return hit["text"]
    if not probe_missing:
        return None
    text = "\n".join(line for line in core.ffmpeg_info(path, timeout=60).splitlines() if KEEP_LINE.match(line))
    with _lock:
        _store()[k] = {"size": st.st_size, "mtime": int(st.st_mtime), "text": text}
        _dirty = True
    return text


def save() -> None:
    """Keep what ffmpeg said, so the next run doesn't read the files again."""
    global _dirty
    with _lock:
        if _dirty and _disk is not None:
            core.save_json(core.PROBES, _disk)
            _dirty = False


def probe(path: Path, probe_missing: bool = True):
    """{'duration', 'video': {...}, 'audio': [...], 'subtitles': [...]}, or None if the file can't be read
    (or, with probe_missing=False, hasn't been read yet)."""
    text = text_of(path, probe_missing)
    return parse(text) if text is not None else None


_sub_memo = {}


def embedded_subs(path: Path, probe_missing: bool = False) -> list:
    """Two-letter languages of the subtitle tracks inside the file (forced ones, which only cover foreign lines, left
    out). By default from what's already been read, so it never waits for ffmpeg."""
    text = text_of(path, probe_missing)
    if not text:
        return []
    if text not in _sub_memo:
        info = parse(text)
        _sub_memo[text] = sorted({LANG2.get(s["language"], s["language"]) for s in (info or {}).get("subtitles", [])
                                  if s["language"] and not s["forced"]})
    return _sub_memo[text]


def parse(text: str):
    d = re.search(r"Duration: (\d+):(\d\d):(\d\d)(?:\.\d+)?", text)
    duration = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + int(d.group(3)) if d else None
    b = re.search(r"Duration:.*?bitrate: (\d+) kb/s", text)
    fmt = re.search(r"Input #0, ([^,]+)", text)
    dv = re.search(r"DOVI configuration record:.*?profile: (\d+)", text)
    video, audio, subs = None, [], []
    for line in text.splitlines():
        m = re.search(r"Stream #\d+:\d+(?:\[\w+\])?(?:\((\w+)\))?[^:]*: (Video|Audio|Subtitle): (.*)", line)
        if not m:
            continue
        lang, kind, rest = (m.group(1) or "").lower(), m.group(2), m.group(3)
        codec = rest.split(",")[0].split(" ")[0].strip().lower()
        if kind == "Video" and video is None and "attached pic" not in line and codec not in ("mjpeg", "png", "bmp"):
            size = re.search(r"\b(\d{2,5})x(\d{2,5})\b", rest)
            dar = re.search(r"DAR (\d+):(\d+)", rest)
            w, h = (int(size.group(1)), int(size.group(2))) if size else (None, None)
            aspect = (int(dar.group(1)) / int(dar.group(2))) if dar and int(dar.group(2)) else ((w / h) if w and h else None)
            hdr = ""
            if "dovi" in text.lower() or "dolby vision" in text.lower():
                hdr = "dolbyvision"
            elif "smpte2084" in rest:
                hdr = "hdr10"
            elif "arib-std-b67" in rest:
                hdr = "hlg"
            prof = re.match(r"\w+ \(([^)]+)\)", rest)
            pix = re.search(r", (yuv\w+|p0\d\d\w*|gray\w*)", rest)
            video = {"codec": codec, "width": w, "height": h, "aspect": round(aspect, 2) if aspect else None, "hdr": hdr,
                     "profile": prof.group(1) if prof else "", "bit_depth": 10 if pix and re.search(r"1[02]le|p010", pix.group(1)) else 8,
                     "dv_profile": int(dv.group(1)) if dv else None}
        elif kind == "Audio":
            ch = re.search(r"Hz, ([^,]+),", rest)
            layout = (ch.group(1).strip() if ch else "").lower()
            n = re.match(r"(\d+) channels", layout)
            channels = int(n.group(1)) if n else CHANNELS.get(layout.split("(")[0], None)
            prof = re.match(r"\w+ \(([^)]+)\)", rest)
            audio.append({"codec": codec, "channels": channels, "language": lang if lang != "und" else "",
                          "profile": prof.group(1) if prof else ""})
        elif kind == "Subtitle":
            subs.append({"language": lang if lang != "und" else "", "forced": "(forced)" in rest, "codec": codec})
    if duration is None and video is None:
        return None
    return {"duration": duration, "video": video, "audio": audio, "subtitles": subs,
            "bitrate": int(b.group(1)) if b else None, "format": fmt.group(1).strip() if fmt else ""}


def streamdetails(info: dict):
    """The <fileinfo> element Kodi and Jellyfin read."""
    fileinfo = ET.Element("fileinfo")
    sd = ET.SubElement(fileinfo, "streamdetails")
    v = info.get("video")
    if v:
        ve = ET.SubElement(sd, "video")
        for tag, val in (("codec", v["codec"]), ("aspect", v["aspect"]), ("width", v["width"]), ("height", v["height"]),
                         ("durationinseconds", info.get("duration")), ("hdrtype", v.get("hdr"))):
            if val not in (None, ""):
                ET.SubElement(ve, tag).text = str(val)
    for a in info.get("audio", []):
        ae = ET.SubElement(sd, "audio")
        for tag, val in (("codec", a["codec"]), ("language", a["language"]), ("channels", a["channels"])):
            if val not in (None, ""):
                ET.SubElement(ae, tag).text = str(val)
    for s in info.get("subtitles", []):
        if s["language"]:
            ET.SubElement(ET.SubElement(sd, "subtitle"), "language").text = s["language"]
    return fileinfo


def add_fileinfo(root, video: Path) -> bool:
    """Replace the .nfo's <fileinfo> with fresh details from the file. If the file can't be read, an existing
    <fileinfo> (from tinyMediaManager, say) is left as it is."""
    info = probe(video)
    if not info:
        return False
    for old in root.findall("fileinfo"):
        root.remove(old)
    root.append(streamdetails(info))
    return True


def summary(info) -> dict:
    """Short readable lines for the detail window."""
    if not info:
        return {}
    v = info.get("video") or {}
    from .library import resolution_of
    res = resolution_of(v["width"], v["height"]) if v.get("width") and v.get("height") else ""
    video = " ".join(x for x in (res, (v.get("codec") or "").upper(), {"hdr10": "HDR10", "hlg": "HLG", "dolbyvision": "Dolby Vision"}.get(v.get("hdr"), ""),
                                f"{v['width']}×{v['height']}" if v.get("width") else "") if x)
    names = {1: "mono", 2: "stereo", 6: "5.1", 7: "6.1", 8: "7.1"}
    audio = ", ".join(" ".join(x for x in ((a["language"] or "").upper(), names.get(a["channels"], f"{a['channels']} ch" if a["channels"] else ""),
                                          a["codec"].upper()) if x) for a in info.get("audio", []))
    subs = sorted({s["language"] for s in info.get("subtitles", []) if s["language"]})
    dur = info.get("duration")
    return {"video": video, "audio": audio, "subtitles": ", ".join(subs), "duration": f"{dur // 3600} h {dur % 3600 // 60} min" if dur else ""}
