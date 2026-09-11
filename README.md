# Sahayak Banking Core — Financial Inclusion, Fraud, Lending & Insurance

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
  codebase (see "AI Agents" below) is advisory — it proposes, and either
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

## Stack

| Layer | Choice |
|---|---|
| Backend framework | Python 3.11+, BlackSheep (ASGI, async) |
| Relational DB | PostgreSQL (system of record: users, credit scores, transactions, loans, claims) |
| NoSQL DB | MongoDB (KYC document metadata, fraud event logs, RAG query logs, agent audit trail) |
| Cache | Redis (credit score + RAG answer caching, assistant-route rate limiting) |
| Cloud | AWS — S3, DynamoDB, SNS, CloudWatch, EC2, API Gateway |
| Bulk migration | AWS Snowball (one-time historical data onboarding only — see `infra/aws/terraform/snowball_note.md`) |
| DR | Two independent secondary sites, kept warm: **GCP** (Cloud SQL, GKE, Cloud Storage, Cloud VPN) and **Microsoft Azure** (AKS, Cosmos DB, Blob Storage, VMs, VNet) — see "Disaster Recovery" below |
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
   returning HTTP 429 over the limit. This matters more now that `/ask`
   defaults to agentic tool-calling (multiple LLM round-trips per
   request, not one) — see "AI Agents" below.

All Redis calls fail open/soft: if Redis is unreachable, cache reads
return a miss (falls through to recompute) and the rate limiter allows
the request — a cache outage degrades performance, it never takes the
API down.

## Domains

1. **Inclusion** (`app/services/domains/inclusion`) — alt-data credit
   scoring for users with no formal credit history, using UPI
   regularity, bill-payment behavior, and mobile recharge patterns.
   Transparent weighted scorecard + LLM-generated plain-language
   explanation (fair-lending requirement), plus a **credit-improvement
   coach agent** (`agent.py`) that turns the score's factor breakdown
   into specific, actionable next steps.

2. **Fraud** (`app/services/domains/fraud`) — hybrid rule-based +
   Isolation Forest anomaly detection on every transaction (calibrated
   against the training data's actual score distribution, not a fixed
   constant), with DynamoDB velocity counters that actually feed the
   decision, SNS alerts, CloudWatch metrics, Mongo event logging, an
   LLM-generated risk narrative for analysts, and an **investigator
   agent** (`agent.py`) that autonomously pulls velocity/history/profile
   for a flagged transaction and proposes freeze/step-up/clear for a
   human to confirm.

3. **Lending** (`app/services/domains/lending`) — cash-flow-based
   underwriting combining the inclusion credit score with collateral,
   GST-data availability, and bank-statement history into
   approve/review/reject, plus an **autonomous underwriting agent**
   (`agent.py`) for borderline cases that reasons over the applicant's
   score/velocity/loan-history via explicit tool calls, an RBI-style
   rationale generator for every decision, and a document-collection
   agent that drafts requests for missing KYC/income docs.

4. **Insurance** (`app/services/domains/insurance`) — parametric
   claims: auto-triggers payout for crop/property claims when an
   external index (rainfall deficit, flood index) crosses a threshold,
   now gated by an **anomalous-claim check** before any auto-payout,
   plus a **claims triage agent** (`agent.py`) for non-parametric claims
   that drafts a recommended settlement grounded in policy documents via
   RAG.

5. **Assistant** (`app/services/ai`) — RAG-grounded Q&A over
   RBI/scheme documents, provider-agnostic across 4 LLM backends, now a
   **multi-tool agent** by default: given a `user_ref`, it can also
   check that user's own credit score, loan/claim status, or explain a
   fraud flag — routing to domain tools instead of only static
   documents. See `assistant_agent.py`.

## AI Agents

Every agent in this codebase is built on a shared, deliberately small
tool-calling harness: `app/services/ai/agent_orchestrator.py`. Design
choices that apply to all of them:

