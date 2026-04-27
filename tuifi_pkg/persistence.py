"""Persistence: load/save JSON state files."""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from tuifi_pkg import QUALITY_ORDER, TAB_QUEUE, AUTOPLAY_OFF
from tuifi_pkg.models import (
    Track, Album, Artist,
    STATE_DIR, QUEUE_FILE, LIKED_FILE, PLAYLISTS_FILE, HISTORY_FILE, SETTINGS_FILE,
    clamp, track_to_mono, mono_to_track, _default_downloads_dir,
)


# ---------------------------------------------------------------------------
# Low-level JSON helpers
# ---------------------------------------------------------------------------

def load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, obj: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

def load_queue() -> Tuple[List[Track], int]:
    data = load_json(QUEUE_FILE, {})
    if isinstance(data, dict) and data.get("version") == 2 and isinstance(data.get("items"), list):
        items: List[Track] = []
        for it in data["items"]:
            if isinstance(it, dict):
                try:
                    t = Track.from_dict(it)
                    if t.id > 0:
                        items.append(t)
                except Exception:
                    pass
        play_idx = int(data.get("idx", 0) or 0)
        play_idx = clamp(play_idx, 0, max(0, len(items) - 1))
        return items, play_idx
    return [], 0


def save_queue(items: List[Track], play_idx: int) -> None:
    play_idx = clamp(play_idx, 0, max(0, len(items) - 1))
    save_json(QUEUE_FILE, {"version": 2, "idx": play_idx, "items": [t.to_dict() for t in items]})


# ---------------------------------------------------------------------------
# Liked
# ---------------------------------------------------------------------------

def load_liked() -> Tuple[List[Dict[str, Any]], set, List[Dict[str, Any]], set, List[Dict[str, Any]], set, List[Dict[str, Any]], set]:
    """Load liked.json. Returns (tracks, track_ids, albums, album_ids, artists, artist_ids, playlists, playlist_names).
    Migrates from old likes.json if liked.json doesn't exist yet."""
    data = load_json(LIKED_FILE, None)
    if data is None:
        old = load_json(os.path.join(STATE_DIR, "likes.json"), None)
        if isinstance(old, dict):
            old_ids = [int(x) for x in old.get("tracks", []) if str(x).isdigit()]
            queue_items, _ = load_queue()
            by_id = {t.id: t for t in queue_items}
            now_ms = int(time.time() * 1000)
            tracks = [track_to_mono(by_id[tid], now_ms) for tid in old_ids if tid in by_id]
            save_json(LIKED_FILE, {"favorites_tracks": tracks})
            return tracks, {t["id"] for t in tracks}, [], set(), [], set(), [], set()
        return [], set(), [], set(), [], set(), [], set()
    tracks = [d for d in data.get("favorites_tracks", []) if isinstance(d, dict) and d.get("id")]
    albums = [d for d in data.get("favorites_albums", []) if isinstance(d, dict) and d.get("id")]
    artists = [d for d in data.get("favorites_artists", []) if isinstance(d, dict) and d.get("id")]
    playlists = [d for d in data.get("tuifi_liked_playlists", data.get("favorites_playlists", [])) if isinstance(d, dict) and d.get("name")]
    return (
        tracks, {int(d["id"]) for d in tracks},
        albums, {int(d["id"]) for d in albums},
        artists, {int(d["id"]) for d in artists},
        playlists, {d["name"] for d in playlists},
    )


def save_liked(
    tracks: List[Dict[str, Any]],
    albums: List[Dict[str, Any]],
    artists: List[Dict[str, Any]],
    playlists: List[Dict[str, Any]],
) -> None:
    save_json(LIKED_FILE, {
        "favorites_tracks": tracks,
        "favorites_albums": albums,
        "favorites_artists": artists,
        "tuifi_liked_playlists": playlists,
    })


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------

def load_playlists() -> Tuple[Dict[str, List[Any]], Dict[str, Dict[str, Any]]]:
    """Load playlists.json. Returns (name→List[Track], name→metadata dict)."""
    data = load_json(PLAYLISTS_FILE, None)
    playlists: Dict[str, List[Any]] = {}
    meta: Dict[str, Dict[str, Any]] = {}
    if data is None:
        old = load_json(PLAYLISTS_FILE, {})
        if isinstance(old, dict) and old:
            first = next(iter(old.values()), None)
            if isinstance(first, list) and (not first or isinstance(first[0], int)):
                return {}, {}
        return {}, {}
    now_ms = int(time.time() * 1000)
    for pl in data.get("user_playlists", []):
        if not isinstance(pl, dict):
            continue
        name = str(pl.get("name", "") or "")
        if not name:
            continue
        tracks = [t for t in (mono_to_track(d) for d in pl.get("tracks", [])) if t is not None]
        playlists[name] = tracks
        meta[name] = {
            "id": str(pl.get("id") or uuid.uuid4()),
            "createdAt": int(pl.get("createdAt") or now_ms),
        }
    return playlists, meta


