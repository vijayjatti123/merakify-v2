from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    gemini_image_model: str = "gemini-3.1-flash-image"
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
