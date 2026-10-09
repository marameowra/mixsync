from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MIXSYNC_")

    database_url: str = "sqlite:////config/mixsync.db"
    config_dir: str = "/config"

    @field_validator("database_url")
    @classmethod
    def _supported_url(cls, v: str) -> str:
        if not v.startswith(("sqlite:", "postgresql")):
            raise ValueError("MIXSYNC_DATABASE_URL must start with sqlite: or postgresql")
        return v
