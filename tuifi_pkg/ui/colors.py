"""Color initialization and lookup for tuifi."""

from __future__ import annotations

import curses
import signal
from typing import Any, Dict, Optional


def name_to_curses_color(name: str) -> int:
    mapping = {
        "black": curses.COLOR_BLACK, "red": curses.COLOR_RED,
        "green": curses.COLOR_GREEN, "yellow": curses.COLOR_YELLOW,
        "blue": curses.COLOR_BLUE, "magenta": curses.COLOR_MAGENTA,
        "cyan": curses.COLOR_CYAN, "white": curses.COLOR_WHITE,
    }
    s = str(name).strip().lower()
    if s in mapping:
        return mapping[s]
    try:
        n = int(s)
        if 0 <= n <= 255:
            return n
    except Exception:
        pass
    return curses.COLOR_WHITE


def init_colors(stdscr, settings: Dict[str, Any]) -> None:
    """Initialise curses and color pairs from settings."""
    try:
        signal.signal(signal.SIGQUIT, signal.SIG_IGN)
    except Exception:
        pass
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)
    curses.noecho()
    curses.cbreak()
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        s = settings
        curses.init_pair(1,  name_to_curses_color(s.get("color_playing",   "green")),   -1)
        curses.init_pair(2,  name_to_curses_color(s.get("color_paused",    "yellow")),  -1)
        curses.init_pair(3,  name_to_curses_color(s.get("color_error",     "red")),     -1)
        curses.init_pair(4,  name_to_curses_color(s.get("color_chrome",    "black")),   -1)
        curses.init_pair(5,  name_to_curses_color(s.get("color_accent",    "magenta")), -1)
        curses.init_pair(6,  name_to_curses_color(s.get("color_accent",    "magenta")), -1)
        curses.init_pair(7,  name_to_curses_color(s.get("color_artist",    "white")),   -1)
        curses.init_pair(8,  name_to_curses_color(s.get("color_album",     "blue")),    -1)
        curses.init_pair(9,  name_to_curses_color(s.get("color_duration",  "black")),   -1)
        curses.init_pair(10, name_to_curses_color(s.get("color_numbers",   "black")),   -1)
        curses.init_pair(11, name_to_curses_color(s.get("color_title",     "white")),   -1)
        curses.init_pair(12, name_to_curses_color(s.get("color_year",      "blue")),    -1)
        curses.init_pair(13, name_to_curses_color(s.get("color_separator", "white")),   -1)
        curses.init_pair(14, name_to_curses_color(s.get("color_liked",     "white")),   -1)
        curses.init_pair(15, name_to_curses_color(s.get("color_mark",      "red")),     -1)
        curses.init_pair(16, curses.COLOR_WHITE, curses.COLOR_BLACK)


def C(color_mode: bool, pair: int) -> int:
    """Return curses color attribute for pair, or 0 if color mode is off."""
    if color_mode and curses.has_colors():
        return curses.color_pair(pair)
    return 0
