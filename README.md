# Sahayak Banking Core — Financial Inclusion, Fraud, Lending & Insurance

Backend-only system (no frontend) addressing multiple India-specific
finance-sector problems from one codebase: financial inclusion via
alt-data credit scoring, real-time fraud detection, cash-flow-based
MSME/personal lending, and parametric insurance claims — plus a
multi-provider RAG/LLM assistant layer.

## Stack

| Layer | Choice |
|---|---|
| Backend framework | Python 3.11+, BlackSheep (ASGI, async) |
| Relational DB | PostgreSQL (system of record: users, credit scores, transactions, loans, claims) |
| NoSQL DB | MongoDB (KYC document metadata, fraud event logs, RAG query logs) |
| Cache | Redis (credit score + RAG answer caching, assistant-route rate limiting) |
| Cloud | AWS — S3, DynamoDB, SNS, CloudWatch, EC2, API Gateway |
| Bulk migration | AWS Snowball (one-time historical data onboarding only — see `infra/aws/terraform/snowball_note.md`) |
| DR | GCP (Cloud SQL, Compute Engine, Cloud Storage, Cloud Run) as secondary site |
| AI/ML | RAG (LangChain + FAISS), LLMs (Anthropic Claude / OpenAI / Ollama / LangChain-agnostic), scikit-learn (fraud anomaly detection), rule-based scorecard (credit scoring) |

## Why two databases

- **PostgreSQL** for anything transactional/relational that needs ACID
  guarantees and joins: user records, credit score history, loan
  applications, insurance claims, transaction ledger.
- **MongoDB** for flexible, high-write, less-structured data: KYC
  document metadata (varies by doc type), fraud event logs (variable
  rule-trigger arrays), RAG query/response logs.
- **Redis** as a pure cache in front of the above two — never the
  source of truth, always reconstructible, safe to flush. See "Caching
  & rate limiting" below.

## Caching & rate limiting (Redis)

`app/db/redis_client.py` provides three things, used by the inclusion
and assistant services:

