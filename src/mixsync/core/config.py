from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MIXSYNC_")

    database_url: str = "sqlite:////config/mixsync.db"
    config_dir: str = "/config"
    mb_contact: str = Field(default="https://github.com/marameowra/mixsync", min_length=1)
    mb_rate: float = Field(default=1.0, gt=0, le=1)  # MusicBrainz allows 1 req/s per IP
    mb_base_url: str = "https://musicbrainz.org"  # optional local mirror
    acoustid_app_key: str = ""
    slskd_url: str = "http://slskd:5030"
    slskd_api_key: str = ""
    data_dir: str = "/data"
    downloads_dir: str = "/downloads"
    path_template: str = "{albumartist}/{album} ({year})/{disc:02}-{track:02} {title}.{ext}"

    @field_validator("database_url")
    @classmethod
    def _supported_url(cls, v: str) -> str:
        if not v.startswith(("sqlite:", "postgresql")):
            raise ValueError("MIXSYNC_DATABASE_URL must start with sqlite: or postgresql")
        return v
