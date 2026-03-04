from __future__ import annotations
import curses
import subprocess
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple
from ..models import Track, Album
from ..config import (TAB_QUEUE, TAB_SEARCH, TAB_RECOMMENDED, TAB_MIX,
                      TAB_ARTIST, TAB_ALBUM, TAB_LIKED, TAB_PLAYLISTS, TAB_HISTORY,
                      TAB_NAMES, QUALITY_ORDER, AUTOPLAY_NAMES, AUTOPLAY_OFF)
from ..utils import fmt_time, fmt_dur, clamp, year_norm, album_year_from_obj


class UIMixin:
    def _on_mpv_tick(self) -> None:
        snap = self.mp.snapshot()
        vo, mu, pa = snap[3], snap[4], snap[2]
        if vo is not None:
            self.desired_volume = clamp(int(vo), 0, 130)
        if mu is not None:
            self.desired_mute = bool(mu)
        prev_pa = getattr(self, "_prev_mpv_pause", None)
        prev_alive = getattr(self, "_prev_mpv_alive", None)
        cur_alive = self.mp.alive()
        if pa != prev_pa or cur_alive != prev_alive:
            self._prev_mpv_pause = pa
            self._prev_mpv_alive = cur_alive
            self._redraw_status_only = False
        else:
            self._redraw_status_only = True
        self._need_redraw = True

    # ---------------------------------------------------------------------------
    # curses / colors
    # ---------------------------------------------------------------------------
    def _name_to_curses_color(self, name: str) -> int:
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

    def _init_curses(self) -> None:
        curses.curs_set(0)
        self.stdscr.nodelay(True)
        self.stdscr.keypad(True)
        curses.noecho()
        curses.cbreak()
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            s = self.settings
            curses.init_pair(1,  self._name_to_curses_color(s.get("color_playing",   "green")),   -1)
            curses.init_pair(2,  self._name_to_curses_color(s.get("color_paused",    "yellow")),  -1)
            curses.init_pair(3,  self._name_to_curses_color(s.get("color_error",     "red")),     -1)
            curses.init_pair(4,  self._name_to_curses_color(s.get("color_chrome",    "black")),   -1)
            curses.init_pair(5,  self._name_to_curses_color(s.get("color_accent",    "magenta")), -1)
            curses.init_pair(6,  self._name_to_curses_color(s.get("color_accent",    "magenta")), -1)
            curses.init_pair(7,  self._name_to_curses_color(s.get("color_artist",    "white")),   -1)
            curses.init_pair(8,  self._name_to_curses_color(s.get("color_album",     "blue")),    -1)
            curses.init_pair(9,  self._name_to_curses_color(s.get("color_duration",  "black")),   -1)
            curses.init_pair(10, self._name_to_curses_color(s.get("color_numbers",   "black")),   -1)
            curses.init_pair(11, self._name_to_curses_color(s.get("color_title",     "white")),   -1)
            curses.init_pair(12, self._name_to_curses_color(s.get("color_year",      "blue")),    -1)
            curses.init_pair(13, self._name_to_curses_color(s.get("color_separator", "white")),   -1)
            curses.init_pair(14, self._name_to_curses_color(s.get("color_liked",     "white")),   -1)
            curses.init_pair(15, self._name_to_curses_color(s.get("color_mark",      "red")),     -1)
            curses.init_pair(16, curses.COLOR_WHITE, curses.COLOR_BLACK)

    def C(self, pair: int) -> int:
        if self.color_mode and curses.has_colors():
            return curses.color_pair(pair)
        return 0

    # ---------------------------------------------------------------------------
    # misc
    # ---------------------------------------------------------------------------
    def toast(self, msg: str, sec: float = 2.0) -> None:
        self.toast_msg = msg
        self.toast_until = time.time() + sec
        self._need_redraw = True

    def _set_loading(self, key: str) -> None:
        self._loading = True
        self._loading_key = key
        self.last_error = None
        self._need_redraw = True
        self._redraw_status_only = False

    def _clear_loading(self, key: str) -> None:
        if self._loading_key == key:
            self._loading = False
            self._need_redraw = True
            self._redraw_status_only = False

    def is_liked(self, tid: int) -> bool:
        return tid in self.liked_ids

    def web_base(self) -> str:
        u = urllib.parse.urlparse(self.api_base)
        host = u.netloc
        scheme = u.scheme or "https"
        if host.startswith("api."):
            host = host[4:]
        return f"{scheme}://{host}"

    def open_url(self, url: str) -> None:
        try:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.toast("Opened")
        except Exception:
            self.toast("Open failed")

    # ---------------------------------------------------------------------------
    # parsing
    # ---------------------------------------------------------------------------
    def _parse_track_obj(self, obj: Dict[str, Any]) -> Optional[Track]:
        try:
            tid = int(obj.get("id") or obj.get("trackId") or 0)
            if tid <= 0:
                return None
            title = str(obj.get("title") or obj.get("name") or "").strip() or f"Track {tid}"

            artist = "Unknown"
            artist_id: Optional[int] = None
            a = obj.get("artist")
            if isinstance(a, dict):
                artist = str(a.get("name") or artist).strip() or artist
                if str(a.get("id", "")).isdigit():
                    artist_id = int(a["id"])
            if artist == "Unknown":
                arts = obj.get("artists")
                if isinstance(arts, list) and arts and isinstance(arts[0], dict):
                    artist = str(arts[0].get("name") or artist).strip() or artist
                    if str(arts[0].get("id", "")).isdigit():
                        artist_id = int(arts[0]["id"])

            album_obj = obj.get("album") or {}
            album = str(album_obj.get("title") or album_obj.get("name") or obj.get("albumTitle") or "").strip() or "Unknown"
            year = album_year_from_obj(album_obj) if isinstance(album_obj, dict) else "????"
            if year == "????":
                year = album_year_from_obj(obj)

            track_no = int(obj.get("trackNumber") or obj.get("track_no") or obj.get("trackNo") or 0)
            duration: Optional[int] = None
            for k in ("duration", "durationSeconds", "trackDuration"):
                v = obj.get(k)
                if isinstance(v, (int, float)) and v > 0:
                    duration = int(v)
                    break

            album_id: Optional[int] = None
            if isinstance(album_obj, dict) and str(album_obj.get("id", "")).isdigit():
                album_id = int(album_obj["id"])

            return Track(id=tid, title=title, artist=artist, album=album, year=year,
                         track_no=track_no, duration=duration, artist_id=artist_id, album_id=album_id)
        except Exception:
            return None

    def _parse_album_obj(self, obj: Dict[str, Any]) -> Optional[Album]:
        try:
            aid = int(obj.get("id") or 0)
            title = str(obj.get("title") or obj.get("name") or "").strip()
            if not title:
                return None
            artist = "Unknown"
            a = obj.get("artist")
            if isinstance(a, dict):
                artist = str(a.get("name") or artist).strip() or artist
            return Album(id=aid, title=title, artist=artist, year=album_year_from_obj(obj))
        except Exception:
            return None

    def _extract_tracks_from_search(self, payload: Dict[str, Any]) -> List[Track]:
        tracks: List[Track] = []

        def scan_list(lst):
            for it in lst:
                if isinstance(it, dict):
                    x = it.get("item", it) if isinstance(it.get("item"), dict) else it
                    if isinstance(x.get("track"), dict):
                        x = x["track"]
                    t = self._parse_track_obj(x)
                    if t:
                        tracks.append(t)

        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                scan_list(data["items"])
            elif isinstance(payload.get("items"), list):
                scan_list(payload["items"])

        seen = set()
        out: List[Track] = []
        for t in tracks:
            if t.id not in seen:
                seen.add(t.id)
                out.append(t)
        return out

    def _looks_like_track_dict(self, d: Dict[str, Any]) -> bool:
        if "id" not in d or not (("title" in d) or ("name" in d)):
            return False
        return any(k in d for k in ("trackNumber", "trackNo", "duration", "durationSeconds",
                                    "trackDuration", "isrc", "artists", "artist"))

    def _scan_for_track_dicts(self, root: Any, out: List[Dict[str, Any]], limit: int = 2500) -> None:
        if len(out) >= limit:
            return
        if isinstance(root, dict):
            if self._looks_like_track_dict(root):
                out.append(root)
                return
            for v in root.values():
                self._scan_for_track_dicts(v, out, limit)
                if len(out) >= limit:
                    return
        elif isinstance(root, list):
            for v in root:
                self._scan_for_track_dicts(v, out, limit)
                if len(out) >= limit:
                    return

    def _extract_tracks_from_album_payload(self, payload: Dict[str, Any]) -> List[Track]:
        candidates: List[Any] = []

        def get_path(root, path):
            cur = root
            for p in path:
                if isinstance(cur, dict) and p in cur:
                    cur = cur[p]
                else:
                    return None
            return cur

        for path in (("data", "tracks", "items"), ("data", "tracks"), ("data", "items"),
                     ("tracks", "items"), ("tracks",), ("items",)):
            cur = get_path(payload, path)
            if isinstance(cur, list) and cur:
                candidates = cur
                break

        tracks: List[Track] = []
        if candidates:
            for it in candidates:
                if isinstance(it, dict):
                    x = it.get("item", it) if isinstance(it.get("item"), dict) else it
                    if isinstance(x.get("track"), dict):
                        x = x["track"]
                    t = self._parse_track_obj(x)
                    if t:
                        tracks.append(t)

        if not tracks:
            dicts: List[Dict[str, Any]] = []
            self._scan_for_track_dicts(payload, dicts, limit=2500)
            for d in dicts:
                t = self._parse_track_obj(d)
                if t:
                    tracks.append(t)

        seen = set()
        out: List[Track] = []
        for t in tracks:
            if t.id not in seen:
                seen.add(t.id)
                out.append(t)
        out.sort(key=lambda t: (t.track_no if t.track_no > 0 else 10_000, t.title.lower()))
        return out

    # ---------------------------------------------------------------------------
    # metadata helpers
    # ---------------------------------------------------------------------------
    def _track_year(self, t: Track) -> str:
        y = year_norm(t.year)
        return y if y != "????" else self.meta.year.get(t.id, "????")

    def _track_duration(self, t: Track) -> Optional[int]:
        return t.duration or self.meta.duration.get(t.id)

    # ---------------------------------------------------------------------------
    # formatting
    # ---------------------------------------------------------------------------
    def _make_track_parts(self, t: Track) -> Tuple[str, str, str, str]:
        yv = self._track_year(t) if self.show_track_year else "????"
        album_year = ""
        if self.tab_align:
            album_year = t.album if self.show_track_album else ""
            year_part = yv if (self.show_track_year and yv != "????") else ""
            dur = ""
            if self.show_track_duration:
                dv = self._track_duration(t)
                if dv:
                    dur = fmt_dur(dv)
            return (t.artist, t.title, album_year, dur, year_part)  # type: ignore[return-value]
        else:
            if self.show_track_album and t.album:
                album_year = f"({t.album}, {yv})" if (self.show_track_year and yv != "????") else f"({t.album})"
            elif self.show_track_year and yv != "????":
                album_year = f"({yv})"
            dur = ""
            if self.show_track_duration:
                dv = self._track_duration(t)
                if dv:
                    dur = f"[{fmt_dur(dv)}]"
            return (t.artist, t.title, album_year, dur)  # type: ignore[return-value]

    def fmt_track_line_bw(self, t: Track, width: int, liked: bool) -> str:
        parts = self._make_track_parts(t)
        a, title, album_year, dur = parts[0], parts[1], parts[2], parts[3]
        head = "♥ " if liked else ""
        if self.tab_align:
            year_part = parts[4] if len(parts) > 4 else ""
            bits = [f"{head}{a}", title]
            if self.show_track_album:
                bits.append(album_year)
            if self.show_track_year and year_part:
                bits.append(year_part)
            if dur:
                bits.append(dur)
            s = "\t".join(bits)
        else:
            bits = [f"{head}{a} - {title}"]
            if album_year:
                bits.append(album_year)
            if dur:
                bits.append(dur)
            s = " ".join(bits)
        return s if len(s) <= width else (s[:max(0, width - 1)] + "…")

    def fmt_track_status(self, t: Track, width: int) -> str:
        yv = self._track_year(t)
        s = f"{t.artist} - {t.title} • {t.album}" + (f" • {yv}" if yv != "????" else "")
        return s if len(s) <= width else (s[:max(0, width - 1)] + "…")

    # ---------------------------------------------------------------------------
    # drawing
    # ---------------------------------------------------------------------------
    def _status_color_pair(self, paused: Optional[bool], alive: bool) -> int:
        if not self.color_mode or not curses.has_colors():
            return 0
        if self.last_error:
            return curses.color_pair(3)
        if paused:
            return curses.color_pair(2)
        if alive:
            return curses.color_pair(1)
        return curses.color_pair(4)

    def _draw_tabs(self, y: int, x: int, w: int) -> None:
        order = (1, 2, 3, 4, 5, 6, 7, 8, 9)
        s = "  ".join([TAB_NAMES[i] for i in order])[:max(0, w - 1)]
        self.stdscr.addstr(y, x, s, self.C(4))
        cur = TAB_NAMES.get(self.tab, "")
        idx = s.find(cur)
        if idx >= 0:
            self.stdscr.addstr(y + 1, x + idx, "─" * min(len(cur), max(0, w - idx - 1)), self.C(5) or curses.A_BOLD)

    def _draw_line_no(self, y: int, x: int, idx1: int, width: int) -> int:
        if not self.show_numbers or width < 5:
            return 0
        s = f"{idx1:>4} "
        self.stdscr.addstr(y, x, s[:width], self.C(10))
        return len(s)

    def _draw_track_line_colored(self, y: int, x: int, w: int, t: Track, selected: bool,
                                  marked: bool, priority_pos: int = 0,
                                  force_no_tsv: bool = False, simple_format: bool = False) -> None:
        base_attr = curses.A_REVERSE if selected else 0
        liked = self.is_liked(t.id)
        use_tsv = self.tab_align and not force_no_tsv and not simple_format

        prio_str = str(priority_pos) if priority_pos > 0 else ""
        n_digits = len(prio_str)
        pref_w = 3 if n_digits <= 2 else (1 + n_digits + 1)
        if w <= pref_w:
            return
        self.stdscr.addstr(y, x, " " * pref_w, base_attr)
        if marked:
            self.stdscr.addstr(y, x, "+", base_attr | self.C(15))
        if priority_pos > 0:
            self.stdscr.addstr(y, x + 1, prio_str[:pref_w - 1], base_attr | self.C(5))
        x += pref_w
        w -= pref_w

        heart_w = 2
        if liked:
            self.stdscr.addstr(y, x, ("♥ ")[:max(0, w)], base_attr | self.C(14))
        elif use_tsv:
            self.stdscr.addstr(y, x, "  "[:max(0, w)], base_attr)
        if liked or use_tsv:
            x += heart_w
            w -= heart_w
            if w <= 0:
                return

        if simple_format:
            artist = t.artist
            title = t.title
            dv = self._track_duration(t)
            dur = f" [{fmt_dur(dv)}]" if dv else ""
            segs: List[Tuple[str, int]] = [
                (artist, 7), (" - ", 13), (title, 11),
            ]
            if dur:
                segs.append((dur, 9))
            rem = max(0, w - 1)
            cx = x
            for text, pair in segs:
                if rem <= 0 or not text:
                    continue
                text = text[:rem]
                self.stdscr.addstr(y, cx, text, base_attr | (self.C(pair) if pair else 0))
                cx += len(text)
                rem -= len(text)
            if rem > 0:
                self.stdscr.addstr(y, cx, " " * rem, base_attr)
            return

        parts = self._make_track_parts(t)
        artist, title, album_or_combined, dur = parts[0], parts[1], parts[2], parts[3]

        if use_tsv:
            year_part = parts[4] if len(parts) > 4 else ""
            field_defs: List[Tuple[str, int, str]] = [(artist, 7, "artist"), (title, 11, "title")]
            if self.show_track_album and album_or_combined:
                field_defs.append((album_or_combined, 8, "album"))
            if self.show_track_year and year_part:
                field_defs.append((year_part, 12, "year"))
            if dur:
                field_defs.append((dur, 9, "duration"))
            n_fields = len(field_defs)
            if n_fields == 0:
                return
            cx = x
            rem = w
            for fi, (text, pair, fkey) in enumerate(field_defs):
                if rem <= 0:
                    break
                is_last = (fi == n_fields - 1)
                if is_last:
                    fw = rem
                    display = text[:fw].ljust(fw)[:fw]
                else:
                    mcw = self.tsv_field_widths.get(fkey, self.tsv_max_col_width)
                    if mcw <= 0:
                        mcw = w
                    fw = min(mcw, rem)
                    if len(text) >= fw:
                        display = (text[:max(0, fw - 2)] + "…") if fw > 1 else "…"
                        display = display.ljust(fw)[:fw]
                    else:
                        display = text.ljust(fw)[:fw]
                self.stdscr.addstr(y, cx, display, base_attr | self.C(pair))
                cx += fw
                rem -= fw
        else:
            segs = [
                (artist, 7),
                (" - ", 13),
                (title, 11),
            ]
            if album_or_combined:
                segs += [(" ", 0), (album_or_combined, 8)]
            if dur:
                segs += [(" ", 0), (dur, 9)]
            rem = max(0, w - 1)
            cx = x
            for text, pair in segs:
                if rem <= 0 or not text:
                    continue
                text = text[:rem]
                self.stdscr.addstr(y, cx, text, base_attr | (self.C(pair) if pair else 0))
                cx += len(text)
                rem -= len(text)
            if rem > 0:
                self.stdscr.addstr(y, cx, " " * rem, base_attr)

    def _draw_track_line(self, y: int, x: int, w: int, t: Track, selected: bool,
                         marked: bool, idx1: Optional[int], priority_pos: int = 0,
                         force_no_tsv: bool = False, simple_format: bool = False) -> None:
        base_attr = curses.A_REVERSE if selected else 0

        offs = self._draw_line_no(y, x, idx1 or 0, w) if idx1 is not None else 0
        x += offs
        w -= offs
        if w <= 0:
            return

        if self.color_mode:
            self._draw_track_line_colored(y, x, w, t, selected, marked, priority_pos,
                                           force_no_tsv=force_no_tsv, simple_format=simple_format)
        else:
            liked = self.is_liked(t.id)
            use_tsv = self.tab_align and not force_no_tsv and not simple_format
            prio_str = str(priority_pos) if priority_pos > 0 else ""
            n_digits = len(prio_str)
            pref_w = 3 if n_digits <= 2 else (1 + n_digits + 1)
            if priority_pos > 0 and marked:
                pref = ("+" + prio_str).ljust(pref_w)
            elif priority_pos > 0:
                pref = (" " + prio_str).ljust(pref_w)
            elif marked:
                pref = "+  "
            else:
                pref = "   "
            pref = pref[:pref_w].ljust(pref_w)
            self.stdscr.addstr(y, x, pref[:max(0, w - 1)], base_attr)
            x += pref_w
            w -= pref_w
            if w <= 0:
                return
            if simple_format:
                dv = self._track_duration(t)
                dur_s = f" [{fmt_dur(dv)}]" if dv else ""
                head = "♥ " if liked else ""
                line = f"{head}{t.artist} - {t.title}{dur_s}"
                self.stdscr.addstr(y, x, line.ljust(max(0, w - 1))[:max(0, w - 1)], base_attr)
                return
            if use_tsv:
                if liked:
                    self.stdscr.addstr(y, x, "♥ "[:max(0, w - 1)], base_attr)
                else:
                    self.stdscr.addstr(y, x, "  "[:max(0, w - 1)], base_attr)
                x += 2
                w -= 2
                if w <= 0:
                    return
                parts = self._make_track_parts(t)
                artist, title = parts[0], parts[1]
                album_or_combined = parts[2]
                dur = parts[3]
                year_part = parts[4] if len(parts) > 4 else ""
                field_defs_bw = [(artist, "artist"), (title, "title")]
                if self.show_track_album and album_or_combined:
                    field_defs_bw.append((album_or_combined, "album"))
                if self.show_track_year and year_part:
                    field_defs_bw.append((year_part, "year"))
                if dur:
                    field_defs_bw.append((dur, "duration"))
                n_fields = len(field_defs_bw)
                cx = x
                rem = w
                for fi, (text, fkey) in enumerate(field_defs_bw):
                    if rem <= 0:
                        break
                    is_last = (fi == n_fields - 1)
                    if is_last:
                        fw = rem
                        display = text[:fw].ljust(fw)[:fw]
                    else:
                        mcw = self.tsv_field_widths.get(fkey, self.tsv_max_col_width)
                        if mcw <= 0:
                            mcw = w
                        fw = min(mcw, rem)
                        if len(text) >= fw:
                            display = (text[:max(0, fw - 2)] + "…") if fw > 1 else "…"
                            display = display.ljust(fw)[:fw]
                        else:
                            display = text.ljust(fw)[:fw]
                    self.stdscr.addstr(y, cx, display, base_attr)
                    cx += fw
                    rem -= fw
            else:
                head = "♥ " if liked else ""
                line = self.fmt_track_line_bw(t, w - 1, liked=liked)
                self.stdscr.addstr(y, x, line.ljust(max(0, w - 1))[:max(0, w - 1)], base_attr)

    def _draw_left(self, y: int, x: int, h: int, w: int) -> None:
        typ, items = self._left_items()
        if h <= 0 or w <= 0:
            return
        if self._loading:
            self.stdscr.addstr(y, x, "Loading…", self.C(4))
            return

        n = len(items)
        if typ == "queue_tab":
            self.queue_cursor = clamp(self.queue_cursor, 0, max(0, n - 1))
            active_idx = self.queue_cursor
        else:
            self.left_idx = clamp(self.left_idx, 0, max(0, n - 1))
            active_idx = self.left_idx
        if active_idx < self.left_scroll:
            self.left_scroll = active_idx
        if active_idx >= self.left_scroll + h:
            self.left_scroll = active_idx - h + 1
        self.left_scroll = clamp(self.left_scroll, 0, max(0, n - h))

        if self.show_track_year or self.show_track_duration:
            for i in range(self.left_scroll, min(n, self.left_scroll + min(h, 14))):
                it = items[i]
                if isinstance(it, Track):
                    if (self.show_track_year and year_norm(it.year) == "????") or (self.show_track_duration and not it.duration):
                        self.meta.want(it.id)

        pq_set = {qi: pos+1 for pos, qi in enumerate(self.priority_queue)}

        # Empty-tab hints
        if n == 0 and self.tab == TAB_QUEUE:
            self.stdscr.addstr(y, x, " Press e/E on tracks in any tab to enqueue, q to show queue overlay"[:max(0, w - 1)].ljust(max(0, w - 1)), self.C(10))
            return
        if typ == "tracks" and n == 0 and self.tab == TAB_SEARCH:
            self.stdscr.addstr(y, x, " Search term with /"[:max(0, w - 1)].ljust(max(0, w - 1)), self.C(10))
            return
        if typ == "tracks" and n == 0 and self.tab == TAB_RECOMMENDED:
            hint = (
                " A playing track is required to get recommendations\n"
                "\n"
                " If Autoplay is set to recommended, the queue will expand with recommended suggestions\n"
                " based on last queue items"
            )
            for i, line in enumerate(hint.splitlines()):
                if y + i < h - 1:
                    self.stdscr.addstr(
                        y + i, x,
                        line[:max(0, w - 1)].ljust(max(0, w - 1)),
                        self.C(10),
                    )
            return

        if n == 0 and self.tab == TAB_MIX:
            hint = (
                " Press 4 on a track in any tab to load its track mix\n"
                "\n"
                " If Autoplay is set to mix, the queue will expand with mix suggestions\n"
                " based on last queue items"
            )
            if self.mix_track:
                hint = " No mix tracks loaded — press 4 with a track selected"

            for i, line in enumerate(hint.splitlines()):
                if y + i < h - 1:
                    self.stdscr.addstr(
                        y + i, x,
                        line[:max(0, w - 1)].ljust(max(0, w - 1)),
                        self.C(10),
                    )
            return
        if  n == 0 and self.tab == TAB_ARTIST:
            self.stdscr.addstr(y, x, " Press 5 on a track in any tab to show its artist"[:max(0, w - 1)].ljust(max(0, w - 1)), self.C(10))
            return
        if  n == 0 and self.tab == TAB_ALBUM:
            self.stdscr.addstr(y, x, " Press 6 on a track in any tab to show its album"[:max(0, w - 1)].ljust(max(0, w - 1)), self.C(10))
            return
        if typ == "tracks" and n == 0 and self.tab == TAB_LIKED:
            self.stdscr.addstr(y, x, " Press l/L on tracks in any tab to like them"[:max(0, w - 1)].ljust(max(0, w - 1)), self.C(10))
            return
        if typ == "tracks" and n == 0 and self.tab == TAB_HISTORY:
            self.stdscr.addstr(y, x, " Play tracks to build history"[:max(0, w - 1)].ljust(max(0, w - 1)), self.C(10))
            return
        if  n == 0 and self.tab == TAB_PLAYLISTS:
            self.stdscr.addstr(y, x, " Press n to create a new playlist"[:max(0, w - 1)].ljust(max(0, w - 1)), self.C(10))
            return

        for row in range(h):
            i = self.left_scroll + row
            yy = y + row
            if i >= n:
                break
            it = items[i]
            if typ == "queue_tab":
                selected = (i == self.queue_cursor)
            else:
                selected = (not self._queue_context() and i == self.left_idx)

            if typ in ("queue_tab", "tracks") and isinstance(it, Track):
                marked = (i in (self.marked_queue_idx if typ == "queue_tab" else self.marked_left_idx))
                ppos = pq_set.get(i, 0) if typ == "queue_tab" else 0
                if typ == "queue_tab":
                    alive2 = self.mp.alive()
                    _, _, pa2, _, _ = self.mp.snapshot()
                    ct = self.current_track
                    playing = alive2 and ct is not None and ct.id == it.id and i == self.queue_play_idx
                    base_attr2 = curses.A_REVERSE if selected else 0
                    if playing and not pa2:
                        self.stdscr.addstr(yy, x, " ", base_attr2)
                        self.stdscr.addstr(yy, x + 1, "▶", base_attr2 | self.C(1))
                    elif playing and pa2:
                        self.stdscr.addstr(yy, x, " ", base_attr2)
                        self.stdscr.addstr(yy, x + 1, "⏸", base_attr2 | self.C(2))
                    else:
                        self.stdscr.addstr(yy, x, "  ", base_attr2)
                    self._draw_track_line(yy, x + 2, max(0, w - 2), it, selected=selected,
                                         marked=marked, idx1=i + 1, priority_pos=ppos)
                else:
                    self._draw_track_line(yy, x, w, it, selected=selected, marked=marked, idx1=i + 1, priority_pos=ppos)
                continue

            if typ == "artist_mixed":
                if isinstance(it, tuple) and it[0] == "sep":
                    extra = "──" if self.tab_align else ""
                    line = f"{extra}── {it[1]} ──"
                    offs = self._draw_line_no(yy, x, i + 1, w) if self.show_numbers else 0
                    self.stdscr.addstr(yy, x + offs, line[:max(0, w - offs - 1)].ljust(max(0, w - offs - 1)),
                                       self.C(4))
                    continue
                if isinstance(it, Album):
                    yv = year_norm(it.year)
                    ys = f", {yv}" if (self.show_track_year and yv != "????") else ""
                    indent = "     " if self.tab_align else "   "
                    line = f"{indent}{it.title}{ys}"
                    offs = self._draw_line_no(yy, x, i + 1, w) if self.show_numbers else 0
                    self.stdscr.addstr(yy, x + offs, line[:max(0, w - offs - 1)].ljust(max(0, w - offs - 1)),
                                       (curses.A_REVERSE if selected else 0) | self.C(8))
                    continue
                if isinstance(it, Track):
                    marked = (i in self.marked_left_idx)
                    self._draw_track_line(yy, x, w, it, selected=selected, marked=marked, idx1=i + 1)
                    continue

            if typ == "album_mixed":
                if isinstance(it, tuple) and it[0] == "album_title" and isinstance(it[1], Album):
                    a = it[1]
                    yv = year_norm(a.year)
                    ys = f", {yv}" if (self.show_track_year and yv != "????") else ""
                    line = f"  {a.artist} — {a.title}{ys}"
                    offs = self._draw_line_no(yy, x, i + 1, w) if self.show_numbers else 0
                    self.stdscr.addstr(yy, x + offs, line[:max(0, w - offs - 1)].ljust(max(0, w - offs - 1)),
                                       (curses.A_REVERSE if selected else 0) | self.C(8))
                    continue
                if isinstance(it, Track):
                    marked = (i in self.marked_left_idx)
                    self._draw_track_line(yy, x, w, it, selected=selected, marked=marked, idx1=i + 1)
                    continue

            if typ == "playlists":
                offs = self._draw_line_no(yy, x, i + 1, w) if self.show_numbers else 0
                count = len(self.playlists.get(str(it), []))
                display = f"{it} ({count} tracks)" if count else str(it)
                self.stdscr.addstr(yy, x + offs, display[:max(0, w - offs - 1)].ljust(max(0, w - offs - 1)),
                                   curses.A_REVERSE if selected else 0)
                continue

            offs = self._draw_line_no(yy, x, i + 1, w) if self.show_numbers else 0
            self.stdscr.addstr(yy, x + offs, str(it)[:max(0, w - offs - 1)].ljust(max(0, w - offs - 1)),
                               curses.A_REVERSE if selected else 0)

    def _queue_title(self) -> str:
        if not self.queue_items:
            return " Queue 0/0"
        pq_info = f" [+{len(self.priority_queue)}]" if self.priority_queue else ""
        return f" Queue {clamp(self.queue_play_idx + 1, 1, len(self.queue_items))}/{len(self.queue_items)}{pq_info}"

    def _draw_queue(self, y: int, x: int, h: int, w: int) -> None:
        if h <= 1 or w <= 0:
            return
        total_h = h
        scr_h, scr_w = self.stdscr.getmaxyx()
        if x > 0 and x < scr_w:
            for yy in range(y, y + total_h):
                try:
                    self.stdscr.addch(yy, x, "│", self.C(4))
                except Exception:
                    pass
        x += 1
        w -= 1
        if w <= 0:
            return
        self.stdscr.addstr(y, x, self._queue_title()[:max(0, w - 1)].ljust(max(0, w - 1)), self.C(4))
        y += 1
        h -= 1
        if not self.queue_items:
            return

        self.queue_cursor = clamp(self.queue_cursor, 0, len(self.queue_items) - 1)
        q_scroll = max(0, self.queue_cursor - h + 1) if self.queue_cursor >= h else 0

        for i in range(q_scroll, min(len(self.queue_items), q_scroll + min(h, 14))):
            t = self.queue_items[i]
            if not t.duration:
                self.meta.want(t.id)

        tp, du, pa, vo, mu = self.mp.snapshot()
        alive = self.mp.alive()
        pq_set = {qi: pos+1 for pos, qi in enumerate(self.priority_queue)}

        for row in range(h):
            i = q_scroll + row
            yy = y + row
            if i >= len(self.queue_items):
                break
            t = self.queue_items[i]
            selected = (self._queue_context() and i == self.queue_cursor)
            base_attr = curses.A_REVERSE if selected else 0
            ct = self.current_track
            playing = alive and ct is not None and ct.id == t.id and i == self.queue_play_idx
            if playing and not pa:
                pfx_sym = "▶"
                pfx_color = self.C(1)
            elif playing and pa:
                pfx_sym = "⏸"
                pfx_color = self.C(2)
            else:
                pfx_sym = ""
                pfx_color = 0

            offs = self._draw_line_no(yy, x, i + 1, w) if self.show_numbers else 0
            px = x + offs
            pw = w - offs
            if pw <= 0:
                continue
            self.stdscr.addstr(yy, px, " ", base_attr)
            if pw > 1:
                sym = pfx_sym if pfx_sym else " "
                self.stdscr.addstr(yy, px + 1, sym, base_attr | pfx_color)
            marked = (i in self.marked_queue_idx)
            ppos = pq_set.get(i, 0)
            self._draw_track_line(yy, px + 2, max(0, pw - 2), t, selected=selected,
                                  marked=marked, idx1=None, priority_pos=ppos,
                                  force_no_tsv=True, simple_format=True)

    def _draw_status(self, y: int, x: int, w: int) -> None:
        tp, du, pa, vo, mu = self.mp.snapshot()
        alive = self.mp.alive()

        if self.show_toggles:
            parts = []
            if self.repeat_mode == 1:
                parts.append("repeat: all")
            elif self.repeat_mode == 2:
                parts.append("repeat: one")
            if self.shuffle_on:
                parts.append("shuffle")
            if self.autoplay != AUTOPLAY_OFF:
                parts.append(f"autoplay: {AUTOPLAY_NAMES[self.autoplay]}")
            parts.append(QUALITY_ORDER[self.quality_idx].lower())
            cur_vol = int(vo) if vo is not None else self.desired_volume
            parts.append(f"vol: {cur_vol}")
            if mu if mu is not None else self.desired_mute:
                parts.append("muted")
            if self.tab_align:
                parts.append("tsv")
            if self.priority_queue:
                parts.append(f"pq: {len(self.priority_queue)}")
            if self.filter_q:
                parts.append(f"filter: {self.filter_q}")
            # Show buffer size when autoplay is active
            with self._autoplay_lock:
                buf_n = len(self._autoplay_buffer)
                fetching = self._autoplay_prefetch_running
            if self.autoplay != AUTOPLAY_OFF and (buf_n > 0 or fetching):
                parts.append(f"buffer: {'…' if fetching else buf_n}")
            line1 = " " + "   ".join(parts)
            self.stdscr.addstr(y, x, line1[:max(0, w - 1)].ljust(max(0, w - 1)), curses.A_DIM if self.color_mode else 0)
        else:
            self.stdscr.addstr(y, x, " " * max(0, w - 1))

        state = "⏹"
        if alive:
            state = "⏸" if pa else "▶"
        left = f" {state} {fmt_time(tp)}/{fmt_time(du)} "
        song = self.fmt_track_status(self.current_track, 10_000) if self.current_track else ""
        if self.last_error:
            song = f"ERROR: {self.last_error}"
        line2 = (left + song)[:max(0, w - 1)].ljust(max(0, w - 1))

        now = time.time()
        if not self.dl.active and self.dl.progress_clear_at and now > self.dl.progress_clear_at:
            self.dl.progress_line = ""
            self.dl.progress_clear_at = 0.0
        right = ""
        if self.dl.progress_line:
            right = self.dl.progress_line
        elif self.dl.error:
            right = "DLERR"
        elif now < self.toast_until and self.toast_msg:
            right = self.toast_msg
        if right:
            right = right[:max(0, w - 2)]
            tpos = max(0, (w - 1) - len(right))
            line2 = line2[:tpos] + right + line2[tpos + len(right):]

        self.stdscr.addstr(y + 1, x, line2, self._status_color_pair(pa, alive))

    def _draw_overlay_box(self, title: str, lines: List[str], scroll: int, box_w: int, box_h: int) -> None:
        h, w = self.stdscr.getmaxyx()
        box_w = min(w - 6, box_w)
        box_h = min(h - 6, box_h)
        y0 = (h - box_h) // 2
        x0 = (w - box_w) // 2
        for yy in range(y0, y0 + box_h):
            self.stdscr.addstr(yy, x0, " " * box_w)
        win = self.stdscr.derwin(box_h, box_w, y0, x0)
        win.box()
        win.addstr(0, 2, f" {title} ", self.C(4))
        inner_h = box_h - 2
        start = clamp(scroll, 0, max(0, len(lines) - inner_h))
        for i in range(inner_h):
            idx = start + i
            if idx >= len(lines):
                break
            line = lines[idx]
            if line.startswith("\x01"):
                text = line[1:][:box_w - 4].ljust(box_w - 4)
                win.addstr(1 + i, 2, text, curses.color_pair(16))
            else:
                win.addstr(1 + i, 2, line[:box_w - 4])
        win.refresh()

    def _draw_help(self) -> None:
        lines = [
            "",
            "\x01 TABS",
            " 1 Queue  2 Search  3 Recommended  4 Mix  5 Artist  6 Album  7 Liked  8 Playlists  9 History",
            "",
            "\x01 PLAYBACK                                           PLAYLISTS (8)",
            " p         play/pause                               n     new list",
            " m         mute                                     d     delete list",
            " -/+       volume                                   Enter open list",
            " ←/→       seek 5s                                  Bkspc back to lists",
            " Shift ←/→ seek 30s                                 e/E   enqueue to end/after current",
            " ,/<       prev track                               a     add selected/marked to playlist",
            " ./>       next track                               X     remove selected from playlist",
            " t         resume playback from last know position",
            "",
            "\x01 ACTIONS",
            " Enter     play without adding to queue",
            " e/E       enqueue to end/after current",
            " b         flag to play next (set priority N)",
            " B         clear priority flags",
            " 4         show mix based on selected track",
            " 5/6       show tracks from artist/album of selected track",
            " D         download (selected, marked, album, all tracks of a playlist)",
            " Space     mark/unmark selected and advance",
            " U/u       mark/unmark all",
            " l         like/unlike selected or marked",
            " L         like/unlike playing",
            " g/G       move cursor to top/bottom",
            " j/k/↓/↑   move cursor down/up",
            " J/K       move track or marked tracks down/up",
            " x         remove track from queue/playlist",
            " X         clear queue",
            " z         jump to currently playing",
            " i/I       show selected/playing info",
            " v/V       show lyrics of selected/playing",
            " o/O       open selected/playing in browser",
            "",
            "\x01 GENERAL",
            " /         search online",
            " f         filter term in current view",
            " (/)       prev/next filter hit",
            " Esc       close search/filter prompt",
            " Q         quit",
            "",
            "\x01 VIEW",
            " q         mini-queue overlay",
            " Tab       move cursor between main tabs and mini-queue overlay"
            " c         color/bw",
            " w/y/d/n   album/year/duration/line number fields",
            " T         status bar",
            " \\         toggle TSV mode",
            " z (queue) jump to playing",
            "",
            "\x01 TOGGLES",
            " A         autoplay mode (off, mix, recommended)",
            " R         repeat mode (off, all, one)",
            " S         shuffle (off, on)",
            " F         media quality",
            "",
            "\x01 AUTOPLAY MODES",
            " off:         no automatic queue extension",
            " mix:         refill queue from the current track mix",
            " recommended: refill queue from track recommendations",
            " Refill candidates are picked based on suggestions (mix or recommended) pooled from",
            " recent play history + upcoming queue tracks",
            "",
            "\x01 SETTINGS (settings.json)",
            " Autoplay:",
            " autoplay_n:  tracks to add per autoplay refill (default 3)",
            "",
            " History tab:",
            " history_max: max history entries to keep (default 0 = unlimited)",
            "",
            " Download file hierarchy:",
            " download_dir (Linux default /tmp/tuifi/)",
            " download_structure (default {artist}/{artist} - {album} ({year}))",
            " download_filename (default {track:02d}. {artist} - {title})",
            "",
            " Colors:",
            " color_playing  color_paused  color_error  color_chrome  color_accent",
            " color_artist   color_title   color_album  color_year    color_separator",
            " color_duration color_numbers color_liked  color_mark",
            " values: black red green yellow blue magenta cyan white (or 0-255)",
            "",
            " TSV fields (general or per field overrides):",
            " tsv_max_col_width: default max column width in TSV mode (default 32, 0=unlimited)",
            " tsv_max_artist_width     tsv_max_title_width     tsv_max_album_width",
            " tsv_max_year_width       tsv_max_duration_width",
            "",
        ]
        box_h = 38
        h, _ = self.stdscr.getmaxyx()
        inner_h = min(h - 8, box_h - 2)
        self._help_max_scroll = max(0, len(lines) - inner_h)
        self._draw_overlay_box("Help  (? or q to close)", lines, self.help_scroll, box_w=96, box_h=box_h)

    def _draw_info(self) -> None:
        if self.info_album and not self.info_track:
            a = self.info_album
            lines: List[str] = [
                f"Album  : {a.title}",
                f"Artist : {a.artist}",
                f"Year   : {year_norm(a.year)}",
                f"ID     : {a.id}" if a.id else "",
                "",
            ]
            if self.info_loading:
                lines.append("Loading album info…")
            else:
                payload = self.info_payload or {}
                if "error" in payload:
                    lines.append(f"Error: {payload.get('error')}")
                else:
                    data = payload.get("data") if isinstance(payload, dict) else payload
                    if isinstance(data, dict):
                        for k in ("numberOfTracks", "numberOfVolumes", "releaseDate",
                                  "audioQuality", "explicit", "upc", "popularity"):
                            if k in data:
                                lines.append(f"{k}: {data[k]}")
            lines = [l for l in lines if l is not None]
            self._draw_overlay_box("Album info", lines, self.info_scroll, box_w=76, box_h=16)
            return

        t = self.info_track
        if not t:
            self._draw_overlay_box("Info", ["(no selection)"], 0, box_w=72, box_h=10)
            return
        lines = [
            f"Title   : {t.title}",
            f"Artist  : {t.artist}" + (f" (id {t.artist_id})" if t.artist_id else ""),
            f"Album   : {t.album}" + (f" (id {t.album_id})" if t.album_id else ""),
        ]
        yv = self._track_year(t)
        if yv != "????":
            lines.append(f"Year    : {yv}")
        dv = self._track_duration(t)
        if dv:
            lines.append(f"Duration: {fmt_dur(dv)}")
        lines += [f"Track id: {t.id}", f"Liked   : {'yes' if self.is_liked(t.id) else 'no'}", ""]
        if self.info_loading:
            lines.append("Loading /info …")
        else:
            payload = self.info_payload or {}
            if "error" in payload:
                lines.append(f"Error: {payload.get('error')}")
            else:
                data = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(data, dict):
                    for k in ("audioQuality", "explicit", "popularity", "streamReady"):
                        if k in data:
                            lines.append(f"{k}: {data.get(k)}")
        self._draw_overlay_box("Track info", lines, self.info_scroll, box_w=76, box_h=16)

    def _draw_lyrics(self) -> None:
        t_id = self.lyrics_track_id
        title = "Lyrics"
        t_ref = self.lyrics_track
        if t_ref is None and self.current_track and self.current_track.id == t_id:
            t_ref = self.current_track
        if t_ref:
            title = f"Lyrics – {t_ref.artist} - {t_ref.title}"
        if self.lyrics_loading:
            lines = ["Loading lyrics…"]
        else:
            lines = self.lyrics_lines or ["(empty)"]
        self._draw_overlay_box(title[:80], lines, self.lyrics_scroll, box_w=84, box_h=30)

    def draw(self) -> None:
        if not self._need_redraw:
            return

        h, w = self.stdscr.getmaxyx()
        top_h = 2
        status_h = 2
        usable_h = h - top_h - status_h

        if self._redraw_status_only and not self.show_help and not self.info_overlay and not self.lyrics_overlay:
            self._draw_status(h - status_h, 0, w)
            self.stdscr.refresh()
            self._need_redraw = False
            self._redraw_status_only = False
            return

        self._redraw_status_only = False
        self._need_redraw = False
        self.stdscr.erase()

        left_w = w if not self.queue_overlay else max(20, w - 44)

        self._draw_tabs(0, 0, w)
        self._draw_left(top_h, 0, usable_h, left_w)

        if self.queue_overlay:
            self._draw_queue(top_h, left_w, usable_h, w - left_w)

        self._draw_status(h - status_h, 0, w)

        if self.show_help:
            self._draw_help()
        if self.info_overlay:
            self._draw_info()
        if self.lyrics_overlay:
            self._draw_lyrics()

        self.stdscr.refresh()
