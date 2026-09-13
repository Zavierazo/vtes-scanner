"""Validate worker limits, then replace this process with uWSGI."""

import os
from pathlib import Path
import sys


def main():
    minimum = int(os.environ.get("WEB_CONCURRENCY", "1"))
    maximum = int(os.environ.get("MAX_WEB_CONCURRENCY", "3"))
    if (not 1 <= minimum <= maximum <= 3):
        raise ValueError("Require 1 <= WEB_CONCURRENCY <= MAX_WEB_CONCURRENCY <= 3")
    if (len(sys.argv) > 2):
        raise ValueError("Usage: python serve.py [module:app]")
    command = [
        "uwsgi", "--ini", str(Path(__file__).with_name("uwsgi.ini")),
        "--module", sys.argv[1] if (len(sys.argv) == 2) else "server:app",
        "--processes", str(maximum),
        # uWSGI requires cheaper < processes; zero disables adaptive spawning.
        "--cheaper", str(minimum if (minimum < maximum) else 0),
        "--cheaper-initial", str(minimum),
    ]
    os.execvp(command[0], command)


if (__name__ == "__main__"):
    main()
