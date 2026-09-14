# Sahayak Banking Core

**Financial inclusion, real-time fraud defense, cash-flow lending, and parametric insurance — one backend, four problems that usually need four separate vendors.**

Backend-only system (no frontend) built for India-specific finance-sector
problems: alt-data credit scoring for people with no formal credit
history, real-time transaction fraud detection, cash-flow-based
MSME/personal lending, and parametric (auto-payout) insurance claims —
all sitting behind a shared, multi-provider RAG/LLM assistant layer and a
common security/auth boundary.

**At a glance:**

- **Four finance domains, one codebase** — inclusion, fraud, lending, and
  insurance share a single user record, credit-score history, security
  boundary, and audit trail instead of shipping as separate products.
- **Real-time decisions with a reason attached** — every score, flag,
  approval, and payout is accompanied by an LLM-generated, plain-language
  explanation grounded in the actual signals used, not a black box.
- **Agents propose, humans/code decide** — a shared agent-orchestration
  layer lets AI recommend actions (freeze a card, approve a loan) via a
  propose-then-confirm pattern; it never executes a consequential action
  on its own.
- **No AI vendor lock-in** — six interchangeable LLM backends (Anthropic,
  OpenAI, Gemini, Bedrock, Ollama, LangChain-auto) behind one interface,
  including a fully offline path for low-connectivity bank branches.
- **Security enforced at every layer** — verified JWT on every route,
  per-object ownership checks, per-route rate limiting, and an
  append-style audit log for every security- or finance-relevant action.
- **Built for India-specific signals** — UPI transaction patterns, GST
  data availability, and rainfall/flood-index parametric triggers are
  first-class inputs, not fields borrowed from a different market.

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

## Frequently Asked Questions

### What is this application all about?

Sahayak Banking Core is a backend-only system that answers four of the
hardest problems in Indian retail/MSME finance from one codebase instead
of four separate vendors:

1. **Financial inclusion** — alt-data credit scoring
   (`app/services/domains/inclusion/credit_scoring.py`) for the ~250M+
   Indians with no formal credit file, using proxy signals such as UPI
   transaction regularity, utility-bill payment behavior, mobile-recharge
   patterns, and GST filings, run through a transparent, weighted
   scorecard rather than a black-box model.
2. **Real-time fraud detection** (`app/services/domains/fraud/anomaly_detection.py`)
   — a hybrid engine that runs deterministic rules (high-value transfer to
   a new beneficiary, txn-velocity bursts) alongside an Isolation Forest
   ML model on every transaction, so both known fraud patterns and novel
   ones get caught.
3. **Cash-flow-based lending** (`app/services/domains/lending/service.py`)
   — underwriting that combines the inclusion credit score with
   loan-specific signals (collateral, GST-data availability, bank
   statement history) into an auditable approve/review/reject decision,
   for borrowers who can't produce a traditional credit-bureau file.
4. **Parametric insurance** (`app/services/domains/insurance/service.py`)
   — crop/property claims that auto-trigger a payout the moment an
   external index (rainfall deficit, flood level) crosses a pre-agreed
   threshold, instead of waiting weeks on a manual assessor.

A shared, provider-agnostic RAG assistant and an agentic tool-calling
layer (`app/services/ai/`) sit across all four domains, answering
scheme/policy questions and giving every automated decision a
plain-language explanation grounded in real signals — not a
post-hoc guess.

### How is this application different from a real-time application?

It **is** a real-time application everywhere it counts — fraud checks run
synchronously in the transaction path, credit scoring returns in the same
request/response cycle, and the assistant answers live. What sets it
apart is what happens *around* that real-time core:

- **Explainable at the point of decision, not after.** Every scoring and
  fraud-flagging call also produces an LLM-generated, plain-language
  explanation grounded in the actual signals used, computed inline —
  not bolted on as a separate offline job.
