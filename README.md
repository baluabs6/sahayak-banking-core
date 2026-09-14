# Sahayak Banking Core

<<<<<<< HEAD
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

### Request lifecycle (every authenticated call)

1. **Edge** — API Gateway terminates TLS, applies WAF rules and outer
   throttling before a request ever reaches the app process.
2. **Authentication middleware** (`app/core/auth_middleware.py`) — verifies
   the `Authorization: Bearer <jwt>` header on every route except
   `/health`, `/`, `/openapi`, and `/api/v1/auth/*` (you can't present a
   token before you have one). Invalid/missing/expired tokens are
   rejected with `401` before any controller code runs.
3. **Security headers + CORS middleware** — adds `HSTS`,
   `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` to every
   response, and enforces an explicit origin allow-list rather than an
   open CORS policy.
4. **Request validation** — each controller validates the JSON body
   against a Pydantic schema (`schemas.py` per domain) before touching
   business logic: types, bounds (e.g. transaction amounts must be
   positive and capped), and allowed enum values are enforced up front.
5. **Object-level authorization** — `ensure_owner_or_role()` checks that
   the caller's token identity (`sub`, `role`) matches the `user_ref`
   being acted on, or that the caller holds an `analyst`/`admin` role
   explicitly permitted for that route. This is what stops one customer
   from reading or acting on another customer's financial records.
6. **Rate limiting** — a Redis fixed-window counter, keyed on the
   authenticated identity (not a client-suppliable value), caps each
   route independently (assistant, fraud evaluation, credit scoring,
   lending, insurance, and login all have their own limits).
7. **Domain service** — the actual business logic: scoring, fraud
   detection, underwriting decision, or claim adjudication — reads/writes
   PostgreSQL as the system of record, and MongoDB for flexible,
   high-write data (fraud events, KYC metadata, RAG query logs).
8. **Caching** — Redis caches expensive/repeatable results (credit score
   computation + LLM explanation, RAG answers) with short TTLs. All cache
   operations fail **open/soft**: a Redis outage degrades performance
   (falls through to recompute) rather than taking the API down.
9. **Audit log** — `app/core/audit.py` writes an append-style event to
   MongoDB (`audit_logs`) for every security- or finance-relevant action
   — who did what, to whose record, with what outcome — independent of
   the business response, so it can't be skipped by a code path that
   forgets to call it inline with the main logic.
10. **Downstream AWS calls** — SNS alerts (fraud/loan status), CloudWatch
    metrics, DynamoDB velocity counters, and S3 (KYC documents,
    KMS-encrypted at rest) all wrap in try/except so local dev and CI
    never require live AWS credentials to run.

### Why this shape

- **One security boundary, five domains.** Auth, validation, rate
  limiting, and audit logging are implemented once as shared
  middleware/helpers and applied uniformly — a new domain added later
  inherits all of it by following the same controller pattern, instead of
  re-implementing security per-domain.
- **Postgres for truth, Mongo for flexibility, Redis for speed.**
  Relational, ACID-guaranteed data (users, scores, transactions, loans,
  claims) lives in Postgres because it needs joins and consistency.
  Schema-variable, high-write data (fraud-rule-trigger arrays, KYC doc
  metadata that differs per document type, RAG logs, audit logs) lives in
  Mongo because forcing it into rigid relational columns would mean
  constant migrations. Redis touches neither as a system of record — it
  is always reconstructible and safe to flush.
- **Provider-agnostic AI layer.** Every domain calls one
  `get_llm_provider()` factory; six backends (Anthropic, OpenAI, Ollama,
  Gemini, Bedrock, LangChain-auto) implement the same `LLMProvider`
  interface, so switching providers — including to a fully offline model
  for a bank branch with unreliable internet — never touches domain logic.
- **Deployable as one unit or five.** Locally and in this repo, all five
  domains run in a single `uvicorn` process for simplicity. In
  production, each `app/services/domains/<domain>` package is structured
  to be pulled out and deployed independently behind API Gateway (see
  `infra/api_gateway/`), so a fraud-detection traffic spike doesn't need
  to scale the lending service too.

