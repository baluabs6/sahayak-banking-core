# Sahayak Banking Core

**Financial inclusion, real-time fraud defense, cash-flow lending, and parametric insurance — one backend, four problems that usually need four separate vendors.**

Backend-only system (no frontend) built for India-specific finance-sector
problems: alt-data credit scoring for people with no formal credit
history, real-time transaction fraud detection, cash-flow-based
MSME/personal lending, and parametric (auto-payout) insurance claims —
all sitting behind a shared, multi-provider RAG/LLM assistant layer and a
common security/auth boundary.

---

## Application Architecture

### High-level shape

```
                                   ┌─────────────────────────┐
                                   │   API Gateway (AWS)      │
                                   │  WAF + outer throttling  │
                                   └────────────┬─────────────┘
                                                │  HTTPS
                                   ┌────────────▼─────────────┐
                                   │   BlackSheep ASGI app     │
                                   │      (app/main.py)        │
                                   │                            │
                                   │  ┌──────────────────────┐ │
                                   │  │ auth_middleware        │ │  ← verifies JWT on every
                                   │  │ (JWT verify + identity)│ │     non-public route
                                   │  └──────────┬───────────┘ │
                                   │  ┌──────────▼───────────┐ │
                                   │  │ security_headers +    │ │  ← HSTS, nosniff,
                                   │  │ CORS middleware        │ │     frame-deny, CORS allow-list
                                   │  └──────────┬───────────┘ │
                                   └─────────────┼─────────────┘
                                                 │
        ┌───────────────┬───────────────┬───────┼───────────┬──────────────┬───────────────┐
        ▼               ▼               ▼       ▼           ▼              ▼               ▼
   /api/v1/auth   /api/v1/inclusion /api/v1/fraud   /api/v1/lending /api/v1/insurance /api/v1/assistant
   (login, refresh, (credit scoring)  (txn evaluate,  (underwriting)  (parametric      (RAG-grounded
    internal-token)                    events review)                  claims)          Q&A)
        │               │               │               │              │               │
        │               │  each controller: Pydantic request validation │               │
        │               │  → ownership/role check (ensure_owner_or_role) │               │
        │               │  → Redis rate limit → domain service → audit log             │
        └───────┬───────┴───────┬───────┴───────┬───────┴──────┬───────┴───────┬────────┘
                │               │               │               │              │
                ▼               ▼               ▼               ▼              ▼
        ┌──────────────────────────────────────────────────────────────────────────┐
        │                          Domain services layer                             │
        │  InclusionService  FraudService  LendingService  InsuranceService  RAGService │
        └──────┬──────────────┬───────────────┬───────────────┬────────────────┬────┘
               │              │               │               │                │
     ┌─────────▼───┐  ┌───────▼──────┐ ┌──────▼──────┐  ┌─────▼──────┐  ┌──────▼───────┐
     │ PostgreSQL   │  │ MongoDB      │ │ Redis        │  │ AWS         │  │ LLM providers │
     │ (system of   │  │ (KYC meta,   │ │ (cache +     │  │ S3 / DynamoDB│  │ Anthropic /   │
     │ record: users,│  │ fraud events,│ │ rate limit,  │  │ SNS / Cloud- │  │ OpenAI /      │
     │ scores, txns, │  │ RAG logs,    │ │ fail-open/   │  │ Watch        │  │ Ollama /      │
     │ loans, claims)│  │ audit logs)  │ │ soft on error)│ │              │  │ Gemini /      │
     └───────────────┘  └──────────────┘ └───────────────┘  └─────────────┘  │ Bedrock /     │
                                                                                │ LangChain-auto│
                                                                                └───────────────┘
```

## Stack

| Layer | Choice |
|---|---|
| Backend framework | Python 3.11+, BlackSheep (ASGI, async) |
| Relational DB | PostgreSQL (system of record: users, credit scores, transactions, loans, claims) |
| NoSQL DB | MongoDB (KYC document metadata, fraud event logs, RAG query logs, audit logs) |
| Cache | Redis (credit score + RAG answer caching, per-route rate limiting) |
| Cloud | AWS — S3, DynamoDB, SNS, CloudWatch, EC2, API Gateway |
| Bulk migration | AWS Snowball (one-time historical data onboarding only — see `infra/aws/terraform/snowball_note.md`) |
| DR | GCP (Cloud SQL, Compute Engine, Cloud Storage, Cloud Run) as secondary site |
| AI/ML | RAG (LangChain + FAISS, optional hybrid BM25+dense retrieval), LLMs (Anthropic / OpenAI / Ollama / Gemini / Bedrock / LangChain-agnostic), scikit-learn (fraud anomaly detection), rule-based scorecard (credit scoring) |
| Auth | JWT (access + refresh tokens), role-based access (customer / analyst / admin), OTP login stub (swap for a real SMS provider / Cognito before production) |

### Security & Auth

- Every non-public route requires a verified `Authorization: Bearer`
  JWT (`app/core/auth_middleware.py`).
- `ensure_owner_or_role()` enforces that a `customer` token can only act
  on its own `user_ref`; `analyst`/`admin` tokens are explicitly
  permitted per-route (e.g. fraud-event review), closing the
  object-level-authorization gap that existed when routes trusted a
  client-supplied `user_ref` directly.
- All request bodies are validated against Pydantic schemas
  (`schemas.py` in each domain) — types, bounds, and enums enforced
  before business logic runs.
- Every domain route is rate-limited per authenticated identity via
  Redis (`app/db/redis_client.py::check_rate_limit`), not just the
  assistant endpoint.
- `app/config.py::validate_production_config` refuses to start the app
  in `staging`/`production` with a default/weak `JWT_SECRET` or a
  missing `INTERNAL_ADMIN_BOOTSTRAP_KEY`.
- Security- and finance-relevant actions write to an append-style audit
  log in MongoDB (`app/core/audit.py`) — auth issuance/denial, credit
  score computation, fraud evaluation, loan decisions, claim filings.
