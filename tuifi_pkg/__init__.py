"""tuifi package — a TUI music player for TIDAL HiFi API."""

APP_NAME = "tuifi"
VERSION = "2.3.15"
DEFAULT_API = ""

TAB_SEARCH      = 1
TAB_QUEUE       = 2
TAB_RECOMMENDED = 3
TAB_MIX         = 4
TAB_ARTIST      = 5
TAB_ALBUM       = 6
TAB_LIKED       = 7
TAB_PLAYLISTS   = 8
TAB_HISTORY     = 9
TAB_PLAYBACK    = 10

TAB_NAMES = {
    TAB_SEARCH:      "Search\u00b9",
    TAB_QUEUE:       "Queue\u00b2",
    TAB_RECOMMENDED: "Recommended\u00b3",
    TAB_MIX:         "Mix\u2074",
    TAB_ARTIST:      "Artist\u2075",
    TAB_ALBUM:       "Album\u2076",
    TAB_LIKED:       "Liked\u2077",
    TAB_PLAYLISTS:   "Playlists\u2078",
    TAB_HISTORY:     "History\u2079",
    TAB_PLAYBACK:    "Playback\u2070",
}

TAB_SHORT_NAMES = {
    TAB_SEARCH:      "Src\u00b9",
    TAB_QUEUE:       "Que\u00b2",
    TAB_RECOMMENDED: "Rec\u00b3",
    TAB_MIX:         "Mix\u2074",
    TAB_ARTIST:      "Art\u2075",
    TAB_ALBUM:       "Alb\u2076",
    TAB_LIKED:       "Lkd\u2077",
    TAB_PLAYLISTS:   "Pls\u2078",
    TAB_HISTORY:     "Hist\u2079",
    TAB_PLAYBACK:    "Plb\u2070",
}

# Autoplay modes
AUTOPLAY_OFF         = 0
AUTOPLAY_MIX         = 1
AUTOPLAY_RECOMMENDED = 2
AUTOPLAY_NAMES       = ["off", "mix", "recommended"]

QUALITY_ORDER = ["HI_RES_LOSSLESS", "LOSSLESS", "HIGH", "LOW"]

LIKED_FILTER_NAMES = ["All", "Tracks", "Artists", "Albums", "Playlists"]