---

## What is this application about?

Sahayak Banking Core is a backend system that answers four of the
hardest, highest-impact problems in Indian retail/MSME finance from one
consistent codebase, instead of four disconnected point solutions:

1. **Who can we lend to, when they have no credit file?** — alt-data
   credit scoring from UPI regularity, bill-payment behavior, and mobile
   recharge patterns, with a transparent, explainable scorecard rather
   than a black box.
2. **Is this transaction fraudulent, right now?** — hybrid rule +
   ML (Isolation Forest) fraud detection running on every transaction,
   with velocity tracking, alerting, and analyst-facing risk narratives.
3. **Should this loan be approved, and on what basis?** — cash-flow-based
   underwriting that combines the inclusion credit score with
   loan-specific signals (GST availability, bank statement history,
   collateral) into an auditable decision.
4. **Why does an insurance claim take weeks to pay out?** — parametric
   insurance that auto-triggers a payout the moment an external index
   (rainfall deficit, flood level) crosses a threshold, instead of
   waiting on a manual assessor.

A shared RAG-grounded assistant sits across all four, answering
scheme/policy questions and explaining automated decisions in plain
language — because a credit or fraud decision nobody can explain isn't
one a regulator, a bank, or a customer should have to accept.

## How is this application different from real-time applications?

It **is** a real-time application in every place that matters — fraud
evaluation happens synchronously on the transaction path, credit scoring
returns a decision in the same request/response cycle, and the assistant
answers Q&A live. The difference is what it does *around* that real-time
core that most real-time fintech demos skip entirely:

- **Real-time, but explainable at the point of decision.** Most real-time
  fraud/credit systems return a score and nothing else. Every scoring
  and fraud-flagging call here also produces a plain-language,
  LLM-generated explanation grounded in the actual signals used — not
  bolted on afterward, but part of the same request.
- **Real-time, but fails safe, not fast-and-broken.** Redis caching and
  rate limiting are explicitly fail-open/soft — a cache or limiter outage
  degrades performance, it never takes the transaction path down. AWS
  service calls (SNS, CloudWatch, DynamoDB) are wrapped so a downstream
  AWS hiccup never blocks a fraud decision from returning to the caller.
- **Real-time, but auditable after the fact.** A live decision that
  can't be reconstructed six months later during a dispute or regulatory
  review isn't actually production-ready, however fast it responds. Every
  scoring call, fraud evaluation, loan decision, and claim filing writes
  an audit event capturing who acted, on whose behalf, and what happened.
- **Real-time, but provider-resilient.** The LLM layer behind every
  "real-time explanation" can fail over across six backends — including a
  fully offline model — so a single vendor's outage or rate limit doesn't
  stop the real-time path from completing.

## How is it useful for the end user, and how will it help them?

- **People with no formal credit history stop being invisible to
  lenders.** A gig worker, street vendor, or small farmer with a phone
  and a UPI habit — but no CIBIL file — gets a real, explainable credit
  score instead of an automatic rejection for "insufficient credit
  history."
- **Fraud gets caught before money leaves the account, not after.** The
  hybrid rule + ML engine evaluates a transaction inline, meaning a
  suspicious high-value transfer to a new beneficiary can be blocked or
  flagged in the same request that submits it — not discovered in a
  monthly statement review.
- **Loan and claim decisions come with a reason, not a black box.** Every
  automated score, approval, rejection, and payout trigger is
  accompanied by a plain-language explanation the end user can actually
  read and act on — a fair-lending requirement, not a nicety.
- **Insurance payouts arrive in days, not after a manual assessor visits
  a flooded field.** Parametric claims settle automatically the moment
  an objective external index crosses a pre-agreed threshold, which is
  exactly the gap that causes the most-cited complaint about crop
  insurance in India today: slow, disputed manual assessment.
- **Support works in the user's own language, grounded in real policy
  documents.** The assistant answers scheme and product questions from
  actual RBI/scheme text (RAG-grounded, not free-form generation), and
  the offline (Ollama) LLM path means this can run in a bank branch with
  poor connectivity without sending customer queries to a third-party
  API.

