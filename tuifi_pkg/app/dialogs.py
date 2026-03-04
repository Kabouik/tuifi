from __future__ import annotations
import curses
from typing import List, Optional
from ..utils import clamp


class DialogsMixin:
    def prompt_text(self, title: str, initial: str = "") -> Optional[str]:
        h, w = self.stdscr.getmaxyx()
        box_w = clamp(max(34, len(title) + 8), 34, w - 6)
        box_h = 3
        y0 = (h - box_h) // 2
        x0 = (w - box_w) // 2
        for yy in range(y0, y0 + box_h):
            self.stdscr.addstr(yy, x0, " " * box_w)
        win = self.stdscr.derwin(box_h, box_w, y0, x0)
        win.keypad(True)
        win.box()
        label = title[:box_w - 4]
        label_len = len(label) + 1
        s = initial
        cur = len(s)
        curses.curs_set(1)
        win.nodelay(False)
        inner_w = max(1, box_w - 4 - label_len)
        input_x = 2 + label_len
        while True:
            view_start = max(0, cur - inner_w + 1) if cur >= inner_w else 0
            display = s[view_start:view_start + inner_w]
            win.addstr(1, 2, label, self.C(4))
            win.addstr(1, 2 + len(label), " ")
            win.addstr(1, input_x, " " * inner_w)
            win.addstr(1, input_x, display)
            win.move(1, input_x + min(cur - view_start, inner_w))
            win.refresh()
            ch = win.getch()
            if ch == 27:
                win.nodelay(True)
                while win.getch() != -1:
                    pass
                win.nodelay(False)
                curses.curs_set(0)
                self.stdscr.nodelay(True)
                return None
            if ch in (10, 13):
                curses.curs_set(0)
                self.stdscr.nodelay(True)
                return s.strip()
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                if cur > 0:
                    s = s[:cur - 1] + s[cur:]
                    cur -= 1
            elif ch == curses.KEY_DC:
                if cur < len(s):
                    s = s[:cur] + s[cur + 1:]
            elif ch in (curses.KEY_LEFT, 2):
                cur = max(0, cur - 1)
            elif ch in (curses.KEY_RIGHT, 6):
                cur = min(len(s), cur + 1)
            elif ch in (curses.KEY_HOME, 1):
                cur = 0
            elif ch in (curses.KEY_END, 5):
                cur = len(s)
            elif ch == 11:
                s = s[:cur]
            elif ch == 21:
                s = ""
                cur = 0
            elif ch == 23:
                i = cur
                while i > 0 and s[i - 1] == " ":
                    i -= 1
                while i > 0 and s[i - 1] != " ":
                    i -= 1
                s = s[:i] + s[cur:]
                cur = i
            elif 32 <= ch <= 1114111:
                try:
                    c = chr(ch)
                    s = s[:cur] + c + s[cur:]
                    cur += 1
                except Exception:
                    pass

    def prompt_yes_no(self, title: str) -> bool:
        h, w = self.stdscr.getmaxyx()
        box_w = clamp(max(30, len(title) + 8), 30, w - 6)
        y0 = (h - 5) // 2
        x0 = (w - box_w) // 2
        for yy in range(y0, y0 + 5):
            self.stdscr.addstr(yy, x0, " " * box_w)
        win = self.stdscr.derwin(5, box_w, y0, x0)
        win.box()
        win.addstr(2, 2, title[:box_w - 4], self.C(4))
        win.refresh()
        while True:
            ch = self.stdscr.getch()
            if ch in (ord("y"), ord("Y")):
                return True
            if ch in (ord("n"), ord("N"), 27):
                return False

    def pick_playlist(self, title: str) -> Optional[str]:
        names = sorted(self.playlists.keys())
        if not names:
            self.toast("No playlists")
            return None
        h, w = self.stdscr.getmaxyx()
        box_w = min(w - 6, 56)
        box_h = min(h - 6, max(10, min(18, len(names) + 4)))
        y0 = (h - box_h) // 2
        x0 = (w - box_w) // 2
        idx = 0
        self.stdscr.nodelay(False)
        result: Optional[str] = None
        try:
            while True:
                for yy in range(y0, y0 + box_h):
                    self.stdscr.addstr(yy, x0, " " * box_w)
                win = self.stdscr.derwin(box_h, box_w, y0, x0)
                win.box()
                win.addstr(0, 2, f" {title} ", self.C(4))
                inner_h = box_h - 2
                scroll = clamp(idx - inner_h // 2, 0, max(0, len(names) - inner_h))
                for i in range(inner_h):
                    j = scroll + i
                    if j >= len(names):
                        break
                    attr = curses.A_REVERSE if j == idx else 0
                    win.addstr(1 + i, 2, names[j][:box_w - 4].ljust(box_w - 4), attr)
                win.refresh()
                ch = self.stdscr.getch()
                if ch == 27:
                    result = None
                    break
                if ch in (10, 13):
                    result = names[idx]
                    break
                if ch in (curses.KEY_DOWN, ord("j")):
                    idx = clamp(idx + 1, 0, len(names) - 1)
                if ch in (curses.KEY_UP, ord("k")):
                    idx = clamp(idx - 1, 0, len(names) - 1)
                if ch in (curses.KEY_HOME, ord("g")):
                    idx = 0
                if ch in (curses.KEY_END, ord("G")):
                    idx = len(names) - 1
        finally:
            self.stdscr.nodelay(True)
        self._need_redraw = True
        self._redraw_status_only = False
        return result
