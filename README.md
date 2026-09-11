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

## Stack

| Layer | Choice |
|---|---|
| Backend framework | Python 3.11+, BlackSheep (ASGI, async) |
| Relational DB | PostgreSQL (system of record: users, credit scores, transactions, loans, claims) |
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

