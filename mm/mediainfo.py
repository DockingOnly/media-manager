"""Technical details of a video file (codecs, resolution, HDR, audio channels, languages), read with ffmpeg,
for the <fileinfo><streamdetails> block Kodi and Jellyfin read from .nfo files."""
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from . import core

CHANNELS = {"mono": 1, "stereo": 2, "2.1": 3, "quad": 4, "4.0": 4, "5.0": 5, "5.1": 6, "6.1": 7, "7.1": 8}
_cache = {}


def probe(path: Path):
    """{'duration', 'video': {...}, 'audio': [...], 'subtitles': [...]}, or None if the file can't be read."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (core.key_of(path), st.st_size, int(st.st_mtime))
    if key in _cache:
        return _cache[key]
    text = core.ffmpeg_info(path, timeout=60)
    info = parse(text)
    _cache[key] = info
    return info


def parse(text: str):
    d = re.search(r"Duration: (\d+):(\d\d):(\d\d)(?:\.\d+)?", text)
    duration = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + int(d.group(3)) if d else None
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
            video = {"codec": codec, "width": w, "height": h, "aspect": round(aspect, 2) if aspect else None, "hdr": hdr}
        elif kind == "Audio":
            ch = re.search(r"Hz, ([^,]+),", rest)
            layout = (ch.group(1).strip() if ch else "").lower()
            n = re.match(r"(\d+) channels", layout)
            channels = int(n.group(1)) if n else CHANNELS.get(layout.split("(")[0], None)
            audio.append({"codec": codec, "channels": channels, "language": lang if lang != "und" else ""})
        elif kind == "Subtitle":
            subs.append({"language": lang if lang != "und" else "", "forced": "(forced)" in rest})
    if duration is None and video is None:
        return None
    return {"duration": duration, "video": video, "audio": audio, "subtitles": subs}


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
