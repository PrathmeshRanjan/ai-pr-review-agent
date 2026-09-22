# AI Pull Request Review Agent

An automated code review system built as a multi-agent pipeline. When a pull request event arrives via GitHub webhook, the system fans out analysis across four specialized sub-agents running in parallel: security, code quality, test coverage, and documentation. Each agent reasons over the diff together with repository context retrieved via semantic vector search. An arbitration layer aggregates agent findings using confidence-weighted voting, enforces safety thresholds, and publishes structured review comments directly to the pull request.

---

## Architectural Highlights

- **Parallel Specialist Reasoners**: Deconstructs code review into four independent domain analyzers (Security, Code Quality, Test Coverage, Documentation) instead of relying on a monolithic prompt.
- **Unified PostgreSQL Data Layer**: Consolidates relational data, semantic embeddings (`pgvector` DiskANN/HNSW), and time-series operational telemetry (`timescaledb` hypertables) into a single database engine.
- **RAG Codebase Grounding**: Indexes repository source code to inject cross-file dependencies and existing conventions into agent context windows, preventing diff evaluation in isolation.
- **Transparent Human-in-the-Loop (HITL)**: When agent confidence drops below threshold or critical security flags are raised, the system records the review in a triage queue while immediately publishing an informational advisory comment on the pull request with a human verification warning banner.
- **Deterministic Sandboxing**: Validates syntax and AST integrity inside a restricted subprocess execution environment with command allowlists and byte caps, keeping untrusted code away from host runtimes.
- **Financial Controls (FinOps)**: Hard daily spend limits via `BudgetGuard`, token attribution per agent/model, and complexity-tiered routing recommendations to prevent cost runaways.
- **Reliability & Idempotency**: Distributed Redis locks prevent duplicate review runs from webhook retries; circuit breakers prevent cascading failures when external LLM providers degrade.

---

## Architecture Overview

```
GitHub Pull Request Webhook
            │
            ▼
FastAPI Ingress (HMAC SHA-256 Signature Verification + Redis Distributed Lock)
            │
            ▼ Enqueue Review Job
ARQ Background Worker Pool
            │
            ▼ LangGraph Multi-Agent Orchestrator
  ┌─────────────────────────────────────────────────────────┐
  │ 1. Context Retrieval (pgvector semantic search)         │
  │ 2. Security Screening Gate (Threat model assessment)    │
  │ 3. Parallel Fan-Out Analysis:                           │
  │    ├── Security Agent (Secrets pattern check, OWASP/CWE)│
  │    ├── Quality Agent (Cyclomatic complexity, diff size) │
  │    ├── Test Coverage Agent (Edge cases, regressions)    │
  │    └── Docs Agent (Docstrings, API contract changes)    │
  │ 4. Arbitration & Confidence-Weighted Voting             │
  └─────────────────────────────────────────────────────────┘
            │
            ├─── Confidence >= 0.70 & No Critical Block
            │    └── Post Review Comment to GitHub PR (APPROVE / COMMENT)
            │
            └─── Confidence < 0.70 OR Critical Flags (Option B Transparent HITL)
                 ├── Enqueue to Postgres + Redis HITL Review Queue
                 └── Post Advisory Review to GitHub PR with Warning Banner
            │
            ▼
Persistence & Telemetry (PostgreSQL: code_chunks, reviews, agent_events, llm_call_logs)
```

---

## System Components

### 1. Webhook Ingress & Idempotency
- **Endpoint**: `POST /webhook/github`
- Validates the `X-Hub-Signature-256` HMAC signature against `GITHUB_WEBHOOK_SECRET`.
- Acquires an atomic lock in Redis (`lock:webhook:{delivery_id}`) with a 5-minute TTL to ensure duplicate webhook deliveries are acknowledged with HTTP 200 without duplicate execution.
- Dispatches review jobs to the ARQ worker queue.

### 2. Orchestration & Specialist Agents
Built with LangGraph to maintain an explicit, typed state machine across execution stages:
- **Security Agent**: Uses deterministic regular expressions (`_SECRET_PATTERNS`) for literal credentials and API keys, followed by model reasoning for SQL injection, cross-site scripting, authorization flaws, and insecure dependencies.
- **Quality Agent**: Evaluates structural maintainability, cyclomatic complexity, code modularity, and adherence to repository styling patterns. Enforces maximum diff size boundaries (500 KB limit).
- **Test Agent**: Detects missing test coverage for newly introduced branches, identifies unhandled edge cases, and checks test suite regression exposure.
- **Docs Agent**: Checks whether exported symbols, public APIs, schemas, and environment variables are documented. Flags docstring drift.

