# PyInstaller recipe for MediaManager.exe. Build it with packaging\build.bat (not by hand).
#
# Only the program goes in: the page (static\) and the tools it runs (ffmpeg, deno). Your settings, keys, scans,
# matches, history, backups and logs are never bundled. The packaged app keeps those in
# %LOCALAPPDATA%\Media Manager (see core.data_dir), and API keys stay in Windows Credential Manager.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

APP = Path(SPECPATH).parent
VENV_SCRIPTS = APP / ".venv" / "Scripts"

datas = [(str(APP / "static"), "static")]
datas += collect_data_files("guessit") + collect_data_files("babelfish") + collect_data_files("rebulk")
datas += collect_data_files("yt_dlp_ejs")          # the scripts yt-dlp uses to solve YouTube's challenges
datas += collect_data_files("certifi")
binaries = []
deno = VENV_SCRIPTS / "deno.exe"                     # lets yt-dlp unlock YouTube's HD trailers
if deno.exists():
    binaries.append((str(deno), "bin"))              # deno.find_deno_bin() also looks in <bundle>\bin

hiddenimports = collect_submodules("mm") + collect_submodules("yt_dlp") + collect_submodules("yt_dlp_ejs")
hiddenimports += collect_submodules("guessit") + collect_submodules("babelfish") + collect_submodules("rebulk")

a = Analysis(
    [str(APP / "app.py")],
    pathex=[str(APP)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "unittest", "pydoc", "tests"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MediaManager",
    icon=str(Path(SPECPATH) / "icon.ico"),
    console=True,          # the small window shows what the app is doing; closing the browser tab ends it
    upx=False,             # packed exes trip antivirus more often
)
coll = COLLECT(exe, a.binaries, a.datas, name="MediaManager", upx=False)