def save_playlists(playlists: Dict[str, List[Any]], meta: Dict[str, Dict[str, Any]]) -> None:
    now_ms = int(time.time() * 1000)
    user_playlists = []
    for name, tracks in playlists.items():
        m = meta.get(name, {})
        user_playlists.append({
            "id": m.get("id") or str(uuid.uuid4()),
            "name": name,
            "tracks": [track_to_mono(t) for t in tracks],
            "numberOfTracks": len(tracks),
            "createdAt": m.get("createdAt") or now_ms,
            "updatedAt": now_ms,
        })
    save_json(PLAYLISTS_FILE, {"user_playlists": user_playlists})


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def load_history() -> List[Any]:
    data = load_json(HISTORY_FILE, {})
    return [t for t in (mono_to_track(d) for d in data.get("history_tracks", [])) if t is not None]


def save_history(tracks: List[Any]) -> None:
    save_json(HISTORY_FILE, {"history_tracks": [track_to_mono(t) for t in tracks]})


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _load_jsonc(path: str, default: Any) -> Any:
    """Load a JSONC file (JSON with // line comments, including inline ones)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        # Walk character by character, respecting string literals, and strip
        # // comments (both full-line and inline).
        buf: List[str] = []
        i = 0
        n = len(raw)
        while i < n:
            c = raw[i]
            if c == '"':                        # enter string literal
                buf.append(c)
                i += 1
                while i < n:
                    sc = raw[i]
                    buf.append(sc)
                    if sc == '\\' and i + 1 < n:
                        i += 1
                        buf.append(raw[i])
                    elif sc == '"':
                        break
                    i += 1
                i += 1
            elif c == '/' and i + 1 < n and raw[i + 1] == '/':
                while i < n and raw[i] != '\n':  # skip to end of line
                    i += 1
            else:
                buf.append(c)
                i += 1
        return json.loads("".join(buf))
    except Exception:
        return default


def _migrate_settings_json() -> None:
    """One-shot migration: if settings.json exists but settings.jsonc does not,
    read the old file, write the new one (via save_settings), then delete it."""
    if os.path.exists(SETTINGS_FILE):
        return  # already on the new format
    old_path = SETTINGS_FILE[:-1]  # strip trailing 'c' → settings.json
    if not os.path.exists(old_path):
        return
    try:
        raw = load_json(old_path, {})
        if not isinstance(raw, dict):
            raw = {}
        save_settings(raw)          # writes settings.jsonc with section headers
        os.remove(old_path)
    except Exception:
        pass                        # leave old file in place if anything fails


def _detect_sideview_default() -> str:
    """Return the best sideview default based on available system tools."""
    has_cava     = shutil.which("cava") is not None
    has_graphics = (shutil.which("ueberzugpp") is not None
                    or shutil.which("chafa") is not None)
    if has_cava and has_graphics:
        return "both"
    if has_cava:
        return "spectrum"
    if has_graphics:
        return "cover"
    return "off"


def load_settings() -> Dict[str, Any]:
    _migrate_settings_json()
    s = _load_jsonc(SETTINGS_FILE, {})
    if not isinstance(s, dict):
        s = {}

    # -------------------------------------------------------------------------
    # Runtime state — written automatically by the app on exit / during use.
    # These keys are valid in settings.json but are not intended for manual
    # editing; their values are overwritten every session.
    # -------------------------------------------------------------------------
    s.setdefault("_resume_position", 0.0)       # playback position (seconds) at last exit
    s.setdefault("_resume_queue_idx", 0)        # queue index at last exit
    s.setdefault("initial_tab", TAB_QUEUE)      # last active tab (restored on startup)
    s.setdefault("initial_filter", "")          # last active filter string (restored on startup)

    # -------------------------------------------------------------------------
    # User-editable settings — safe to change in settings.json.
    # -------------------------------------------------------------------------

    # -- API --
    s.setdefault("api", "")                     # API base URL; overrides the compiled-in default

    # -- Audio --
    s.setdefault("volume", 100)
    s.setdefault("mute", False)
    s.setdefault("quality", QUALITY_ORDER[0])   # e.g. "lossless", "high", "low"

    # -- Playback / autoextend --
    s.setdefault("autoextend", AUTOPLAY_OFF)    # off/mix/recommended
    s.setdefault("autoextend_n", 3)             # tracks added per autoextend refill
    s.setdefault("auto_resume_playback", True)  # resume last position on startup

    # -- UI chrome --
    s.setdefault("color_mode", True)
    s.setdefault("queue_overlay", False)
    s.setdefault("show_toggles", True)
    s.setdefault("show_track_album", True)
    s.setdefault("show_track_year", True)
    s.setdefault("show_track_duration", True)
    s.setdefault("show_line_numbers", False)
    s.setdefault("show_album_track_count", True)
    s.setdefault("tab_align", True)
    s.setdefault("sideview", _detect_sideview_default())  # cover|both|spectrum|off; auto-detected on first run

    # -- History --
    s.setdefault("history_max", 0)              # 0 = unlimited

    # -- Artist tab --
    s.setdefault("include_singles_and_eps_in_artist_tab", False)  # toggle with #
    s.setdefault("max_all_tracks_number", 0)                      # 0 = unlimited

    # -- Miscellaneous user preferences --
    s.setdefault("remember_last_input", False)     # prefill search/filter with last query within a session
    s.setdefault("playback_tab_layout", "lyrics")  # right-pane layout: "lyrics", "cover", etc.
    s.setdefault("cover_lyrics_color_pair", "default")  # color name; "default" = terminal default

    # -- Confirmation prompts when switching to a tab that already has content --
    # Set to true to skip the "press again to confirm" step and fetch immediately.
    s.setdefault("recommended_tab_no_confirm_refetch", False)
    s.setdefault("mix_tab_no_confirm_refetch", False)
    s.setdefault("artist_tab_no_confirm_refetch", False)
    s.setdefault("album_tab_no_confirm_refetch", False)

    # -- Colors (name or 0-255 terminal color index) --
    s.setdefault("color_playing",   "green")
    s.setdefault("color_paused",    "yellow")
    s.setdefault("color_error",     "red")
    s.setdefault("color_chrome",    "blue")
    s.setdefault("color_accent",    "magenta")
    s.setdefault("color_artist",    "white")
    s.setdefault("color_album",     "blue")
    s.setdefault("color_year",      "blue")
    s.setdefault("color_duration",  "blue")
    s.setdefault("color_line_numbers",        "blue")
    s.setdefault("color_album_track_count",   "white")
    s.setdefault("color_title",     "white")
    s.setdefault("color_separator", "white")
    s.setdefault("color_liked",     "white")
    s.setdefault("color_mark",      "red")
    s.setdefault("color_spectrum",  "")     # empty = inherit color_chrome

    # -- Spectrum / cava --
    s.setdefault("spectrum_method", "pulse")
    s.setdefault("spectrum_source", "")     # empty = cava default

    # -- Downloads --
    s.setdefault("download_dir", _default_downloads_dir())
    s.setdefault("download_structure", "{artist}/{artist} - {album} ({year})")
    s.setdefault("download_filename", "{track:02d}. {artist} - {title}")

    # -- TSV display widths (0 = unlimited) --
    s.setdefault("tsv_max_col_width", 32)
    s.setdefault("tsv_max_artist_width", 25)
    s.setdefault("tsv_max_title_width", 0)
    s.setdefault("tsv_max_album_width", 0)
    s.setdefault("tsv_max_year_width", 6)
    s.setdefault("tsv_max_duration_width", 6)

    return s


def save_settings(s: Dict[str, Any]) -> None:
    """Write settings.jsonc with real // section headers and inline comments.

    The output is valid JSONC (understood by VS Code, Neovim + json5 plugin,
    etc.) so editors render comment lines differently from string values.
    _load_jsonc() strips all // comments before parsing on the way back in.
    """
    # Inline hint shown after the value for settings with discrete options.
    _hints: Dict[str, str] = {
        "quality":             "HI_RES_LOSSLESS | LOSSLESS | HIGH | LOW",
        "autoextend":          "off | mix | recommended",
        "playback_tab_layout": "lyrics | miniqueue | miniqueue_cover",
        "download_structure":  "placeholders: {artist} {album} {year}",
        "download_filename":   "placeholders: {track:02d} {artist} {title} {album} {year}",
        "tsv_max_col_width":   "fallback max width when a column has no specific limit; 0=unlimited",
        "tsv_max_artist_width":"0=unlimited",
        "tsv_max_title_width": "0=unlimited",
        "tsv_max_album_width": "0=unlimited",
        "tsv_max_year_width":  "0=unlimited",
        "tsv_max_duration_width": "0=unlimited",
        "history_max":         "0=unlimited",
        "max_all_tracks_number": "0=unlimited",
        "cover_lyrics_color_pair": "color name for lyrics panel text; \"default\" = terminal default",
        "color_spectrum":  "spectrum bar color; empty = inherit color_chrome; click spectrum to cycle",
        "spectrum_method": "cava input method: pulse | pipewire | alsa | fifo | ...",
        "spectrum_source": "cava input source (device/path); empty = cava default",
        "initial_tab":         "last active tab, restored on startup",
    }

    # Each entry: ("comment", text) | ("kv", key, value, hint_or_None)
    flat: List[Any] = []

    def _section(label: str) -> None:
        flat.append(("comment", label))

    def _key(k: str, hint: str = "") -> None:
        if k in s:
            flat.append(("kv", k, s[k], hint or _hints.get(k, "")))

    def _keys(*keys: str) -> None:
        for k in keys:
            _key(k)

    _section("── USER SETTINGS  (edit freely in this file) " + "─" * 24)
    _keys(
        "api",
        "auto_resume_playback",
        "remember_last_input",
        "history_max",
        "playback_tab_layout",
        "max_all_tracks_number",
        "recommended_tab_no_confirm_refetch",
        "mix_tab_no_confirm_refetch",
        "artist_tab_no_confirm_refetch",
        "album_tab_no_confirm_refetch",
    )

    _section("── COLORS  (color name or 0-255 terminal index) " + "─" * 20)
    _keys(
        "color_playing", "color_paused", "color_error", "color_chrome",
        "color_accent", "color_artist", "color_album", "color_year",
        "color_duration", "color_line_numbers", "color_album_track_count", "color_title",
        "color_separator", "color_liked", "color_mark",
        "cover_lyrics_color_pair",
        "color_spectrum",
    )

    _section("── SPECTRUM  (cava spectrum pane settings) " + "─" * 26)
    _keys("spectrum_method", "spectrum_source")

    _section("── DOWNLOADS " + "─" * 56)
    _keys("download_dir", "download_structure", "download_filename")

    _section("── TRACK-LIST COLUMN WIDTHS  (0 = unlimited) " + "─" * 23)
    _keys(
        "tsv_max_col_width", "tsv_max_artist_width", "tsv_max_title_width",
        "tsv_max_album_width", "tsv_max_year_width", "tsv_max_duration_width",
    )

    _section("── IN-APP TOGGLES  (changed via UI keys, edit to set defaults) " + "─" * 6)
    _keys(
        "volume", "mute", "quality",
        "autoextend", "autoextend_n",
        "color_mode", "queue_overlay",
        "show_toggles", "show_track_album", "show_track_year",
        "show_track_duration", "show_line_numbers", "show_album_track_count", "tab_align",
        "include_singles_and_eps_in_artist_tab",
        "playback_tab_preview_next",
        "cover_pane",
    )

    _section("── RUNTIME STATE  (managed automatically, do not edit) " + "─" * 14)
    _keys("_resume_position", "_resume_queue_idx", "initial_tab", "initial_filter")

    # Forward-compat: any unknown keys from future versions or manual additions
    # (also drops legacy // string keys written by the previous format)
    known = {item[1] for item in flat if item[0] == "kv"}
    extras = [(k, v) for k, v in s.items() if k not in known and not k.startswith("//")]
    if extras:
        _section("── OTHER " + "─" * 60)
        for k, v in extras:
            flat.append(("kv", k, v, ""))

    # Serialise to JSONC
    kv_items = [x for x in flat if x[0] == "kv"]
    last_key = kv_items[-1][1] if kv_items else None

    # Align inline comments: find the longest "key": value line in each section
    # then pad to that width + 2 spaces before the //.
    buf = ["{\n"]
    for item in flat:
        if item[0] == "comment":
            buf.append(f"\n  // {item[1]}\n")
        else:
            _, k, v, hint = item
            comma = "" if k == last_key else ","
            val_str = f"  {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}{comma}"
            if hint:
                buf.append(f"{val_str}  // {hint}\n")
            else:
                buf.append(f"{val_str}\n")
    buf.append("}\n")

    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("".join(buf))
    os.replace(tmp, SETTINGS_FILE)
