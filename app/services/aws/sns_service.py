"""
SNS service wrapper — pushes real-time alerts to downstream subscribers
(SMS to end users via SNS-SMS, email via SES-subscribed topic, or
internal ops Slack webhook via Lambda subscriber).
"""
import json

import boto3

from app.config import get_settings

settings = get_settings()

_client = None  # module-level singleton — see note in SNSService.__init__


def _get_client():
    """Lazily builds and caches the boto3 SNS client at module scope.

    SNSService (like FraudService/LendingService that own it) is
    instantiated fresh on every request; building a new boto3 client each
    time redoes credential/region resolution on every call for no benefit,
    since the client itself is stateless and thread-safe to share."""
    global _client
    if _client is None:
        _client = boto3.client(
            "sns",
            region_name=settings.aws_region,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )
    return _client


class SNSService:
    def __init__(self) -> None:
        self._client = _get_client()

    def publish_fraud_alert(self, user_id: str, txn_ref: str, reason: str, risk_score: float) -> str:
        message = {
            "type": "FRAUD_ALERT",
            "user_id": user_id,
            "txn_ref": txn_ref,
            "reason": reason,
            "risk_score": risk_score,
        }
        resp = self._client.publish(
            TopicArn=settings.sns_topic_fraud_alerts,
            Message=json.dumps(message),
            Subject="Suspicious transaction detected",
            MessageAttributes={"event_type": {"DataType": "String", "StringValue": "fraud_alert"}},
        )
        return resp["MessageId"]

    def publish_loan_status_update(self, user_id: str, application_ref: str, status: str) -> str:
        message = {
            "type": "LOAN_STATUS_UPDATE",
            "user_id": user_id,
            "application_ref": application_ref,
            "status": status,
        }
        resp = self._client.publish(
            TopicArn=settings.sns_topic_loan_notifications,
            Message=json.dumps(message),
            Subject=f"Loan application {application_ref} update",
        )
        return resp["MessageId"]

    def publish_repayment_nudge(self, user_id: str, application_ref: str, risk_level: str, reason: str) -> str:
        """Proactive nudge for a borrower whose repayment risk has crossed a
        threshold (upcoming/missed EMI) — sent before a loan actually goes
        into default, not as a post-default recovery notice."""
        message = {
            "type": "REPAYMENT_RISK_NUDGE",
            "user_id": user_id,
            "application_ref": application_ref,
            "risk_level": risk_level,
            "reason": reason,
        }
        resp = self._client.publish(
            TopicArn=settings.sns_topic_collections_nudges,
            Message=json.dumps(message),
            Subject=f"Upcoming repayment reminder — {application_ref}",
            MessageAttributes={"event_type": {"DataType": "String", "StringValue": "repayment_nudge"}},
        )
        return resp["MessageId"]
