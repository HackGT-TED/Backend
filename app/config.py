"""Environment-backed settings.

Secrets stay in the environment or a local ``.env`` file (never commit that file).
See ``.env.example`` for every variable this service reads.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    muse_spark_api_key: str = ""
    # Meta Model API, OpenAI-compatible Chat Completions.
    # https://ai.developer.meta.com/docs/protocols/chat-completions
    muse_spark_base_url: str = "https://api.meta.ai/v1"
    muse_spark_model: str = "muse-spark-1.3"

    freesound_api_key: str = ""
    freesound_base_url: str = "https://freesound.org"

    database_url: str = "sqlite:///./data/hackgt.db"

    # Unused by the v1 pipeline (clients upload Deepgram JSON). Kept so a
    # later direct-transcription path can share this settings object.
    deepgram_api_key: str = ""
    deepgram_model: str = "nova-2"
    deepgram_language: str = "en"

    media_dir: str = "./media"
    cors_origins: str = "*"
    http_timeout_seconds: float = 60.0

    def cors_origin_list(self) -> list[str]:
        raw = self.cors_origins.strip()
        if raw == "*":
            return ["*"]
        return [origin.strip() for origin in raw.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
