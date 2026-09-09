"""
CloudWatch service wrapper — emits custom business + operational metrics.
Pairs with CloudWatch Alarms (e.g. fraud_flag_rate > threshold triggers
a PagerDuty/SNS alarm) and dashboards for ops visibility.
"""
import boto3

from app.config import get_settings

settings = get_settings()


class CloudWatchService:
    def __init__(self) -> None:
        self._client = boto3.client(
            "cloudwatch",
            region_name=settings.aws_region,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )

    def put_metric(self, metric_name: str, value: float, unit: str = "Count", dimensions: dict | None = None) -> None:
        dims = [{"Name": k, "Value": str(v)} for k, v in (dimensions or {}).items()]
        self._client.put_metric_data(
            Namespace=settings.cloudwatch_namespace,
            MetricData=[
                {
                    "MetricName": metric_name,
                    "Value": value,
                    "Unit": unit,
                    "Dimensions": dims,
                }
            ],
        )

    def record_fraud_flag(self, domain: str = "fraud") -> None:
        self.put_metric("FraudFlagsRaised", 1, dimensions={"domain": domain})

    def record_loan_decision(self, decision: str) -> None:
        self.put_metric("LoanDecisions", 1, dimensions={"decision": decision})

    def record_api_latency_ms(self, route: str, latency_ms: float) -> None:
        self.put_metric("ApiLatencyMs", latency_ms, unit="Milliseconds", dimensions={"route": route})