## Why would a user choose this over another application? What makes it different?

- **One coherent platform instead of four disconnected vendors.**
  Inclusion, fraud, lending, and insurance usually ship as separate
  products from separate vendors with separate data models. Here they
  share one user record, one credit-score history, one audit trail, and
  one security boundary — a lending decision can directly reference the
  same credit score the inclusion service computed, with no
  integration/reconciliation layer in between.
- **Explainability is structural, not an afterthought.** The credit
  scorecard is a transparent weighted model by design (not a black-box
  model swapped in for a marginal accuracy gain), and every LLM-generated
  explanation is grounded in the actual computed signals — a genuine
  fair-lending and dispute-resolution advantage over systems that treat
  "explainability" as a nice-to-have.
- **No vendor lock-in on the AI layer.** Six interchangeable LLM
  backends behind one interface, selectable per environment or even
  per-request, including a fully offline path — most fintech AI features
  are hard-wired to a single provider and go down (or become
  unaffordable) when that provider's pricing or availability changes.
- **Security is enforced, not just documented.** Every route requires a
  verified JWT; ownership of the record being acted on is checked against
  the token identity, not trusted from the request body; every request
  body is schema-validated; every sensitive action is rate-limited and
  audit-logged. That's the difference between a system that's *described*
  as secure and one where broken access control isn't just possible by
  omission.
- **Built for India's actual constraints, not adapted from a US
  template.** UPI transaction patterns, GST data availability, Aadhaar
  linkage, regional-language support, and rainfall/flood-index parametric
  triggers are first-class inputs — not generic "transaction data" and
  "credit bureau score" fields borrowed from a different market's
  assumptions.

---
=======
**Sahayak** ("helper" in Hindi) is a backend-only system (no frontend)
that addresses four India-specific financial-services problems from one
codebase — instead of four separate point solutions — plus an agentic
AI layer that sits across all of them:

1. **Financial inclusion** for the ~250M+ Indians with no formal credit
   history, via alt-data credit scoring.
2. **Real-time fraud detection** for UPI/NEFT/IMPS-era transaction volumes.
3. **Cash-flow-based MSME/personal lending** for borrowers who can't
   produce traditional collateral or credit-bureau history.
4. **Parametric insurance** that pays out on an index crossing a
   threshold, instead of a slow manual claims process.

## How this differs from a typical fintech backend, in practice

Most banking-adjacent backends in the wild fall into one of two shapes:
a single-product API (just fraud, just KYC, just a lending calculator),
or a monolith where "AI" means one bolted-on chatbot endpoint. Sahayak
is architected differently on three specific points:

- **The credit-scoring, lending, and fraud domains share signals, not
  just a database.** A lending decision (`lending/service.py`) reads
  the *same* inclusion credit score used for a customer-facing "check my
  score" call, and the fraud engine's velocity counters are consulted by
  both the fraud domain and the lending underwriting agent — so "does
  this person look risky" is answered consistently everywhere it's
  asked, rather than each domain rolling its own risk logic that can
  quietly disagree with the others.
- **Every AI-driven decision is explainable by construction, not by a
  post-hoc summary.** The credit scorecard (`credit_scoring.py`) is a
  transparent, inspectable weighted formula — not a black-box model —
  specifically so `explain_credit_decision` and the lending rationale
  agent can cite the *actual* factors that drove a number, satisfying
  RBI-style fair-lending explainability. Most systems that bolt an LLM
  onto a black-box score can only ever produce a plausible-sounding
  explanation, not a true one.
- **Agents recommend; deterministic code decides.** Every agent in this
  codebase is advisory — it proposes, and either
  a human or a fixed, auditable rule makes the actual call. This is a
  deliberate choice for a banking context: an LLM agent that could
  directly approve a loan or freeze an account is a liability regulators
  and users are right to be nervous about; one that drafts a
  recommendation with its full reasoning trail logged for audit is not.

## Architecture

