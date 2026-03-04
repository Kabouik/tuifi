from __future__ import annotations

import os
import re
import time
from typing import Any, Optional


APP_NAME = "tuifi"  # local ref to avoid circular import with config

# Global debug log path (set by --verbose flag in main)
_DEBUG_LOG: Optional[str] = None


def print_version(prog: str) -> None:
    from .config import VERSION
    print(f"tuifi v{VERSION}")


def debug_log(msg: str) -> None:
    if _DEBUG_LOG:
        try:
            with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        except Exception:
            pass


def mkdirp(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def clamp(n: int, lo: int, hi: int) -> int:
    return lo if n < lo else hi if n > hi else n


def safe_filename(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"[\/\\\0]+", "-", s)
    s = re.sub(r"[\:\*\?\"\<\>\|]+", "", s)
    s = re.sub(r"\s+", " ", s)
    return s or "_"


def year_norm(y: str) -> str:
    y = (y or "").strip()
    return y if (len(y) == 4 and y.isdigit()) else "????"


def album_year_from_obj(obj: Any) -> str:
    if not isinstance(obj, dict):
        return "????"
    for k in ("releaseDate", "streamStartDate", "date", "copyright"):
        v = obj.get(k)
        if isinstance(v, str):
            m = re.search(r"(19\d{2}|20\d{2})", v)
            if m:
                return m.group(1)
    for k in ("year", "releaseYear"):
        v = obj.get(k)
        if isinstance(v, (int, str)) and str(v).isdigit():
            return str(v)
    return "????"


def fmt_time(sec: Optional[float]) -> str:
    if sec is None:
        return "--:--"
    try:
        sec = float(sec)
    except Exception:
        return "--:--"
    sec = max(0, sec)
    return f"{int(sec)//60:02d}:{int(sec)%60:02d}"


def fmt_dur(sec: Optional[int]) -> str:
    if sec is None or sec <= 0:
        return ""
    return f"{sec//60}:{sec%60:02d}"