### 3. Arbitration Engine & Safety Rules
The aggregator merges individual agent findings using the following policies:
- **Confidence-Weighted Voting**: The aggregate score is a weighted mean across successful agents.
- **Safety-Threshold Rule**: Disagreements trigger explicit escalation. If the Security agent fails or 3 or more agents flag critical findings, the system overrides optimistic verdicts and marks the review for human inspection.
- **Option B Transparent Publishing**: Instead of silently dropping reviews or waiting for human triage, the system immediately posts a review comment to the GitHub pull request containing all findings and an escalation notice (`[!WARNING] AI Review — Pending Human Verification`). The author receives rapid feedback, and human reviewers can approve or dismiss the review via the HITL API.

### 4. Data Layer (PostgreSQL with pgvector)
A single database handles three operational workloads:
- **Semantic Memory (`code_chunks`)**: Stores chunked repository files and vector embeddings generated by `gemini-embedding-001`. Indexed via DiskANN / HNSW for sub-10ms nearest-neighbor retrieval.
- **Operational Data (`reviews`, `hitl_reviews`, `hitl_feedback`)**: Tracks review status, findings snapshots, human verdicts, and feedback signals for continuous evaluation.
- **Events Spine (`agent_events`, `llm_call_logs`)**: Records every span, token count, latency measurement, and execution cost. Continuous aggregates compute real-time p95 latency and spend rollups.

### 5. Execution Sandbox & Security Gate
- **Subprocess Sandbox**: Isolated execution wrapper used for syntax checks (`python3 -c compile(...)` and `node --check`). Configured with timeouts (default 5s), output truncation (10 KB limit), and strict command allowlisting. Disallows shell execution (`shell=False`).
- **Threat Model Gate**: Assesses incoming diffs for prompt injection patterns, indirect jailbreaks, and sensitive data leakage before prompt assembly.

### 6. Financial Guardrails (LLM FinOps)
- **BudgetGuard**: Enforces a strict daily spend threshold (default $50.00/day). If spend exceeds the cap, incoming reviews gracefully degrade to an informational status and route directly to the human queue rather than incurring unbounded API costs.
- **Cost Attribution**: Records input tokens, output tokens, and dollar costs per call in `llm_call_logs`, grouped by workflow ID and agent type.
- **Routing Advisor**: Compares current execution against cheaper model tiers to compute opportunity cost metrics for dashboard reporting.

---

## Live Verification on GitHub

A pull request review generated by this system is published on GitHub:

