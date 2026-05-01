"""MPV IPC wrapper and background poller."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from tuifi_pkg import APP_NAME
from tuifi_pkg.models import debug_log


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
        self.demuxer_cache_duration: Optional[float] = None
        self.playlist_pos: Optional[int] = None    # mpv internal playlist position (0-based)
        self.playlist_count: Optional[int] = None  # number of entries in mpv playlist
        self._mpv_stderr_path: Optional[str] = None
        self._mpv_stderr_fh = None

    def start(self, url: str, resume: bool = False, start_pos: float = 0.0,
              gapless: bool = False) -> None:
        """Start mpv on *url*, killing any existing process first.

        When *gapless* is True the process is kept alive between tracks
        (``--idle=yes``) and audio is decoded gaplessly (``--gapless-audio=weak``).
        Additional tracks are queued via :meth:`preload` while this process
        lives.  The process is still killed on the *next* :meth:`start` call
        (e.g. a user-initiated skip), so normal playback control is unchanged.
        """
        self.stop()
        _tmp = os.environ.get("TMPDIR", "/tmp")
        _clutter = os.path.join(_tmp, APP_NAME, "clutter")
        try:
            os.makedirs(_clutter, exist_ok=True)
        except Exception:
            _clutter = _tmp
        self.sock_path = os.path.join(_clutter, f"mpv-{os.getpid()}-{int(time.time()*1000)}.sock")
        try:
            if os.path.exists(self.sock_path):
                os.unlink(self.sock_path)
        except Exception:
            pass
        args = [
            "mpv", "--no-video", "--force-window=no", "--really-quiet",
            "--idle=yes" if gapless else "--idle=no",
            f"--input-ipc-server={self.sock_path}",
            "--reset-on-next-file=no",
        ]
        if gapless:
            # --gapless-audio=weak: decoder handoff without gap; falls back to a tiny
            # gap only when codec/samplerate differs between adjacent tracks.
            # --prefetch-playlist=yes: mpv proactively downloads the next playlist entry
            # while the current track plays — essential for zero-gap DASH streaming.
            # NOTE: --demuxer-lavf-o and --ytdl=no are intentionally NOT set globally:
            # they break remote HTTPS DASH manifest URLs by restricting lavf's format
            # probing (allowed_extensions check) and disabling yt-dlp fallbacks.  They
            # are only added for local .mpd files (see below).
            args += [
                "--gapless-audio=weak",
                "--prefetch-playlist=yes",
            ]
        if not resume:
            args.append("--no-resume-playback")
        if start_pos > 0.0:
            args.append(f"--start={start_pos:.1f}")

        is_mpd = isinstance(url, str) and url.endswith(".mpd") and os.path.isfile(url)
        if is_mpd:
            args += [
                "--demuxer-lavf-o=allowed_extensions=MPD,m4s,mp4,aac,flac,mp3,frag",
                "--ytdl=no",
                "--cache=no",
            ]

        args.append(url)

        from tuifi_pkg.models import _DEBUG_LOG
        if _DEBUG_LOG:
            self._mpv_stderr_path = os.path.join(_clutter, f"mpv-err-{os.getpid()}.log")
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
            from tuifi_pkg.models import _DEBUG_LOG
            if _DEBUG_LOG and self._mpv_stderr_fh:
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
            self.demuxer_cache_duration = None
            self.playlist_pos = None
            self.playlist_count = None

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

    def replace(self, url: str) -> None:
        """Replace the current file via IPC without restarting the process.

        Only valid when mpv is alive (gapless mode).  Clears any preloaded
        playlist entries, then issues ``loadfile url replace`` so mpv switches
        immediately.  Resets the cached time_pos so callers can wait for a
        fresh value from the new file.
        """
        self.playlist_clear()
        self.cmd("loadfile", url, "replace")
        with self._lock:
            self.time_pos = None  # invalidate so probe loop waits for fresh data

    def preload(self, url: str) -> None:
        """Append *url* to mpv's internal playlist for gapless continuation.

        mpv will start playing it automatically when the current file ends.
        Only meaningful when the process was started with ``gapless=True``.
        """
        self.cmd("loadfile", url, "append-play")
        debug_log(f"mpv preload: loadfile append-play {url[:80]!r}")

    def playlist_clear(self) -> None:
        """Remove all playlist entries after the currently playing file."""
        self.cmd("playlist-clear")
        debug_log("mpv playlist_clear")

    def poll_once(self) -> None:
        if not self.alive():
            with self._lock:
                self.time_pos = self.duration = None
                self.pause = self.volume = self.mute = None
                self.demuxer_cache_duration = None
                self.playlist_pos = None
                self.playlist_count = None
            return
        tp = self.get("time-pos")
        du = self.get("duration")
        pa = self.get("pause")
        vo = self.get("volume")
        mu = self.get("mute")
        cd = self.get("demuxer-cache-duration")
        pp = self.get("playlist-pos")
        pc = self.get("playlist-count")
        with self._lock:
            self.time_pos = tp if isinstance(tp, (int, float)) else None
            self.duration = du if isinstance(du, (int, float)) else None
            self.pause = bool(pa) if pa is not None else None
            try:
                self.volume = float(vo) if vo is not None else None
            except Exception:
                self.volume = None
            self.mute = bool(mu) if mu is not None else None
            self.demuxer_cache_duration = cd if isinstance(cd, (int, float)) else None
            self.playlist_pos = int(pp) if isinstance(pp, int) else None
            self.playlist_count = int(pc) if isinstance(pc, int) else None

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