```
                        ┌─────────────────────────────────────────┐
                        │              Clients (API)               │
                        └───────────────────┬───────────────────────┘
                                             │  HTTPS (API Gateway in prod
                                             │  — see infra/api_gateway)
                        ┌───────────────────▼───────────────────────┐
                        │        BlackSheep ASGI app (app/main.py)   │
                        │   one process, 5 domain controllers        │
                        └──┬───────┬───────┬───────┬───────┬─────────┘
                           │       │       │       │       │
                 ┌─────────▼─┐ ┌───▼────┐ ┌▼───────┐ ┌────▼─────┐ ┌───▼──────────┐
                 │ Inclusion │ │ Fraud  │ │Lending │ │Insurance │ │  Assistant   │
                 │  service  │ │service │ │service │ │ service  │ │ (RAG+agent)  │
                 └─────┬─────┘ └───┬────┘ └───┬────┘ └────┬─────┘ └──────┬───────┘
                       │           │          │           │              │
        ┌──────────────┼───────────┼──────────┼───────────┼──────────────┤
        │              │           │          │           │              │
   ┌────▼────┐   ┌─────▼─────┐┌────▼────┐┌────▼─────┐┌────▼─────┐ ┌──────▼───────┐
   │ Postgres│   │  MongoDB  ││  Redis  ││   AWS    ││  RAG /   │ │Agent          │
   │ (system │   │(KYC docs, ││ (cache +││(S3/Dynamo││  LLM     │ │Orchestrator   │
   │of record│   │fraud logs,││rate     ││DB/SNS/   ││ provider │ │(tool-calling, │
   │users,   │   │agent audit││limit)   ││CloudWatch││ layer    │ │ propose-then- │
   │scores,  │   │  trail)   │└─────────┘└──────────┘└──────────┘ │ confirm)      │
   │loans,   │   └───────────┘                                    └───────┬───────┘
   │claims,  │                                                            │
   │txns)    │                                          each domain agent │
   └─────────┘                                          registers its own │
                                                          read-only tools ─┘

DR (cross-cloud, both kept warm, promoted independently — see infra/):
   AWS (primary, ap-south-1) ──sync──▶ GCP (asia-south1) ──sync──▶ Azure (centralindia)
```

Every domain follows the same three-file shape:
`service.py` (deterministic business logic), `agent.py` (advisory
AI reasoning on top, where one exists), `routes.py` (HTTP surface).
>>>>>>> 3646832acdd6b8b99d0b0da0a8bec52147ac1cdf

## Stack

| Layer | Choice |
|---|---|
| Backend framework | Python 3.11+, BlackSheep (ASGI, async) |
| Relational DB | PostgreSQL (system of record: users, credit scores, transactions, loans, claims) |
<<<<<<< HEAD
| NoSQL DB | MongoDB (KYC document metadata, fraud event logs, RAG query logs, audit logs) |
| Cache | Redis (credit score + RAG answer caching, per-route rate limiting) |
| Cloud | AWS — S3, DynamoDB, SNS, CloudWatch, EC2, API Gateway |
| Bulk migration | AWS Snowball (one-time historical data onboarding only — see `infra/aws/terraform/snowball_note.md`) |
| DR | GCP (Cloud SQL, Compute Engine, Cloud Storage, Cloud Run) as secondary site |
| AI/ML | RAG (LangChain + FAISS, optional hybrid BM25+dense retrieval), LLMs (Anthropic / OpenAI / Ollama / Gemini / Bedrock / LangChain-agnostic), scikit-learn (fraud anomaly detection), rule-based scorecard (credit scoring) |
| Auth | JWT (access + refresh tokens), role-based access (customer / analyst / admin), OTP login stub (swap for a real SMS provider / Cognito before production) |

## Domains

1. **Auth** (`app/services/domains/auth`) — OTP-based login stub, refresh
   tokens, and a bootstrap-key-gated internal-token flow for
   analyst/admin service accounts.
