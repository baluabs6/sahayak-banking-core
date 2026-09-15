"""
Central application configuration.
All values are read from environment variables (.env in local dev,
injected as ECS/EC2 task env vars or Secrets Manager in AWS).
"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App ---
    app_name: str = "sahayak-banking-core"
    environment: str = "local"  # local | staging | production
    log_level: str = "INFO"

    # --- PostgreSQL (source of truth: users, accounts, transactions, loans, insurance) ---
    # No credentials are hardcoded here — set POSTGRES_DSN in your local .env
    # (see .env.example) or inject it via Secrets Manager / your orchestrator
    # in staging/production. The placeholder below will simply fail to
    # connect until overridden, by design.
    postgres_dsn: str = "postgresql+asyncpg://****************************@localhost:5432/sahayak_db"

    # --- MongoDB (flexible/unstructured: KYC docs, fraud events, chat/RAG logs) ---
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db_name: str = "sahayak_nosql"

    # --- Redis (cache + rate limiting; NOT a system of record) ---
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl_credit_score_seconds: int = 600       # 10 min: alt-data signals don't change that fast
    cache_ttl_rag_answer_seconds: int = 1800        # 30 min: policy/scheme Q&A answers are stable
    rate_limit_assistant_per_minute: int = 10       # per-user cap on the LLM-backed assistant route

    # --- AWS ---
    aws_region: str = "ap-south-1"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    s3_bucket_kyc_docs: str = "sahayak-kyc-documents"
    s3_bucket_data_lake: str = "sahayak-data-lake"
    dynamodb_table_sessions: str = "sahayak-user-sessions"
    dynamodb_table_fraud_signals: str = "sahayak-fraud-signals"
    sns_topic_fraud_alerts: str = "arn:aws:sns:ap-south-1:000000000000:fraud-alerts"
    sns_topic_loan_notifications: str = "arn:aws:sns:ap-south-1:000000000000:loan-notifications"
    sns_topic_collections_nudges: str = "arn:aws:sns:ap-south-1:000000000000:collections-nudges"
    cloudwatch_namespace: str = "SahayakBankingCore/Backend"

    # --- Auth ---
    # Must be overridden via JWT_SECRET env var / Secrets Manager — see
    # validation in get_settings() below, which refuses to boot with this
    # placeholder outside `environment == "local"`.
    jwt_secret: str = "****************************"
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 60
    jwt_refresh_expiry_days: int = 14
    # Dev-only OTP accepted by verify_login_otp() when environment == "local".
    # NEVER used outside local — see app/core/security.py::verify_login_otp.
    dev_fallback_otp: str = "000000"
    # Shared secret required to mint analyst/admin tokens via /auth/internal-token.
    # Must be set via env/secrets manager in staging/production.
    internal_admin_bootstrap_key: str | None = None
    # Per-route request caps (requests/minute), enforced via Redis in each controller.
    rate_limit_default_per_minute: int = 30
    rate_limit_transactions_per_minute: int = 20

    # --- AI / LLM providers (provider-agnostic; pick at runtime) ---
    llm_provider: str = "anthropic"  # anthropic | openai | ollama | langchain_auto
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-4-6"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-1.5-flash"
    bedrock_model_id: str = "anthropic.claude-3-5-sonnet-20241022-v2:0"

    # --- Hybrid retrieval (dense + sparse) ---
    use_hybrid_retrieval: bool = False
    hybrid_dense_weight: float = 0.5  # sparse weight = 1 - this

    # --- RAG / vector store ---
    vector_store_backend: str = "faiss"  # faiss | pgvector
    embedding_provider: str = "anthropic"  # anthropic | openai | local

    # --- GCP DR ---
    gcp_project_id: str | None = None
    gcp_dr_region: str = "asia-south1"

    # --- Microsoft Azure DR (second, independent secondary site — see infra/azure_dr) ---
    azure_subscription_id: str | None = None
    azure_dr_region: str = "centralindia"
    azure_storage_account_name: str | None = None
    # Cosmos DB (MongoDB API) connection string for the DR site — never
    # hardcode a real value; only ever set via env var / Key Vault injection.
    azure_cosmosdb_mongo_uri: str | None = None
    dr_active_site: str = "gcp"  # gcp | azure — which secondary is currently promoted, if any


_PLACEHOLDER_JWT_SECRET = "****************************"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    # Fail fast rather than silently signing tokens with a known placeholder
    # secret outside local dev — this used to default to the literal string
    # "****************************" with nothing enforcing that anyone actually
    # changed it. This is a first, immediate check; validate_production_config()
    # below runs the fuller set of checks from app.main's on_start hook.
    if settings.environment != "local" and settings.jwt_secret == _PLACEHOLDER_JWT_SECRET:
        raise RuntimeError(
            "JWT_SECRET is still the placeholder value. Set a real secret via "
            "env var / Secrets Manager before running outside `environment=local`."
        )
    return settings


class InsecureConfigurationError(RuntimeError):
    """Raised at startup when staging/production is about to boot with
    unsafe defaults (weak/default JWT secret, missing admin bootstrap key, etc.)."""


def validate_production_config(settings: "Settings") -> None:
    """Fail-closed startup guard. Call this from app.main's on_start hook.
    Deliberately does nothing in `local` so dev setup stays frictionless."""
    if settings.environment == "local":
        return

    from app.core.security import is_secret_strong_enough

    problems: list[str] = []
    if not is_secret_strong_enough(settings.jwt_secret):
        problems.append(
            "JWT_SECRET is missing, default, or too short (<32 chars) for a "
            f"'{settings.environment}' environment."
        )
    if not settings.internal_admin_bootstrap_key or len(settings.internal_admin_bootstrap_key) < 24:
        problems.append(
            "INTERNAL_ADMIN_BOOTSTRAP_KEY is missing/too short - analyst/admin "
            "token issuance would be unsafe."
        )
    if settings.dev_fallback_otp == "000000":
        # Not a hard failure by itself (verify_login_otp already fails closed
        # outside `local`), but flagged so it's never silently left at default.
        problems.append(
            "DEV_FALLBACK_OTP is still the default value - confirm it is unused "
            "outside local (verify_login_otp() already blocks it, this is belt-and-braces)."
        )

    if problems:
        raise InsecureConfigurationError(
            f"Refusing to start in '{settings.environment}' with insecure configuration:\n- "
            + "\n- ".join(problems)
        )
