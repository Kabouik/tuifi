"""HiFiClient and HTTP helper functions."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from tuifi_pkg import APP_NAME


def http_get_bytes(url: str, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_get_json(url: str, timeout: float = 12.0) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    raw = data.decode("utf-8", "replace")
    if not raw.strip():
        raise RuntimeError("Empty HTTP response")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        hint = raw.strip()[:120].replace("\n", "\\n")
        raise RuntimeError(f"Non-JSON HTTP response: {e} ; head={hint!r}") from e


def http_stream_download(url: str, dst_path: str, progress_cb, timeout: float = 120.0) -> None:
    import os
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        total = None
        try:
            cl = r.headers.get("Content-Length")
            if cl and cl.isdigit():
                total = int(cl)
        except Exception:
            total = None
        downloaded = 0
        tmp = dst_path + ".part"
        with open(tmp, "wb") as f:
            while True:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                progress_cb(downloaded, total)
        os.replace(tmp, dst_path)


class HiFiClient:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def _u(self, path: str, q: Dict[str, Any]) -> str:
        return f"{self.base}{path}?{urllib.parse.urlencode(q)}"

    def search_tracks(self, q: str, limit: int = 160) -> Dict[str, Any]:
        return http_get_json(self._u("/search/", {"s": q, "limit": limit}))

    def recommendations(self, track_id: int, limit: int = 50) -> Dict[str, Any]:
        return http_get_json(self._u("/recommendations/", {"id": int(track_id), "limit": limit}))

    def info(self, track_id: int) -> Dict[str, Any]:
        return http_get_json(self._u("/info/", {"id": int(track_id)}))

    def track(self, track_id: int, quality: str) -> Dict[str, Any]:
        return http_get_json(self._u("/track/", {"id": int(track_id), "quality": quality}))

    def track_manifests(self, track_id: int, formats: list, usage: str = "PLAYBACK") -> Dict[str, Any]:
        from urllib.parse import urlencode
        params = [("id", str(track_id)), ("usage", usage), ("manifestType", "MPEG_DASH"),
                  ("uriScheme", "HTTPS"), ("adaptive", "false")]
        for fmt in formats:
            params.append(("formats", fmt))
        return http_get_json(f"{self.base}/trackManifests/?{urlencode(params)}")

    def lyrics(self, track_id: int) -> Dict[str, Any]:
        return http_get_json(self._u("/lyrics/", {"id": int(track_id)}))

    def album(self, album_id: int, limit: Optional[int] = None) -> Dict[str, Any]:
        q: Dict[str, Any] = {"id": int(album_id)}
        if limit is not None:
            q["limit"] = int(limit)
        return http_get_json(self._u("/album/", q))

    def artist(self, artist_id: int, skip_tracks: bool = False) -> Dict[str, Any]:
        try:
            params: Dict[str, Any] = {"f": int(artist_id)}
            if skip_tracks:
                params["skip_tracks"] = True
            return http_get_json(self._u("/artist/", params))
        except Exception:
            return http_get_json(self._u("/artist/", {"id": int(artist_id)}))

    def artist_similar(self, artist_id: int) -> Dict[str, Any]:
        return http_get_json(self._u("/artist/similar/", {"id": int(artist_id)}))

    def mix(self, mix_id: str) -> Dict[str, Any]:
        return http_get_json(self._u("/mix/", {"id": mix_id}))
