"""
Central app configuration.
All environment-dependent values are loaded here and nowhere else,
so the rest of the codebase never touches os.environ directly.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = "RecoveryOS"
    ENV: str = "dev"

    # Database
    DATABASE_URL: str = "postgresql+psycopg2://recoveryos:recoveryos@localhost:5432/recoveryos"

    # Redis / queue (used from Phase 3 onward for delayed/scheduled actions)
    REDIS_URL: str = "redis://localhost:6379/0"

    # Razorpay (test mode) — filled in during Phase 1
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""

    # LLM provider — filled in during Phase 6 (Promise-to-Pay)
    LLM_PROVIDER: str = ""
    LLM_API_KEY: str = ""

    # CORS
    FRONTEND_ORIGIN: str = "http://localhost:5173"


settings = Settings()
