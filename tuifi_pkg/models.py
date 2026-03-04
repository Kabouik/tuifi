from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


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


def track_to_mono(t: Track, added_at: Optional[int] = None) -> Dict[str, Any]:
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


def mono_to_track(d: Dict[str, Any]) -> Optional[Track]:
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
