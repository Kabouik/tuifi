from __future__ import annotations
import base64
import os
import random
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from ..models import Track, Album, track_to_mono, mono_to_track
from ..config import QUALITY_ORDER, TAB_ARTIST, TAB_ALBUM, TAB_PLAYLISTS
from ..api import http_get_bytes, http_get_json, http_stream_download
from ..persistence import save_liked, save_playlists, save_history
from ..utils import debug_log, safe_filename, mkdirp, clamp, fmt_dur, year_norm
import tuifi_pkg.config as _config_mod


class LibraryMixin:
    def _record_history(self, t: Track) -> None:
        self.history_tracks = [h for h in self.history_tracks if h.id != t.id]
        self.history_tracks.insert(0, t)
        limit = int(self.settings.get("history_max", 0) or 0)
        if limit > 0:
            self.history_tracks = self.history_tracks[:limit]
        save_history(self.history_tracks)

    def _schedule_liked_refresh(self) -> None:
        self._liked_refresh_due = time.time() + 1.0

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
        save_liked(self.liked_tracks)
        self._schedule_liked_refresh()

    def like_selected(self) -> None:
        if self._queue_context():
            marked = self._marked_tracks_from_queue()
            if marked:
                for t in marked:
                    self.toggle_like(t, silent=True)
                self.toast(f"Liked/unliked {len(marked)}")
                return
            t = self._queue_selected_track()
        else:
            marked = self._marked_tracks_from_left()
            if marked:
                for t in marked:
                    self.toggle_like(t, silent=True)
                self.toast(f"Liked/unliked {len(marked)}")
                return
            t = self._selected_left_track()
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
            self.marked_left_idx = {i for i, it in enumerate(items) if isinstance(it, Track)}
        self.toast("Marked all")
        self._need_redraw = True
        self._redraw_status_only = False

    def unmark_all_current_view(self) -> None:
        if self._queue_context():
            self.marked_queue_idx.clear()
        else:
            self.marked_left_idx.clear()
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
            if isinstance(items[i], Track):
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
            try:
                aid = self._resolve_album_id_for_album(album)
                if not aid:
                    self.toast("Album id?")
                    return
                tracks = self._fetch_album_tracks_by_album_id(aid)
                self._enqueue_tracks(tracks, insert_after_playing)
            except Exception:
                self.toast("Error")
        threading.Thread(target=worker, daemon=True).start()

    def enqueue_key(self, insert_after_playing: bool) -> None:
        if not self._queue_context():
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
        structure = str(self.settings.get("download_structure") or "{artist}/{artist} - {album} ({year})")
        rel_path = structure.format(
            artist=safe_filename(t.artist),
            album=safe_filename(t.album),
            year=yv if yv != "????" else "unknown",
        )
        out_dir = os.path.join(_config_mod.DOWNLOADS_DIR, rel_path)
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

    def start_download_tracks(self, tracks: List[Track]) -> None:
        if not tracks:
            self.toast("Nothing to download")
            return
        self.dl.progress_line = f"DL queued {len(tracks)}"
        self._need_redraw = True
        self._redraw_status_only = True
        self.dl.enqueue(tracks, self._download_worker)

    # ---------------------------------------------------------------------------
    # album resolve / fetch
    # ---------------------------------------------------------------------------
    def _resolve_album_id_for_album(self, album: Album) -> Optional[int]:
        if album.id and album.id > 0:
            return album.id
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
        self._need_redraw = True
        self._redraw_status_only = False

    def fetch_artist_async(self, ctx: Optional[Track]) -> None:
        if not ctx:
            self.toast("No context")
            return
        self.artist_albums, self.artist_tracks = [], []
        key = f"artist:{ctx.id}:{time.time()}"
        self._set_loading(key)

        def worker() -> None:
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
                tracks: List[Track] = []

                if aid:
                    payload = self.client.artist(int(aid))
                    if self._loading_key != key:
                        return
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
                                    aobj = self._parse_album_obj(x)
                                    if aobj:
                                        albums.append(aobj)
                            if albums:
                                break

                    dicts: List[Dict[str, Any]] = []
                    self._scan_for_track_dicts(payload, dicts, limit=1200)
                    for d in dicts:
                        t = self._parse_track_obj(d)
                        if t:
                            tracks.append(t)

                if not tracks:
                    payload = self.client.search_tracks(ctx.artist, limit=300)
                    if self._loading_key != key:
                        return
                    a0 = ctx.artist.strip().lower()
                    tracks = [t for t in self._extract_tracks_from_search(payload) if t.artist.strip().lower() == a0]

                if not albums:
                    best: Dict[Tuple, Album] = {}
                    for t in tracks:
                        k2 = (t.artist.strip().lower(), t.album.strip().lower())
                        if k2 not in best:
                            best[k2] = Album(id=t.album_id or 0, title=t.album, artist=t.artist, year=t.year)
                        else:
                            cur = best[k2]
                            if cur.id == 0 and t.album_id:
                                cur.id = t.album_id
                            if year_norm(cur.year) == "????" and year_norm(t.year) != "????":
                                cur.year = t.year
                    albums = sorted(best.values(), key=lambda a: (-(int(a.year) if year_norm(a.year) != "????" else 0), a.title.lower()))

                self.artist_albums = albums[:500]
                self.artist_tracks = tracks[:600]
                self.toast("Artist")
            except Exception as e:
                if self._loading_key == key:
                    self.last_error = str(e)
                    self.toast("Error")
            finally:
                self._clear_loading(key)

        threading.Thread(target=worker, daemon=True).start()