2. **Inclusion** (`app/services/domains/inclusion`) — alt-data credit
   scoring for users with no formal credit history, using UPI
   regularity, bill-payment behavior, and mobile recharge patterns.
   Transparent weighted scorecard + LLM-generated plain-language
   explanation (fair-lending requirement).
3. **Fraud** (`app/services/domains/fraud`) — hybrid rule-based +
   Isolation Forest anomaly detection on every transaction, with
   DynamoDB velocity counters, SNS alerts, CloudWatch metrics, Mongo
   event logging, and an LLM-generated risk narrative for analysts.
4. **Lending** (`app/services/domains/lending`) — cash-flow-based
   underwriting combining the inclusion credit score with loan-specific
   factors into an approve/review/reject decision.
5. **Insurance** (`app/services/domains/insurance`) — parametric claims:
   auto-triggers payout for crop/property claims when an external index
   (rainfall deficit, flood index) crosses a threshold.
6. **Assistant** (`app/services/ai`) — RAG-grounded Q&A over
   RBI/scheme documents, provider-agnostic across six LLM backends.

## Security & Auth

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
- **Not yet production-ready:** `verify_login_otp()` is a clearly-marked
  stub that only works in `ENVIRONMENT=local`; wire it to a real SMS OTP
  provider or an IdP (e.g. AWS Cognito) before this touches real users.

## Local setup

```bash
cp .env.example .env          # fill in AWS/LLM keys as available; local DBs work out of the box
docker-compose up -d          # starts Postgres, MongoDB, Redis, and (optional) Ollama
pip install -r requirements.txt --break-system-packages
python -m data.generate_dummy_data   # seeds Postgres + Mongo with dummy data
uvicorn app.main:app --reload --port 8000
```

Get a token (local dev OTP is `000000` by default — `DEV_FALLBACK_OTP`):
```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"user_ref": "USR-1001", "otp": "000000"}'
# -> { "access_token": "...", "refresh_token": "...", "token_type": "Bearer" }
```

Then, with `TOKEN` set to the returned `access_token`:
```bash
curl -X POST http://localhost:8000/api/v1/inclusion/credit-score \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"user_ref": "USR-1001", "alt_data": {"avg_monthly_upi_txn_count": 22, "avg_monthly_upi_volume_inr": 8100, "utility_bill_ontime_ratio": 0.92, "mobile_recharge_regularity_score": 0.81, "months_of_transaction_history": 14, "existing_loan_count": 1, "existing_loan_default_flag": false}}'

curl -X POST http://localhost:8000/api/v1/fraud/transactions/evaluate \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"user_ref": "USR-1001", "amount_inr": 48000, "txn_type": "NEFT_OUT", "merchant": "New Beneficiary", "location": "Bengaluru, KA", "device_id": "DEV-NEW1"}'

curl -X POST http://localhost:8000/api/v1/assistant/ask \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query": "How does crop insurance get paid out faster?", "llm_provider": "anthropic"}'
```

Note: a `customer` token's `sub` must match the `user_ref` in the request
body (see `ensure_owner_or_role`), so use the `user_ref` you logged in as.
For cross-user analyst actions (e.g. `/fraud/events/{user_ref}`), issue an
analyst token via `/api/v1/auth/internal-token` with
`INTERNAL_ADMIN_BOOTSTRAP_KEY` set in `.env`.

## Choosing the LLM backend

Set `LLM_PROVIDER` in `.env` to one of `anthropic`, `openai`, `ollama`,
`gemini`, `bedrock`, or `langchain_auto` — every domain service goes
through `app/services/ai/llm_provider.get_llm_provider()`, so nothing
else in the codebase needs to change. Any request can also override the
provider per-call via `"llm_provider": "..."` in the JSON body.

- `anthropic` / `openai` / `gemini` — hosted APIs, need an API key.
- `bedrock` — routes through AWS Bedrock, keeping the LLM call inside the
  same AWS account/VPC/IAM boundary as the rest of the app's AWS usage.
- `ollama` — fully local/offline model (useful for a bank-branch
  deployment with no reliable internet, or for keeping sensitive prompts
  off any third-party API).
