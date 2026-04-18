"""Data models and utility functions for tuifi."""

from __future__ import annotations

import os
import platform
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from tuifi_pkg import APP_NAME


# ---------------------------------------------------------------------------
# Debug logging (set _DEBUG_LOG externally to enable)
# ---------------------------------------------------------------------------
_DEBUG_LOG: Optional[str] = None


def debug_log(msg: str) -> None:
    if _DEBUG_LOG:
        try:
            with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Config/state path resolution
# ---------------------------------------------------------------------------

def _resolve_config_dir() -> str:
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        return os.path.join(base, "tuifi")
    if system == "Darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "tuifi")
    return os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config")),
        "tuifi",
    )


def _default_downloads_dir() -> str:
    if platform.system() == "Windows" or os.path.exists("/data/data/com.termux"):
        return os.path.join(os.path.expanduser("~"), "Downloads", "tuifi")
    return "/tmp/tuifi/downloads"


STATE_DIR      = _resolve_config_dir()
QUEUE_FILE     = os.path.join(STATE_DIR, "queue.json")
LIKED_FILE     = os.path.join(STATE_DIR, "liked.json")
PLAYLISTS_FILE = os.path.join(STATE_DIR, "playlists.json")
HISTORY_FILE   = os.path.join(STATE_DIR, "history.json")
SETTINGS_FILE  = os.path.join(STATE_DIR, "settings.json")
DOWNLOADS_DIR  = _default_downloads_dir()


# ---------------------------------------------------------------------------
# Small utility functions
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Track:
    id: int
    title: str
    artist: str
    album: str
    year: str
    track_no: int
    duration: Optional[int] = None
    artist_id: Optional[int] = None
    album_id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "artist": self.artist,
            "album": self.album, "year": self.year, "track_no": self.track_no,
            "duration": self.duration, "artist_id": self.artist_id, "album_id": self.album_id,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Track":
        def opt_int(x) -> Optional[int]:
            try:
                return int(x) if x is not None else None
            except Exception:
                return None
        return Track(
            id=int(d.get("id", 0)),
            title=str(d.get("title", "") or ""),
            artist=str(d.get("artist", "") or ""),
            album=str(d.get("album", "") or ""),
            year=str(d.get("year", "") or "????"),
            track_no=int(d.get("track_no", 0) or 0),
            duration=opt_int(d.get("duration")),
            artist_id=opt_int(d.get("artist_id")),
            album_id=opt_int(d.get("album_id")),
        )


@dataclass
class Album:
    id: int
    title: str
    artist: str
    year: str
    track_id: Optional[int] = None


@dataclass
class Artist:
    id: int
    name: str
    track_id: Optional[int] = None


# ---------------------------------------------------------------------------
# Track serialisation helpers (Monochrome API format)
# ---------------------------------------------------------------------------

def track_to_mono(t: "Track", added_at: Optional[int] = None) -> Dict[str, Any]:
    year = t.year if t.year and t.year != "????" else ""
    release_date = f"{year}-01-01T00:00:00.000+0000" if year else None
    return {
        "id": t.id,
        "addedAt": added_at,
        "title": t.title,
        "duration": t.duration,
        "explicit": False,
        "artist": {"id": t.artist_id, "name": t.artist, "handle": None, "type": "MAIN", "picture": None},
        "artists": [{"id": t.artist_id, "name": t.artist}],
        "album": {"id": t.album_id, "title": t.album, "cover": None, "releaseDate": release_date,
                  "vibrantColor": "#FFFFFF", "artist": None, "numberOfTracks": None, "mediaMetadata": None},
        "copyright": None, "isrc": None, "trackNumber": t.track_no,
        "streamStartDate": release_date, "version": None, "mixes": {},
        "isTracker": False, "trackerInfo": None, "audioUrl": None, "remoteUrl": None,
        "audioQuality": "LOSSLESS", "mediaMetadata": {"tags": ["LOSSLESS"]},
    }


def mono_to_track(d: Dict[str, Any]) -> Optional["Track"]:
    if not isinstance(d, dict):
        return None
    try:
        tid = int(d.get("id", 0))
        if tid <= 0:
            return None
        artist_d = d.get("artist") or {}
        album_d  = d.get("album")  or {}
        artist_name = str(artist_d.get("name", "") or "") if isinstance(artist_d, dict) else str(artist_d)
        artist_id   = artist_d.get("id") if isinstance(artist_d, dict) else None
        album_title = str(album_d.get("title", "") or "") if isinstance(album_d, dict) else str(album_d)
        album_id    = album_d.get("id") if isinstance(album_d, dict) else None
        rd = (album_d.get("releaseDate") or "") if isinstance(album_d, dict) else ""
        year = rd[:4] if rd and len(rd) >= 4 else "????"
        def _int(x: Any) -> Optional[int]:
            try: return int(x) if x is not None else None
            except Exception: return None
        return Track(
            id=tid, title=str(d.get("title", "") or ""),
            artist=artist_name, album=album_title, year=year,
            track_no=int(d.get("trackNumber", 0) or 0),
            duration=_int(d.get("duration")),
            artist_id=_int(artist_id), album_id=_int(album_id),
        )
    except Exception:
        return None
