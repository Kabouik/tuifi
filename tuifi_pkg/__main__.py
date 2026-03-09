"""Entry point for `python -m tuifi_pkg`."""

from __future__ import annotations

import sys

from tuifi_pkg.app import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