- `langchain_auto` — LangChain's own chat-model abstraction, picks the
  first available credential; use this when you want LangChain itself
  (not this app's config) to own provider selection/fallback logic.

Set `USE_HYBRID_RETRIEVAL=true` to combine dense (FAISS) and sparse
(BM25) retrieval via LangChain's `EnsembleRetriever` — improves recall on
RBI-circular/scheme text where exact terms (scheme names, section
numbers) matter as much as semantic similarity.

## Project layout

```
app/
  main.py                    # entrypoint: mounts controllers, auth/CORS/security-header middleware
  config.py                   # env-driven settings + production config guard
  db/                          # Postgres (SQLAlchemy) + Mongo (Motor) + Redis connections
  models/                      # ORM models (Postgres) + Pydantic schemas (Mongo)
  core/                         # JWT auth, auth middleware, audit logging, structured logging
  services/
    aws/                        # S3, DynamoDB, SNS, CloudWatch wrappers
    ai/                          # LLM provider abstraction + RAG service + assistant routes
    domains/
      auth/                        # login/refresh/internal-token issuance
      inclusion/                    # credit scoring engine + service + routes + schemas
      fraud/                         # anomaly detection engine + service + routes + schemas
      lending/                       # underwriting service + routes + schemas
      insurance/                     # parametric claims service + routes + schemas
data/
  dummy_datasets/              # JSON seed data for every domain
  generate_dummy_data.py       # seeding script
infra/
  aws/terraform/               # S3, DynamoDB, SNS, CloudWatch, EC2, security group
  gcp_dr/                       # disaster recovery strategy + runbook
  api_gateway/                  # OpenAPI spec + setup notes
docker-compose.yml             # local Postgres + Mongo + optional Ollama
requirements.txt
.env.example
```

## What's real vs. reference-only

- **Real, runnable locally**: all app code, dummy datasets,
  Postgres/Mongo integration, JWT auth + ownership checks, per-route rate
  limiting, audit logging, the fraud ML model, the credit scorecard, and
  the RAG pipeline (with an LLM API key).
- **Reference/production-shaped, needs your cloud account to run**: AWS
  service calls (S3/DynamoDB/SNS/CloudWatch — real boto3 code, wrapped in
  try/except so local dev doesn't break without credentials), Terraform,
  API Gateway config, GCP DR setup, AWS Bedrock provider.
- **Explicitly a stub, must be replaced before production**:
  `verify_login_otp()` (fixed dev OTP only) — wire to a real SMS OTP
  provider or IdP before any real user logs in.
=======
| NoSQL DB | MongoDB (KYC document metadata, fraud event logs, RAG query logs, agent audit trail) |
| Cache | Redis (credit score + RAG answer caching, assistant-route rate limiting) |
| Cloud | AWS — S3, DynamoDB, SNS, CloudWatch, EC2, API Gateway |
| Bulk migration | AWS Snowball (one-time historical data onboarding only — see `infra/aws/terraform/snowball_note.md`) |
| DR | Two independent secondary sites, kept warm: **GCP** (Cloud SQL, GKE, Cloud Storage, Cloud VPN) and **Microsoft Azure** (AKS, Cosmos DB, Blob Storage, VMs, VNet) |
| AI/ML | RAG (LangChain + FAISS), LLMs (Anthropic Claude / OpenAI / Ollama / LangChain-agnostic), scikit-learn (fraud anomaly detection), rule-based scorecard (credit scoring), Anthropic tool-calling (agent orchestration) |

## Why two databases

- **PostgreSQL** for anything transactional/relational that needs ACID
  guarantees and joins: user records, credit score history, loan
  applications, insurance claims, transaction ledger.
- **MongoDB** for flexible, high-write, less-structured data: KYC
  document metadata (varies by doc type), fraud event logs (variable
  rule-trigger arrays), RAG query/response logs, and the full tool-call
  audit trail for every agent run.
- **Redis** as a pure cache in front of the above two — never the
  source of truth, always reconstructible, safe to flush.

>>>>>>> 3646832acdd6b8b99d0b0da0a8bec52147ac1cdf
