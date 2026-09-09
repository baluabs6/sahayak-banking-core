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
    postgres_dsn: str = "postgresql+asyncpg://sahayak_user:sahayak_pass@localhost:5432/sahayak_db"

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
    cloudwatch_namespace: str = "SahayakBankingCore/Backend"

    # --- Auth ---
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 60

    # --- AI / LLM providers (provider-agnostic; pick at runtime) ---
    llm_provider: str = "anthropic"  # anthropic | openai | ollama | langchain_auto
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-4-6"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"

    # --- RAG / vector store ---
    vector_store_backend: str = "faiss"  # faiss | pgvector
    embedding_provider: str = "anthropic"  # anthropic | openai | local

    # --- GCP DR ---
    gcp_project_id: str | None = None
    gcp_dr_region: str = "asia-south1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
