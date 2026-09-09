# API Gateway setup notes

`openapi.yaml` defines an AWS API Gateway **HTTP API** that fronts the
app via a **VPC Link** to a private Network Load Balancer sitting in
front of the EC2/ECS-hosted BlackSheep app(s) — the app never gets a
public IP directly.

## Why API Gateway here

- **Auth**: attach a JWT authorizer (validates the tokens issued by
  `app/core/security.py`, or swap to a Cognito authorizer) at the
  gateway, so unauthenticated requests never reach the app.
- **Throttling / quotas**: per-API-key rate limits — important for the
  `/assistant/ask` route specifically, since it fans out to paid LLM
  APIs and needs abuse protection.
- **Single entry point**: mobile/web clients call one gateway domain;
  which backend instance/service actually serves each path is an
  internal routing detail.

## Splitting into true microservices later

Today, `app/main.py` runs all five domain controllers (inclusion,
fraud, lending, insurance, assistant) in one BlackSheep process — the
simplest deployable unit for a first version.

To split into independently deployable/scalable microservices:
1. Turn each `app/services/domains/<domain>/` package (plus its shared
   `app/db`, `app/config.py`, `app/services/aws`) into its own
   deployable app with its own `main.py` exposing just that domain's
   controller.
2. Deploy each behind its own ECS service / Lambda, still sharing the
   same Postgres/Mongo clusters.
3. Update `openapi.yaml`'s `uri` per path to point at each service's
   own NLB/VPC Link instead of one shared `APP_SERVICE_URL`.
   Fraud detection and RAG/LLM calls are the best early candidates to
   split out first — they have very different scaling and latency
   profiles from the rest (fraud needs low-latency/high-throughput;
   LLM calls are slow and bursty).

## Terraform snippet (API Gateway resource)

```hcl
resource "aws_apigatewayv2_api" "sahayak_api" {
  name          = "sahayak-banking-core"
  protocol_type = "HTTP"
  body          = file("${path.module}/../api_gateway/openapi.yaml")
}

resource "aws_apigatewayv2_stage" "prod" {
  api_id      = aws_apigatewayv2_api.sahayak_api.id
  name        = "prod"
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = 200
    throttling_rate_limit  = 100
  }
}
```
