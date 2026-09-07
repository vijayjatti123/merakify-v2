from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    anthropic_api_key: str = ""
    database_url: str = "sqlite:///./merakify.db"
    allowed_origins: str = "http://localhost:5173"

    class Config:
        env_file = ".env"
        case_sensitive = False
        # Map ANTHROPIC_API_KEY -> anthropic_api_key etc.
        env_prefix = ""


settings = Settings()
