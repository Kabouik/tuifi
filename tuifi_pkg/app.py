"""App class, argument parsing and CLI entry point for tuifi."""

from __future__ import annotations

import base64
import curses
import hashlib
import json
import locale
import os
import unicodedata
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import urllib.parse
import webbrowser
from queue import Queue, Empty
from typing import Any, Dict, List, Optional, Set, Tuple

from tuifi_pkg import (
    APP_NAME, VERSION, DEFAULT_API,
    TAB_SEARCH, TAB_QUEUE, TAB_RECOMMENDED, TAB_MIX, TAB_ARTIST,
    TAB_ALBUM, TAB_LIKED, TAB_PLAYLISTS, TAB_HISTORY, TAB_PLAYBACK,
    TAB_NAMES, TAB_SHORT_NAMES,
    AUTOPLAY_OFF, AUTOPLAY_MIX, AUTOPLAY_RECOMMENDED, AUTOPLAY_NAMES,
    QUALITY_ORDER,
    LIKED_FILTER_NAMES,
)
from tuifi_pkg.models import (
    Track, Album, Artist,
    debug_log, _DEBUG_LOG,
    _resolve_config_dir, _default_downloads_dir,
    STATE_DIR, QUEUE_FILE, LIKED_FILE, PLAYLISTS_FILE, HISTORY_FILE, SETTINGS_FILE, DOWNLOADS_DIR, COVER_CACHE_DIR,
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

# Populated by _probe_sixel_support() in main() before curses starts.
_SIXEL_SUPPORTED: bool = False


def _char_display_width(c: str) -> int:
    """Return the terminal display width of a single character (1 or 2 columns)."""
    eaw = unicodedata.east_asian_width(c)
    return 2 if eaw in ("W", "F") else 1


def _str_display_width(s: str) -> int:
    """Return the total terminal display width of a string."""
    return sum(_char_display_width(c) for c in s)


def _truncate_to_display_width(s: str, width: int) -> str:
    """Truncate string so its display width does not exceed `width` columns."""
    cols = 0
    for i, c in enumerate(s):
        cw = _char_display_width(c)
        if cols + cw > width:
            return s[:i]
        cols += cw
    return s


def print_version(prog: str) -> None:
    print(f"tuifi v{VERSION}")


def _yr_int(t: "Track") -> int:
    """Return track year as sortable int; unknown years sort last (9999)."""
    y = year_norm(t.year)
    return int(y) if y.isdigit() else 9999


def _track_sort_key(t: "Track") -> tuple:
    return (_yr_int(t), t.album.lower(), t.track_no or 9999, t.title.lower())


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

        # autoplay: 0=off 1=mix 2=recommended  (migrate old bool True→recommended)
        raw_ap = self.settings.get("autoplay", AUTOPLAY_OFF)
        if isinstance(raw_ap, str):
            raw_ap = AUTOPLAY_NAMES.index(raw_ap) if raw_ap in AUTOPLAY_NAMES else AUTOPLAY_OFF
        self.autoplay: int = clamp(int(AUTOPLAY_RECOMMENDED if raw_ap is True else raw_ap), 0, 2)
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
        self.tab = clamp(self.tab, TAB_QUEUE, TAB_PLAYBACK)

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

        self._reset_left_cursor()
        self._tab_positions: Dict[int, Tuple[int, int]] = {}
        self._prev_tab: int = self.tab

        self._recommended_tab_has_content: bool = False
        self._mix_tab_has_content: bool = False
        self._artist_tab_has_content: bool = False
        self._album_tab_has_content: bool = False
        self._recommended_pending_ctx: Optional[Track] = None
        self._mix_pending_ctx = None  # Track | Album | Artist
        self._artist_pending_ctx: Optional[Track] = None
        self._album_pending_ctx: Optional[Track] = None
        self.marked_left_idx: set = set(); self.marked_queue_idx: set = set()
        self.priority_queue: List[int] = []
        self._queue_resume_idx: Optional[int] = None  # queue_play_idx to return to after priorities
        self.repeat_mode = 0; self.shuffle_on = False
        self.current_track: Optional[Track] = None; self.last_error: Optional[str] = None
        self._last_played_track: Optional[Track] = None   # survives end-of-track; used for post-end seek
        self._last_played_duration: Optional[float] = None  # last known duration while mpv was alive
        self.toast_msg = ""; self.toast_until = 0.0
        self.show_help = False; self.help_scroll = 0

        self.info_scroll = 0; self.info_loading = False; self.info_follow_selection = True; self._info_refresh_due = 0.0
        self.info_track: Optional[Track] = None; self.info_album: Optional[Album] = None; self.info_artist: Optional[Artist] = None
        self.info_payload: Optional[Dict[str, Any]] = None; self._info_target_id: Optional[int] = None; self._info_target_album_id: Optional[int] = None

        self.lyrics_overlay = False; self.lyrics_scroll = 0; self.lyrics_loading = False
        self.lyrics_lines: List[str] = []; self.lyrics_track_id: Optional[int] = None; self.lyrics_track: Optional["Track"] = None

        # Playback tab state
        self.cover_track: Optional[Track] = None; self.cover_path: Optional[str] = None; self.cover_loading: bool = False
        self._cover_backend_cache: Optional[str] = None   # "ueberzugpp"/"chafa-kitty"/"chafa"/"chafa-symbols"/"none"
        self._cover_render_key: str = ""           # "path:WxH" to detect when re-render needed
        self._cover_render_buf: Optional[bytes] = None    # cached chafa/ANSI output
        self._cover_sixel_visible: bool = False; self._cover_sixel_cols: int = 0; self._cover_sixel_rows: int = 0; self._cover_sixel_x: int = 0
        self._cover_ub_socket: Optional[str] = None; self._cover_ub_pid: Optional[int] = None

        # Side cover preview pane (toggled with C, persists across sessions)
        self._album_cover_pane: bool = bool(self.settings.get("cover_pane", True))
        self._album_cover_item_key: str = ""   # "a:{album_id}" or "t:{track_id}"
        self._album_cover_path: Optional[str] = None
        self._album_cover_loading: bool = False
        self._album_cover_render_buf: Optional[bytes] = None
        self._album_cover_render_key: str = ""
        self._album_cover_visible: bool = False
        self._album_cover_visible_top: int = 0
        self._album_cover_visible_x: int = 0
        self._album_cover_visible_rows: int = 0
        self._album_cover_visible_cols: int = 0
        self._album_cover_rows_offset: int = 0       # rows reserved for minicover above miniqueue (used by mouse handler)

        self._q_overlay_scroll: int = 0
        self._mouse_last_press: tuple = (0.0, -1, -1)  # (time, row, col) for double-click / long-press
        self._mouse_long_press_pending: bool = False
        self._cover_lyrics: bool = True; self._cover_lyrics_max_scroll: int = 10_000
        self._show_singles_eps: bool = bool(self.settings.get("include_singles_and_eps_in_artist_tab", False))
        self._last_artist_fetch_track: Optional["Track"] = None
        self._artist_cache: Dict[int, Tuple[List[Any], List[Any], Tuple[int, str]]] = {}

        self._skip_delta: int = 0; self._skip_at: float = 0.0
        self.filter_q = ""; self.filter_hits: List[int] = []; self.filter_pos = -1  # not persisted
        self._lyrics_filter_q = ""; self._lyrics_filter_hits: List[int] = []; self._lyrics_filter_pos = -1

        self._full_redraw()
        self._queue_redraw_only = False; self._loading = False; self._loading_key = ""; self._liked_refresh_due: float = 0.0

        self._last_mpd_path: Optional[str] = None
        self._play_serial: int = 0          # bumped on every play_track call; stale threads bail
        self._play_lock = threading.Lock()  # serializes mp.start() so only one runs at a time
        self._current_track_serial: int = 0 # serial of the play_track call that set current_track

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
        if not self._autoplay_should_prefetch(): return

        pool = self._autoplay_seed_pool()
        if not pool: return

        seed = random.choice(pool)

        # Don't re-fetch if we already fetched from this seed recently
        with self._autoplay_lock:
            if seed.id == self._autoplay_last_seed_id: return
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

        if self.autoplay == AUTOPLAY_OFF: return

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
        if not tracks: return
        queue_ids = {t.id for t in self.queue_items}
        fresh = [t for t in tracks if t.id not in queue_ids]
        if not fresh: return
        self.queue_items.extend(fresh)
        self._toast_redraw(f"Autoplay +{len(fresh)}")
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
        if not ctx: self.toast("No context track"); return
        self.mix_tracks = []
        self.mix_title = ""
        self.mix_track = ctx.artist
        key = f"mix:{ctx.id}:{time.time()}"
        self._set_loading(key)

        def worker() -> None:
            mix_payload = self._fetch_track_mix_payload_for_track(ctx)
            if self._loading_key != key: return

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
            self._mix_tab_has_content = True
            self.toast(f"Mix: {len(tracks)} tracks")

        self._bg(worker, loading_key=key, on_error="Mix error", record_error=True)

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
            if self._loading_key != key: return
            if mix_id:
                mix_payload = self.client.mix(mix_id)
            elif seed_track:
                mix_payload = self._fetch_track_mix_payload_for_track(seed_track)
            else:
                self.toast("No mix for album")
                return
            if not mix_payload or self._loading_key != key: return
            tracks = self._extract_tracks_from_mix_payload(mix_payload)
            self.mix_tracks = tracks
            self.mix_title = f"{album.artist} — {album.title} (Mix)"
            self._mix_tab_has_content = True
            self.toast(f"Mix: {len(tracks)} tracks")

        self._bg(worker, loading_key=key, on_error="Mix error", record_error=True)

    def fetch_mix_from_artist_async(self, artist: Artist) -> None:
        """Load the Mix tab seeded from an artist."""
        self.mix_tracks = []
        self.mix_title = ""
        self.mix_track = artist.name
        key = f"mix:artist:{artist.id}:{time.time()}"
        self._set_loading(key)

        def worker() -> None:
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
            if not mix_id: self.toast("No mix for artist"); return
            if self._loading_key != key: return
            mix_payload = self.client.mix(mix_id)
            tracks = self._extract_tracks_from_mix_payload(mix_payload)
            if self._loading_key != key: return
            self.mix_tracks = tracks
            self.mix_title = f"{artist.name} (Mix)"
            self._mix_tab_has_content = True
            self.toast(f"Mix: {len(tracks)} tracks")

        self._bg(worker, loading_key=key, on_error="Mix error", record_error=True)

    # ---------------------------------------------------------------------------
    # mpv tick callback
    # ---------------------------------------------------------------------------
    def _on_mpv_tick(self) -> None:
        snap = self.mp.snapshot()
        vo, mu, pa = snap[3], snap[4], snap[2]
        if snap[1] is not None:  # duration
            self._last_played_duration = snap[1]
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
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        curses.mouseinterval(0)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            s = self.settings
            for _p, _k, _d in [
                (1,"color_playing","green"), (2,"color_paused","yellow"), (3,"color_error","red"),
                (4,"color_chrome","black"),  (5,"color_accent","magenta"),(6,"color_accent","magenta"),
                (7,"color_artist","white"),  (8,"color_album","blue"),    (9,"color_duration","black"),
                (10,"color_numbers","black"),(11,"color_title","white"),  (12,"color_year","blue"),
                (13,"color_separator","white"),(14,"color_liked","white"),(15,"color_mark","red"),
            ]:
                curses.init_pair(_p, self._name_to_curses_color(s.get(_k, _d)), -1)
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

    def _full_redraw(self) -> None:
        self._need_redraw = True
        self._redraw_status_only = False

    def _toast_redraw(self, msg: str) -> None:
        self.toast(msg)
        self._full_redraw()

    def _persist_settings(self) -> None:
        self.settings.update({
            "volume": self.desired_volume, "mute": self.desired_mute,
            "color_mode": self.color_mode, "queue_overlay": self.queue_overlay,
            "show_toggles": self.show_toggles, "show_numbers": self.show_numbers,
            "show_track_album": self.show_track_album, "show_track_year": self.show_track_year,
            "show_track_duration": self.show_track_duration,
            "quality": QUALITY_ORDER[self.quality_idx],
            "autoplay": AUTOPLAY_NAMES[self.autoplay], "initial_tab": self.tab,
            "tab_align": self.tab_align,
            "include_singles_and_eps_in_artist_tab": self._show_singles_eps,
            "remember_last_input": bool(self.settings.get("remember_last_input", False)),
            "tsv_max_col_width": int(self.settings.get("tsv_max_col_width", 32) or 32),
            "autoplay_n": self.autoplay_n,
            "history_max": int(self.settings.get("history_max", 0) or 0),
            "auto_resume_playback": bool(self.settings.get("auto_resume_playback", False)),
            "playback_tab_layout": str(self.settings.get("playback_tab_layout", "lyrics")),
            "cover_lyrics_color_pair": int(self.settings.get("cover_lyrics_color_pair", 0) or 0),
            "recommended_tab_no_confirm_refetch": bool(self.settings.get("recommended_tab_no_confirm_refetch", False)),
            "mix_tab_no_confirm_refetch": bool(self.settings.get("mix_tab_no_confirm_refetch", False)),
            "artist_tab_no_confirm_refetch": bool(self.settings.get("artist_tab_no_confirm_refetch", False)),
            "album_tab_no_confirm_refetch": bool(self.settings.get("album_tab_no_confirm_refetch", False)),
        })
        try:
            save_settings(self.settings)
        except Exception:
            pass

    def _reset_left_cursor(self) -> None:
        self.left_idx = 0
        self.left_scroll = 0

    def _draw_hint(self, y: int, x: int, h: int, w: int, text: str) -> None:
        """Draw a single-/multi-line hint in colour 10, clipped to the panel."""
        for i, line in enumerate(text.splitlines()):
            if y + i >= y + h:
                break
            self.stdscr.addstr(y + i, x, line[:max(0, w - 1)].ljust(max(0, w - 1)), self.C(10))

    def _parse_similar_artists_payload(self, payload: Any) -> List[Dict[str, Any]]:
        """Extract list of {id, name} dicts from an artist_similar API payload."""
        sim_items: Any = None
        if isinstance(payload, dict):
            for key in ("artists", "items", "data"):
                if isinstance(payload.get(key), list):
                    sim_items = payload[key]
                    break
        elif isinstance(payload, list):
            sim_items = payload
        result: List[Dict[str, Any]] = []
        if sim_items:
            for a in sim_items:
                if isinstance(a, dict):
                    name = a.get("name") or a.get("artistName") or ""
                    if name:
                        result.append({"id": a.get("id") or 0, "name": name})
        return result

    def _set_loading(self, key: str) -> None:
        self._loading = True
        self._loading_key = key
        self.last_error = None
        self._full_redraw()

    def _clear_loading(self, key: str) -> None:
        if self._loading_key == key:
            self._loading = False
            self._full_redraw()

    def _bg(self, fn, *, loading_key: str = "", on_error: str = "Error",
            record_error: bool = False) -> None:
        """Run fn() in a daemon thread. Manages loading state and error toasts."""
        if loading_key:
            self._set_loading(loading_key)
        def _run():
            try:
                fn()
            except Exception as e:
                debug_log(f"bg error ({loading_key or fn.__name__}): {e}")
                if record_error and (not loading_key or self._loading_key == loading_key):
                    self.last_error = str(e)
                if on_error:
                    self.toast(on_error)
            finally:
                if loading_key:
                    self._clear_loading(loading_key)
                self._need_redraw = True
        threading.Thread(target=_run, daemon=True).start()

    def _with_album_tracks_async(self, album: "Album", on_tracks, init_toast: str = "") -> None:
        """Resolve album id and fetch tracks in background, then call on_tracks(tracks)."""
        if init_toast:
            self.toast(init_toast)
        def worker() -> None:
            aid = self._resolve_album_id_for_album(album)
            if not aid: self.toast("Album id?"); return
            on_tracks(self._fetch_album_tracks_by_album_id(aid))
        self._bg(worker)

    def _bg_download_album(self, album: "Album") -> None:
        self._with_album_tracks_async(album, self.start_download_tracks)

    def _resolve_artist_id_via_track(self, artist: "Artist", aid: int = 0) -> int:
        """Return aid; if 0 and artist has a track_id, try to resolve via track info API."""
        if aid or not artist.track_id:
            return aid
        try:
            info = self.client.info(artist.track_id)
            data = info.get("data") if isinstance(info, dict) else None
            if isinstance(data, dict):
                a = data.get("artist")
                if isinstance(a, dict) and str(a.get("id", "")).isdigit():
                    return int(a["id"])
        except Exception:
            pass
        return 0

    def is_liked(self, tid: int) -> bool:
        return tid in self.liked_ids

    def web_base(self) -> str:
        # TODO: derive this properly. The two approaches tried so far are both
        # unsatisfactory:
        #   1. Strip the "api." subdomain from the API URL — fragile; not all
        #      instances expose a web UI at that domain, and the mapping is not
        #      guaranteed.
        #   2. Hardcode a specific public instance — works for most users today
        #      but is wrong in principle.
        # For now we hardcode monochrome.tf: it is a known public TIDAL web
        # client, and any user who can open tuifi already has a working API
        # instance, so the domain is reachable.
        #
        # u = urllib.parse.urlparse(self.api_base)
        # host = u.netloc
        # scheme = u.scheme or "https"
        # if host.startswith("api."):
        #     host = host[4:]
        # return f"{scheme}://{host}"
        return "https://monochrome.tf"

    def open_url(self, url: str) -> None:
        try:
            if os.path.exists("/data/data/com.termux"):
                subprocess.Popen(["termux-open-url", url])
            else:
                webbrowser.open(url)
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

    def _parse_items_list(self, lst: List[Any]) -> List[Track]:
        """Parse a list of API item dicts into Track objects."""
        tracks = []
        for it in lst:
            if isinstance(it, dict):
                x = it.get("item", it) if isinstance(it.get("item"), dict) else it
                if isinstance(x.get("track"), dict):
                    x = x["track"]
                t = self._parse_track_obj(x)
                if t:
                    tracks.append(t)
        return tracks

    def _extract_tracks_from_search(self, payload: Dict[str, Any]) -> List[Track]:
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return list({t.id: t for t in self._parse_items_list(data["items"])}.values())
            if isinstance(payload.get("items"), list):
                return list({t.id: t for t in self._parse_items_list(payload["items"])}.values())
        return []

    def _looks_like_track_dict(self, d: Dict[str, Any]) -> bool:
        if "id" not in d or not (("title" in d) or ("name" in d)):
            return False
        return any(k in d for k in ("trackNumber", "trackNo", "duration", "durationSeconds",
                                    "trackDuration", "isrc", "artists", "artist"))

    def _scan_for_track_dicts(self, root: Any, out: List[Dict[str, Any]], limit: int = 2500) -> None:
        if len(out) >= limit: return
        if isinstance(root, dict):
            if self._looks_like_track_dict(root):
                out.append(root)
                return
            for v in root.values():
                self._scan_for_track_dicts(v, out, limit)
                if len(out) >= limit: return
        elif isinstance(root, list):
            for v in root:
                self._scan_for_track_dicts(v, out, limit)
                if len(out) >= limit: return

    def _scan_parse_tracks(self, payload: Any, limit: int = 2500) -> List[Track]:
        """Scan payload for track dicts and parse each into a Track object."""
        dicts: List[Dict[str, Any]] = []
        self._scan_for_track_dicts(payload, dicts, limit=limit)
        return [t for d in dicts if (t := self._parse_track_obj(d))]

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

        tracks = self._parse_items_list(candidates) if candidates else []

        if not tracks:
            tracks.extend(self._scan_parse_tracks(payload))

        out = list({t.id: t for t in tracks}.values())
        out.sort(key=lambda t: (t.track_no if t.track_no > 0 else 10_000, t.title.lower()))
        return out


    def _dedupe_tracks(self, tracks: List[Track]) -> List[Track]:
        seen_ids: Set[int] = set()
        seen_ta: Set[Tuple[str, str]] = set()
        out: List[Track] = []
        for t in tracks:
            if t.id in seen_ids: continue
            # Also deduplicate by title+artist to collapse same track released
            # under different IDs in multiple album editions.
            ta = (t.title.strip().lower(), t.artist.strip().lower())
            if ta in seen_ta: continue
            seen_ids.add(t.id)
            seen_ta.add(ta)
            out.append(t)
        return out

    def _dedupe_albums(self, albums: List[Album]) -> List[Album]:
        # Key by (artist, title, year) so that different-ID editions of the
        # same album are collapsed into one entry.
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
        out.sort(key=lambda a: (-(int(a.year) if year_norm(a.year) != "????" else 0), a.title.lower()))
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

    def _build_synthetic_albums(self, tracks: List[Track]) -> List[Album]:
        """Build a deduplicated, sorted list of Album objects inferred from track metadata."""
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
        return self._dedupe_albums(list(best.values()))

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
            tracks.extend(self._scan_parse_tracks(payload))

        tracks = self._dedupe_tracks(tracks)

        tracks.sort(key=_track_sort_key)

        if not albums and tracks:
            albums = self._build_synthetic_albums(tracks)

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

    def fmt_track_status(self, t: Track, width: int) -> str:
        yv = self._track_year(t)
        s = f"{t.artist} - {t.title} • {t.album}" + (f" • {yv}" if yv != "????" else "")
        return s if _str_display_width(s) <= width else (_truncate_to_display_width(s, max(0, width - 1)) + "…")

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
        if self.tab == TAB_RECOMMENDED and getattr(self, "_recommended_pending_ctx", None):
            _rc = self._recommended_pending_ctx
            return ("tracks", [
                ("pending_refetch_hint", f'Press 3 again to refetch recommendations for "{_rc.artist} - {_rc.title}".'),
                ("pending_refetch_hint", ""),
            ] + list(self.recommended_results))
        if self.tab == TAB_MIX and getattr(self, "_mix_pending_ctx", None):
            _mc = self._mix_pending_ctx
            if isinstance(_mc, Track):
                _mix_desc = f'"{_mc.artist} - {_mc.title}"'
            elif isinstance(_mc, Album):
                _mix_desc = f'album "{_mc.artist} - {_mc.title}"'
            else:
                _mix_desc = f'artist "{_mc.name}"'
            return ("tracks", [
                ("pending_refetch_hint", f"Press 4 again to refetch mix for {_mix_desc}."),
                ("pending_refetch_hint", ""),
            ] + list(self.mix_tracks))
        _simple_tabs = {TAB_QUEUE: ("queue_tab", self.queue_items), TAB_SEARCH: ("tracks", self.search_results),
                        TAB_RECOMMENDED: ("tracks", self.recommended_results), TAB_MIX: ("tracks", self.mix_tracks),
                        TAB_HISTORY: ("tracks", self.history_tracks), TAB_PLAYBACK: ("playback_tab", [])}
        if self.tab in _simple_tabs:
            return _simple_tabs[self.tab]
        if self.tab == TAB_ARTIST:
            items: List[Any] = []
            if getattr(self, "_artist_pending_ctx", None):
                _ar = self._artist_pending_ctx
                items.append(("pending_refetch_hint",
                              f'Press 5 again to refetch data for artist [{_ar.artist}]'))
                items.append(("pending_refetch_hint", ""))
            if self.artist_ctx:
                items.append(("artist_header", self.artist_ctx))
            if self.artist_albums:
                _sep_hint = "), incl. singles/EPs (#: toggle)" if self._show_singles_eps else "), excl. singles/EPs (#: toggle)"
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
            if getattr(self, "_album_pending_ctx", None):
                _alp = self._album_pending_ctx
                items.append(("pending_refetch_hint",
                              f'Press 6 again to refetch data for album [{_alp.artist} - {_alp.album}]'))
                items.append(("pending_refetch_hint", ""))
            if self.album_header:
                items.append(("album_title", self.album_header))
            items.extend(self.album_tracks)
            return ("album_mixed", items)
        if self.tab == TAB_LIKED:
            f = self.liked_filter
            _liked_map = {1: ("tracks", self.liked_cache), 2: ("liked_mixed", self.liked_artist_cache),
                          3: ("liked_mixed", self.liked_album_cache), 4: ("liked_mixed", self.liked_playlist_cache)}
            if f in _liked_map:
                return _liked_map[f]
            # f == 0: all categories with section separators
            # order: Playlists, Artists, Albums, Tracks
            items: List[Any] = []
            for _lbl, _cache in [("Playlists", self.liked_playlist_cache),
                                  ("Artists",   self.liked_artist_cache),
                                  ("Albums",    self.liked_album_cache),
                                  ("Tracks",    self.liked_cache)]:
                if _cache:
                    items.append(("sep", _lbl))
                    items.extend(_cache)
            return ("liked_mixed", items)
        if self.tab == TAB_PLAYLISTS:
            if self.playlist_view_name is None:
                return ("playlists", self.playlist_names)
            return ("tracks", self.playlist_view_tracks)
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
        if self.tab == TAB_PLAYBACK:
            return self.current_track
        return self._selected_left_track()

    # ---------------------------------------------------------------------------
    # prompts
    # ---------------------------------------------------------------------------
    def prompt_text(self, title: str, initial: str = "") -> Optional[str]:
        h, w = self.stdscr.getmaxyx()
        box_w = clamp(max(73, len(title) + 8), 34, w - 6)
        box_h = 5
        y0, x0, win = self._popup_win(box_h, box_w)
        win.box()
        label = title[:box_w - 4]
        label_len = len(label) + 1
        s = initial
        cur = len(s)
        undo_stack: list = []
        curses.curs_set(1)
        self.stdscr.nodelay(False)
        inner_w = max(1, box_w - 4 - label_len)
        input_x = 2 + label_len
        hint_text = " ^a/^e: home/end  ^u/^k: clear to left/right  ^w: del word  ^/: undo "
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
                undo_stack.append((s, cur))
                s = s[:cur] + ch + s[cur:]
                cur += 1
            elif ch == 27:
                self.stdscr.nodelay(True)
                while self.stdscr.getch() != -1:
                    pass
                curses.curs_set(0)
                self._full_redraw()
                return None
            elif ch in (10, 13):
                curses.curs_set(0)
                self.stdscr.nodelay(True)
                self._full_redraw()
                return s.strip()
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                if cur > 0:
                    undo_stack.append((s, cur))
                    s = s[:cur - 1] + s[cur:]
                    cur -= 1
            elif ch == curses.KEY_DC:
                if cur < len(s):
                    undo_stack.append((s, cur))
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
                undo_stack.append((s, cur))
                s = s[:cur]
            elif ch == 21:
                undo_stack.append((s, cur))
                s = s[cur:]
                cur = 0
            elif ch == 23:
                i = cur
                while i > 0 and s[i - 1] == " ":
                    i -= 1
                while i > 0 and s[i - 1] != " ":
                    i -= 1
                undo_stack.append((s, cur))
                s = s[:i] + s[cur:]
                cur = i
            elif ch == 31:
                if undo_stack:
                    s, cur = undo_stack.pop()
            elif ch == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, bstate = curses.getmouse()
                except curses.error:
                    continue
                if (not (y0 <= my < y0 + box_h and x0 <= mx < x0 + box_w)
                        and bstate & (curses.BUTTON1_PRESSED | curses.BUTTON3_PRESSED)):
                    curses.curs_set(0)
                    self.stdscr.nodelay(True)
                    self._full_redraw()
                    return None

    def prompt_yes_no(self, title: str) -> bool:
        _, w = self.stdscr.getmaxyx()
        box_w = clamp(max(30, len(title) + 8), 30, w - 6)
        _, _, win = self._popup_win(5, box_w)
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
        idx = self.pick_from_list(title, names, simple=True)
        return names[idx] if idx >= 0 else None

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

            _clutter = os.path.join(os.environ.get("TMPDIR") or tempfile.gettempdir(), APP_NAME, "clutter")
            os.makedirs(_clutter, exist_ok=True)
            mpd_path = os.path.join(_clutter, f"{track_id}-{int(time.time()*1000)}.mpd")
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
        for _prop, _val in [("volume", float(self.desired_volume)),
                            ("mute", bool(self.desired_mute)),
                            ("loop-file", "inf" if self.repeat_mode == 2 else "no")]:
            try:
                self.mp.cmd("set_property", _prop, _val)
            except Exception:
                pass

    def play_track(self, t: Track, resume: bool = False, start_pos: float = 0.0) -> None:
        self._play_serial += 1
        _my_serial = self._play_serial

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
                # Bail if a newer play was requested while we were fetching the URL
                if _my_serial != self._play_serial:
                    return
                is_mpd = url.endswith(".mpd") and os.path.isfile(url)
                mpd_path = url if is_mpd else None
                with self._play_lock:
                    # Bail if superseded while waiting for the lock
                    if _my_serial != self._play_serial:
                        return
                    self.mp.start(url, resume=resume, start_pos=start_pos)
                self._apply_mpv_prefs()
                if is_mpd:
                    time.sleep(0.5)
                    if not self.mp.alive():
                        debug_log(f"play_track: mpv died on DASH for {quality} — trying lower quality")
                        raise RuntimeError("dash playback failed")
                # Bail if superseded after start; the newer thread's mp.start() will
                # have already called stop() on our proc via its own start sequence
                if _my_serial != self._play_serial:
                    return
                # success
                if qi != start_qi:
                    debug_log(f"  Quality fallback: {QUALITY_ORDER[start_qi]} → {quality}")
                    self.toast(f"Quality fallback: {quality}", sec=3.0)
                self._last_mpd_path = mpd_path
                self.current_track = t
                self._current_track_serial = _my_serial
                self._last_played_track = t
                self._last_played_duration = None  # will be updated by _on_mpv_tick
                self.last_error = None
                self._full_redraw()
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
        self._toast_redraw(f"Error: {last_err[:60]}")

    def toggle_pause(self) -> None:
        self.mp.cmd("cycle", "pause")

    def mute_toggle(self) -> None:
        self.mp.cmd("cycle", "mute")

    def volume_add(self, delta: float) -> None:
        self.mp.cmd("add", "volume", float(delta))

    def seek_rel(self, sec: float) -> None:
        if not self.mp.alive() and sec < 0 and self._last_played_track:
            start_pos = max(0.0, (self._last_played_duration or 0.0) + sec)
            t = self._last_played_track
            self._bg(lambda: self.play_track(t, start_pos=start_pos), on_error="")
            return
        self.mp.cmd("seek", float(sec), "relative")

    def play_track_with_resume(self) -> None:
        if not self.current_track and self.queue_items:
            idx = clamp(self.queue_play_idx, 0, len(self.queue_items) - 1)
            t = self.queue_items[idx]
            self.queue_play_idx = idx
            self.toast("Resuming…")
            self._bg(lambda: self.play_track(t, resume=True), on_error="")
            return
        t = self._current_selection_track() if not self.current_track else self.current_track
        if not t: self.toast("No track"); return
        self.toast("Resuming…")
        self._bg(lambda: self.play_track(t, resume=True), on_error="")

    # ---------------------------------------------------------------------------
    # priority queue
    # ---------------------------------------------------------------------------
    def _priority_index_of(self, queue_idx: int) -> int:
        try:
            return self.priority_queue.index(queue_idx) + 1
        except ValueError:
            return 0

    def _swap_queue_items(self, i: int, j: int) -> None:
        """Swap two queue positions, preserving priority and play-idx references."""
        self.queue_items[i], self.queue_items[j] = self.queue_items[j], self.queue_items[i]
        pi, pj = self._priority_index_of(i), self._priority_index_of(j)
        if pi > 0: self.priority_queue[pi - 1] = j
        if pj > 0: self.priority_queue[pj - 1] = i
        if self.queue_play_idx == i: self.queue_play_idx = j
        elif self.queue_play_idx == j: self.queue_play_idx = i

    def toggle_priority(self, queue_idx: int) -> None:
        if queue_idx in self.priority_queue:
            self.priority_queue.remove(queue_idx)
            if not self.priority_queue:
                self._queue_resume_idx = None
            self.toast("Priority removed")
        else:
            if not self.priority_queue:
                self._queue_resume_idx = self.queue_play_idx
            self.priority_queue.append(queue_idx)
            self._toast_redraw(f"Priority {len(self.priority_queue)}")

    def clear_priority_queue(self) -> None:
        n = len(self.priority_queue)
        if not n: self.toast("Priority queue empty"); return
        if self.prompt_yes_no(f"Clear {n} priority track(s)? (y/n)"):
            self.priority_queue.clear()
            self._queue_resume_idx = None
            self._toast_redraw("Priority cleared")

    def _remap_priority_after_delete(self, deleted_indices: List[int]) -> None:
        deleted_set = set(deleted_indices)
        new_pq = []
        for pi in self.priority_queue:
            if pi in deleted_set: continue
            shift = sum(1 for d in deleted_indices if d < pi)
            new_pq.append(pi - shift)
        self.priority_queue = new_pq
        if self._queue_resume_idx is not None:
            shift = sum(1 for d in deleted_indices if d < self._queue_resume_idx)
            self._queue_resume_idx -= shift

    def _remap_priority_after_insert(self, insert_pos: int, count: int) -> None:
        self.priority_queue = [pi + count if pi >= insert_pos else pi for pi in self.priority_queue]
        if self._queue_resume_idx is not None and self._queue_resume_idx >= insert_pos:
            self._queue_resume_idx += count

    # ---------------------------------------------------------------------------
    # queue playback
    # ---------------------------------------------------------------------------
    def play_queue_index(self, idx: int, start_pos: float = 0.0) -> None:
        if not self.queue_items: return
        idx = clamp(idx, 0, len(self.queue_items) - 1)
        prev_play_idx = self.queue_play_idx
        self.queue_play_idx = idx
        self.queue_cursor = idx
        if idx in self.priority_queue:
            self.priority_queue.remove(idx)
        t = self.queue_items[idx]
        def _worker() -> None:
            self.play_track(t, start_pos=start_pos)
            if self.last_error and (self.current_track is None or self.current_track.id != t.id):
                self.queue_play_idx = prev_play_idx
            self._full_redraw()
        self._bg(_worker, on_error="")
        self._full_redraw()

    def next_track(self) -> None:
        if not self.queue_items: return
        if self.priority_queue:
            next_idx = self.priority_queue[0]
            self.play_queue_index(next_idx)
            return
        if self._queue_resume_idx is not None:
            # All priorities exhausted — restore the position we had before they started
            self.queue_play_idx = clamp(self._queue_resume_idx, 0, len(self.queue_items) - 1)
            self._queue_resume_idx = None
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

    def _commit_liked(self) -> None:
        self._save_liked()
        self._schedule_liked_refresh()

    def _get_wch_int(self) -> int:
        """Read one character via get_wch() and return it as int; -1 on timeout/error."""
        try:
            ch = self.stdscr.get_wch()
        except curses.error:
            return -1
        if isinstance(ch, str):
            try:
                return ord(ch)
            except Exception:
                return -1
        return ch

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
        self._commit_liked()

    def toggle_like_album(self, album: Album) -> None:
        if album.id in self.liked_album_ids:
            self.liked_album_ids.discard(album.id)
            self.liked_albums = [d for d in self.liked_albums if d.get("id") != album.id]
            self.toast("Album unliked")
        else:
            self.liked_album_ids.add(album.id)
            self.liked_albums.insert(0, {"id": album.id, "title": album.title, "artist": album.artist, "year": album.year})
            self.toast("Album liked")
        self._commit_liked()

    def toggle_like_artist(self, artist_id: int, name: str) -> None:
        if artist_id in self.liked_artist_ids:
            self.liked_artist_ids.discard(artist_id)
            self.liked_artists = [d for d in self.liked_artists if d.get("id") != artist_id]
            self.toast("Artist unliked")
        else:
            self.liked_artist_ids.add(artist_id)
            self.liked_artists.insert(0, {"id": artist_id, "name": name})
            self.toast("Artist liked")
        self._commit_liked()

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
        self._commit_liked()

    def like_selected(self) -> None:
        if not self._queue_context():
            if self.tab == TAB_PLAYBACK:
                if self.current_track: self.toggle_like(self.current_track)
                return
            marked_albums, marked_artists, marked_playlists, cancelled = self._marked_batch()
            if cancelled: return
            for items, fn, label in [
                (marked_albums,    self.toggle_like_album,                             "albums"),
                (marked_artists,   lambda ar: self.toggle_like_artist(ar.id, ar.name), "artists"),
                (marked_playlists, self.toggle_like_playlist,                          "playlists"),
            ]:
                if items:
                    for item in items: fn(item)
                    self.toast(f"Liked/unliked {len(items)} {label}"); self._full_redraw(); return
            it = self._selected_left_item()
            if isinstance(it, tuple) and it[0] == "artist_header":
                if self.artist_ctx: self.toggle_like_artist(*self.artist_ctx)
                return
            if isinstance(it, tuple) and len(it) == 2 and it[0] == "album_title" and isinstance(it[1], Album):
                self.toggle_like_album(it[1]); return
            if isinstance(it, Album): self.toggle_like_album(it); return
            if isinstance(it, Artist): self.toggle_like_artist(it.id, it.name); return
            if isinstance(it, str) and (
                (self.tab == TAB_PLAYLISTS and self.playlist_view_name is None) or self.tab == TAB_LIKED
            ):
                self.toggle_like_playlist(it); return
        tracks = self._target_tracks()
        if len(tracks) > 1:
            for t in tracks: self.toggle_like(t, silent=True)
            self.toast(f"Liked/unliked {len(tracks)}")
        elif tracks:
            self.toggle_like(tracks[0])

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
            _, items = self._left_items()
            sel = self._selected_left_item()
            _t = type(sel) if isinstance(sel, (Album, Artist, str)) else Track
            self.marked_left_idx = {i for i, it in enumerate(items) if isinstance(it, _t)}
        self._toast_redraw("Marked all")

    def unmark_all_current_view(self) -> None:
        if self._queue_context():
            self.marked_queue_idx.clear()
        else:
            _, items = self._left_items()
            sel = self._selected_left_item()
            _t = type(sel) if isinstance(sel, (Album, Artist, str)) else Track
            self.marked_left_idx = {i for i in self.marked_left_idx
                                    if not (0 <= i < len(items) and isinstance(items[i], _t))}
        self._toast_redraw("Unmarked")

    def toggle_mark_and_advance(self) -> None:
        if self._queue_context():
            if not self.queue_items: return
            i = clamp(self.queue_cursor, 0, len(self.queue_items) - 1)
            if i in self.marked_queue_idx:
                self.marked_queue_idx.remove(i)
            else:
                self.marked_queue_idx.add(i)
            self.queue_cursor = clamp(self.queue_cursor + 1, 0, len(self.queue_items) - 1)
        else:
            typ, items = self._left_items()
            if not items: return
            i = clamp(self.left_idx, 0, len(items) - 1)
            if isinstance(items[i], (Track, Album, Artist, str)):
                if i in self.marked_left_idx:
                    self.marked_left_idx.remove(i)
                else:
                    self.marked_left_idx.add(i)
                self.left_idx = clamp(self.left_idx + 1, 0, len(items) - 1)
        self._full_redraw()

    def _marked_by_type(self, t) -> list:
        _, items = self._left_items()
        return [items[i] for i in sorted(self.marked_left_idx) if 0 <= i < len(items) and isinstance(items[i], t)]

    def _marked_tracks_from_left(self)    -> List[Track]:  return self._marked_by_type(Track)
    def _marked_albums_from_left(self)    -> List[Album]:  return self._marked_by_type(Album)
    def _marked_artists_from_left(self)   -> List[Artist]: return self._marked_by_type(Artist)
    def _marked_playlists_from_left(self) -> List[str]:    return self._marked_by_type(str)

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

    def _marked_batch(self):
        """Return (albums, artists, playlists, cancelled) from marked items, resolving conflicts."""
        a = self._marked_albums_from_left()
        ar = self._marked_artists_from_left()
        pl = self._marked_playlists_from_left()
        return self._resolve_batch_conflict(a, ar, pl)

    def _marked_tracks_from_queue(self) -> List[Track]:
        return [self.queue_items[i] for i in sorted(self.marked_queue_idx) if 0 <= i < len(self.queue_items)]

    def _target_tracks(self) -> List[Track]:
        """Return marked or cursor tracks from the active context (queue or left panel)."""
        if self._queue_context():
            return self._marked_tracks_from_queue() or ([t] if (t := self._queue_selected_track()) else [])
        return self._marked_tracks_from_left() or ([t] if (t := self._selected_left_track()) else [])

    # ---------------------------------------------------------------------------
    # enqueue
    # ---------------------------------------------------------------------------
    def _enqueue_tracks(self, tracks: List[Track], insert_after_playing: bool) -> None:
        if not tracks: return
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
                self._toast_redraw(f"Enqueued+ {len(tracks)}")

    def enqueue_album_async(self, album: Album, insert_after_playing: bool) -> None:
        self._with_album_tracks_async(album, lambda t: self._enqueue_tracks(t, insert_after_playing), "Album…")

    def _process_marked_batch_async(self, items, label: str, fetch_one, on_tracks, dedupe: bool = False) -> None:
        """Fetch tracks for each item in background, then call on_tracks(combined_tracks)."""
        self.toast(f"Fetching {len(items)} {label}…")
        def worker() -> None:
            all_tracks: List[Track] = []
            for item in items:
                all_tracks.extend(fetch_one(item))
            if all_tracks:
                on_tracks(self._dedupe_tracks(all_tracks) if dedupe else all_tracks)
            else:
                self.toast("No tracks")
        self._bg(worker)

    def _process_marked_artists_async(self, artists: List[Artist], on_tracks) -> None:
        def _f(a):
            aid = self._resolve_artist_id_via_track(a, a.id)
            return self._fetch_artist_catalog_by_artist_id(aid)[1] if aid else []
        self._process_marked_batch_async(artists, "artists", _f, on_tracks, dedupe=True)

    def _download_marked_artists_async(self, artists: List[Artist]) -> None:
        self._process_marked_artists_async(artists, self.start_download_tracks)

    def _process_marked_albums_async(self, albums: List[Album], on_tracks) -> None:
        def _f(a):
            aid = self._resolve_album_id_for_album(a)
            return self._fetch_album_tracks_by_album_id(aid) if aid else []
        self._process_marked_batch_async(albums, "albums", _f, on_tracks)

    def _download_marked_albums_async(self, albums: List[Album]) -> None:
        self._process_marked_albums_async(albums, self.start_download_tracks)

    def _enqueue_marked_artists_async(self, artists: List[Artist], insert_after_playing: bool) -> None:
        self.toast(f"Fetching {len(artists)} artists…")
        def worker() -> None:
            all_tracks: List[Track] = []
            for artist in artists:
                aid = self._resolve_artist_id_via_track(artist, artist.id)
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
        self._process_marked_albums_async(albums, lambda t: self._enqueue_tracks(t, insert_after_playing))

    def enqueue_key(self, insert_after_playing: bool) -> None:
        if not self._queue_context():
            marked_albums, marked_artists, marked_playlists, cancelled = self._marked_batch()
            if cancelled: return
            if marked_albums:
                self._enqueue_marked_albums_async(marked_albums, insert_after_playing)
                return
            if marked_artists:
                self._enqueue_marked_artists_async(marked_artists, insert_after_playing)
                return
            if marked_playlists:
                self._enqueue_tracks(self._tracks_from_playlists(marked_playlists), insert_after_playing)
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

        self._enqueue_tracks(self._target_tracks(), insert_after_playing)

    def _fetch_artist_tracks(self, artist: Artist) -> List[Track]:
        """Blocking: fetch and dedupe all tracks for a single artist (catalog then search fallback)."""
        tracks: List[Track] = []
        aid = self._resolve_artist_id_via_track(artist, artist.id)
        if aid:
            _albums, tracks = self._fetch_artist_catalog_by_artist_id(aid)
        if not tracks:
            payload2 = self.client.search_tracks(artist.name, limit=300)
            a0 = artist.name.strip().lower()
            tracks = [t for t in self._extract_tracks_from_search(payload2)
                      if t.artist.strip().lower() == a0]
        return self._dedupe_tracks(tracks)

    def _with_artist_tracks_async(self, artist: Artist, on_tracks, init_toast: str = "", sort: bool = True) -> None:
        """Fetch all artist tracks in background, then call on_tracks(tracks)."""
        if init_toast:
            self.toast(init_toast)
        def worker() -> None:
            tracks = self._fetch_artist_tracks(artist)
            if tracks:
                on_tracks(sorted(tracks, key=_track_sort_key) if sort else tracks)
            else:
                self.toast("No tracks")
        self._bg(worker)

    def _enqueue_artist_async(self, artist: Artist, insert_after_playing: bool) -> None:
        self._with_artist_tracks_async(artist, lambda t: self._enqueue_tracks(t, insert_after_playing), "Artist…")

    def _download_artist_async(self, artist: Artist) -> None:
        self._with_artist_tracks_async(artist, self.start_download_tracks, "Artist DL…", sort=False)

    def save_mix_as_playlist_async(self, name: str, seed: Any) -> None:
        """Create a playlist from a mix seed (Track/Album/Artist), save to tab 8 and liked."""
        now_ms = int(time.time() * 1000)
        if name in self.playlists:
            self.toast("Name exists")
            return
        self.playlists[name] = []
        self.playlists_meta[name] = {"id": str(uuid.uuid4()), "createdAt": now_ms}
        self._save_playlists()
        # Also mark as liked so it appears in Liked → Playlists & mixes
        if name not in self.liked_playlist_ids:
            self.liked_playlist_ids.add(name)
            self.liked_playlists.insert(0, {"name": name, "id": self.playlists_meta[name]["id"]})
            self._save_liked()
        self._toast_redraw("Mix saved (loading tracks…)")

        def worker() -> None:
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
                self._save_playlists()
                self.toast(f"Mix '{name}': {len(tracks)} tracks saved")
            else:
                self.toast(f"Mix '{name}' saved (no tracks found)")

        self._bg(worker, on_error="Mix save error", record_error=True)

    def open_artist_by_id(self, artist_id: int, name: str) -> None:
        ctx = Track(id=0, title="", artist=name, album="", year="????",
                    track_no=0, artist_id=artist_id)
        self.switch_tab(TAB_ARTIST, refresh=False)
        self.fetch_artist_async(ctx)

    def playlists_open_by_name(self, name: str) -> None:
        self.switch_tab(TAB_PLAYLISTS, refresh=False)
        self.playlist_names = sorted(self.playlists.keys())
        self._reset_left_cursor()
        if name in self.playlists:
            self.playlist_view_name = name
            self.playlist_view_tracks = []
            self.fetch_playlist_tracks_async(name)
        else:
            self.playlist_view_name = None
            self.playlist_view_tracks = []
            self._full_redraw()

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
        y0, x0, win = self._popup_win(box_h, box_w)
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
                    win.addstr(box_h - 1, 2, " j/k ^n/^p: navigate   Esc/q: close "[:box_w - 4], self.C(10))
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

                if ch == curses.KEY_MOUSE:
                    try:
                        _, mx, my, _, bstate = curses.getmouse()
                    except curses.error:
                        continue
                    if not (y0 <= my < y0 + box_h and x0 <= mx < x0 + box_w):
                        if bstate & (curses.BUTTON1_PRESSED | curses.BUTTON3_PRESSED):
                            return -1                                          # press outside → close
                        continue                                               # release outside → ignore
                    if bstate & curses.BUTTON1_PRESSED:
                        row_in_box = my - y0 - item_row
                        if 0 <= row_in_box < inner_h:
                            fi = scroll + row_in_box
                            if 0 <= fi < len(visible):
                                if fi == idx:
                                    return visible[idx][0]                    # re-click selected → confirm
                                idx = fi
                    continue

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
                    elif ch in (curses.KEY_HOME, "g"): idx = 0
                    elif ch in (curses.KEY_END, "G"): idx = max(0, len(visible) - 1)
                    elif ch == curses.KEY_PPAGE: idx = max(0, idx - max(1, inner_h - 1))
                    elif ch == curses.KEY_NPAGE: idx = min(max(0, len(visible) - 1), idx + max(1, inner_h - 1))
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
            self._full_redraw()

    def like_popup_from_playing(self) -> None:
        t = self._current_selection_track()
        if not t: self.toast("No track selected"); return
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

    def _run_actions(self, title: str, actions) -> None:
        choice = self.pick_from_list(title, [a[0] for a in actions])
        if choice >= 0:
            actions[choice][1]()

    def context_actions_popup(self) -> None:
        """Show a context-sensitive actions popup for the current selection."""
        if self._queue_context():
            it = self._queue_selected_track()
        elif self.tab == TAB_PLAYBACK:
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

        # Build (label, action) list based on selection type
        if isinstance(it, Track):
            artist_id = it.artist_id or self.meta.artist_id.get(it.id, 0) or 0
            album_id = it.album_id or self.meta.album_id.get(it.id, 0) or 0
            album_obj = Album(id=album_id, title=it.album, artist=it.artist, year=it.year,
                              track_id=it.id if not album_id else None)
            artist_obj = Artist(id=artist_id, name=it.artist,
                                track_id=it.id if not artist_id else None)
            liked_t  = self.is_liked(it.id)
            liked_ar = artist_id in self.liked_artist_ids
            liked_al = album_obj.id in self.liked_album_ids
            actions = [
                ("Go to Recommended tab [3]", lambda: (self.switch_tab(TAB_RECOMMENDED, refresh=False), self.fetch_recommended_async(it))),
                ("Go to Mix tab [4]",         lambda: (self.switch_tab(TAB_MIX, refresh=False), self.fetch_mix_async(it))),
                ("Go to Artist tab [5]",      lambda: (self.switch_tab(TAB_ARTIST, refresh=False), self.fetch_artist_async(it))),
                ("Go to Album tab [6]",       lambda: self.open_album_from_track(it)),
                ("Enqueue [e]",               lambda: self._enqueue_tracks([it], insert_after_playing=False)),
                ("Enqueue next [E]",          lambda: self._enqueue_tracks([it], insert_after_playing=True)),
                ("Enqueue album [a]",         lambda: self.enqueue_album_async(album_obj, insert_after_playing=False)),
                ("Enqueue album next [A]",    lambda: self.enqueue_album_async(album_obj, insert_after_playing=True)),
                ("Enqueue all artist' tracks",      lambda: self._enqueue_artist_async(artist_obj, insert_after_playing=False)),
                ("Enqueue all artist' tracks next", lambda: self._enqueue_artist_async(artist_obj, insert_after_playing=True)),
                (f"{'Unlike' if liked_t  else 'Like'} track [l]",  lambda: self.toggle_like(it)),
                (f"{'Unlike' if liked_ar else 'Like'} artist",     lambda: self.toggle_like_artist(artist_id, it.artist)),
                (f"{'Unlike' if liked_al else 'Like'} album",      lambda: self.toggle_like_album(album_obj)),
                ("Add to playlist [p]",       lambda: self.playlists_add_tracks([it])),
                ("Download [d]",              lambda: self.start_download_tracks([it])),
                ("Download album [D]",        lambda: self._bg_download_album(album_obj)),
                ("Download artist",           lambda: self._download_artist_async(artist_obj)),
                ("Similar artists [s]",       lambda: self.show_similar_artists_dialog(artist_obj)),
                ("Like & save corresponding mix…", lambda: (
                    self.save_mix_as_playlist_async(n, it)
                    if (n := self.prompt_text("Mix name:", f"(Mix) {it.artist} - {it.title}")) else None)),
                ("Show lyrics [v]",           lambda: curses.ungetch(ord("v"))),
                ("Show info [i]",             lambda: curses.ungetch(ord("i"))),
            ]
            self._run_actions(f"{it.artist} — {it.title}", actions)

        elif isinstance(it, Album):
            liked_al = it.id in self.liked_album_ids
            ar_obj = Artist(id=0, name=it.artist, track_id=it.track_id)
            actions = [
                ("Go to Mix tab [4]",    lambda: (self.switch_tab(TAB_MIX, refresh=False), self.fetch_mix_from_album_async(it))),
                ("Go to Artist tab [5]", lambda: (self.switch_tab(TAB_ARTIST, refresh=False),
                                                  self.fetch_artist_async(Track(id=0, title="", artist=it.artist, album="", year="????", track_no=0)))),
                ("Go to Album tab [6]",  lambda: self.open_album_from_album_obj(it)),
                ("Enqueue album [e]",    lambda: self.enqueue_album_async(it, insert_after_playing=False)),
                ("Enqueue album next [E]", lambda: self.enqueue_album_async(it, insert_after_playing=True)),
                (f"{'Unlike' if liked_al else 'Like'} album [l]", lambda: self.toggle_like_album(it)),
                ("Add to playlist [a]",  lambda: self._add_album_to_playlist_async(it)),
                ("Download album [d]",   lambda: self._bg_download_album(it)),
                ("Similar artists [s]",  lambda: self.show_similar_artists_dialog(ar_obj, album_id=it.id)),
                ("Like & save corresponding mix…", lambda: (
                    self.save_mix_as_playlist_async(n, it)
                    if (n := self.prompt_text("Mix name:", f"(Mix) {it.artist} - {it.title}")) else None)),
                ("Show lyrics [v]",      lambda: curses.ungetch(ord("v"))),
                ("Show info [i]",        lambda: curses.ungetch(ord("i"))),
            ]
            self._run_actions(f"{it.artist} — {it.title}", actions)

        elif isinstance(it, Artist):
            liked_ar = it.id in self.liked_artist_ids
            actions = [
                ("Go to Mix tab [4]",                lambda: (self.switch_tab(TAB_MIX, refresh=False), self.fetch_mix_from_artist_async(it))),
                ("Go to Artist tab [5]",             lambda: self.open_artist_by_id(it.id, it.name)),
                ("Enqueue all artist' tracks [e]",   lambda: self._enqueue_artist_async(it, insert_after_playing=False)),
                ("Enqueue all artist' tracks next [E]", lambda: self._enqueue_artist_async(it, insert_after_playing=True)),
                (f"{'Unlike' if liked_ar else 'Like'} artist [l]", lambda: self.toggle_like_artist(it.id, it.name)),
                ("Add to playlist [a]",              lambda: self._add_artist_to_playlist_async(it)),
                ("Download artist [d]",              lambda: self._download_artist_async(it)),
                ("Similar artists [s]",              lambda: self.show_similar_artists_dialog(it)),
                ("Show lyrics [v]",                  lambda: curses.ungetch(ord("v"))),
            ] + ([] if self.tab == TAB_ARTIST else [("Show info [i]", lambda: curses.ungetch(ord("i")))])
            self._run_actions(it.name, actions)

        elif isinstance(it, str):
            liked_pl = it in self.liked_playlist_ids
            actions = [
                ("Open",                                            lambda: self.playlists_open_by_name(it)),
                ("Enqueue [e]",                                     lambda: self._enqueue_playlist_async(it, insert_after_playing=False)),
                (f"{'Unlike' if liked_pl else 'Like'} playlist [l]", lambda: self.toggle_like_playlist(it)),
                ("Add to playlist [a]",                            lambda: self._add_playlist_to_playlist_async(it)),
                ("Download with subfolders [d]",                   lambda: self._download_playlist_async(it, flat=False)),
                ("Download flat [D]",                              lambda: self._download_playlist_async(it, flat=True)),
                ("Show lyrics [v]",                                lambda: curses.ungetch(ord("v"))),
                ("Show info [i]",                                  lambda: curses.ungetch(ord("i"))),
            ]
            self._run_actions(it, actions)

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
        if not tracks: self.toast("Nothing to download"); return
        self.toast(f"DL playlist {'flat' if flat else 'structured'}…")
        worker = self._make_playlist_download_worker(name, flat)
        self.dl.progress_line = f"DL queued {len(tracks)}"
        self._need_redraw = True
        self._redraw_status_only = True
        self.dl.enqueue(tracks, worker)

    def playlists_download_prompt(self) -> None:
        if self.tab != TAB_PLAYLISTS or self.playlist_view_name is not None: return
        if not self.playlist_names: self.toast("No playlists"); return
        name = self.playlist_names[clamp(self.left_idx, 0, len(self.playlist_names) - 1)]
        items = [
            ("Download with subfolders [d]", False),
            ("Download flat [D]",            True),
        ]
        hint = " j/k: navigate   Enter: confirm   Esc/q: cancel "
        _, w = self.stdscr.getmaxyx()
        box_w = clamp(max(len(name), max(len(s) for s, _ in items), len(hint)) + 6, 40, w - 6)
        box_h = len(items) + 3   # title row + items + hint row
        _, _, win = self._popup_win(box_h, box_w)
        idx = 0
        self.stdscr.nodelay(False)
        while True:
            win.erase(); win.box()
            win.addstr(0, 2, f" {name} "[:box_w - 2], self.C(4))
            for i, (label, _) in enumerate(items):
                attr = curses.A_REVERSE if i == idx else 0
                win.addstr(1 + i, 2, label[:box_w - 4].ljust(box_w - 4), attr)
            win.addstr(box_h - 1, 2, hint[:box_w - 4], self.C(10))
            win.touchwin(); win.refresh()
            ch = self.stdscr.getch()
            if ch in (curses.KEY_DOWN, ord("j"), 14): idx = (idx + 1) % len(items)
            elif ch in (curses.KEY_UP, ord("k"), 16): idx = (idx - 1) % len(items)
            elif ch in (10, 13): self._download_playlist_async(name, flat=items[idx][1]); return
            elif ch == ord("d"): self._download_playlist_async(name, flat=False); return
            elif ch == ord("D"): self._download_playlist_async(name, flat=True); return
            elif ch in (27, ord("q")): return

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
            mb = done / (1024 * 1024)
            pct = f" {int(done*100/tot)}%" if tot and tot > 0 else ""
            sp(f"DL {count_s}{pct} {mb:.1f}MB {label}")

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
                lyr_lines = self._fetch_lyrics_lines(t.id, strip_lrc=False)
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
    # Playback tab
    # ---------------------------------------------------------------------------

    def _supports_kitty_protocol(self) -> bool:
        """Return True if the terminal claims to support the Kitty graphics protocol."""
        term      = os.environ.get("TERM", "")
        term_prog = os.environ.get("TERM_PROGRAM", "")
        return (
            term == "xterm-kitty"
            or "KITTY_WINDOW_ID" in os.environ
            or term_prog in ("WezTerm", "ghostty", "iTerm.app")
        )

    def _cover_backend(self) -> str:
        """Detect the best available image rendering backend (cached).

        Priority: ueberzugpp > chafa-kitty > chafa (sixel) > chafa-symbols > none.
        chafa-kitty is preferred over sixel because the Kitty protocol renders images
        in the terminal's compositor layer, so ncurses redraws cannot overwrite them.
        chafa-symbols is used when chafa is available but the terminal does not support sixel
        (as determined by _probe_sixel_support() / DA1 query before curses starts).
        """
        if self._cover_backend_cache is None:
            if shutil.which("ueberzugpp"):
                self._cover_backend_cache = "ueberzugpp"
            elif shutil.which("chafa"):
                if self._supports_kitty_protocol():
                    self._cover_backend_cache = "chafa-kitty"
                elif _SIXEL_SUPPORTED:
                    self._cover_backend_cache = "chafa"
                else:
                    self._cover_backend_cache = "chafa-symbols"
            else:
                self._cover_backend_cache = "none"
        return self._cover_backend_cache

    def _cover_cache_path(self, album_id: Optional[int], url: str = "") -> str:
        """Return persistent cache path for a cover image.

        Keyed by album_id when available (allows cache hits before any API
        call).  Falls back to MD5(url) for tracks with no album_id.
        """
        os.makedirs(COVER_CACHE_DIR, exist_ok=True)
        if album_id:
            return os.path.join(COVER_CACHE_DIR, f"a{album_id}.jpg")
        h = hashlib.md5(url.encode()).hexdigest()
        return os.path.join(COVER_CACHE_DIR, f"{h}.jpg")

    def fetch_cover_async(self, t: Optional[Track]) -> None:
        """Download cover art for track t. Called on playback start and when entering Playback tab."""
        if not t: return
        if self.cover_track and self.cover_track.id == t.id and self.cover_path:
            return  # already loaded for this track
        self.cover_track = t
        # Keep existing cover_path/render_buf until new cover is ready so the
        # old artwork remains visible on screen while the new one loads (no blank gap).
        self.cover_loading = True
        if self.tab == TAB_PLAYBACK:
            self._need_redraw = True

        def worker() -> None:
            try:
                dest = self._cover_cache_path(t.album_id)
                if os.path.exists(dest):
                    debug_log(f"fetch_cover_async: cache hit album_id={t.album_id} track={t.id}")
                else:
                    debug_log(f"fetch_cover_async: cache miss album_id={t.album_id} track={t.id} — fetching URL")
                    url = self._fetch_cover_url_for_track(t)
                    if not url:
                        debug_log(f"fetch_cover_async: no cover URL found for track={t.id}")
                        if self.cover_track and self.cover_track.id == t.id:
                            self.cover_path = None
                            self._cover_render_key = ""
                            self._cover_render_buf = None
                        return
                    dest = self._cover_cache_path(t.album_id, url)
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
                if self.tab == TAB_PLAYBACK:
                    self._full_redraw()

        threading.Thread(target=worker, daemon=True).start()

    def _ueberzug_start(self) -> bool:
        """Start the ueberzugpp daemon if not already running. Returns True on success."""
        if self._cover_ub_socket and self._cover_ub_pid:
            # Check daemon still alive
            try:
                os.kill(self._cover_ub_pid, 0)
                return True
            except (OSError, PermissionError):
                self._cover_ub_socket = None
                self._cover_ub_pid = None

        pid_file = os.path.join(STATE_DIR, "ueberzugpp.pid")
        try:
            subprocess.Popen(
                ["ueberzugpp", "-o", "sixel", "layer", "--no-stdin", "--silent",
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
            tmpdir = os.environ.get("TMPDIR") or tempfile.gettempdir()
            socket_path = os.path.join(tmpdir, f"ueberzugpp-{pid}.socket")
            self._cover_ub_pid = pid
            self._cover_ub_socket = socket_path
            return True
        except Exception as e:
            debug_log(f"ueberzugpp start error: {e}")
            return False

    def _ueberzug_show(self, path: str, x: int, y: int, w: int, h: int) -> None:
        if not self._cover_ub_socket: return
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
        if not self._cover_ub_socket: return
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

    def _draw_playback_hint(self, y: int, x: int, h: int, w: int) -> None:
        """Draw status text in the playback tab area (image rendering happens after curses refresh)."""
        backend = self._cover_backend()
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
            return
        # If a cover is already on screen (old or newly loaded), skip all text so
        # ncurses doesn't write to the same row where the sixel starts, which
        # would briefly erase that row and cause a subtle flicker.
        if self.cover_path: return
        if self.cover_loading:
            self.stdscr.addstr(y, x, " Loading cover…"[:max(0, w - 1)], self.C(4))
        else:
            self.stdscr.addstr(y, x, " No cover art available for this track"[:max(0, w - 1)], self.C(10))

    def _cover_portrait(self, h: int, w: int) -> bool:
        """Portrait mode: terminal is square or taller in pixel terms (2:1 cell ratio assumed).
        Triggers as soon as pixel height >= pixel width, i.e. w <= 2*h."""
        if not self._cover_lyrics or (self.queue_overlay and self.tab != TAB_QUEUE):
            return False
        return w <= h * 2

    def _cover_img_rows_portrait(self, h: int, w: int) -> int:
        """Height of the cover image in portrait layout.
        Targets a square render (w//2 rows), capped so at least 3 lyrics content lines
        remain.  Any space below the cover is handed to the lyrics panel."""
        usable = h - 2 - 2 - 1  # tab bar, status bar, bottom gap
        min_lyrics_h = 4         # 1 title bar + 3 content lines
        gap = 1                  # blank row between lyrics panel and status bar
        ideal = w // 2           # square image assuming ~2:1 cell pixel ratio
        return max(4, min(ideal, usable - min_lyrics_h - gap))

    def _cover_img_cols(self, w: int) -> int:
        """Compute cover image display width accounting for side panels."""
        if self.queue_overlay and self.tab != TAB_QUEUE:
            # 1-col gap between main cover and right panel (miniqueue / minicover / both).
            right_w = 45
        elif self._cover_lyrics:
            right_w = self._lyrics_panel_w(w) + 2
        else:
            right_w = 0
        # When the standalone album cover pane is visible (no miniqueue), reserve its
        # width + 1-col gap — consistent with the miniqueue case above.
        if self._album_cover_pane and not (self.queue_overlay and self.tab != TAB_QUEUE):
            right_w = max(right_w, self._album_cover_pane_w(w) + 1)
        return w - right_w

    def _prerender_cover(self, path: str) -> None:
        """Pre-render cover image with chafa in the background download thread.
        Populates _cover_render_buf/_cover_render_key so the main thread can
        write the image instantly without running chafa again."""
        backend = self._cover_backend()
        if backend not in ("chafa", "chafa-kitty", "chafa-symbols"):
            return
        try:
            h, w = self.stdscr.getmaxyx()
        except Exception:
            return
        top_h = 2
        status_h = 2
        if self._cover_portrait(h, w):
            img_rows = self._cover_img_rows_portrait(h, w)
            img_cols = min(w, img_rows * 2)
        else:
            img_rows = h - top_h - status_h - 1  # -1 matches _render_cover_image gap
            img_cols = self._cover_img_cols(w)
        if img_rows <= 0 or img_cols <= 0: return
        fmt = "kitty" if backend == "chafa-kitty" else ("sixel" if backend == "chafa" else "symbols")
        render_key = f"{path}:{img_cols}x{img_rows}:{fmt}"
        try:
            result = subprocess.run(
                ["chafa", f"--format={fmt}", f"--size={img_cols}x{img_rows}", path],
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
        Called only when self.tab == TAB_PLAYBACK and cover_path is set."""
        if not self.cover_path or not os.path.exists(self.cover_path): return
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
        if self._cover_portrait(h, w):
            img_rows = self._cover_img_rows_portrait(h, w)
            img_cols = min(w, img_rows * 2)
            img_x = (w - img_cols) // 2
        else:
            img_rows = h - top_h - status_h - 1
            img_cols = self._cover_img_cols(w)
            img_x = 0
        if img_rows <= 0 or img_cols <= 0: return

        chafa_fmt = "kitty" if backend == "chafa-kitty" else ("sixel" if backend == "chafa" else "symbols")
        render_key = f"{self.cover_path}:{img_cols}x{img_rows}:{chafa_fmt}"

        if backend == "ueberzugpp":
            if not self._ueberzug_start(): return
            # ueberzugpp is idempotent — send on every render
            self._ueberzug_show(self.cover_path, x=img_x, y=top_h,
                                w=img_cols, h=img_rows)
            self._cover_render_key = render_key
            self._cover_sixel_visible = True
            self._cover_sixel_cols = img_cols
            self._cover_sixel_rows = img_rows
            self._cover_sixel_x = img_x
            return

        is_kitty = backend == "chafa-kitty"

        # Kitty: always delete any existing image before rendering.  This covers
        # both size changes (V key, lyrics panel, queue overlay) and re-renders
        # after popups.  The delete is a single short escape and is harmless when
        # no image is present.
        if is_kitty:
            sys.stdout.buffer.write(b"\033_Ga=d,d=A\033\\")
            sys.stdout.buffer.flush()

        # chafa/chafa-kitty path: cache rendered bytes, re-run only on content/size change.
        # Sixel: always re-send because ncurses wipes the area on every refresh.
        is_symbols = backend == "chafa-symbols"
        if render_key == self._cover_render_key and self._cover_render_buf:
            self._write_image_to_terminal(top_h, self._cover_render_buf, img_cols,
                                          img_rows, kitty=is_kitty, x_offset=img_x,
                                          symbols=is_symbols)
            return

        try:
            result = subprocess.run(
                ["chafa", f"--format={chafa_fmt}", f"--size={img_cols}x{img_rows}",
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
                self._write_image_to_terminal(top_h, result.stdout, img_cols,
                                              img_rows, kitty=is_kitty, x_offset=img_x,
                                              symbols=is_symbols)
        except Exception as e:
            debug_log(f"chafa render error: {e}")

    @staticmethod
    def _kitty_set_z_below(data: bytes) -> bytes:
        """Inject z=-1 into the first Kitty APC sequence so the image renders
        below terminal text, letting ncurses popups/panels appear on top of it."""
        idx = data.find(b"\033_G")
        if idx == -1:
            return data
        return data[:idx + 3] + b"z=-1," + data[idx + 3:]

    def _write_image_to_terminal(self, top_row: int, data: bytes, cols: int = 0,
                                  rows: int = 0, kitty: bool = False,
                                  x_offset: int = 0, symbols: bool = False) -> None:
        """Write image data (sixel, kitty, or ANSI symbols) directly to the terminal."""
        # Strip trailing newlines — chafa often appends one, and writing a newline
        # when the cursor is near the bottom of the terminal causes the terminal to
        # scroll, shifting the cover image and corrupting the layout.
        data = data.rstrip(b"\r\n")
        if kitty:
            data = self._kitty_set_z_below(data)
        if symbols:
            # ANSI/symbols output contains raw '\n' line separators.  Curses disables
            # OPOST/ONLCR, so '\n' is a pure linefeed (cursor-down only, no CR): each
            # line would start at the column where the previous one ended.  Absolutely
            # position every line to avoid misalignment, especially at x_offset > 0.
            col = x_offset + 1
            buf = b"\0337"
            for i, line in enumerate(data.split(b"\n")):
                buf += f"\033[{top_row + 1 + i};{col}H".encode() + line
            buf += b"\0338"
        else:
            # Save cursor, move to content area, write image, restore cursor
            buf = (
                b"\0337"                                                   # save cursor (VT100)
                + f"\033[{top_row + 1};{x_offset + 1}H".encode()          # move to row, col
                + data
                + b"\0338"                                                 # restore cursor
            )
        sys.stdout.buffer.write(buf)
        sys.stdout.buffer.flush()
        self._cover_sixel_visible = True
        if cols:
            self._cover_sixel_cols = cols
        if rows:
            self._cover_sixel_rows = rows
        self._cover_sixel_x = x_offset

    def _cover_erase_terminal(self) -> None:
        """Remove the cover image from the terminal before a full curses redraw."""
        if not self._cover_sixel_visible: return
        if self._cover_backend() == "chafa-kitty":
            # Kitty graphics protocol: send delete-all-placements command.
            # This removes all compositor-managed images instantly without
            # needing to overwrite cell regions with spaces.
            sys.stdout.buffer.write(b"\033_Ga=d,d=A\033\\")
            sys.stdout.buffer.flush()
        else:
            # Sixel/symbols: overwrite the image area with spaces because some
            # terminals don't clear sixel pixels when curses paints over them.
            h, w = self.stdscr.getmaxyx()
            top_h = 2
            status_h = 2
            img_rows = self._cover_sixel_rows if self._cover_sixel_rows > 0 else (h - top_h - status_h)
            img_cols = self._cover_sixel_cols if self._cover_sixel_cols > 0 else w
            if img_rows > 0 and img_cols > 0:
                blank_line = b" " * img_cols
                col = self._cover_sixel_x + 1
                buf = b"".join(
                    f"\033[{top_h + 1 + r};{col}H".encode() + blank_line
                    for r in range(img_rows)
                )
                sys.stdout.buffer.write(buf)
                sys.stdout.buffer.flush()
        self._cover_sixel_visible = False

    def _lyrics_panel_w(self, w: int) -> int:
        """Width of the lyrics panel in the playback tab (adapts to terminal width)."""
        # Give lyrics up to half the terminal; cover gets the rest (plus 2-col gap).
        return max(44, min(w // 2, w - 50))

    def _erase_popup_bg(self, y0: int, x0: int, rows: int, cols: int) -> None:
        """Erase a popup area with raw ANSI writes to clear any sixel underneath."""
        if not self._cover_sixel_visible and not self._album_cover_visible: return
        blank = b" " * cols
        buf = b"".join(
            f"\033[{y0 + 1 + r};{x0 + 1}H".encode() + blank
            for r in range(rows)
        )
        sys.stdout.buffer.write(buf)
        sys.stdout.buffer.flush()

    def _popup_win(self, box_h: int, box_w: int):
        """Create a centered, key-enabled popup window; return (y0, x0, win)."""
        h, w = self.stdscr.getmaxyx()
        y0 = (h - box_h) // 2
        x0 = (w - box_w) // 2
        # For kitty backend: delete the compositor image before drawing the popup
        # so it appears on a clean background.  The image is restored on next redraw.
        if self._cover_sixel_visible and self._cover_backend() == "chafa-kitty":
            sys.stdout.buffer.write(b"\033_Ga=d,d=A\033\\")
            sys.stdout.buffer.flush()
            self._cover_sixel_visible = False
        self._popup_clear_bg(y0, x0, box_h, box_w)
        win = self.stdscr.derwin(box_h, box_w, y0, x0)
        win.keypad(True)
        win.bkgd(' ', self.C(0))
        return y0, x0, win

    def _popup_refresh(self, y0: int, x0: int, box_h: int, box_w: int) -> None:
        """Handle _need_redraw inside a popup loop: redraw background if not status-only.

        Intentionally does NOT clear _need_redraw after a full redraw so the
        popup loop immediately redraws its own content in the same iteration
        (avoiding a blank-popup flicker frame between draw() and win.refresh()).
        For status-only redraws _need_redraw is cleared normally since those
        don't erase the screen and the popup is still visible.
        """
        if self._redraw_status_only:
            # Status-only: background not erased, popup still visible — safe to clear.
            self._need_redraw = False
            self._redraw_status_only = False
        else:
            # Full redraw: draw() erases the screen. Leave _need_redraw = True so
            # the popup loop falls through to redraw its own content immediately
            # in the same iteration, before the next _get_wch_int() call.
            self._redraw_status_only = False
            self.draw()
            self._popup_clear_bg(y0, x0, box_h, box_w)
            # Now clear it — popup will redraw content below in the same pass.
            self._need_redraw = False

    def _popup_clear_bg(self, y0: int, x0: int, box_h: int, box_w: int, _erase_bg: bool = True) -> None:
        """Erase popup background (sixel + spaces)."""
        h, w = self.stdscr.getmaxyx()
        pad_y = max(0, y0 - 1); pad_x = max(0, x0 - 2)
        pad_h = min(h - pad_y, box_h + 2); pad_w = min(w - pad_x, box_w + 4)
        self._erase_popup_bg(pad_y, pad_x, pad_h, pad_w)
        if _erase_bg:
            # Use pair 16 (white on black) — the only explicitly non-default-bg pair —
            # so popup background cells are opaque when a kitty z=-1 image is beneath.
            bg_attr = self.C(0)
            for yy in range(pad_y, pad_y + pad_h):
                try:
                    self.stdscr.addstr(yy, pad_x, " " * pad_w, bg_attr)
                except curses.error:
                    pass

    def _draw_playback_lyrics_panel(self, y: int, x: int, h: int, w: int) -> None:
        """Draw lyrics as a right-side panel in the playback tab."""
        if w < 10: return
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
        """Clear the displayed cover image (called when leaving playback tab or before full redraw)."""
        if self._cover_backend_cache == "ueberzugpp":
            self._ueberzug_remove()
        self._cover_erase_terminal()
        self._cover_render_key = ""
        self._cover_render_buf = None
        # Force ncurses to do a full repaint on next refresh so sixel residue is
        # overwritten even in cells with transparent background.
        self.stdscr.clearok(True)

    # ---------------------------------------------------------------------------
    # Artist tab cover preview pane
    # ---------------------------------------------------------------------------

    def _album_cover_pane_w(self, w: int) -> int:
        """Width of the side cover preview pane (matches miniqueue column width)."""
        return min(44, max(20, w - 20))


    def _fetch_cover_url_for_album(self, album: Album) -> Optional[str]:
        """Fetch cover URL for album via client.album()."""
        try:
            payload = self.client.album(album.id)
            if not isinstance(payload, dict):
                return None
            # Payload may be wrapped: {"data": {...}} or the album dict directly.
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            for search in (data, data.get("album") or {}):
                if not isinstance(search, dict):
                    continue
                for k in ("coverUrl", "imageUrl", "squareImageUrl"):
                    v = search.get(k)
                    if isinstance(v, str) and v.startswith("http"):
                        return v
                for k in ("cover", "coverArt", "squareImage", "image"):
                    v = search.get(k)
                    if isinstance(v, str) and v:
                        url = self._tidal_cover_uuid_to_url(v)
                        if url:
                            return url
        except Exception as e:
            debug_log(f"_fetch_cover_url_for_album error: {e}")
        return None

    def _prefetch_album_covers_async(self, albums: List[Album], artist_id: int) -> None:
        """Pre-download covers for all albums in a single background thread.
        Aborts early if the artist changes. Does not update render state."""
        def worker() -> None:
            hits = misses = 0
            for alb in albums:
                if not alb.id or alb.id <= 0:
                    continue
                if not self.artist_ctx or self.artist_ctx[0] != artist_id:
                    return  # artist changed
                try:
                    dest = self._cover_cache_path(alb.id)
                    if os.path.exists(dest):
                        hits += 1
                        continue
                    url = self._fetch_cover_url_for_album(alb)
                    if not url:
                        continue
                    data = http_get_bytes(url, timeout=15.0)
                    if not self.artist_ctx or self.artist_ctx[0] != artist_id:
                        return
                    with open(dest, "wb") as f:
                        f.write(data)
                    misses += 1
                except Exception as e:
                    debug_log(f"_prefetch_album_covers_async error alb={alb.id}: {e}")
            debug_log(f"_prefetch_album_covers_async done: {hits} cache hits, {misses} fetched")
        threading.Thread(target=worker, daemon=True).start()

    def _fetch_album_cover_async(self, album: Album) -> None:
        """Download and pre-render cover for album in a background thread.
        The previous cover stays visible until the new one is ready (no flicker).
        If no cover is found, the pane is cleared after a short delay."""
        if not album.id or album.id <= 0:
            debug_log(f"_fetch_album_cover_async: skipping album with id={album.id!r}")
            return
        item_key = f"a:{album.id}"
        if self._album_cover_loading and self._album_cover_item_key == item_key:
            return
        if self._album_cover_item_key == item_key and self._album_cover_path:
            return  # already loaded
        # Update item key immediately so subsequent draw() calls know what we're loading.
        # Do NOT clear _album_cover_path/_render_buf: keep showing old cover until new is ready.
        self._album_cover_item_key = item_key
        self._album_cover_loading = True

        def worker() -> None:
            try:
                dest = self._cover_cache_path(album.id)
                if os.path.exists(dest):
                    debug_log(f"_fetch_album_cover_async: cache hit album_id={album.id}")
                else:
                    debug_log(f"_fetch_album_cover_async: cache miss album_id={album.id} — fetching URL")
                    url = self._fetch_cover_url_for_album(album)
                    if not url:
                        debug_log(f"_fetch_album_cover_async: no cover URL found for album_id={album.id}")
                        time.sleep(2.0)
                        if self._album_cover_item_key == item_key:
                            self._album_cover_path = None
                            self._album_cover_render_buf = None
                            self._album_cover_render_key = ""
                            self._need_redraw = True
                        return
                    data = http_get_bytes(url, timeout=15.0)
                    with open(dest, "wb") as f:
                        f.write(data)
                if self._album_cover_item_key != item_key:
                    return  # selection changed while downloading
                self._prerender_album_cover(dest)
                self._album_cover_path = dest
                self._need_redraw = True
            except Exception as e:
                debug_log(f"_fetch_album_cover_async error: {e}")
            finally:
                if self._album_cover_item_key == item_key:
                    self._album_cover_loading = False

        threading.Thread(target=worker, daemon=True).start()

    def _fetch_track_cover_async(self, track: Track) -> None:
        """Download and pre-render cover for a track in a background thread.
        Checks the persistent cover cache by album_id first; only makes an
        API call when the file is not already on disk.
        The previous cover stays visible until the new one is ready (no flicker)."""
        if not track.id or track.id <= 0:
            return
        item_key = f"t:{track.id}"
        if self._album_cover_loading and self._album_cover_item_key == item_key:
            return
        if self._album_cover_item_key == item_key and self._album_cover_path:
            return  # already loaded

        # Update item key; keep old path/buf visible until new cover is ready.
        self._album_cover_item_key = item_key
        self._album_cover_loading = True

        # Fast path: file already on disk (keyed by album_id) → skip API call entirely.
        dest = self._cover_cache_path(track.album_id)
        if os.path.exists(dest):
            debug_log(f"_fetch_track_cover_async: cache hit album_id={track.album_id} track={track.id}")
            def fast_worker(_dest=dest) -> None:
                try:
                    if self._album_cover_item_key != item_key:
                        return
                    self._prerender_album_cover(_dest)
                    self._album_cover_path = _dest
                    self._need_redraw = True
                except Exception as e:
                    debug_log(f"_fetch_track_cover_async fast error: {e}")
                finally:
                    if self._album_cover_item_key == item_key:
                        self._album_cover_loading = False
            threading.Thread(target=fast_worker, daemon=True).start()
            return

        def worker() -> None:
            try:
                debug_log(f"_fetch_track_cover_async: cache miss album_id={track.album_id} track={track.id} — fetching URL")
                url = self._fetch_cover_url_for_track(track)
                if not url:
                    debug_log(f"_fetch_track_cover_async: no cover URL found for track={track.id}")
                    time.sleep(2.0)
                    if self._album_cover_item_key == item_key:
                        self._album_cover_path = None
                        self._album_cover_render_buf = None
                        self._album_cover_render_key = ""
                        self._need_redraw = True
                    return
                dest = self._cover_cache_path(track.album_id, url)
                if not os.path.exists(dest):
                    data = http_get_bytes(url, timeout=15.0)
                    with open(dest, "wb") as f:
                        f.write(data)
                if self._album_cover_item_key != item_key:
                    return
                self._prerender_album_cover(dest)
                self._album_cover_path = dest
                self._need_redraw = True
            except Exception as e:
                debug_log(f"_fetch_track_cover_async error: {e}")
            finally:
                if self._album_cover_item_key == item_key:
                    self._album_cover_loading = False

        threading.Thread(target=worker, daemon=True).start()

    def _prerender_album_cover(self, path: str) -> None:
        """Pre-render artist cover with chafa (called from background thread)."""
        backend = self._cover_backend()
        if backend not in ("chafa", "chafa-kitty", "chafa-symbols"):
            return
        try:
            h, w = self.stdscr.getmaxyx()
        except Exception:
            return
        top_h = 2
        status_h = 2
        pane_w = self._album_cover_pane_w(w)
        img_rows = h - top_h - status_h - 1
        img_cols = pane_w
        if img_rows <= 0 or img_cols <= 0:
            return
        fmt = "kitty" if backend == "chafa-kitty" else ("sixel" if backend == "chafa" else "symbols")
        render_key = f"{path}:{img_cols}x{img_rows}:{fmt}"
        try:
            result = subprocess.run(
                ["chafa", f"--format={fmt}", f"--size={img_cols}x{img_rows}", path],
                capture_output=True, timeout=8,
            )
            if result.returncode != 0 or not result.stdout:
                result = subprocess.run(
                    ["chafa", "--format=symbols", f"--size={img_cols}x{img_rows}", path],
                    capture_output=True, timeout=8,
                )
            if result.returncode == 0 and result.stdout:
                self._album_cover_render_buf = result.stdout
                self._album_cover_render_key = render_key
        except Exception as e:
            debug_log(f"_prerender_album_cover error: {e}")

    def _render_album_cover_pane(self, top_h: int, x: int, pane_w: int, pane_h: int) -> None:
        """Write artist cover image to terminal. Called after stdscr.refresh()."""
        if not self._album_cover_path or not os.path.exists(self._album_cover_path):
            return
        backend = self._cover_backend()
        if backend == "none":
            return

        img_rows = pane_h
        img_cols = pane_w

        if backend == "ueberzugpp":
            if not self._ueberzug_start():
                return
            try:
                subprocess.run(
                    ["ueberzugpp", "cmd", "-s", self._cover_ub_socket,
                     "-i", "tuifi_artist", "-a", "add",
                     "-x", str(x), "-y", str(top_h),
                     "--max-width", str(img_cols), "--max-height", str(img_rows),
                     "-f", self._album_cover_path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
                )
            except Exception as e:
                debug_log(f"_render_album_cover_pane ueberzugpp error: {e}")
            self._album_cover_visible = True
            self._album_cover_visible_top = top_h
            self._album_cover_visible_x = x
            self._album_cover_visible_rows = img_rows
            self._album_cover_visible_cols = img_cols
            return

        is_kitty = backend == "chafa-kitty"
        is_symbols = backend == "chafa-symbols"
        fmt = "kitty" if is_kitty else ("sixel" if backend == "chafa" else "symbols")
        render_key = f"{self._album_cover_path}:{img_cols}x{img_rows}:{fmt}"

        if render_key == self._album_cover_render_key and self._album_cover_render_buf:
            buf = self._album_cover_render_buf
        else:
            try:
                result = subprocess.run(
                    ["chafa", f"--format={fmt}", f"--size={img_cols}x{img_rows}",
                     self._album_cover_path],
                    capture_output=True, timeout=8,
                )
                if result.returncode != 0 or not result.stdout:
                    result = subprocess.run(
                        ["chafa", "--format=symbols", f"--size={img_cols}x{img_rows}",
                         self._album_cover_path],
                        capture_output=True, timeout=8,
                    )
                if result.returncode == 0 and result.stdout:
                    self._album_cover_render_buf = result.stdout
                    self._album_cover_render_key = render_key
                    buf = result.stdout
                else:
                    return
            except Exception as e:
                debug_log(f"_render_album_cover_pane chafa error: {e}")
                return

        # Write directly (not via _write_image_to_terminal) to avoid touching
        # _cover_sixel_visible/_cover_sixel_* which belong to the playback tab.
        buf = buf.rstrip(b"\r\n")
        if is_kitty:
            buf = self._kitty_set_z_below(buf)
        if is_symbols:
            col = x + 1
            raw = b"\0337"
            for i, line in enumerate(buf.split(b"\n")):
                raw += f"\033[{top_h + 1 + i};{col}H".encode() + line
            raw += b"\0338"
        else:
            raw = (b"\0337"
                   + f"\033[{top_h + 1};{x + 1}H".encode()
                   + buf
                   + b"\0338")
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()
        self._album_cover_visible = True
        self._album_cover_visible_top = top_h
        self._album_cover_visible_x = x
        self._album_cover_visible_rows = img_rows
        self._album_cover_visible_cols = img_cols

    def _erase_album_cover_terminal(self) -> None:
        """Remove artist cover image from the terminal."""
        if not self._album_cover_visible:
            return
        backend = self._cover_backend()
        if backend == "ueberzugpp":
            if self._cover_ub_socket:
                try:
                    subprocess.run(
                        ["ueberzugpp", "cmd", "-s", self._cover_ub_socket,
                         "-i", "tuifi_artist", "-a", "remove"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
                    )
                except Exception:
                    pass
        elif backend == "chafa-kitty":
            sys.stdout.buffer.write(b"\033_Ga=d,d=A\033\\")
            sys.stdout.buffer.flush()
        else:
            rows = self._album_cover_visible_rows
            cols = self._album_cover_visible_cols
            top = self._album_cover_visible_top
            col = self._album_cover_visible_x + 1
            if rows > 0 and cols > 0:
                blank = b" " * cols
                buf = b"".join(
                    f"\033[{top + 1 + r};{col}H".encode() + blank
                    for r in range(rows)
                )
                sys.stdout.buffer.write(buf)
                sys.stdout.buffer.flush()
        self._album_cover_visible = False

    def _album_cover_clear(self) -> None:
        """Full artist cover cleanup: erase from terminal and reset state."""
        self._erase_album_cover_terminal()
        self._album_cover_item_key = ""
        self._album_cover_path = None
        self._album_cover_loading = False
        self._album_cover_render_buf = None
        self._album_cover_render_key = ""
        self.stdscr.clearok(True)

    def start_download_tracks(self, tracks: List[Track]) -> None:
        if not tracks: self.toast("Nothing to download"); return
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
        self._reset_left_cursor()
        key = f"album-open:{time.time()}"
        self._set_loading(key)

        def worker() -> None:
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
            if self._loading_key != key: return
            self.album_tracks = tracks[:1500]
            for t in self.album_tracks[:40]:
                if (self.show_track_year and year_norm(t.year) == "????") or (self.show_track_duration and not t.duration):
                    self.meta.want(t.id)
            self._album_tab_has_content = True
            self.toast(f"Album {len(self.album_tracks)}")

        self._bg(worker, loading_key=key, on_error="Error", record_error=True)

    def open_album_from_track(self, t: Track) -> None:
        self.open_album_from_album_obj(Album(id=t.album_id or self.meta.album_id.get(t.id, 0),
                                            title=t.album, artist=t.artist, year=t.year))

    # ---------------------------------------------------------------------------
    # tab loaders
    # ---------------------------------------------------------------------------
    def fetch_recommended_async(self, ctx: Optional[Track]) -> None:
        if not ctx: self.toast("No context"); return
        self.recommended_results = []
        key = f"rec:{ctx.id}:{time.time()}"
        self._set_loading(key)

        def worker() -> None:
            tracks = self._fetch_recommended_tracks_for_track(ctx)
            if self._loading_key != key: return
            self.recommended_results = tracks
            self._recommended_tab_has_content = True
            self.toast("Recommended")

        self._bg(worker, loading_key=key, on_error="Error", record_error=True)

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
        self._full_redraw()

    def fetch_artist_async(self, ctx: Optional[Track]) -> None:
        if not ctx: self.toast("No context"); return
        # Early-exit if the same artist is already loaded (by known artist_id)
        if (ctx.artist_id and ctx.artist_id in self._artist_cache and
                self.artist_ctx and self.artist_ctx[0] == ctx.artist_id):
            self._last_artist_fetch_track = ctx
            return
        self._last_artist_fetch_track = ctx
        self.artist_albums, self.artist_tracks = [], []
        self.artist_ctx = None
        if self._album_cover_visible:
            self._erase_album_cover_terminal()
        self._album_cover_item_key = ""
        self._album_cover_path = None
        self._album_cover_render_buf = None
        self._album_cover_render_key = ""
        key = f"artist:{ctx.id}:{time.time()}"
        self._set_loading(key)

        def worker() -> None:
            # Show artist name immediately before any API call so the tab header
            # is visible as soon as fetch_artist_async is called.
            if ctx.artist_id:
                self.artist_ctx = (ctx.artist_id, ctx.artist)
                self._full_redraw()
            aid = (ctx.artist_id or self.meta.artist_id.get(ctx.id) or
                   self._resolve_artist_id_via_track(Artist(id=0, name=ctx.artist, track_id=ctx.id)))

            # Cache hit after resolving aid
            if aid and int(aid) in self._artist_cache:
                cached_albums, cached_tracks, cached_ctx = self._artist_cache[int(aid)]
                self.artist_albums = cached_albums
                self.artist_tracks = cached_tracks
                self.artist_ctx = cached_ctx
                self._loading_key = ""
                self._loading = False
                self._artist_tab_has_content = True
                self._full_redraw()
                return

            albums: List[Album] = []
            raw_tracks: List[Track] = []

            def _commit_tracks() -> None:
                partial = self._dedupe_tracks(raw_tracks)
                partial.sort(key=_track_sort_key)
                self.artist_tracks = partial[:600]
                self._full_redraw()

            if aid:
                payload = self.client.artist(int(aid))
                if self._loading_key != key: return
                albums = self._extract_artist_albums_from_payload(payload)
                albums = self._dedupe_albums(albums)

                # Publish albums immediately so the UI fills in before track fetching starts
                self.artist_ctx = (int(aid), ctx.artist)
                self.artist_albums = albums[:500]
                self._full_redraw()
                # Pre-download all album covers in the background so C toggle is instant.
                self._prefetch_album_covers_async(albums[:500], int(aid))

                # Fetch tracks album by album and update UI after each one
                for alb in albums:
                    if self._loading_key != key: return
                    if alb.id:
                        try:
                            new_tracks = self._fetch_album_tracks_by_album_id(alb.id)
                            raw_tracks.extend(new_tracks)
                            _commit_tracks()
                        except Exception:
                            pass

                # Fallback: scan artist payload for track dicts if album fetches yielded nothing
                if not raw_tracks:
                    raw_tracks.extend(self._scan_parse_tracks(payload))

            if not raw_tracks:
                payload2 = self.client.search_tracks(ctx.artist, limit=300)
                if self._loading_key != key: return
                a0 = ctx.artist.strip().lower()
                raw_tracks = [t for t in self._extract_tracks_from_search(payload2) if t.artist.strip().lower() == a0]

            # Always supplement API albums with those inferred from track
            # metadata — handles artists where the API returns an incomplete
            # album list (e.g. only one entry despite many albums in tracks).
            albums = self._dedupe_albums(albums + self._build_synthetic_albums(raw_tracks))

            # Final commit with fully deduped/sorted results
            albums = self._dedupe_albums(albums)
            self.artist_albums = albums[:500]
            if aid:
                self.artist_ctx = (int(aid), ctx.artist)
            _commit_tracks()
            if aid:
                self._artist_cache[int(aid)] = (self.artist_albums, self.artist_tracks, self.artist_ctx)
            self._artist_tab_has_content = True
            self.toast("Artist")

        self._bg(worker, loading_key=key, on_error="Error", record_error=True)

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
        self._full_redraw()

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

    def _info_payload_fields(self, keys: tuple, fallback_to_root: bool = False) -> List[str]:
        """Extract key/value lines from self.info_payload; returns error line on failure."""
        payload = self.info_payload or {}
        if "error" in payload:
            return [f"Error: {payload.get('error')}"]
        data = payload.get("data") if isinstance(payload, dict) else None
        if fallback_to_root and not isinstance(data, dict):
            data = payload
        if isinstance(data, dict):
            return [f"{k}: {data[k]}" for k in keys if k in data]
        return []

    def _info_lines(self) -> Tuple[str, List[str]]:
        """Build (title, lines) for the info overlay from current info state."""
        if self.info_artist and not self.info_track and not self.info_album:
            ar = self.info_artist
            lines: List[str] = [f"Artist : {ar.name}"] + ([f"ID     : {ar.id}"] if ar.id else []) + [""]
            if self.info_loading:
                lines.append("Loading artist info…")
            else:
                lines.extend(self._info_payload_fields(
                    ("popularity", "numberOfAlbums", "numberOfTracks", "artistTypes", "url"),
                    fallback_to_root=True))
                if (self.info_payload or {}).get("_similar"):
                    lines.append(f"  [s] browse {len(self.info_payload['_similar'])} similar artists")
            return "Artist info", [l for l in lines if l is not None]
        if self.info_album and not self.info_track:
            a = self.info_album
            lines = [f"Album  : {a.title}", f"Artist : {a.artist}",
                     f"Year   : {year_norm(a.year)}"] + ([f"ID     : {a.id}"] if a.id else []) + [""]
            if self.info_loading:
                lines.append("Loading album info…")
            else:
                lines.extend(self._info_payload_fields(
                    ("numberOfTracks", "numberOfVolumes", "releaseDate",
                     "audioQuality", "explicit", "upc", "popularity")))
            return "Album info", [l for l in lines if l is not None]
        t = self.info_track
        if not t:
            return "Info", ["(no selection)"]
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
            lines.extend(self._info_payload_fields(
                ("audioQuality", "explicit", "popularity", "streamReady")))
        return "Track info", lines

    def show_info_dialog(self) -> None:
        """Inner-loop info popup (track/album/artist) — same pattern as show_similar_artists_dialog."""
        self._need_redraw = True
        self.draw()
        h, w = self.stdscr.getmaxyx()
        box_h = min(h - 4, 24)
        box_w = min(w - 8, 82)
        y0, x0, win = self._popup_win(box_h, box_w)
        info_scroll = 0
        self.stdscr.timeout(100)
        try:
            while True:
                if self._need_redraw:
                    self._popup_refresh(y0, x0, box_h, box_w)

                self._do_info_fetch_if_due()

                # Build title and lines from current info state
                title, lines = self._info_lines()

                inner_h = box_h - 2
                max_scroll = max(0, len(lines) - inner_h)
                info_scroll = min(info_scroll, max_scroll)
                start = clamp(info_scroll, 0, max_scroll)

                win.erase()
                win.box()
                win.addstr(0, 2, f" {title} "[:box_w - 2], self.C(4))
                self._render_popup_lines(win, lines, start, inner_h, box_w)
                try:
                    win.addstr(box_h - 1, 2, " j/k: cursor   PgUp/Dn: scroll info   g/G: top/bottom   q/i/ESC: close "[:box_w - 4], self.C(10))
                except curses.error:
                    pass
                win.touchwin()
                win.refresh()

                ch = self._get_wch_int()
                if ch == -1:
                    continue

                if ch == curses.KEY_MOUSE:
                    try:
                        _, mx, my, _, bstate = curses.getmouse()
                    except curses.error:
                        continue
                    if not (y0 <= my < y0 + box_h and x0 <= mx < x0 + box_w):
                        if bstate & (curses.BUTTON1_PRESSED | curses.BUTTON3_PRESSED):
                            break
                        continue
                    _btn5 = getattr(curses, 'BUTTON5_PRESSED', 0x200000)
                    if bstate & curses.BUTTON4_PRESSED:
                        info_scroll = max(0, info_scroll - 3)
                    elif bstate & _btn5:
                        info_scroll = min(max_scroll, info_scroll + 3)
                    continue

                if ch in (27, ord("q"), ord("i"), ord("I")):
                    break
                elif ch in (ord("j"), curses.KEY_DOWN, ord("k"), curses.KEY_UP):
                    _d = 1 if ch in (ord("j"), curses.KEY_DOWN) else -1
                    if self._queue_context():
                        self.queue_cursor = clamp(self.queue_cursor + _d, 0, max(0, len(self.queue_items) - 1))
                    else:
                        _typ, _items = self._left_items()
                        _ni = self.left_idx + _d
                        while 0 <= _ni < len(_items) and isinstance(_items[_ni], tuple) and _items[_ni][0] == "sep":
                            _ni += _d
                        self.left_idx = clamp(_ni, 0, max(0, len(_items) - 1))
                    info_scroll = 0
                    self._update_info_for_selection()
                    self._need_redraw = True
                    self._redraw_status_only = True
                else:
                    _p = self._page_step()
                    _nv = (max(0, info_scroll - _p) if ch == curses.KEY_PPAGE else
                           min(max_scroll, info_scroll + _p) if ch == curses.KEY_NPAGE else
                           0 if ch in (curses.KEY_HOME, ord("g")) else
                           max_scroll if ch in (curses.KEY_END, ord("G")) else None)
                    if _nv is not None: info_scroll = _nv
                if ch == ord("s") and self.info_artist:
                    break  # fall through to show similar artists after dialog
        finally:
            self.stdscr.nodelay(True)
            self._full_redraw()

    def _do_info_fetch_if_due(self) -> None:
        if not self._info_target_id: return
        if time.time() < self._info_refresh_due: return
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

    def _open_info_track(self, t: Track) -> None:
        self.info_scroll = 0
        self._request_info_refresh(t)
        self._need_redraw = True
        self.show_info_dialog()

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
        if t:
            self._open_info_track(t)

    def toggle_info_playing(self) -> None:
        self.info_follow_selection = False
        t = self.current_track
        if t:
            self._open_info_track(t)

    def _start_info_load(self, fetch_fn, _dialog: bool = True) -> None:
        """Common scaffold: reset loading state, fetch in background, optionally show dialog."""
        self.info_payload = None
        self.info_loading = True
        self._info_target_id = None
        self._full_redraw()
        def worker() -> None:
            try:
                self.info_payload = fetch_fn()
            except Exception as e:
                self.info_payload = {"error": str(e)}
            self.info_loading = False
            self._need_redraw = True
        threading.Thread(target=worker, daemon=True).start()
        if _dialog:
            self.show_info_dialog()

    def open_info_album(self, album: Album, _dialog: bool = True) -> None:
        self.info_scroll = 0
        self.info_track = None
        self.info_album = album
        self.info_artist = None
        def _fetch():
            aid = self._resolve_album_id_for_album(album)
            if not aid:
                return {"error": "Album id not found"}
            payload = self.client.album(int(aid))
            return payload if isinstance(payload, dict) else {"raw": payload}
        self._start_info_load(_fetch, _dialog)

    def open_info_artist(self, artist: Artist, _dialog: bool = True) -> None:
        self.info_scroll = 0
        self.info_track = None
        self.info_album = None
        self.info_artist = artist
        def _fetch():
            aid = self._resolve_artist_id_via_track(artist, artist.id)
            if not aid:
                return {"error": "Artist id not found"}
            payload = self.client.artist(int(aid))
            payload = payload if isinstance(payload, dict) else {"raw": payload}
            try:
                similar = self._parse_similar_artists_payload(self.client.artist_similar(int(aid)))
                if similar:
                    payload["_similar"] = similar
            except Exception:
                pass
            return payload
        self._start_info_load(_fetch, _dialog)

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
            # Resolve artist id if needed
            if not aid:
                aid = self._resolve_artist_id_via_track(artist)
            if not aid and album_id:
                try:
                    tracks = self._fetch_album_tracks_by_album_id(album_id)
                    if tracks:
                        t0 = tracks[0]
                        aid = t0.artist_id or self._resolve_artist_id_via_track(
                            Artist(id=0, name=t0.artist, track_id=t0.id))
                except Exception:
                    pass
            if not aid: self.toast("Artist id not found"); return
            try:
                sim_payload = self.client.artist_similar(aid)
            except Exception as e:
                self.toast(f"Error: {e}")
                return
            cached = self._parse_similar_artists_payload(sim_payload)

        if not cached: self.toast("No similar artists found"); return

        artists: List[Artist] = [Artist(id=int(a["id"]) if str(a["id"]).lstrip("-").isdigit() else 0,
                                        name=a["name"]) for a in cached]

        # Redraw underlying screen before drawing dialog on top.
        self._need_redraw = True
        self.draw()

        h, w = self.stdscr.getmaxyx()
        box_w = min(w - 6, max(56, max(len(a.name) for a in artists) + 8))
        box_h = min(h - 6, max(8, len(artists) + 4))
        y0, x0, win = self._popup_win(box_h, box_w)
        idx = 0
        _last_click: tuple = (0.0, -1)
        hint = " j/k ^n/^p: navigate  Enter/5: go to  Esc/q: close "
        hint2 = " a: add to playlist   e/E: enqueue    l: like "
        try:
            while True:
                if self._need_redraw:
                    self._popup_refresh(y0, x0, box_h, box_w)

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
                ch = self._get_wch_int()
                if ch == -1:
                    continue

                if ch == curses.KEY_MOUSE:
                    try:
                        _, mx, my, _, bstate = curses.getmouse()
                    except curses.error:
                        continue
                    if not (y0 <= my < y0 + box_h and x0 <= mx < x0 + box_w):
                        if bstate & (curses.BUTTON1_PRESSED | curses.BUTTON3_PRESSED):
                            break
                        continue
                    if bstate & curses.BUTTON1_PRESSED:
                        row_in_box = my - y0 - 1
                        if 0 <= row_in_box < inner_h and scroll + row_in_box < len(artists):
                            fi = scroll + row_in_box
                            now_t = time.time()
                            is_dbl = now_t - _last_click[0] < 0.35 and _last_click[1] == fi
                            _last_click = (now_t, fi)
                            if is_dbl:
                                ar = artists[fi]
                                self.open_artist_by_id(ar.id, ar.name)
                                break
                            idx = fi
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
            self._full_redraw()

    def _extract_lyrics(self, payload: Any, strip_lrc: bool = True) -> List[str]:
        """Extract lyrics text from an API payload dict.

        strip_lrc=True  → strip LRC timestamps (for overlay display).
        strip_lrc=False → return raw lines including timestamps (for dialog).
        """
        if not isinstance(payload, dict):
            return []

        def _looks_like(s: str) -> bool:
            s = s.strip()
            if not s or len(s) < 30:
                return False
            if "\n" not in s and len(s) < 200:
                return False
            if s.startswith("{") or s.startswith("[{") or s.startswith("http"):
                return False
            if strip_lrc and len(s) > 50 and "/" in s[:20]:
                return False
            return True

        def _find(obj: Any, depth: int = 0) -> str:
            if depth > 10:
                return ""
            if isinstance(obj, str):
                return obj.strip() if _looks_like(obj) else ""
            if isinstance(obj, dict):
                for k in ("subtitles", "lyrics", "lyric"):
                    v = obj.get(k)
                    if isinstance(v, str) and _looks_like(v):
                        return v.strip()
                    if isinstance(v, (dict, list)):
                        r = _find(v, depth + 1)
                        if r:
                            return r
                for k, v in obj.items():
                    if k.lower() in ("lyrics", "lyric", "subtitles", "subtitle", "lyricstext",
                                     "lyricssubtitles", "tracklyrics"):
                        r = _find(v, depth + 1)
                        if r:
                            return r
                for v in obj.values():
                    if isinstance(v, (dict, list)):
                        r = _find(v, depth + 1)
                        if r:
                            return r
            if isinstance(obj, list):
                for item in obj:
                    r = _find(item, depth + 1)
                    if r:
                        return r
            return ""

        text = _find(payload)
        if not text:
            if strip_lrc:
                debug_log(f"_extract_lyrics: no lyrics text found in payload keys={list(payload.keys())[:10]}")
            return []

        debug_log(f"_extract_lyrics: found {len(text)} chars, first 60: {text[:60]!r}")
        lines = text.splitlines()
        if not strip_lrc:
            return lines

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

    def _fetch_lyrics_lines(self, t_id: int, strip_lrc: bool = True) -> List[str]:
        """Fetch lyrics via lyrics API, falling back to info API. Returns empty list on failure."""
        lines: List[str] = []
        try:
            lines = self._extract_lyrics(self.client.lyrics(t_id), strip_lrc)
        except Exception:
            pass
        if not lines:
            try:
                lines = self._extract_lyrics(self.client.info(t_id), strip_lrc)
            except Exception:
                pass
        return lines

    def toggle_lyrics(self, target: Optional["Track"] = None) -> None:
        if self.lyrics_overlay:
            self.lyrics_overlay = False
            self._need_redraw = True
            return
        t = target or self.current_track or self._current_selection_track()
        if not t: self.toast("No track"); return
        self.lyrics_overlay = True
        self.lyrics_scroll = 0
        self.lyrics_track = t
        self._full_redraw()
        if self.lyrics_track_id == t.id and self.lyrics_lines: return
        self.lyrics_track_id = t.id
        self.lyrics_lines = []
        self.lyrics_loading = True
        self._lyrics_filter_q = ""
        self._lyrics_filter_hits = []
        self._lyrics_filter_pos = -1

        def worker() -> None:
            try:
                lines = self._fetch_lyrics_lines(t.id)
                self.lyrics_lines = lines or ["No lyrics available for this track."]
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
                    self.lyrics_lines = self._extract_lyrics(payload, strip_lrc=False)
                except Exception:
                    self.lyrics_lines = []
                self.lyrics_loading = False
                self._full_redraw()

            threading.Thread(target=_worker, daemon=True).start()

        self._hide_cover_for_popup = True
        self._need_redraw = True
        self.draw()
        h, w = self.stdscr.getmaxyx()
        box_h = min(h - 4, 32)
        box_w = min(w - 8, 86)
        y0, x0, win = self._popup_win(box_h, box_w)
        scroll = 0
        self.stdscr.timeout(100)
        try:
            while True:
                if self._need_redraw:
                    self._popup_refresh(y0, x0, box_h, box_w)

                t_ref = track
                title = f" Lyrics – {t_ref.artist} - {t_ref.title} "
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

                ch = self._get_wch_int()
                if ch == -1:
                    continue

                if ch == curses.KEY_MOUSE:
                    try:
                        _, mx, my, _, bstate = curses.getmouse()
                    except curses.error:
                        continue
                    if not (y0 <= my < y0 + box_h and x0 <= mx < x0 + box_w):
                        if bstate & (curses.BUTTON1_PRESSED | curses.BUTTON3_PRESSED):
                            break
                        continue
                    _btn5 = getattr(curses, 'BUTTON5_PRESSED', 0x200000)
                    if bstate & curses.BUTTON4_PRESSED:
                        scroll = max(0, scroll - 3)
                    elif bstate & _btn5:
                        scroll = min(max_scroll, scroll + 3)
                    continue

                if ch in (27, ord("v"), ord("V"), ord("q")):
                    break
                _p = self._page_step()
                _nv = (min(max_scroll, scroll + 1) if ch in (ord("j"), curses.KEY_DOWN, 14) else
                       max(0, scroll - 1) if ch in (ord("k"), curses.KEY_UP, 16) else
                       max(0, scroll - _p) if ch == curses.KEY_PPAGE else
                       min(max_scroll, scroll + _p) if ch == curses.KEY_NPAGE else
                       0 if ch in (curses.KEY_HOME, ord("g")) else
                       max_scroll if ch in (curses.KEY_END, ord("G")) else None)
                if _nv is not None: scroll = _nv
        finally:
            self._hide_cover_for_popup = False
            self.stdscr.nodelay(True)
            self._full_redraw()

    # ---------------------------------------------------------------------------
    # search
    # ---------------------------------------------------------------------------
    def do_search_prompt_anywhere(self) -> None:
        self.playlist_view_name = None
        _remember = self.settings.get("remember_last_input", False)
        q = self.prompt_text("Search:", self.search_q if _remember else "")
        if q is None: return
        self.search_q = q
        self.last_error = None
        try:
            payload = self.client.search_tracks(self.search_q, limit=260)
            results = self._extract_tracks_from_search(payload)
            if results:
                self.search_results = results
                self.switch_tab(TAB_SEARCH, refresh=False)
                self._reset_left_cursor()
                self.toast(f"{len(self.search_results)} results")
            else:
                self.search_results = []
                self.toast("0 results")
        except Exception as e:
            self.last_error = str(e)
            self._toast_redraw("Error")

    # ---------------------------------------------------------------------------
    # filter / find
    # ---------------------------------------------------------------------------
    def _compute_filter_hits(self) -> None:
        q = self.filter_q.strip().lower()
        self.filter_hits = []
        self.filter_pos = -1
        if not q: return
        # When the miniqueue overlay is visible on a non-queue tab, filter the queue.
        if self.queue_overlay and self.tab != TAB_QUEUE:
            items: List[Any] = self.queue_items
        else:
            _typ, items = self._left_items()
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
        if self.tab == TAB_QUEUE or (self.queue_overlay and self.tab != TAB_QUEUE):
            self.queue_cursor = clamp(idx, 0, max(0, len(self.queue_items) - 1))
        else:
            self.left_idx = idx

    def filter_prompt(self) -> None:
        _remember = self.settings.get("remember_last_input", False)
        q = self.prompt_text("Filter:", self.filter_q if _remember else "")
        if q is None: return
        self.filter_q = q
        self._compute_filter_hits()
        if not self.filter_hits:
            self.toast("No match")
            return
        self.filter_pos = 0
        self._set_filter_cursor(self.filter_hits[0])
        self.toast(f"1/{len(self.filter_hits)}")

    def filter_next(self, delta: int) -> None:
        if not self.filter_hits: return
        self.filter_pos = (self.filter_pos + delta) % len(self.filter_hits)
        self._set_filter_cursor(self.filter_hits[self.filter_pos])
        self.toast(f"{self.filter_pos+1}/{len(self.filter_hits)}")

    def _compute_lyrics_filter_hits(self) -> None:
        q = self._lyrics_filter_q.strip().lower()
        self._lyrics_filter_hits = []
        self._lyrics_filter_pos = -1
        if not q: return
        for i, line in enumerate(self.lyrics_lines or []):
            if q in line.lower():
                self._lyrics_filter_hits.append(i)
        if self._lyrics_filter_hits:
            self._lyrics_filter_pos = 0

    def lyrics_filter_prompt(self) -> None:
        _remember = self.settings.get("remember_last_input", False)
        q = self.prompt_text("Lyrics filter:", self._lyrics_filter_q if _remember else "")
        if q is None: return
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
        if not self._lyrics_filter_hits: return
        self._lyrics_filter_pos = (self._lyrics_filter_pos + delta) % len(self._lyrics_filter_hits)
        self.lyrics_scroll = self._lyrics_filter_hits[self._lyrics_filter_pos]
        self._need_redraw = True
        self.toast(f"{self._lyrics_filter_pos+1}/{len(self._lyrics_filter_hits)}")

    # ---------------------------------------------------------------------------
    # playlists
    # ---------------------------------------------------------------------------
    def playlists_create(self) -> None:
        name = self.prompt_text("New playlist name:", "")
        if not name: return
        if name in self.playlists:
            self.toast("Exists")
            return
        now_ms = int(time.time() * 1000)
        self.playlists[name] = []
        self.playlists_meta[name] = {"id": str(uuid.uuid4()), "createdAt": now_ms}
        self._save_playlists()
        self._toast_redraw("Created")

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
                self._save_playlists()
                self.left_idx = clamp(self.left_idx, 0, max(0, len(self.playlist_names) - 1))
                self._toast_redraw(f"Deleted {len(marked)}")
                return
            if not self.playlist_names: return
            name = self.playlist_names[clamp(self.left_idx, 0, len(self.playlist_names)-1)]
        else:
            name = self.playlist_view_name
        if not name: return
        if not self.prompt_yes_no(f"Delete '{name}'? (y/n)"):
            return
        self.playlists.pop(name, None)
        self.playlists_meta.pop(name, None)
        self._save_playlists()
        self.playlist_view_name = None
        self.playlist_view_tracks = []
        self._reset_left_cursor()
        self._toast_redraw("Deleted")

    def playlists_open_selected(self) -> None:
        if not self.playlist_names: return
        name = self.playlist_names[clamp(self.left_idx, 0, len(self.playlist_names)-1)]
        self.playlist_view_name = name
        self.playlist_view_tracks = []
        self._reset_left_cursor()
        self.fetch_playlist_tracks_async(name)

    def fetch_playlist_tracks_async(self, name: str) -> None:
        self.playlist_view_tracks = list(self.playlists.get(name, []))
        self._full_redraw()

    def _enqueue_playlist_async(self, name: str, insert_after_playing: bool) -> None:
        tracks = self.playlists.get(name, [])
        if tracks:
            self._enqueue_tracks(list(tracks), insert_after_playing)
        else:
            self.toast("Empty playlist")

    def _tracks_from_playlists(self, names: List[str]) -> List[Track]:
        return [t for name in names for t in self.playlists.get(name, [])]

    def _save_playlists(self) -> None:
        """Persist playlists to disk and refresh the sorted name list."""
        save_playlists(self.playlists, self.playlists_meta)
        self.playlist_names = sorted(self.playlists.keys())

    def _add_tracks_to_named_playlist(self, tracks: List[Track], name: str) -> None:
        self.playlists.setdefault(name, []).extend(tracks)
        save_playlists(self.playlists, self.playlists_meta)
        self._toast_redraw(f"Added {len(tracks)}")
        if self.tab == TAB_PLAYLISTS and self.playlist_view_name == name:
            self.playlist_view_tracks = list(self.playlists[name])

    def playlists_add_tracks(self, tracks: List[Track]) -> None:
        if not tracks: self.toast("No tracks"); return
        if not (name := self.pick_playlist("Add to playlist")): return
        self._add_tracks_to_named_playlist(tracks, name)

    def _add_album_to_playlist_async(self, album: Album) -> None:
        if not (name := self.pick_playlist("Add to playlist")): return
        self._with_album_tracks_async(album,
            lambda t: self._add_tracks_to_named_playlist(t, name) if t else self.toast("No tracks"),
            "Fetching album…")

    def _add_marked_artists_to_playlist_async(self, artists: List[Artist]) -> None:
        if not (name := self.pick_playlist("Add to playlist")): return
        self._process_marked_artists_async(artists, lambda t: self._add_tracks_to_named_playlist(t, name))

    def _add_marked_albums_to_playlist_async(self, albums: List[Album]) -> None:
        if not (name := self.pick_playlist("Add to playlist")): return
        self._process_marked_albums_async(albums, lambda t: self._add_tracks_to_named_playlist(t, name))

    def _add_artist_to_playlist_async(self, artist: Artist) -> None:
        if not (name := self.pick_playlist("Add to playlist")): return
        self._with_artist_tracks_async(artist, lambda t: self._add_tracks_to_named_playlist(t, name), "Fetching artist…")

    def _add_playlist_to_playlist_async(self, source_name: str) -> None:
        tracks = list(self.playlists.get(source_name, []))
        if not tracks: self.toast("Empty playlist"); return
        if not (name := self.pick_playlist("Add to playlist", exclude=source_name)): return
        self._add_tracks_to_named_playlist(tracks, name)

    def playlists_add_from_context(self) -> None:
        if self._queue_context():
            self.playlists_add_tracks(self._target_tracks())
            return
        marked_albums, marked_artists, marked_playlists, cancelled = self._marked_batch()
        if cancelled: return
        if marked_albums:
            self._add_marked_albums_to_playlist_async(marked_albums)
            return
        if marked_artists:
            self._add_marked_artists_to_playlist_async(marked_artists)
            return
        if marked_playlists:
            name = self.pick_playlist("Add to playlist")
            if name:
                all_tracks = self._tracks_from_playlists(marked_playlists)
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
        self.playlists_add_tracks(self._target_tracks())

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

    def _tab_names_dict(self, w: int) -> dict:
        """Return the appropriate tab-name mapping for the available width (full/short/digit)."""
        order = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)
        if len("  ".join(TAB_NAMES[i] for i in order)) < w:
            return TAB_NAMES
        if len("  ".join(TAB_SHORT_NAMES[i] for i in order)) < w:
            return TAB_SHORT_NAMES
        return {i: TAB_NAMES[i][-1] for i in order}

    def _draw_tabs(self, y: int, x: int, w: int) -> None:
        order = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)
        names = self._tab_names_dict(w)
        s = "  ".join(names[i] for i in order)[:max(0, w - 1)]
        self.stdscr.addstr(y, x, s, self.C(4))
        cur = names.get(self.tab, "")
        idx = s.find(cur)
        if idx >= 0:
            self.stdscr.addstr(y + 1, x + idx, "─" * min(len(cur), max(0, w - idx - 1)), self.C(5) or curses.A_BOLD)

    def _draw_line_no(self, y: int, x: int, idx1: int, width: int) -> int:
        if not self.show_numbers or width < 5:
            return 0
        s = f"{idx1:>4} "
        self.stdscr.addstr(y, x, s[:width], self.C(10))
        return len(s)

    def _draw_segs(self, y: int, x: int, w: int, segs, base_attr: int) -> None:
        """Draw colored text segments left-to-right, padding remainder with spaces."""
        rem = max(0, w - 1)
        cx = x
        for text, pair in segs:
            if rem <= 0 or not text: continue
            text = text[:rem]
            self.stdscr.addstr(y, cx, text, base_attr | (self.C(pair) if self.color_mode else 0))
            cx += len(text)
            rem -= len(text)
        if rem > 0:
            self.stdscr.addstr(y, cx, " " * rem, base_attr)

    def _draw_track_line(self, y: int, x: int, w: int, t: Track, selected: bool,
                         marked: bool, idx1: Optional[int], priority_pos: int = 0,
                         force_no_tsv: bool = False, simple_format: bool = False) -> None:
        base_attr = curses.A_REVERSE if selected else 0
        _c = lambda pair: self.C(pair) if self.color_mode else 0

        offs = self._draw_line_no(y, x, idx1 or 0, w) if idx1 is not None else 0
        x += offs
        w -= offs
        if w <= 0: return

        liked = self.is_liked(t.id)
        use_tsv = self.tab_align and not force_no_tsv and not simple_format

        prio_str = str(priority_pos) if priority_pos > 0 else ""
        n_digits = len(prio_str)
        pref_w = 3 if n_digits <= 2 else (1 + n_digits + 1)
        if w <= pref_w: return
        self.stdscr.addstr(y, x, " " * pref_w, base_attr)
        if marked:
            self.stdscr.addstr(y, x, "+", base_attr | _c(15))
        if priority_pos > 0:
            self.stdscr.addstr(y, x + 1, prio_str[:pref_w - 1], base_attr | _c(5))
        x += pref_w
        w -= pref_w

        heart_w = 2
        if liked:
            self.stdscr.addstr(y, x, ("♥ ")[:max(0, w)], base_attr | _c(14))
        elif use_tsv:
            self.stdscr.addstr(y, x, "  "[:max(0, w)], base_attr)
        if liked or use_tsv:
            x += heart_w
            w -= heart_w
            if w <= 0: return

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
            self._draw_segs(y, x, w, segs, base_attr)
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
            if n_fields == 0: return
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
                self.stdscr.addstr(y, cx, display, base_attr | _c(pair))
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
            self._draw_segs(y, x, w, segs, base_attr)

    def _draw_left(self, y: int, x: int, h: int, w: int) -> None:
        typ, items = self._left_items()
        if h <= 0 or w <= 0: return
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

        # Playback tab: leave content area blank for image; show status/hint text only
        if typ == "playback_tab":
            self._draw_playback_hint(y, x, h, w)
            return

        # Empty-tab hints
        if n == 0:
            _hints = {
                TAB_QUEUE:       " Press e/E on tracks in any tab to enqueue, q to show queue overlay",
                TAB_SEARCH:      " Search on TIDAL with /",
                TAB_RECOMMENDED: " A playing track is required to get recommendations\n\n If Autoplay is set to \"recommended\", the queue will expand with recommended suggestions\n based on last queue items",
                TAB_MIX:         (" No mix tracks loaded — press 4 with a track, album or artist selected"
                                   if self.mix_track else
                                   " Press 4 on a track, album or artist in any tab to load its track mix\n\n If Autoplay is set to \"mix\", the queue will expand with mix suggestions\n based on last queue items"),
                TAB_ARTIST:      " Press 5 on a track or album in any tab to show its artist",
                TAB_ALBUM:       " Press 6 on a track in any tab to show its album",
                TAB_LIKED:       "\n Nothing liked here — press l on items to like them\n Cycle sub-categories with 7/[/] or ^←/^→, or jump directly with Alt+1-5",
                TAB_HISTORY:     " Play tracks to build history",
                TAB_PLAYLISTS:   (" Empty playlist — press a on a track to add it" if self.playlist_view_name is not None else " Press n to create a new playlist"),
            }
            _hint = _hints.get(self.tab)
            if _hint:
                self._draw_hint(y, x, h, w, _hint)
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

            if isinstance(it, tuple) and it[0] == "pending_refetch_hint":
                text = it[1]
                attr = self.C(5) | curses.A_BOLD if text else 0
                try:
                    self.stdscr.addstr(yy, x, text[:max(0, w - 1)].ljust(max(0, w - 1)), attr)
                except curses.error:
                    pass
                continue
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
                    line = f"──── {it[1]}" if self.tab_align else f"── {it[1]}"
                    offs = self._draw_line_no(yy, x, i + 1, w) if self.show_numbers else 0
                    avail = max(0, w - offs - 1)
                    _TOGGLE_SUFFIX = "(#: toggle)"
                    _suf_idx = line.find(_TOGGLE_SUFFIX)
                    if self.tab == TAB_ARTIST and _suf_idx != -1 and _suf_idx < avail:
                        prefix = line[:_suf_idx].ljust(_suf_idx)
                        suffix = line[_suf_idx:avail]
                        self.stdscr.addstr(yy, x + offs, prefix[:avail], self.C(4))
                        self.stdscr.addstr(yy, x + offs + _suf_idx, suffix, self.C(5))
                    else:
                        self.stdscr.addstr(yy, x + offs, line[:avail].ljust(avail), self.C(4))
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

            if typ == "liked_mixed":
                if isinstance(it, tuple) and it[0] == "sep":
                    line = f"── {it[1]} ──"
                    offs = self._draw_line_no(yy, x, i + 1, w) if self.show_numbers else 0
                    self.stdscr.addstr(yy, x + offs, line[:max(0, w - offs - 1)].ljust(max(0, w - offs - 1)), self.C(4))
                    continue
                if isinstance(it, Album):
                    yv = year_norm(it.year)
                    ys = f", {yv}" if (self.show_track_year and yv != "????") else ""
                    marked = (i in self.marked_left_idx)
                    self._draw_liked_row(yy, x, w, i, selected, marked,
                                         f"{it.artist} — {it.title}{ys}", self.C(8))
                    continue
                if isinstance(it, Artist):
                    marked = (i in self.marked_left_idx)
                    self._draw_liked_row(yy, x, w, i, selected, marked, it.name, self.C(7))
                    continue
                if isinstance(it, str):  # playlist name
                    count = len(self.playlists.get(it, []))
                    marked = (i in self.marked_left_idx)
                    content = f"{it} ({count} tracks)" if count else it
                    self._draw_liked_row(yy, x, w, i, selected, marked, content)
                    continue

            if typ in ("artist_mixed", "album_mixed", "liked_mixed") and isinstance(it, Track):
                marked = (i in self.marked_left_idx)
                self._draw_track_line(yy, x, w, it, selected=selected, marked=marked, idx1=i + 1)
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
        if h <= 1 or w <= 0: return
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
        if w <= 0: return
        self.stdscr.addstr(y, x, self._queue_title()[:max(0, w - 1)].ljust(max(0, w - 1)), self.C(4))
        y += 1
        h -= 1
        if not self.queue_items: return

        self.queue_cursor = clamp(self.queue_cursor, 0, len(self.queue_items) - 1)
        if self.queue_cursor < self._q_overlay_scroll:
            self._q_overlay_scroll = self.queue_cursor
        if self.queue_cursor >= self._q_overlay_scroll + h:
            self._q_overlay_scroll = self.queue_cursor - h + 1
        self._q_overlay_scroll = clamp(self._q_overlay_scroll, 0, max(0, len(self.queue_items) - h))
        q_scroll = self._q_overlay_scroll

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
            pfx_sym, pfx_color = ("▶", self.C(1)) if (playing and not pa) else ("⏸", self.C(2)) if (playing and pa) else ("", 0)

            offs = self._draw_line_no(yy, x, i + 1, w) if self.show_numbers else 0
            px = x + offs
            pw = w - offs
            if pw <= 0: continue
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
            if self.repeat_mode:
                parts.append("repeat: " + ("all" if self.repeat_mode == 1 else "one"))
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
            if self._show_singles_eps:
                parts.append("singles/EPs: on")
            # Show buffer size when autoplay is active
            with self._autoplay_lock:
                buf_n = len(self._autoplay_buffer)
                fetching = self._autoplay_prefetch_running
            if self.autoplay != AUTOPLAY_OFF and (buf_n > 0 or fetching):
                parts.append(f"buffer: {'…' if fetching else buf_n}")
            line1 = " ? help  |  " + "   ".join(parts)
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
        col_limit = max(0, w - 1)
        line2 = _truncate_to_display_width(left + song, col_limit).ljust(col_limit)

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
            right = _truncate_to_display_width(right, max(0, w - 2))
            tpos = max(0, col_limit - _str_display_width(right))
            line2 = line2[:tpos] + right + line2[tpos + len(right):]

        try:
            self.stdscr.addstr(y + 1, x, line2, self._status_color_pair(pa, alive))
        except curses.error:
            pass

    def _render_popup_lines(self, win, lines: List[str], start: int, inner_h: int, box_w: int) -> None:
        """Render lines into a popup window, supporting \\x01 highlighted headers."""
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

    def show_help_dialog(self) -> None:
        """Inner-loop help popup - no flicker (same pattern as show_similar_artists_dialog)."""
        lines = [
            "",
            "\x01 TABS",
            " 1 Queue  2 Search  3 Recommended  4 Mix  5 Artist  6 Album  7 Liked  8 Playlists  9 History  0 Playback",
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
            " f         filter term in current view (in playback tab: filter lyrics)",
            " (/)       prev/next filter hit (in playback tab: prev/next lyrics match)",
            " h/?       help (also: right-click on empty area)",
            " Esc       close prompts",
            " Q         quit",
            "",
            "\x01 VIEW",
            " q         mini-queue overlay",
            " C         side cover pane",
            " Tab       move cursor between main view and mini-queue overlay",
            " z         jump to playing track in the mini-queue",
            " ^\u2190/^\u2192/1-9 Navigate main tabs and sub-tabs",
            " 7/[/]     jump to Liked tab then cycle its sub-tabs",
            " Alt+1-5   jump directly to Liked sub-tabs (Allᴹ⁻¹ Tracksᴹ⁻² Artistsᴹ⁻³ Albumsᴹ⁻⁴ Playlistsᴹ⁻⁵)",
            " ;/Bkspc   go back to last tab without refreshing",
            " g/G       go to top/bottom",
            " j/k/\u2193/\u2191   go down/up",
            " ^\u2193/^\u2191     jump to adjacent sub-section (tabs 5 and 7) or to next artist/album in list",
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
            "\x01 SETTINGS (edit settings.jsonc manually)",
            " TIDAL HiFi API instance:",
            f" api: API base URL (current: {self.settings.get('api', '') or 'not set'})",
            " if not set, the first invocation of tuifi with the --api runtime flag will set it",
            "",
            " Autoplay:",
            " autoplay_n:  number of tracks to add per autoplay refill (default: 3)",
            "",
            " History tab:",
            " history_max: max history entries to keep (default: 0 = unlimited)",
            "",
            " Playback:",
            " auto_resume_playback: resume last position on startup (default: true)",
            "",
            " Artist tab:",
            " include_singles_and_eps_in_artist_tab: show singles/EPs (default: false, toggle with #)",
            " cover_pane: show side cover pane on startup (default: true, toggle with C)",
            " playback_tab_layout: default layout when entering tab 0",
            "   values: \"lyrics\" (default), \"miniqueue\", \"miniqueue_cover\"",
            "",
            " Download file structure:",
            " download_dir (Linux default: /tmp/tuifi/)",
            " download_structure (default: {artist}/{artist} - {album} ({year}))",
            " download_filename (default: {track:02d}. {artist} - {title})",
            " Playlists can also be downloaded with a flat structure",
            "",
            " Colors:",
            " color_playing  color_paused  color_error  color_chrome  color_accent",
            " color_artist   color_title   color_album  color_year    color_separator",
            " color_duration color_numbers color_liked  color_mark",
            " values: black red green yellow blue magenta cyan white (or 0-255)",
            "",
            " TSV fields (general or per field overrides):",
            " tsv_max_col_width: default max column width in TSV mode (default 32, 0=unlimited)",
            " tsv_max_artist_width       tsv_max_title_width       tsv_max_album_width",
            " tsv_max_year_width         tsv_max_duration_width",
            "",
            f"\x01 tuifi v{VERSION}",
        ]
        h, w = self.stdscr.getmaxyx()
        box_w = min(w - 4, 109)
        box_h = min(h - 6, 38)
        inner_h = box_h - 2
        max_scroll = max(0, len(lines) - inner_h)
        scroll = clamp(getattr(self, "help_scroll", 0), 0, max_scroll)
        self._hide_cover_for_popup = True
        self._need_redraw = True
        self.draw()
        y0, x0, win = self._popup_win(box_h, box_w)
        self.stdscr.timeout(100)
        try:
            while True:
                if self._need_redraw:
                    self._popup_refresh(y0, x0, box_h, box_w)

                scroll = clamp(scroll, 0, max_scroll)
                win.erase()
                win.box()
                win.addstr(0, 2, " Help "[:box_w - 2], self.C(4))
                self._render_popup_lines(win, lines, scroll, inner_h, box_w)
                try:
                    win.addstr(box_h - 1, 2,
                               " j/k/wheel: scroll   PgUp/PgDn: pages   g/G: top/bottom   h/?/q/Esc: close "[:box_w - 4],
                               self.C(10))
                except curses.error:
                    pass
                win.touchwin()
                win.refresh()

                ch = self._get_wch_int()
                if ch == -1:
                    continue
                if ch == curses.KEY_MOUSE:
                    try:
                        _, mx, my, _, bstate = curses.getmouse()
                    except curses.error:
                        continue
                    _btn5 = getattr(curses, "BUTTON5_PRESSED", 0x200000)
                    if bstate & curses.BUTTON4_PRESSED:
                        scroll = max(0, scroll - 3)
                    elif bstate & _btn5:
                        scroll = min(max_scroll, scroll + 3)
                    elif bstate & (curses.BUTTON1_PRESSED | curses.BUTTON3_PRESSED):
                        if not (y0 <= my < y0 + box_h and x0 <= mx < x0 + box_w):
                            break  # click outside dialog → close
                    continue
                if ch in (27, ord("?"), ord("Q"), ord("q"), ord("h")):
                    break
                _p = self._page_step()
                if ch in (curses.KEY_DOWN, ord("j")):
                    scroll = min(scroll + 1, max_scroll)
                elif ch in (curses.KEY_UP, ord("k")):
                    scroll = max(0, scroll - 1)
                elif ch == curses.KEY_PPAGE:
                    scroll = max(0, scroll - _p)
                elif ch == curses.KEY_NPAGE:
                    scroll = min(scroll + _p, max_scroll)
                elif ch in (curses.KEY_HOME, ord("g")):
                    scroll = 0
                elif ch in (curses.KEY_END, ord("G")):
                    scroll = max_scroll
        finally:
            self._hide_cover_for_popup = False
            self.help_scroll = scroll
            self.stdscr.nodelay(True)
            self._full_redraw()

    def _draw_liked_row(self, yy: int, x: int, w: int, i: int, selected: bool, marked: bool,
                        content: str, color: int = 0) -> None:
        """Draw a liked-item row: line_no + mark prefix + ♥ + content."""
        base_attr = curses.A_REVERSE if selected else 0
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
            self.stdscr.addstr(yy, px, content[:pw].ljust(pw)[:pw], base_attr | color)

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
        if not self._need_redraw: return

        h, w = self.stdscr.getmaxyx()
        top_h = 3 if self.tab == TAB_LIKED else 2
        status_h = 2
        usable_h = h - top_h - status_h
        if self._redraw_status_only:
            self._draw_status(h - status_h, 0, w)
            sys.stdout.buffer.write(b"\033[?2026h")
            sys.stdout.buffer.flush()
            try:
                self.stdscr.noutrefresh()
                curses.doupdate()
            finally:
                sys.stdout.buffer.write(b"\033[?2026l")
                sys.stdout.buffer.flush()
            self._need_redraw = False
            self._redraw_status_only = False
            return

        self._queue_redraw_only = False
        self._redraw_status_only = False
        self._need_redraw = False

        # Throttle: when the sixel cover is already visible and more input is queued
        # (user is still scrolling), skip the erase+sixel cycle entirely.  Curses
        # will only send the changed text cells (lyrics, status), leaving the sixel
        # undisturbed in the terminal.  On the quiet frame (no pending input) the
        # full erase+render runs inside the \033[?2026h sync block so terminals that
        # support DEC 2026 present the transition atomically with no flicker.
        # Scrollable popups (Help, Lyrics) set this flag so both cover images are
        # fully erased for the duration: ncurses scroll corrupts residual sixel rows.
        hide_cover = getattr(self, "_hide_cover_for_popup", False)

        _throttle_cover = False
        if (self.tab == TAB_PLAYBACK and self.cover_path and self._cover_sixel_visible
                and not hide_cover):
            _next = self.stdscr.getch()
            if _next != -1:
                curses.ungetch(_next)
                _throttle_cover = True

        if not _throttle_cover:
            # On the playback tab with a valid cover, keep the existing sixel in place
            # while curses repaints the UI chrome — the new cover is written after
            # stdscr.refresh(), and overlays are drawn on top of it afterwards.
            # On any other tab (or if cover_path is unset), erase any stale sixel first.
            if hide_cover or not (self.tab == TAB_PLAYBACK and self.cover_path):
                self._cover_erase_terminal()
            # Artist cover pane: always erase before full redraw; re-rendered after refresh.
            if self._album_cover_visible:
                self._erase_album_cover_terminal()
            # Full redraw: stdscr.erase()+refresh() will overwrite the sixel area with
            # spaces, so mark it as no longer visible.  This ensures _render_cover_image
            # re-writes it even if the render key is unchanged.
            self._cover_sixel_visible = False
            self.stdscr.erase()

        # When TAB_QUEUE is active the queue fills the full left panel; don't also
        # draw it as a right-side overlay (that would show the queue twice).
        queue_panel = self.queue_overlay and self.tab != TAB_QUEUE
        left_w = w if not queue_panel else max(20, w - 44)

        # Side cover pane (C toggle): works on all tabs.
        # When the miniqueue is also shown, share its right column (cover on top, queue below).
        # Hidden when on Playback tab in lyrics mode (main cover already fills the screen).
        artist_pane_w = 0
        artist_cover_rows = 0   # non-zero only when stacked with miniqueue
        # Minicover is suppressed on Playback tab unless the miniqueue is also open
        # (there's no cursor on the bare Playback tab to drive cover selection).
        _pane_active = self._album_cover_pane and not hide_cover and not (
            self.tab == TAB_PLAYBACK and not queue_panel
        )
        if _pane_active:
            # Determine what's focused and trigger fetch if needed.
            _sel_item_key = ""
            if self._queue_context() and self.queue_items:
                qt = self.queue_items[clamp(self.queue_cursor, 0, len(self.queue_items) - 1)]
                if isinstance(qt, Track):
                    _sel_item_key = f"t:{qt.id}"
                    if _sel_item_key != self._album_cover_item_key:
                        self._fetch_track_cover_async(qt)
            else:
                sel_alb = self._selected_left_album()
                if sel_alb and sel_alb.id:
                    _sel_item_key = f"a:{sel_alb.id}"
                    if _sel_item_key != self._album_cover_item_key:
                        self._fetch_album_cover_async(sel_alb)
                else:
                    sel_trk = self._selected_left_track()
                    if sel_trk:
                        _sel_item_key = f"t:{sel_trk.id}"
                        if _sel_item_key != self._album_cover_item_key:
                            self._fetch_track_cover_async(sel_trk)

            if queue_panel:
                artist_pane_w = w - left_w  # share the queue column
                artist_cover_rows = min(artist_pane_w // 2, max(6, usable_h - 8))
            else:
                artist_pane_w = self._album_cover_pane_w(w)
                left_w = max(10, left_w - artist_pane_w)

        self._draw_tabs(0, 0, w)
        if self.tab == TAB_LIKED:
            self._draw_liked_filter_bar(2, 0, w)
        self._draw_left(top_h, 0, usable_h, left_w)

        if queue_panel:
            self._draw_queue(top_h + artist_cover_rows, left_w,
                             usable_h - artist_cover_rows, w - left_w)

        self._draw_status(h - status_h, 0, w)

        if self.tab == TAB_PLAYBACK and self._cover_lyrics and not queue_panel:
            # Auto-fetch lyrics for the playing track if not already loaded.
            t_cov = self.current_track
            if t_cov and not self.lyrics_loading and not (self.lyrics_track_id == t_cov.id and self.lyrics_lines):
                self.toggle_lyrics(t_cov)
                self.lyrics_overlay = False
            if self._cover_portrait(h, w):  # portrait: lyrics below the cover image
                cover_rows = self._cover_img_rows_portrait(h, w)
                self._draw_playback_lyrics_panel(top_h + cover_rows, 0,
                                              usable_h - cover_rows - 1, w)
            else:       # landscape: lyrics on the right
                lyrics_w = self._lyrics_panel_w(w)
                self._draw_playback_lyrics_panel(top_h, w - lyrics_w, usable_h, lyrics_w)

        if self.tab == TAB_PLAYBACK and self.cover_path and not _throttle_cover:
            # Prevent ncurses scroll-region optimisation for the content rows.
            # redrawln marks those rows as physically corrupted so ncurses
            # rewrites each cell individually on the next refresh instead of
            # issuing ESC[S/ESC[T sequences that shift the sixel image.
            self.stdscr.redrawln(top_h, usable_h)

        # Persist offset so the mouse handler can compute correct queue click rows.
        self._album_cover_rows_offset = artist_cover_rows if queue_panel else 0

        if _pane_active and self._album_cover_path and artist_pane_w > 0:
            _ar_rows = artist_cover_rows if queue_panel else usable_h
            if _ar_rows > 0:
                self.stdscr.redrawln(top_h, _ar_rows)

        # Synchronized output (DEC mode 2026): tells the terminal to buffer all
        # output until the ESU marker, presenting the entire frame atomically.
        # Harmless on terminals that don't support it (unknown sequences ignored).
        sys.stdout.buffer.write(b"\033[?2026h")
        sys.stdout.buffer.flush()
        try:
            self.stdscr.refresh()

            # After curses has refreshed, write the cover image.  Overlays (info,
            # lyrics, help) are drawn AFTER the image so they appear on top of it.
            if self.tab == TAB_PLAYBACK and self.cover_path and not hide_cover and not _throttle_cover:
                # _cover_sixel_visible was cleared at the top of this full-redraw
                # path, so _render_cover_image always re-writes after erase+refresh.
                self._render_cover_image()
                # Re-assert tab-bar and status-bar rows: the image write corrupts
                # the terminal's physical view of those rows, so redrawln forces
                # ncurses to fully repaint them on the next refresh.
                if self._cover_sixel_visible:
                    self.stdscr.redrawln(0, top_h)
                    self.stdscr.redrawln(h - status_h - 1, status_h + 1)
                    self.stdscr.refresh()

            # Side cover pane: render cover in the right pane after curses refresh.
            if _pane_active and self._album_cover_path and artist_pane_w > 0:
                artist_x = w - artist_pane_w
                _render_h = artist_cover_rows if queue_panel else (usable_h - 1)
                if _render_h > 0:
                    self._render_album_cover_pane(top_h, artist_x, artist_pane_w, _render_h)
                    if self._album_cover_visible:
                        self.stdscr.redrawln(0, top_h)
                        self.stdscr.redrawln(h - status_h - 1, status_h + 1)
                        self.stdscr.refresh()
        finally:
            sys.stdout.buffer.write(b"\033[?2026l")
            sys.stdout.buffer.flush()


    # ---------------------------------------------------------------------------
    # tab switching
    # ---------------------------------------------------------------------------
    def _goto_liked_filter(self, f: int) -> None:
        """Switch to (or stay on) the Liked tab and select the given filter index."""
        if self.tab != TAB_LIKED:
            self.switch_tab(TAB_LIKED, refresh=False)
            self.fetch_liked_async()
        self.liked_filter = f
        self._reset_left_cursor()
        self.toast(f"Liked: {LIKED_FILTER_NAMES[f]}")

    def switch_tab(self, t: int, refresh: bool = True) -> None:
        # save current tab position before switching
        self._tab_positions[self.tab] = (self.left_idx, self.left_scroll)
        if t != self.tab:
            self._prev_tab = self.tab
            if self.tab == TAB_PLAYBACK:
                self._cover_clear_image()
            if self.tab == TAB_RECOMMENDED:
                self._recommended_pending_ctx = None  # type: ignore[attr-defined]
            if self.tab == TAB_MIX:
                self._mix_pending_ctx = None          # type: ignore[attr-defined]
            if self.tab == TAB_ARTIST:
                self._artist_pending_ctx = None       # type: ignore[attr-defined]
            # Reset current displayed cover when switching tabs (new context needs fresh cover).
            if self._album_cover_visible:
                self._erase_album_cover_terminal()
            self._album_cover_item_key = ""
            self._album_cover_path = None
            self._album_cover_render_buf = None
            self._album_cover_render_key = ""
            self.stdscr.clearok(True)
            if self.tab == TAB_ALBUM:
                self._album_pending_ctx = None        # type: ignore[attr-defined]

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
            self._reset_left_cursor()

        if t in (TAB_RECOMMENDED, TAB_MIX) and refresh:
            ctx = self._selected_left_track() or self.current_track
            (self.fetch_recommended_async if t == TAB_RECOMMENDED else self.fetch_mix_async)(ctx)
        elif t == TAB_LIKED and refresh:
            self.fetch_liked_async()
        elif t == TAB_ARTIST and refresh:
            ctx = self._current_selection_track() or self.current_track
            self.fetch_artist_async(ctx)
        elif t == TAB_PLAYLISTS:
            self.playlist_names = sorted(self.playlists.keys())
            self.playlist_view_name = None
            self.playlist_view_tracks = []
        elif t == TAB_PLAYBACK:
            _layout = self.settings.get("playback_tab_layout", "lyrics")
            if _layout == "lyrics":
                self._cover_lyrics = True
            elif _layout == "miniqueue":
                self._cover_lyrics = False
                self.queue_overlay = True
            elif _layout == "miniqueue_cover":
                self._cover_lyrics = False
                self.queue_overlay = True
                self._album_cover_pane = True
                self.settings["cover_pane"] = True
            self.fetch_cover_async(self.current_track)
            if self.queue_overlay:
                self.jump_to_playing_in_queue()

        self._full_redraw()

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
        self._full_redraw()

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
            self.jump_to_playing_in_queue()

        def _mk_ctrl(names, fallback=()):
            def _check(c):
                try: return curses.keyname(c) in names
                except Exception: return c in fallback
            return _check
        _is_ctrl_right = _mk_ctrl((b"kRIT5", b"kRIT3"), (444, 560))
        _is_ctrl_left  = _mk_ctrl((b"kLFT5", b"kLFT3"), (443, 545))
        _is_ctrl_down  = _mk_ctrl((b"kDN5", b"kDN3"))
        _is_ctrl_up    = _mk_ctrl((b"kUP5", b"kUP3"))

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
            return cur

        def _alb_up(lst: list, cur: int) -> int:
            if not (0 <= cur < len(lst)) or not isinstance(lst[cur], Track):
                return cur
            key = _gkey(lst[cur])
            i = cur - 1
            while i >= 0 and isinstance(lst[i], Track) and _gkey(lst[i]) == key:
                i -= 1
            start = i + 1
            if start < cur:
                return start
            if i >= 0 and isinstance(lst[i], Track):
                prev_key = _gkey(lst[i])
                while i > 0 and isinstance(lst[i - 1], Track) and _gkey(lst[i - 1]) == prev_key:
                    i -= 1
                return i
            return cur

        def _tog(attr: str, on: str, off: str) -> None:
            v = not getattr(self, attr)
            setattr(self, attr, v)
            self.toast(on if v else off)

        def _skip(d: int) -> None:
            self._skip_delta += d
            self._skip_at = time.time()

        _DISPATCH: Dict[int, Any] = {
            ord("i"): self.toggle_info_selected,
            ord("I"): self.toggle_info_playing,
            ord("P"): self.play_track_with_resume,
            ord("z"): self.jump_to_playing_in_queue,
            ord("B"): self.clear_priority_queue,
            ord("u"): self.unmark_all_current_view,
            ord("U"): self.mark_all_current_view,
            ord("m"): self.mute_toggle,
            ord("l"): self.like_selected,
            ord("L"): self.like_playing,
            ord("*"): self.like_popup_from_playing,
            ord(":"): self.context_actions_popup,
            ord("!"): self.context_actions_popup,
            ord("-"): lambda: self.volume_add(-2.0),
            ord("+"): lambda: self.volume_add(2.0),
            ord("="): lambda: self.volume_add(2.0),
            curses.KEY_LEFT: lambda: self.seek_rel(-5.0),
            curses.KEY_RIGHT: lambda: self.seek_rel(5.0),
            getattr(curses, "KEY_SLEFT", -999): lambda: self.seek_rel(-30.0),
            getattr(curses, "KEY_SRIGHT", -999): lambda: self.seek_rel(30.0),
            ord("/"): self.do_search_prompt_anywhere,
            ord(" "): self.toggle_mark_and_advance,
            ord("a"): self.playlists_add_from_context,
            ord("0"): lambda: self.switch_tab(TAB_PLAYBACK),
            ord("e"): lambda: self.enqueue_key(insert_after_playing=False),
            ord("E"): lambda: self.enqueue_key(insert_after_playing=True),
            ord("R"): lambda: (setattr(self, "repeat_mode", (self.repeat_mode + 1) % 3),
                               self.toast(["Repeat: off", "Repeat: all", "Repeat: one"][self.repeat_mode])),
            ord("S"): lambda: _tog("shuffle_on", "Shuffle: on", "Shuffle: off"),
            ord("F"): lambda: (setattr(self, "quality_idx", (self.quality_idx + 1) % len(QUALITY_ORDER)),
                               self.toast(f"Quality: {QUALITY_ORDER[self.quality_idx]}")),
            ord("T"): lambda: _tog("show_toggles", "Toggles: on", "Toggles: off"),
            ord("c"): lambda: _tog("color_mode", "Color", "B/W"),
            ord("w"): lambda: _tog("show_track_album", "Album field: on", "Album field: off"),
            ord("y"): lambda: _tog("show_track_year", "Year field: on", "Year field: off"),
            ord("<"): lambda: _skip(-1),
            ord(","): lambda: _skip(-1),
            ord(">"): lambda: _skip(+1),
            ord("."): lambda: _skip(+1),
            ord("\\"): lambda: _tog("tab_align", "TSV: on", "TSV: off"),
            ord("f"): lambda: self.lyrics_filter_prompt() if self.tab == TAB_PLAYBACK and self._cover_lyrics and not self.queue_overlay else self.filter_prompt(),
            ord("("): lambda: self.lyrics_filter_next(-1) if self.tab == TAB_PLAYBACK and self._cover_lyrics and not self.queue_overlay else self.filter_next(-1),
            ord(")"): lambda: self.lyrics_filter_next(1) if self.tab == TAB_PLAYBACK and self._cover_lyrics and not self.queue_overlay else self.filter_next(+1),
            ord("p"): lambda: self.play_queue_index(self.queue_play_idx) if not self.mp.alive() and self.queue_items else self.toggle_pause(),
            ord(";"): lambda: self.switch_tab(self._prev_tab, refresh=False) if self._prev_tab != self.tab else None,
            ord("n"): lambda: self.playlists_create() if self.tab == TAB_PLAYLISTS else _tog("show_numbers", "Line numbers: on", "Line numbers: off"),
            ord("d"): lambda: self.playlists_delete_current() if self.tab == TAB_PLAYLISTS else _tog("show_track_duration", "Duration field: on", "Duration field: off"),
        }

        while True:
            now = time.time()

            if self._liked_refresh_due and now >= self._liked_refresh_due:
                self._liked_refresh_due = 0.0
                if self.tab == TAB_LIKED:
                    self.fetch_liked_async()

            self._do_info_fetch_if_due()


            if self.current_track and not self.mp.alive() and self._play_serial == self._current_track_serial:
                tp, du, pa, vo, mu = self.mp.snapshot()
                if tp is None and du is None:
                    self.current_track = None
                    self.next_track()
                    self._full_redraw()

            if now - last_persist > 2.0:
                last_persist = now
                self._persist_settings()

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
                if self._mouse_long_press_pending:
                    t0, _, _ = self._mouse_last_press
                    if time.time() - t0 >= 0.5:
                        self._mouse_long_press_pending = False
                        self.context_actions_popup()
                time.sleep(0.004)
                continue

            self._full_redraw()

            if ch == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, bstate = curses.getmouse()
                except curses.error:
                    continue
                h, w = self.stdscr.getmaxyx()
                top_h = 3 if self.tab == TAB_LIKED else 2
                usable_h = h - top_h - 2
                queue_panel = self.queue_overlay and self.tab != TAB_QUEUE
                left_w = w if not queue_panel else max(20, w - 44)
                if bstate & curses.BUTTON4_PRESSED:                           # wheel up
                    curses.ungetch(ord('k'))
                elif bstate & getattr(curses, 'BUTTON5_PRESSED', 0x200000):  # wheel down
                    curses.ungetch(ord('j'))
                elif self.tab == TAB_LIKED and my == 2 and bstate & curses.BUTTON1_PRESSED:  # liked filter bar
                    _flabels = ["Allᴹ⁻¹", "Tracksᴹ⁻²", "Artistsᴹ⁻³", "Albumsᴹ⁻⁴", "Playlistsᴹ⁻⁵"]
                    cx = 1
                    for i, label in enumerate(_flabels):
                        if cx <= mx < cx + len(label):
                            self._goto_liked_filter(i)
                            break
                        cx += len(label) + 2
                elif my < 2 and bstate & curses.BUTTON1_PRESSED:             # tab bar
                    order = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)
                    ns = self._tab_names_dict(w)
                    pos = 0
                    for i in order:
                        nm = ns[i]
                        if pos <= mx < pos + len(nm):
                            curses.ungetch(ord(str(i % 10)))  # reuse full key handler (context, confirm dialogs, etc.)
                            break
                        pos += len(nm) + 2
                elif my >= h - 2 and bstate & curses.BUTTON1_PRESSED:        # status bar
                    if my == h - 2:                                            # toggles line
                        _tog("show_toggles", "Toggles: on", "Toggles: off")
                    else:                                                       # playback line
                        _DISPATCH[ord("p")]()
                elif top_h <= my < top_h + usable_h:
                    if bstate & curses.BUTTON1_PRESSED:
                        now = time.time()
                        t0, ly, lx = self._mouse_last_press
                        is_dbl = now - t0 < 0.35 and ly == my and lx == mx
                        self._mouse_last_press = (now, my, mx)
                        self._mouse_long_press_pending = not is_dbl
                        if queue_panel and mx >= left_w:                      # queue overlay
                            _q_top = top_h + self._album_cover_rows_offset + 1
                            clicked = self._q_overlay_scroll + (my - _q_top)
                            if my >= _q_top and 0 <= clicked < len(self.queue_items):
                                self.queue_cursor = clamp(clicked, 0, len(self.queue_items) - 1)
                                self.focus = "queue"
                                if is_dbl: curses.ungetch(10)
                        else:                                                  # left panel
                            _, items = self._left_items()
                            clicked = self.left_scroll + (my - top_h)
                            if 0 <= clicked < len(items):
                                if self.tab == TAB_QUEUE:
                                    self.queue_cursor = clamp(clicked, 0, len(items) - 1)
                                    self.focus = "queue"
                                else:
                                    self.left_idx = clicked
                                    self.focus = "left"
                                if is_dbl: curses.ungetch(10)
                            elif self.tab == TAB_PLAYBACK:                       # click on cover image
                                _on_cover = True
                                if self._cover_lyrics and not self.queue_overlay:
                                    if self._cover_portrait(h, w):
                                        _on_cover = (my - top_h) < self._cover_img_rows_portrait(h, w)
                                    else:
                                        _on_cover = mx < w - self._lyrics_panel_w(w)
                                if _on_cover:                                  # cycle pane: lyrics→queue→none→lyrics
                                    if not self.queue_overlay and self._cover_lyrics:
                                        self._cover_lyrics = False
                                        self.queue_overlay = True
                                        self.focus = "queue"
                                        self.queue_cursor = clamp(self.queue_play_idx, 0, len(self.queue_items) - 1)
                                    elif self.queue_overlay:
                                        self.queue_overlay = False
                                        self.focus = "left"
                                    else:
                                        self._cover_lyrics = True
                                        if self.current_track and not self.lyrics_lines and not self.lyrics_loading:
                                            self.toggle_lyrics(self.current_track)
                                            self.lyrics_overlay = False
                                    self._cover_render_key = ""
                                    self._cover_render_buf = None
                    elif bstate & curses.BUTTON3_PRESSED:                     # RMB → context menu or help
                        if queue_panel and mx >= left_w:                      # miniqueue
                            _q_top = top_h + self._album_cover_rows_offset + 1
                            _clicked = self._q_overlay_scroll + (my - _q_top)
                            if my >= _q_top and 0 <= _clicked < len(self.queue_items):
                                self.queue_cursor = clamp(_clicked, 0, len(self.queue_items) - 1)
                                self.focus = "queue"
                                self.draw()
                                self.context_actions_popup()
                        else:
                            _, _items = self._left_items()
                            _clicked = self.left_scroll + (my - top_h)
                            if 0 <= _clicked < len(_items):
                                if self.tab == TAB_QUEUE:
                                    self.queue_cursor = clamp(_clicked, 0, len(self.queue_items) - 1)
                                    self.focus = "queue"
                                else:
                                    self.left_idx = _clicked
                                    self.focus = "left"
                                self.draw()
                                self.context_actions_popup()
                            else:
                                self.show_help_dialog()
                    elif bstate & curses.BUTTON1_RELEASED:
                        self._mouse_long_press_pending = False
                continue

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
                    if 1 <= _ctrl_digit <= 5:
                        self._goto_liked_filter(_ctrl_digit - 1)
                    continue
                # Plain ESC: dismiss overlays
                elif self.tab == TAB_PLAYBACK and self._lyrics_filter_q:
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

            if ch in (ord("?"), ord("h")):
                self.show_help_dialog()
                continue


            if isinstance(ch, int):
                _fn = _DISPATCH.get(ch)
                if _fn is not None:
                    _fn()
                    continue

            # V in playback tab:
            #   - If miniqueue is open → close it and show lyrics (one action, no toggle).
            #   - If miniqueue is closed → toggle inline lyrics panel on/off.
            if ch == ord("V") and self.tab == TAB_PLAYBACK:
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
                if not _sar and self.tab == TAB_PLAYBACK and self.current_track:
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

            if ch == ord("q"):
                self.queue_overlay = not self.queue_overlay
                if not self.queue_overlay and self.focus == "queue":
                    self.focus = "left"
                elif self.queue_overlay and self.tab == TAB_PLAYBACK:
                    self.focus = "queue"
                self.toast("Queue overlay: on" if self.queue_overlay else "Queue overlay: off")
                continue

            if ch == ord("\t") and self.queue_overlay:
                self.focus = "queue" if self.focus == "left" else "left"
                continue

            # Tab switching: 1-9 (keys "1"-"9" map directly to TAB_SEARCH..TAB_HISTORY = 1..9)
            if ord("1") <= ch <= ord("9"):
                t_num = ch - ord("0")
                if t_num == TAB_RECOMMENDED:
                    if self.tab == TAB_RECOMMENDED and not self._queue_context():
                        if self._recommended_pending_ctx:
                            _ctx2 = self._recommended_pending_ctx
                            self._recommended_pending_ctx = None
                            self.fetch_recommended_async(_ctx2)
                        else:
                            _direct = self._current_selection_track() or self.current_track
                            if _direct:
                                self.fetch_recommended_async(_direct)
                        continue
                    ctx = self._current_selection_track() or self.current_track
                    _rc_no_confirm = bool(self.settings.get("recommended_tab_no_confirm_refetch", False))
                    if ctx:
                        if self._recommended_tab_has_content and not _rc_no_confirm:
                            self._recommended_pending_ctx = ctx
                            self.switch_tab(TAB_RECOMMENDED, refresh=False)
                        else:
                            self._recommended_pending_ctx = None
                            self.switch_tab(TAB_RECOMMENDED, refresh=False)
                            self.fetch_recommended_async(ctx)
                    else:
                        self._recommended_pending_ctx = None
                        self.switch_tab(TAB_RECOMMENDED, refresh=False)
                elif t_num == TAB_MIX:
                    if self.tab == TAB_MIX and not self._queue_context():
                        if self._mix_pending_ctx:
                            _mctx = self._mix_pending_ctx
                            self._mix_pending_ctx = None
                            if isinstance(_mctx, Album):
                                self.fetch_mix_from_album_async(_mctx)
                            elif isinstance(_mctx, Artist):
                                self.fetch_mix_from_artist_async(_mctx)
                            else:
                                self.fetch_mix_async(_mctx)
                        else:
                            _dit = self._selected_left_item()
                            _dctx = self._current_selection_track() or self.current_track
                            if isinstance(_dit, Album): self.fetch_mix_from_album_async(_dit)
                            elif isinstance(_dit, Artist): self.fetch_mix_from_artist_async(_dit)
                            elif isinstance(_dit, tuple) and _dit[0] == 'artist_header':
                                self.fetch_mix_from_artist_async(Artist(id=_dit[1][0], name=_dit[1][1]))
                            elif isinstance(_dit, tuple) and _dit[0] == 'album_title' and isinstance(_dit[1], Album):
                                self.fetch_mix_from_album_async(_dit[1])
                            elif _dctx: self.fetch_mix_async(_dctx)
                        continue
                    it = self._selected_left_item() if not self._queue_context() else None
                    ctx = self._current_selection_track() or self.current_track
                    _mx_no_confirm = bool(self.settings.get("mix_tab_no_confirm_refetch", False))
                    if isinstance(it, Album):
                        _mix_seed = it
                    elif isinstance(it, Artist):
                        _mix_seed = it
                    elif isinstance(it, tuple) and it[0] == "artist_header":
                        _mix_seed = Artist(id=it[1][0], name=it[1][1])
                    elif isinstance(it, tuple) and it[0] == "album_title" and isinstance(it[1], Album):
                        _mix_seed = it[1]
                    else:
                        _mix_seed = ctx
                    if _mix_seed:
                        if self._mix_tab_has_content and not _mx_no_confirm:
                            self._mix_pending_ctx = _mix_seed
                            self.switch_tab(TAB_MIX, refresh=False)
                        else:
                            self._mix_pending_ctx = None
                            self.switch_tab(TAB_MIX, refresh=False)
                            if isinstance(_mix_seed, Album):
                                self.fetch_mix_from_album_async(_mix_seed)
                            elif isinstance(_mix_seed, Artist):
                                self.fetch_mix_from_artist_async(_mix_seed)
                            else:
                                self.fetch_mix_async(_mix_seed)
                    else:
                        self._mix_pending_ctx = None
                        self.switch_tab(TAB_MIX, refresh=False)

                elif t_num == TAB_ARTIST:
                    if self.tab == TAB_PLAYBACK:
                        self.switch_tab(TAB_ARTIST)
                        continue
                    if self.tab == TAB_ARTIST and not self._queue_context():
                        if self._artist_pending_ctx:
                            _ctx2 = self._artist_pending_ctx
                            self._artist_pending_ctx = None
                            self.fetch_artist_async(_ctx2)
                        continue
                    it = self._selected_left_item() if not self._queue_context() else None
                    if isinstance(it, Artist):
                        self._artist_pending_ctx = None
                        self.open_artist_by_id(it.id, it.name)
                        continue
                    if isinstance(it, tuple) and it[0] == "album_title" and isinstance(it[1], Album):
                        _alb_it = it[1]
                        ctx = Track(id=0, title="", artist=_alb_it.artist, album="",
                                    year="????", track_no=0)
                    elif isinstance(it, Album):
                        ctx = Track(id=0, title="", artist=it.artist, album="",
                                    year="????", track_no=0)
                    else:
                        ctx = self._current_selection_track() or self.current_track
                    _ar_no_confirm = bool(self.settings.get("artist_tab_no_confirm_refetch", False))
                    if ctx:
                        if self._artist_tab_has_content and not _ar_no_confirm:
                            self._artist_pending_ctx = ctx
                            self.switch_tab(TAB_ARTIST, refresh=False)
                        else:
                            self._artist_pending_ctx = None
                            self.switch_tab(TAB_ARTIST, refresh=False)
                            self.fetch_artist_async(ctx)
                    else:
                        self._artist_pending_ctx = None
                        self.switch_tab(TAB_ARTIST, refresh=False)

                elif t_num == TAB_ALBUM:
                    if self.tab == TAB_ALBUM and not self._queue_context():
                        if self._album_pending_ctx:
                            _ctx2 = self._album_pending_ctx
                            self._album_pending_ctx = None
                            self.open_album_from_track(_ctx2)
                        continue
                    it = self._selected_left_item() if not self._queue_context() else None
                    ctx = self._current_selection_track() or self.current_track
                    _al_no_confirm = bool(self.settings.get("album_tab_no_confirm_refetch", False))
                    if isinstance(it, Album):
                        self._album_pending_ctx = None
                        self.open_album_from_album_obj(it)
                    elif ctx:
                        if self._album_tab_has_content and not _al_no_confirm:
                            self._album_pending_ctx = ctx
                            self.switch_tab(TAB_ALBUM, refresh=False)
                        else:
                            self._album_pending_ctx = None
                            self.open_album_from_track(ctx)
                    else:
                        self._album_pending_ctx = None
                        self.switch_tab(TAB_ALBUM, refresh=False)
                elif t_num == TAB_LIKED:
                    if self.tab == TAB_LIKED:
                        self._goto_liked_filter(
                            (self.liked_filter + 1) % len(LIKED_FILTER_NAMES)
                        )
                    else:
                        self.switch_tab(TAB_LIKED, refresh=True)
                else:
                    self.switch_tab(t_num, refresh=True)
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
            if ch == ord("C"):
                self._album_cover_pane = not self._album_cover_pane
                self.settings["cover_pane"] = self._album_cover_pane
                if not self._album_cover_pane:
                    # Only erase the rendered image; keep cached paths/tmpdir intact.
                    self._erase_album_cover_terminal()
                self._full_redraw()
                continue

            if ch == ord("#"):
                self._show_singles_eps = not self._show_singles_eps
                self.settings["include_singles_and_eps_in_artist_tab"] = self._show_singles_eps
                self.toast(f"Singles/EPs: {'on' if self._show_singles_eps else 'off'}")
                if self.tab == TAB_ARTIST:
                    _ref = self._last_artist_fetch_track
                    if not _ref and self.artist_ctx:
                        _ref = Track(id=0, title='', artist=self.artist_ctx[1],
                                     album='', year='????', track_no=0,
                                     artist_id=self.artist_ctx[0])
                    if _ref:
                        # Bypass cache so the toggle always triggers a live refetch
                        _cache_key = (_ref.artist_id or
                                      (self.artist_ctx[0] if self.artist_ctx else None))
                        if _cache_key:
                            self._artist_cache.pop(int(_cache_key), None)
                        self.fetch_artist_async(_ref)
                self._full_redraw()
                continue
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                if not (self.tab == TAB_PLAYLISTS and self.playlist_view_name is not None):
                    if self._prev_tab != self.tab:
                        self.switch_tab(self._prev_tab, refresh=False)
                    continue
                # falls through to the TAB_PLAYLISTS block to exit playlist view
            if ch in (ord("["), ord("]")) and self.tab == TAB_LIKED:
                delta = 1 if ch == ord("]") else -1
                self._goto_liked_filter((self.liked_filter + delta) % len(LIKED_FILTER_NAMES))
                continue

            # Ctrl+Left/Ctrl+Right: cycle through all tabs including Liked subtabs
            # Sequence: Search Queue Recommended Mix Artist Album
            #           Liked/All Liked/Tracks Liked/Artists Liked/Albums Liked/Playlists
            #           Playlists History Playback (wraps)
            if isinstance(ch, int) and (_is_ctrl_left(ch) or _is_ctrl_right(ch)):
                _NAV_SEQ = [
                    (TAB_SEARCH, 0),
                    (TAB_QUEUE, 0), (TAB_RECOMMENDED, 0), (TAB_MIX, 0),
                    (TAB_ARTIST, 0), (TAB_ALBUM, 0),
                    (TAB_LIKED, 0), (TAB_LIKED, 1), (TAB_LIKED, 2),
                    (TAB_LIKED, 3), (TAB_LIKED, 4),
                    (TAB_PLAYLISTS, 0), (TAB_HISTORY, 0), (TAB_PLAYBACK, 0),
                ]
                cur_f = self.liked_filter if self.tab == TAB_LIKED else 0
                cur_pos = next(
                    (i for i, (t, f) in enumerate(_NAV_SEQ)
                     if t == self.tab and f == cur_f), 0)
                delta = 1 if _is_ctrl_right(ch) else -1
                nxt_tab, nxt_f = _NAV_SEQ[(cur_pos + delta) % len(_NAV_SEQ)]
                if nxt_tab == TAB_LIKED:
                    self._goto_liked_filter(nxt_f)
                else:
                    self.switch_tab(nxt_tab, refresh=True)
                    self._need_redraw = True
                continue

            # Ctrl+Down/Ctrl+Up: jump between album groups/sections
            if isinstance(ch, int) and (_is_ctrl_down(ch) or _is_ctrl_up(ch)):
                _dir = 1 if _is_ctrl_down(ch) else -1
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

            if self.tab == TAB_PLAYLISTS:
                if ch in (curses.KEY_BACKSPACE, 127, 8):
                    if self.playlist_view_name is not None:
                        self.playlist_view_name = None
                        self.playlist_view_tracks = []
                        self.playlist_names = sorted(self.playlists.keys())
                        self._reset_left_cursor()
                        self._full_redraw()
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
                    self._toast_redraw("Removed from playlist")
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
                    self._toast_redraw("Queue cleared")
                continue

            if ch in (ord("J"), ord("K"),
                      getattr(curses, "KEY_SR", -998), getattr(curses, "KEY_SF", -997)):
                if not self._queue_context(): continue
                if not self.queue_items: continue
                delta = +1 if ch in (ord("J"), getattr(curses, "KEY_SF", -997)) else -1
                idxs = sorted([i for i in self.marked_queue_idx if 0 <= i < len(self.queue_items)])
                if not idxs:
                    i = clamp(self.queue_cursor, 0, len(self.queue_items) - 1)
                    j = i + delta
                    if 0 <= j < len(self.queue_items):
                        self._swap_queue_items(i, j)
                        self.queue_cursor = j
                    continue

                s = set(idxs)
                for i in (idxs if delta < 0 else reversed(idxs)):
                    j = i + delta
                    if 0 <= j < len(self.queue_items) and j not in s:
                        self._swap_queue_items(i, j)
                        s.discard(i); s.add(j)
                self.marked_queue_idx = s
                m = sorted(s)
                if m:
                    self.queue_cursor = m[0] if delta < 0 else m[-1]
                continue

            if ch == ord("D"):
                if self.tab == TAB_PLAYLISTS and self.playlist_view_name is None:
                    self.playlists_download_prompt()
                    continue
                if not self._queue_context():
                    marked_albums, marked_artists, marked_playlists, _cancelled = self._marked_batch()
                    if _cancelled: continue
                    if marked_albums:
                        self._download_marked_albums_async(marked_albums)
                        continue
                    if marked_artists:
                        self._download_marked_artists_async(marked_artists)
                        continue
                    if marked_playlists:
                        self.start_download_tracks(self._tracks_from_playlists(marked_playlists))
                        continue
                if not self._queue_context() and self.tab == TAB_ALBUM and self._selected_album_title_line():
                    self.start_download_tracks(list(self.album_tracks))
                    continue
                if not self._queue_context() and self.tab == TAB_ARTIST:
                    _dit = self._selected_left_item()
                    if isinstance(_dit, tuple) and _dit[0] == "artist_header":
                        if self.artist_albums:
                            self._download_marked_albums_async(self.artist_albums)
                        continue
                    alb = self._selected_left_album()
                    if alb:
                        self._bg_download_album(alb)
                        continue
                if self.tab == TAB_PLAYLISTS and self.playlist_view_name is not None:
                    self.start_download_tracks(list(self.playlist_view_tracks))
                    continue
                self.start_download_tracks(self._target_tracks())
                continue

            # In the playback tab with the inline lyrics panel (no queue overlay, no
            # full-screen overlay), intercept navigation keys to scroll the panel.
            if (self.tab == TAB_PLAYBACK and self._cover_lyrics and not self.queue_overlay):
                _lmax = self._cover_lyrics_max_scroll; _p = self._page_step()
                _nv = (min(self.lyrics_scroll + 1, _lmax) if ch in (curses.KEY_DOWN, ord("j"), 14) else
                       max(0, self.lyrics_scroll - 1) if ch in (curses.KEY_UP, ord("k"), 16) else
                       max(0, self.lyrics_scroll - _p) if ch == curses.KEY_PPAGE else
                       min(self.lyrics_scroll + _p, _lmax) if ch == curses.KEY_NPAGE else
                       0 if ch in (curses.KEY_HOME, ord("g")) else
                       _lmax if ch in (curses.KEY_END, ord("G")) else None)
                if _nv is not None:
                    self.lyrics_scroll = _nv
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
            if ch in (curses.KEY_DOWN, ord("j"), curses.KEY_UP, ord("k"),
                      getattr(curses, "KEY_SF", -997), getattr(curses, "KEY_SR", -998)):
                d = 1 if ch in (curses.KEY_DOWN, ord("j"), getattr(curses, "KEY_SF", -997)) else -1
                if self._queue_context():
                    self.queue_cursor = clamp(self.queue_cursor + d, 0, max(0, len(self.queue_items) - 1))
                else:
                    _typ, items = self._left_items()
                    new_idx = self.left_idx + d
                    while (0 <= new_idx < len(items)) and isinstance(items[new_idx], tuple) and items[new_idx][0] in ("sep", "pending_refetch_hint"):
                        new_idx += d
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
                    self._bg(lambda _t=it: self.play_track(_t), on_error="")
                continue

        # Cleanup
        for _fn in (lambda: save_queue(self.queue_items, self.queue_play_idx),
                    self._save_liked,
                    lambda: save_playlists(self.playlists, self.playlists_meta)):
            try:
                _fn()
            except Exception:
                pass
        try:
            if self.current_track and self.mp.alive():
                _tp, _du, _pa, _vo, _mu = self.mp.snapshot()
                if _tp is not None and _tp > 1.0:
                    self.settings["_resume_queue_idx"] = self.queue_play_idx
                    self.settings["_resume_position"] = float(_tp)
        except Exception:
            pass
        self._persist_settings()
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
                "  --api URL, -a URL   TIDAL HiFi API base URL (can also be set in settings.jsonc)\n"
                "  --verbose, -v       Write debug log to debug.log in the config directory\n"
                "  --version, -V       Show version\n"
                "\n"
                f"Press ? in tuifi for keybinds more options\n"
            )
            sys.exit(0)
        i += 1
    return out


def _probe_sixel_support() -> bool:
    """Detect sixel support via the DA1 terminal query (Primary Device Attributes).

    Sends ESC[c to /dev/tty and waits up to 500 ms for the response.  A terminal
    that supports sixel includes attribute 4 in its reply, e.g.:
        ESC[?64;1;2;4;6c
    This is the only truly reliable method; environment-variable heuristics (TERM,
    COLORTERM, …) do not indicate sixel capability.  Must be called before
    curses.wrapper() while the terminal is in its normal cooked/echo state.
    """
    if sys.platform == "win32":
        return False
    import select
    import termios

    if not sys.stdin.isatty():
        return False
    try:
        fd = os.open("/dev/tty", os.O_RDWR)
        try:
            old = termios.tcgetattr(fd)
            raw = termios.tcgetattr(fd)
            raw[3] &= ~(termios.ICANON | termios.ECHO | termios.ECHONL)  # type: ignore[index]
            raw[6][termios.VMIN] = 0   # type: ignore[index]
            raw[6][termios.VTIME] = 0  # type: ignore[index]
            termios.tcsetattr(fd, termios.TCSAFLUSH, raw)
            os.write(fd, b"\033[c")
            resp = b""
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                rem = deadline - time.monotonic()
                r, _, _ = select.select([fd], [], [], rem)
                if not r:
                    break
                chunk = os.read(fd, 64)
                if not chunk:
                    break
                resp += chunk
                if b"c" in resp:
                    break
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            os.close(fd)
        m = re.search(rb"\x1b\[\?([0-9;]+)c", resp)
        if m:
            return b"4" in m.group(1).split(b";")
        return False
    except Exception as e:
        debug_log(f"sixel probe error: {e}")
        return False


def main(argv: List[str]) -> int:
    locale.setlocale(locale.LC_ALL, "")
    args = parse_args(argv)

    if not args.get("_api_explicit"):
        stored = load_settings()
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

    _api_val = args.get("api", "") or ""
    if args.get("_api_explicit") and _api_val:
        _s = load_settings()
        if _s.get("api") != _api_val:
            _s["api"] = _api_val
            save_settings(_s)
            print(f"API saved to settings: {_api_val}")
    if not _api_val:
        print("ERROR: No TIDAL HiFi API URL configured. Check the README for guidelines.")
        print("  Set one at runtime (and it will be saved in settings) with: tuifi --api https://api.example-hifi-instance.com")
        print("  or in settings.jsonc directly: { \"api\": \"https://api.example-hifi-instance.com\" }")
        sys.exit(1)

    def wrapped(stdscr: "curses._CursesWindow") -> None:
        app = App(stdscr, _api_val, args)
        if app.tab == TAB_QUEUE:
            app.focus = "queue"
        app.run()

    global _SIXEL_SUPPORTED
    _SIXEL_SUPPORTED = _probe_sixel_support()

    os.environ.setdefault("ESCDELAY", "25")  # shorten ncurses ESC wait (ms)
    curses.wrapper(wrapped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
