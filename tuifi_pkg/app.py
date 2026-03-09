"""App class, argument parsing and CLI entry point for tuifi."""

from __future__ import annotations

import base64
import curses
import hashlib
import json
import locale
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from queue import Queue, Empty
from typing import Any, Dict, List, Optional, Set, Tuple

from tuifi_pkg import (
    APP_NAME, VERSION, DEFAULT_API,
    TAB_SEARCH, TAB_QUEUE, TAB_RECOMMENDED, TAB_MIX, TAB_ARTIST,
    TAB_ALBUM, TAB_LIKED, TAB_PLAYLISTS, TAB_HISTORY, TAB_COVER,
    TAB_NAMES,
    AUTOPLAY_OFF, AUTOPLAY_MIX, AUTOPLAY_RECOMMENDED, AUTOPLAY_NAMES,
    QUALITY_ORDER,
)
from tuifi_pkg.models import (
    Track, Album, Artist,
    debug_log, _DEBUG_LOG,
    _resolve_config_dir, _default_downloads_dir,
    STATE_DIR, QUEUE_FILE, LIKED_FILE, PLAYLISTS_FILE, HISTORY_FILE, SETTINGS_FILE, DOWNLOADS_DIR,
    mkdirp, clamp, safe_filename, year_norm, album_year_from_obj,
    fmt_time, fmt_dur,
    track_to_mono, mono_to_track,
)
from tuifi_pkg.persistence import (
    load_json, save_json,
    load_queue, save_queue,
    load_liked, save_liked,
    load_playlists, save_playlists,
    load_history, save_history,
    load_settings, save_settings,
)
from tuifi_pkg.client import HiFiClient, http_get_bytes, http_get_json, http_stream_download
from tuifi_pkg.audio import MPV, MPVPoller
from tuifi_pkg.workers import MetaFetcher, DownloadManager


def print_version(prog: str) -> None:
    print(f"tuifi v{VERSION}")


