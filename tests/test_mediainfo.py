"""Technical details in .nfo files: codecs, resolution, HDR, audio channels and languages, read with ffmpeg."""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mm import core, mediainfo, nfo  # noqa: E402

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="mm-test-"))
work = S / "mi"
shutil.rmtree(work, ignore_errors=True)
work.mkdir(parents=True)
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)

HDR = """  Duration: 02:01:30.00, start: 0.000000, bitrate: 50000 kb/s
  Stream #0:0: Video: hevc (Main 10), yuv420p10le(tv, bt2020nc/bt2020/smpte2084), 3840x2160 [SAR 1:1 DAR 16:9], 23.98 fps
  Stream #0:1(eng): Audio: truehd, 48000 Hz, 7.1, s32 (24 bit)
  Stream #0:2(ger): Audio: ac3, 48000 Hz, 5.1(side), fltp, 640 kb/s
  Stream #0:3(eng): Subtitle: hdmv_pgs_subtitle (pgs) (forced)
  Stream #0:4: Video: mjpeg (Baseline), yuvj420p(pc), 600x900, 90k tbr (attached pic)"""
DV = """  Duration: 00:44:00.00
  Stream #0:0(und): Video: hevc (Main 10), yuv420p10le(tv, bt2020nc/bt2020/smpte2084), 3840x1608, 23.98 fps
      DOVI configuration record: version: 1.0, profile: 8
  Stream #0:1(und): Audio: aac (LC), 48000 Hz, mono, fltp"""
i = mediainfo.parse(HDR)
ok(i["duration"] == 7290 and i["video"] == {"codec": "hevc", "width": 3840, "height": 2160, "aspect": 1.78, "hdr": "hdr10"},
   "HDR10 4K video read (cover art ignored)")
ok([(a["codec"], a["channels"], a["language"]) for a in i["audio"]] == [("truehd", 8, "eng"), ("ac3", 6, "ger")], "7.1 and 5.1 audio tracks with languages")
d = mediainfo.parse(DV)
ok(d["video"]["hdr"] == "dolbyvision" and d["audio"][0]["channels"] == 1 and d["audio"][0]["language"] == "", "Dolby Vision; mono; an undetermined language is left empty")
x = ET.tostring(mediainfo.streamdetails(i), encoding="unicode")
ok("<hdrtype>hdr10</hdrtype>" in x and "<channels>8</channels>" in x and "<durationinseconds>7290</durationinseconds>" in x
   and x.count("<subtitle>") == 1, "the <fileinfo><streamdetails> block Kodi and Jellyfin read")

# a real (tiny) video with sound, written into a new .nfo
video = work / "Clip (2020).mkv"
subprocess.run([core.ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:duration=2", "-f", "lavfi", "-i",
                "sine=duration=2", "-ac", "2", "-c:v", "mpeg4", "-c:a", "aac", "-metadata:s:a:0", "language=eng", str(video)], check=True)
fresh = nfo.build_nfo({"id": 1, "title": "Clip"})
nfo.write_nfo(video.with_suffix(".nfo"), fresh, 1, overwrite=True, video=video)
root = ET.fromstring(video.with_suffix(".nfo").read_text(encoding="utf-8").split("?>", 1)[1])
ok(root.findtext("fileinfo/streamdetails/video/width") == "1280" and root.findtext("fileinfo/streamdetails/audio/channels") == "2"
   and root.findtext("fileinfo/streamdetails/audio/language") == "eng", "a real file's details written into its .nfo")

# a file that can't be read keeps its existing details (tinyMediaManager's, say)
broken = work / "Broken (2020).mkv"
broken.write_bytes(b"not a video")
broken.with_suffix(".nfo").write_text('<?xml version="1.0"?><movie><title>Broken</title><fileinfo><streamdetails><video><codec>h264</codec></video></streamdetails></fileinfo></movie>', encoding="utf-8")
nfo.write_nfo(broken.with_suffix(".nfo"), fresh, 1, overwrite=True, video=broken)
ok("<codec>h264</codec>" in broken.with_suffix(".nfo").read_text(encoding="utf-8"), "when a file can't be read, its existing details are kept")

shutil.rmtree(S, ignore_errors=True)
print("done")
