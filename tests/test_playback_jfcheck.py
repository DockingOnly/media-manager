"""Playback check (what Jellyfin converts, per kind of player), the saved ffmpeg readings, subtitles inside videos,
and the check of Jellyfin's own settings with its one-click fixes."""
import json
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mm import core, mediainfo, playback, web  # noqa: E402

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="mm-test-"))
lib, st = S / "libP", S / "stP"
for d in (lib, st):
    shutil.rmtree(d, ignore_errors=True)
st.mkdir(parents=True)
for name in ("CONFIG", "MATCHES", "RENAME_LOG", "LIBRARY", "EDITS", "TV_LIBRARY", "TV_MATCHES", "TV_CACHE", "TV_RENAME_LOG", "PROBES"):
    setattr(core, name, st / f"{name.lower()}.json")
mediainfo._disk = None
core.save_config(tmdb_key="x", folder=str(lib))
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)
calls = []


def fake_ffmpeg(p, timeout=60):
    calls.append(Path(p).name)
    return Path(p).read_text()


core.ffmpeg_info = fake_ffmpeg


def info(video, audio="aac", subs=(), bitrate=5000, fmt="matroska,webm", extra=""):
    lines = [f"Input #0, {fmt}, from 'x':", f"  Duration: 01:40:00.00, start: 0.000000, bitrate: {bitrate} kb/s",
             f"  Stream #0:0: Video: {video}, 1920x1080, 23.98 fps"]
    if extra:
        lines.append(extra)
    lines.append(f"  Stream #0:1(eng): Audio: {audio}, 48000 Hz, 5.1(side), fltp")
    for i, s in enumerate(subs):
        lines.append(f"  Stream #0:{i + 2}{s}")
    return "\n".join(lines)


R = lambda text, prof, ext=".mkv": [sev for sev, _ in playback.reasons(mediainfo.parse(text) if text else None, ext, prof)]
plain = info("h264 (High), yuv420p(progressive)")
ok(all(R(plain, p) == [] for p in playback.PROFILES), "H.264 with AAC in MKV plays directly everywhere")
hevc = info("hevc (Main 10), yuv420p10le(tv, bt709)", audio="eac3")
ok(R(hevc, "browser") == ["maybe", "maybe"] and R(hevc, "tv") == [] and R(hevc, "app") == [], "H.265 + EAC3: depends in a browser, fine on a TV")
xvid = info("mpeg4 (Advanced Simple Profile) (XVID / 0x44495658), yuv420p", audio="mp3", fmt="avi")
ok(R(xvid, "tv", ".avi") == ["heavy", "light"] and R(xvid, "app", ".avi") == [], "an old Xvid AVI: video converted on a TV; fine in a desktop player")
ok(R(info("h264 (High 10), yuv420p10le(progressive)"), "tv") == ["heavy"], "10-bit H.264 is converted")
dv5 = info("hevc (Main 10), yuv420p10le(tv, bt2020nc/bt2020/smpte2084)", extra="      DOVI configuration record: version: 1.0, profile: 5")
ok("heavy" in R(dv5, "tv") and R(dv5, "app") == [], "Dolby Vision profile 5 needs converting without Dolby Vision")
ok(R(info("h264 (High), yuv420p", audio="truehd"), "tv") == ["light"] and R(info("h264 (High), yuv420p", audio="dca (DTS-HD MA)"), "tv") == ["maybe"],
   "TrueHD audio converted (light); DTS (ffmpeg's 'dca') depends on the TV")
pgs = info("h264 (High), yuv420p", subs=["(eng): Subtitle: hdmv_pgs_subtitle (pgs)"])
ok(R(pgs, "browser") == ["subs"] and R(pgs, "tv") == [], "picture subtitles are burned in only in a browser")
ok(R(info("hevc (Main 10), yuv420p10le", bitrate=65000), "tv") == ["remote"], "a very high bitrate is noted for watching away from home")
ok(R("", "tv") == ["broken"], "a file ffmpeg can't read is flagged as possibly damaged")

# The check itself, through the app, and the saved readings
(lib / "Heat (1995)").mkdir(parents=True)
(lib / "Heat (1995)" / "Heat (1995).mkv").write_text(info("h264 (High), yuv420p", subs=["(eng): Subtitle: subrip (srt)", "(ger): Subtitle: subrip (srt) (forced)"]))
(lib / "Old (1999)").mkdir()
(lib / "Old (1999)" / "Old (1999).avi").write_text(xvid)
(lib / "Bad (2001)").mkdir()
(lib / "Bad (2001)" / "Bad (2001).mkv").write_text("not a video")
c = web.app.test_client()


def run(url, data):
    r = c.post(url, json=data).get_json()
    while not (j := c.get(f"/api/job/{r['job']}").get_json())["finished"]:
        time.sleep(0.05)
    assert not j["error"], j["error"]
    return j["result"]


