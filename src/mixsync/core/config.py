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
    model_config = SettingsConfigDict(env_prefix="MIXSYNC_", hide_input_in_errors=True)

    database_url: str = Field(
        default="sqlite:////config/mixsync.db",
        description="SQLAlchemy URL of the database: sqlite: or postgresql://.",
    )
    config_dir: str = Field(
        default="/config", description="Folder for the secret key, backups and worker state."
    )
    secret_key: str = Field(
        default="",
        description="Fernet key that encrypts stored secrets; wins over config_dir/secret.key.",
    )
    mb_contact: str = Field(
        default="https://github.com/marameowra/mixsync",
        min_length=1,
        description="Contact URL or email sent in the User-Agent of MusicBrainz requests.",
    )
    mb_rate: float = Field(
        default=1.0,
        gt=0,
        le=1,
        description="MusicBrainz requests per second, above 0 and at most 1 (its per-IP limit).",
    )
    mb_base_url: str = Field(
        default="https://musicbrainz.org",
        description="MusicBrainz server to query; set it to use a local mirror.",
    )
    acoustid_app_key: str = Field(
        default="", description="AcoustID application API key, used to look up fingerprints."
    )
    acoustid_user_key: str = Field(
        default="",
        description="AcoustID user key; all fingerprint submissions go under it.",
    )
    deezer_suggestions: bool = Field(
        default=False,
        description="Mark unverified offers with on-demand Deezer lookups.",
    )
    discogs_suggestions: bool = Field(
        default=False,
        description="Mark unverified offers with Discogs lookups via a user token.",
    )
    slskd_url: str = Field(default="http://slskd:5030", description="Base URL of the slskd server.")
    slskd_api_key: str = Field(default="", description="API key for the slskd server.")
    slskd_max_searches: int = Field(
        default=2, ge=1, description="Most slskd searches in flight at once, across all workers."
    )
    slskd_search_debounce: int = Field(
        default=300,
        ge=0,
        description="Seconds a search result is reused before the same query repeats.",
    )
    prowlarr_url: str = Field(
        default="",
        description="Base URL of Prowlarr; its torrents go to qBittorrent, NZBs to SABnzbd.",
    )
    prowlarr_api_key: str = Field(default="", description="API key for Prowlarr.")
    qbittorrent_url: str = Field(default="", description="Base URL of the qBittorrent Web UI.")
    qbittorrent_username: str = Field(default="", description="qBittorrent Web UI username.")
    qbittorrent_password: str = Field(default="", description="qBittorrent Web UI password.")
    qbittorrent_dir: Path | None = Field(
        default=None,
        description="Save path of the mixsync category in qBittorrent: seeding, copied only.",
    )
    sabnzbd_url: str = Field(default="", description="Base URL of SABnzbd.")
    sabnzbd_api_key: str = Field(default="", description="API key for SABnzbd.")
    sabnzbd_dir: Path | None = Field(
        default=None,
        description="Folder of the mixsync category in SABnzbd; released after import.",
    )
    max_concurrent_downloads: int = Field(
        default=3, ge=1, description="Most downloads running at the same time."
    )
    slow_download_window: int = Field(
        default=60, ge=1, description="Seconds over which a transfer's average rate is taken."
    )
    slow_download_floor: int = Field(
        default=50_000,
        ge=0,
        description="Bytes per second below which a transfer counts as slow.",
    )
    slow_download_share: float = Field(
        default=0.10,
        ge=0,
        le=1,
        description="Share (0 to 1) of total download throughput below which a transfer is slow.",
    )
    navidrome_url: str = Field(
        default="http://navidrome:4533", description="Base URL of the Navidrome server."
    )
    navidrome_user: str = Field(
        default="", description="Navidrome admin username; startScan needs an admin."
    )
    navidrome_password: str = Field(default="", description="Password of the Navidrome user.")
    navidrome_library: str = Field(
        default="Music Library", description="Name of the main library in Navidrome."
    )
    navidrome_discover_library: str = Field(
        default="discover", description="Name of the Navidrome library over /data/discover."
    )
    backup_dir: Path | None = Field(
        default=None,
        description="Backup folder; unset means config_dir/backups. SQLite only.",
    )
    data_dir: str = Field(
        default="/data", description="Holds the library, discover, trash and quarantine folders."
    )
    downloads_dir: str = Field(default="/downloads", description="Folder downloads land in.")
    drop_dir: Path | None = Field(
        default=None,
        description="Drop folder to import from; unset means none. Originals go to the trash.",
    )
    adopt_downloads: bool = Field(
        default=False,
        description="Import album folders nobody requested that land in downloads.",
    )
    adopt_settle_minutes: int = Field(
        default=10,
        ge=1,
        description="Minutes a folder must stay quiet before it is adopted.",
    )
    import_source_dir: str = Field(
        default="/import-source", description="The old library to import from; mounted read-only."
    )
    flac_level: int = Field(
        default=8,
        ge=0,
        le=8,
        description="FLAC compression level, 0 to 8 (above 8 leaves the subset).",
    )
    canonical_media: list[str] = Field(
        default=["Digital Media", "CD"],
        description="Preferred release formats, best first, comma-separated.",
    )
    canonical_countries: list[str] = Field(
        default=["XW"],
        description="Preferred release countries, best first, comma-separated.",
    )
    embed_cover: bool = Field(
        default=False, description="Also embed the front cover (up to 1 MB) in imported files."
    )
    trash_days: int = Field(
        default=30, ge=1, description="Days after which trash in a day folder is purged."
    )
    timezone: str = Field(default="UTC", description="Time zone that report days are read in.")
    path_template: str = Field(
        default="{albumartist}/{album} ({year})/{disc:02}-{track:02} {title}.{ext}",
        description="Where imported files go under data_dir/library; must end in .{ext}.",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", description="Log verbosity: DEBUG, INFO, WARNING, ERROR or CRITICAL."
    )

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
        """The drop folder holds originals that get moved to the trash, the qBittorrent dir
        seeding downloads that are only ever copied (ADR 0022), the SABnzbd dir Usenet downloads
        released after import; none may overlap the library, the downloads, the old library or
        each other, so adopt and the drop folder never scan a client's dir."""
        own = ("drop_dir", "qbittorrent_dir", "sabnzbd_dir")
        mounts = ("data_dir", "downloads_dir", "import_source_dir", *own)
        for name in own[1:]:
            if (d := getattr(self, name)) is not None and not Path(d).is_absolute():
                # the client resolves a relative path against its own default, not ours
                raise ValueError(f"MIXSYNC_{name.upper()} must be an absolute path")
        for name in own:
            if (mine := getattr(self, name)) is None:
                continue
            here = Path(mine).resolve()
            for o in mounts:
                if o == name or getattr(self, o) is None:
                    continue
                other = Path(getattr(self, o)).resolve()
                if here == other or here.is_relative_to(other) or other.is_relative_to(here):
                    raise ValueError(f"MIXSYNC_{name.upper()} must not overlap {o} ({other})")
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
