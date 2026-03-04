from __future__ import annotations
from .core import CoreMixin
from .ui import UIMixin
from .playback import PlaybackMixin
from .library import LibraryMixin
from .overlays import OverlaysMixin
from .dialogs import DialogsMixin
from .navigation import NavigationMixin


class App(CoreMixin, UIMixin, PlaybackMixin, LibraryMixin, OverlaysMixin, DialogsMixin, NavigationMixin):
    pass
