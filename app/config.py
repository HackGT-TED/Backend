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

    xai_api_key: str = ""
    # xAI OpenAI-compatible Chat Completions.
    # https://docs.x.ai/docs/models/grok-4.7
    xai_base_url: str = "https://api.x.ai/v1"
    xai_model: str = "grok-4.7"

    freesound_api_key: str = ""
    freesound_base_url: str = "https://freesound.org"

    # Server-side only. The service role bypasses RLS. The anon key is not read.
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_sfx_bucket: str = "story-sfx"

    # Prerecorded transcription: POST {base}/v1/listen.
    # https://developers.deepgram.com/docs/pre-recorded-audio
    deepgram_api_key: str = ""
    deepgram_base_url: str = "https://api.deepgram.com"
    deepgram_model: str = "nova-3"
    deepgram_language: str = "en"

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
