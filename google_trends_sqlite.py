"""DEPRECATED: replaced by ``python -m trends.rss_collector``.

The original script inserted every RSS item on every run (no deduplication) and stored
pubDate as Pacific wall-clock time. It is kept only as a shim so an old shortcut or habit
starts the new idempotent collector instead of writing legacy data. Use run.bat or:

    python -m trends.rss_collector --once
    python -m trends.rss_collector --schedule
"""
import sys
import warnings

from trends.rss_collector import main


if __name__ == "__main__":
    warnings.warn(
        "google_trends_sqlite.py is deprecated; running `python -m trends.rss_collector --schedule`",
        DeprecationWarning,
        stacklevel=1,
    )
    print("google_trends_sqlite.py 已停用，改為執行 trends.rss_collector --schedule", file=sys.stderr)
    raise SystemExit(main(["--schedule", *sys.argv[1:]]))
