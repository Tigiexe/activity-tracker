from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    api_key: str = "change-me"
    db_path: str = "./data/activity.db"
    host: str = "127.0.0.1"
    port: int = 8000

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ACTIVITY_",
        case_sensitive=False,
    )


settings = Settings()
