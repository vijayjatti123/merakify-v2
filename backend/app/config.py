from pydantic_settings import BaseSettings
from pydantic import AliasChoices, Field


class Settings(BaseSettings):
    anthropic_api_key: str = ""
    open_router_api_key: str = ""
    director_model: str = "google/gemini-3.8-flash"
    planning_structured_outputs: bool = False
    google_ai_api_key: str = ""
    gemini_image_model: str = "gemini-3.1-flash-image"
    gemini_preview_check_model: str = "gemini-3.6-flash"
    sarvam_api_key: str = ""
    elevenlabs_api_key: str = ""
    evolink_api_key: str = ""
    fal_api_key: str = Field(default="", validation_alias=AliasChoices("FAL_API_KEY", "FAL_API-KEY", "fal_api_key"))
    hedra_api_key: str = ""
    replicate_api_token: str = ""
    database_url: str = "sqlite:///./merakify.db"
    allowed_origins: str = "http://localhost:5173"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = ""
    aws_s3_bucket: str = ""
    aws_s3_presigned_url_ttl_sec: int = 3600

    class Config:
        env_file = ".env"
        case_sensitive = False
        # Map ANTHROPIC_API_KEY -> anthropic_api_key etc.
        env_prefix = ""


settings = Settings()
