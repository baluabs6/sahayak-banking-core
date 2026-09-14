"""
Async MongoDB connectivity via Motor.
Mongo holds flexible/unstructured/high-write data: KYC document metadata,
fraud event logs, RAG chat/query logs, device fingerprints.
"""
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.config import get_settings

settings = get_settings()

_client: AsyncIOMotorClient | None = None


def get_mongo_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(settings.mongo_uri)
    return _client


def get_mongo_db() -> AsyncIOMotorDatabase:
    return get_mongo_client()[settings.mongo_db_name]


# Collection name constants (single source of truth to avoid typos across services)
COLLECTION_KYC_DOCUMENTS = "kyc_documents"
COLLECTION_FRAUD_EVENTS = "fraud_events"
COLLECTION_RAG_QUERY_LOGS = "rag_query_logs"
COLLECTION_DEVICE_FINGERPRINTS = "device_fingerprints"
COLLECTION_AUDIT_LOGS = "audit_logs"


async def ensure_indexes() -> None:
    """Create indexes on startup — call once from app lifespan."""
    db = get_mongo_db()
    await db[COLLECTION_KYC_DOCUMENTS].create_index("user_id", unique=True)
    await db[COLLECTION_FRAUD_EVENTS].create_index("user_id")
    await db[COLLECTION_FRAUD_EVENTS].create_index("created_at")
    await db[COLLECTION_RAG_QUERY_LOGS].create_index("user_id")
    await db[COLLECTION_DEVICE_FINGERPRINTS].create_index("device_id", unique=True)
    await db[COLLECTION_AUDIT_LOGS].create_index("target_user_ref")
    await db[COLLECTION_AUDIT_LOGS].create_index("actor_ref")
    await db[COLLECTION_AUDIT_LOGS].create_index("created_at")