- **Propose-then-confirm, not auto-execute.** Any agent whose job touches
  a real action (freezing a card, approving a loan, paying a claim)
  only ever calls a `propose_*` tool that echoes the recommendation back
  — it never calls a tool that actually performs the action. A human (or
  the existing deterministic `_decide`/`_should_auto_trigger` logic)
  makes the real call.
- **Full audit trail.** Every agent run's complete tool-call trace is
  logged to MongoDB (`COLLECTION_AGENT_AUDIT_LOG`) via
  `log_agent_run()` — what the agent looked up, what it got back, and
  what it concluded, for after-the-fact review.
- **Cost/latency control.** Tool-calling loops multiply LLM calls;
  agent-driven endpoints are opt-in or clearly separated from the fast
  deterministic path (e.g. `POST /lending/applications` stays a single
  fast call unless `"explain": true`; agent review is a separate
  endpoint called only for `under_review` applications).
- **Anthropic tool-calling specifically**, not the provider-agnostic
  `LLMProvider.complete()` used for plain narrative generation — see the
  docstring in `agent_orchestrator.py` for why, and how to extend it to
  other providers if needed.

| Agent | Where | Endpoint |
|---|---|---|
| Autonomous underwriting agent | `lending/agent.py` | `POST /api/v1/lending/applications/{ref}/agent-review` |
| Lending decision rationale | `lending/agent.py` | `"explain": true` on `POST /api/v1/lending/applications` |
| Document-collection agent | `lending/agent.py` | `POST /api/v1/lending/applications/{ref}/document-request` |
| Fraud investigator agent | `fraud/agent.py` | `POST /api/v1/fraud/investigate/{user_ref}` |
| Fraud case-file drafting | `fraud/agent.py` | `POST /api/v1/fraud/case-file/{user_ref}` |
| Insurance claims triage agent | `insurance/agent.py` | `POST /api/v1/insurance/claims/{ref}/triage` |
| Insurance anomalous-claim check | `insurance/agent.py` | automatic, inside `POST /api/v1/insurance/claims` |
| Credit-improvement coach | `inclusion/agent.py` | `POST /api/v1/inclusion/users/{user_ref}/coach` |
| Multi-tool assistant | `ai/assistant_agent.py` | `POST /api/v1/assistant/ask` (default; `"agent_mode": false` for plain RAG) |

## Disaster Recovery

Two independent secondary sites, so a correlated failure hitting AWS
*and* one DR cloud still leaves a path to recovery:

- **GCP** (`infra/gcp_dr/`) — Cloud SQL standby, GCS buckets, GKE,
  Cloud VPN back to AWS, Secret Manager. Promoted first if AWS alone is
  down.
- **Microsoft Azure** (`infra/azure_dr/`) — AKS, Cosmos DB (MongoDB
  API), Blob Storage, a fallback VM, VNet with a Site-to-Site VPN back
  to AWS, Key Vault. Promoted if GCP is *also* degraded — see
  `infra/azure_dr/README.md` for the full failover runbook and
  RPO/RTO targets.

Both stacks are real, provisionable Terraform (not documentation-only),
kept warm at minimal scale, and held to the same RPO/RTO bar so either
can be promoted without a degraded fallback. Which site is currently
promoted is tracked via `DR_ACTIVE_SITE` in config.

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

curl -X POST http://localhost:8000/api/v1/inclusion/users/USR-1001/coach

curl -X POST http://localhost:8000/api/v1/fraud/transactions/evaluate \
  -H "Content-Type: application/json" \
  -d '{"user_ref": "USR-1003", "amount_inr": 48000, "txn_type": "NEFT_OUT", "merchant": "New Beneficiary", "location": "Bengaluru, KA", "device_id": "DEV-NEW1"}'

