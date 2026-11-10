"""Download source adapters."""

from mixsync.core.config import Settings

# name -> (label, the settings that must be set); this order is the default source priority
DOWNLOAD_SOURCES = {
    "slskd": ("Soulseek (slskd)", ("slskd_url",)),
    "prowlarr": (
        "Torrents and Usenet (Prowlarr)",
        ("prowlarr_url", "prowlarr_api_key"),  # and a download client: see prowlarr_protocols
    ),
}


def prowlarr_protocols(settings: Settings) -> frozenset[str]:
    """The release protocols a configured client can download; Prowlarr offers only these."""
    torrent = settings.qbittorrent_url and settings.qbittorrent_dir
    usenet = settings.sabnzbd_url and settings.sabnzbd_api_key and settings.sabnzbd_dir
    return frozenset(p for p, ok in (("torrent", torrent), ("usenet", usenet)) if ok)
