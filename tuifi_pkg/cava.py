"""CavaReader: spawns cava with a binary FIFO and exposes normalised bar values."""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
import threading
from typing import Any, Dict, List, Optional


_CAVA_CFG_TEMPLATE = """\
[general]
bars = {bars}
framerate = {framerate}

[input]
method = {method}
{source_line}
[output]
method = raw
raw_target = {fifo}
data_format = binary
bit_format = 16bit

[smoothing]
noise_reduction = 77
"""


class CavaReader:
    """Spawns cava as a subprocess, reads bar values from a binary FIFO.

    Uses a named pipe (FIFO) with 16-bit binary output instead of stdout/ASCII,
    which avoids line-buffering issues and gives fixed-size chunk reads.

    Settings keys (from app settings dict):
        spectrum_method – cava input method (default: "pulse")
        spectrum_source – cava input source/device (default: "" = cava default)
        cava_framerate  – output framerate (default: 30)
    """

    _FRAME_MAX = 65535  # 16-bit unsigned max

    def __init__(self, settings: Dict[str, Any]) -> None:
        self._settings = settings
        self._bars: int = 0
        self._lock = threading.Lock()
        self._values: List[float] = []
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._fifo_path: str = os.path.join(tempfile.gettempdir(), "tuifi_cava.fifo")
        self._cfg_path:  str = os.path.join(tempfile.gettempdir(), "tuifi_cava.cfg")
        self.running: bool = False

    # ------------------------------------------------------------------

    @staticmethod
    def available() -> bool:
        return shutil.which("cava") is not None

    def start(self, bars: int) -> bool:
        """Start (or restart) cava with the given bar count. Returns False if cava not found."""
        self.stop()
        if not self.available():
            return False
        self._bars = bars
        with self._lock:
            self._values = [0.0] * bars

        # Create FIFO if needed
        if not os.path.exists(self._fifo_path):
            try:
                os.mkfifo(self._fifo_path)
            except OSError:
                return False

        # Write config
        framerate = int(self._settings.get("cava_framerate", 30))
        method = str(self._settings.get("spectrum_method", "pulse"))
        source = str(self._settings.get("spectrum_source", "") or "")
        source_line = f"source = {source}" if source else ""
        try:
            with open(self._cfg_path, "w", encoding="utf-8") as f:
                f.write(_CAVA_CFG_TEMPLATE.format(
                    bars=bars, framerate=framerate,
                    method=method, source_line=source_line, fifo=self._fifo_path,
                ))
        except OSError:
            return False

        try:
            self._proc = subprocess.Popen(
                ["cava", "-p", self._cfg_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return False

        self.running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self.running = False
        if self._proc:
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None
        # Clean up FIFO so next start() creates a fresh one
        try:
            os.unlink(self._fifo_path)
        except OSError:
            pass

    def get_values(self) -> List[float]:
        """Thread-safe snapshot of current bar values (0.0–1.0)."""
        with self._lock:
            return list(self._values)

    # ------------------------------------------------------------------

    def _read_loop(self) -> None:
        chunk_size = self._bars * 2  # 16-bit = 2 bytes per bar
        fmt = f"<{self._bars}H"       # little-endian unsigned shorts
        try:
            # Open FIFO — blocks until cava connects to the write end
            with open(self._fifo_path, "rb") as fifo:
                while self.running:
                    chunk = fifo.read(chunk_size)
                    if len(chunk) < chunk_size:
                        break
                    vals = struct.unpack(fmt, chunk)
                    normalised = [v / self._FRAME_MAX for v in vals]
                    with self._lock:
                        self._values = normalised
        except Exception:
            pass
        self.running = False
