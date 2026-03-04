from __future__ import annotations
from typing import Any, List, Optional, Tuple
from ..models import Track, Album
from ..config import (TAB_QUEUE, TAB_SEARCH, TAB_RECOMMENDED, TAB_MIX,
                      TAB_ARTIST, TAB_ALBUM, TAB_LIKED, TAB_PLAYLISTS, TAB_HISTORY)
from ..utils import clamp


class NavigationMixin:
    def _queue_context(self) -> bool:
        return self.tab == TAB_QUEUE or self.focus == "queue"

    def _left_items(self) -> Tuple[str, List[Any]]:
        if self._loading:
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
            if self.artist_albums:
                items.append(("sep", "Albums"))
                items.extend(self.artist_albums)
            if self.artist_tracks:
                items.append(("sep", "Tracks"))
                items.extend(self.artist_tracks)
            return ("artist_mixed", items)
        if self.tab == TAB_ALBUM:
            items = []
            if self.album_header:
                items.append(("album_title", self.album_header))
            items.extend(self.album_tracks)
            return ("album_mixed", items)
        if self.tab == TAB_LIKED:
            return ("tracks", self.liked_cache)
        if self.tab == TAB_PLAYLISTS:
            if self.playlist_view_name is None:
                return ("playlists", self.playlist_names)
            return ("tracks", self.playlist_view_tracks)
        if self.tab == TAB_HISTORY:
            return ("tracks", self.history_tracks)
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
        return self._selected_left_track()

    def switch_tab(self, t: int, refresh: bool = True) -> None:
        self.tab = t
        self.left_idx = 0
        self.left_scroll = 0
        self._loading = False
        self._loading_key = ""
        self.show_help = False
        self.marked_left_idx.clear()

        if self.tab == TAB_QUEUE:
            self.focus = "queue"
        elif self.focus == "queue" and not self.queue_overlay:
            self.focus = "left"

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
