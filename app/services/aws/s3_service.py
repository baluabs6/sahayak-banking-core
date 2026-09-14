"""
S3 service wrapper.
Used for: KYC document storage (encrypted, per-user prefix), and the raw
data-lake landing zone for batch analytics / model training exports.

Requires real AWS credentials + bucket to actually execute — this is
production-shaped code, not a live connection.
"""
import io

import boto3
from botocore.exceptions import ClientError

from app.config import get_settings

settings = get_settings()

_client = None  # module-level singleton, see app/services/aws/sns_service.py::_get_client


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            region_name=settings.aws_region,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )
    return _client


class S3Service:
    def __init__(self) -> None:
        self._client = _get_client()

    def upload_kyc_document(self, user_id: str, doc_type: str, file_bytes: bytes, content_type: str) -> str:
        """Uploads a KYC document under a per-user prefix; returns the S3 key."""
        key = f"kyc/{user_id}/{doc_type}.bin"
        self._client.put_object(
            Bucket=settings.s3_bucket_kyc_docs,
            Key=key,
            Body=io.BytesIO(file_bytes),
            ContentType=content_type,
            ServerSideEncryption="aws:kms",
        )
        return key

    def get_presigned_url(self, bucket: str, key: str, expires_seconds: int = 300) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires_seconds,
        )

    def export_to_data_lake(self, dataset_name: str, jsonl_bytes: bytes) -> str:
        """Lands a batch export (e.g. nightly transaction dump) in the data lake bucket,
        partitioned by dataset name — ready for Glue/Athena/Snowflake ingestion."""
        from datetime import datetime, timezone

        # KMS-encrypted like the KYC upload path (a prior version of this
        # export was unencrypted-at-rest — closed here), and uses a
        # timezone-aware timestamp rather than the deprecated utcnow().
        key = f"raw/{dataset_name}/dt={datetime.now(timezone.utc):%Y-%m-%d}/{dataset_name}.jsonl"
        self._client.put_object(
            Bucket=settings.s3_bucket_data_lake,
            Key=key,
            Body=jsonl_bytes,
            ServerSideEncryption="aws:kms",
        )
        return key

    def object_exists(self, bucket: str, key: str) -> bool:
        try:
            self._client.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError:
            return False
