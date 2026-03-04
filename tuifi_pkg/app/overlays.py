from __future__ import annotations
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional
from ..models import Track, Album
from ..config import TAB_LIKED, TAB_PLAYLISTS, TAB_QUEUE, TAB_ALBUM, TAB_ARTIST, TAB_SEARCH
from ..persistence import save_playlists
from ..utils import debug_log, clamp, fmt_dur


class OverlaysMixin:
    def _request_info_refresh(self, t: Track) -> None:
        self._info_target_id = t.id
        self._info_refresh_due = time.time() + 0.12
        self.info_track = t
        self.info_album = None
        self.info_loading = True
        self._need_redraw = True
        self._redraw_status_only = False

    def _do_info_fetch_if_due(self) -> None:
        if not self.info_overlay or not self._info_target_id:
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
        if self.info_overlay:
            self.info_overlay = False
            self._need_redraw = True
            return
        self.info_follow_selection = True
        if self.tab == TAB_ALBUM and self._selected_album_title_line() and self.album_header:
            self.open_info_album(self.album_header)
            return
        if self.tab == TAB_ARTIST:
            alb = self._selected_left_album()
            if alb:
                self.open_info_album(alb)
                return
        t = self._current_selection_track()
        if not t:
            return
        self.info_overlay = True
        self.info_scroll = 0
        self._request_info_refresh(t)
        self._need_redraw = True

    def toggle_info_playing(self) -> None:
        if self.info_overlay:
            self.info_overlay = False
            self._need_redraw = True
            return
        self.info_follow_selection = False
        t = self.current_track
        if not t:
            return
        self.info_overlay = True
        self.info_scroll = 0
        self._request_info_refresh(t)
        self._need_redraw = True

    def open_info_album(self, album: Album) -> None:
        self.info_overlay = True
        self.info_scroll = 0
        self.info_track = None
        self.info_album = album
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

    # ---------------------------------------------------------------------------
    # search
    # ---------------------------------------------------------------------------
    def do_search_prompt_anywhere(self) -> None:
        self.playlist_view_name = None
        self.switch_tab(TAB_SEARCH, refresh=False)
        q = self.prompt_text("Search:", self.search_q)
        if q is None:
            return
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
        cur = self._get_filter_cursor()
        j = next((k for k, idx in enumerate(self.filter_hits) if idx >= cur), 0)
        self.filter_pos = j
        self._set_filter_cursor(self.filter_hits[self.filter_pos])
        self.toast(f"{self.filter_pos+1}/{len(self.filter_hits)}")

    def filter_next(self, delta: int) -> None:
        if not self.filter_hits:
            return
        self.filter_pos = (self.filter_pos + delta) % len(self.filter_hits)
        self._set_filter_cursor(self.filter_hits[self.filter_pos])
        self.toast(f"{self.filter_pos+1}/{len(self.filter_hits)}")

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

    def playlists_add_tracks(self, tracks: List[Track]) -> None:
        if not tracks:
            self.toast("No tracks")
            return
        name = self.pick_playlist("Add to playlist")
        if not name:
            return
        self.playlists.setdefault(name, []).extend(tracks)
        save_playlists(self.playlists, self.playlists_meta)
        self.toast(f"Added {len(tracks)}")
        self._need_redraw = True
        self._redraw_status_only = False
        if self.tab == TAB_PLAYLISTS and self.playlist_view_name == name:
            self.playlist_view_tracks = list(self.playlists[name])

    def playlists_add_from_context(self) -> None:
        if self._queue_context():
            tracks = self._marked_tracks_from_queue() or ([self._queue_selected_track()] if self._queue_selected_track() else [])
        else:
            tracks = self._marked_tracks_from_left() or ([self._selected_left_track()] if self._selected_left_track() else [])
        self.playlists_add_tracks([t for t in tracks if t])