items = run("/api/scan", {"folder": str(lib)})["items"]
heat = next(i for i in items if i["path"].startswith("Heat"))
calls.clear()
lib_now = c.get("/api/library").get_json()["items"]
ok(not calls, "showing the library never runs ffmpeg")
res = run("/api/playback", {"kind": "movies"})
by = {f["name"].split(" (")[0]: f for f in res["files"]}
ok([s for s, _ in by["Bad"]["by_profile"]["tv"]] == ["broken"] and [s for s, _ in by["Old"]["by_profile"]["tv"]] == ["heavy", "light"]
   and by["Heat"]["by_profile"]["tv"] == [], "each file is judged per kind of player")
ok(core.PROBES.exists() and len(json.loads(core.PROBES.read_text(encoding="utf-8"))) == 3, "what ffmpeg said is saved")
mediainfo._disk = None  # as after a restart
calls.clear()
run("/api/playback", {"kind": "movies"})
ok(not calls, "the next check reads nothing again")
heat_now = next(i for i in c.get("/api/library").get_json()["items"] if i["path"].startswith("Heat"))
ok(heat_now["files"]["subs_in"] == ["en"] and heat_now["files"]["subs"] == [], "subtitles inside the video are shown (forced tracks left out)")
(lib / "Heat (1995)" / "Heat (1995).eng.srt").write_text("1")
heat_now = next(i for i in c.get("/api/library").get_json()["items"] if i["path"].startswith("Heat"))
ok(heat_now["files"]["subs"] == ["en"], "a subtitle file named .eng counts as 'en', the same as the track inside")

# ---------------------------------------------------------------- Jellyfin's settings
state = {"libs": [{"Name": "Movies", "ItemId": "L1", "CollectionType": "movies", "Locations": [str(lib)],
                   "LibraryOptions": {"DisabledLocalMetadataReaders": ["Nfo"], "MetadataSavers": ["Nfo"], "Other": 7,
                                      "TypeOptions": [{"Type": "Movie", "MetadataFetchers": ["TheMovieDb"]}]}},
                  {"Name": "Music", "ItemId": "L2", "CollectionType": "music", "Locations": ["E:\\Music"], "LibraryOptions": {}}],
         "nfo": {"UserId": "", "ReleaseDateFormat": "yyyy-MM-dd"}, "posts": []}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, data=None):
        raw = json.dumps(data).encode() if data is not None else b""
        self.send_response(code)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/Library/VirtualFolders":
            return self._send(200, state["libs"])
        if self.path == "/System/Configuration/xbmcmetadata":
            return self._send(200, state["nfo"])
        if self.path == "/Users":
            return self._send(200, [{"Id": "U1", "Name": "me"}])
        return self._send(200, {"ServerName": "Den", "Version": "10.10.7"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")
        state["posts"].append((self.path, body))
        if self.path == "/Library/VirtualFolders/LibraryOptions":
            next(x for x in state["libs"] if x["ItemId"] == body["Id"])["LibraryOptions"] = body["LibraryOptions"]
        if self.path == "/System/Configuration/xbmcmetadata":
            state["nfo"] = body
        return self._send(204)


srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
core.save_config(jellyfin_url=f"http://127.0.0.1:{srv.server_address[1]}", jellyfin_key="k")
d = c.get("/api/jellyfin/check").get_json()
lv = [(i["level"], i["fix"]) for i in d["issues"]]
ok(lv[0] == ("problem", "enable_nfo") and ("warning", "no_nfo_saver") in lv and ("warning", "nfo_user") in lv
   and any(i["level"] == "note" and "TheMovieDb" in i["text"] for i in d["issues"]),
   f"ignored .nfo files, Jellyfin's own .nfo writer, no watched-status user, online lookups: all found {lv}")
ok(d["libraries"][0]["yours"] == ["movies"] and d["libraries"][1]["yours"] == [], "which Jellyfin library holds your folder")
d = c.post("/api/jellyfin/fix", json={"fix": "enable_nfo", "library": "L1"}).get_json()
opts = state["libs"][0]["LibraryOptions"]
ok(opts["DisabledLocalMetadataReaders"] == [] and opts["MetadataSavers"] == ["Nfo"] and opts["Other"] == 7
   and not any(i["fix"] == "enable_nfo" for i in d["issues"]), "Fix turns .nfo reading back on, and changes nothing else")
c.post("/api/jellyfin/fix", json={"fix": "no_nfo_saver", "library": "L1"})
d = c.post("/api/jellyfin/fix", json={"fix": "nfo_user"}).get_json()
ok(state["libs"][0]["LibraryOptions"]["MetadataSavers"] == [] and state["nfo"] == {"UserId": "U1", "ReleaseDateFormat": "yyyy-MM-dd"}
   and not [i for i in d["issues"] if i["level"] != "note"], "the other fixes work too; afterwards only notes remain")
r = c.post("/api/jellyfin/fix", json={"fix": "delete_everything"})
ok(r.status_code == 400, "only the offered fixes can be made")
srv.shutdown()
shutil.rmtree(S, ignore_errors=True)
print("done")