- **Fails safe, never fails hard.** Redis caching and rate limiting are
  explicitly fail-open/soft (an outage degrades performance rather than
  taking the transaction path down), and every AWS call (SNS, CloudWatch,
  DynamoDB) is wrapped so a downstream hiccup never blocks a live fraud
  or lending decision from returning.
- **Auditable after the fact.** A real-time decision that can't be
  reconstructed months later during a regulatory review isn't actually
  production-ready. Every scoring call, fraud evaluation, loan decision,
  claim filing, and agent tool-call trace is written to an append-style
  audit log (`app/core/audit.py`, `COLLECTION_AGENT_AUDIT_LOG`).
- **Agents propose, they never execute.** The agent orchestrator
  (`app/services/ai/agent_orchestrator.py`) enforces a propose-then-confirm
  pattern for anything consequential — freezing a card, approving a loan,
  triggering a payout. An agent can only register a `propose_*` tool; a
  human reviewer confirms the action separately. That's a deliberate
  difference from "real-time AI" systems that let a model act directly.
- **Provider-resilient, not single-vendor-dependent.** The LLM layer
  behind every real-time explanation can fail over across six backends —
  Anthropic, OpenAI, Gemini, Bedrock, Ollama (fully offline), or
  LangChain-auto — so one vendor's outage or rate limit never stalls the
  real-time path.

### Why should an end user choose this application over any other?

- **One coherent platform instead of four disconnected vendors.**
  Inclusion, fraud, lending, and insurance share one user record, one
  credit-score history, one audit trail, and one security boundary — a
  lending decision reads the *same* credit score the inclusion service
  computed, with no integration/reconciliation layer in between.
- **Explainability is structural, not a nice-to-have.** The credit
  scorecard is a transparent weighted model by design, and every
  LLM-generated explanation is grounded in the actual computed
  signals — a genuine fair-lending and dispute-resolution advantage over
  systems that treat "explainability" as an afterthought.
- **Fraud is caught before money leaves, not after.** The hybrid
  rule + Isolation Forest engine evaluates every transaction inline, so a
  suspicious high-value transfer to a new beneficiary can be blocked or
  flagged in the same request that submits it.
- **Insurance payouts arrive in days, not after a field visit.**
  Parametric claims auto-settle the moment an objective external index
  crosses a threshold — and even an auto-triggered claim is still
  screened against the user's recent claim history before it pays out,
  so speed doesn't come at the cost of a basic anomaly check.
- **Security is enforced, not just documented.** Every route requires a
  verified JWT; `ensure_owner_or_role()` checks ownership against the
  token identity rather than trusting a client-supplied `user_ref`; every
  request body is schema-validated; every sensitive action is
  rate-limited and audit-logged.
- **No vendor lock-in on the AI layer.** Six interchangeable LLM
  backends behind one interface, selectable per environment or even
  per-request, including a fully offline path for a bank branch with
  unreliable internet.
- **Built for India's actual constraints.** UPI transaction patterns, GST
  data availability, regional-language RAG support, and rainfall/flood
  parametric triggers are first-class inputs, not generic fields adapted
  from a different market's assumptions.

### How does the fraud engine avoid drowning analysts in false positives?

It doesn't rely on rules alone. Deterministic rules (amount threshold,
new-beneficiary type, txn-velocity) catch known patterns fast and
explainably; the Isolation Forest model is trained on real per-transaction
features (amount, hour, amount-vs-historical-average ratio, new-device
flag) computed against each user's own prior transaction history, so it
can flag novel anomalies that no static rule anticipated. Either signal
alone can trigger a flag, and the result is enriched with an
LLM-generated risk narrative for the analyst reviewing it — a score
without a reason is exactly what this system is built to avoid.

### Can the AI ever approve a loan, freeze a card, or trigger a payout by itself?

