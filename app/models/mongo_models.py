"""
Pydantic schemas describing document shapes stored in MongoDB.
Mongo is schema-flexible, but we still validate at the application boundary.
"""
from datetime import datetime

from pydantic import BaseModel, Field


class KycDocumentEntry(BaseModel):
    doc_type: str  # aadhaar | pan | selfie | gst_certificate
    s3_key: str
    verified: bool = False
    verified_by: str | None = None  # auto_ocr | face_match | manual


class KycDocumentRecord(BaseModel):
    user_id: str
    documents: list[KycDocumentEntry] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class FraudEvent(BaseModel):
    user_id: str
    txn_ref: str
    rule_triggers: list[str] = Field(default_factory=list)  # e.g. ["geo_velocity", "new_beneficiary_high_value"]
    ml_anomaly_score: float
    llm_risk_narrative: str | None = None  # LLM-generated plain-language explanation
    action_taken: str = "flagged_for_review"  # flagged_for_review | auto_blocked | cleared
    created_at: datetime = Field(default_factory=datetime.utcnow)


class RagQueryLog(BaseModel):
    user_id: str | None = None
    query: str
    retrieved_doc_ids: list[str] = Field(default_factory=list)
    llm_provider: str
    response_summary: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class DeviceFingerprint(BaseModel):
    device_id: str
    user_ids_seen: list[str] = Field(default_factory=list)
    first_seen: datetime = Field(default_factory=datetime.utcnow)
    last_seen: datetime = Field(default_factory=datetime.utcnow)
    trust_score: float = 0.5
