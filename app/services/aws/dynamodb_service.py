"""
DynamoDB service wrapper.
Used for two low-latency, high-throughput access patterns that don't
suit Postgres/Mongo well:
  1. Session tokens (TTL-based, single-digit-ms reads)
  2. Real-time fraud signal counters (per-user/per-device velocity checks)
"""
import time

import boto3

from app.config import get_settings

settings = get_settings()

_resource = None  # module-level singleton, see app/services/aws/sns_service.py::_get_client


def _get_resource():
    global _resource
    if _resource is None:
        _resource = boto3.resource(
            "dynamodb",
            region_name=settings.aws_region,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )
    return _resource


class DynamoDBService:
    def __init__(self) -> None:
        self._resource = _get_resource()
        self._sessions_table = self._resource.Table(settings.dynamodb_table_sessions)
        self._fraud_signals_table = self._resource.Table(settings.dynamodb_table_fraud_signals)

    # --- Sessions ---
    def put_session(self, session_id: str, user_id: str, ttl_seconds: int = 3600) -> None:
        self._sessions_table.put_item(
            Item={
                "session_id": session_id,
                "user_id": user_id,
                "created_at": int(time.time()),
                "ttl": int(time.time()) + ttl_seconds,  # DynamoDB TTL attribute
            }
        )

    def get_session(self, session_id: str) -> dict | None:
        resp = self._sessions_table.get_item(Key={"session_id": session_id})
        return resp.get("Item")

    def revoke_session(self, session_id: str) -> None:
        self._sessions_table.delete_item(Key={"session_id": session_id})

    # --- Fraud velocity signals ---
    def increment_txn_velocity(self, user_id: str, window_key: str) -> int:
        """Atomic counter for transactions-per-window (e.g. per-minute), used
        for real-time velocity-based fraud rules."""
        resp = self._fraud_signals_table.update_item(
            Key={"user_id": user_id, "window_key": window_key},
            UpdateExpression="ADD txn_count :incr SET ttl = :ttl",
            ExpressionAttributeValues={":incr": 1, ":ttl": int(time.time()) + 300},
            ReturnValues="UPDATED_NEW",
        )
        return int(resp["Attributes"]["txn_count"])

    def get_txn_velocity(self, user_id: str, window_key: str) -> int:
        """Reads the current count for a velocity window. Previously nothing
        in the codebase ever called this — increment_txn_velocity wrote a
        counter every transaction that the fraud engine never consulted, so
        rapid-fire transactions from the same user never actually tripped a
        velocity rule. Returns 0 if the window doesn't exist yet."""
        resp = self._fraud_signals_table.get_item(Key={"user_id": user_id, "window_key": window_key})
        item = resp.get("Item")
        return int(item["txn_count"]) if item else 0
