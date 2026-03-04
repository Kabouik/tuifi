from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from queue import Queue, Empty
from typing import Any, Dict, List, Optional, Tuple

from .config import APP_NAME
from .models import Track
from .api import HiFiClient
from .utils import debug_log, album_year_from_obj

# module-level reference so MPV.start() can check it without importing utils._DEBUG_LOG directly
import tuifi_pkg.utils as _utils_mod


class MPV:
    """mpv JSON IPC (control + state)."""
    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.sock_path: Optional[str] = None
        self._req_id = 0
        self._lock = threading.Lock()
        self.time_pos: Optional[float] = None
        self.duration: Optional[float] = None
        self.pause: Optional[bool] = None
        self.volume: Optional[float] = None
        self.mute: Optional[bool] = None
        self._mpv_stderr_path: Optional[str] = None
        self._mpv_stderr_fh = None

    def start(self, url: str, resume: bool = False) -> None:
        self.stop()
        self.sock_path = f"/tmp/{APP_NAME}-mpv-{os.getpid()}-{int(time.time()*1000)}.sock"
        try:
            if os.path.exists(self.sock_path):
                os.unlink(self.sock_path)
        except Exception:
            pass
        args = [
            "mpv", "--no-video", "--force-window=no", "--really-quiet",
            "--idle=no", f"--input-ipc-server={self.sock_path}",
        ]
        if not resume:
            args.append("--no-resume-playback")

        is_mpd = isinstance(url, str) and url.endswith(".mpd") and os.path.isfile(url)
        if is_mpd:
            args += [
                "--demuxer-lavf-o=allowed_extensions=MPD,m4s,mp4,aac,flac,mp3,frag",
                "--ytdl=no",
                "--cache=no",
            ]

        args.append(url)

        if _utils_mod._DEBUG_LOG:
            self._mpv_stderr_path = f"/tmp/{APP_NAME}-mpv-err-{os.getpid()}.log"
            try:
                self._mpv_stderr_fh = open(self._mpv_stderr_path, "w")
                stderr_target: Any = self._mpv_stderr_fh
            except Exception:
                self._mpv_stderr_fh = None
                stderr_target = subprocess.DEVNULL
        else:
            self._mpv_stderr_path = None
            self._mpv_stderr_fh = None
            stderr_target = subprocess.DEVNULL

        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=stderr_target)
        for _ in range(40):
            if self.sock_path and os.path.exists(self.sock_path):
                break
            time.sleep(0.02)

    def stop(self) -> None:
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2.0)
            except Exception:
                pass
            if _utils_mod._DEBUG_LOG and self._mpv_stderr_fh:
                try:
                    self._mpv_stderr_fh.flush()
                    self._mpv_stderr_fh.close()
                    self._mpv_stderr_fh = None
                    if self._mpv_stderr_path and os.path.exists(self._mpv_stderr_path):
                        with open(self._mpv_stderr_path, "r", errors="replace") as ef:
                            err_content = ef.read().strip()
                        if err_content:
                            debug_log(f"  mpv stderr: {err_content[:400]}")
                except Exception:
                    pass
            self.proc = None
        if self.sock_path:
            try:
                if os.path.exists(self.sock_path):
                    os.unlink(self.sock_path)
            except Exception:
                pass
        self.sock_path = None
        with self._lock:
            self.time_pos = self.duration = None
            self.pause = self.volume = self.mute = None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _rpc(self, payload: Dict[str, Any], timeout: float = 0.10) -> Optional[Dict[str, Any]]:
        if not self.sock_path or not os.path.exists(self.sock_path):
            return None
        self._req_id += 1
        payload = dict(payload)
        payload["request_id"] = self._req_id
        msg = (json.dumps(payload) + "\n").encode("utf-8")
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(self.sock_path)
            s.sendall(msg)
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
            s.close()
            if not data:
                return None
            try:
                return json.loads(data.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                return None
        except Exception:
            return None

    def cmd(self, *args: Any) -> None:
        self._rpc({"command": list(args)})

    def get(self, prop: str) -> Optional[Any]:
        r = self._rpc({"command": ["get_property", prop]})
        if isinstance(r, dict) and r.get("error") == "success":
            return r.get("data")
        return None

    def poll_once(self) -> None:
        if not self.alive():
            with self._lock:
                self.time_pos = self.duration = None
                self.pause = self.volume = self.mute = None
            return
        tp = self.get("time-pos")
        du = self.get("duration")
        pa = self.get("pause")
        vo = self.get("volume")
        mu = self.get("mute")
        with self._lock:
            self.time_pos = tp if isinstance(tp, (int, float)) else None
            self.duration = du if isinstance(du, (int, float)) else None
            self.pause = bool(pa) if pa is not None else None
            try:
                self.volume = float(vo) if vo is not None else None
            except Exception:
                self.volume = None
            self.mute = bool(mu) if mu is not None else None

    def snapshot(self) -> Tuple[Optional[float], Optional[float], Optional[bool], Optional[float], Optional[bool]]:
        with self._lock:
            return (self.time_pos, self.duration, self.pause, self.volume, self.mute)


class MPVPoller:
    """Background poller so UI never blocks on IPC."""
    def __init__(self, mp: MPV, on_tick) -> None:
        self.mp = mp
        self.on_tick = on_tick
        self._stop = False
        self._prev_snapshot: tuple = (None, None, None, None, None)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True

    def _run(self) -> None:
        while not self._stop:
            self.mp.poll_once()
            snap = self.mp.snapshot()
            if snap != self._prev_snapshot:
                self._prev_snapshot = snap
                try:
                    self.on_tick()
                except Exception:
                    pass
            time.sleep(0.33)


class DownloadManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: List[Track] = []
        self._thread: Optional[threading.Thread] = None
        self.active = False
        self.progress_line = ""
        self.progress_clear_at = 0.0
        self.error: Optional[str] = None
        self._total = 0
        self._completed = 0

    def enqueue(self, tracks: List[Track], worker_fn) -> None:
        if not tracks:
            return
        with self._lock:
            if not self._queue and not self.active:
                self._total = len(tracks)
                self._completed = 0
            else:
                self._total += len(tracks)
            self._queue.extend(tracks)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, args=(worker_fn,), daemon=True)
                self._thread.start()

    def _run(self, worker_fn) -> None:
        self.active = True
        self.error = None
        while True:
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
