from __future__ import annotations
import base64
import json
import os
import random
import re
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional
from ..models import Track, Album
from ..config import QUALITY_ORDER, AUTOPLAY_OFF, AUTOPLAY_MIX, AUTOPLAY_RECOMMENDED, APP_NAME, AUTOPLAY_NAMES
from ..api import http_get_bytes, http_get_json
from ..utils import debug_log, clamp, fmt_time


class PlaybackMixin:
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
        2. Trigger a prefetch (if one isn't already running / buffer non-empty).
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

            mpd_path = f"/tmp/{APP_NAME}-{track_id}-{int(time.time()*1000)}.mpd"
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

    def play_track(self, t: Track, resume: bool = False) -> None:
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
                self.mp.start(url, resume=resume)
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
    def play_queue_index(self, idx: int) -> None:
        if not self.queue_items:
            return
        idx = clamp(idx, 0, len(self.queue_items) - 1)
        prev_play_idx = self.queue_play_idx
        self.queue_play_idx = idx
        self.queue_cursor = idx
        if idx in self.priority_queue:
            self.priority_queue.remove(idx)
        self.play_track(self.queue_items[idx])
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
