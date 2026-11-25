"""Media manager: start it with run.bat. The code lives in the mm folder (see mm/__init__.py)."""
import sys

if __name__ == "__main__":
    if sys.argv[1:] == ["watch"]:  # watch for new files without the page (for Task Scheduler or startup)
        from mm.cli import watch_forever
        sys.exit(watch_forever())
    if len(sys.argv) > 1:  # command-line mode: app.py --help
        from mm.cli import run
        sys.exit(run())
    from mm.web import main
    main()