No — that's a deliberate boundary in the design, not a missing feature.
Every domain agent (`agent.py` in each `app/services/domains/<domain>`
package) is advisory: it can *propose* an action via a `propose_*` tool,
but the orchestrator flags that result with `requires_confirmation=True`
and a human reviewer has to confirm it separately. Deterministic,
auditable code — not a model — makes the actual call. In a banking
context, an LLM agent that could directly move money or approve credit is
a liability regulators and users are right to be wary of.

### What happens if an LLM provider (or a whole cloud region) goes down?

Two independent layers of resilience handle this:

- **LLM level** — every domain calls a single `get_llm_provider()`
  factory; six interchangeable backends (Anthropic, OpenAI, Gemini,
  Bedrock, Ollama, LangChain-auto) implement the same interface, so a
  provider outage or rate limit is a config change, not a code change —
  and `ollama` keeps the assistant working fully offline.
- **Infrastructure level** — disaster recovery runs across two
  independent, kept-warm secondary sites (GCP and Microsoft Azure) behind
  the AWS primary, so a full AWS region failure has a promotable fallback
  rather than an outage.

### How does Sahayak stop one customer from seeing or acting on another customer's data?

`ensure_owner_or_role()` checks the caller's token identity (`sub`,
`role`) against the `user_ref` the request is acting on for every route —
a `customer` token can only touch its own record; only an explicitly
permitted `analyst`/`admin` token can act cross-user (e.g. reviewing
another user's fraud events). This closes the classic
object-level-authorization gap where a route trusts a client-supplied
`user_ref` at face value instead of the verified token.

### What issues were found in a code review, and have they been fixed?

Yes — a review of the actual source (not just this README) found six
unresolved `git` merge conflicts left inside the application code itself,
plus two related security/correctness bugs. All are now fixed:

- **Unresolved merge-conflict markers in 6 files, which were hard Python
  `SyntaxError`s** — the app could not even start, since `app/main.py`
  imports several of the affected modules at boot. Markers were left in
  `app/config.py`, `app/db/mongo.py`, `app/services/ai/routes.py`,
  `app/services/aws/s3_service.py`,
  `app/services/domains/insurance/routes.py`, and
  `app/services/domains/lending/routes.py`. Each was resolved by
  combining both sides rather than picking one — several conflicts had
  *both* branches contributing functionality the other side depended on
  (e.g. one branch's imports were required by the other branch's function
  body), so a naive one-sided resolution would have swapped one failure
  mode for another (`ImportError`/`NameError` instead of `SyntaxError`).
  All affected files now import and parse cleanly.
- **A refresh token could be used as an access token.**
  `verify_access_token()` in `app/core/security.py` never checked the
  token's `type` claim, so a 14-day-lived refresh token — meant only to
  mint new short-lived access tokens via `/auth/refresh` — worked
  identically to a 60-minute access token if presented directly as a
  Bearer token. Fixed by rejecting any token that carries a `type` claim
  in `verify_access_token()`, since genuine access tokens never set one.
- **A silent encryption regression on the data-lake export path.**
  One side of the `s3_service.py` conflict uploaded batch exports to the
  data-lake bucket with `ServerSideEncryption="aws:kms"`; the other did a
  plain `put_object` with no encryption at rest. The merge kept the
  KMS-encrypted version.
- **A `NameError` bug in `insurance/routes.py`.** One side of that
  conflict called `service.file_claim(user, payload)`, where `payload`
  was never defined in that branch — it would have failed on every
  request. Fixed to use the validated `body.model_dump()`, and the
  claim-filing audit-log call from the other branch was kept rather than
  dropped.
- **A whole endpoint was at risk of being silently deleted.** The
  `/claims/{claim_ref}/triage` route (an advisory claims-triage agent)
  and the `/applications/{application_ref}/agent-review` and
  `/applications/{application_ref}/document-request` routes (advisory
  underwriting-agent endpoints) each existed on only one side of their
  respective conflicts. Resolving toward the other branch would have
  removed them entirely. All three are preserved, alongside the
  request-validation, rate-limiting, and audit-logging that existed on
  the other side of the same conflicts.
