from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MIXSYNC_")

    database_url: str = "sqlite:////config/mixsync.db"
    config_dir: str = "/config"
    secret_key: str = ""  # Fernet key for stored user secrets; wins over config_dir/secret.key
    mb_contact: str = Field(default="https://github.com/marameowra/mixsync", min_length=1)
    mb_rate: float = Field(default=1.0, gt=0, le=1)  # MusicBrainz allows 1 req/s per IP
    mb_base_url: str = "https://musicbrainz.org"  # optional local mirror
    acoustid_app_key: str = ""
    acoustid_user_key: str = ""  # one key for the deployment; all submissions go under it
    slskd_url: str = "http://slskd:5030"
    slskd_api_key: str = ""
    max_concurrent_downloads: int = Field(default=3, ge=1)
    slow_download_window: int = Field(default=60, ge=1)  # seconds the average rate is taken over
    slow_download_floor: int = Field(default=50_000, ge=0)  # bytes/s: below this a transfer is slow
    slow_download_share: float = Field(default=0.10, ge=0, le=1)  # of total download throughput
    navidrome_url: str = "http://navidrome:4533"
    navidrome_user: str = ""  # an admin: startScan needs it
    navidrome_password: str = ""
    navidrome_library: str = "Music Library"  # the main library's name in Navidrome
    navidrome_discover_library: str = "discover"  # the library over /data/discover
    data_dir: str = "/data"
    downloads_dir: str = "/downloads"
    import_source_dir: str = "/import-source"  # the old library; mounted read-only
    flac_level: int = Field(default=8, ge=0, le=8)  # above 8 leaves the FLAC subset
    canonical_media: list[str] = ["Digital Media", "CD"]  # preferred formats, best first
    canonical_countries: list[str] = ["XW"]  # preferred release countries, best first
    trash_days: int = Field(default=30, ge=1)  # trash from day folders older than this is purged
    timezone: str = "UTC"  # the zone report days are read in
    path_template: str = "{albumartist}/{album} ({year})/{disc:02}-{track:02} {title}.{ext}"

    @field_validator("database_url")
    @classmethod
    def _supported_url(cls, v: str) -> str:
        if not v.startswith(("sqlite:", "postgresql")):
            raise ValueError("MIXSYNC_DATABASE_URL must start with sqlite: or postgresql")
        return v

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"MIXSYNC_TIMEZONE is not a known time zone: {v!r}") from None
        return v