Review Link: [Pull Request #2 Review](https://github.com/PrathmeshRanjan/ai-pr-review-agent/pull/2#pullrequestreview-5274961023)

Key elements displayed in the review:
- Transparent Option B escalation notice (`[!WARNING] AI Review — Pending Human Verification`).
- Line-level recommendations for currency validation, numeric boundary checks, and error handling.
- Workflow execution ID, per-agent confidence breakdown, and timestamp audit trail.

---

## Architecture Comparison

| Architectural Approach | Vector Memory (RAG) | HITL Feedback Loop | Infrastructure Footprint | Tradeoff |
|---|---|---|---|---|
| **Webhook Service + Option B (This Implementation)** | Persistent pgvector store | Immediate PR advisory + async human review queue | Docker stack (API + Worker + Postgres + Redis) | Requires hosted or containerized service; delivers lowest feedback latency and highest auditability. |
| **Serverless GitHub Actions** | None (ephemeral runner storage) | Automated threshold only | Zero server infrastructure | Discarded: Cannot maintain persistent local vector indexes across runs without paying for external managed vector databases. |
| **Strict Silent HITL (Option A)** | Persistent pgvector store | Blocked until human sign-off | Server + Postgres + Redis | Discarded: Review comments are withheld until an operator logs into a dashboard, creating team bottlenecks. |
| **GitHub App with Checks API** | Persistent pgvector store | Checks API annotations | Server + GitHub App keys | Discarded: Increases setup complexity (private key rotations, diff hunk coordinate mapping) without user experience gains over PR comments. |
| **Scheduled Polling Script** | Persistent pgvector store | Delayed | Periodic cron runner | Discarded: Polling interval adds 5-15 minute latency and consumes GitHub API rate limit budgets. |

---

## Tech Stack

| Layer | Technology | Function |
|---|---|---|
| Runtime | Docker (Multi-Container) | Containerized local execution |
| Web Framework | FastAPI (Python 3.10) | Webhook receiver and management REST API |
| Orchestrator | LangGraph | State machine, parallel fan-out, arbitration |
| Job Queue | ARQ + Redis | Asynchronous distributed worker pool |
| Database | PostgreSQL 16 (`pgvector`) | Relational persistence + vector search |
| Auxiliary Vector Store | Qdrant | Secondary vector indexing client |
| Primary LLM | Mistral (`mistral-small-latest`) | Agent code reasoning and finding generation |
| Fallback LLM | Google Gemini (`gemini-2.5-flash`) | Provider failover target |
| Embeddings | Google Gemini (`gemini-embedding-001`)| Vector embeddings for codebase chunks |
| Observability | OpenTelemetry + JSONL Audit Log | Trace spans and immutable decision records |

---

## Getting Started

### Prerequisites
- Docker and Docker Compose
- GitHub account and a repository where you have admin/write permissions
- Mistral API key (primary model)
- Google Gemini API key (embeddings and fallback model)
- ngrok (for tunneling GitHub webhooks to your local machine)

### 1. Configuration
Create a `.env` file from the provided template:

```bash
cp .env.example .env
```

Set the required environment variables:
```ini
GITHUB_WEBHOOK_SECRET=your_generated_webhook_secret
GITHUB_TOKEN=your_github_personal_access_token
MISTRAL_API_KEY=your_mistral_api_key
GOOGLE_API_KEY=your_google_gemini_api_key
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/pr_review_agent
REDIS_URL=redis://localhost:6379
API_KEY=your_internal_api_key
```

### 2. Start the Stack
Start the containers using Docker Compose:

```bash
docker compose -f docker-compose.dev.yml up -d
```

Verify service health:
```bash
curl http://localhost:8001/health
```

The interactive API documentation is available at `http://localhost:8001/docs`.

### 3. Expose Webhook via ngrok
Expose the FastAPI ingress port to the internet:

```bash
ngrok http 8001
```

Copy the forwarding URL (e.g. `https://your-subdomain.ngrok-free.app`).

### 4. Configure GitHub Repository Webhook
In your target repository, navigate to **Settings > Webhooks > Add webhook**:
- **Payload URL**: `https://your-subdomain.ngrok-free.app/webhook/github`
- **Content type**: `application/json`
- **Secret**: The value assigned to `GITHUB_WEBHOOK_SECRET` in `.env`
- **Events**: Select **Let me select individual events** -> check **Pull requests**.

---

## Usage & Operations

### Pre-Indexing a Codebase for RAG
To bootstrap semantic memory for a repository before reviewing PRs:

```bash
docker compose -f docker-compose.dev.yml exec api python3 scripts/index-repo.py <owner/repo>
```

This scans repository files, computes vector embeddings, and stores them in PostgreSQL with pgvector indexes.

### Manual PR Review Trigger
You can also trigger an automated review on any open pull request via CLI:

```bash
docker compose -f docker-compose.dev.yml exec api python3 scripts/trigger-pr-review.py <owner/repo> <pr_number>
```

### Review Queue & HITL Operations
List items awaiting human review:
```bash
curl -H "X-API-Key: your_internal_api_key" http://localhost:8001/api/v1/hitl/queue
```

Submit a human resolution for an escalated review:
```bash
curl -X POST http://localhost:8001/api/v1/hitl/<hitl_id>/decision \
  -H "X-API-Key: your_internal_api_key" \
  -H "Content-Type: application/json" \
  -d '{
    "human_verdict": "approve",
    "reason": "Security findings were verified and deemed acceptable for this context.",
    "reviewer_id": "lead_engineer"
  }'
```

---

## Verification & Testing

The test suite covers unit tests, regression gates, and pipeline integration:

| Test Suite | Command | Coverage |
|---|---|---|
| Review Aggregation & Posting | `docker exec prreview_api python3 tests/test_phase8.py` | Verdict mapping, comment construction, Option B fallback |
| Security Gate & Threat Model | `docker exec prreview_api pytest tests/test_phase11.py` | Injection detection, regex patterns, payload sanitization |
| End-to-End Pipeline Demo | `bash scripts/demo.sh` | Webhook ingress, ARQ processing, LangGraph execution, HITL triage |

Run the complete regression suite inside the API container:
```bash
docker exec prreview_api pytest tests/
```

---

## Directory Layout

```
backend/
├── agents/             Specialist agents (security, quality, test, docs, base contracts)
├── api/                REST routers (reviews, queue, hitl_router, economics_router)
├── auth/               API key and role-based access dependencies
├── config/             Application settings and environment validation
├── data/               Ingestion, chunking, and index freshness checks
├── database/           PostgreSQL engine, models, and repository operations
├── economics/          BudgetGuard, cost attribution, and model routing advisor
├── hitl/               Human-in-the-loop queue, dispute resolution, and escalation rules
├── integrations/       GitHub API client and data transfer schemas
├── job_queue/          ARQ worker configuration and async job definitions
├── memory/             pgvector client and Qdrant connector
├── models/             Pydantic models for reviews, findings, and verdicts
├── observability/      Audit logger, OpenTelemetry tracing, and metric alerting
├── orchestrator/       LangGraph workflow graph, execution nodes, and state machine
├── reliability/        Circuit breaker registry and retry policies
├── security/           Input threat model and RBAC definitions
├── tools/              Tool registry, deterministic sandbox, and LLM client wrappers
└── webhook_receiver/   GitHub webhook HMAC validation and queue dispatch

docs/
└── adr/                Architectural Decision Records (ADR-001 through ADR-004)

scripts/
├── demo.sh             End-to-end integration and demonstration runner
├── index-repo.py       Repository codebase RAG indexing utility
├── migrations/         PostgreSQL schema definitions and pgvector initialization
└── trigger-pr-review.py CLI trigger for testing live pull requests
```
