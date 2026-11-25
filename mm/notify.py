"""Windows notifications (the pop-ups in the corner of the screen), for when the folder watcher has handled new
files. They work whether or not the page is open; clicking one opens the app.

Shown through Windows PowerShell, which every Windows 10 and 11 has, so nothing extra needs installing. Only the
app switches this on (`enabled`), so tests never pop anything up."""
import os
import subprocess
from xml.sax.saxutils import escape

from . import core

enabled = False
APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
SCRIPT = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
    "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null;"
    "$x = New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml($env:MM_TOAST);"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:MM_TOAST_APP).Show("
    "[Windows.UI.Notifications.ToastNotification]::new($x))"
)


def toast_xml(title: str, text: str, link: str = "") -> str:
    launch = f' launch="{escape(link, {chr(34): "&quot;"})}" activationType="protocol"' if link else ""
    return (f'<toast{launch}><visual><binding template="ToastGeneric"><text>{escape(title[:120])}</text>'
            f'<text>{escape(text[:400])}</text></binding></visual></toast>')


def show(title: str, text: str) -> bool:
    """Pop up a notification. False if it couldn't be shown (not Windows, or switched off)."""
    if not enabled or os.name != "nt":
        return False
    env = dict(os.environ, MM_TOAST=toast_xml(title, text, f"http://127.0.0.1:{core.PORT}"), MM_TOAST_APP=APP_ID)
    try:
        subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", SCRIPT],
                         env=env, creationflags=core.NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def watch_result(r: dict) -> bool:
    """Tell you what the watcher did, if it did anything worth knowing."""
    if not r or not (r.get("added") or r.get("problem")):
        return False
    if not core.load_config().get("watch_notify", True):
        return False
    from .watch import describe
    n = len(r.get("added") or [])
    what = ("movie" if r["kind"] == "movies" else "episode") + ("" if n == 1 else "s")
    title = f"{n} new {what} handled" if n else "The folder watcher needs you"
    return show(title, describe(r))
