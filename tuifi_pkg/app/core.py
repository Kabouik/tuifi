from __future__ import annotations
import curses
import os
import random
import threading
import time
from typing import Any, Dict, List, Optional
from ..models import Track, Album
from ..config import (STATE_DIR, QUALITY_ORDER, AUTOPLAY_OFF, AUTOPLAY_MIX, AUTOPLAY_RECOMMENDED,
                      AUTOPLAY_NAMES, TAB_QUEUE, TAB_SEARCH, TAB_RECOMMENDED, TAB_MIX,
                      TAB_ARTIST, TAB_ALBUM, TAB_LIKED, TAB_PLAYLISTS, TAB_HISTORY,
                      TAB_NAMES, _default_downloads_dir)
from ..api import HiFiClient
from ..player import MPV, MPVPoller, DownloadManager, MetaFetcher
from ..persistence import (load_settings, save_settings, load_queue, save_queue,
                            load_liked, save_liked, load_playlists, save_playlists,
                            load_history, save_history)
from ..utils import debug_log, mkdirp, clamp
import tuifi_pkg.config as _config_mod
import tuifi_pkg.utils as _utils_mod


class CoreMixin:
    def __init__(self, stdscr, api_base: str, args: Dict[str, Any]) -> None:
        self.stdscr = stdscr
        self.api_base = api_base.rstrip("/")
        self.client = HiFiClient(self.api_base)
        self.meta = MetaFetcher(self.client)
        self.mp = MPV()
        self.mp_poller = MPVPoller(self.mp, self._on_mpv_tick)
        self.dl = DownloadManager()

        mkdirp(STATE_DIR)

        self.settings = load_settings()

        _config_mod.DOWNLOADS_DIR = str(self.settings.get("download_dir") or _default_downloads_dir())
        mkdirp(_config_mod.DOWNLOADS_DIR)

        self.desired_volume = clamp(int(self.settings.get("volume", 70) or 70), 0, 130)
        self.desired_mute = bool(self.settings.get("mute", False))
        self.color_mode = bool(self.settings.get("color_mode"))
        self.queue_overlay = bool(self.settings.get("queue_overlay"))
        self.show_toggles = bool(self.settings.get("show_toggles"))
        self.show_numbers = bool(self.settings.get("show_numbers"))
        self.show_track_album = bool(self.settings.get("show_track_album", True))
        self.show_track_year = bool(self.settings.get("show_track_year", True))
        self.show_track_duration = bool(self.settings.get("show_track_duration", False))
        self.tab_align = bool(self.settings.get("tab_align"))
        self.tsv_max_col_width = int(self.settings.get("tsv_max_col_width", 32) or 32)
        def _field_w(key: str) -> int:
            v = int(self.settings.get(key, 0) or 0)
            return v if v > 0 else self.tsv_max_col_width
        self.tsv_field_widths = {
            "artist":   _field_w("tsv_max_artist_width"),
            "title":    _field_w("tsv_max_title_width"),
            "album":    _field_w("tsv_max_album_width"),
            "year":     _field_w("tsv_max_year_width"),
            "duration": _field_w("tsv_max_duration_width"),
        }

        q0 = str(self.settings.get("quality") or QUALITY_ORDER[0])
        self.quality_idx = QUALITY_ORDER.index(q0) if q0 in QUALITY_ORDER else 0

        # autoplay: 0=off 1=mix 2=recommended  (migrate old bool)
        raw_ap = self.settings.get("autoplay", AUTOPLAY_OFF)
        if raw_ap is True:
            raw_ap = AUTOPLAY_RECOMMENDED
        elif raw_ap is False:
            raw_ap = AUTOPLAY_OFF
        self.autoplay: int = clamp(int(raw_ap), 0, 2)
        self.autoplay_n: int = max(1, int(self.settings.get("autoplay_n", 3) or 3))

        # ---- autoplay state ----
        # Ring buffer of recently played tracks (up to 10), newest last
        self._play_history: List[Track] = []
        self._HISTORY_MAX = 10
        # Prefetched tracks waiting to be enqueued
        self._autoplay_buffer: List[Track] = []
        # True while a background prefetch is running
        self._autoplay_prefetch_running = False
        # Lock protecting _autoplay_buffer and _autoplay_prefetch_running
        self._autoplay_lock = threading.Lock()
        # Track id that triggered the most recent prefetch (avoid double-fetching)
        self._autoplay_last_seed_id: Optional[int] = None

        self.queue_items, self.queue_play_idx = load_queue()
        self.liked_tracks, self.liked_ids = load_liked()
        self.playlists, self.playlists_meta = load_playlists()
        self.history_tracks = load_history()
        self.queue_cursor = 0
        self.focus = "left"

        self.tab = int(self.settings.get("initial_tab") or TAB_QUEUE)
        self.tab = clamp(self.tab, TAB_QUEUE, TAB_HISTORY)

        self.search_q = ""
        self.search_results: List[Track] = []
        self.recommended_results: List[Track] = []
        # Mix tab state
        self.mix_tracks: List[Track] = []
        self.mix_title: str = ""
        self.mix_track: str = ""
        self.liked_cache: List[Track] = []
        self.artist_albums: List[Album] = []
        self.artist_tracks: List[Track] = []
        self.album_header: Optional[Album] = None
        self.album_tracks: List[Track] = []
        self.playlist_names: List[str] = sorted(self.playlists.keys())
        self.playlist_view_name: Optional[str] = None
        self.playlist_view_tracks: List[Track] = []

        self.left_idx = 0
        self.left_scroll = 0

        self.marked_left_idx: set = set()
        self.marked_queue_idx: set = set()

        self.priority_queue: List[int] = []

        self.repeat_mode = 0
        self.shuffle_on = False

        self.current_track: Optional[Track] = None
        self.last_error: Optional[str] = None

        self.toast_msg = ""
        self.toast_until = 0.0

        self.show_help = False
        self.help_scroll = 0

        self.info_overlay = False
        self.info_scroll = 0
        self.info_track: Optional[Track] = None
        self.info_album: Optional[Album] = None
        self.info_payload: Optional[Dict[str, Any]] = None
        self.info_loading = False
        self.info_follow_selection = True
        self._info_target_id: Optional[int] = None
        self._info_target_album_id: Optional[int] = None
        self._info_refresh_due = 0.0

        self.lyrics_overlay = False
        self.lyrics_scroll = 0
        self.lyrics_lines: List[str] = []
        self.lyrics_loading = False
        self.lyrics_track_id: Optional[int] = None
        self.lyrics_track: Optional["Track"] = None

        self.filter_q: str = str(self.settings.get("initial_filter") or "")
        self.filter_hits: List[int] = []
        self.filter_pos: int = -1

        self._need_redraw = True
        self._redraw_status_only = False
        self._loading = False
        self._loading_key = ""
        self._liked_refresh_due: float = 0.0

        self._last_mpd_path: Optional[str] = None

        self._init_curses()

        if self.filter_q:
            self._compute_filter_hits()

    def run(self) -> None:
        last_persist = 0.0

        if self.tab == TAB_LIKED:
            self.fetch_liked_async()

        while True:
            now = time.time()

            if self._liked_refresh_due and now >= self._liked_refresh_due:
                self._liked_refresh_due = 0.0
                if self.tab == TAB_LIKED:
                    self.fetch_liked_async()

            self._do_info_fetch_if_due()

            if self.info_overlay and not self.info_album and self.info_follow_selection:
                t = self._current_selection_track()
                if t and (self.info_track is None or t.id != self.info_track.id):
                    self._request_info_refresh(t)

            if self.current_track and not self.mp.alive():
                tp, du, pa, vo, mu = self.mp.snapshot()
                if tp is None and du is None:
                    self.current_track = None
                    self.next_track()
                    self._need_redraw = True
                    self._redraw_status_only = False

            if now - last_persist > 2.0:
                last_persist = now
                self.settings.update({
                    "volume": self.desired_volume, "mute": self.desired_mute,
                    "color_mode": self.color_mode, "queue_overlay": self.queue_overlay,
                    "show_toggles": self.show_toggles, "show_numbers": self.show_numbers,
                    "show_track_album": self.show_track_album, "show_track_year": self.show_track_year,
                    "show_track_duration": self.show_track_duration,
                    "quality": QUALITY_ORDER[self.quality_idx],
                    "autoplay": self.autoplay, "initial_tab": self.tab,
                    "initial_filter": self.filter_q,
                    "tab_align": self.tab_align,
                })
                try:
                    save_settings(self.settings)
                except Exception:
                    pass

            self.draw()

            ch = self.stdscr.getch()
            if ch == -1:
                time.sleep(0.004)
                continue

            self._need_redraw = True
            self._redraw_status_only = False

            if ch == 27:
                if self.lyrics_overlay:
                    self.lyrics_overlay = False
                elif self.info_overlay:
                    self.info_overlay = False
                elif self.show_help:
                    self.show_help = False
                elif self.filter_q:
                    self.filter_q = ""
                    self.filter_hits = []
                    self.filter_pos = -1
                    self.toast("Filter cleared")
                continue

            if ch == ord("Q"):
                break

            if ch == ord("?"):
                self.show_help = not self.show_help
                continue

            if self.show_help:
                if ch in (27, ord("?"), ord("Q"), ord("q")):
                    self.show_help = False
                elif ch in (curses.KEY_DOWN, ord("j")):
                    self.help_scroll = min(self.help_scroll + 1, max(0, getattr(self, "_help_max_scroll", 10_000)))
                elif ch in (curses.KEY_UP, ord("k")):
                    self.help_scroll = max(0, self.help_scroll - 1)
                elif ch == curses.KEY_PPAGE:
                    self.help_scroll = max(0, self.help_scroll - self._page_step())
                elif ch == curses.KEY_NPAGE:
                    self.help_scroll = min(self.help_scroll + self._page_step(), max(0, getattr(self, "_help_max_scroll", 10_000)))
                elif ch in (curses.KEY_HOME, ord("g")):
                    self.help_scroll = 0
                elif ch in (curses.KEY_END, ord("G")):
                    self.help_scroll = max(0, getattr(self, "_help_max_scroll", 10_000))
                continue

            if self.lyrics_overlay:
                if ch in (27, ord("v"), ord("V"), ord("q")):
                    self.lyrics_overlay = False
                elif ch in (curses.KEY_DOWN, ord("j")):
                    self.lyrics_scroll += 1
                elif ch in (curses.KEY_UP, ord("k")):
                    self.lyrics_scroll = max(0, self.lyrics_scroll - 1)
                elif ch == curses.KEY_PPAGE:
                    self.lyrics_scroll = max(0, self.lyrics_scroll - self._page_step())
                elif ch == curses.KEY_NPAGE:
                    self.lyrics_scroll += self._page_step()
                elif ch in (curses.KEY_HOME, ord("g")):
                    self.lyrics_scroll = 0
                elif ch in (curses.KEY_END, ord("G")):
                    self.lyrics_scroll = 10_000
                continue

            if ch == ord("/"):
                self.do_search_prompt_anywhere()
                continue
            if ch == ord("f"):
                self.filter_prompt()
                continue
            if ch == ord("("):
                self.filter_next(-1)
                continue
            if ch == ord(")"):
                self.filter_next(+1)
                continue

            if ch == ord("v"):
                t_sel = self._current_selection_track()
                self.toggle_lyrics(t_sel)
                continue
            if ch == ord("V"):
                self.toggle_lyrics(self.current_track)
                continue

            if ch == ord("i"):
                self.toggle_info_selected()
                continue
            if ch == ord("I"):
                self.toggle_info_playing()
                continue

            if self.info_overlay:
                if ch in (curses.KEY_DOWN, ord("j")):
                    self.info_scroll += 1
                    continue
                if ch in (curses.KEY_UP, ord("k")):
                    self.info_scroll = max(0, self.info_scroll - 1)
                    continue

            if ch == ord("o"):
                t = self._current_selection_track()
                if t:
                    self.open_url(f"{self.web_base()}/track/{t.id}")
                continue
            if ch == ord("O"):
                if self.current_track:
                    self.open_url(f"{self.web_base()}/track/{self.current_track.id}")
                continue

            if ch == ord("t"):
                self.play_track_with_resume()
                continue

            if ch == ord("\\"):
                self.tab_align = not self.tab_align
                self.toast("TSV: on" if self.tab_align else "TSV: off")
                continue

            if ch == ord("q"):
                self.queue_overlay = not self.queue_overlay
                if not self.queue_overlay and self.focus == "queue":
                    self.focus = "left"
                self.toast("Queue overlay: on" if self.queue_overlay else "Queue overlay: off")
                continue

            if ch == ord("\t") and self.queue_overlay:
                self.focus = "queue" if self.focus == "left" else "left"
                continue

            # Tab switching: 1-9
            if ch in (ord("1"), ord("2"), ord("3"), ord("4"),
                      ord("5"), ord("6"), ord("7"), ord("8"), ord("9")):
                mapping = {
                    ord("1"): TAB_QUEUE,
                    ord("2"): TAB_SEARCH,
                    ord("3"): TAB_RECOMMENDED,
                    ord("4"): TAB_MIX,
                    ord("5"): TAB_ARTIST,
                    ord("6"): TAB_ALBUM,
                    ord("7"): TAB_LIKED,
                    ord("8"): TAB_PLAYLISTS,
                    ord("9"): TAB_HISTORY,
                }
                t_num = mapping[ch]
                if t_num == TAB_MIX:
                    ctx = self._current_selection_track() or self.current_track
                    self.switch_tab(TAB_MIX, refresh=False)
                    self.fetch_mix_async(ctx)
                elif t_num == TAB_ARTIST:
                    ctx = self._current_selection_track() or self.current_track
                    self.switch_tab(TAB_ARTIST, refresh=False)
                    self.fetch_artist_async(ctx)
                elif t_num == TAB_ALBUM:
                    ctx = self._current_selection_track() or self.current_track
                    if ctx:
                        self.open_album_from_track(ctx)
                    else:
                        self.switch_tab(TAB_ALBUM, refresh=False)
                else:
                    self.switch_tab(t_num, refresh=True)
                continue

            if ch == ord("z"):
                self.jump_to_playing_in_queue()
                continue

            if ch == ord("a"):
                self.playlists_add_from_context()
                continue

            if ch == ord("n") and self.tab != TAB_PLAYLISTS:
                self.show_numbers = not self.show_numbers
                self.toast("Line numbers: on" if self.show_numbers else "Line numbers: off")
                continue

            if ch == ord("b"):
                if self._queue_context() and self.queue_items:
                    idx = clamp(self.queue_cursor, 0, len(self.queue_items) - 1)
                    self.toggle_priority(idx)
                elif not self._queue_context():
                    t_sel = self._selected_left_track()
                    if t_sel:
                        self._enqueue_tracks([t_sel], False)
                        new_idx = len(self.queue_items) - 1
                        self.toggle_priority(new_idx)
                continue

            if ch == ord("B"):
                self.clear_priority_queue()
                continue

            if ch == ord("u"):
                self.unmark_all_current_view()
                continue
            if ch == ord("U"):
                self.mark_all_current_view()
                continue

            # Playback
            if ch == ord("p"):
                if not self.mp.alive() and self.queue_items:
                    self.play_queue_index(self.queue_play_idx)
                else:
                    self.toggle_pause()
                continue
            if ch == ord("m"):
                self.mute_toggle()
                continue
            if ch == ord("-"):
                self.volume_add(-2.0)
                continue
            if ch in (ord("+"), ord("=")):
                self.volume_add(2.0)
                continue
            if ch == curses.KEY_LEFT:
                self.seek_rel(-5.0)
                continue
            if ch == curses.KEY_RIGHT:
                self.seek_rel(5.0)
                continue
            if ch == getattr(curses, "KEY_SLEFT", -999):
                self.seek_rel(-30.0)
                continue
            if ch == getattr(curses, "KEY_SRIGHT", -999):
                self.seek_rel(30.0)
                continue

            if ch in (ord("<"), ord(",")):
                self.prev_track()
                continue
            if ch in (ord(">"), ord(".")):
                self.next_track()
                continue

            if ch == ord("R"):
                self.repeat_mode = (self.repeat_mode + 1) % 3
                self.toast(["Repeat: off", "Repeat: all", "Repeat: one"][self.repeat_mode])
                continue
            if ch == ord("S"):
                self.shuffle_on = not self.shuffle_on
                self.toast("Shuffle: on" if self.shuffle_on else "Shuffle: off")
                continue
            if ch == ord("A"):
                self.autoplay = (self.autoplay + 1) % 3
                self.toast(f"Autoplay: {AUTOPLAY_NAMES[self.autoplay]}")
                # Reset buffer/seed when mode changes
                with self._autoplay_lock:
                    self._autoplay_buffer = []
                    self._autoplay_last_seed_id = None
                # Kick off a prefetch immediately if switching to an active mode
                if self.autoplay != AUTOPLAY_OFF:
                    self._autoplay_trigger_prefetch()
                continue
            if ch == ord("F"):
                self.quality_idx = (self.quality_idx + 1) % len(QUALITY_ORDER)
                self.toast(f"Quality: {QUALITY_ORDER[self.quality_idx]}")
                continue
            if ch == ord("T"):
                self.show_toggles = not self.show_toggles
                self.toast("Toggles: on" if self.show_toggles else "Toggles: off")
                continue
            if ch == ord("c"):
                self.color_mode = not self.color_mode
                self.toast("Color" if self.color_mode else "B/W")
                continue
            if ch == ord("w"):
                self.show_track_album = not self.show_track_album
                self.toast("Album field: on" if self.show_track_album else "Album field: off")
                continue
            if ch == ord("y"):
                self.show_track_year = not self.show_track_year
                self.toast("Year field: on" if self.show_track_year else "Year field: off")
                continue
            if ch == ord("d") and self.tab != TAB_PLAYLISTS:
                self.show_track_duration = not self.show_track_duration
                self.toast("Duration field: on" if self.show_track_duration else "Duration field: off")
                continue

            if ch == ord("l"):
                self.like_selected()
                continue
            if ch == ord("L"):
                self.like_playing()
                continue

            if ch == ord(" "):
                self.toggle_mark_and_advance()
                continue

            if ch == ord("e"):
                self.enqueue_key(insert_after_playing=False)
                continue
            if ch == ord("E"):
                self.enqueue_key(insert_after_playing=True)
                continue

            if self.tab == TAB_PLAYLISTS:
                if ch == ord("n"):
                    self.playlists_create()
                    continue
                if ch == ord("d"):
                    self.playlists_delete_current()
                    continue
                if ch == ord("a"):
                    self.playlists_add_from_context()
                    continue
                if ch in (curses.KEY_BACKSPACE, 127, 8):
                    if self.playlist_view_name is not None:
                        self.playlist_view_name = None
                        self.playlist_view_tracks = []
                        self.playlist_names = sorted(self.playlists.keys())
                        self.left_idx = 0
                        self.left_scroll = 0
                        self._need_redraw = True
                        self._redraw_status_only = False
                    continue
                if ch == ord("x") and self.playlist_view_name is not None:
                    pname = self.playlist_view_name
                    tracks = self.playlist_view_tracks
                    idxs = sorted([i for i in self.marked_left_idx if 0 <= i < len(tracks)])
                    if not idxs:
                        idxs = [clamp(self.left_idx, 0, max(0, len(tracks) - 1))] if tracks else []
                    for idx in sorted(idxs, reverse=True):
                        if 0 <= idx < len(tracks):
                            tid = tracks[idx].id
                            self.playlist_view_tracks = [t for j, t in enumerate(tracks) if j != idx]
                            tracks = self.playlist_view_tracks
                            self.playlists[pname] = [t for t in self.playlists.get(pname, []) if t.id != tid]
                    self.marked_left_idx.clear()
                    self.left_idx = clamp(self.left_idx, 0, max(0, len(self.playlist_view_tracks) - 1))
                    save_playlists(self.playlists, self.playlists_meta)
                    self.toast("Removed from playlist")
                    self._need_redraw = True
                    self._redraw_status_only = False
                    continue

            if ch == ord("x") and self.tab != TAB_PLAYLISTS:
                if self.queue_items:
                    idxs = sorted([i for i in self.marked_queue_idx if 0 <= i < len(self.queue_items)])
                    if not idxs:
                        idxs = [clamp(self.queue_cursor, 0, len(self.queue_items) - 1)]
                    for idx in sorted(idxs, reverse=True):
                        del self.queue_items[idx]
                        if idx < self.queue_play_idx:
                            self.queue_play_idx -= 1
                        elif idx == self.queue_play_idx:
                            self.queue_play_idx = clamp(self.queue_play_idx, 0, max(0, len(self.queue_items) - 1))
                    self._remap_priority_after_delete(idxs)
                    self.marked_queue_idx.clear()
                    self.queue_cursor = clamp(self.queue_cursor, 0, max(0, len(self.queue_items) - 1))
                    self.toast("Removed")
                continue

            if ch == ord("X"):
                if self.queue_items and self.prompt_yes_no("Clear queue? (y/n)"):
                    if self.current_track and 0 <= self.queue_play_idx < len(self.queue_items):
                        playing_item = self.queue_items[self.queue_play_idx]
                        self.queue_items = [playing_item]
                        self.queue_play_idx = 0
                        self.queue_cursor = 0
                    else:
                        self.queue_items = []
                        self.queue_play_idx = 0
                        self.queue_cursor = 0
                    self.marked_queue_idx.clear()
                    self.priority_queue.clear()
                    self.toast("Queue cleared")
                    self._need_redraw = True
                    self._redraw_status_only = False
                continue

            if ch in (ord("J"), ord("K")):
                if not self.queue_items:
                    continue
                delta = +1 if ch == ord("J") else -1
                idxs = sorted([i for i in self.marked_queue_idx if 0 <= i < len(self.queue_items)])
                if not idxs:
                    i = clamp(self.queue_cursor, 0, len(self.queue_items) - 1)
                    j = i + delta
                    if 0 <= j < len(self.queue_items):
                        self.queue_items[i], self.queue_items[j] = self.queue_items[j], self.queue_items[i]
                        pi, pj = self._priority_index_of(i), self._priority_index_of(j)
                        if pi > 0:
                            self.priority_queue[pi - 1] = j
                        if pj > 0:
                            self.priority_queue[pj - 1] = i
                        self.queue_cursor = j
                        if self.queue_play_idx == i:
                            self.queue_play_idx = j
                        elif self.queue_play_idx == j:
                            self.queue_play_idx = i
                    continue

                s = set(idxs)
                if delta < 0:
                    for i in idxs:
                        j = i - 1
                        if j < 0 or j in s:
                            continue
                        self.queue_items[j], self.queue_items[i] = self.queue_items[i], self.queue_items[j]
                        pi, pj = self._priority_index_of(i), self._priority_index_of(j)
                        if pi > 0:
                            self.priority_queue[pi - 1] = j
                        if pj > 0:
                            self.priority_queue[pj - 1] = i
                        s.discard(i)
                        s.add(j)
                        if self.queue_play_idx == i:
                            self.queue_play_idx = j
                        elif self.queue_play_idx == j:
                            self.queue_play_idx = i
                else:
                    for i in reversed(idxs):
                        j = i + 1
                        if j >= len(self.queue_items) or j in s:
                            continue
                        self.queue_items[j], self.queue_items[i] = self.queue_items[i], self.queue_items[j]
                        pi, pj = self._priority_index_of(i), self._priority_index_of(j)
                        if pi > 0:
                            self.priority_queue[pi - 1] = j
                        if pj > 0:
                            self.priority_queue[pj - 1] = i
                        s.discard(i)
                        s.add(j)
                        if self.queue_play_idx == i:
                            self.queue_play_idx = j
                        elif self.queue_play_idx == j:
                            self.queue_play_idx = i
                self.marked_queue_idx = s
                m = sorted(s)
                if m:
                    self.queue_cursor = m[0] if delta < 0 else m[-1]
                continue

            if ch == ord("D"):
                if not self._queue_context() and self.tab == TAB_ALBUM and self._selected_album_title_line():
                    self.start_download_tracks(list(self.album_tracks))
                    continue
                if not self._queue_context() and self.tab == TAB_ARTIST:
                    alb = self._selected_left_album()
                    if alb:
                        def worker_dl() -> None:
                            try:
                                aid = self._resolve_album_id_for_album(alb)
                                if not aid:
                                    self.toast("Album id?")
                                    return
                                self.start_download_tracks(self._fetch_album_tracks_by_album_id(aid))
                            except Exception:
                                self.toast("Error")
                        threading.Thread(target=worker_dl, daemon=True).start()
                        continue
                if self.tab == TAB_PLAYLISTS and self.playlist_view_name is not None:
                    self.start_download_tracks(list(self.playlist_view_tracks))
                    continue
                if self._queue_context():
                    tracks = self._marked_tracks_from_queue() or ([self._queue_selected_track()] if self._queue_selected_track() else [])
                else:
                    tracks = self._marked_tracks_from_left() or ([self._selected_left_track()] if self._selected_left_track() else [])
                self.start_download_tracks([t for t in tracks if t])
                continue

            # Navigation
            if ch == curses.KEY_PPAGE:
                self.nav_page(-1)
                continue
            if ch == curses.KEY_NPAGE:
                self.nav_page(+1)
                continue
            if ch in (curses.KEY_HOME, ord("g")):
                self.nav_home()
                continue
            if ch in (curses.KEY_END, ord("G")):
                self.nav_end()
                continue
            if ch in (curses.KEY_DOWN, ord("j")):
                if self._queue_context():
                    self.queue_cursor = clamp(self.queue_cursor + 1, 0, max(0, len(self.queue_items) - 1))
                else:
                    _typ, items = self._left_items()
                    new_idx = self.left_idx + 1
                    while new_idx < len(items) and isinstance(items[new_idx], tuple) and items[new_idx][0] == "sep":
                        new_idx += 1
                    self.left_idx = clamp(new_idx, 0, max(0, len(items) - 1))
                continue
            if ch in (curses.KEY_UP, ord("k")):
                if self._queue_context():
                    self.queue_cursor = clamp(self.queue_cursor - 1, 0, max(0, len(self.queue_items) - 1))
                else:
                    _typ, items = self._left_items()
                    new_idx = self.left_idx - 1
                    while new_idx >= 0 and isinstance(items[new_idx], tuple) and items[new_idx][0] == "sep":
                        new_idx -= 1
                    self.left_idx = clamp(new_idx, 0, max(0, len(items) - 1))
                continue

            if ch in (10, 13):
                if self.tab == TAB_PLAYLISTS and self.playlist_view_name is None:
                    self.playlists_open_selected()
                    continue
                if self._queue_context():
                    self.play_queue_index(self.queue_cursor)
                    continue
                if self.tab == TAB_ARTIST:
                    alb = self._selected_left_album()
                    if alb:
                        self.open_album_from_album_obj(alb)
                        continue
                it = self._selected_left_track()
                if it:
                    self.play_track(it)
                continue

        # Cleanup
        try:
            save_queue(self.queue_items, self.queue_play_idx)
        except Exception:
            pass
        try:
            save_liked(self.liked_tracks)
        except Exception:
            pass
        try:
            save_playlists(self.playlists, self.playlists_meta)
        except Exception:
            pass
        try:
            self.settings.update({
                "volume": self.desired_volume, "mute": self.desired_mute,
                "color_mode": self.color_mode, "queue_overlay": self.queue_overlay,
                "show_toggles": self.show_toggles, "show_numbers": self.show_numbers,
                "show_track_album": self.show_track_album, "show_track_year": self.show_track_year,
                "show_track_duration": self.show_track_duration,
                "quality": QUALITY_ORDER[self.quality_idx],
                "autoplay": self.autoplay, "initial_tab": self.tab,
                "initial_filter": self.filter_q,
                "tab_align": self.tab_align,
            })
            save_settings(self.settings)
        except Exception:
            pass
        self.meta.stop()
        self.mp_poller.stop()
        self.mp.stop()