curl -X POST http://localhost:8000/api/v1/lending/applications \
  -H "Content-Type: application/json" \
  -d '{"user_ref": "USR-1005", "loan_type": "msme_working_capital", "requested_amount_inr": 300000, "collateral_provided": false, "gst_data_available": true, "bank_statement_months_provided": 4, "explain": true}'

curl -X POST http://localhost:8000/api/v1/assistant/ask \
  -H "Content-Type: application/json" \
  -d '{"query": "How does crop insurance get paid out faster?", "llm_provider": "anthropic"}'

# Agentic, account-aware assistant (unlocks personal tools via user_ref):
curl -X POST http://localhost:8000/api/v1/assistant/ask \
  -H "Content-Type: application/json" \
  -d '{"query": "What is my credit score and why?", "user_ref": "USR-1001"}'
```

## Choosing the LLM backend

Set `LLM_PROVIDER` in `.env` to one of `anthropic`, `openai`, `ollama`,
or `langchain_auto` — every domain service goes through
`app/services/ai/llm_provider.get_llm_provider()`, so nothing else in
the codebase needs to change. Any request can also override the
provider per-call via `"llm_provider": "..."` in the JSON body (see
the `/assistant/ask`, `/inclusion/credit-score` endpoints). Note: the
tool-calling **agents** (the table above) currently talk to Anthropic
directly regardless of this setting — see the docstring in
`agent_orchestrator.py` for why and how to extend it.

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
  main.py                    # entrypoint, mounts all domain controllers
  config.py                  # env-driven settings (single source of truth)
  db/                        # Postgres (SQLAlchemy) + Mongo (Motor) + Redis connections
  models/                    # ORM models (Postgres) + Pydantic schemas (Mongo)
  core/                      # JWT auth, structured logging
  services/
    aws/                     # S3, DynamoDB, SNS, CloudWatch wrappers (cached singleton clients)
    ai/                      # LLM provider abstraction, RAG service, agent orchestrator,
                              # multi-tool assistant agent, assistant routes
    domains/
      inclusion/             # credit scoring engine + service + coach agent + routes
      fraud/                 # anomaly detection engine + service + investigator agent + routes
      lending/                # underwriting service + underwriting/rationale/doc agent + routes
      insurance/               # parametric claims service + triage/anomaly agent + routes
data/
  dummy_datasets/            # JSON seed data for every domain
  generate_dummy_data.py     # seeding script
infra/
  aws/terraform/             # S3, DynamoDB, SNS, CloudWatch, EC2, security group
  gcp_dr/terraform/          # GCP DR: Cloud SQL, GCS, GKE, Cloud VPN, Secret Manager
  azure_dr/terraform/        # Azure DR: AKS, Cosmos DB, Blob Storage, VM, VNet, VPN gateway
  api_gateway/                # OpenAPI spec + setup notes
docker-compose.yml            # local Postgres + Mongo + optional Ollama
requirements.txt
.env.example
```

## What's real vs. reference-only

- **Real, runnable locally**: all app code (including every agent —
  they'll run end-to-end with a real `ANTHROPIC_API_KEY`), dummy
  datasets, Postgres/Mongo integration, fraud ML model, credit
  scorecard, RAG pipeline, docker-compose local stack.
- **Reference/production-shaped, needs your cloud account to run**: AWS
  service calls (S3/DynamoDB/SNS/CloudWatch — real boto3 code, wrapped
  in try/except so local dev doesn't break without credentials),
  Terraform for AWS/GCP DR/Azure DR, API Gateway config.

## Secrets & configuration

No credential ever ships with a real value in this repo — `.env.example`,
`docker-compose.yml`, and every default in `config.py` use
`****************************` placeholders or require an explicit env
var (`docker-compose.yml`'s Postgres credentials will refuse to start
the container until you set them in a local, untracked `.env`). The app
also refuses to boot with the placeholder JWT secret outside
`ENVIRONMENT=local` — see `get_settings()` in `app/config.py`.
