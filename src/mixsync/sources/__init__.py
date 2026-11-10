"""Download source adapters."""

# name -> (label, the settings that must be set); this order is the default source priority
DOWNLOAD_SOURCES = {
    "slskd": ("Soulseek (slskd)", ("slskd_url",)),
    "prowlarr": (
        "Torrents (Prowlarr + qBittorrent)",
        ("prowlarr_url", "prowlarr_api_key", "qbittorrent_url", "qbittorrent_dir"),
    ),
}
