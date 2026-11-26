"""The packaged MediaManager.exe: where it keeps its data and finds its page, and the switches for trying a build.
Each case runs in a fresh Python, with LOCALAPPDATA pointed at a temporary folder, so nothing real is touched."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
S = Path(tempfile.mkdtemp(prefix="mm-test-pkg-"))
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)

PROBE = """
import json, sys
sys.path.insert(0, {app!r})
if {frozen}:
    sys.frozen = True
    sys._MEIPASS = {meipass!r}
from mm import core, web
print(json.dumps({{"base": str(core.BASE), "res": str(core.RES), "config": str(core.CONFIG), "port": core.PORT,
                  "isolated": core.ISOLATED, "static": web.app.static_folder, "exists": core.BASE.is_dir()}}))
"""


def probe(frozen=False, **env):
    clean = {k: v for k, v in os.environ.items() if not k.startswith("MM_")}
    clean.update(LOCALAPPDATA=str(S / "appdata"), **env)
    code = PROBE.format(app=str(APP), frozen=frozen, meipass=str(APP))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=clean, cwd=str(S))
    if out.returncode:
        print(out.stderr)
    return json.loads(out.stdout.strip().splitlines()[-1])


src = probe()
ok(Path(src["base"]) == APP and Path(src["config"]) == APP / "config.json", "run from source: data stays in the app folder, as before")
ok(src["port"] == 8765 and not src["isolated"], "run from source: the usual port, not isolated")

exe = probe(frozen=True)
want = S / "appdata" / "Media Manager"
ok(Path(exe["base"]) == want and Path(exe["config"]) == want / "config.json", "packaged: data goes to %LOCALAPPDATA%\\Media Manager")
ok(exe["exists"], "packaged: the data folder is created on first start")
ok(Path(exe["static"]) == APP / "static" and Path(exe["res"]) == APP, "packaged: the page is served from the unpacked bundle")

trial = probe(frozen=True, MM_DATA=str(S / "trial"), MM_PORT="8790", MM_ISOLATED="1")
ok(Path(trial["base"]) == (S / "trial").resolve() and trial["exists"], "MM_DATA puts the data in a folder of your choice")
ok(trial["port"] == 8790, "MM_PORT runs it on another port, next to a copy that's already open")
ok(trial["isolated"], "MM_ISOLATED=1 is recognised")
ok(probe(MM_PORT="abc")["port"] == 8765, "a port that isn't a number is ignored")

# MM_ISOLATED keeps Credential Manager, backups, the log file and notifications off even with the app's own config
code = f"""
import sys; sys.path.insert(0, {str(APP)!r})
from mm import backup, core, notify, vault, web
web.app_mode()
print(vault.enabled, backup.enabled, notify.enabled, core.CONFIG == core.BASE / "config.json")
"""
env = {k: v for k, v in os.environ.items() if not k.startswith("MM_")}
env.update(MM_DATA=str(S / "iso"), MM_ISOLATED="1", LOCALAPPDATA=str(S / "appdata"))
out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env).stdout.split()
ok(out == ["False", "False", "False", "True"], "MM_ISOLATED: no credentials, backups, log file or notifications, even with the app's own config")
ok(not (S / "iso" / "logs").exists() and not (S / "iso" / "backups").exists(), "MM_ISOLATED: no log or backup folder written")