class App:
    # ---------------------------------------------------------------------------
    # Construction
    # ---------------------------------------------------------------------------
    def __init__(self, stdscr: "curses._CursesWindow", api_base: str, args: Dict[str, Any]) -> None:
        self.stdscr = stdscr
        self.api_base = api_base.rstrip("/")
        self.client = HiFiClient(self.api_base)
        self.meta = MetaFetcher(self.client)
        self.mp = MPV()
        self.mp_poller = MPVPoller(self.mp, self._on_mpv_tick)
        self.dl = DownloadManager()

        mkdirp(STATE_DIR)

        self.settings = load_settings()

        global DOWNLOADS_DIR
        DOWNLOADS_DIR = str(self.settings.get("download_dir") or _default_downloads_dir())
        mkdirp(DOWNLOADS_DIR)

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
        (self.liked_tracks, self.liked_ids,
         self.liked_albums, self.liked_album_ids,
         self.liked_artists, self.liked_artist_ids,
         self.liked_playlists, self.liked_playlist_ids) = load_liked()
        self.playlists, self.playlists_meta = load_playlists()
        self.history_tracks = load_history()
        self.queue_cursor = 0
        self.focus = "left"

        self.tab = int(self.settings.get("initial_tab") or TAB_QUEUE)
        self.tab = clamp(self.tab, TAB_QUEUE, TAB_COVER)

        self.search_q = ""
        self.search_results: List[Track] = []
        self.recommended_results: List[Track] = []
        # Mix tab state
        self.mix_tracks: List[Track] = []
        self.mix_title: str = ""
        self.mix_track: str = ""
        self.liked_cache: List[Track] = []
        self.liked_album_cache: List[Album] = []
        self.liked_artist_cache: List[Artist] = []
        self.liked_playlist_cache: List[str] = []
        self.liked_filter: int = 0
        self.artist_ctx: Optional[Tuple[int, str]] = None
        self.artist_albums: List[Album] = []
        self.artist_tracks: List[Track] = []
        self.album_header: Optional[Album] = None
        self.album_tracks: List[Track] = []
        self.playlist_names: List[str] = sorted(self.playlists.keys())
        self.playlist_view_name: Optional[str] = None
        self.playlist_view_tracks: List[Track] = []

        self.left_idx = 0
        self.left_scroll = 0
        self._tab_positions: Dict[int, Tuple[int, int]] = {}
        self._prev_tab: int = self.tab

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
        self.info_artist: Optional[Artist] = None
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

        # Cover tab state
        self.cover_track: Optional[Track] = None   # track whose cover is loaded
        self.cover_path: Optional[str] = None      # local path to cached image file
        self.cover_loading: bool = False
        self._cover_backend_cache: Optional[str] = None   # "ueberzugpp"/"chafa"/"none"
        self._cover_render_key: str = ""           # "path:WxH" to detect when re-render needed
        self._cover_render_buf: Optional[bytes] = None    # cached chafa/ANSI output
        self._cover_sixel_visible: bool = False    # True when image data is on the terminal
        self._cover_sixel_cols: int = 0            # width (columns) of the last rendered sixel
        self._cover_ub_socket: Optional[str] = None       # ueberzugpp socket path
        self._cover_ub_pid: Optional[int] = None          # ueberzugpp daemon PID
        self._cover_lyrics: bool = True                # show lyrics panel in cover tab
        self._cover_lyrics_max_scroll: int = 10_000   # updated each draw; prevents over-scrolling inline panel
        self._show_singles_eps: bool = bool(self.settings.get("include_singles_and_eps_in_artist_tab", False))
        self._last_artist_fetch_track: Optional["Track"] = None

        self._skip_delta: int = 0         # accumulated next/prev presses waiting to be applied
        self._skip_at: float = 0.0        # timestamp of last skip key press

        self.filter_q: str = ""   # not persisted across sessions
        self.filter_hits: List[int] = []
        self.filter_pos: int = -1
        self._lyrics_filter_q: str = ""
        self._lyrics_filter_hits: List[int] = []
        self._lyrics_filter_pos: int = -1

        self._need_redraw = True
        self._redraw_status_only = False
        self._queue_redraw_only = False    # fast path: only queue panel changed (cover tab)
        self._loading = False
        self._loading_key = ""
        self._liked_refresh_due: float = 0.0

        self._last_mpd_path: Optional[str] = None

        self._init_curses()

    # ---------------------------------------------------------------------------
    # Autoplay helpers
    # ---------------------------------------------------------------------------

    def _autoplay_seed_pool(self) -> List[Track]:
        """Return the combined candidate pool for choosing a prefetch seed.

        Combines recent play history (newest first) with upcoming queue tracks
        that have not yet been played.  Works even when history or queue is empty.
        """
        seen_ids: set = set()
        pool: List[Track] = []

        # Recent history, newest first (most diverse seed candidates)
        for t in reversed(self._play_history):
            if t.id not in seen_ids:
                seen_ids.add(t.id)
                pool.append(t)

        # Upcoming queue tracks (not yet played)
        remaining_start = self.queue_play_idx + 1 if self.queue_items else 0
        for t in self.queue_items[remaining_start:]:
            if t.id not in seen_ids:
                seen_ids.add(t.id)
                pool.append(t)

        # Last resort: current track
        if not pool and self.current_track:
            pool.append(self.current_track)

        return pool

    def _autoplay_tracks_remaining(self) -> int:
        """Number of queue tracks still to be played after the current one."""
        if not self.queue_items:
            return 0
        return max(0, len(self.queue_items) - 1 - self.queue_play_idx)

    def _autoplay_should_prefetch(self) -> bool:
        """Return True if we should kick off a background prefetch right now."""
        if self.autoplay == AUTOPLAY_OFF:
            return False
        with self._autoplay_lock:
            if self._autoplay_prefetch_running:
                return False
            # Only prefetch when the buffer is empty so we don't stack fetches
            if self._autoplay_buffer:
                return False
        return True

    def _autoplay_record_played(self, t: Track) -> None:
        """Add t to the play-history ring buffer."""
        # Avoid consecutive duplicates
        if self._play_history and self._play_history[-1].id == t.id:
            return
        self._play_history.append(t)
        if len(self._play_history) > self._HISTORY_MAX:
            self._play_history.pop(0)

    def _autoplay_trigger_prefetch(self) -> None:
        """Kick off a background thread to fill _autoplay_buffer."""
        if not self._autoplay_should_prefetch():
            return

        pool = self._autoplay_seed_pool()
        if not pool:
            return

        seed = random.choice(pool)

        # Don't re-fetch if we already fetched from this seed recently
        with self._autoplay_lock:
            if seed.id == self._autoplay_last_seed_id:
                return
            self._autoplay_prefetch_running = True
            self._autoplay_last_seed_id = seed.id

        mode = self.autoplay
        n = self.autoplay_n
        debug_log(f"autoplay prefetch: mode={AUTOPLAY_NAMES[mode]} seed={seed.artist}/{seed.title}")

        def worker() -> None:
            try:
                tracks: List[Track] = []
                if mode == AUTOPLAY_MIX:
                    tracks = self._fetch_mix_tracks_for_track(seed)
                elif mode == AUTOPLAY_RECOMMENDED:
                    tracks = self._fetch_recommended_tracks_for_track(seed)

                # Deduplicate against current queue
                queue_ids = {t.id for t in self.queue_items}
                fresh = [t for t in tracks if t.id not in queue_ids]

                # Pick n random tracks from the result
                if len(fresh) > n:
                    fresh = random.sample(fresh, n)

                with self._autoplay_lock:
                    self._autoplay_buffer = fresh
                    debug_log(f"autoplay prefetch done: {len(fresh)} tracks buffered")

            except Exception as e:
                debug_log(f"autoplay prefetch error: {e}")
            finally:
                with self._autoplay_lock:
                    self._autoplay_prefetch_running = False

        threading.Thread(target=worker, daemon=True).start()

    def _autoplay_maybe_enqueue(self) -> None:
        """Called each time a new track starts playing.

        1. Record the new track in history.
        2. Trigger a prefetch (if one isn't already running/buffer non-empty).
        3. If the queue is running low AND we have buffered tracks, enqueue them.
        """
        t = self.current_track
        if t:
            self._autoplay_record_played(t)

        if self.autoplay == AUTOPLAY_OFF:
            return

        remaining = self._autoplay_tracks_remaining()

        # Drain buffer into queue when running low
        with self._autoplay_lock:
            buf = list(self._autoplay_buffer)

        if buf and remaining < self.autoplay_n:
            self._autoplay_buffer_drain(buf)
            # After draining, kick off a new prefetch immediately so the
            # buffer is ready for the next low-water-mark event
            with self._autoplay_lock:
                self._autoplay_buffer = []
                self._autoplay_last_seed_id = None  # allow a fresh seed

        # Always try to kick off a prefetch so the buffer stays warm
        self._autoplay_trigger_prefetch()

    def _autoplay_buffer_drain(self, tracks: List[Track]) -> None:
        """Append buffered tracks to the end of the queue (thread-safe w.r.t. queue)."""
        if not tracks:
            return
        queue_ids = {t.id for t in self.queue_items}
        fresh = [t for t in tracks if t.id not in queue_ids]
        if not fresh:
            return
        self.queue_items.extend(fresh)
        self.toast(f"Autoplay +{len(fresh)}")
        self._need_redraw = True
        self._redraw_status_only = False
        debug_log(f"autoplay: enqueued {len(fresh)} tracks, queue now {len(self.queue_items)}")

    def _fetch_track_mix_payload_for_track(self, seed: Track) -> Optional[Dict[str, Any]]:
        """Fetch the TRACK_MIX payload for `seed` via /info -> mixes -> TRACK_MIX.

        Returns the /mix payload dict, or None if no TRACK_MIX is available.
        """
        try:
            info = self.client.info(seed.id)
        except Exception as e:
            debug_log(f"_fetch_track_mix_payload_for_track: /info failed for {seed.id}: {e}")
            return None

        mix_id: Optional[str] = None

        # Navigate: data.mixes.TRACK_MIX  or  mixes.TRACK_MIX
        for root in (info.get("data") if isinstance(info, dict) else None, info):
            if isinstance(root, dict):
                mixes = root.get("mixes")
                if isinstance(mixes, dict):
                    v = mixes.get("TRACK_MIX")
                    if isinstance(v, str) and v.strip():
                        mix_id = v.strip()
                        break

        if not mix_id:
            debug_log(f"_fetch_track_mix_payload_for_track: no TRACK_MIX for track {seed.id}")
            return None

        debug_log(f"_fetch_track_mix_payload_for_track: fetching mix {mix_id} (TRACK_MIX)")
        try:
            return self.client.mix(mix_id)
        except Exception as e:
            debug_log(f"_fetch_track_mix_payload_for_track: /mix failed for {mix_id}: {e}")
            return None

    def _fetch_mix_tracks_for_track(self, seed: Track) -> List[Track]:
        """Fetch the TRACK_MIX for `seed` and return a list of Track objects."""
        mix_payload = self._fetch_track_mix_payload_for_track(seed)
        if not mix_payload:
            return []
        return self._extract_tracks_from_mix_payload(mix_payload)

    def _fetch_recommended_tracks_for_track(self, seed: Track) -> List[Track]:
        """Fetch recommendations seeded from `seed`."""
        payload = self.client.recommendations(seed.id, limit=50)
        tracks: List[Track] = []
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            for it in data["items"]:
                if isinstance(it, dict) and isinstance(it.get("track"), dict):
                    t = self._parse_track_obj(it["track"])
                    if t:
                        tracks.append(t)
        return tracks

    def _extract_tracks_from_mix_payload(self, payload: Dict[str, Any]) -> List[Track]:
        """Parse tracks from /mix/ response (items array at top level)."""
        tracks: List[Track] = []
        items = payload.get("items")
        if not isinstance(items, list):
            # Try data.items
            data = payload.get("data")
            if isinstance(data, dict):
                items = data.get("items")
        if isinstance(items, list):
            for obj in items:
                if isinstance(obj, dict):
                    t = self._parse_track_obj(obj)
                    if t:
                        tracks.append(t)
        return tracks

    # ---------------------------------------------------------------------------
    # Mix tab loader
    # ---------------------------------------------------------------------------

    def fetch_mix_async(self, ctx: Optional[Track]) -> None:
        """Load the artist mix for `ctx` into the Mix tab."""
        if not ctx:
            self.toast("No context track")
            return
        self.mix_tracks = []
        self.mix_title = ""
        self.mix_track = ctx.artist
        key = f"mix:{ctx.id}:{time.time()}"
        self._set_loading(key)

        def worker() -> None:
            try:
                mix_payload = self._fetch_track_mix_payload_for_track(ctx)
                if self._loading_key != key:
                    return

                if not mix_payload:
                    self.mix_tracks = []
                    self.mix_title = ""
                    self.toast("No track mix")
                    return

                tracks = self._extract_tracks_from_mix_payload(mix_payload)

                # Prefer server-provided mix title if present
                mix_title = ""
                try:
                    m = mix_payload.get("mix")
                    if isinstance(m, dict):
                        t_str = m.get("title")
                        if isinstance(t_str, str) and t_str.strip():
                            mix_title = t_str.strip()
                except Exception:
                    pass

                if not mix_title:
                    mix_title = f"{ctx.artist} – {ctx.title} (Track Mix)"

                self.mix_tracks = tracks
                self.mix_title = mix_title
                self.toast(f"Mix: {len(tracks)} tracks")
            except Exception as e:
                if self._loading_key == key:
                    self.last_error = str(e)
                    self.toast("Mix error")
            finally:
                self._clear_loading(key)
                
        threading.Thread(target=worker, daemon=True).start()

    def _extract_mix_id_from_payload(self, payload: Dict[str, Any]) -> Optional[str]:
        """Search common paths for any mix ID in an API payload (album, artist, track info)."""
        if not isinstance(payload, dict):
            return None
        # Roots to search: payload["data"], payload["artist"], payload itself
        roots = [payload.get("data"), payload.get("artist"), payload]
        for root in roots:
            if not isinstance(root, dict):
                continue
            mixes = root.get("mixes")
            if isinstance(mixes, dict):
                for v in mixes.values():
                    if isinstance(v, str) and v.strip():
                        return v.strip()
        return None

    def fetch_mix_from_album_async(self, album: Album) -> None:
        """Load the Mix tab seeded from an album (uses first album track as seed)."""
        self.mix_tracks = []
        self.mix_title = ""
        self.mix_track = album.title
        key = f"mix:album:{album.id}:{time.time()}"
        self._set_loading(key)

        def worker() -> None:
            try:
                # Try mix ID embedded in album payload first
                seed_track: Optional[Track] = None
                mix_id: Optional[str] = None
                if album.id and album.id > 0:
                    try:
                        payload = self.client.album(int(album.id))
                        mix_id = self._extract_mix_id_from_payload(payload)
                        if not mix_id:
                            # Fall back: use first track from album as mix seed
                            tracks = self._extract_tracks_from_album_payload(payload)
                            if tracks:
                                seed_track = tracks[0]
                    except Exception:
                        pass
                if self._loading_key != key:
                    return
                if mix_id:
                    mix_payload = self.client.mix(mix_id)
                elif seed_track:
                    mix_payload = self._fetch_track_mix_payload_for_track(seed_track)
                else:
                    self.toast("No mix for album")
                    return
                if not mix_payload or self._loading_key != key:
                    return
                tracks = self._extract_tracks_from_mix_payload(mix_payload)
                self.mix_tracks = tracks
                self.mix_title = f"{album.artist} — {album.title} (Mix)"
                self.toast(f"Mix: {len(tracks)} tracks")
            except Exception as e:
                if self._loading_key == key:
                    self.last_error = str(e)
                    self.toast("Mix error")
            finally:
                self._clear_loading(key)

        threading.Thread(target=worker, daemon=True).start()

    def fetch_mix_from_artist_async(self, artist: Artist) -> None:
        """Load the Mix tab seeded from an artist."""
        self.mix_tracks = []
        self.mix_title = ""
        self.mix_track = artist.name
        key = f"mix:artist:{artist.id}:{time.time()}"
        self._set_loading(key)

        def worker() -> None:
            try:
                mix_id: Optional[str] = None
                if artist.id and artist.id > 0:
                    try:
                        payload = self.client.artist(int(artist.id))
                        mix_id = self._extract_mix_id_from_payload(payload)
                        if not mix_id:
                            # ?f= may omit mixes; try ?id= explicitly
                            payload2 = http_get_json(self.client._u("/artist/", {"id": int(artist.id)}))
                            mix_id = self._extract_mix_id_from_payload(payload2)
                    except Exception:
                        pass
                if not mix_id:
                    self.toast("No mix for artist")
                    return
                if self._loading_key != key:
                    return
                mix_payload = self.client.mix(mix_id)
                tracks = self._extract_tracks_from_mix_payload(mix_payload)
                if self._loading_key != key:
                    return
                self.mix_tracks = tracks
                self.mix_title = f"{artist.name} (Mix)"
                self.toast(f"Mix: {len(tracks)} tracks")
            except Exception as e:
                if self._loading_key == key:
                    self.last_error = str(e)
                    self.toast("Mix error")
            finally:
                self._clear_loading(key)

        threading.Thread(target=worker, daemon=True).start()

    # ---------------------------------------------------------------------------
    # mpv tick callback
    # ---------------------------------------------------------------------------
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
    # curses/colors
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
        # Ignore SIGQUIT so Ctrl+4 (which some terminals map to ^\) doesn't crash the app
        try:
            signal.signal(signal.SIGQUIT, signal.SIG_IGN)
        except Exception:
            pass
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

    def _bg(self, fn, *, loading_key: str = "", on_error: str = "Error") -> None:
        """Run fn() in a daemon thread. Manages loading state and error toasts."""
        if loading_key:
            self._set_loading(loading_key)
        def _run():
            try:
                fn()
            except Exception as e:
                debug_log(f"bg error ({loading_key or fn.__name__}): {e}")
                if on_error:
                    self.toast(on_error)
            finally:
                if loading_key:
                    self._clear_loading(loading_key)
                self._need_redraw = True
        threading.Thread(target=_run, daemon=True).start()

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


    def _dedupe_tracks(self, tracks: List[Track]) -> List[Track]:
        seen_ids: Set[int] = set()
        seen_ta: Set[Tuple[str, str]] = set()
        out: List[Track] = []
        for t in tracks:
            if t.id in seen_ids:
                continue
            # Also deduplicate by title+artist to collapse same track released
            # under different IDs in multiple album editions.
            ta = (t.title.strip().lower(), t.artist.strip().lower())
            if ta in seen_ta:
                continue
            seen_ids.add(t.id)
            seen_ta.add(ta)
            out.append(t)
        return out

    def _dedupe_albums(self, albums: List[Album]) -> List[Album]:
        # Key by (artist, title, year) so that different-ID editions of the
        # same album are collapsed into one entry (matching Monochrome.tf behaviour).
        best: Dict[Tuple[str, str, str], Album] = {}
        for a in albums:
            k = (
                a.artist.strip().lower(),
                a.title.strip().lower(),
                year_norm(a.year),
            )
            if k not in best:
                best[k] = Album(id=a.id, title=a.title, artist=a.artist, year=a.year, track_id=a.track_id)
                continue
            cur = best[k]
            # Prefer the lowest non-zero id (tends to be the primary/canonical release)
            if a.id and (not cur.id or a.id < cur.id):
                cur.id = a.id
            if year_norm(cur.year) == "????" and year_norm(a.year) != "????":
                cur.year = a.year
            if not cur.track_id and a.track_id:
                cur.track_id = a.track_id
        out = list(best.values())
        out.sort(key=lambda a: (int(a.year) if year_norm(a.year) != "????" else 9999, a.title.lower()))
        return out

    def _extract_artist_albums_from_payload(self, payload: Dict[str, Any]) -> List[Album]:
        include_singles = self._show_singles_eps
        albums: List[Album] = []
        for path in (("albums", "items"), ("data", "albums", "items"), ("albums",), ("data", "albums")):
            cur: Any = payload
            ok = True
            for p in path:
                if isinstance(cur, dict) and p in cur:
                    cur = cur[p]
                else:
                    ok = False
                    break
            if ok and isinstance(cur, list):
                for it in cur:
                    if isinstance(it, dict):
                        x = it.get("item", it) if isinstance(it.get("item"), dict) else it
                        if not include_singles:
                            alb_type = str(x.get("type") or "").upper()
                            if alb_type in ("SINGLE", "EP"):
                                continue
                        aobj = self._parse_album_obj(x)
                        if aobj:
                            albums.append(aobj)
                if albums:
                    break
        return self._dedupe_albums(albums)

    def _fetch_artist_catalog_by_artist_id(self, artist_id: int) -> Tuple[List[Album], List[Track]]:
        payload = self.client.artist(int(artist_id))

        albums = self._extract_artist_albums_from_payload(payload)
        tracks: List[Track] = []

        for alb in albums:
            if alb.id:
                try:
                    tracks.extend(self._fetch_album_tracks_by_album_id(alb.id))
                except Exception:
                    pass

        if not tracks:
            dicts: List[Dict[str, Any]] = []
            self._scan_for_track_dicts(payload, dicts, limit=2500)
            for d in dicts:
                t = self._parse_track_obj(d)
                if t:
                    tracks.append(t)

        tracks = self._dedupe_tracks(tracks)

        def _yr(t: Track) -> int:
            y = year_norm(t.year)
            return int(y) if y.isdigit() else 9999

        tracks.sort(key=lambda t: (_yr(t), t.album.lower(), t.track_no or 9999, t.title.lower()))

        if not albums and tracks:
            best: Dict[Tuple[str, str], Album] = {}
            for t in tracks:
                k = (t.artist.strip().lower(), t.album.strip().lower())
                if k not in best:
                    best[k] = Album(id=t.album_id or 0, title=t.album, artist=t.artist, year=t.year)
                else:
                    cur = best[k]
                    if cur.id == 0 and t.album_id:
                        cur.id = t.album_id
                    if year_norm(cur.year) == "????" and year_norm(t.year) != "????":
                        cur.year = t.year
            albums = self._dedupe_albums(list(best.values()))

        return albums, tracks

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
    # UI model
    # ---------------------------------------------------------------------------
    def _queue_context(self) -> bool:
        return self.tab == TAB_QUEUE or self.focus == "queue"

    def _left_items(self) -> Tuple[str, List[Any]]:
        if self._loading:
            # For the Artist tab, fall through to show partial data while loading
            if not (self.tab == TAB_ARTIST and (self.artist_ctx or self.artist_albums or self.artist_tracks)):
                return ("loading", [])
        if self.tab == TAB_QUEUE:
            return ("queue_tab", self.queue_items)
        if self.tab == TAB_SEARCH:
            return ("tracks", self.search_results)
        if self.tab == TAB_RECOMMENDED:
            return ("tracks", self.recommended_results)
        if self.tab == TAB_MIX:
            return ("tracks", self.mix_tracks)
        if self.tab == TAB_ARTIST:
            items: List[Any] = []
            if self.artist_ctx:
                items.append(("artist_header", self.artist_ctx))
            if self.artist_albums:
                _sep_hint = "), press # to exclude singles/EPs" if self._show_singles_eps else "), press # to include singles/EPs"
                alb_label = f"Albums ({len(self.artist_albums)}" + ("…" if self._loading else "") + _sep_hint
                items.append(("sep", alb_label))
                items.extend(self.artist_albums)
            if self.artist_tracks:
                trk_label = f"Tracks ({len(self.artist_tracks)}" + ("…)" if self._loading else ")")
                items.append(("sep", trk_label))
                items.extend(self.artist_tracks)
            return ("artist_mixed", items)
        if self.tab == TAB_ALBUM:
            items = []
            if self.album_header:
                items.append(("album_title", self.album_header))
            items.extend(self.album_tracks)
            return ("album_mixed", items)
        if self.tab == TAB_LIKED:
            f = self.liked_filter
            if f == 1:
                return ("tracks", self.liked_cache)
            if f == 2:
                return ("liked_mixed", self.liked_artist_cache)
            if f == 3:
                return ("liked_mixed", self.liked_album_cache)
            if f == 4:
                return ("liked_mixed", self.liked_playlist_cache)
            # f == 0: all categories with section separators
            # order: Playlists, Artists, Albums, Tracks
            items: List[Any] = []
            if self.liked_playlist_cache:
                items.append(("sep", "Playlists"))
                items.extend(self.liked_playlist_cache)
            if self.liked_artist_cache:
                items.append(("sep", "Artists"))
                items.extend(self.liked_artist_cache)
            if self.liked_album_cache:
                items.append(("sep", "Albums"))
                items.extend(self.liked_album_cache)
            if self.liked_cache:
                items.append(("sep", "Tracks"))
                items.extend(self.liked_cache)
            return ("liked_mixed", items)
        if self.tab == TAB_PLAYLISTS:
            if self.playlist_view_name is None:
                return ("playlists", self.playlist_names)
            return ("tracks", self.playlist_view_tracks)
        if self.tab == TAB_HISTORY:
            return ("tracks", self.history_tracks)
        if self.tab == TAB_COVER:
            return ("cover_tab", [])
        return ("none", [])

    def _selected_left_item(self) -> Optional[Any]:
        _typ, items = self._left_items()
        if items and 0 <= self.left_idx < len(items):
            return items[self.left_idx]
        return None

    def _selected_left_track(self) -> Optional[Track]:
        it = self._selected_left_item()
        return it if isinstance(it, Track) else None

    def _selected_left_album(self) -> Optional[Album]:
        if self.tab not in (TAB_ARTIST,):
            return None
        it = self._selected_left_item()
        return it if isinstance(it, Album) else None

    def _selected_album_title_line(self) -> bool:
        if self.tab != TAB_ALBUM:
            return False
        it = self._selected_left_item()
        return isinstance(it, tuple) and len(it) == 2 and it[0] == "album_title"

    def _queue_selected_track(self) -> Optional[Track]:
        if not self.queue_items:
            return None
        self.queue_cursor = clamp(self.queue_cursor, 0, len(self.queue_items) - 1)
        return self.queue_items[self.queue_cursor]

    def _current_selection_track(self) -> Optional[Track]:
        if self._queue_context():
            return self._queue_selected_track()
        if self.tab == TAB_COVER:
            return self.current_track
        return self._selected_left_track()

    # ---------------------------------------------------------------------------
    # prompts
    # ---------------------------------------------------------------------------
    def prompt_text(self, title: str, initial: str = "") -> Optional[str]:
        h, w = self.stdscr.getmaxyx()
        box_w = clamp(max(63, len(title) + 8), 34, w - 6)
        box_h = 5
        y0 = (h - box_h) // 2
        x0 = (w - box_w) // 2
        for yy in range(y0, y0 + box_h):
            self.stdscr.addstr(yy, x0, " " * box_w)
        self._erase_popup_bg(max(0, y0-1), max(0, x0-2), min(h-max(0,y0-1), box_h+2), min(w-max(0,x0-2), box_w+4))
        for yy in range(max(0, y0-1), min(h, y0+box_h+1)):
            try:
                self.stdscr.addstr(yy, max(0, x0-2), " " * min(w-max(0,x0-2), box_w+4))
            except curses.error:
                pass
        win = self.stdscr.derwin(box_h, box_w, y0, x0)
        win.keypad(True)
        win.box()
        label = title[:box_w - 4]
        label_len = len(label) + 1
        s = initial
        cur = len(s)
        curses.curs_set(1)
        self.stdscr.nodelay(False)
        inner_w = max(1, box_w - 4 - label_len)
        input_x = 2 + label_len
        hint_text = " ^a/^e: home/end  ^u/^k: clear to left/right  ^w: del word "
        while True:
            view_start = max(0, cur - inner_w + 1) if cur >= inner_w else 0
            display = s[view_start:view_start + inner_w]
            win.addstr(1, 2, label, self.C(4))
            win.addstr(1, 2 + len(label), " ")
            win.addstr(1, input_x, " " * inner_w)
            win.addstr(1, input_x, display)
            try:
                win.addstr(box_h - 1, 2, hint_text[:box_w - 4], self.C(10))
            except curses.error:
                pass
            win.move(1, input_x + min(cur - view_start, inner_w))
            win.refresh()
            ch = self.stdscr.get_wch()
            if isinstance(ch, str) and not ch.isprintable():
                try:
                    ch = ord(ch)
                except TypeError:
                    ch = -1
            if isinstance(ch, str):
                s = s[:cur] + ch + s[cur:]
                cur += 1
            elif ch == 27:
                self.stdscr.nodelay(True)
                while self.stdscr.getch() != -1:
                    pass
                curses.curs_set(0)
                self._need_redraw = True
                self._redraw_status_only = False
                return None
            elif ch in (10, 13):
                curses.curs_set(0)
                self.stdscr.nodelay(True)
                self._need_redraw = True
                self._redraw_status_only = False
                return s.strip()
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
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
                s = s[cur:]
                cur = 0
            elif ch == 23:
                i = cur
                while i > 0 and s[i - 1] == " ":
                    i -= 1
                while i > 0 and s[i - 1] != " ":
                    i -= 1
                s = s[:i] + s[cur:]
                cur = i

    def prompt_yes_no(self, title: str) -> bool:
        h, w = self.stdscr.getmaxyx()
        box_w = clamp(max(30, len(title) + 8), 30, w - 6)
        box_h = 5
        y0 = (h - box_h) // 2
        x0 = (w - box_w) // 2
        pad_y = max(0, y0 - 1)
        pad_x = max(0, x0 - 2)
        pad_h = min(h - pad_y, box_h + 2)
        pad_w = min(w - pad_x, box_w + 4)
        self._erase_popup_bg(pad_y, pad_x, pad_h, pad_w)
        for yy in range(pad_y, pad_y + pad_h):
            try:
                self.stdscr.addstr(yy, pad_x, " " * pad_w)
            except curses.error:
                pass
        win = self.stdscr.derwin(box_h, box_w, y0, x0)
        win.erase()
        win.box()
        win.addstr(2, 2, title[:box_w - 4], self.C(4))
        win.touchwin()
        win.refresh()
        while True:
            ch = self.stdscr.getch()
            if ch in (ord("y"), ord("Y")):
                return True
            if ch in (ord("n"), ord("N"), 27):
                return False

    def pick_playlist(self, title: str, exclude: Optional[str] = None) -> Optional[str]:
        names = [n for n in sorted(self.playlists.keys()) if n != exclude]
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
                if ch in (27, ord("q"), ord("c")):
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

    # ---------------------------------------------------------------------------
    # mpv controls
    # ---------------------------------------------------------------------------
    def _extract_url_from_dash_mpd(self, mpd_xml: str) -> Optional[str]:
        try:
            import xml.etree.ElementTree as ET
            xml_clean = re.sub(r'\s+xmlns[^"]*"[^"]*"', '', mpd_xml)
            xml_clean = re.sub(r'\s+xmlns[^\']*\'[^\']*\'', '', xml_clean)
            root = ET.fromstring(xml_clean)

            def strip_ns(tag: str) -> str:
                return tag.split("}")[-1] if "}" in tag else tag

            base_urls: List[str] = []
            for el in root.iter():
                if strip_ns(el.tag) == "BaseURL" and el.text and el.text.strip().startswith("http"):
                    base_urls.append(el.text.strip())

            debug_log(f"  DASH MPD BaseURLs found: {base_urls[:3]}")

            for rep in root.iter():
                if strip_ns(rep.tag) != "Representation":
                    continue

                seg_base = None
                for child in rep:
                    if strip_ns(child.tag) == "SegmentBase":
                        seg_base = child
                        break
                for child in rep.iter():
                    if strip_ns(child.tag) == "SegmentBase":
                        seg_base = child
                        break

                rep_base_url = None
                for child in rep:
                    if strip_ns(child.tag) == "BaseURL" and child.text and child.text.strip():
                        rep_base_url = child.text.strip()
                        break
                if not rep_base_url and base_urls:
                    rep_base_url = base_urls[0]

                if seg_base is not None and rep_base_url and rep_base_url.startswith("http"):
                    debug_log(f"  DASH: SegmentBase shape, BaseURL={rep_base_url[:80]}")
                    return rep_base_url

                for child in rep.iter():
                    if strip_ns(child.tag) == "SegmentTemplate":
                        debug_log(f"  DASH: SegmentTemplate shape — will use .mpd file")
                        return None

            if base_urls:
                candidate = base_urls[0]
                if re.search(r'\.(flac|m4a|mp4|aac|mp3|ogg)(\?|$)', candidate, re.I):
                    debug_log(f"  DASH: returning bare BaseURL as audio file: {candidate[:80]}")
                    return candidate

        except Exception as e:
            debug_log(f"  DASH MPD parse error: {e}")
        return None

    def _resolve_stream_url_for_quality(self, track_id: int, quality: str) -> str:
        debug_log(f"_resolve_stream_url_for_quality: track_id={track_id} quality={quality}")
        tr = self.client.track(track_id, quality)
        debug_log(f"  /track response keys: {list(tr.keys()) if isinstance(tr, dict) else type(tr)}")
        data = tr.get("data") if isinstance(tr, dict) else None
        if not isinstance(data, dict):
            debug_log(f"  ERROR: invalid /track response, data={str(data)[:200]!r}")
            raise RuntimeError("invalid /track response")
        manifest = data.get("manifest")
        mime = str(data.get("manifestMimeType") or "")
        debug_log(f"  manifestMimeType={mime!r} manifest_head={str(manifest)[:80] if manifest else None!r}")
        if not isinstance(manifest, str) or not manifest:
            debug_log(f"  ERROR: missing manifest")
            raise RuntimeError("missing manifest")

        if manifest.startswith("http"):
            debug_log(f"  manifest is direct URL: {manifest[:80]}")
            return manifest

        try:
            raw = base64.b64decode(manifest.encode("utf-8"))
        except Exception as e:
            debug_log(f"  ERROR: base64 decode failed: {e}")
            raise RuntimeError(f"manifest decode error: {e}")
        raw_text = raw.decode("utf-8", "replace")
        debug_log(f"  decoded payload head: {raw_text[:120]!r}")

        if "tidal.bts" in mime:
            mj = json.loads(raw_text)
            urls = mj.get("urls") or []
            debug_log(f"  bts manifest decoded, {len(urls)} url(s): {urls[0][:80] if urls else 'none'}")
            if not urls:
                raise RuntimeError("manifest has no urls")
            return str(urls[0])

        if "dash" in mime.lower() or raw_text.lstrip().startswith("<?xml") or raw_text.lstrip().startswith("<MPD"):
            debug_log(f"  DASH manifest — parsing MPD XML")
            debug_log(f"  MPD content (first 800 chars): {raw_text[:800]!r}")

            direct_url = self._extract_url_from_dash_mpd(raw_text)
            if direct_url:
                debug_log(f"  DASH: extracted direct URL from MPD: {direct_url[:120]}")
                return direct_url

            mpd_path = f"{os.environ.get('TMPDIR', '/tmp')}/{APP_NAME}-{track_id}-{int(time.time()*1000)}.mpd"
            with open(mpd_path, "w", encoding="utf-8") as f:
                f.write(raw_text)
            debug_log(f"  DASH .mpd path: {mpd_path}")
            return mpd_path

        try:
            mj = json.loads(raw_text)
            urls = mj.get("urls") or []
            debug_log(f"  json manifest decoded, {len(urls)} url(s): {urls[0][:80] if urls else 'none'}")
            if urls:
                return str(urls[0])
        except json.JSONDecodeError:
            pass

        debug_log(f"  ERROR: could not extract stream url from manifest")
        raise RuntimeError("could not extract stream url")

    _QUALITY_FALLBACK_KWS = (
        "stream unavailable", "missing manifest", "invalid /track",
        "manifest has no urls", "could not extract", "manifest decode",
        "non-json", "empty http", "403", "404", "not available",
        "dash playback failed",
    )

    def _apply_mpv_prefs(self) -> None:
        try:
            self.mp.cmd("set_property", "volume", float(self.desired_volume))
        except Exception:
            pass
        try:
            self.mp.cmd("set_property", "mute", bool(self.desired_mute))
        except Exception:
            pass
        try:
            loop_file = "inf" if self.repeat_mode == 2 else "no"
            self.mp.cmd("set_property", "loop-file", loop_file)
        except Exception:
            pass

    def play_track(self, t: Track, resume: bool = False, start_pos: float = 0.0) -> None:
        if self._last_mpd_path:
            try:
                if os.path.exists(self._last_mpd_path):
                    os.unlink(self._last_mpd_path)
            except Exception:
                pass
            self._last_mpd_path = None

        start_qi = self.quality_idx
        last_err = f"no available quality for track {t.id}"
        for qi in range(start_qi, len(QUALITY_ORDER)):
            quality = QUALITY_ORDER[qi]
            mpd_path: Optional[str] = None
            try:
                url = self._resolve_stream_url_for_quality(t.id, quality)
                is_mpd = url.endswith(".mpd") and os.path.isfile(url)
                mpd_path = url if is_mpd else None
                self.mp.start(url, resume=resume, start_pos=start_pos)
                self._apply_mpv_prefs()
                if is_mpd:
                    time.sleep(0.5)
                    if not self.mp.alive():
                        debug_log(f"play_track: mpv died on DASH for {quality} — trying lower quality")
                        raise RuntimeError("dash playback failed")
                # success
                if qi != start_qi:
                    debug_log(f"  Quality fallback: {QUALITY_ORDER[start_qi]} → {quality}")
                    self.toast(f"Quality fallback: {quality}", sec=3.0)
                self._last_mpd_path = mpd_path
                self.current_track = t
                self.last_error = None
                self._need_redraw = True
                self._redraw_status_only = False
                self._record_history(t)
                # Pre-fetch cover art in the background so tab 0 is instant
                self.fetch_cover_async(t)
                # Trigger autoplay logic whenever a new track starts
                self._autoplay_maybe_enqueue()
                return
            except Exception as e:
                err_str = str(e)
                debug_log(f"play_track: quality {quality} failed: {err_str}")
                if mpd_path:
                    try:
                        if os.path.exists(mpd_path):
                            os.unlink(mpd_path)
                    except Exception:
                        pass
                last_err = err_str
                if any(kw in err_str.lower() for kw in self._QUALITY_FALLBACK_KWS):
                    if qi + 1 < len(QUALITY_ORDER):
                        debug_log(f"  Falling back from {quality} to {QUALITY_ORDER[qi + 1]}")
                        continue
                break

        # all qualities failed
        if "Expecting value" in last_err or "JSONDecodeError" in last_err:
            last_err = "stream unavailable"
        debug_log(f"play_track ERROR track_id={t.id}: {last_err}")
        self.last_error = last_err
        self.toast(f"Error: {last_err[:60]}")
        self._need_redraw = True
        self._redraw_status_only = False

    def toggle_pause(self) -> None:
        self.mp.cmd("cycle", "pause")

    def mute_toggle(self) -> None:
        self.mp.cmd("cycle", "mute")

    def volume_add(self, delta: float) -> None:
        self.mp.cmd("add", "volume", float(delta))

    def seek_rel(self, sec: float) -> None:
        self.mp.cmd("seek", float(sec), "relative")

    def play_track_with_resume(self) -> None:
        if not self.current_track and self.queue_items:
            idx = clamp(self.queue_play_idx, 0, len(self.queue_items) - 1)
            t = self.queue_items[idx]
            self.queue_play_idx = idx
            self.play_track(t, resume=True)
            self.toast("Resuming…")
            return
        t = self._current_selection_track() if not self.current_track else self.current_track
        if not t:
            self.toast("No track")
            return
        self.play_track(t, resume=True)
        self.toast("Resuming…")

    # ---------------------------------------------------------------------------
    # priority queue
    # ---------------------------------------------------------------------------
    def _priority_index_of(self, queue_idx: int) -> int:
        try:
            return self.priority_queue.index(queue_idx) + 1
        except ValueError:
            return 0

    def toggle_priority(self, queue_idx: int) -> None:
        if queue_idx in self.priority_queue:
            self.priority_queue.remove(queue_idx)
            self.toast("Priority removed")
        else:
            self.priority_queue.append(queue_idx)
            self.toast(f"Priority {len(self.priority_queue)}")
        self._need_redraw = True
        self._redraw_status_only = False

    def clear_priority_queue(self) -> None:
        n = len(self.priority_queue)
        if not n:
            self.toast("Priority queue empty")
            return
        if self.prompt_yes_no(f"Clear {n} priority track(s)? (y/n)"):
            self.priority_queue.clear()
            self.toast("Priority cleared")
            self._need_redraw = True
            self._redraw_status_only = False

    def _remap_priority_after_delete(self, deleted_indices: List[int]) -> None:
        deleted_set = set(deleted_indices)
        new_pq = []
        for pi in self.priority_queue:
            if pi in deleted_set:
                continue
            shift = sum(1 for d in deleted_indices if d < pi)
            new_pq.append(pi - shift)
        self.priority_queue = new_pq

    def _remap_priority_after_insert(self, insert_pos: int, count: int) -> None:
        self.priority_queue = [pi + count if pi >= insert_pos else pi for pi in self.priority_queue]

    # ---------------------------------------------------------------------------
    # queue playback
    # ---------------------------------------------------------------------------
    def play_queue_index(self, idx: int, start_pos: float = 0.0) -> None:
        if not self.queue_items:
            return
        idx = clamp(idx, 0, len(self.queue_items) - 1)
        prev_play_idx = self.queue_play_idx
        self.queue_play_idx = idx
        self.queue_cursor = idx
        if idx in self.priority_queue:
            self.priority_queue.remove(idx)
        self.play_track(self.queue_items[idx], start_pos=start_pos)
        if self.last_error and (self.current_track is None or self.current_track.id != self.queue_items[idx].id):
            self.queue_play_idx = prev_play_idx
        self._need_redraw = True
        self._redraw_status_only = False

    def next_track(self) -> None:
        if not self.queue_items:
            return
        if self.priority_queue:
            next_idx = self.priority_queue[0]
            self.play_queue_index(next_idx)
            return
        if self.repeat_mode == 2:
            self.play_queue_index(self.queue_play_idx)
            return
        if self.shuffle_on and len(self.queue_items) > 1:
            self.queue_play_idx = random.randrange(0, len(self.queue_items))
        else:
            self.queue_play_idx += 1
            if self.queue_play_idx >= len(self.queue_items):
                if self.repeat_mode == 1:
                    self.queue_play_idx = 0
                else:
                    self.queue_play_idx = len(self.queue_items) - 1
                    return
        self.play_queue_index(self.queue_play_idx)

    def prev_track(self) -> None:
        if not self.queue_items:
            return
        self.queue_play_idx -= 1
        if self.queue_play_idx < 0:
            self.queue_play_idx = len(self.queue_items) - 1 if self.repeat_mode == 1 else 0
        self.play_queue_index(self.queue_play_idx)

    # ---------------------------------------------------------------------------
    # likes
    # ---------------------------------------------------------------------------
    def _record_history(self, t: Track) -> None:
        self.history_tracks = [h for h in self.history_tracks if h.id != t.id]
        self.history_tracks.insert(0, t)
        limit = int(self.settings.get("history_max", 0) or 0)
        if limit > 0:
            self.history_tracks = self.history_tracks[:limit]
        save_history(self.history_tracks)

    def _schedule_liked_refresh(self) -> None:
        self._liked_refresh_due = time.time() + 1.0

    def _save_liked(self) -> None:
        save_liked(self.liked_tracks, self.liked_albums, self.liked_artists, self.liked_playlists)

    def toggle_like(self, t: Track, silent: bool = False) -> None:
        if t.id in self.liked_ids:
            self.liked_ids.discard(t.id)
            self.liked_tracks = [d for d in self.liked_tracks if d.get("id") != t.id]
            if not silent:
                self.toast("Unliked")
        else:
            self.liked_ids.add(t.id)
            self.liked_tracks.insert(0, track_to_mono(t, int(time.time() * 1000)))
            if not silent:
                self.toast("Liked")
        self._save_liked()
        self._schedule_liked_refresh()

    def toggle_like_album(self, album: Album) -> None:
        if album.id in self.liked_album_ids:
            self.liked_album_ids.discard(album.id)
            self.liked_albums = [d for d in self.liked_albums if d.get("id") != album.id]
            self.toast("Album unliked")
        else:
            self.liked_album_ids.add(album.id)
            self.liked_albums.insert(0, {"id": album.id, "title": album.title, "artist": album.artist, "year": album.year})
            self.toast("Album liked")
        self._save_liked()
        self._schedule_liked_refresh()

    def toggle_like_artist(self, artist_id: int, name: str) -> None:
        if artist_id in self.liked_artist_ids:
            self.liked_artist_ids.discard(artist_id)
            self.liked_artists = [d for d in self.liked_artists if d.get("id") != artist_id]
            self.toast("Artist unliked")
        else:
            self.liked_artist_ids.add(artist_id)
            self.liked_artists.insert(0, {"id": artist_id, "name": name})
            self.toast("Artist liked")
        self._save_liked()
        self._schedule_liked_refresh()

    def toggle_like_playlist(self, name: str) -> None:
        if name in self.liked_playlist_ids:
            self.liked_playlist_ids.discard(name)
            self.liked_playlists = [d for d in self.liked_playlists if d.get("name") != name]
            self.toast("Playlist unliked")
        else:
            self.liked_playlist_ids.add(name)
            pl_id = (self.playlists_meta.get(name) or {}).get("id", str(uuid.uuid4()))
            self.liked_playlists.insert(0, {"name": name, "id": pl_id})
            self.toast("Playlist liked")
        self._save_liked()
        self._schedule_liked_refresh()

    def like_selected(self) -> None:
        if not self._queue_context():
            if self.tab == TAB_COVER:
                if self.current_track:
                    self.toggle_like(self.current_track)
                return
            marked_albums = self._marked_albums_from_left()
            marked_artists = self._marked_artists_from_left()
            marked_playlists = self._marked_playlists_from_left()
            marked_albums, marked_artists, marked_playlists, cancelled = \
                self._resolve_batch_conflict(marked_albums, marked_artists, marked_playlists)
            if cancelled:
                return
            if marked_albums:
                for alb in marked_albums:
                    self.toggle_like_album(alb)
                self.toast(f"Liked/unliked {len(marked_albums)} albums")
                self._need_redraw = True
                self._redraw_status_only = False
                return
            if marked_artists:
                for ar in marked_artists:
                    self.toggle_like_artist(ar.id, ar.name)
                self.toast(f"Liked/unliked {len(marked_artists)} artists")
                self._need_redraw = True
                self._redraw_status_only = False
                return
            if marked_playlists:
                for pl in marked_playlists:
                    self.toggle_like_playlist(pl)
                self.toast(f"Liked/unliked {len(marked_playlists)} playlists")
                self._need_redraw = True
                self._redraw_status_only = False
                return
            it = self._selected_left_item()
            # Artist header row in artist_mixed view
            if isinstance(it, tuple) and it[0] == "artist_header":
                if self.artist_ctx:
                    self.toggle_like_artist(*self.artist_ctx)
                return
            # Album header tuple (album_mixed)
            if isinstance(it, tuple) and len(it) == 2 and it[0] == "album_title" and isinstance(it[1], Album):
                self.toggle_like_album(it[1])
                return
            # Album object (in liked_mixed or artist view)
            if isinstance(it, Album):
                self.toggle_like_album(it)
                return
            # Artist object (in liked_mixed)
            if isinstance(it, Artist):
                self.toggle_like_artist(it.id, it.name)
                return
            # Playlist name string: in playlists list view or liked_mixed
            if isinstance(it, str) and (
                (self.tab == TAB_PLAYLISTS and self.playlist_view_name is None)
                or self.tab == TAB_LIKED
            ):
                self.toggle_like_playlist(it)
                return
            # Fall through: like marked or selected tracks
            marked = self._marked_tracks_from_left()
            if marked:
                for t in marked:
                    self.toggle_like(t, silent=True)
                self.toast(f"Liked/unliked {len(marked)}")
                return
            t = self._selected_left_track()
            if t:
                self.toggle_like(t)
            return
        marked = self._marked_tracks_from_queue()
        if marked:
            for t in marked:
                self.toggle_like(t, silent=True)
            self.toast(f"Liked/unliked {len(marked)}")
            return
        t = self._queue_selected_track()
        if t:
            self.toggle_like(t)

    def like_playing(self) -> None:
        if self.current_track:
            self.toggle_like(self.current_track)

    # ---------------------------------------------------------------------------
    # marking
    # ---------------------------------------------------------------------------
    def mark_all_current_view(self) -> None:
        if self._queue_context():
            self.marked_queue_idx = set(range(len(self.queue_items)))
        else:
            typ, items = self._left_items()
            sel = self._selected_left_item()
            if isinstance(sel, Album):
                self.marked_left_idx = {i for i, it in enumerate(items) if isinstance(it, Album)}
            elif isinstance(sel, Artist):
                self.marked_left_idx = {i for i, it in enumerate(items) if isinstance(it, Artist)}
            elif isinstance(sel, str):
                self.marked_left_idx = {i for i, it in enumerate(items) if isinstance(it, str)}
            else:
                self.marked_left_idx = {i for i, it in enumerate(items) if isinstance(it, Track)}
        self.toast("Marked all")
        self._need_redraw = True
        self._redraw_status_only = False

    def unmark_all_current_view(self) -> None:
        if self._queue_context():
            self.marked_queue_idx.clear()
        else:
            typ, items = self._left_items()
            sel = self._selected_left_item()
            if isinstance(sel, Album):
                self.marked_left_idx = {i for i in self.marked_left_idx
                                        if not (0 <= i < len(items) and isinstance(items[i], Album))}
            elif isinstance(sel, Artist):
                self.marked_left_idx = {i for i in self.marked_left_idx
                                        if not (0 <= i < len(items) and isinstance(items[i], Artist))}
            elif isinstance(sel, str):
                self.marked_left_idx = {i for i in self.marked_left_idx
                                        if not (0 <= i < len(items) and isinstance(items[i], str))}
            else:
                self.marked_left_idx = {i for i in self.marked_left_idx
                                        if not (0 <= i < len(items) and isinstance(items[i], Track))}
        self.toast("Unmarked")
        self._need_redraw = True
        self._redraw_status_only = False

    def toggle_mark_and_advance(self) -> None:
        if self._queue_context():
            if not self.queue_items:
                return
            i = clamp(self.queue_cursor, 0, len(self.queue_items) - 1)
            if i in self.marked_queue_idx:
                self.marked_queue_idx.remove(i)
            else:
                self.marked_queue_idx.add(i)
            self.queue_cursor = clamp(self.queue_cursor + 1, 0, len(self.queue_items) - 1)
        else:
            typ, items = self._left_items()
            if not items:
                return
            i = clamp(self.left_idx, 0, len(items) - 1)
            if isinstance(items[i], (Track, Album, Artist, str)):
                if i in self.marked_left_idx:
                    self.marked_left_idx.remove(i)
                else:
                    self.marked_left_idx.add(i)
                self.left_idx = clamp(self.left_idx + 1, 0, len(items) - 1)
        self._need_redraw = True
        self._redraw_status_only = False

    def _marked_tracks_from_left(self) -> List[Track]:
        typ, items = self._left_items()
        return [items[i] for i in sorted(self.marked_left_idx) if 0 <= i < len(items) and isinstance(items[i], Track)]

    def _marked_albums_from_left(self) -> List[Album]:
        typ, items = self._left_items()
        return [items[i] for i in sorted(self.marked_left_idx) if 0 <= i < len(items) and isinstance(items[i], Album)]

    def _marked_artists_from_left(self) -> List[Artist]:
        typ, items = self._left_items()
        return [items[i] for i in sorted(self.marked_left_idx) if 0 <= i < len(items) and isinstance(items[i], Artist)]

    def _marked_playlists_from_left(self) -> List[str]:
        typ, items = self._left_items()
        return [items[i] for i in sorted(self.marked_left_idx) if 0 <= i < len(items) and isinstance(items[i], str)]

    def _cursor_item_type(self) -> str:
        """Returns 'album', 'artist', 'playlist', 'track', or '' for the left panel cursor."""
        it = self._selected_left_item()
        if isinstance(it, tuple):
            if it[0] == "artist_header":
                return "artist"
            if it[0] == "album_title":
                return "album"
        if isinstance(it, Album):
            return "album"
        if isinstance(it, Artist):
            return "artist"
        if isinstance(it, str):
            return "playlist"
        if self._selected_left_track():
            return "track"
        return ""

    def _prompt_marked_or_cursor(self, n: int, marked_desc: str, cursor_desc: str) -> str:
        """Show conflict prompt. Returns 'marked', 'cursor', or '' (cancelled)."""
        idx = self.pick_from_list(
            "Apply to:",
            [f"{n} marked {marked_desc}", f"Cursor ({cursor_desc})"],
        )
        if idx == 0:
            return "marked"
        if idx == 1:
            return "cursor"
        return ""

    def _resolve_batch_conflict(
        self,
        marked_albums: List[Album],
        marked_artists: List[Artist],
        marked_playlists: List[str],
    ):
        """If marked non-track items conflict with cursor type, prompt and resolve.
        Returns (marked_albums, marked_artists, marked_playlists, cancelled)."""
        ctype = self._cursor_item_type()
        for items, mtype in [
            (marked_albums, "album"),
            (marked_artists, "artist"),
            (marked_playlists, "playlist"),
        ]:
            if items and ctype and ctype != mtype:
                r = self._prompt_marked_or_cursor(len(items), f"{mtype}s", ctype)
                if r == "":
                    return [], [], [], True
                if r == "cursor":
                    if mtype == "album":
                        marked_albums = []
                    elif mtype == "artist":
                        marked_artists = []
                    else:
                        marked_playlists = []
                break
        return marked_albums, marked_artists, marked_playlists, False

    def _marked_tracks_from_queue(self) -> List[Track]:
        return [self.queue_items[i] for i in sorted(self.marked_queue_idx) if 0 <= i < len(self.queue_items)]

    # ---------------------------------------------------------------------------
    # enqueue
    # ---------------------------------------------------------------------------
    def _enqueue_tracks(self, tracks: List[Track], insert_after_playing: bool) -> None:
        if not tracks:
            return
        if not insert_after_playing:
            self.queue_items.extend(tracks)
            if len(self.queue_items) == len(tracks):
                self.queue_play_idx = 0
                self.queue_cursor = 0
            self.toast(f"Enqueued {len(tracks)}")
        else:
            if self.queue_items and 0 <= self.queue_play_idx < len(self.queue_items):
                ins = self.queue_play_idx + 1
                for i, t in enumerate(tracks):
                    self.queue_items.insert(ins + i, t)
                self._remap_priority_after_insert(ins, len(tracks))
                if self.marked_queue_idx:
                    self.marked_queue_idx = {mi + len(tracks) if mi >= ins else mi for mi in self.marked_queue_idx}
                if self.queue_cursor >= ins:
                    self.queue_cursor += len(tracks)
                self.toast(f"Enqueued+ {len(tracks)}")
            else:
                self.queue_items.extend(tracks)
                if len(self.queue_items) == len(tracks):
                    self.queue_play_idx = 0
                    self.queue_cursor = 0
                self.toast(f"Enqueued+ {len(tracks)}")
        self._need_redraw = True
        self._redraw_status_only = False

    def enqueue_album_async(self, album: Album, insert_after_playing: bool) -> None:
        self.toast("Album…")
        def worker() -> None:
            aid = self._resolve_album_id_for_album(album)
            if not aid:
                self.toast("Album id?")
                return
            tracks = self._fetch_album_tracks_by_album_id(aid)
            self._enqueue_tracks(tracks, insert_after_playing)
        self._bg(worker)

    def _download_marked_artists_async(self, artists: List[Artist]) -> None:
        self.toast(f"Fetching {len(artists)} artists…")
        def worker() -> None:
            all_tracks: List[Track] = []
            for artist in artists:
                aid = artist.id
                if not aid and artist.track_id:
                    try:
                        info = self.client.info(artist.track_id)
                        data = info.get("data") if isinstance(info, dict) else None
                        if isinstance(data, dict):
                            a = data.get("artist")
                            if isinstance(a, dict) and str(a.get("id", "")).isdigit():
                                aid = int(a["id"])
                    except Exception:
                        pass
                if aid:
                    _albums, tracks = self._fetch_artist_catalog_by_artist_id(aid)
                    all_tracks.extend(tracks)
            if all_tracks:
                self.start_download_tracks(self._dedupe_tracks(all_tracks))
            else:
                self.toast("No tracks")
        self._bg(worker)

    def _download_marked_albums_async(self, albums: List[Album]) -> None:
        self.toast(f"Fetching {len(albums)} albums…")
        def worker() -> None:
            all_tracks: List[Track] = []
            for album in albums:
                aid = self._resolve_album_id_for_album(album)
                if aid:
                    all_tracks.extend(self._fetch_album_tracks_by_album_id(aid))
            if all_tracks:
                self.start_download_tracks(all_tracks)
            else:
                self.toast("No tracks")
        self._bg(worker)

    def _open_artist_from_album_async(self, album: Album) -> None:
        """Switch to Artist tab and load the artist, resolving artist_id via the album API."""
        self.switch_tab(TAB_ARTIST, refresh=False)
        self.toast("Loading artist…")
        def worker() -> None:
            ar_id: Optional[int] = None
            ar_name = album.artist
            aid = album.id if album.id else None
            if not aid and album.track_id:
                aid = self._resolve_album_id_for_album(album)
            if aid:
                payload = self.client.album(aid)
                data = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(data, dict):
                    ar = data.get("artist")
                    if isinstance(ar, dict) and ar.get("id"):
                        ar_id = int(ar["id"])
                        ar_name = ar.get("name") or ar_name
                    if not ar_id:
                        ars = data.get("artists")
                        if isinstance(ars, list) and ars and isinstance(ars[0], dict):
                            if ars[0].get("id"):
                                ar_id = int(ars[0]["id"])
                                ar_name = ars[0].get("name") or ar_name
            ctx = Track(id=album.track_id or 0, title="", artist=ar_name,
                        album="", year="????", track_no=0, artist_id=ar_id or 0)
            self.fetch_artist_async(ctx)
        self._bg(worker)

    def _enqueue_marked_artists_async(self, artists: List[Artist], insert_after_playing: bool) -> None:
        self.toast(f"Fetching {len(artists)} artists…")
        def worker() -> None:
            all_tracks: List[Track] = []
            for artist in artists:
                aid = artist.id
                if not aid and artist.track_id:
                    try:
                        info = self.client.info(artist.track_id)
                        data = info.get("data") if isinstance(info, dict) else None
                        if isinstance(data, dict):
                            a = data.get("artist")
                            if isinstance(a, dict) and str(a.get("id", "")).isdigit():
                                aid = int(a["id"])
                    except Exception:
                        pass
                if aid:
                    _albums, tracks = self._fetch_artist_catalog_by_artist_id(aid)
                    all_tracks.extend(tracks)
                else:
                    payload2 = self.client.search_tracks(artist.name, limit=200)
                    a0 = artist.name.strip().lower()
                    all_tracks.extend(t for t in self._extract_tracks_from_search(payload2)
                                      if t.artist.strip().lower() == a0)
            if all_tracks:
                self._enqueue_tracks(self._dedupe_tracks(all_tracks), insert_after_playing)
            else:
                self.toast("No tracks")
        self._bg(worker)

    def _enqueue_marked_albums_async(self, albums: List[Album], insert_after_playing: bool) -> None:
        self.toast(f"Fetching {len(albums)} albums…")
        def worker() -> None:
            all_tracks: List[Track] = []
            for album in albums:
                aid = self._resolve_album_id_for_album(album)
                if aid:
                    all_tracks.extend(self._fetch_album_tracks_by_album_id(aid))
            if all_tracks:
                self._enqueue_tracks(all_tracks, insert_after_playing)
            else:
                self.toast("No tracks")
        self._bg(worker)

    def enqueue_key(self, insert_after_playing: bool) -> None:
        if not self._queue_context():
            marked_albums = self._marked_albums_from_left()
            marked_artists = self._marked_artists_from_left()
            marked_playlists = self._marked_playlists_from_left()
            marked_albums, marked_artists, marked_playlists, cancelled = \
                self._resolve_batch_conflict(marked_albums, marked_artists, marked_playlists)
            if cancelled:
                return
            if marked_albums:
                self._enqueue_marked_albums_async(marked_albums, insert_after_playing)
                return
            if marked_artists:
                self._enqueue_marked_artists_async(marked_artists, insert_after_playing)
                return
            if marked_playlists:
                all_tracks: List[Track] = []
                for pl in marked_playlists:
                    all_tracks.extend(self.playlists.get(pl, []))
                self._enqueue_tracks(all_tracks, insert_after_playing)
                return
            it = self._selected_left_item()
            # Artist header row in Artist tab
            if isinstance(it, tuple) and it[0] == "artist_header":
                artist_id, name = it[1]
                self._enqueue_artist_async(Artist(id=artist_id, name=name),
                                           insert_after_playing)
                return
            # Album title row in Album tab
            if isinstance(it, tuple) and it[0] == "album_title" and isinstance(it[1], Album):
                self.enqueue_album_async(it[1], insert_after_playing)
                return
            if self.tab == TAB_LIKED:
                if isinstance(it, Album):
                    self.enqueue_album_async(it, insert_after_playing)
                    return
                if isinstance(it, Artist):
                    self._enqueue_artist_async(it, insert_after_playing)
                    return
                if isinstance(it, str):
                    self._enqueue_playlist_async(it, insert_after_playing)
                    return
            if self.tab == TAB_ARTIST:
                alb = self._selected_left_album()
                if alb:
                    self.enqueue_album_async(alb, insert_after_playing)
                    return
            if self.tab == TAB_ALBUM and self._selected_album_title_line() and self.album_header:
                self.enqueue_album_async(self.album_header, insert_after_playing)
                return
            if self.tab == TAB_PLAYLISTS and self.playlist_view_name is None:
                name = self.playlist_names[clamp(self.left_idx, 0, len(self.playlist_names) - 1)] if self.playlist_names else None
                if name:
                    self._enqueue_playlist_async(name, insert_after_playing)
                    return
            if self.tab == TAB_PLAYLISTS and self.playlist_view_name is not None and self.playlist_view_tracks:
                self._enqueue_tracks(list(self.playlist_view_tracks), insert_after_playing)
                return

        if self._queue_context():
            tracks = self._marked_tracks_from_queue() or ([self._queue_selected_track()] if self._queue_selected_track() else [])
        else:
            tracks = self._marked_tracks_from_left() or ([self._selected_left_track()] if self._selected_left_track() else [])
            tracks = [t for t in tracks if t is not None]
        self._enqueue_tracks(tracks, insert_after_playing)

    def _enqueue_artist_async(self, artist: Artist, insert_after_playing: bool) -> None:
        self.toast("Artist…")
        def worker() -> None:
            tracks: List[Track] = []
            aid = artist.id
            if not aid and artist.track_id:
                try:
                    info = self.client.info(artist.track_id)
                    data = info.get("data") if isinstance(info, dict) else None
                    if isinstance(data, dict):
                        a = data.get("artist")
                        if isinstance(a, dict) and str(a.get("id", "")).isdigit():
                            aid = int(a["id"])
                except Exception:
                    pass
            if aid:
                _albums, tracks = self._fetch_artist_catalog_by_artist_id(aid)
            if not tracks:
                payload2 = self.client.search_tracks(artist.name, limit=300)
                a0 = artist.name.strip().lower()
                tracks = [t for t in self._extract_tracks_from_search(payload2)
                          if t.artist.strip().lower() == a0]
            if tracks:
                def _yr2(t: Track) -> int:
                    y = year_norm(t.year)
                    return int(y) if y.isdigit() else 9999
                tracks = self._dedupe_tracks(tracks)
                tracks = sorted(tracks, key=lambda t: (_yr2(t), t.album.lower(), t.track_no or 9999, t.title.lower()))
                self._enqueue_tracks(tracks, insert_after_playing)
            else:
                self.toast("No tracks")
        self._bg(worker)

    def _download_artist_async(self, artist: Artist) -> None:
        self.toast("Artist DL…")
        def worker() -> None:
            tracks: List[Track] = []
            aid = artist.id
            if not aid and artist.track_id:
                try:
                    info = self.client.info(artist.track_id)
                    data = info.get("data") if isinstance(info, dict) else None
                    if isinstance(data, dict):
                        a = data.get("artist")
                        if isinstance(a, dict) and str(a.get("id", "")).isdigit():
                            aid = int(a["id"])
                except Exception:
                    pass
            if aid:
                _albums, tracks = self._fetch_artist_catalog_by_artist_id(aid)
            if not tracks:
                payload2 = self.client.search_tracks(artist.name, limit=300)
                a0 = artist.name.strip().lower()
                tracks = [t for t in self._extract_tracks_from_search(payload2)
                          if t.artist.strip().lower() == a0]
            if tracks:
                self.start_download_tracks(self._dedupe_tracks(tracks))
            else:
                self.toast("No tracks")
        self._bg(worker)

    def save_mix_as_playlist_async(self, name: str, seed: Any) -> None:
        """Create a playlist from a mix seed (Track/Album/Artist), save to tab 8 and liked."""
        now_ms = int(time.time() * 1000)
        if name in self.playlists:
            self.toast("Name exists")
            return
        self.playlists[name] = []
        self.playlists_meta[name] = {"id": str(uuid.uuid4()), "createdAt": now_ms}
        save_playlists(self.playlists, self.playlists_meta)
        self.playlist_names = sorted(self.playlists.keys())
        # Also mark as liked so it appears in Liked → Playlists & mixes
        if name not in self.liked_playlist_ids:
            self.liked_playlist_ids.add(name)
            self.liked_playlists.insert(0, {"name": name, "id": self.playlists_meta[name]["id"]})
            self._save_liked()
        self.toast("Mix saved (loading tracks…)")
        self._need_redraw = True
        self._redraw_status_only = False

        def worker() -> None:
            try:
                tracks: List[Track] = []
                if isinstance(seed, Track):
                    mix_payload = self._fetch_track_mix_payload_for_track(seed)
                    if mix_payload:
                        tracks = self._extract_tracks_from_mix_payload(mix_payload)
                elif isinstance(seed, Album):
                    payload = None
                    if seed.id and seed.id > 0:
                        try:
                            payload = self.client.album(int(seed.id))
                        except Exception:
                            pass
                    mix_id = self._extract_mix_id_from_payload(payload) if payload else None
                    if mix_id:
                        tracks = self._extract_tracks_from_mix_payload(self.client.mix(mix_id))
                    elif payload:
                        album_tracks = self._extract_tracks_from_album_payload(payload)
                        if album_tracks:
                            mix_payload = self._fetch_track_mix_payload_for_track(album_tracks[0])
                            if mix_payload:
                                tracks = self._extract_tracks_from_mix_payload(mix_payload)
                elif isinstance(seed, Artist):
                    artist_payload = None
                    if seed.id and seed.id > 0:
                        try:
                            artist_payload = self.client.artist(int(seed.id))
                        except Exception:
                            pass
                    mix_id = self._extract_mix_id_from_payload(artist_payload) if artist_payload else None
                    if mix_id:
                        tracks = self._extract_tracks_from_mix_payload(self.client.mix(mix_id))
                    elif artist_payload:
                        dicts: List[Dict[str, Any]] = []
                        self._scan_for_track_dicts(artist_payload, dicts, limit=5)
                        for d in dicts:
                            t = self._parse_track_obj(d)
                            if t:
                                mix_payload = self._fetch_track_mix_payload_for_track(t)
                                if mix_payload:
                                    tracks = self._extract_tracks_from_mix_payload(mix_payload)
                                    break
                if tracks:
                    self.playlists[name] = tracks
                    save_playlists(self.playlists, self.playlists_meta)
                    self.playlist_names = sorted(self.playlists.keys())
                    self.toast(f"Mix '{name}': {len(tracks)} tracks saved")
                else:
                    self.toast(f"Mix '{name}' saved (no tracks found)")
                self._need_redraw = True
                self._redraw_status_only = False
            except Exception as e:
                self.last_error = str(e)
                self.toast("Mix save error")

        threading.Thread(target=worker, daemon=True).start()

    def open_artist_by_id(self, artist_id: int, name: str) -> None:
        ctx = Track(id=0, title="", artist=name, album="", year="????",
                    track_no=0, artist_id=artist_id)
        self.switch_tab(TAB_ARTIST, refresh=False)
        self.fetch_artist_async(ctx)

    def playlists_open_by_name(self, name: str) -> None:
        self.switch_tab(TAB_PLAYLISTS, refresh=False)
        self.playlist_names = sorted(self.playlists.keys())
        self.left_idx = 0
        self.left_scroll = 0
        if name in self.playlists:
            self.playlist_view_name = name
            self.playlist_view_tracks = []
            self.fetch_playlist_tracks_async(name)
        else:
            self.playlist_view_name = None
            self.playlist_view_tracks = []
            self._need_redraw = True
            self._redraw_status_only = False

    def pick_from_list(self, title: str, options: List[str], simple: bool = False, cancel_keys: tuple = ()) -> int:
        """Pager-like selection popup.

        Normal mode: j/^n/^n/↓ navigate down; k/^k/^p/↑ navigate up;
                     Enter(CR=13) confirms; letter shortcuts [x] trigger action;
                     / enters filter mode; Esc cancels.
        Filter mode: type to fuzzy-filter; Enter/^n confirm; Esc goes back;
                     Backspace deletes (empty filter → back to normal mode).
        """
        if not options:
            return -1

        def _fuzzy(q: str, s: str) -> bool:
            s_l = s.lower()
            qi = 0
            for c in s_l:
                if qi < len(q) and c == q[qi]:
                    qi += 1
            return qi == len(q)

        def _shortcut(label: str) -> Optional[str]:
            """Extract single-char shortcut from trailing [x]."""
            if len(label) >= 3 and label[-1] == "]" and label[-3] == "[":
                return label[-2]
            return None

        h, w = self.stdscr.getmaxyx()
        box_w = min(w - 6, max(52, max(len(s) for s in options) + 8))
        extra_rows = 3 if simple else 4   # title + hint (simple: no filter bar)
        box_h = min(h - 6, max(6, len(options) + extra_rows))
        y0 = (h - box_h) // 2
        x0 = (w - box_w) // 2
        pad_y = max(0, y0 - 1)
        pad_x = max(0, x0 - 2)
        pad_h = min(h - pad_y, box_h + 2)
        pad_w = min(w - pad_x, box_w + 4)
        self._erase_popup_bg(pad_y, pad_x, pad_h, pad_w)
        for yy in range(pad_y, pad_y + pad_h):
            try:
                self.stdscr.addstr(yy, pad_x, " " * pad_w)
            except curses.error:
                pass
        win = self.stdscr.derwin(box_h, box_w, y0, x0)
        win.keypad(True)
        idx = 0
        filt = ""
        filt_active = False
        self.stdscr.nodelay(False)
        try:
            while True:
                if not simple and filt_active and filt:
                    visible: List[Tuple[int, str]] = [
                        (i, s) for i, s in enumerate(options) if _fuzzy(filt.lower(), s)
                    ]
                else:
                    visible = list(enumerate(options))
                idx = clamp(idx, 0, max(0, len(visible) - 1))

                win.erase()
                win.box()
                win.addstr(0, 2, f" {title} "[:box_w - 2], self.C(4))

                if not simple:
                    # filter bar
                    if filt_active:
                        filt_disp = f"/{filt}_"
                        filt_attr = self.C(5)
                        hint = f" {len(visible)}/{len(options)}  ^n/^p: navigate  Enter: select  Esc: back "
                    else:
                        filt_disp = "Type / to filter"
                        filt_attr = self.C(10)
                        hint = " j/k ^n/^p: navigate   /: filter   Esc/q: close "
                    win.addstr(1, 2, filt_disp[:box_w - 4], filt_attr)

                item_row = 1 if simple else 2
                inner_h = box_h - (3 if simple else 4)
                scroll = max(0, idx - inner_h + 1) if idx >= inner_h else 0
                for row in range(inner_h):
                    fi = scroll + row
                    if fi >= len(visible):
                        break
                    attr = curses.A_REVERSE if fi == idx else 0
                    win.addstr(item_row + row, 2, visible[fi][1][:box_w - 4].ljust(box_w - 4), attr)

                if simple:
                    win.addstr(box_h - 1, 2, " j/k ^n/^p: navigate   Enter: select   Esc/q: close "[:box_w - 4], self.C(10))
                else:
                    win.addstr(box_h - 1, 2, hint[:box_w - 4], self.C(10))
                win.touchwin()
                win.refresh()

                ch = self.stdscr.get_wch()
                if isinstance(ch, str) and not ch.isprintable():
                    try:
                        ch = ord(ch)
                    except TypeError:
                        ch = -1

                # Esc always closes the popup regardless of mode
                if ch == 27:
                    return -1

                if not simple and filt_active:
                    # ^N(14)/↓ navigate down; ^P(16)/^K(11)/↑ navigate up; Enter confirms
                    nav_down = ch in (curses.KEY_DOWN, 14)
                    nav_up   = ch in (curses.KEY_UP, 11, 16)
                    if nav_down:
                        idx = min(idx + 1, max(0, len(visible) - 1))
                    elif nav_up:
                        idx = max(idx - 1, 0)
                    elif ch in (10, 13):                            # Enter = confirm
                        return visible[idx][0] if visible else -1
                    elif isinstance(ch, str):
                        filt += ch
                        idx = 0
                    elif ch in (curses.KEY_BACKSPACE, 127, 8):
                        filt = filt[:-1]
                        if not filt:                                # empty filter → back to normal
                            orig = visible[idx][0] if visible else 0
                            filt_active = False
                            idx = orig
                else:
                    # j/^N(14)/↓ navigate down; k/^P(16)/^K(11)/↑ navigate up
                    nav_down = ch in (curses.KEY_DOWN, 14, "j")
                    nav_up   = ch in (curses.KEY_UP, 11, 16, "k")
                    if nav_down:
                        idx = min(idx + 1, max(0, len(visible) - 1))
                    elif nav_up:
                        idx = max(idx - 1, 0)
                    elif ch in (10, 13):                            # Enter (CR or LF) = confirm
                        return visible[idx][0] if visible else -1
                    elif ch in ("q", "c") or (cancel_keys and ch in cancel_keys):  # close
                        return -1
                    elif not simple and ch in ("/", ":", "!"):      # enter filter mode
                        filt_active = True
                        filt = ""
                        idx = 0
                    elif not simple and isinstance(ch, str) and ch not in ("j", "k", "/", ":", "!", "q", "c"):
                        # shortcut key [x] matching
                        for fi, (oi, label) in enumerate(visible):
                            sc = _shortcut(label)
                            if sc and sc == ch:
                                return oi
        finally:
            self.stdscr.nodelay(True)
            self._need_redraw = True
            self._redraw_status_only = False

    def like_popup_from_playing(self) -> None:
        t = self._current_selection_track()
        if not t:
            self.toast("No track selected")
            return
        artist_id = t.artist_id or self.meta.artist_id.get(t.id, 0) or 0
        options = [
            f"Artist: {t.artist}",
            f"Album: {t.album}",
            "Store mix in playlist and like…",
        ]
        choice = self.pick_from_list("Like/unlike", options, simple=True, cancel_keys=("*",))
        if choice == 0:
            self.toggle_like_artist(artist_id, t.artist)
        elif choice == 1:
            album_obj = Album(
                id=t.album_id or self.meta.album_id.get(t.id, 0) or 0,
                title=t.album, artist=t.artist, year=t.year,
            )
            self.toggle_like_album(album_obj)
        elif choice == 2:
            default_name = f"(Mix) {t.artist} - {t.title}"
            name = self.prompt_text("Mix name:", default_name)
            if name:
                self.save_mix_as_playlist_async(name, t)

    def context_actions_popup(self) -> None:
        """Show a context-sensitive actions popup for the current selection."""
        if self._queue_context():
            it = self._queue_selected_track()
        elif self.tab == TAB_COVER:
            it = self.current_track
        else:
            it = self._selected_left_item()

        # Unwrap artist_header tuple → Artist
        if isinstance(it, tuple) and it[0] == "artist_header":
            artist_id, name = it[1]
            it = Artist(id=artist_id, name=name)
        # Unwrap album_title tuple → Album
        elif isinstance(it, tuple) and it[0] == "album_title" and isinstance(it[1], Album):
            it = it[1]

        # Determine context label and build action list
        if isinstance(it, Track):
            artist_id = it.artist_id or self.meta.artist_id.get(it.id, 0) or 0
            album_id = it.album_id or self.meta.album_id.get(it.id, 0) or 0
            liked_t = self.is_liked(it.id)
            liked_ar = artist_id in self.liked_artist_ids
            album_obj = Album(id=album_id, title=it.album, artist=it.artist, year=it.year,
                              track_id=it.id if not album_id else None)
            liked_al = album_obj.id in self.liked_album_ids
            title = f"{it.artist} — {it.title}"
            artist_obj = Artist(id=artist_id, name=it.artist,
                                track_id=it.id if not artist_id else None)
            options = [
                "Go to Recommended tab [3]",
                "Go to Mix tab [4]",
                "Go to Artist tab [5]",
                "Go to Album tab [6]",
                "Enqueue [e]",
                "Enqueue next [E]",
                "Enqueue album [a]",
                "Enqueue album next [A]",
                "Enqueue all artist' tracks",
                "Enqueue all artist' tracks next",
                f"{'Unlike' if liked_t else 'Like'} track [l]",
                f"{'Unlike' if liked_ar else 'Like'} artist",
                f"{'Unlike' if liked_al else 'Like'} album",
                "Add to playlist [p]",
                "Download [d]",
                "Download album [D]",
                "Download artist",
                "Similar artists [s]",
                "Like & save corresponding mix…",
            ]
            choice = self.pick_from_list(title, options)
            if choice == 0:
                self.switch_tab(TAB_RECOMMENDED, refresh=False)
                self.fetch_recommended_async(it)
            elif choice == 1:
                self.switch_tab(TAB_MIX, refresh=False)
                self.fetch_mix_async(it)
            elif choice == 2:
                self.switch_tab(TAB_ARTIST, refresh=False)
                self.fetch_artist_async(it)
            elif choice == 3:
                self.open_album_from_track(it)
            elif choice == 4:
                self._enqueue_tracks([it], insert_after_playing=False)
            elif choice == 5:
                self._enqueue_tracks([it], insert_after_playing=True)
            elif choice == 6:
                self.enqueue_album_async(album_obj, insert_after_playing=False)
            elif choice == 7:
                self.enqueue_album_async(album_obj, insert_after_playing=True)
            elif choice == 8:
                self._enqueue_artist_async(artist_obj, insert_after_playing=False)
            elif choice == 9:
                self._enqueue_artist_async(artist_obj, insert_after_playing=True)
            elif choice == 10:
                self.toggle_like(it)
            elif choice == 11:
                self.toggle_like_artist(artist_id, it.artist)
            elif choice == 12:
                self.toggle_like_album(album_obj)
            elif choice == 13:
                self.playlists_add_tracks([it])
            elif choice == 14:
                self.start_download_tracks([it])
            elif choice == 15:
                def _dl_album() -> None:
                    try:
                        aid = self._resolve_album_id_for_album(album_obj)
                        if aid:
                            self.start_download_tracks(self._fetch_album_tracks_by_album_id(aid))
                        else:
                            self.toast("Album id?")
                    except Exception:
                        self.toast("Error")
                threading.Thread(target=_dl_album, daemon=True).start()
            elif choice == 16:
                self._download_artist_async(artist_obj)
            elif choice == 17:
                self.show_similar_artists_dialog(artist_obj)
            elif choice == 18:
                default_name = f"(Mix) {it.artist} - {it.title}"
                name = self.prompt_text("Mix name:", default_name)
                if name:
                    self.save_mix_as_playlist_async(name, it)

        elif isinstance(it, Album):
            liked_al = it.id in self.liked_album_ids
            title = f"{it.artist} — {it.title}"
            options = [
                "Go to Mix tab [4]",
                "Go to Artist tab [5]",
                "Go to Album tab [6]",
                "Enqueue album [e]",
                "Enqueue album next [E]",
                f"{'Unlike' if liked_al else 'Like'} album [l]",
                "Add to playlist [a]",
                "Download album [d]",
                "Similar artists [s]",
                "Like & save corresponding mix…",
            ]
            choice = self.pick_from_list(title, options)
            if choice == 0:
                self.switch_tab(TAB_MIX, refresh=False)
                self.fetch_mix_from_album_async(it)
            elif choice == 1:
                fake = Track(id=0, title="", artist=it.artist, album="", year="????", track_no=0)
                self.switch_tab(TAB_ARTIST, refresh=False)
                self.fetch_artist_async(fake)
            elif choice == 2:
                self.open_album_from_album_obj(it)
            elif choice == 3:
                self.enqueue_album_async(it, insert_after_playing=False)
            elif choice == 4:
                self.enqueue_album_async(it, insert_after_playing=True)
            elif choice == 5:
                self.toggle_like_album(it)
            elif choice == 6:
                self._add_album_to_playlist_async(it)
            elif choice == 7:
                def _dl_alb() -> None:
                    try:
                        aid = self._resolve_album_id_for_album(it)
                        if aid:
                            self.start_download_tracks(self._fetch_album_tracks_by_album_id(aid))
                        else:
                            self.toast("Album id?")
                    except Exception:
                        self.toast("Error")
                threading.Thread(target=_dl_alb, daemon=True).start()
            elif choice == 8:
                ar_obj = Artist(id=0, name=it.artist, track_id=it.track_id)
                self.show_similar_artists_dialog(ar_obj, album_id=it.id)
            elif choice == 9:
                default_name = f"(Mix) {it.artist} - {it.title}"
                name = self.prompt_text("Mix name:", default_name)
                if name:
                    self.save_mix_as_playlist_async(name, it)

        elif isinstance(it, Artist):
            liked_ar = it.id in self.liked_artist_ids
            title = it.name
            options = [
                "Go to Mix tab [4]",
                "Go to Artist tab [5]",
                "Enqueue all artist' tracks [e]",
                "Enqueue all artist' tracks next [E]",
                f"{'Unlike' if liked_ar else 'Like'} artist [l]",
                "Add to playlist [a]",
                "Download artist [d]",
                "Similar artists [s]",
            ]
            choice = self.pick_from_list(title, options)
            if choice == 0:
                self.switch_tab(TAB_MIX, refresh=False)
                self.fetch_mix_from_artist_async(it)
            elif choice == 1:
                self.open_artist_by_id(it.id, it.name)
            elif choice == 2:
                self._enqueue_artist_async(it, insert_after_playing=False)
            elif choice == 3:
                self._enqueue_artist_async(it, insert_after_playing=True)
            elif choice == 4:
                self.toggle_like_artist(it.id, it.name)
            elif choice == 5:
                self._add_artist_to_playlist_async(it)
            elif choice == 6:
                self._download_artist_async(it)
            elif choice == 7:
                self.show_similar_artists_dialog(it)

        elif isinstance(it, str):
            # playlist name
            liked_pl = it in self.liked_playlist_ids
            title = it
            options = [
                "Open",
                "Enqueue [e]",
                f"{'Unlike' if liked_pl else 'Like'} playlist [l]",
                "Add to playlist [a]",
                "Download with subfolders [d]",
                "Download flat [D]",
            ]
            choice = self.pick_from_list(title, options)
            if choice == 0:
                self.playlists_open_by_name(it)
            elif choice == 1:
                self._enqueue_playlist_async(it, insert_after_playing=False)
            elif choice == 2:
                self.toggle_like_playlist(it)
            elif choice == 3:
                self._add_playlist_to_playlist_async(it)
            elif choice == 4:
                self._download_playlist_async(it, flat=False)
            elif choice == 5:
                self._download_playlist_async(it, flat=True)

        else:
            self.toast("No selection")

    # ---------------------------------------------------------------------------
    # downloads
    # ---------------------------------------------------------------------------
    def _guess_ext(self, url: str) -> str:
        u = url.lower()
        if ".flac" in u:
            return "flac"
        if ".m4a" in u or "mp4" in u:
            return "m4a"
        if ".mp3" in u:
            return "mp3"
        return "bin"

    def _download_worker(self, t: Track, remaining: int, current: int, total: int, set_progress) -> None:
        self._download_worker_impl(t, remaining, current, total, set_progress, DOWNLOADS_DIR, flat=False)

    def _make_playlist_download_worker(self, playlist_name: str, flat: bool):
        root = os.path.join(DOWNLOADS_DIR, safe_filename(playlist_name))
        def worker(t: Track, remaining: int, current: int, total: int, set_progress) -> None:
            self._download_worker_impl(t, remaining, current, total, set_progress, root, flat=flat)
        return worker

    def _download_playlist_async(self, name: str, flat: bool) -> None:
        tracks = list(self.playlists.get(name, []))
        if not tracks:
            self.toast("Nothing to download")
            return
        self.toast(f"DL playlist {'flat' if flat else 'structured'}…")
        worker = self._make_playlist_download_worker(name, flat)
        self.dl.progress_line = f"DL queued {len(tracks)}"
        self._need_redraw = True
        self._redraw_status_only = True
        self.dl.enqueue(tracks, worker)

    def _download_worker_impl(self, t: Track, remaining: int, current: int, total: int, set_progress, root: str, flat: bool) -> None:
        label = f"{t.artist[:16]} - {t.title[:16]}"
        count_s = f"[{current}/{total}]"

        def sp(msg: str) -> None:
            set_progress(msg)
            self._need_redraw = True
            self._redraw_status_only = True

        url = None
        last_err = ""
        for qi in range(self.quality_idx, len(QUALITY_ORDER)):
            q = QUALITY_ORDER[qi]
            try:
                candidate = self._resolve_stream_url_for_quality(t.id, q)
                if candidate.endswith(".mpd") and os.path.isfile(candidate):
                    debug_log(f"_download_worker: DASH at {q}, trying lower quality")
                    last_err = f"DASH at {q}"
                    continue
                url = candidate
                break
            except Exception as e:
                last_err = str(e)
                continue

        if url is None:
            err_msg = f"all qualities failed: {last_err}"
            sp(f"DL FAIL {count_s} {label}")
            self.toast(f"DL Error: {err_msg[:50]}")
            debug_log(f"_download_worker: {err_msg}")
            return

        ext = self._guess_ext(url)
        yv = self._track_year(t)
        if flat:
            out_dir = root
        else:
            structure = str(self.settings.get("download_structure") or "{artist}/{artist} - {album} ({year})")
            rel_path = structure.format(
                artist=safe_filename(t.artist),
                album=safe_filename(t.album),
                year=yv if yv != "????" else "unknown",
            )
            out_dir = os.path.join(root, rel_path)
        mkdirp(out_dir)
        # filename pattern is configurable via settings["download_filename"]
        filename_pat = str(self.settings.get("download_filename") or "{track:02d}. {artist} - {title}")

        # Use safe values; keep "00" for unknown track number
        track_n = t.track_no if (isinstance(t.track_no, int) and t.track_no > 0) else 0
        year_s = yv if yv != "????" else "unknown"

        try:
            base_name_raw = filename_pat.format(
                artist=t.artist or "",
                album=t.album or "",
                title=t.title or "",
                year=year_s,
                track=track_n,
            )
        except Exception:
            # fallback if user pattern is invalid
            base_name_raw = f"{track_n:02d}. {t.artist} - {t.title}"

        base_name = safe_filename(base_name_raw)
        out_path = os.path.join(out_dir, f"{base_name}.{ext}")

        def cb(done: int, tot: Optional[int]) -> None:
            if tot and tot > 0:
                pct = int((done * 100) / tot)
                mb = done / (1024 * 1024)
                sp(f"DL {count_s} {pct}% {mb:.1f}MB {label}")
            else:
                mb = done / (1024 * 1024)
                sp(f"DL {count_s} {mb:.1f}MB {label}")

        sp(f"DL {count_s} resolving {label}")
        try:
            http_stream_download(url, out_path, cb, timeout=120.0)
        except Exception as e:
            sp(f"DL FAIL {count_s} {label}")
            self.toast(f"DL Error: {str(e)[:50]}")
            debug_log(f"_download_worker: http error: {e}")
            return
        sp(f"DL {count_s} done {label}")

        cover_path = os.path.join(out_dir, "cover.jpg")
        if not os.path.exists(cover_path):
            try:
                cover_url = self._fetch_cover_url_for_track(t)
                if cover_url:
                    sp(f"DL {count_s} cover {label}")
                    cover_data = http_get_bytes(cover_url, timeout=20.0)
                    with open(cover_path, "wb") as f:
                        f.write(cover_data)
            except Exception:
                pass

        lrc_path = os.path.join(out_dir, f"{base_name}.lrc")
        if not os.path.exists(lrc_path):
            try:
                sp(f"DL {count_s} lyrics {label}")
                lyr_lines = []
                try:
                    lyr_payload = self.client.lyrics(t.id)
                    lyr_lines = self._extract_lrc_from_payload(lyr_payload)
                except Exception:
                    pass
                if not lyr_lines:
                    try:
                        info_payload = self.client.info(t.id)
                        lyr_lines = self._extract_lrc_from_payload(info_payload)
                    except Exception:
                        pass
                if lyr_lines:
                    with open(lrc_path, "w", encoding="utf-8") as f:
                        f.write("\n".join(lyr_lines))
            except Exception:
                pass

        # Always overwrite the last sub-step (e.g. "lyrics ...") with a final completion line.
        # Use full (non-truncated) artist/title here, and include the extension.
        full_label = f"{t.artist} - {t.title}.{ext}"
        sp(f"DL complete {count_s} {full_label}")


    def _fetch_cover_url_for_track(self, t: Track) -> Optional[str]:
        try:
            info = self.client.info(t.id)
            data = info.get("data") if isinstance(info, dict) else None
            if isinstance(data, dict):
                alb = data.get("album")
                if isinstance(alb, dict):
                    for k in ("coverUrl", "imageUrl", "squareImageUrl"):
                        v = alb.get(k)
                        if isinstance(v, str) and v.startswith("http"):
                            return v
                    for k in ("cover", "coverArt", "squareImage", "image"):
                        v = alb.get(k)
                        if isinstance(v, str) and v:
                            url = self._tidal_cover_uuid_to_url(v)
                            if url:
                                return url
                for k in ("coverUrl", "imageUrl"):
                    v = data.get(k)
                    if isinstance(v, str) and v.startswith("http"):
                        return v
                for k in ("cover", "image"):
                    v = data.get(k)
                    if isinstance(v, str) and v:
                        url = self._tidal_cover_uuid_to_url(v)
                        if url:
                            return url
        except Exception as e:
            debug_log(f"_fetch_cover_url_for_track error: {e}")
        return None

    def _tidal_cover_uuid_to_url(self, cover: str, size: int = 640) -> Optional[str]:
        cover = cover.strip()
        if not cover:
            return None
        if cover.startswith("http"):
            return cover
        parts = cover.split("-")
        if len(parts) >= 4:
            path = "/".join(parts)
            return f"https://resources.tidal.com/images/{path}/{size}x{size}.jpg"
        return None

    # ---------------------------------------------------------------------------
    # Cover tab
    # ---------------------------------------------------------------------------

    def _cover_backend(self) -> str:
        """Detect the best available image rendering backend (cached)."""
        if self._cover_backend_cache is None:
            if shutil.which("ueberzugpp"):
                self._cover_backend_cache = "ueberzugpp"
            elif shutil.which("chafa"):
                self._cover_backend_cache = "chafa"
            else:
                self._cover_backend_cache = "none"
        return self._cover_backend_cache

    def _cover_cache_path(self, url: str) -> str:
        h = hashlib.md5(url.encode()).hexdigest()
        cache_dir = os.path.join(STATE_DIR, "cover_cache")
        os.makedirs(cache_dir, exist_ok=True)
        return os.path.join(cache_dir, f"{h}.jpg")

    def fetch_cover_async(self, t: Optional[Track]) -> None:
        """Download cover art for track t. Called on playback start and when entering Cover tab."""
        if not t:
            return
        if self.cover_track and self.cover_track.id == t.id and self.cover_path:
            return  # already loaded for this track
        self.cover_track = t
        # Keep existing cover_path/render_buf until new cover is ready so the
        # old artwork remains visible on screen while the new one loads (no blank gap).
        self.cover_loading = True
        if self.tab == TAB_COVER:
            self._need_redraw = True

        def worker() -> None:
            try:
                url = self._fetch_cover_url_for_track(t)
                if not url:
                    # No cover found: clear only if this track is still the target.
                    if self.cover_track and self.cover_track.id == t.id:
                        self.cover_path = None
                        self._cover_render_key = ""
                        self._cover_render_buf = None
                    return
                dest = self._cover_cache_path(url)
                if not os.path.exists(dest):
                    data = http_get_bytes(url, timeout=15.0)
                    with open(dest, "wb") as f:
                        f.write(data)
                # Only apply if this track is still the target (avoid stale workers
                # overwriting the cover for a more recently-started track).
                if self.cover_track and self.cover_track.id == t.id:
                    self._prerender_cover(dest)   # render while still in bg thread
                    self.cover_path = dest        # set after pre-render so draw() sees cache
            except Exception as e:
                debug_log(f"fetch_cover_async error: {e}")
                if self.cover_track and self.cover_track.id == t.id:
                    self.cover_path = None
                    self._cover_render_key = ""
                    self._cover_render_buf = None
            finally:
                self.cover_loading = False
                if self.tab == TAB_COVER:
                    self._need_redraw = True
                    self._redraw_status_only = False

        threading.Thread(target=worker, daemon=True).start()

    def _ueberzug_start(self) -> bool:
        """Start the ueberzugpp daemon if not already running. Returns True on success."""
        if self._cover_ub_socket and self._cover_ub_pid:
            # Check daemon still alive
            try:
                os.kill(self._cover_ub_pid, 0)
                return True
            except OSError:
                self._cover_ub_socket = None
                self._cover_ub_pid = None

        pid_file = os.path.join(STATE_DIR, "ueberzugpp.pid")
        try:
            subprocess.Popen(
                ["ueberzugpp", "layer", "--no-stdin", "--silent",
                 "--use-escape-codes", "--pid-file", pid_file],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            # Wait briefly for the daemon to write its PID
            for _ in range(20):
                time.sleep(0.05)
                if os.path.exists(pid_file):
                    break
            with open(pid_file) as f:
                pid = int(f.read().strip())
            tmpdir = os.environ.get("TMPDIR", "/tmp")
            socket_path = os.path.join(tmpdir, f"ueberzugpp-{pid}.socket")
            self._cover_ub_pid = pid
            self._cover_ub_socket = socket_path
            return True
        except Exception as e:
            debug_log(f"ueberzugpp start error: {e}")
            return False

    def _ueberzug_show(self, path: str, x: int, y: int, w: int, h: int) -> None:
        if not self._cover_ub_socket:
            return
        try:
            subprocess.run(
                ["ueberzugpp", "cmd", "-s", self._cover_ub_socket,
                 "-i", "tuifi_cover", "-a", "add",
                 "-x", str(x), "-y", str(y),
                 "--max-width", str(w), "--max-height", str(h),
                 "-f", path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
            )
        except Exception as e:
            debug_log(f"ueberzugpp show error: {e}")

    def _ueberzug_remove(self) -> None:
        if not self._cover_ub_socket:
            return
        try:
            subprocess.run(
                ["ueberzugpp", "cmd", "-s", self._cover_ub_socket,
                 "-i", "tuifi_cover", "-a", "remove"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
            )
        except Exception:
            pass

    def _ueberzug_stop(self) -> None:
        self._ueberzug_remove()
        if self._cover_ub_pid:
            try:
                os.kill(self._cover_ub_pid, 15)  # SIGTERM
            except OSError:
                pass
        self._cover_ub_pid = None
        self._cover_ub_socket = None

    def _draw_cover_hint(self, y: int, x: int, h: int, w: int) -> None:
        """Draw status text in the cover tab area (image rendering happens after curses refresh)."""
        # If a cover is already on screen (old or newly loaded), skip all text so
        # ncurses doesn't write to the same row where the sixel starts, which
        # would briefly erase that row and cause a subtle flicker.
        if self.cover_path:
            return
        backend = self._cover_backend()
        if self.cover_loading:
            self.stdscr.addstr(y, x, " Loading cover…"[:max(0, w - 1)], self.C(4))
        elif not self.cover_path:
            t = self._current_selection_track() or self.current_track
            if not t:
                self.stdscr.addstr(y, x, " Play or select a track to show its cover art"[:max(0, w - 1)], self.C(10))
                return
            if backend == "none":
                lines = [
                    " No image renderer found. Install one of:",
                    "   chafa   — terminal image renderer (sixel/symbols, recommended)",
                    "   ueberzugpp — overlay image renderer",
                    " On Arch:   pacman -S chafa",
                    " On Debian: apt install chafa",
                    " On macOS:  brew install chafa",
                    "",
                    " Check sixel support: printf '\\033[c' in your terminal",
                    " (look for '4;' in the response, e.g. foot, WezTerm, xterm -ti vt340)",
                ]
                for i, line in enumerate(lines):
                    if y + i < h - 1:
                        self.stdscr.addstr(y + i, x, line[:max(0, w - 1)], self.C(10))
            else:
                self.stdscr.addstr(y, x, " No cover art available for this track"[:max(0, w - 1)], self.C(10))

    def _prerender_cover(self, path: str) -> None:
        """Pre-render cover image with chafa in the background download thread.
        Populates _cover_render_buf/_cover_render_key so the main thread can
        write the image instantly without running chafa again."""
        if self._cover_backend() != "chafa":
            return
        try:
            h, w = self.stdscr.getmaxyx()
        except Exception:
            return
        top_h = 2
        status_h = 2
        img_rows = h - top_h - status_h - 1  # -1 matches _render_cover_image gap
        queue_panel_active = self.queue_overlay and self.tab != TAB_QUEUE
        if queue_panel_active:
            right_w = 44
        elif self._cover_lyrics:
            right_w = self._lyrics_panel_w(w) + 2  # +2 for gap between cover and lyrics
        else:
            right_w = 0
        img_cols = w - right_w
        if img_rows <= 0 or img_cols <= 0:
            return
        render_key = f"{path}:{img_cols}x{img_rows}:chafa"
        try:
            result = subprocess.run(
                ["chafa", "--format=sixel", f"--size={img_cols}x{img_rows}", path],
                capture_output=True, timeout=8,
            )
            if result.returncode != 0 or not result.stdout:
                result = subprocess.run(
                    ["chafa", "--format=symbols",
                     f"--size={img_cols}x{img_rows}", path],
                    capture_output=True, timeout=8,
                )
            if result.returncode == 0 and result.stdout:
                self._cover_render_buf = result.stdout
                self._cover_render_key = render_key
        except Exception as e:
            debug_log(f"prerender_cover error: {e}")

    def _render_cover_image(self) -> None:
        """Write cover image to the terminal after curses has refreshed.
        Called only when self.tab == TAB_COVER and cover_path is set."""
        if not self.cover_path or not os.path.exists(self.cover_path):
            return
        backend = self._cover_backend()
        if backend == "none":
            return

        h, w = self.stdscr.getmaxyx()
        top_h = 2           # tab bar rows
        status_h = 2        # status bar rows
        # Leave a 1-row gap at the bottom so the sixel never reaches the terminal
        # edge.  Writing a sixel that ends at the very last terminal row can cause
        # some terminals to scroll the screen, shifting the image and corrupting
        # the layout.
        img_rows = h - top_h - status_h - 1
        # Don't render sixel over the queue panel or inline lyrics panel — those
        # are drawn as character cells and must remain visible above the sixel.
        # When the lyrics_overlay popup is open it is a centred box drawn on top
        # of the sixel (after rendering), so no column reservation is needed.
        queue_panel_active = self.queue_overlay and self.tab != TAB_QUEUE
        if queue_panel_active:
            right_w = 44
        elif self._cover_lyrics:
            right_w = self._lyrics_panel_w(w) + 2  # +2 for gap between cover and lyrics
        else:
            right_w = 0
        img_cols = w - right_w
        if img_rows <= 0 or img_cols <= 0:
            return

        render_key = f"{self.cover_path}:{img_cols}x{img_rows}:{backend}"

        if backend == "ueberzugpp":
            if not self._ueberzug_start():
                return
            # ueberzugpp is idempotent — send on every render
            self._ueberzug_show(self.cover_path, x=0, y=top_h,
                                w=img_cols, h=img_rows)
            self._cover_render_key = render_key
            self._cover_sixel_visible = True
            self._cover_sixel_cols = img_cols
            return

        # chafa path: cache rendered bytes, re-run only on change.
        # Skip the write entirely when the sixel is already on screen and nothing
        # changed — avoids the visible flash on every progress-tick redraw.
        if render_key == self._cover_render_key and self._cover_render_buf:
            if not self._cover_sixel_visible:
                self._write_image_to_terminal(top_h, self._cover_render_buf, img_cols)
            return

        try:
            result = subprocess.run(
                ["chafa", "--format=sixel", f"--size={img_cols}x{img_rows}",
                 self.cover_path],
                capture_output=True, timeout=8,
            )
            if result.returncode != 0 or not result.stdout:
                # Fallback: symbols work everywhere chafa is installed
                result = subprocess.run(
                    ["chafa", "--format=symbols",
                     f"--size={img_cols}x{img_rows}", self.cover_path],
                    capture_output=True, timeout=8,
                )
            if result.returncode == 0 and result.stdout:
                self._cover_render_buf = result.stdout
                self._cover_render_key = render_key
                self._write_image_to_terminal(top_h, result.stdout, img_cols)
        except Exception as e:
            debug_log(f"chafa render error: {e}")

    def _write_image_to_terminal(self, top_row: int, data: bytes, cols: int = 0) -> None:
        """Write image data (sixel or ANSI) directly to the terminal at the given row."""
        # Strip trailing newlines — chafa often appends one, and writing a newline
        # when the cursor is near the bottom of the terminal causes the terminal to
        # scroll, shifting the cover image and corrupting the layout.
        data = data.rstrip(b"\r\n")
        # Save cursor, move to content area, write image, restore cursor
        sys.stdout.buffer.write(
            b"\0337"                                          # save cursor (VT100)
            + f"\033[{top_row + 1};1H".encode()              # move to row, col 1
            + data
            + b"\0338"                                        # restore cursor
        )
        sys.stdout.buffer.flush()
        self._cover_sixel_visible = True
        if cols:
            self._cover_sixel_cols = cols

    def _cover_erase_terminal(self) -> None:
        """Overwrite the image area with spaces before a full curses redraw.
        Needed because some terminals don't clear sixel/ANSI pixels when curses
        paints over them (e.g. during popup overlays or tab switches)."""
        if not self._cover_sixel_visible:
            return
        h, w = self.stdscr.getmaxyx()
        top_h = 2
        status_h = 2
        img_rows = h - top_h - status_h
        # Only erase the columns that were actually covered by the last sixel.
        img_cols = self._cover_sixel_cols if self._cover_sixel_cols > 0 else w
        if img_rows > 0 and img_cols > 0:
            blank_line = b" " * img_cols
            buf = b"".join(
                f"\033[{top_h + 1 + r};1H".encode() + blank_line
                for r in range(img_rows)
            )
            sys.stdout.buffer.write(buf)
            sys.stdout.buffer.flush()
        self._cover_sixel_visible = False

    def _lyrics_panel_w(self, w: int) -> int:
        """Width of the lyrics panel in the cover tab (adapts to terminal width)."""
        # Give lyrics up to half the terminal; cover gets the rest (plus 2-col gap).
        return max(44, min(w // 2, w - 50))

    def _erase_popup_bg(self, y0: int, x0: int, rows: int, cols: int) -> None:
        """Erase a popup area with raw ANSI writes to clear any sixel underneath."""
        if not self._cover_sixel_visible:
            return
        blank = b" " * cols
        buf = b"".join(
            f"\033[{y0 + 1 + r};{x0 + 1}H".encode() + blank
            for r in range(rows)
        )
        sys.stdout.buffer.write(buf)
        sys.stdout.buffer.flush()

    def _draw_cover_lyrics_panel(self, y: int, x: int, h: int, w: int) -> None:
        """Draw lyrics as a right-side panel in the cover tab."""
        if w < 10:
            return
        lyr_attr = curses.color_pair(int(self.settings.get("cover_lyrics_color_pair", 0))) if self.color_mode and curses.has_colors() else 0
        # Title bar: show filter state or navigation hint
        if self._lyrics_filter_q and self._lyrics_filter_hits:
            n = len(self._lyrics_filter_hits)
            pos = self._lyrics_filter_pos + 1
            title = f"[{self._lyrics_filter_q}] {pos}/{n}  (/): pref/next match  f: re-filter  Esc: clear"
        elif self._lyrics_filter_q:
            title = f"[{self._lyrics_filter_q}] no match  f: re-filter  Esc: clear"
        else:
            title = "Lyrics  j/k: scroll  f: filter  V: show/hide"
        self.stdscr.addstr(y, x, title[:w].ljust(w), self.C(4))
        lines = self.lyrics_lines or []
        inner_h = h - 1
        if not lines:
            if self.lyrics_loading:
                self.stdscr.addstr(y + 1, x, "Fetching lyrics…"[:w].ljust(w), self.C(4))
            else:
                self.stdscr.addstr(y + 1, x, "No lyrics"[:w].ljust(w), lyr_attr)
            return
        self._cover_lyrics_max_scroll = max(0, len(lines) - inner_h)
        scroll = max(0, min(self.lyrics_scroll, self._cover_lyrics_max_scroll))
        hit_set = set(self._lyrics_filter_hits) if self._lyrics_filter_q else set()
        current_hit = (self._lyrics_filter_hits[self._lyrics_filter_pos]
                       if self._lyrics_filter_hits and 0 <= self._lyrics_filter_pos < len(self._lyrics_filter_hits)
                       else -1)
        for row in range(inner_h):
            li = scroll + row
            if y + 1 + row >= y + h:
                break
            text = lines[li].rstrip() if li < len(lines) else ""
            if li == current_hit:
                attr = curses.A_REVERSE
            elif li in hit_set:
                attr = self.C(5)
            else:
                attr = lyr_attr
            self.stdscr.addstr(y + 1 + row, x, text[:w].ljust(w), attr)

    def _cover_clear_image(self) -> None:
        """Clear the displayed cover image (called when leaving cover tab or before full redraw)."""
        if self._cover_backend_cache == "ueberzugpp":
            self._ueberzug_remove()
        self._cover_erase_terminal()
        self._cover_render_key = ""
        self._cover_render_buf = None
        # Force ncurses to do a full repaint on next refresh so sixel residue is
        # overwritten even in cells with transparent background.
        self.stdscr.clearok(True)

    def start_download_tracks(self, tracks: List[Track]) -> None:
        if not tracks:
            self.toast("Nothing to download")
            return
        self.dl.progress_line = f"DL queued {len(tracks)}"
        self._need_redraw = True
        self._redraw_status_only = True
        self.dl.enqueue(tracks, self._download_worker)

    # ---------------------------------------------------------------------------
    # album resolve/fetch
    # ---------------------------------------------------------------------------
    def _resolve_album_id_for_album(self, album: Album) -> Optional[int]:
        if album.id and album.id > 0:
            return album.id
        # Fast path: if we have a hint track id, ask the API directly
        if album.track_id:
            try:
                info = self.client.info(album.track_id)
                data = info.get("data") if isinstance(info, dict) else None
                if isinstance(data, dict):
                    alb = data.get("album")
                    if isinstance(alb, dict) and str(alb.get("id", "")).isdigit():
                        return int(alb["id"])
            except Exception:
                pass
        try:
            payload = self.client.search_tracks(f"{album.artist} {album.title}", limit=180)
            tracks = self._extract_tracks_from_search(payload)
            al0 = album.title.strip().lower()
            a0 = album.artist.strip().lower()
            pick = next((t for t in tracks if t.album.strip().lower() == al0 and t.artist.strip().lower() == a0), None)
            if pick is None and tracks:
                pick = tracks[0]
            if pick is None:
                return None
            info = self.client.info(pick.id)
            data = info.get("data") if isinstance(info, dict) else None
            if isinstance(data, dict):
                alb = data.get("album")
                if isinstance(alb, dict) and str(alb.get("id", "")).isdigit():
                    return int(alb["id"])
        except Exception:
            return None
        return None

    def _fetch_album_tracks_by_album_id(self, album_id: int) -> List[Track]:
        return self._extract_tracks_from_album_payload(self.client.album(int(album_id)))

    def open_album_from_album_obj(self, album: Album) -> None:
        self.switch_tab(TAB_ALBUM, refresh=False)
        self.album_header = album
        self.album_tracks = []
        self.left_idx = 0
        self.left_scroll = 0
        key = f"album-open:{time.time()}"
        self._set_loading(key)

        def worker() -> None:
            try:
                aid = self._resolve_album_id_for_album(album)
                if aid:
                    tracks = self._fetch_album_tracks_by_album_id(aid)
                    self.album_header = Album(id=aid, title=album.title, artist=album.artist, year=album.year)
                else:
                    payload = self.client.search_tracks(album.title, limit=280)
                    al0 = album.title.strip().lower()
                    tracks = sorted(
                        [t for t in self._extract_tracks_from_search(payload) if t.album.strip().lower() == al0],
                        key=lambda t: (t.track_no if t.track_no > 0 else 10_000, t.title.lower())
                    )
                if self._loading_key != key:
                    return
                self.album_tracks = tracks[:1500]
                for t in self.album_tracks[:40]:
                    if (self.show_track_year and year_norm(t.year) == "????") or (self.show_track_duration and not t.duration):
                        self.meta.want(t.id)
                self.toast(f"Album {len(self.album_tracks)}")
            except Exception as e:
                if self._loading_key == key:
                    self.last_error = str(e)
                    self.toast("Error")
            finally:
                self._clear_loading(key)

        threading.Thread(target=worker, daemon=True).start()

    def open_album_from_track(self, t: Track) -> None:
        self.open_album_from_album_obj(Album(id=t.album_id or self.meta.album_id.get(t.id, 0),
                                            title=t.album, artist=t.artist, year=t.year))

    # ---------------------------------------------------------------------------
    # tab loaders
    # ---------------------------------------------------------------------------
    def fetch_recommended_async(self, ctx: Optional[Track]) -> None:
        if not ctx:
            self.toast("No context")
            return
        self.recommended_results = []
        key = f"rec:{ctx.id}:{time.time()}"
        self._set_loading(key)

        def worker() -> None:
            try:
                payload = self.client.recommendations(ctx.id, limit=50)
                if self._loading_key != key:
                    return
                tracks: List[Track] = []
                data = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(data, dict) and isinstance(data.get("items"), list):
                    for it in data["items"]:
                        if isinstance(it, dict) and isinstance(it.get("track"), dict):
                            t = self._parse_track_obj(it["track"])
                            if t:
                                tracks.append(t)
                self.recommended_results = tracks
                self.toast("Recommended")
            except Exception as e:
                if self._loading_key == key:
                    self.last_error = str(e)
                    self.toast("Error")
            finally:
                self._clear_loading(key)

        threading.Thread(target=worker, daemon=True).start()

    def fetch_liked_async(self) -> None:
        self.liked_cache = [t for t in (mono_to_track(d) for d in self.liked_tracks) if t is not None]
        self.liked_album_cache = [
            Album(id=d["id"], title=d.get("title", ""), artist=d.get("artist", ""), year=d.get("year", ""))
            for d in self.liked_albums
        ]
        self.liked_artist_cache = [
            Artist(id=d["id"], name=d.get("name", ""))
            for d in self.liked_artists
        ]
        self.liked_playlist_cache = [d["name"] for d in self.liked_playlists]
        self._need_redraw = True
        self._redraw_status_only = False

    def fetch_artist_async(self, ctx: Optional[Track]) -> None:
        if not ctx:
            self.toast("No context")
            return
        self._last_artist_fetch_track = ctx
        self.artist_albums, self.artist_tracks = [], []
        self.artist_ctx = None
        key = f"artist:{ctx.id}:{time.time()}"
        self._set_loading(key)

        def worker() -> None:
            # Show artist name immediately before any API call so the tab header
            # is visible as soon as fetch_artist_async is called.
            if ctx.artist_id:
                self.artist_ctx = (ctx.artist_id, ctx.artist)
                self._need_redraw = True
                self._redraw_status_only = False
            try:
                aid = ctx.artist_id or self.meta.artist_id.get(ctx.id)
                if not aid:
                    info = self.client.info(ctx.id)
                    data = info.get("data") if isinstance(info, dict) else None
                    if isinstance(data, dict):
                        a = data.get("artist")
                        if isinstance(a, dict) and str(a.get("id", "")).isdigit():
                            aid = int(a["id"])

                albums: List[Album] = []
                raw_tracks: List[Track] = []

                def _yr(t: Track) -> int:
                    y = year_norm(t.year)
                    return int(y) if y.isdigit() else 9999

                def _commit_tracks() -> None:
                    partial = self._dedupe_tracks(raw_tracks)
                    partial.sort(key=lambda t: (_yr(t), t.album.lower(), t.track_no or 9999, t.title.lower()))
                    self.artist_tracks = partial[:600]
                    self._need_redraw = True
                    self._redraw_status_only = False

                if aid:
                    payload = self.client.artist(int(aid))
                    if self._loading_key != key:
                        return
                    albums = self._extract_artist_albums_from_payload(payload)
                    albums = self._dedupe_albums(albums)

                    # Publish albums immediately so the UI fills in before track fetching starts
                    self.artist_ctx = (int(aid), ctx.artist)
                    self.artist_albums = albums[:500]
                    self._need_redraw = True
                    self._redraw_status_only = False

                    # Fetch tracks album by album and update UI after each one
                    for alb in albums:
                        if self._loading_key != key:
                            return
                        if alb.id:
                            try:
                                new_tracks = self._fetch_album_tracks_by_album_id(alb.id)
                                raw_tracks.extend(new_tracks)
                                _commit_tracks()
                            except Exception:
                                pass

                    # Fallback: scan artist payload for track dicts if album fetches yielded nothing
                    if not raw_tracks:
                        dicts: List[Dict[str, Any]] = []
                        self._scan_for_track_dicts(payload, dicts, limit=2500)
                        for d in dicts:
                            t = self._parse_track_obj(d)
                            if t:
                                raw_tracks.append(t)

                if not raw_tracks:
                    payload2 = self.client.search_tracks(ctx.artist, limit=300)
                    if self._loading_key != key:
                        return
                    a0 = ctx.artist.strip().lower()
                    raw_tracks = [t for t in self._extract_tracks_from_search(payload2) if t.artist.strip().lower() == a0]

                if not albums:
                    best: Dict[Tuple, Album] = {}
                    for t in raw_tracks:
                        k2 = (t.artist.strip().lower(), t.album.strip().lower())
                        if k2 not in best:
                            best[k2] = Album(id=t.album_id or 0, title=t.album, artist=t.artist, year=t.year)
                        else:
                            cur = best[k2]
                            if cur.id == 0 and t.album_id:
                                cur.id = t.album_id
                            if year_norm(cur.year) == "????" and year_norm(t.year) != "????":
                                cur.year = t.year
                    albums = sorted(best.values(), key=lambda a: (int(a.year) if year_norm(a.year) != "????" else 9999, a.title.lower()))

                # Final commit with fully deduped/sorted results
                albums = self._dedupe_albums(albums)
                self.artist_albums = albums[:500]
                if aid:
                    self.artist_ctx = (int(aid), ctx.artist)
                _commit_tracks()
                self.toast("Artist")
            except Exception as e:
                if self._loading_key == key:
                    self.last_error = str(e)
                    self.toast("Error")
            finally:
                self._clear_loading(key)

        threading.Thread(target=worker, daemon=True).start()

    # ---------------------------------------------------------------------------
    # info overlay
    # ---------------------------------------------------------------------------
    def _request_info_refresh(self, t: Track) -> None:
        self._info_target_id = t.id
        self._info_refresh_due = time.time() + 0.12
        self.info_track = t
        self.info_album = None
        self.info_artist = None
        self.info_loading = True
        self._need_redraw = True
        self._redraw_status_only = False

    def _update_info_for_selection(self) -> None:
        """Refresh info content to match the current selection (used by show_info_dialog cursor nav)."""
        if self._queue_context():
            t = self._current_selection_track()
            if t and (self.info_track is None or t.id != self.info_track.id):
                self._request_info_refresh(t)
        else:
            it = self._selected_left_item()
            if isinstance(it, Artist):
                if self.info_artist is None or self.info_artist.id != it.id:
                    self.open_info_artist(it, _dialog=False)
            elif isinstance(it, tuple) and it[0] == "artist_header":
                ar = Artist(id=it[1][0], name=it[1][1])
                if self.info_artist is None or self.info_artist.id != ar.id:
                    self.open_info_artist(ar, _dialog=False)
            elif isinstance(it, Album):
                if self.info_album is None or self.info_album.id != it.id:
                    self.open_info_album(it, _dialog=False)
            elif isinstance(it, tuple) and it[0] == "album_title" and isinstance(it[1], Album):
                alb = it[1]
                if self.info_album is None or self.info_album.id != alb.id:
                    self.open_info_album(alb, _dialog=False)
            else:
                t = self._current_selection_track()
                if t and (self.info_track is None or t.id != self.info_track.id):
                    self._request_info_refresh(t)

    def show_info_dialog(self) -> None:
        """Inner-loop info popup (track/album/artist) — same pattern as show_similar_artists_dialog."""
        self._need_redraw = True
        self.draw()
        h, w = self.stdscr.getmaxyx()
        box_h = min(h - 4, 24)
        box_w = min(w - 8, 82)
        y0 = (h - box_h) // 2
        x0 = (w - box_w) // 2
        pad_y = max(0, y0 - 1)
        pad_x = max(0, x0 - 2)
        pad_h = min(h - pad_y, box_h + 2)
        pad_w = min(w - pad_x, box_w + 4)
        self._erase_popup_bg(pad_y, pad_x, pad_h, pad_w)
        for yy in range(pad_y, pad_y + pad_h):
            try:
                self.stdscr.addstr(yy, pad_x, " " * pad_w)
            except curses.error:
                pass
        win = self.stdscr.derwin(box_h, box_w, y0, x0)
        win.keypad(True)
        info_scroll = 0
        self.stdscr.timeout(100)
        try:
            while True:
                if self._need_redraw:
                    if not self._redraw_status_only:
                        self.draw()
                        self._erase_popup_bg(pad_y, pad_x, pad_h, pad_w)
                        for yy in range(pad_y, pad_y + pad_h):
                            try:
                                self.stdscr.addstr(yy, pad_x, " " * pad_w)
                            except curses.error:
                                pass
                    self._need_redraw = False
                    self._redraw_status_only = False

                self._do_info_fetch_if_due()

                # Build title and lines from current info state
                if self.info_artist and not self.info_track and not self.info_album:
                    title = "Artist info"
                    ar = self.info_artist
                    lines: List[str] = [f"Artist : {ar.name}", f"ID     : {ar.id}" if ar.id else "", ""]
                    if self.info_loading:
                        lines.append("Loading artist info…")
                    else:
                        payload = self.info_payload or {}
                        if "error" in payload:
                            lines.append(f"Error: {payload.get('error')}")
                        else:
                            data = payload.get("data") if isinstance(payload, dict) else payload
                            if not isinstance(data, dict):
                                data = payload
                            if isinstance(data, dict):
                                for k in ("popularity", "numberOfAlbums", "numberOfTracks",
                                          "artistTypes", "url"):
                                    if k in data:
                                        lines.append(f"{k}: {data[k]}")
                            if payload.get("_similar"):
                                lines.append(f"  [s] browse {len(payload['_similar'])} similar artists")
                    lines = [l for l in lines if l is not None]
                elif self.info_album and not self.info_track:
                    title = "Album info"
                    a = self.info_album
                    lines = [f"Album  : {a.title}", f"Artist : {a.artist}",
                             f"Year   : {year_norm(a.year)}", f"ID     : {a.id}" if a.id else "", ""]
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
                else:
                    t = self.info_track
                    if not t:
                        title = "Info"
                        lines = ["(no selection)"]
                    else:
                        title = "Track info"
                        lines = [f"Title   : {t.title}",
                                 f"Artist  : {t.artist}" + (f" (id {t.artist_id})" if t.artist_id else ""),
                                 f"Album   : {t.album}" + (f" (id {t.album_id})" if t.album_id else "")]
                        yv = self._track_year(t)
                        if yv != "????":
                            lines.append(f"Year    : {yv}")
                        dv = self._track_duration(t)
                        if dv:
                            lines.append(f"Duration: {fmt_dur(dv)}")
                        lines += [f"Track id: {t.id}", f"Liked   : {'yes' if self.is_liked(t.id) else 'no'}", ""]
                        if self.info_loading:
                            lines.append("Loading /info…")
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

                inner_h = box_h - 2
                max_scroll = max(0, len(lines) - inner_h)
                info_scroll = min(info_scroll, max_scroll)
                start = clamp(info_scroll, 0, max_scroll)

                win.erase()
                win.box()
                win.addstr(0, 2, f" {title} "[:box_w - 2], self.C(4))
                for i in range(inner_h):
                    idx = start + i
                    if idx >= len(lines):
                        break
                    line = lines[idx]
                    if line.startswith("\x01"):
                        text = line[1:][:box_w - 4].ljust(box_w - 4)
                        try:
                            win.addstr(1 + i, 2, text, curses.color_pair(16))
                        except curses.error:
                            pass
                    else:
                        try:
                            win.addstr(1 + i, 2, line[:box_w - 4])
                        except curses.error:
                            pass
                try:
                    win.addstr(box_h - 1, 2, " j/k: cursor   PgUp/Dn: scroll info   g/G: top/bottom   q/i/ESC: close "[:box_w - 4], self.C(10))
                except curses.error:
                    pass
                win.touchwin()
                win.refresh()

                try:
                    ch = self.stdscr.get_wch()
                except curses.error:
                    continue
                if isinstance(ch, str):
                    try:
                        ch = ord(ch)
                    except Exception:
                        continue

                if ch in (27, ord("q"), ord("i"), ord("I")):
                    break
                elif ch in (ord("j"), curses.KEY_DOWN):
                    # Move cursor in the main list and update info
                    if self._queue_context():
                        self.queue_cursor = clamp(self.queue_cursor + 1, 0, max(0, len(self.queue_items) - 1))
                    else:
                        _typ, _items = self._left_items()
                        _ni = self.left_idx + 1
                        while _ni < len(_items) and isinstance(_items[_ni], tuple) and _items[_ni][0] == "sep":
                            _ni += 1
                        self.left_idx = clamp(_ni, 0, max(0, len(_items) - 1))
                    info_scroll = 0
                    self._update_info_for_selection()
                    self._need_redraw = True
                    self._redraw_status_only = False
                elif ch in (ord("k"), curses.KEY_UP):
                    if self._queue_context():
                        self.queue_cursor = clamp(self.queue_cursor - 1, 0, max(0, len(self.queue_items) - 1))
                    else:
                        _typ, _items = self._left_items()
                        _ni = self.left_idx - 1
                        while _ni >= 0 and isinstance(_items[_ni], tuple) and _items[_ni][0] == "sep":
                            _ni -= 1
                        self.left_idx = clamp(_ni, 0, max(0, len(_items) - 1))
                    info_scroll = 0
                    self._update_info_for_selection()
                    self._need_redraw = True
                    self._redraw_status_only = False
                elif ch == curses.KEY_PPAGE:
                    info_scroll = max(0, info_scroll - self._page_step())
                elif ch == curses.KEY_NPAGE:
                    info_scroll = min(max_scroll, info_scroll + self._page_step())
                elif ch in (curses.KEY_HOME, ord("g")):
                    info_scroll = 0
                elif ch in (curses.KEY_END, ord("G")):
                    info_scroll = max_scroll
                elif ch == ord("s") and self.info_artist:
                    break  # fall through to show similar artists after dialog
        finally:
            self.stdscr.nodelay(True)
            self._need_redraw = True
            self._redraw_status_only = False

    def _do_info_fetch_if_due(self) -> None:
        if not self._info_target_id:
            return
        if time.time() < self._info_refresh_due:
            return
        tid = self._info_target_id
        self._info_target_id = None
        self.info_payload = None
        self.info_loading = True

        def worker() -> None:
            try:
                payload = self.client.info(tid)
                self.info_payload = payload if isinstance(payload, dict) else {"raw": payload}
            except Exception as e:
                self.info_payload = {"error": str(e)}
            self.info_loading = False
            self._need_redraw = True

        threading.Thread(target=worker, daemon=True).start()

    def toggle_info_selected(self) -> None:
        self.info_follow_selection = True
        if not self._queue_context():
            if self.tab == TAB_ALBUM and self._selected_album_title_line() and self.album_header:
                self.open_info_album(self.album_header)
                return
            if self.tab == TAB_ARTIST:
                alb = self._selected_left_album()
                if alb:
                    self.open_info_album(alb)
                    return
            it = self._selected_left_item()
            if isinstance(it, tuple) and it[0] == "artist_header":
                ar = Artist(id=it[1][0], name=it[1][1])
                self.show_similar_artists_dialog(ar)
                return
            if isinstance(it, Artist):
                self.show_similar_artists_dialog(it)
                return
            if isinstance(it, Album):
                self.open_info_album(it)
                return
            if isinstance(it, tuple) and it[0] == "album_title" and isinstance(it[1], Album):
                self.open_info_album(it[1])
                return
        t = self._current_selection_track()
        if not t:
            return
        self.info_scroll = 0
        self._request_info_refresh(t)
        self._need_redraw = True
        self.show_info_dialog()

    def toggle_info_playing(self) -> None:
        self.info_follow_selection = False
        t = self.current_track
        if not t:
            return
        self.info_scroll = 0
        self._request_info_refresh(t)
        self._need_redraw = True
        self.show_info_dialog()

    def open_info_album(self, album: Album, _dialog: bool = True) -> None:
        self.info_scroll = 0
        self.info_track = None
        self.info_album = album
        self.info_artist = None
        self.info_payload = None
        self.info_loading = True
        self._info_target_id = None
        self._need_redraw = True
        self._redraw_status_only = False

        def worker() -> None:
            try:
                aid = self._resolve_album_id_for_album(album)
                if aid:
                    payload = self.client.album(int(aid))
                    self.info_payload = payload if isinstance(payload, dict) else {"raw": payload}
                else:
                    self.info_payload = {"error": "Album id not found"}
            except Exception as e:
                self.info_payload = {"error": str(e)}
            self.info_loading = False
            self._need_redraw = True

        threading.Thread(target=worker, daemon=True).start()
        if _dialog:
            self.show_info_dialog()

    def open_info_artist(self, artist: Artist, _dialog: bool = True) -> None:
        self.info_scroll = 0
        self.info_track = None
        self.info_album = None
        self.info_artist = artist
        self.info_payload = None
        self.info_loading = True
        self._info_target_id = None
        self._need_redraw = True
        self._redraw_status_only = False

        def worker() -> None:
            try:
                aid = artist.id
                if not aid and artist.track_id:
                    try:
                        info = self.client.info(artist.track_id)
                        data = info.get("data") if isinstance(info, dict) else None
                        if isinstance(data, dict):
                            a = data.get("artist")
                            if isinstance(a, dict) and str(a.get("id", "")).isdigit():
                                aid = int(a["id"])
                    except Exception:
                        pass
                if aid:
                    payload = self.client.artist(int(aid))
                    payload = payload if isinstance(payload, dict) else {"raw": payload}
                    # Fetch similar artists
                    try:
                        sim_payload = self.client.artist_similar(int(aid))
                        sim_items: Any = None
                        if isinstance(sim_payload, dict):
                            for _key in ("artists", "items", "data"):
                                if isinstance(sim_payload.get(_key), list):
                                    sim_items = sim_payload[_key]
                                    break
                        elif isinstance(sim_payload, list):
                            sim_items = sim_payload
                        if sim_items:
                            similar: List[Dict[str, Any]] = []
                            for _a in sim_items:
                                if isinstance(_a, dict):
                                    _aid2 = _a.get("id") or 0
                                    _aname = _a.get("name") or _a.get("artistName") or ""
                                    if _aname:
                                        similar.append({"id": _aid2, "name": _aname})
                            if similar:
                                payload["_similar"] = similar
                    except Exception:
                        pass
                    self.info_payload = payload
                else:
                    self.info_payload = {"error": "Artist id not found"}
            except Exception as e:
                self.info_payload = {"error": str(e)}
            self.info_loading = False
            self._need_redraw = True

        threading.Thread(target=worker, daemon=True).start()
        if _dialog:
            self.show_info_dialog()

    def show_similar_artists_dialog(self, artist: Artist, album_id: int = 0) -> None:
        """Interactive dialog listing similar artists with action keys."""
        aid = artist.id
        # Try to use cached similar list from info_payload if available
        cached: Optional[List[Dict[str, Any]]] = None
        if (self.info_artist and self.info_artist.id == aid
                and isinstance(self.info_payload, dict)):
            cached = self.info_payload.get("_similar")

        if cached is None:
            self.toast("Loading similar artists…")
            self._need_redraw = True
            # Resolve artist id if needed
            if not aid and artist.track_id:
                try:
                    info = self.client.info(artist.track_id)
                    data = info.get("data") if isinstance(info, dict) else None
                    if isinstance(data, dict):
                        _a = data.get("artist")
                        if isinstance(_a, dict) and str(_a.get("id", "")).lstrip("-").isdigit():
                            aid = int(_a["id"])
                except Exception:
                    pass
            if not aid and album_id:
                try:
                    tracks = self._fetch_album_tracks_by_album_id(album_id)
                    if tracks:
                        t0 = tracks[0]
                        if t0.artist_id:
                            aid = t0.artist_id
                        elif t0.id:
                            info = self.client.info(t0.id)
                            data = info.get("data") if isinstance(info, dict) else None
                            if isinstance(data, dict):
                                _a = data.get("artist")
                                if isinstance(_a, dict) and str(_a.get("id", "")).lstrip("-").isdigit():
                                    aid = int(_a["id"])
                except Exception:
                    pass
            if not aid:
                self.toast("Artist id not found")
                return
            try:
                sim_payload = self.client.artist_similar(aid)
            except Exception as e:
                self.toast(f"Error: {e}")
                return
            cached = []
            sim_items: Any = None
            if isinstance(sim_payload, dict):
                for _key in ("artists", "items", "data"):
                    if isinstance(sim_payload.get(_key), list):
                        sim_items = sim_payload[_key]
                        break
            elif isinstance(sim_payload, list):
                sim_items = sim_payload
            if sim_items:
                for _a in sim_items:
                    if isinstance(_a, dict):
                        _aid2 = _a.get("id") or 0
                        _aname = _a.get("name") or _a.get("artistName") or ""
                        if _aname:
                            cached.append({"id": _aid2, "name": _aname})

        if not cached:
            self.toast("No similar artists found")
            return

        artists: List[Artist] = [Artist(id=int(a["id"]) if str(a["id"]).lstrip("-").isdigit() else 0,
                                        name=a["name"]) for a in cached]

        # Redraw underlying screen before drawing dialog on top.
        self._need_redraw = True
        self.draw()

        h, w = self.stdscr.getmaxyx()
        box_w = min(w - 6, max(56, max(len(a.name) for a in artists) + 8))
        box_h = min(h - 6, max(8, len(artists) + 4))
        y0 = (h - box_h) // 2
        x0 = (w - box_w) // 2
        pad_y = max(0, y0 - 1)
        pad_x = max(0, x0 - 2)
        pad_h = min(h - pad_y, box_h + 2)
        pad_w = min(w - pad_x, box_w + 4)
        self._erase_popup_bg(pad_y, pad_x, pad_h, pad_w)
        for yy in range(pad_y, pad_y + pad_h):
            try:
                self.stdscr.addstr(yy, pad_x, " " * pad_w)
            except curses.error:
                pass
        win = self.stdscr.derwin(box_h, box_w, y0, x0)
        win.keypad(True)
        idx = 0
        hint = " j/k ^n/^p: navigate  Enter/5: go to  Esc/q: close "
        hint2 = " a: add to playlist   e/E: enqueue    l: like "
        try:
            while True:
                if self._need_redraw:
                    # For status-only progress ticks, skip the full redraw so that
                    # stdscr.refresh() doesn't overwrite the popup area with spaces.
                    if not self._redraw_status_only:
                        self.draw()
                        self._erase_popup_bg(pad_y, pad_x, pad_h, pad_w)
                        for yy in range(pad_y, pad_y + pad_h):
                            try:
                                self.stdscr.addstr(yy, pad_x, " " * pad_w)
                            except curses.error:
                                pass
                    self._need_redraw = False
                    self._redraw_status_only = False

                idx = clamp(idx, 0, len(artists) - 1)
                win.erase()
                win.box()
                win.addstr(0, 2, f" Similar to {artist.name} "[:box_w - 2], self.C(4))
                inner_h = box_h - 3
                scroll = max(0, idx - inner_h + 1) if idx >= inner_h else 0
                for row in range(inner_h):
                    fi = scroll + row
                    if fi >= len(artists):
                        break
                    attr = curses.A_REVERSE if fi == idx else 0
                    win.addstr(1 + row, 2, artists[fi].name[:box_w - 4].ljust(box_w - 4), attr)
                win.addstr(box_h - 2, 2, hint[:box_w - 4], self.C(10))
                win.addstr(box_h - 1, 2, hint2[:box_w - 4], self.C(10))
                win.touchwin()  # force full resend so popup stays visible over sixel
                win.refresh()

                self.stdscr.timeout(100)
                try:
                    ch = self.stdscr.get_wch()
                except curses.error:
                    continue  # timeout — loop to redraw if needed

                if isinstance(ch, str):
                    try:
                        ch = ord(ch)
                    except Exception:
                        continue

                if ch in (ord("j"), curses.KEY_DOWN, 14):
                    idx = min(len(artists) - 1, idx + 1)
                elif ch in (ord("k"), curses.KEY_UP, 16):
                    idx = max(0, idx - 1)
                elif ch in (10, 13, ord("5")):
                    ar = artists[idx]
                    self.open_artist_by_id(ar.id, ar.name)
                    break
                elif ch == ord("e"):
                    self._enqueue_artist_async(artists[idx], insert_after_playing=False)
                    self.toast(f"Enqueued {artists[idx].name}")
                elif ch == ord("E"):
                    self._enqueue_artist_async(artists[idx], insert_after_playing=True)
                    self.toast(f"Enqueued next: {artists[idx].name}")
                elif ch == ord("a"):
                    self._add_artist_to_playlist_async(artists[idx])
                elif ch == ord("l"):
                    self.toggle_like_artist(artists[idx].id, artists[idx].name)
                elif ch in (27, ord("q"), ord("s"), ord("i"), ord("c")):
                    break
        finally:
            self.stdscr.nodelay(True)
            self._need_redraw = True
            self._redraw_status_only = False

    def _extract_lrc_from_payload(self, payload: Any) -> List[str]:
        if not isinstance(payload, dict):
            return []

        def _looks_like_lyrics(s: str) -> bool:
            s = s.strip()
            if not s or len(s) < 30:
                return False
            if "\n" not in s and len(s) < 200:
                return False
            if s.startswith("{") or s.startswith("[{") or s.startswith("http"):
                return False
            return True

        def _find_raw(obj: Any, depth: int = 0) -> str:
            if depth > 10:
                return ""
            if isinstance(obj, str):
                return obj.strip() if _looks_like_lyrics(obj) else ""
            if isinstance(obj, dict):
                for k in ("subtitles", "lyrics", "lyric"):
                    v = obj.get(k)
                    if isinstance(v, str) and _looks_like_lyrics(v):
                        return v.strip()
                    if isinstance(v, (dict, list)):
                        r = _find_raw(v, depth + 1)
                        if r:
                            return r
                for k, v in obj.items():
                    kl = k.lower()
                    if kl in ("lyrics", "lyric", "subtitles", "subtitle", "lyricstext",
                               "lyricssubtitles", "tracklyrics"):
                        r = _find_raw(v, depth + 1)
                        if r:
                            return r
                for v in obj.values():
                    if isinstance(v, (dict, list)):
                        r = _find_raw(v, depth + 1)
                        if r:
                            return r
            if isinstance(obj, list):
                for item in obj:
                    r = _find_raw(item, depth + 1)
                    if r:
                        return r
            return ""

        text = _find_raw(payload)
        if not text:
            return []
        return text.splitlines()

    def _extract_lyrics_from_payload(self, payload: Any) -> List[str]:
        if not isinstance(payload, dict):
            return []

        def _looks_like_lyrics(s: str) -> bool:
            s = s.strip()
            if not s:
                return False
            if len(s) < 30:
                return False
            if "\n" not in s and len(s) < 200:
                return False
            if s.startswith("{") or s.startswith("[{") or s.startswith("http"):
                return False
            if len(s) > 50 and "/" in s[:20]:
                return False
            return True

        def _find_text(obj: Any, depth: int = 0) -> str:
            if depth > 10:
                return ""
            if isinstance(obj, str):
                if _looks_like_lyrics(obj):
                    return obj.strip()
                return ""
            if isinstance(obj, dict):
                for k in ("subtitles", "lyrics", "lyric"):
                    v = obj.get(k)
                    if isinstance(v, str) and _looks_like_lyrics(v):
                        return v.strip()
                    if isinstance(v, (dict, list)):
                        r = _find_text(v, depth + 1)
                        if r:
                            return r
                for k, v in obj.items():
                    kl = k.lower()
                    if kl in ("lyrics", "lyric", "subtitles", "subtitle", "lyricstext",
                               "lyricssubtitles", "tracklyrics"):
                        r = _find_text(v, depth + 1)
                        if r:
                            return r
                for v in obj.values():
                    if isinstance(v, (dict, list)):
                        r = _find_text(v, depth + 1)
                        if r:
                            return r
            if isinstance(obj, list):
                for item in obj:
                    r = _find_text(item, depth + 1)
                    if r:
                        return r
            return ""

        text = _find_text(payload)
        if not text:
            debug_log(f"_extract_lyrics: no lyrics text found in payload keys={list(payload.keys())[:10]}")
            return []
        debug_log(f"_extract_lyrics: found {len(text)} chars, first 60: {text[:60]!r}")
        lines = text.splitlines()
        cleaned: List[str] = []
        lrc_re = re.compile(r"^\[\d+:\d+\.\d+\](.*)$")
        has_lrc = any(lrc_re.match(l) for l in lines[:5] if l.strip())
        for l in lines:
            if has_lrc:
                m = lrc_re.match(l)
                if m:
                    cleaned.append(m.group(1).strip())
                    continue
            cleaned.append(l)
        return cleaned

    def toggle_lyrics(self, target: Optional["Track"] = None) -> None:
        if self.lyrics_overlay:
            self.lyrics_overlay = False
            self._need_redraw = True
            return
        t = target or self.current_track or self._current_selection_track()
        if not t:
            self.toast("No track")
            return
        self.lyrics_overlay = True
        self.lyrics_scroll = 0
        self.lyrics_track = t
        self._need_redraw = True
        self._redraw_status_only = False
        if self.lyrics_track_id == t.id and self.lyrics_lines:
            return
        self.lyrics_track_id = t.id
        self.lyrics_lines = []
        self.lyrics_loading = True
        self._lyrics_filter_q = ""
        self._lyrics_filter_hits = []
        self._lyrics_filter_pos = -1

        def worker() -> None:
            lines: List[str] = []
            try:
                try:
                    lyr_payload = self.client.lyrics(t.id)
                    lines = self._extract_lyrics_from_payload(lyr_payload)
                except Exception:
                    pass
                if not lines:
                    info_payload = self.client.info(t.id)
                    lines = self._extract_lyrics_from_payload(info_payload)
                if not lines:
                    lines = ["No lyrics available for this track."]
                self.lyrics_lines = lines
            except Exception as e:
                self.lyrics_lines = [f"Error fetching lyrics: {e}"]
            self.lyrics_loading = False
            self._need_redraw = True

        threading.Thread(target=worker, daemon=True).start()

    def show_lyrics_dialog(self, track: "Track") -> None:
        """Inner-loop lyrics popup — same pattern as show_similar_artists_dialog."""
        # Ensure lyrics are available/being fetched for this track
        if not (self.lyrics_track_id == track.id and (self.lyrics_lines or self.lyrics_loading)):
            self.lyrics_track_id = track.id
            self.lyrics_lines = []
            self.lyrics_loading = True
            self.lyrics_track = track

            def _worker() -> None:
                try:
                    payload = self.client.lyrics(track.id)
                    self.lyrics_lines = self._extract_lrc_from_payload(payload)
                except Exception:
                    self.lyrics_lines = []
                self.lyrics_loading = False
                self._need_redraw = True
                self._redraw_status_only = False

            threading.Thread(target=_worker, daemon=True).start()

        self._need_redraw = True
        self.draw()
        h, w = self.stdscr.getmaxyx()
        box_h = min(h - 4, 32)
        box_w = min(w - 8, 86)
        y0 = (h - box_h) // 2
        x0 = (w - box_w) // 2
        pad_y = max(0, y0 - 1)
        pad_x = max(0, x0 - 2)
        pad_h = min(h - pad_y, box_h + 2)
        pad_w = min(w - pad_x, box_w + 4)
        self._erase_popup_bg(pad_y, pad_x, pad_h, pad_w)
        for yy in range(pad_y, pad_y + pad_h):
            try:
                self.stdscr.addstr(yy, pad_x, " " * pad_w)
            except curses.error:
                pass
        win = self.stdscr.derwin(box_h, box_w, y0, x0)
        win.keypad(True)
        scroll = 0
        self.stdscr.timeout(100)
        try:
            while True:
                if self._need_redraw:
                    if not self._redraw_status_only:
                        self.draw()
                        self._erase_popup_bg(pad_y, pad_x, pad_h, pad_w)
                        for yy in range(pad_y, pad_y + pad_h):
                            try:
                                self.stdscr.addstr(yy, pad_x, " " * pad_w)
                            except curses.error:
                                pass
                    self._need_redraw = False
                    self._redraw_status_only = False

                t_ref = track
                title = f"Lyrics – {t_ref.artist} - {t_ref.title}"
                if self.lyrics_loading and self.lyrics_track_id == track.id:
                    lines: List[str] = ["Loading lyrics…"]
                else:
                    lines = self.lyrics_lines if self.lyrics_track_id == track.id else ["(empty)"]
                    if not lines:
                        lines = ["(empty)"]

                inner_h = box_h - 2
                max_scroll = max(0, len(lines) - inner_h)
                scroll = min(scroll, max_scroll)
                start = clamp(scroll, 0, max_scroll)

                win.erase()
                win.box()
                win.addstr(0, 2, title[:box_w - 4], self.C(4))
                for i in range(inner_h):
                    idx = start + i
                    if idx >= len(lines):
                        break
                    try:
                        win.addstr(1 + i, 2, lines[idx][:box_w - 4])
                    except curses.error:
                        pass
                try:
                    win.addstr(box_h - 1, 2, " j/k: scroll   g/G: top/bottom   q/v/Esc: close "[:box_w - 4], self.C(10))
                except curses.error:
                    pass
                win.touchwin()
                win.refresh()

                try:
                    ch = self.stdscr.get_wch()
                except curses.error:
                    continue
                if isinstance(ch, str):
                    try:
                        ch = ord(ch)
                    except Exception:
                        continue

                if ch in (27, ord("v"), ord("V"), ord("q")):
                    break
                elif ch in (ord("j"), curses.KEY_DOWN, 14):
                    scroll = min(max_scroll, scroll + 1)
                elif ch in (ord("k"), curses.KEY_UP, 16):
                    scroll = max(0, scroll - 1)
                elif ch == curses.KEY_PPAGE:
                    scroll = max(0, scroll - self._page_step())
                elif ch == curses.KEY_NPAGE:
                    scroll = min(max_scroll, scroll + self._page_step())
                elif ch in (curses.KEY_HOME, ord("g")):
                    scroll = 0
                elif ch in (curses.KEY_END, ord("G")):
                    scroll = max_scroll
        finally:
            self.stdscr.nodelay(True)
            self._need_redraw = True
            self._redraw_status_only = False

    # ---------------------------------------------------------------------------
    # search
    # ---------------------------------------------------------------------------
    def do_search_prompt_anywhere(self) -> None:
        self.playlist_view_name = None
        q = self.prompt_text("Search:", self.search_q)
        if q is None:
            return
        self.switch_tab(TAB_SEARCH, refresh=False)
        self.search_q = q
        self.search_results = []
        self.left_idx = 0
        self.left_scroll = 0
        self.last_error = None
        try:
            payload = self.client.search_tracks(self.search_q, limit=260)
            self.search_results = self._extract_tracks_from_search(payload)
            self.toast(f"{len(self.search_results)} results")
        except Exception as e:
            self.last_error = str(e)
            self.toast("Error")
        self._need_redraw = True
        self._redraw_status_only = False

    # ---------------------------------------------------------------------------
    # filter / find
    # ---------------------------------------------------------------------------
    def _compute_filter_hits(self) -> None:
        q = self.filter_q.strip().lower()
        self.filter_hits = []
        self.filter_pos = -1
        if not q:
            return
        typ, items = self._left_items()
        for i, it in enumerate(items):
            if isinstance(it, Track):
                dv = self._track_duration(it)
                dur_s = fmt_dur(dv) if dv else ""
                yv = self._track_year(it) if it.year else ""
                haystack = f"{it.artist} {it.title} {it.album} {yv} {dur_s}".lower()
                if q in haystack:
                    self.filter_hits.append(i)
            elif isinstance(it, Album):
                if q in f"{it.artist} {it.title}".lower():
                    self.filter_hits.append(i)
            elif isinstance(it, Artist):
                if q in it.name.lower():
                    self.filter_hits.append(i)
            elif isinstance(it, str):
                if q in it.lower():
                    self.filter_hits.append(i)
        if self.filter_hits:
            self.filter_pos = 0

    def _set_filter_cursor(self, idx: int) -> None:
        if self.tab == TAB_QUEUE:
            self.queue_cursor = idx
        else:
            self.left_idx = idx

    def _get_filter_cursor(self) -> int:
        return self.queue_cursor if self.tab == TAB_QUEUE else self.left_idx

    def filter_prompt(self) -> None:
        q = self.prompt_text("Filter:", self.filter_q)
        if q is None:
            return
        self.filter_q = q
        self._compute_filter_hits()
        if not self.filter_hits:
            self.toast("No match")
            return
        self.filter_pos = 0
        self._set_filter_cursor(self.filter_hits[0])
        self.toast(f"1/{len(self.filter_hits)}")

    def filter_next(self, delta: int) -> None:
        if not self.filter_hits:
            return
        self.filter_pos = (self.filter_pos + delta) % len(self.filter_hits)
        self._set_filter_cursor(self.filter_hits[self.filter_pos])
        self.toast(f"{self.filter_pos+1}/{len(self.filter_hits)}")

    def _compute_lyrics_filter_hits(self) -> None:
        q = self._lyrics_filter_q.strip().lower()
        self._lyrics_filter_hits = []
        self._lyrics_filter_pos = -1
        if not q:
            return
        for i, line in enumerate(self.lyrics_lines or []):
            if q in line.lower():
                self._lyrics_filter_hits.append(i)
        if self._lyrics_filter_hits:
            self._lyrics_filter_pos = 0

    def lyrics_filter_prompt(self) -> None:
        q = self.prompt_text("Lyrics filter:", self._lyrics_filter_q)
        if q is None:
            return
        self._lyrics_filter_q = q
        self._compute_lyrics_filter_hits()
        if not self._lyrics_filter_hits:
            self.toast("No match")
            return
        self._lyrics_filter_pos = 0
        self.lyrics_scroll = self._lyrics_filter_hits[0]
        self._need_redraw = True
        self.toast(f"1/{len(self._lyrics_filter_hits)}")

    def lyrics_filter_next(self, delta: int) -> None:
        if not self._lyrics_filter_hits:
            return
        self._lyrics_filter_pos = (self._lyrics_filter_pos + delta) % len(self._lyrics_filter_hits)
        self.lyrics_scroll = self._lyrics_filter_hits[self._lyrics_filter_pos]
        self._need_redraw = True
        self.toast(f"{self._lyrics_filter_pos+1}/{len(self._lyrics_filter_hits)}")

    # ---------------------------------------------------------------------------
    # playlists
    # ---------------------------------------------------------------------------
    def playlists_create(self) -> None:
        name = self.prompt_text("New playlist name:", "")
        if not name:
            return
        if name in self.playlists:
            self.toast("Exists")
            return
        now_ms = int(time.time() * 1000)
        self.playlists[name] = []
        self.playlists_meta[name] = {"id": str(uuid.uuid4()), "createdAt": now_ms}
        save_playlists(self.playlists, self.playlists_meta)
        self.playlist_names = sorted(self.playlists.keys())
        self.toast("Created")
        self._need_redraw = True
        self._redraw_status_only = False

    def playlists_delete_current(self) -> None:
        if self.playlist_view_name is None:
            marked = self._marked_playlists_from_left()
            if marked:
                if not self.prompt_yes_no(f"Delete {len(marked)} playlists? (y/n)"):
                    return
                for name in marked:
                    self.playlists.pop(name, None)
                    self.playlists_meta.pop(name, None)
                self.marked_left_idx.clear()
                save_playlists(self.playlists, self.playlists_meta)
                self.playlist_names = sorted(self.playlists.keys())
                self.left_idx = clamp(self.left_idx, 0, max(0, len(self.playlist_names) - 1))
                self.toast(f"Deleted {len(marked)}")
                self._need_redraw = True
                self._redraw_status_only = False
                return
            if not self.playlist_names:
                return
            name = self.playlist_names[clamp(self.left_idx, 0, len(self.playlist_names)-1)]
        else:
            name = self.playlist_view_name
        if not name:
            return
        if not self.prompt_yes_no(f"Delete '{name}'? (y/n)"):
            return
        self.playlists.pop(name, None)
        self.playlists_meta.pop(name, None)
        save_playlists(self.playlists, self.playlists_meta)
        self.playlist_view_name = None
        self.playlist_view_tracks = []
        self.playlist_names = sorted(self.playlists.keys())
        self.left_idx = 0
        self.left_scroll = 0
        self.toast("Deleted")
        self._need_redraw = True
        self._redraw_status_only = False

    def playlists_open_selected(self) -> None:
        if not self.playlist_names:
            return
        name = self.playlist_names[clamp(self.left_idx, 0, len(self.playlist_names)-1)]
        self.playlist_view_name = name
        self.playlist_view_tracks = []
        self.left_idx = 0
        self.left_scroll = 0
        self.fetch_playlist_tracks_async(name)

    def fetch_playlist_tracks_async(self, name: str) -> None:
        self.playlist_view_tracks = list(self.playlists.get(name, []))
        self._need_redraw = True
        self._redraw_status_only = False

    def _enqueue_playlist_async(self, name: str, insert_after_playing: bool) -> None:
        tracks = self.playlists.get(name, [])
        if tracks:
            self._enqueue_tracks(list(tracks), insert_after_playing)
        else:
            self.toast("Empty playlist")

    def _add_tracks_to_named_playlist(self, tracks: List[Track], name: str) -> None:
        self.playlists.setdefault(name, []).extend(tracks)
        save_playlists(self.playlists, self.playlists_meta)
        self.toast(f"Added {len(tracks)}")
        self._need_redraw = True
        self._redraw_status_only = False
        if self.tab == TAB_PLAYLISTS and self.playlist_view_name == name:
            self.playlist_view_tracks = list(self.playlists[name])

    def playlists_add_tracks(self, tracks: List[Track]) -> None:
        if not tracks:
            self.toast("No tracks")
            return
        name = self.pick_playlist("Add to playlist")
        if not name:
            return
        self._add_tracks_to_named_playlist(tracks, name)

    def _add_album_to_playlist_async(self, album: Album) -> None:
        name = self.pick_playlist("Add to playlist")
        if not name:
            return
        self.toast("Fetching album…")
        def worker() -> None:
            aid = self._resolve_album_id_for_album(album)
            if not aid:
                self.toast("Album id?")
                return
            tracks = self._fetch_album_tracks_by_album_id(aid)
            if tracks:
                self._add_tracks_to_named_playlist(tracks, name)
            else:
                self.toast("No tracks")
        self._bg(worker)

    def _add_marked_artists_to_playlist_async(self, artists: List[Artist]) -> None:
        name = self.pick_playlist("Add to playlist")
        if not name:
            return
        self.toast(f"Fetching {len(artists)} artists…")
        def worker() -> None:
            all_tracks: List[Track] = []
            for artist in artists:
                aid = artist.id
                if not aid and artist.track_id:
                    try:
                        info = self.client.info(artist.track_id)
                        data = info.get("data") if isinstance(info, dict) else None
                        if isinstance(data, dict):
                            a = data.get("artist")
                            if isinstance(a, dict) and str(a.get("id", "")).isdigit():
                                aid = int(a["id"])
                    except Exception:
                        pass
                if aid:
                    _albums, tracks = self._fetch_artist_catalog_by_artist_id(aid)
                    all_tracks.extend(tracks)
            if all_tracks:
                self._add_tracks_to_named_playlist(all_tracks, name)
            else:
                self.toast("No tracks")
        self._bg(worker)

    def _add_marked_albums_to_playlist_async(self, albums: List[Album]) -> None:
        name = self.pick_playlist("Add to playlist")
        if not name:
            return
        self.toast(f"Fetching {len(albums)} albums…")
        def worker() -> None:
            all_tracks: List[Track] = []
            for album in albums:
                aid = self._resolve_album_id_for_album(album)
                if aid:
                    all_tracks.extend(self._fetch_album_tracks_by_album_id(aid))
            if all_tracks:
                self._add_tracks_to_named_playlist(all_tracks, name)
            else:
                self.toast("No tracks")
        self._bg(worker)

    def _add_artist_to_playlist_async(self, artist: Artist) -> None:
        name = self.pick_playlist("Add to playlist")
        if not name:
            return
        self.toast("Fetching artist…")
        def worker() -> None:
            tracks: List[Track] = []
            aid = artist.id
            if not aid and artist.track_id:
                try:
                    info = self.client.info(artist.track_id)
                    data = info.get("data") if isinstance(info, dict) else None
                    if isinstance(data, dict):
                        a = data.get("artist")
                        if isinstance(a, dict) and str(a.get("id", "")).isdigit():
                            aid = int(a["id"])
                except Exception:
                    pass
            if aid:
                _albums, tracks = self._fetch_artist_catalog_by_artist_id(aid)
            if not tracks:
                payload2 = self.client.search_tracks(artist.name, limit=300)
                a0 = artist.name.strip().lower()
                tracks = [t for t in self._extract_tracks_from_search(payload2)
                          if t.artist.strip().lower() == a0]
            if tracks:
                def _yr(t: Track) -> int:
                    y = year_norm(t.year)
                    return int(y) if y.isdigit() else 9999
                tracks = self._dedupe_tracks(tracks)
                tracks = sorted(tracks, key=lambda t: (_yr(t), t.album.lower(), t.track_no or 9999, t.title.lower()))
                self._add_tracks_to_named_playlist(tracks, name)
            else:
                self.toast("No tracks")
        self._bg(worker)

    def _add_playlist_to_playlist_async(self, source_name: str) -> None:
        tracks = list(self.playlists.get(source_name, []))
        if not tracks:
            self.toast("Empty playlist")
            return
        name = self.pick_playlist("Add to playlist", exclude=source_name)
        if not name:
            return
        self._add_tracks_to_named_playlist(tracks, name)

    def playlists_add_from_context(self) -> None:
        if self._queue_context():
            tracks = self._marked_tracks_from_queue() or ([self._queue_selected_track()] if self._queue_selected_track() else [])
            self.playlists_add_tracks([t for t in tracks if t])
            return
        marked_albums = self._marked_albums_from_left()
        marked_artists = self._marked_artists_from_left()
        marked_playlists = self._marked_playlists_from_left()
        marked_albums, marked_artists, marked_playlists, cancelled = \
            self._resolve_batch_conflict(marked_albums, marked_artists, marked_playlists)
        if cancelled:
            return
        if marked_albums:
            self._add_marked_albums_to_playlist_async(marked_albums)
            return
        if marked_artists:
            self._add_marked_artists_to_playlist_async(marked_artists)
            return
        if marked_playlists:
            name = self.pick_playlist("Add to playlist")
            if name:
                all_tracks: List[Track] = []
                for pl in marked_playlists:
                    all_tracks.extend(self.playlists.get(pl, []))
                if all_tracks:
                    self._add_tracks_to_named_playlist(all_tracks, name)
                else:
                    self.toast("Nothing to add")
            return
        it = self._selected_left_item()
        if isinstance(it, Artist):
            self._add_artist_to_playlist_async(it)
            return
        if isinstance(it, Album):
            self._add_album_to_playlist_async(it)
            return
        if isinstance(it, str):
            self._add_playlist_to_playlist_async(it)
            return
        tracks = self._marked_tracks_from_left() or ([self._selected_left_track()] if self._selected_left_track() else [])
        self.playlists_add_tracks([t for t in tracks if t])

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
        order = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)
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
            if self.tab == TAB_ARTIST and (self.artist_ctx or self.artist_albums or self.artist_tracks):
                pass  # fall through; partial content visible, "…" shown in section headers
            else:
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

        # Cover tab: leave content area blank for image; show status/hint text only
        if typ == "cover_tab":
            self._draw_cover_hint(y, x, h, w)
            return

        # Empty-tab hints
        if n == 0 and self.tab == TAB_QUEUE:
            self.stdscr.addstr(y, x, " Press e/E on tracks in any tab to enqueue, q to show queue overlay"[:max(0, w - 1)].ljust(max(0, w - 1)), self.C(10))
            return
        if typ == "tracks" and n == 0 and self.tab == TAB_SEARCH:
            self.stdscr.addstr(y, x, " Search on TIDAL with /"[:max(0, w - 1)].ljust(max(0, w - 1)), self.C(10))
            return
        if typ == "tracks" and n == 0 and self.tab == TAB_RECOMMENDED:
            hint = (
                " A playing track is required to get recommendations\n"
                "\n"
                " If Autoplay is set to \"recommended\", the queue will expand with recommended suggestions\n"
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
                " Press 4 on a track, album or artist in any tab to load its track mix\n"
                "\n"
                " If Autoplay is set to \"mix\", the queue will expand with mix suggestions\n"
                " based on last queue items"
            )
            if self.mix_track:
                hint = " No mix tracks loaded — press 4 with a track, album or artist selected"

            for i, line in enumerate(hint.splitlines()):
                if y + i < h - 1:
                    self.stdscr.addstr(
                        y + i, x,
                        line[:max(0, w - 1)].ljust(max(0, w - 1)),
                        self.C(10),
                    )
            return
        if  n == 0 and self.tab == TAB_ARTIST:
            self.stdscr.addstr(y, x, " Press 5 on a track or album in any tab to show its artist"[:max(0, w - 1)].ljust(max(0, w - 1)), self.C(10))
            return
        if  n == 0 and self.tab == TAB_ALBUM:
            self.stdscr.addstr(y, x, " Press 6 on a track in any tab to show its album"[:max(0, w - 1)].ljust(max(0, w - 1)), self.C(10))
            return
        if n == 0 and self.tab == TAB_LIKED:
            FILTER_NAMES = ["All", "Tracks", "Artists", "Albums", "Playlists"]
            fn = FILTER_NAMES[self.liked_filter]
            self.stdscr.addstr(y, x, f"\n Nothing liked here — press l on items to like them\n Cycle sub-categories with [/], Alt+7, or ^←/^→, or jump directly with Alt+1-5"[:max(0, w - 1)].ljust(max(0, w - 1)), self.C(10))
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
                if isinstance(it, tuple) and it[0] == "artist_header":
                    artist_id, name = it[1]
                    liked_artist = artist_id in self.liked_artist_ids
                    base_attr = curses.A_REVERSE if selected else 0
                    offs = self._draw_line_no(yy, x, i + 1, w) if self.show_numbers else 0
                    px = x + offs; pw = max(0, w - offs - 1)
                    if pw > 0:
                        self.stdscr.addstr(yy, px, "  "[:pw], base_attr)
                        px += 2; pw -= 2
                    if pw > 0 and liked_artist:
                        self.stdscr.addstr(yy, px, "♥ "[:pw], base_attr | self.C(14))
                        px += 2; pw -= 2
                    if pw > 0:
                        suffix = " (fetching data…)" if self._loading else ""
                        display_name = (name + suffix)[:pw].ljust(pw)[:pw]
                        self.stdscr.addstr(yy, px, display_name,
                                           base_attr | self.C(7) | curses.A_BOLD)
                    continue
                if isinstance(it, tuple) and it[0] == "sep":
                    line = f"──── {it[1]} ──" if self.tab_align else f"── {it[1]} ──"
                    offs = self._draw_line_no(yy, x, i + 1, w) if self.show_numbers else 0
                    self.stdscr.addstr(yy, x + offs, line[:max(0, w - offs - 1)].ljust(max(0, w - offs - 1)),
                                       self.C(4))
                    continue
                if isinstance(it, Album):
                    yv = year_norm(it.year)
                    ys = f", {yv}" if (self.show_track_year and yv != "????") else ""
                    liked_alb = it.id in self.liked_album_ids
                    base_attr = curses.A_REVERSE if selected else 0
                    marked = (i in self.marked_left_idx)
                    offs = self._draw_line_no(yy, x, i + 1, w) if self.show_numbers else 0
                    px = x + offs; pw = max(0, w - offs - 1)
                    indent_w = 5 if self.tab_align else 3
                    pref = ("+" + " " * (indent_w - 1)) if marked else (" " * indent_w)
                    if pw > 0:
                        self.stdscr.addstr(yy, px, pref[:pw],
                                           base_attr | (self.C(15) if marked else 0))
                        px += indent_w; pw -= indent_w
                    if pw > 0 and liked_alb:
                        self.stdscr.addstr(yy, px, "♥ "[:pw], base_attr | self.C(14))
                        px += 2; pw -= 2
                    if pw > 0:
                        self.stdscr.addstr(yy, px, f"{it.title}{ys}"[:pw].ljust(pw)[:pw],
                                           base_attr | self.C(8))
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
                    liked_alb = a.id in self.liked_album_ids
                    base_attr = (curses.A_REVERSE if selected else 0) | self.C(8)
                    offs = self._draw_line_no(yy, x, i + 1, w) if self.show_numbers else 0
                    px = x + offs; pw = max(0, w - offs - 1)
                    if pw > 0:
                        self.stdscr.addstr(yy, px, "  "[:pw], base_attr)
                        px += 2; pw -= 2
                    if pw > 0 and liked_alb:
                        self.stdscr.addstr(yy, px, "♥ "[:pw],
                                           (curses.A_REVERSE if selected else 0) | self.C(14))
                        px += 2; pw -= 2
                    if pw > 0:
                        self.stdscr.addstr(yy, px,
                                           f"{a.artist} — {a.title}{ys}"[:pw].ljust(pw)[:pw], base_attr)
                    continue
                if isinstance(it, Track):
                    marked = (i in self.marked_left_idx)
                    self._draw_track_line(yy, x, w, it, selected=selected, marked=marked, idx1=i + 1)
                    continue

            if typ == "liked_mixed":
                if isinstance(it, tuple) and it[0] == "sep":
                    line = f"── {it[1]} ──"
                    offs = self._draw_line_no(yy, x, i + 1, w) if self.show_numbers else 0
                    self.stdscr.addstr(yy, x + offs, line[:max(0, w - offs - 1)].ljust(max(0, w - offs - 1)), self.C(4))
                    continue
                if isinstance(it, Track):
                    marked = (i in self.marked_left_idx)
                    self._draw_track_line(yy, x, w, it, selected=selected, marked=marked, idx1=i + 1)
                    continue
                if isinstance(it, Album):
                    yv = year_norm(it.year)
                    ys = f", {yv}" if (self.show_track_year and yv != "????") else ""
                    base_attr = curses.A_REVERSE if selected else 0
                    marked = (i in self.marked_left_idx)
                    offs = self._draw_line_no(yy, x, i + 1, w) if self.show_numbers else 0
                    px = x + offs; pw = max(0, w - offs - 1)
                    pref = "+  " if marked else "   "
                    if pw > 0:
                        self.stdscr.addstr(yy, px, pref[:pw],
                                           base_attr | (self.C(15) if marked else 0))
                        px += 3; pw -= 3
                    if pw > 0:
                        self.stdscr.addstr(yy, px, "♥ "[:pw], base_attr | self.C(14))
                        px += 2; pw -= 2
                    if pw > 0:
                        self.stdscr.addstr(yy, px,
                                           f"{it.artist} — {it.title}{ys}"[:pw].ljust(pw)[:pw],
                                           base_attr | self.C(8))
                    continue
                if isinstance(it, Artist):
                    base_attr = curses.A_REVERSE if selected else 0
                    marked = (i in self.marked_left_idx)
                    offs = self._draw_line_no(yy, x, i + 1, w) if self.show_numbers else 0
                    px = x + offs; pw = max(0, w - offs - 1)
                    pref = "+  " if marked else "   "
                    if pw > 0:
                        self.stdscr.addstr(yy, px, pref[:pw], base_attr | (self.C(15) if marked else 0))
                        px += 3; pw -= 3
                    if pw > 0:
                        self.stdscr.addstr(yy, px, "♥ "[:pw], base_attr | self.C(14))
                        px += 2; pw -= 2
                    if pw > 0:
                        self.stdscr.addstr(yy, px, it.name[:pw].ljust(pw)[:pw],
                                           base_attr | self.C(7))
                    continue
                if isinstance(it, str):  # playlist name
                    count = len(self.playlists.get(it, []))
                    base_attr = curses.A_REVERSE if selected else 0
                    marked = (i in self.marked_left_idx)
                    offs = self._draw_line_no(yy, x, i + 1, w) if self.show_numbers else 0
                    px = x + offs; pw = max(0, w - offs - 1)
                    pref = "+  " if marked else "   "
                    if pw > 0:
                        self.stdscr.addstr(yy, px, pref[:pw], base_attr | (self.C(15) if marked else 0))
                        px += 3; pw -= 3
                    if pw > 0:
                        self.stdscr.addstr(yy, px, "♥ "[:pw], base_attr | self.C(14))
                        px += 2; pw -= 2
                    if pw > 0:
                        content = f"{it} ({count} tracks)" if count else it
                        self.stdscr.addstr(yy, px, content[:pw].ljust(pw)[:pw], base_attr)
                    continue

            if typ == "playlists":
                offs = self._draw_line_no(yy, x, i + 1, w) if self.show_numbers else 0
                liked_pl = str(it) in self.liked_playlist_ids
                marked = (i in self.marked_left_idx)
                count = len(self.playlists.get(str(it), []))
                content = f"{it} ({count} tracks)" if count else str(it)
                base_attr = curses.A_REVERSE if selected else 0
                px = x + offs; pw = max(0, w - offs - 1)
                if marked:
                    if pw > 0:
                        self.stdscr.addstr(yy, px, "+ "[:pw], base_attr | self.C(15))
                        px += 2; pw -= 2
                if liked_pl:
                    if pw > 0:
                        self.stdscr.addstr(yy, px, "♥ "[:pw], base_attr | self.C(14))
                        px += 2; pw -= 2
                if pw > 0:
                    self.stdscr.addstr(yy, px, content[:pw].ljust(pw)[:pw], base_attr)
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
            if self._show_singles_eps:
                parts.append("singles/EPs: on")
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

    def _draw_overlay_box(self, title: str, lines: List[str], scroll: int, box_w: int, box_h: int, _erase_bg: bool = True) -> None:
        h, w = self.stdscr.getmaxyx()
        box_w = min(w - 6, box_w)
        box_h = min(h - 6, box_h)
        y0 = (h - box_h) // 2
        x0 = (w - box_w) // 2
        # Erase a 1-cell padding ring outside the box, clearing any sixel pixels
        # and providing a visible blank margin that separates the popup from the
        # background.  This is always done (even in the fast path) because
        # _erase_popup_bg uses raw ANSI writes and doesn't disturb ncurses state.
        pad_y = max(0, y0 - 1)
        pad_x = max(0, x0 - 2)
        pad_h = min(h - pad_y, box_h + 2)
        pad_w = min(w - pad_x, box_w + 4)
        self._erase_popup_bg(pad_y, pad_x, pad_h, pad_w)
        if _erase_bg:
            for yy in range(pad_y, pad_y + pad_h):
                try:
                    self.stdscr.addstr(yy, pad_x, " " * pad_w)
                except curses.error:
                    pass
        win = self.stdscr.derwin(box_h, box_w, y0, x0)
        win.erase()
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
        win.touchwin()  # force full resend so popup is visible over any sixel residue
        win.refresh()

    def _draw_help(self) -> None:
        lines = [
            "",
            "\x01 TABS",
            " 1 Queue  2 Search  3 Recommended  4 Mix  5 Artist  6 Album  7 Liked  8 Playlists  9 History  0 Cover",
            "",
            "\x01 PLAYBACK                                           PLAYLISTS (8)",
            " p         play/pause                               n     new list",
            " m         mute                                     d     delete list",
            " -/+       volume                                   Enter open list",
            " ←/→       seek 5s                                  Bkspc exit list",
            " Shift ←/→ seek 30s                                 e/E   enqueue to end/next",
            " ,/<       prev track                               a     add selected/marked to playlist",
            " ./>       next track",
            " Enter     play selected without adding to queue, or jump to new selected track in queue",
            " P         resume playback from last known position",
            "",
            "\x01 ACTIONS",
            " :/!       contextual actions menu",
            " e/E       enqueue to end/next",
            " b         add selected to priority queue (play next)",
            " B         clear priority flags",
            " 4         show mix based on selected track, album, or artist",
            " 0         show album cover art (requires chafa or ueberzugpp)",
            " 5/6       show artist/album content relative to selected",
            " s         find similar artists",
            " D         download (selected, marked, album, all tracks of a playlist)",
            " Space     (un)mark selected and advance",
            " u/U       (un)mark all",
            " l         (un)like selected or marked: track, album, artist,  playlist",
            " L         (un)like playing track",
            " *         contextual like/unlike menu for selected track: artist/album/playlist",
            " J/K       move track or marked tracks down/up",
            " x         remove track from queue/playlist",
            " X         clear queue/playlist",
            " i/I       show selected/playing info",
            " v/V       show lyrics of selected/playing",
            " o/O       open selected/playing in browser",
            "",
            "\x01 GENERAL",
            " /         search TIDAL",
            " f         filter term in current view (in cover tab: filter lyrics)",
            " (/)       prev/next filter hit (in cover tab: prev/next lyrics match)",
            " Esc       close prompts",
            " Q         quit",
            "",
            "\x01 VIEW",
            " q         mini-queue overlay",
            " Tab       move cursor between main view and mini-queue overlay",
            " z         jump to playing track",
            " ^←/^→/1-9 Navigate main tabs and sub-tabs",
            " Alt+1-5   jump directly to Liked sub-tabs (Allᴹ⁻¹ Tracksᴹ⁻² Artistsᴹ⁻³ Albumsᴹ⁻⁴ Playlistsᴹ⁻⁵)",
            " Alt+7     jump to Liked tab then cycle its sub-tabs",
            " [/]       cycle Liked sub-tabs",
            " ;/Bkspc   go back to last tab without refreshing",
            " g/G       go to top/bottom",
            " j/k/↓/↑   go down/up",
            " ^↓/^↑     jump to adjacent sub-section (tabs 5 and 7) or to next artist/album in list",
            " c         color/bw",
            " w/y/d/n   album/year/duration/line number fields",
            " T         status bar",
            " \\         toggle TSV mode",
            "",
            "\x01 TOGGLES",
            " A         autoplay mode (off, mix, recommended)",
            " R         repeat mode (off, all, one)",
            " S         shuffle (off, on)",
            " F         file quality",
            " #         show/hide singles and EPs in artist tab",
            "",
            "\x01 AUTOPLAY MODES",
            " off:         no automatic queue extension",
            " mix:         refill queue from track mix",
            " recommended: refill queue from track recommendations",
            " Refill candidates are picked haphazardly based on suggestions (mix or recommended)",
            " pooled from recent play history + upcoming queue tracks",
            "",
            "\x01 SETTINGS (settings.json)",
            " API:",
            f" api: API base URL (default {DEFAULT_API})",
            "",
            " Autoplay:",
            " autoplay_n:  number of tracks to add per autoplay refill (default 3)",
            "",
            " History tab:",
            " history_max: max history entries to keep (default 0 = unlimited)",
            "",
            " Playback:",
            " auto_resume_playback: resume last position on startup (default false)",
            "",
            " Artist tab:",
            " include_singles_and_eps_in_artist_tab: show singles/EPs (default false, toggle with #)",
            "",
            " Download file hierarchy:",
            " download_dir (Linux default /tmp/tuifi/)",
            " download_structure (default {artist}/{artist} - {album} ({year}))",
            " download_filename (default {track:02d}. {artist} - {title})",
            " Playlists can be downloaded with hierarchy or flat"
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
            f"\x01 tuifi v{VERSION}",
        ]
        box_h = 38
        h, _ = self.stdscr.getmaxyx()
        inner_h = min(h - 8, box_h - 2)
        self._help_max_scroll = max(0, len(lines) - inner_h)
        self._draw_overlay_box("Help  (? or q to close)", lines, self.help_scroll, box_w=97, box_h=box_h)

    def _draw_info(self, _erase_bg: bool = True) -> None:
        if self.info_artist and not self.info_track and not self.info_album:
            ar = self.info_artist
            lines: List[str] = [f"Artist : {ar.name}", f"ID     : {ar.id}" if ar.id else "", ""]
            if self.info_loading:
                lines.append("Loading artist info…")
            else:
                payload = self.info_payload or {}
                if "error" in payload:
                    lines.append(f"Error: {payload.get('error')}")
                else:
                    data = payload.get("data") if isinstance(payload, dict) else payload
                    if not isinstance(data, dict):
                        data = payload
                    if isinstance(data, dict):
                        for k in ("popularity", "numberOfAlbums", "numberOfTracks",
                                  "artistTypes", "url"):
                            if k in data:
                                lines.append(f"{k}: {data[k]}")
                    if payload.get("_similar"):
                        lines.append(f"  [s] browse {len(payload['_similar'])} similar artists")
            lines = [l for l in lines if l is not None]
            self._draw_overlay_box("Artist info", lines, self.info_scroll, box_w=76, box_h=20, _erase_bg=_erase_bg)
            return
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
            self._draw_overlay_box("Album info", lines, self.info_scroll, box_w=76, box_h=16, _erase_bg=_erase_bg)
            return

        t = self.info_track
        if not t:
            self._draw_overlay_box("Info", ["(no selection)"], 0, box_w=72, box_h=10, _erase_bg=_erase_bg)
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
            lines.append("Loading /info…")
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
        self._draw_overlay_box("Track info", lines, self.info_scroll, box_w=76, box_h=16, _erase_bg=_erase_bg)

    def _draw_lyrics(self, _erase_bg: bool = True) -> None:
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
        h, _ = self.stdscr.getmaxyx()
        inner_h = min(h - 8, 30 - 2)
        self._lyrics_overlay_max_scroll = max(0, len(lines) - inner_h)
        self._draw_overlay_box(title[:80], lines, self.lyrics_scroll, box_w=84, box_h=30, _erase_bg=_erase_bg)

    def _draw_liked_filter_bar(self, y: int, x: int, w: int) -> None:
        FILTER_LABELS = ["Allᴹ⁻¹", "Tracksᴹ⁻²", "Artistsᴹ⁻³", "Albumsᴹ⁻⁴", "Playlistsᴹ⁻⁵"]
        self.stdscr.addstr(y, x, " " * max(0, w - 1), self.C(4))
        cx = x + 1
        for i, label in enumerate(FILTER_LABELS):
            if cx >= w - 1:
                break
            attr = self.C(5) if i == self.liked_filter else self.C(4)
            self.stdscr.addstr(y, cx, label[:max(0, w - cx - 1)], attr)
            cx += len(label)
            if i < len(FILTER_LABELS) - 1 and cx < w - 2:
                self.stdscr.addstr(y, cx, "  ", self.C(4))
                cx += 2

    def draw(self) -> None:
        if not self._need_redraw:
            return

        h, w = self.stdscr.getmaxyx()
        top_h = 3 if self.tab == TAB_LIKED else 2
        status_h = 2
        usable_h = h - top_h - status_h

        if self._redraw_status_only and not self.show_help:
            self._draw_status(h - status_h, 0, w)
            self.stdscr.noutrefresh()
            curses.doupdate()
            self._need_redraw = False
            self._redraw_status_only = False
            return

        self._queue_redraw_only = False
        self._redraw_status_only = False
        self._need_redraw = False

        # On the cover tab with a valid cover, keep the existing sixel in place
        # while curses repaints the UI chrome — the new cover is written after
        # stdscr.refresh(), and overlays are drawn on top of it afterwards.
        # On any other tab (or if cover_path is unset), erase any stale sixel first.
        if not (self.tab == TAB_COVER and self.cover_path):
            self._cover_erase_terminal()
        # Full redraw: stdscr.erase()+refresh() will overwrite the sixel area with
        # spaces, so mark it as no longer visible.  This ensures _render_cover_image
        # re-writes it even if the render key is unchanged.
        self._cover_sixel_visible = False
        self.stdscr.erase()

        # When TAB_QUEUE is active the queue fills the full left panel; don't also
        # draw it as a right-side overlay (that would show the queue twice).
        queue_panel = self.queue_overlay and self.tab != TAB_QUEUE
        left_w = w if not queue_panel else max(20, w - 44)

        self._draw_tabs(0, 0, w)
        if self.tab == TAB_LIKED:
            self._draw_liked_filter_bar(2, 0, w)
        self._draw_left(top_h, 0, usable_h, left_w)

        if queue_panel:
            self._draw_queue(top_h, left_w, usable_h, w - left_w)

        self._draw_status(h - status_h, 0, w)

        if self.tab == TAB_COVER and self._cover_lyrics and not queue_panel:
            # Auto-fetch lyrics for the playing track if not already loaded.
            t_cov = self.current_track
            if t_cov and not self.lyrics_loading and not (self.lyrics_track_id == t_cov.id and self.lyrics_lines):
                self.toggle_lyrics(t_cov)
                self.lyrics_overlay = False
            lyrics_w = self._lyrics_panel_w(w)
            self._draw_cover_lyrics_panel(top_h, w - lyrics_w, usable_h, lyrics_w)

        if self.tab == TAB_COVER and self.cover_path:
            # Prevent ncurses scroll-region optimisation for the content rows.
            # redrawln marks those rows as physically corrupted so ncurses
            # rewrites each cell individually on the next refresh instead of
            # issuing ESC[S/ESC[T sequences that shift the sixel image.
            self.stdscr.redrawln(top_h, usable_h)
        self.stdscr.refresh()

        # After curses has refreshed, write the cover sixel.  Overlays (info,
        # lyrics, help) are drawn AFTER the sixel so they appear on top of it.
        # _draw_overlay_box calls _erase_popup_bg + win.touchwin() to ensure the
        # popup area is clear and its content is fully resent over the sixel.
        if self.tab == TAB_COVER and self.cover_path and not self.show_help:
            # _cover_sixel_visible was cleared at the top of this full-redraw path
            # (see below), so _render_cover_image will always re-write the sixel
            # after a full stdscr.erase()+refresh().  The flag is only kept True
            # during status-only redraws so those skip the expensive write.
            self._render_cover_image()
            # Re-assert the tab-bar and status-bar rows so that any scroll
            # artifacts are overwritten.  redrawln tells ncurses the physical
            # terminal content of those rows is corrupted (by the sixel write)
            # and forces a full character-cell repaint on the next refresh —
            # unlike touchline which ncurses may optimise away when it thinks
            # curscr already has the right content.
            if self._cover_sixel_visible:
                self.stdscr.redrawln(0, top_h)
                # Cover includes a one-row gap at h-status_h-1 to prevent the
                # sixel from touching the terminal's last few rows (some terminals
                # auto-scroll when sixel data reaches the bottom edge).  When a
                # panel scrolls UP the displaced sixel lands on that gap row, so
                # include it (status_h+1 rows) to ensure it is fully repainted.
                self.stdscr.redrawln(h - status_h - 1, status_h + 1)
                self.stdscr.refresh()

        if self.show_help:
            self._draw_help()

    # ---------------------------------------------------------------------------
    # tab switching
    # ---------------------------------------------------------------------------
    def switch_tab(self, t: int, refresh: bool = True) -> None:
        # save current tab position before switching
        self._tab_positions[self.tab] = (self.left_idx, self.left_scroll)
        if t != self.tab:
            self._prev_tab = self.tab
            if self.tab == TAB_COVER:
                self._cover_clear_image()

        self.tab = t
        self._loading = False
        self._loading_key = ""
        self.show_help = False
        self.marked_left_idx.clear()

        if self.tab == TAB_QUEUE:
            self.focus = "queue"
        elif self.focus == "queue":
            self.focus = "left"

        # tabs that fetch fresh network data reset cursor; others restore saved position
        fresh_fetch = refresh and t in (TAB_RECOMMENDED, TAB_MIX, TAB_ARTIST, TAB_ALBUM)
        if not fresh_fetch and t in self._tab_positions:
            self.left_idx, self.left_scroll = self._tab_positions[t]
        else:
            self.left_idx = 0
            self.left_scroll = 0

        if t == TAB_RECOMMENDED and refresh:
            ctx = self._selected_left_track() or self.current_track
            self.fetch_recommended_async(ctx)
        elif t == TAB_MIX and refresh:
            ctx = self._selected_left_track() or self.current_track
            self.fetch_mix_async(ctx)
        elif t == TAB_LIKED and refresh:
            self.fetch_liked_async()
        elif t == TAB_ARTIST and refresh:
            ctx = self._current_selection_track() or self.current_track
            self.fetch_artist_async(ctx)
        elif t == TAB_PLAYLISTS:
            self.playlist_names = sorted(self.playlists.keys())
            self.playlist_view_name = None
            self.playlist_view_tracks = []
        elif t == TAB_HISTORY:
            pass  # history_tracks always up to date
        elif t == TAB_COVER:
            self.fetch_cover_async(self.current_track)
            if self.queue_overlay:
                self.jump_to_playing_in_queue()

        self._need_redraw = True
        self._redraw_status_only = False

    # ---------------------------------------------------------------------------
    # navigation
    # ---------------------------------------------------------------------------
    def _page_step(self) -> int:
        h, _ = self.stdscr.getmaxyx()
        return max(1, h - 2 - 2 - 1)

    def nav_page(self, direction: int) -> None:
        step = self._page_step()
        if self._queue_context():
            self.queue_cursor = clamp(self.queue_cursor + direction * step, 0, max(0, len(self.queue_items) - 1))
        else:
            _typ, items = self._left_items()
            self.left_idx = clamp(self.left_idx + direction * step, 0, max(0, len(items) - 1))

    def nav_home(self) -> None:
        if self._queue_context():
            self.queue_cursor = 0
        else:
            self.left_idx = 0

    def nav_end(self) -> None:
        if self._queue_context():
            self.queue_cursor = max(0, len(self.queue_items) - 1)
        else:
            _typ, items = self._left_items()
            self.left_idx = max(0, len(items) - 1)

    def jump_to_playing_in_queue(self) -> None:
        if not self.queue_items:
            self.toast("Queue empty")
            return
        if not self.queue_overlay:
            self.queue_overlay = True
        self.focus = "queue"
        self.queue_cursor = clamp(self.queue_play_idx, 0, len(self.queue_items) - 1)
        self._need_redraw = True
        self._redraw_status_only = False

    # ---------------------------------------------------------------------------
    # main loop
    # ---------------------------------------------------------------------------
    def run(self) -> None:
        last_persist = 0.0
        # Disable all scroll optimisations — ncurses may otherwise issue terminal
        # scroll sequences when updating list panels, shifting the entire screen
        # content including the sixel cover image.
        self.stdscr.idlok(False)
        self.stdscr.scrollok(False)

        if self.tab == TAB_LIKED:
            self.fetch_liked_async()

        # Auto-resume playback from last session if enabled and queue is populated.
        if self.settings.get("auto_resume_playback") and self.queue_items and not self.mp.alive():
            _resume_idx = int(self.settings.get("_resume_queue_idx", self.queue_play_idx))
            _resume_pos = float(self.settings.get("_resume_position", 0.0))
            _resume_idx = clamp(_resume_idx, 0, len(self.queue_items) - 1)
            self.play_queue_index(_resume_idx, start_pos=_resume_pos)
            if _resume_pos > 1.0:
                self.toast(f"Resumed from {fmt_time(_resume_pos)}")

        while True:
            now = time.time()

            if self._liked_refresh_due and now >= self._liked_refresh_due:
                self._liked_refresh_due = 0.0
                if self.tab == TAB_LIKED:
                    self.fetch_liked_async()

            self._do_info_fetch_if_due()

            if self.info_overlay and self.info_follow_selection:
                if self._queue_context():
                    _qt = self._current_selection_track()
                    if _qt and (self.info_track is None or _qt.id != self.info_track.id):
                        self._request_info_refresh(_qt)
                else:
                    _it = self._selected_left_item()
                    if isinstance(_it, Artist):
                        if self.info_artist is None or self.info_artist.id != _it.id:
                            self.open_info_artist(_it)
                    elif isinstance(_it, tuple) and _it[0] == "artist_header":
                        _ar = Artist(id=_it[1][0], name=_it[1][1])
                        if self.info_artist is None or self.info_artist.id != _ar.id:
                            self.open_info_artist(_ar)
                    elif isinstance(_it, Album):
                        if self.info_album is None or self.info_album.id != _it.id:
                            self.open_info_album(_it)
                    elif isinstance(_it, tuple) and _it[0] == "album_title" and isinstance(_it[1], Album):
                        _alb = _it[1]
                        if self.info_album is None or self.info_album.id != _alb.id:
                            self.open_info_album(_alb)
                    else:
                        _t = self._current_selection_track()
                        if _t and (self.info_track is None or _t.id != self.info_track.id):
                            self._request_info_refresh(_t)

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
                    "tab_align": self.tab_align,
                    "include_singles_and_eps_in_artist_tab": self._show_singles_eps,
                })
                try:
                    save_settings(self.settings)
                except Exception:
                    pass

            # Debounced prev/next skip: accumulate rapid keypresses and jump
            # directly to the final target track after a 150 ms pause.
            if self._skip_delta != 0 and now - self._skip_at >= 0.15:
                n_q = len(self.queue_items)
                if n_q > 0:
                    target = self.queue_play_idx + self._skip_delta
                    if self.repeat_mode == 1:
                        target = target % n_q
                    else:
                        target = clamp(target, 0, n_q - 1)
                self._skip_delta = 0
                if n_q > 0:
                    self.play_queue_index(target)

            self.draw()

            ch = self.stdscr.getch()
            if ch == -1:
                time.sleep(0.004)
                continue

            self._need_redraw = True
            self._redraw_status_only = False

            if ch == 27:
                # Peek for an escape sequence or Alt+digit
                _c2 = self.stdscr.getch()
                _ctrl_digit = 0
                if _c2 == ord('['):
                    # CSI sequence: read until alpha or ~
                    _seq = ""
                    for _ in range(24):
                        _c3 = self.stdscr.getch()
                        if _c3 == -1:
                            break
                        _seq += chr(_c3)
                        if chr(_c3).isalpha() or chr(_c3) == '~':
                            break
                    # Format 1 — CSI u: "N;Mu"  (kitty/foot/CSI u protocol)
                    #   modifier M: Ctrl bit = 4, so Ctrl-only = 5
                    _m = re.match(r'^(\d+);(\d+)u$', _seq)
                    if _m:
                        _key_n, _mod = int(_m.group(1)), int(_m.group(2))
                        if (_mod - 1) & 4:
                            if 49 <= _key_n <= 57:
                                _ctrl_digit = _key_n - 48
                            elif 1 <= _key_n <= 9:
                                _ctrl_digit = _key_n
                    # Format 2 — xterm modifyOtherKeys: "27;M;N~"
                    if not _ctrl_digit:
                        _m2 = re.match(r'^27;(\d+);(\d+)~$', _seq)
                        if _m2:
                            _mod2, _key_n2 = int(_m2.group(1)), int(_m2.group(2))
                            if (_mod2 - 1) & 4:
                                if 49 <= _key_n2 <= 57:
                                    _ctrl_digit = _key_n2 - 48
                                elif 1 <= _key_n2 <= 9:
                                    _ctrl_digit = _key_n2
                elif ord('1') <= _c2 <= ord('9'):
                    # Alt+digit (ESC immediately followed by a digit, nodelay so no gap)
                    _ctrl_digit = _c2 - ord('0')
                elif _c2 != -1:
                    curses.ungetch(_c2)  # not a recognised sequence — put it back
                if _ctrl_digit:
                    _LIKED_FILTER_NAMES = ["All", "Tracks", "Artists", "Albums", "Playlists"]
                    if 1 <= _ctrl_digit <= 5:
                        _lsub = _ctrl_digit - 1
                        if self.tab != TAB_LIKED:
                            self.switch_tab(TAB_LIKED, refresh=False)
                            self.fetch_liked_async()
                        self.liked_filter = _lsub
                        self.left_idx = 0
                        self.left_scroll = 0
                        self.toast(f"Liked: {_LIKED_FILTER_NAMES[_lsub]}")
                        self._need_redraw = True
                    elif _ctrl_digit == 7:
                        if self.tab != TAB_LIKED:
                            self.switch_tab(TAB_LIKED, refresh=False)
                            self.fetch_liked_async()
                            self.toast(f"Liked: {_LIKED_FILTER_NAMES[self.liked_filter]}")
                        else:
                            self.liked_filter = (self.liked_filter + 1) % len(_LIKED_FILTER_NAMES)
                            self.left_idx = 0
                            self.left_scroll = 0
                            self.toast(f"Liked: {_LIKED_FILTER_NAMES[self.liked_filter]}")
                        self._need_redraw = True
                    continue
                # Plain ESC: dismiss overlays
                if self.show_help:
                    self.show_help = False
                elif self.tab == TAB_COVER and self._lyrics_filter_q:
                    self._lyrics_filter_q = ""
                    self._lyrics_filter_hits = []
                    self._lyrics_filter_pos = -1
                elif self.filter_q:
                    self.filter_q = ""
                    self.filter_hits = []
                    self.filter_pos = -1
                    self.toast("Filter cleared")
                continue

            if ch == ord("Q"):
                self._ueberzug_stop()
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

            if ch == ord("/"):
                self.do_search_prompt_anywhere()
                continue
            if ch == ord("f"):
                if self.tab == TAB_COVER and self._cover_lyrics and not self.queue_overlay:
                    self.lyrics_filter_prompt()
                else:
                    self.filter_prompt()
                continue
            if ch == ord("("):
                if self.tab == TAB_COVER and self._cover_lyrics and not self.queue_overlay:
                    self.lyrics_filter_next(-1)
                else:
                    self.filter_next(-1)
                continue
            if ch == ord(")"):
                if self.tab == TAB_COVER and self._cover_lyrics and not self.queue_overlay:
                    self.lyrics_filter_next(1)
                else:
                    self.filter_next(+1)
                continue

            # V in cover tab:
            #   - If miniqueue is open → close it and show lyrics (one action, no toggle).
            #   - If miniqueue is closed → toggle inline lyrics panel on/off.
            if ch == ord("V") and self.tab == TAB_COVER:
                if self.queue_overlay:
                    # Always: close miniqueue and ensure lyrics are shown.
                    self.queue_overlay = False
                    self.focus = "left"
                    self._cover_lyrics = True
                else:
                    self._cover_lyrics = not self._cover_lyrics
                if self._cover_lyrics:
                    # Fetch lyrics if not already loaded.
                    if self.current_track and not self.lyrics_lines and not self.lyrics_loading:
                        self.toggle_lyrics(self.current_track)
                        self.lyrics_overlay = False
                self._cover_render_key = ""   # force re-render at new width
                self._cover_render_buf = None
                continue

            # v: lyrics popup for the selected or playing track.
            if ch == ord("v"):
                t_sel = self._current_selection_track() or self.current_track
                if t_sel:
                    self.show_lyrics_dialog(t_sel)
                continue
            if ch == ord("V"):
                _vt = self.current_track
                if _vt:
                    self.show_lyrics_dialog(_vt)
                continue

            if ch == ord("i"):
                self.toggle_info_selected()
                continue
            if ch == ord("I"):
                self.toggle_info_playing()
                continue

            if ch == ord("s"):
                _sit = self._selected_left_item() if not self._queue_context() else None
                _sar: Optional[Artist] = None
                _salbum_id = 0
                if self.info_artist:
                    _sar = self.info_artist
                elif isinstance(_sit, Artist):
                    _sar = _sit
                elif isinstance(_sit, tuple) and _sit[0] == "artist_header":
                    _sar = Artist(id=_sit[1][0], name=_sit[1][1])
                elif isinstance(_sit, tuple) and _sit[0] == "album_title" and isinstance(_sit[1], Album):
                    _alb = _sit[1]
                    _sar = Artist(id=0, name=_alb.artist, track_id=_alb.track_id)
                    _salbum_id = _alb.id
                elif isinstance(_sit, Album):
                    _sar = Artist(id=0, name=_sit.artist, track_id=_sit.track_id)
                    _salbum_id = _sit.id
                else:
                    _t = self._current_selection_track()
                    if _t:
                        _sar = Artist(id=_t.artist_id or 0, name=_t.artist,
                                      track_id=_t.id if not _t.artist_id else None)
                if not _sar and self.tab == TAB_COVER and self.current_track:
                    _t = self.current_track
                    _sar = Artist(id=_t.artist_id or 0, name=_t.artist,
                                  track_id=_t.id if not _t.artist_id else None)
                if _sar:
                    self.show_similar_artists_dialog(_sar, album_id=_salbum_id)
                continue

            if ch == ord("o"):
                it = self._selected_left_item()
                if isinstance(it, Artist) and it.id:
                    self.open_url(f"{self.web_base()}/artist/{it.id}")
                elif isinstance(it, tuple) and it[0] == "artist_header":
                    self.open_url(f"{self.web_base()}/artist/{it[1][0]}")
                elif isinstance(it, Album) and it.id:
                    self.open_url(f"{self.web_base()}/album/{it.id}")
                else:
                    t = self._current_selection_track()
                    if t:
                        self.open_url(f"{self.web_base()}/track/{t.id}")
                continue
            if ch == ord("O"):
                if self.tab == TAB_ARTIST and self.artist_ctx:
                    self.open_url(f"{self.web_base()}/artist/{self.artist_ctx[0]}")
                elif self.tab == TAB_ALBUM and self.album_header and self.album_header.id:
                    self.open_url(f"{self.web_base()}/album/{self.album_header.id}")
                elif self.current_track:
                    self.open_url(f"{self.web_base()}/track/{self.current_track.id}")
                continue

            if ch == ord("P"):
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
                elif self.queue_overlay and self.tab == TAB_COVER:
                    self.focus = "queue"
                self.toast("Queue overlay: on" if self.queue_overlay else "Queue overlay: off")
                continue

            if ch == ord("\t") and self.queue_overlay:
                self.focus = "queue" if self.focus == "left" else "left"
                continue

            # Tab switching: 0-9
            if ch == ord("0"):
                self.switch_tab(TAB_COVER)
                continue

            if ch in (ord("1"), ord("2"), ord("3"), ord("4"),
                      ord("5"), ord("6"), ord("7"), ord("8"), ord("9")):
                mapping = {
                    ord("1"): TAB_SEARCH,
                    ord("2"): TAB_QUEUE,
                    ord("3"): TAB_RECOMMENDED,
                    ord("4"): TAB_MIX,
                    ord("5"): TAB_ARTIST,
                    ord("6"): TAB_ALBUM,
                    ord("7"): TAB_LIKED,
                    ord("8"): TAB_PLAYLISTS,
                    ord("9"): TAB_HISTORY,
                }
                t_num = mapping[ch]
                if t_num == TAB_RECOMMENDED:
                    if self.tab == TAB_COVER:
                        self.switch_tab(TAB_RECOMMENDED)
                    else:
                        ctx = self._current_selection_track()
                        if ctx:
                            self.switch_tab(TAB_RECOMMENDED, refresh=False)
                            self.fetch_recommended_async(ctx)
                        else:
                            self.switch_tab(TAB_RECOMMENDED, refresh=False)
                elif t_num == TAB_MIX:
                    if self.tab == TAB_COVER:
                        self.switch_tab(TAB_MIX)
                    else:
                        it = self._selected_left_item() if not self._queue_context() else None
                        ctx = self._current_selection_track()
                        if isinstance(it, Album):
                            self.switch_tab(TAB_MIX, refresh=False)
                            self.fetch_mix_from_album_async(it)
                        elif isinstance(it, Artist):
                            self.switch_tab(TAB_MIX, refresh=False)
                            self.fetch_mix_from_artist_async(it)
                        elif isinstance(it, tuple) and it[0] == "artist_header":
                            self.switch_tab(TAB_MIX, refresh=False)
                            self.fetch_mix_from_artist_async(Artist(id=it[1][0], name=it[1][1]))
                        elif ctx:
                            self.switch_tab(TAB_MIX, refresh=False)
                            self.fetch_mix_async(ctx)
                        else:
                            self.switch_tab(TAB_MIX, refresh=False)

                elif t_num == TAB_ARTIST:
                    if self.tab == TAB_COVER:
                        self.switch_tab(TAB_ARTIST)
                        continue
                    if self.tab == TAB_ARTIST and not self._queue_context():
                        continue
                    if not self._queue_context() and self.tab == TAB_ALBUM and self.album_header:
                        self._open_artist_from_album_async(self.album_header)
                        continue
                    it = self._selected_left_item() if not self._queue_context() else None
                    ctx = self._current_selection_track()
                    if isinstance(it, Artist):
                        self.open_artist_by_id(it.id, it.name)
                    elif isinstance(it, Album):
                        self._open_artist_from_album_async(it)
                    elif ctx:
                        self.switch_tab(TAB_ARTIST, refresh=False)
                        self.fetch_artist_async(ctx)
                    else:
                        self.switch_tab(TAB_ARTIST, refresh=False)

                elif t_num == TAB_ALBUM:
                    if self.tab == TAB_COVER:
                        self.switch_tab(TAB_ALBUM)
                    else:
                        it = self._selected_left_item() if not self._queue_context() else None
                        ctx = self._current_selection_track()
                        if isinstance(it, Album):
                            self.open_album_from_album_obj(it)
                        elif ctx:
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
                self._skip_delta -= 1
                self._skip_at = time.time()
                continue
            if ch in (ord(">"), ord(".")):
                self._skip_delta += 1
                self._skip_at = time.time()
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
            if ch == ord("#"):
                self._show_singles_eps = not self._show_singles_eps
                self.settings["include_singles_and_eps_in_artist_tab"] = self._show_singles_eps
                self.toast(f"Singles/EPs: {'on' if self._show_singles_eps else 'off'}")
                if self.tab == TAB_ARTIST and self._last_artist_fetch_track:
                    self.fetch_artist_async(self._last_artist_fetch_track)
                self._need_redraw = True
                self._redraw_status_only = False
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
            if ch == ord("*"):
                self.like_popup_from_playing()
                continue
            if ch in (ord(":"), ord("!")):
                self.context_actions_popup()
                continue
            if ch == ord(";"):
                if self._prev_tab != self.tab:
                    self.switch_tab(self._prev_tab, refresh=False)
                continue
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                if not (self.tab == TAB_PLAYLISTS and self.playlist_view_name is not None):
                    if self._prev_tab != self.tab:
                        self.switch_tab(self._prev_tab, refresh=False)
                    continue
                # falls through to the TAB_PLAYLISTS block to exit playlist view
            if ch in (ord("["), ord("]")) and self.tab == TAB_LIKED:
                _LIKED_FILTER_NAMES = ["All", "Tracks", "Artists", "Albums", "Playlists"]
                delta = 1 if ch == ord("]") else -1
                self.liked_filter = (self.liked_filter + delta) % len(_LIKED_FILTER_NAMES)
                self.left_idx = 0
                self.left_scroll = 0
                self.toast(f"Liked: {_LIKED_FILTER_NAMES[self.liked_filter]}")
                continue

            # Ctrl+Left/Ctrl+Right: cycle through all tabs including Liked subtabs
            # Sequence: Queue Search Recommended Mix Artist Album
            #           Liked/All Liked/Tracks Liked/Artists Liked/Albums Liked/Playlists
            #           Playlists History  (wraps)
            def _is_ctrl_right(c: int) -> bool:
                try:
                    return curses.keyname(c) in (b"kRIT5", b"kRIT3")
                except Exception:
                    return c in (444, 560)
            def _is_ctrl_left(c: int) -> bool:
                try:
                    return curses.keyname(c) in (b"kLFT5", b"kLFT3")
                except Exception:
                    return c in (443, 545)
            if isinstance(ch, int) and (_is_ctrl_left(ch) or _is_ctrl_right(ch)):
                _NAV_SEQ = [
                    (TAB_QUEUE, 0), (TAB_SEARCH, 0),
                    (TAB_RECOMMENDED, 0), (TAB_MIX, 0),
                    (TAB_ARTIST, 0), (TAB_ALBUM, 0),
                    (TAB_LIKED, 0), (TAB_LIKED, 1), (TAB_LIKED, 2),
                    (TAB_LIKED, 3), (TAB_LIKED, 4),
                    (TAB_PLAYLISTS, 0), (TAB_HISTORY, 0),
                ]
                cur_f = self.liked_filter if self.tab == TAB_LIKED else 0
                cur_pos = next(
                    (i for i, (t, f) in enumerate(_NAV_SEQ)
                     if t == self.tab and f == cur_f), 0)
                delta = 1 if _is_ctrl_right(ch) else -1
                nxt_tab, nxt_f = _NAV_SEQ[(cur_pos + delta) % len(_NAV_SEQ)]
                if nxt_tab == TAB_LIKED:
                    # Switch to liked tab and set filter without full refresh
                    if self.tab != TAB_LIKED:
                        self.switch_tab(TAB_LIKED, refresh=False)
                        self.fetch_liked_async()
                    self.liked_filter = nxt_f
                    self.left_idx = 0
                    self.left_scroll = 0
                    _LIKED_FILTER_NAMES = ["All", "Tracks", "Artists", "Albums", "Playlists"]
                    self.toast(f"Liked: {_LIKED_FILTER_NAMES[nxt_f]}")
                else:
                    self.switch_tab(nxt_tab, refresh=True)
                self._need_redraw = True
                continue

            # Ctrl+Down/Ctrl+Up: jump between album groups/sections
            def _is_ctrl_down(c: int) -> bool:
                try: return curses.keyname(c) in (b"kDN5", b"kDN3")
                except Exception: return False
            def _is_ctrl_up(c: int) -> bool:
                try: return curses.keyname(c) in (b"kUP5", b"kUP3")
                except Exception: return False
            if isinstance(ch, int) and (_is_ctrl_down(ch) or _is_ctrl_up(ch)):
                _dir = 1 if _is_ctrl_down(ch) else -1

                # Group key for album-boundary detection: (year, album)
                def _gkey(t: Track) -> tuple:
                    return (year_norm(t.year), t.album.lower())

                def _alb_down(lst: list, cur: int) -> int:
                    if not (0 <= cur < len(lst)) or not isinstance(lst[cur], Track):
                        return cur
                    key = _gkey(lst[cur])
                    i = cur + 1
                    while i < len(lst) and isinstance(lst[i], Track):
                        if _gkey(lst[i]) != key:
                            return i
                        i += 1
                    return cur  # no next group

                def _alb_up(lst: list, cur: int) -> int:
                    if not (0 <= cur < len(lst)) or not isinstance(lst[cur], Track):
                        return cur
                    key = _gkey(lst[cur])
                    i = cur - 1
                    while i >= 0 and isinstance(lst[i], Track) and _gkey(lst[i]) == key:
                        i -= 1
                    start = i + 1
                    if start < cur:
                        return start  # jump to start of current group
                    if i >= 0 and isinstance(lst[i], Track):
                        prev_key = _gkey(lst[i])
                        while i > 0 and isinstance(lst[i - 1], Track) and _gkey(lst[i - 1]) == prev_key:
                            i -= 1
                        return i
                    return cur  # no prev group

                if self._queue_context():
                    # Jump between album groups in queue
                    _q = self.queue_items
                    _c = self.queue_cursor
                    _n = _alb_down(_q, _c) if _dir > 0 else _alb_up(_q, _c)
                    self.queue_cursor = clamp(_n, 0, max(0, len(_q) - 1))
                    self._need_redraw = True
                else:
                    _typ, _items = self._left_items()
                    _cur = self.left_idx
                    _new = _cur

                    if self.tab == TAB_LIKED and self.liked_filter == 0:
                        # sep-based section jump
                        if _dir > 0:
                            _i = _cur + 1
                            while _i < len(_items):
                                if isinstance(_items[_i], tuple) and _items[_i][0] == "sep":
                                    _j = _i + 1
                                    while _j < len(_items) and isinstance(_items[_j], tuple) and _items[_j][0] == "sep":
                                        _j += 1
                                    _new = min(_j, len(_items) - 1)
                                    break
                                _i += 1
                            else:
                                _new = len(_items) - 1
                        else:
                            # Walk back to the sep that starts our section, then back to the previous sep
                            _i = _cur - 1
                            while _i >= 0 and not (isinstance(_items[_i], tuple) and _items[_i][0] == "sep"):
                                _i -= 1
                            # _i is now at current section's sep (or -1)
                            if _i > 0:
                                _i -= 1
                                while _i >= 0 and not (isinstance(_items[_i], tuple) and _items[_i][0] == "sep"):
                                    _i -= 1
                                # _i is at prev section's sep; jump to its first non-sep item
                                _j = (_i + 1) if _i >= 0 else 0
                                while _j < len(_items) and isinstance(_items[_j], tuple) and _items[_j][0] == "sep":
                                    _j += 1
                                _new = _j
                            # if _i <= 0 we're already in the first section, stay

                    elif self.tab == TAB_ARTIST:
                        # Artist tab: full-cycle — artist_header ↔ Albums ↔ Tracks (album-groups)
                        _artist_idx  = 0 if (_items and isinstance(_items[0], tuple) and _items[0][0] == "artist_header") else -1
                        _first_album = next((i for i, x in enumerate(_items) if isinstance(x, Album)), len(_items))
                        _first_track = next((i for i, x in enumerate(_items) if isinstance(x, Track)), len(_items))
                        _last_album  = next((i for i in range(len(_items) - 1, -1, -1) if isinstance(_items[i], Album)), -1)
                        _cur_item    = _items[_cur] if 0 <= _cur < len(_items) else None

                        if _dir > 0:
                            if isinstance(_cur_item, tuple) and _cur_item[0] == "artist_header":
                                _new = _first_album if _first_album < len(_items) else _first_track
                            elif isinstance(_cur_item, Album):
                                _new = _first_track if _first_track < len(_items) else _cur
                            elif isinstance(_cur_item, Track):
                                _new = _alb_down(_items, _cur)
                        else:
                            if isinstance(_cur_item, Album):
                                _new = max(0, _artist_idx) if _artist_idx >= 0 else 0
                            elif isinstance(_cur_item, Track):
                                _n2 = _alb_up(_items, _cur)
                                if _n2 == _cur:
                                    # first track of first group → jump to last album
                                    _new = _last_album if _last_album >= 0 else max(0, _artist_idx if _artist_idx >= 0 else 0)
                                else:
                                    _new = _n2

                    else:
                        # All other tabs: plain album-group jump within the item list
                        _new = _alb_down(_items, _cur) if _dir > 0 else _alb_up(_items, _cur)

                    self.left_idx = clamp(_new, 0, max(0, len(_items) - 1))
                    self._need_redraw = True
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

            if ch == ord("x") and self._queue_context():
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
                if not self._queue_context():
                    marked_albums = self._marked_albums_from_left()
                    marked_artists = self._marked_artists_from_left()
                    marked_playlists = self._marked_playlists_from_left()
                    marked_albums, marked_artists, marked_playlists, _cancelled = \
                        self._resolve_batch_conflict(marked_albums, marked_artists, marked_playlists)
                    if _cancelled:
                        continue
                    if marked_albums:
                        self._download_marked_albums_async(marked_albums)
                        continue
                    if marked_artists:
                        self._download_marked_artists_async(marked_artists)
                        continue
                    if marked_playlists:
                        all_tracks: List[Track] = []
                        for pl in marked_playlists:
                            all_tracks.extend(self.playlists.get(pl, []))
                        self.start_download_tracks(all_tracks)
                        continue
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

            # In the cover tab with the inline lyrics panel (no queue overlay, no
            # full-screen overlay), intercept navigation keys to scroll the panel.
            if (self.tab == TAB_COVER and self._cover_lyrics
                    and not self.queue_overlay):
                _lmax = self._cover_lyrics_max_scroll
                if ch in (curses.KEY_DOWN, ord("j"), 14):
                    self.lyrics_scroll = min(self.lyrics_scroll + 1, _lmax)
                    continue
                if ch in (curses.KEY_UP, ord("k"), 16):
                    self.lyrics_scroll = max(0, self.lyrics_scroll - 1)
                    continue
                if ch == curses.KEY_PPAGE:
                    self.lyrics_scroll = max(0, self.lyrics_scroll - self._page_step())
                    continue
                if ch == curses.KEY_NPAGE:
                    self.lyrics_scroll = min(self.lyrics_scroll + self._page_step(), _lmax)
                    continue
                if ch in (curses.KEY_HOME, ord("g")):
                    self.lyrics_scroll = 0
                    continue
                if ch in (curses.KEY_END, ord("G")):
                    self.lyrics_scroll = _lmax
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
                if self.tab == TAB_LIKED:
                    it_liked = self._selected_left_item()
                    if isinstance(it_liked, Artist):
                        self.open_artist_by_id(it_liked.id, it_liked.name)
                        continue
                    if isinstance(it_liked, Album):
                        self.open_album_from_album_obj(it_liked)
                        continue
                    if isinstance(it_liked, str):
                        self.playlists_open_by_name(it_liked)
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
            self._save_liked()
        except Exception:
            pass
        try:
            save_playlists(self.playlists, self.playlists_meta)
        except Exception:
            pass
        try:
            _exit_settings: Dict[str, Any] = {
                "volume": self.desired_volume, "mute": self.desired_mute,
                "color_mode": self.color_mode, "queue_overlay": self.queue_overlay,
                "show_toggles": self.show_toggles, "show_numbers": self.show_numbers,
                "show_track_album": self.show_track_album, "show_track_year": self.show_track_year,
                "show_track_duration": self.show_track_duration,
                "quality": QUALITY_ORDER[self.quality_idx],
                "autoplay": self.autoplay, "initial_tab": self.tab,
                "tab_align": self.tab_align,
                "include_singles_and_eps_in_artist_tab": self._show_singles_eps,
            }
            if self.current_track and self.mp.alive():
                _tp, _du, _pa, _vo, _mu = self.mp.snapshot()
                if _tp is not None and _tp > 1.0:
                    _exit_settings["_resume_queue_idx"] = self.queue_play_idx
                    _exit_settings["_resume_position"] = float(_tp)
            self.settings.update(_exit_settings)
            save_settings(self.settings)
        except Exception:
            pass
        self.meta.stop()
        self.mp_poller.stop()
        self.mp.stop()



def parse_args(argv: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"api": DEFAULT_API, "_api_explicit": False}
    i = 1
    while i < len(argv):
        a = argv[i]

        if a in ("--api", "-a") and i + 1 < len(argv):
            out["api"] = argv[i + 1]; out["_api_explicit"] = True; i += 2; continue

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
    locale.setlocale(locale.LC_ALL, "")
    args = parse_args(argv)

    if not args.get("_api_explicit"):
        stored = load_json(SETTINGS_FILE, {})
        if isinstance(stored, dict) and stored.get("api"):
            args["api"] = stored["api"]

    if not os.path.isdir(STATE_DIR):
        print(f"tuifi config directory does not exist and will be created at:\n  {STATE_DIR}")
        try:
            input("Press Return to continue, or Ctrl-C to abort.")
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        mkdirp(STATE_DIR)

    if args.get("verbose"):
        import tuifi_pkg.models as _models_mod
        _models_mod._DEBUG_LOG = os.path.join(STATE_DIR, "debug.log")
        debug_log(f"=== tuifi start {time.strftime('%Y-%m-%d %H:%M:%S')} ===")

    def wrapped(stdscr: "curses._CursesWindow") -> None:
        app = App(stdscr, args.get("api", DEFAULT_API), args)
        if app.tab == TAB_QUEUE:
            app.focus = "queue"
        app.run()

    os.environ.setdefault("ESCDELAY", "25")  # shorten ncurses ESC wait (ms)
    curses.wrapper(wrapped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
