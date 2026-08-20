from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/prca"

    LLM_PROVIDER: str = "gemini"

    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"

    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "llama-3.3-70b-versatile"

    OPENROUTER_API_KEY: str = ""
    OPENROUTER_MODEL: str = "openrouter/free"

    OLLAMA_MODEL: str = "llama3.1"

    INNGEST_EVENT_KEY: str = ""
    INNGEST_SIGNING_KEY: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
