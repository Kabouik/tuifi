from __future__ import annotations

import os


APP_NAME = "tuifi"
VERSION = "1.1.0"
DEFAULT_API = "https://api.monochrome.tf"  # See https://github.com/monochrome-music/monochrome/blob/main/INSTANCES.md#official--community-apis


def _resolve_config_dir() -> str:
    import platform
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        return os.path.join(base, "tuifi")
    if system == "Darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "tuifi")
    # Linux, Android/Termux, and other Unix-likes
    return os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config")),
        "tuifi",
    )


def _default_downloads_dir() -> str:
    import platform
    if platform.system() == "Windows" or os.path.exists("/data/data/com.termux"):
        return os.path.join(os.path.expanduser("~"), "Downloads", "tuifi")
    return "/tmp/tuifi"


STATE_DIR      = _resolve_config_dir()
QUEUE_FILE     = os.path.join(STATE_DIR, "queue.json")
LIKED_FILE     = os.path.join(STATE_DIR, "liked.json")
PLAYLISTS_FILE = os.path.join(STATE_DIR, "playlists.json")
HISTORY_FILE   = os.path.join(STATE_DIR, "history.json")
SETTINGS_FILE  = os.path.join(STATE_DIR, "settings.json")
DOWNLOADS_DIR  = _default_downloads_dir()

QUALITY_ORDER = ["HI_RES_LOSSLESS", "LOSSLESS", "HIGH", "LOW"]

TAB_QUEUE       = 1
TAB_SEARCH      = 2
TAB_RECOMMENDED = 3
TAB_MIX         = 4
TAB_ARTIST      = 5
TAB_ALBUM       = 6
TAB_LIKED       = 7
TAB_PLAYLISTS   = 8
TAB_HISTORY     = 9

TAB_NAMES = {
    TAB_QUEUE:       "1 Queue",
    TAB_SEARCH:      "2 Search",
    TAB_RECOMMENDED: "3 Recommended",
    TAB_MIX:         "4 Mix",
    TAB_ARTIST:      "5 Artist",
    TAB_ALBUM:       "6 Album",
    TAB_LIKED:       "7 Liked",
    TAB_PLAYLISTS:   "8 Playlists",
    TAB_HISTORY:     "9 History",
}

# Autoplay modes
AUTOPLAY_OFF         = 0
AUTOPLAY_MIX         = 1
AUTOPLAY_RECOMMENDED = 2
AUTOPLAY_NAMES       = ["off", "mix", "recommended"]
