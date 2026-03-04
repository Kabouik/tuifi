from __future__ import annotations

import curses
import os
import sys
import time
from typing import Dict, List

from .config import DEFAULT_API, STATE_DIR, TAB_QUEUE
from .utils import mkdirp, debug_log, print_version
import tuifi_pkg.utils as _utils_mod
from .app import App


def parse_args(argv: List[str]) -> Dict[str, any]:
    out: Dict[str, any] = {"api": DEFAULT_API}
    i = 1
    while i < len(argv):
        a = argv[i]

        if a in ("--api", "-a") and i + 1 < len(argv):
            out["api"] = argv[i + 1]; i += 2; continue

        if a in ("--verbose", "-v"):
            out["verbose"] = True; i += 1; continue

        if a in ("--version", "-V"):
            print_version(argv[0])
            sys.exit(0)

        if a in ("-h", "--help"):
            print(
                f"Usage: {argv[0]} [options]\n"
                "\n"
                "Options:\n"
                f"  --api URL, -a URL   API base URL (default: {DEFAULT_API})\n"
                "  --verbose, -v       Write debug log to debug.log in the config directory\n"
                "  --version, -V       Show version\n"
                "\n"
                f"Press ? in tuifi for more keybinds and more options (automatically saved in settings.json)\n"
            )
            sys.exit(0)
        i += 1
    return out


def main(argv: List[str]) -> int:
    args = parse_args(argv)

    if not os.path.isdir(STATE_DIR):
        print(f"tuifi config directory does not exist and will be created at:\n  {STATE_DIR}")
        try:
            input("Press Return to continue, or Ctrl-C to abort.")
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        mkdirp(STATE_DIR)

    if args.get("verbose"):
        _utils_mod._DEBUG_LOG = os.path.join(STATE_DIR, "debug.log")
        debug_log(f"=== tuifi start {time.strftime('%Y-%m-%d %H:%M:%S')} ===")

    def wrapped(stdscr: "curses._CursesWindow") -> None:
        app = App(stdscr, args.get("api", DEFAULT_API), args)
        if app.tab == TAB_QUEUE:
            app.focus = "queue"
        app.run()

    curses.wrapper(wrapped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