1. **Credit score caching** (`InclusionService.score_user`) — keyed on
   `user_ref` + a hash of the alt-data payload, TTL 10 min by default
   (`CACHE_TTL_CREDIT_SCORE_SECONDS`). Repeat scoring calls with
   unchanged inputs (e.g. a lending check and a customer "check my
   score" call landing seconds apart) skip both the scorecard math and
   the LLM explanation call. Response includes `"cache_hit": true/false`.

2. **RAG answer caching** (`RAGService.answer`) — keyed on the
   normalized query + LLM provider, TTL 30 min
   (`CACHE_TTL_RAG_ANSWER_SECONDS`). Common FAQ-style scheme/policy
   questions skip a paid LLM call entirely on repeat.

3. **Rate limiting** (`/api/v1/assistant/ask`) — a Redis fixed-window
   counter caps each caller (by `user_ref`, falling back to client IP)
   to `RATE_LIMIT_ASSISTANT_PER_MINUTE` (default 10) requests/minute,
   returning HTTP 429 over the limit. This is an app-level throttle in
   front of the LLM APIs' own cost/latency — API Gateway throttling
   (see `infra/api_gateway/README.md`) is the outer backstop.

All Redis calls fail open/soft: if Redis is unreachable, cache reads
return a miss (falls through to recompute) and the rate limiter allows
the request — a cache outage degrades performance, it never takes the
API down.

## Domains

1. **Inclusion** (`app/services/domains/inclusion`) — alt-data credit
   scoring for users with no formal credit history, using UPI
   regularity, bill-payment behavior, and mobile recharge patterns.
   Deeply implemented: transparent weighted scorecard + LLM-generated
   plain-language explanation (fair-lending requirement).

2. **Fraud** (`app/services/domains/fraud`) — hybrid rule-based +
   Isolation Forest anomaly detection on every transaction, with
   DynamoDB velocity counters, SNS alerts, CloudWatch metrics, Mongo
   event logging, and an LLM-generated risk narrative for analysts.
   Deeply implemented.

3. **Lending** (`app/services/domains/lending`) — cash-flow-based
   underwriting scaffold combining the inclusion credit score with
   loan-specific factors into approve/review/reject.

4. **Insurance** (`app/services/domains/insurance`) — parametric claims
   scaffold: auto-triggers payout for crop/property claims when an
   external index (rainfall deficit, flood index) crosses a threshold,
   instead of waiting on manual assessment.

5. **Assistant** (`app/services/ai`) — RAG-grounded Q&A over
   RBI/scheme documents, provider-agnostic across 4 LLM backends.

## Local setup

```bash
cp .env.example .env          # fill in AWS/LLM keys as available; local DBs work out of the box
docker-compose up -d          # starts Postgres, MongoDB, Redis, and (optional) Ollama
pip install -r requirements.txt --break-system-packages
python -m data.generate_dummy_data   # seeds Postgres + Mongo with dummy data
uvicorn app.main:app --reload --port 8000
```

Then try:
```bash
curl -X POST http://localhost:8000/api/v1/inclusion/credit-score \
  -H "Content-Type: application/json" \
  -d '{"user_ref": "USR-1001", "alt_data": {"avg_monthly_upi_txn_count": 22, "avg_monthly_upi_volume_inr": 8100, "utility_bill_ontime_ratio": 0.92, "mobile_recharge_regularity_score": 0.81, "months_of_transaction_history": 14, "existing_loan_count": 1, "existing_loan_default_flag": false}}'

curl -X POST http://localhost:8000/api/v1/fraud/transactions/evaluate \
  -H "Content-Type: application/json" \
  -d '{"user_ref": "USR-1003", "amount_inr": 48000, "txn_type": "NEFT_OUT", "merchant": "New Beneficiary", "location": "Bengaluru, KA", "device_id": "DEV-NEW1"}'

curl -X POST http://localhost:8000/api/v1/assistant/ask \
  -H "Content-Type: application/json" \
  -d '{"query": "How does crop insurance get paid out faster?", "llm_provider": "anthropic"}'
```

## Choosing the LLM backend

Set `LLM_PROVIDER` in `.env` to one of `anthropic`, `openai`, `ollama`,
or `langchain_auto` — every domain service goes through
`app/services/ai/llm_provider.get_llm_provider()`, so nothing else in
the codebase needs to change. Any request can also override the
provider per-call via `"llm_provider": "..."` in the JSON body (see
the `/assistant/ask`, `/inclusion/credit-score` endpoints).

- `anthropic` / `openai` — hosted APIs, need an API key.
- `ollama` — fully local/offline model (useful for a bank-branch
  deployment with no reliable internet, or for keeping sensitive
  prompts off third-party APIs).
- `langchain_auto` — LangChain's own chat-model abstraction, picks the
  first available credential; use this when you want LangChain itself
  (not this app's config) to own provider selection/fallback logic.

## Project layout

```
app/
  main.py                  # entrypoint, mounts all domain controllers
  config.py                 # env-driven settings (single source of truth)
  db/                        # Postgres (SQLAlchemy) + Mongo (Motor) + Redis connections
  models/                    # ORM models (Postgres) + Pydantic schemas (Mongo)
  core/                       # JWT auth, structured logging
  services/
    aws/                      # S3, DynamoDB, SNS, CloudWatch wrappers
    ai/                        # LLM provider abstraction + RAG service + assistant routes
    domains/
      inclusion/                # credit scoring engine + service + routes
      fraud/                     # anomaly detection engine + service + routes
      lending/                   # underwriting service + routes
      insurance/                 # parametric claims service + routes
data/
  dummy_datasets/            # JSON seed data for every domain
  generate_dummy_data.py     # seeding script
infra/
  aws/terraform/             # S3, DynamoDB, SNS, CloudWatch, EC2, security group
  gcp_dr/                     # disaster recovery strategy + runbook
  api_gateway/                # OpenAPI spec + setup notes
docker-compose.yml           # local Postgres + Mongo + optional Ollama
requirements.txt
.env.example
```

## What's real vs. reference-only

- **Real, runnable locally**: all app code, dummy datasets, Postgres/Mongo
  integration, fraud ML model, credit scorecard, RAG pipeline (with an
  LLM API key), docker-compose local stack.
- **Reference/production-shaped, needs your cloud account to run**: AWS
  service calls (S3/DynamoDB/SNS/CloudWatch — real boto3 code, wrapped
  in try/except so local dev doesn't break without credentials),
  Terraform, API Gateway config, GCP DR setup.
