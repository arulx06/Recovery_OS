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

    # Razorpay Test Mode configuration
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""
    RAZORPAY_API_ENABLED: bool = False

    # Optional LLM provider for drafting and promise-to-pay extraction
    LLM_PROVIDER: str = "anthropic"
    LLM_API_KEY: str = ""
    LLM_API_ENABLED: bool = False
    LLM_MODEL: str = "claude-3-5-haiku-latest"
    LLM_BASE_URL: str | None = None
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

    # Public-demo safety - optional operator protection for sensitive reads and
    # mutations. Webhook remains signature-authenticated; health stays public.
    DEMO_ADMIN_TOKEN_ENABLED: bool = False
    DEMO_ADMIN_TOKEN: str = ""

    # Failure injection — development/test only. Never silently active in production.
    FAILURE_INJECTION_ENABLED: bool = False

    # Experiment guard — prevent accidental huge runs freezing demo.
    EXPERIMENT_MAX_COUNT: int = 1000

    # Startup validation strictness
    ENFORCE_TEST_MODE_ONLY: bool = True


settings = Settings()


def parse_frontend_origins(value: str | None = None) -> list[str]:
    """Parse CORS origins and reject ambiguous wildcard combinations."""
    configured = settings.FRONTEND_ORIGIN if value is None else value
    origins = [origin.strip() for origin in configured.split(",") if origin.strip()]
    if not origins:
        origins = ["http://localhost:5173"]
    if "*" in origins and len(origins) != 1:
        raise ValueError("FRONTEND_ORIGIN cannot mix '*' with explicit origins")
    return origins


def cors_allows_credentials(origins: list[str]) -> bool:
    """Credentialed CORS is valid only for an explicit origin allow-list."""
    return "*" not in origins

# ---------------------------------------------------------------------------
# Startup validation — fail early for dangerous configuration
# ---------------------------------------------------------------------------

def validate_startup_config() -> list[str]:
    """Return list of fatal config errors; caller should abort if non-empty.

    Safe defaults for local deterministic dev are preserved — only truly
    dangerous misconfigurations are treated as fatal.
    """
    errors: list[str] = []
    # Razorpay: if API enabled, credentials must be test-mode and non-empty
    if settings.RAZORPAY_API_ENABLED:
        if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
            errors.append("RAZORPAY_API_ENABLED=true but RAZORPAY_KEY_ID/SECRET incomplete")
        elif settings.ENFORCE_TEST_MODE_ONLY and not settings.RAZORPAY_KEY_ID.startswith("rzp_test_"):
            errors.append("RAZORPAY_API_ENABLED=true but RAZORPAY_KEY_ID is not rzp_test_ — live keys not allowed")
    elif settings.RAZORPAY_KEY_ID and settings.ENFORCE_TEST_MODE_ONLY and settings.RAZORPAY_KEY_ID.startswith("rzp_live_"):
        errors.append("rzp_live_ key supplied but RAZORPAY_API_ENABLED is false — live keys are never accepted in this build")
    # Recovery policy must be known value
    if settings.RECOVERY_POLICY not in ("baseline", "shadow", "adaptive"):
        errors.append(f"Unknown RECOVERY_POLICY={settings.RECOVERY_POLICY!r} — must be baseline|shadow|adaptive")
    # Demo token: if protection enabled, token must be non-empty
    if settings.DEMO_ADMIN_TOKEN_ENABLED and not settings.DEMO_ADMIN_TOKEN:
        errors.append("DEMO_ADMIN_TOKEN_ENABLED=true but DEMO_ADMIN_TOKEN is empty")
    # LLM: if enabled, provider must be anthropic or opencode_zen
    if settings.LLM_API_ENABLED and settings.LLM_PROVIDER.lower() not in ("anthropic", "opencode_zen"):
        errors.append(f"LLM_API_ENABLED=true but LLM_PROVIDER={settings.LLM_PROVIDER!r} — only anthropic and opencode_zen are supported")
    try:
        parse_frontend_origins()
    except ValueError as exc:
        errors.append(str(exc))
    return errors
