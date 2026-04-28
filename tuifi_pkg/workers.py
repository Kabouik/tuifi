"""Background workers: MetaFetcher and DownloadManager."""

from __future__ import annotations

import threading
import time
from queue import Queue, Empty
from typing import Any, Dict, List, Optional, Tuple

from tuifi_pkg.models import Track, album_year_from_obj, debug_log
from tuifi_pkg.client import HiFiClient


class MetaFetcher:
    def __init__(self, client: HiFiClient) -> None:
        self.client = client
        self.q: "Queue[int]" = Queue()
        self.pending: set = set()
        self.lock = threading.Lock()
        self.year: Dict[int, str] = {}
        self.album_id: Dict[int, int] = {}
        self.artist_id: Dict[int, int] = {}
        self.duration: Dict[int, int] = {}
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True

    def want(self, tid: int) -> None:
        if tid <= 0:
            return
        with self.lock:
            if tid in self.pending:
                return
            self.pending.add(tid)
            self.q.put(tid)

    def _run(self) -> None:
        while not self._stop:
            try:
                tid = self.q.get(timeout=0.2)
            except Empty:
                continue
            try:
                info = self.client.info(tid)
                data = info.get("data") if isinstance(info, dict) else None
                if isinstance(data, dict):
                    alb = data.get("album")
                    if isinstance(alb, dict):
                        y = album_year_from_obj(alb)
                        if y != "????":
                            self.year[tid] = y
                        if str(alb.get("id", "")).isdigit():
                            self.album_id[tid] = int(alb["id"])
                    if tid not in self.year:
                        y2 = album_year_from_obj(data)
                        if y2 != "????":
                            self.year[tid] = y2
                    a = data.get("artist")
                    if isinstance(a, dict) and str(a.get("id", "")).isdigit():
                        self.artist_id[tid] = int(a["id"])
                    dv = data.get("duration")
                    if isinstance(dv, (int, float)) and dv > 0:
                        self.duration[tid] = int(dv)
            except Exception:
                pass
            finally:
                with self.lock:
                    self.pending.discard(tid)
                self.q.task_done()


class DownloadManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: List[Track] = []
        self._thread: Optional[threading.Thread] = None
        self.active = False
        self.paused = False
        self._resume_event = threading.Event()
        self._resume_event.set()
        self.progress_line = ""
        self.progress_clear_at = 0.0
        self.error: Optional[str] = None
        self._total = 0
        self._completed = 0
        self.failed = 0
        self._all_tracks: List[Track] = []           # full ordered list since last fresh batch
        self._track_status: Dict[int, str] = {}      # track.id → "DONE"|"FAIL"

    def toggle_pause(self) -> bool:
        """Toggle pause state. Returns True if now paused."""
        with self._lock:
            self.paused = not self.paused
        if self.paused:
            self._resume_event.clear()
        else:
            self._resume_event.set()
        return self.paused

    def cancel(self) -> None:
        """Clear pending queue. Current download finishes normally."""
        with self._lock:
            for t in self._queue:
                self._track_status[t.id] = "CANC"
            n = len(self._queue)
            self._queue.clear()
            self._total -= n
        self.paused = False
        self._resume_event.set()

    def remove(self, track_id: int) -> None:
        """Remove a track from the queue and display list by id."""
        with self._lock:
            before = len(self._queue)
            self._queue = [t for t in self._queue if t.id != track_id]
            self._total -= before - len(self._queue)
            self._all_tracks = [t for t in self._all_tracks if t.id != track_id]
            self._track_status.pop(track_id, None)

    def retry_failed(self, worker_fn) -> int:
        """Re-queue all FAIL tracks. Returns the number of tracks re-queued."""
        with self._lock:
            fail_tracks = [t for t in self._all_tracks if self._track_status.get(t.id) == "FAIL"]
            if not fail_tracks:
                return 0
            for t in fail_tracks:
                del self._track_status[t.id]
                self._queue.append(t)
            self.failed -= len(fail_tracks)
            self._total += len(fail_tracks)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, args=(worker_fn,), daemon=True)
                self._thread.start()
        return len(fail_tracks)

    def mark_result(self, t: Track, status: str) -> None:
        """Record a track result ('DONE' or 'FAIL') for display in the dialog."""
        with self._lock:
            self._track_status[t.id] = status

    def queue_snapshot(self) -> tuple:
        """Return (all_tracks, track_status, completed, total, failed) as a safe copy."""
        with self._lock:
            return list(self._all_tracks), dict(self._track_status), self._completed, self._total, self.failed

    def enqueue(self, tracks: List[Track], worker_fn) -> None:
        if not tracks:
            return
        with self._lock:
            if not self._queue and not self.active:
                self._total = len(tracks)
                self._completed = 0
                self.failed = 0
                self._all_tracks = list(tracks)
                self._track_status = {}
            else:
                self._total += len(tracks)
                self._all_tracks.extend(tracks)
            self._queue.extend(tracks)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, args=(worker_fn,), daemon=True)
                self._thread.start()

    def _run(self, worker_fn) -> None:
        self.active = True
        self.error = None
        while True:
            self._resume_event.wait()
            with self._lock:
                if not self._queue:
                    break
                t = self._queue.pop(0)
                remaining = len(self._queue)
                current = self._total - remaining
                total = self._total
            try:
                worker_fn(t, remaining, current, total, self._set_progress)
            except Exception as e:
                self.error = str(e)
            with self._lock:
                self._completed += 1
        self.active = False
        self.progress_clear_at = time.time() + 2.0

    def _set_progress(self, s: str) -> None:
        with self._lock:
            self.progress_line = s
