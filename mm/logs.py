"""What the app prints and keeps.

The window only shows what matters: what you asked the app to do, and anything that went wrong. The page
checking in every few seconds, progress polling and pages loading are left out.

Everything worth keeping (actions, finished tasks, errors with their details) also goes to logs\\media-manager.log,
which keeps about 3 MB before the oldest lines are dropped.
"""
import logging
import re
from logging.handlers import RotatingFileHandler

from . import core

NOISE = re.compile(r'"(?:GET [^"]*|POST /api/(?:ping|bye|job/[^ "]*)[^"]*)" (?:200|204|304) ')
LOG_DIR = core.BASE / "logs"
FILE_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"


ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Quiet(logging.Filter):
    """Drops routine requests: anything that loaded fine, and the page's own check-ins."""
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return not NOISE.search(ANSI.sub("", record.getMessage()))
        except Exception:
            return True


class Plain(logging.Formatter):
    """The file gets the text without the window's colours."""
    def format(self, record):
        return ANSI.sub("", super().format(record))


def log_file():
    return LOG_DIR / "media-manager.log"


def setup() -> None:
    """Called once by the app and the command line (not by tests)."""
    quiet = Quiet()
    fh = None
    try:
        LOG_DIR.mkdir(exist_ok=True)
        fh = RotatingFileHandler(log_file(), maxBytes=1_000_000, backupCount=3, encoding="utf-8", delay=True)
        fh.setFormatter(Plain(FILE_FORMAT))
        fh.addFilter(quiet)
    except OSError:
        pass
    # requests: the window shows actions and failures, without the routine ones
    wz = logging.getLogger("werkzeug")
    wz.setLevel(logging.INFO)
    if not getattr(wz, "_mm_setup", False):
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(message)s"))
        sh.addFilter(quiet)
        wz.addHandler(sh)
        if fh:
            wz.addHandler(fh)
        wz.propagate = False
        wz._mm_setup = True
    # the app's own messages: problems in the window, everything in the file
    mm = logging.getLogger("mm")
    mm.setLevel(logging.INFO)
    if not getattr(mm, "_mm_setup", False):
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        sh.setLevel(logging.WARNING)
        mm.addHandler(sh)
        if fh:
            mm.addHandler(fh)
        mm._mm_setup = True
