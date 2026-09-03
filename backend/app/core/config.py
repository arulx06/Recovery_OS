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

    # Durable action transport. PostgreSQL remains authoritative; Redis/RQ
    # only carries action IDs and can be rebuilt by reconciliation.
    REDIS_URL: str = "redis://localhost:6379/0"
    RQ_QUEUE_NAME: str = "recoveryos"
    TASK_QUEUE_ENABLED: bool = True
    WAIT_DELAY_SECONDS: int = 3600
    NATIVE_RETRY_DELAY_SECONDS: int = 86400
    ACTION_MAX_ATTEMPTS: int = 3
    ACTION_RETRY_DELAY_SECONDS: int = 60
    ACTION_CLAIM_TIMEOUT_SECONDS: int = 300
    ACTION_JOB_TIMEOUT_SECONDS: int = 30
    MERCHANT_TIMEZONE: str = "Asia/Kolkata"

    # Razorpay (test mode) — filled in during Phase 1
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""
    RAZORPAY_API_ENABLED: bool = False

    # LLM provider — filled in during Phase 6 (Promise-to-Pay)
    LLM_PROVIDER: str = "anthropic"
    LLM_API_KEY: str = ""
    LLM_API_ENABLED: bool = False
    LLM_MODEL: str = "claude-3-5-haiku-latest"
    LLM_TIMEOUT_SECONDS: float = 10.0
    LLM_MAX_RETRIES: int = 2
    LLM_MESSAGE_DRAFT_ENABLED: bool = True
    LLM_PTP_EXTRACTION_ENABLED: bool = True

    # Recovery policy dispatch — baseline is the safe default.
    RECOVERY_POLICY: str = "baseline"  # baseline | shadow | adaptive
    ADAPTIVE_POLICY_PROFILE: str = "balanced"  # revenue_first | balanced | low_friction
    ADAPTIVE_FRICTION_WEIGHT: float | None = None  # overrides profile if set
    MODEL_ARTIFACT_PATH: str = "app/ml/artifacts/model.joblib"
    MODEL_MANIFEST_PATH: str = "app/ml/artifacts/manifest.json"

    # CORS
    FRONTEND_ORIGIN: str = "http://localhost:5173"


settings = Settings()
