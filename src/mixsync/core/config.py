from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# the fields library/paths.render fills in; tests/unit/web/test_settings.py checks they match
PATH_FIELDS = {
    "albumartist": "a",
    "album": "a",
    "year": 2000,
    "disc": 1,
    "track": 1,
    "title": "t",
    "genre": "g",
}


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
    deezer_suggestions: bool = False  # Mark unverified offers on-demand Deezer lookups
    discogs_suggestions: bool = False  # Mark unverified offers Discogs lookups via a user token
    slskd_url: str = "http://slskd:5030"
    slskd_api_key: str = ""
    slskd_max_searches: int = Field(default=2, ge=1)  # slskd searches in flight, all workers
    slskd_search_debounce: int = Field(default=300, ge=0)  # seconds a query's result is reused
    max_concurrent_downloads: int = Field(default=3, ge=1)
    slow_download_window: int = Field(default=60, ge=1)  # seconds the average rate is taken over
    slow_download_floor: int = Field(default=50_000, ge=0)  # bytes/s: below this a transfer is slow
    slow_download_share: float = Field(default=0.10, ge=0, le=1)  # of total download throughput
    navidrome_url: str = "http://navidrome:4533"
    navidrome_user: str = ""  # an admin: startScan needs it
    navidrome_password: str = ""
    navidrome_library: str = "Music Library"  # the main library's name in Navidrome
    navidrome_discover_library: str = "discover"  # the library over /data/discover
    backup_dir: Path | None = None  # unset: config_dir/backups; SQLite only (Postgres: pg_dump)
    data_dir: str = "/data"
    downloads_dir: str = "/downloads"
    drop_dir: Path | None = None  # unset: no drop folder; read-write, originals go to the trash
    adopt_downloads: bool = False  # import album folders nobody requested that land in downloads
    adopt_settle_minutes: int = Field(default=10, ge=1)  # a folder must be this quiet to be adopted
    import_source_dir: str = "/import-source"  # the old library; mounted read-only
    flac_level: int = Field(default=8, ge=0, le=8)  # above 8 leaves the FLAC subset
    canonical_media: list[str] = ["Digital Media", "CD"]  # preferred formats, best first
    canonical_countries: list[str] = ["XW"]  # preferred release countries, best first
    trash_days: int = Field(default=30, ge=1)  # trash from day folders older than this is purged
    timezone: str = "UTC"  # the zone report days are read in
    path_template: str = "{albumartist}/{album} ({year})/{disc:02}-{track:02} {title}.{ext}"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_level(cls, v: object) -> object:
        return v.upper() if isinstance(v, str) else v

    @field_validator("database_url")
    @classmethod
    def _supported_url(cls, v: str) -> str:
        if v.startswith("sqlite:"):
            return v
        # Only psycopg 3 is installed; a bare postgresql:// would make SQLAlchemy pick psycopg2.
        for bare in ("postgresql://", "postgres://"):
            if v.startswith(bare):
                return "postgresql+psycopg://" + v[len(bare) :]
        if v.startswith("postgresql+psycopg://"):
            return v
        raise ValueError(
            "MIXSYNC_DATABASE_URL must start with sqlite:, postgresql:// or postgresql+psycopg://"
        )

    @model_validator(mode="after")
    def _drop_dir_apart(self) -> "Settings":
        """The drop folder holds originals that get moved to the trash; it must not overlap
        the library, the downloads or the old library."""
        if self.drop_dir is None:
            return self
        drop = self.drop_dir.resolve()
        for name in ("data_dir", "downloads_dir", "import_source_dir"):
            other = Path(getattr(self, name)).resolve()
            if drop == other or drop.is_relative_to(other) or other.is_relative_to(drop):
                raise ValueError(f"MIXSYNC_DROP_DIR must not overlap {name} ({other})")
        return self

    @field_validator("path_template")
    @classmethod
    def _renderable(cls, v: str) -> str:
        if not v.endswith(".{ext}"):
            raise ValueError("MIXSYNC_PATH_TEMPLATE must end in .{ext}")
        try:
            v.removesuffix(".{ext}").format(**PATH_FIELDS)
        except (KeyError, ValueError, IndexError, AttributeError, TypeError):
            raise ValueError(f"MIXSYNC_PATH_TEMPLATE is not a valid template: {v!r}") from None
        return v

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"MIXSYNC_TIMEZONE is not a known time zone: {v!r}") from None
        return v
