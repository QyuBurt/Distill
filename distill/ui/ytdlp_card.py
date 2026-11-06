"""Settings > Downloads: the quality yt-dlp downloads video and audio sites at. (yt-dlp and ffmpeg themselves are
downloaded and updated automatically: Settings > Tools > Download Tools.)"""
from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QWidget

from .. import ytdlp
from .state import LABEL_W
from .widgets import Card, hint


class YtdlpCard(Card):
    def __init__(self, parent: QWidget | None = None):
        super().__init__("Video and Audio Sites (yt-dlp)", parent)
        self.add(hint("Paste a YouTube, Vimeo, SoundCloud, Bandcamp or Twitch link (or yt:<any link>) into the Download "
                      "page and Distill hands it to yt-dlp. Playlists become one row per video."))
        row = QHBoxLayout()
        label = QLabel("Quality")
        label.setFixedWidth(LABEL_W)
        self.preset = QComboBox()
        self.preset.addItems(list(ytdlp.PRESETS))
        self.preset.setCurrentText(ytdlp.SETTINGS.preset)
        self.preset.currentTextChanged.connect(self._preset_changed)
        row.addWidget(label)
        row.addWidget(self.preset)
        row.addStretch(1)
        self.add(row)
        self.add(hint("yt-dlp and ffmpeg (needed to merge video and audio, and for MP3) are downloaded and kept up to "
                      "date for you under Tools > Download Tools. The dots at the bottom right show their state."))

    def _preset_changed(self, text: str) -> None:
        ytdlp.SETTINGS.preset = text
        ytdlp.save_settings()
