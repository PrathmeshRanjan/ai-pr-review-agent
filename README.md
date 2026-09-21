# AI PR Review Agent

A production-grade, open source AI Pull Request Review Agent. A developer opens a PR. A webhook fires. Four specialist sub-agents run in parallel — security, code quality, test coverage, docs. Each one reasons over the diff plus codebase context retrieved via semantic search. An aggregator merges findings into a single structured review and posts it back to the PR. Low-confidence findings route to a human approval queue.

Every phase has a gate: tests pass, evals pass, a written checkpoint before the next phase begins.

---

## What It Does

- Receives a GitHub PR webhook
- Runs 4 parallel specialist sub-agents: security, quality, test coverage, docs
- Each agent reasons about its domain using the PR diff + codebase context (RAG via pgvectorscale)
- Posts structured review comments back to the GitHub PR
- Routes low-confidence findings to a human approval queue (HITL)
- Every agent action, LLM call, and decision is recorded in an events table
- Real-time cost and latency dashboards powered by continuous aggregates
- Learns from merged vs rejected reviews over time

---

## Data Layer — PostgreSQL (pgvector)

Most AI projects end up juggling three separate stores: a vector DB for RAG, a time-series store for traces, and Postgres for structured data. This project uses PostgreSQL with the `pgvector` extension to collapse all three into one database.

One connection pool. One backup policy. One place to reason about the data.

### Three roles, one database

| Layer | Feature | What it does |
|---|---|---|
| Semantic memory | pgvector (DiskANN / HNSW) | Stores chunked code, ADRs, and prior reviews. 4 specialist agents query it for context on every PR. Replaces external vector databases entirely. |
| Agent events | Time-series events | Every span, LLM call, tool call, and decision lands in one time-ordered table: `agent_events`. Powers the trace viewer, audit trail, and cost ledger. |
| Live dashboards | Continuous aggregates | Real-time rollups for cost per PR, p95 latency per agent, rejection rate. Materialized so the dashboard stays fast as history grows. |
| Cost control | Events + aggregates | Token cost attribution per agent span. Budget caps read from the same aggregate the dashboard does. |

### Schema sketch

```sql
-- The events spine — every agent action as a time-ordered row
CREATE TABLE agent_events (
  ts          TIMESTAMPTZ NOT NULL,
  review_id   UUID        NOT NULL,
  agent       TEXT        NOT NULL,   -- security | quality | tests | docs
  event_type  TEXT        NOT NULL,   -- span.start | llm.call | tool.call | decision
  model       TEXT,
  tokens_in   INT,
  tokens_out  INT,
  cost_usd    NUMERIC(10,6),
  latency_ms  INT,
  outcome     TEXT,
  payload     JSONB
);

SELECT create_hypertable('agent_events', 'ts', chunk_time_interval => INTERVAL '1 day');

-- Continuous aggregate: per-agent cost and latency, refreshed every minute
CREATE MATERIALIZED VIEW agent_health_1m
WITH (timescaledb.continuous) AS
SELECT
  time_bucket('1 minute', ts)    AS bucket,
  agent,
  sum(cost_usd)                  AS cost_usd,
  approx_percentile(0.95, percentile_agg(latency_ms)) AS p95_ms
FROM agent_events
GROUP BY bucket, agent;

-- Semantic memory with pgvectorscale DiskANN
CREATE TABLE code_chunks (
  id        UUID PRIMARY KEY,
  repo      TEXT NOT NULL,
  path      TEXT NOT NULL,
  content   TEXT NOT NULL,
  embedding VECTOR(1536) NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX code_chunks_emb_idx ON code_chunks
  USING diskann (embedding vector_cosine_ops);
```

---

## Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI (Python 3.10) |
| Orchestration | LangGraph (parallel fan-out, checkpointing) |
| Job Queue | Redis + ARQ |
| Memory | PostgreSQL (pgvector DiskANN / HNSW) |
| LLM | Mistral (mistral-small-latest) + Google Gemini (gemini-2.5-flash fallback) |
| Embeddings | Google Gemini (gemini-embedding-001) |
| Sandbox | Docker (isolated code execution) |
| Frontend | Next.js (review dashboard, HITL queue, trace viewer) |
| Observability | OpenTelemetry + events table |
| Deploy | Railway |

---

## Architecture

```
GitHub PR webhook
       |
       v
FastAPI ingress  (idempotency key + HMAC)
       |
       v  enqueue(review_job)
ARQ Worker - LangGraph orchestrator
       |
       +---> security_agent
       +---> quality_agent
       +---> tests_agent
       +---> docs_agent
                |
                v
         aggregator --> HITL?
                |
                v
         post_to_github
       |         |         |
       v         v         v
   pgvector   events     continuous
   vector     table      aggregates
   memory     (events)   (dashboard)
```

Modular monolith. One FastAPI service, 11 internal modules. See `docs/adr/ADR-002-architecture-style.md`.

---

## Database Setup

Run the idempotent schema migration against your PostgreSQL database:

```bash
psql $DATABASE_URL < scripts/migrations/2026-06-vector-init.sql
```

---

## Local Development

```bash
cp .env.example .env          # fill in DATABASE_URL, GITHUB_TOKEN, MISTRAL_API_KEY, GOOGLE_API_KEY
docker compose up             # starts Redis + API + Worker
```

The API will be available at `http://localhost:8000`.
Health check: `GET /health`
Interactive API Docs (Swagger): `http://localhost:8000/docs`

---

## Repository Pre-Indexing (RAG Bootstrap)

By default, repository source files are indexed automatically in the background after the first PR review completes. If you want the **very first PR review** on a repository to have immediate codebase RAG context, you can pre-index it beforehand:

```bash
python3 scripts/index-repo.py <owner/repo>
```

**Example:**
```bash
python3 scripts/index-repo.py octocat/Hello-World
```

This script:
1. Scans the target GitHub repository for source code files (`.py`, `.ts`, `.go`, etc.).
2. Generates semantic embeddings using Google's `gemini-embedding-001`.
3. Upserts code chunks into the `code_chunks` table in PostgreSQL with pgvector DiskANN indexing.
4. Subsequent reviews on this repository will instantly retrieve relevant prior code context.

---

## Triggering Reviews for Real PRs

You can trigger an automated AI review on any active GitHub Pull Request using:

```bash
docker compose -f docker-compose.dev.yml exec api python3 scripts/trigger-pr-review.py <owner/repo> <pr_number>
```

**Example:**
```bash
docker compose -f docker-compose.dev.yml exec api python3 scripts/trigger-pr-review.py PrathmeshRanjan/ai-pr-review-agent 1
```

This will:
1. Fetch the real PR metadata, diff, and files from GitHub via GitHub REST API.
2. Query vector memory for relevant repository code context.
3. Run the 4 specialist AI agents (Security, Quality, Test, Docs) in parallel.
4. Aggregate findings and output the final verdict.

---

## 20-Phase Build Roadmap

Each phase is one chapter in the course. Ends green. Has a written gate before the next phase starts.

| # | Phase |
|---|---|
| 0 | Cognitive Design — autonomy level, HITL boundaries |
| 1 | System Architecture — module graph, ADRs |
| 2 | Frontend Engineering — dashboard shell, streaming |
| 3 | Backend and API Layer — FastAPI, webhook, idempotency |
| 4 | Workflow Orchestration — LangGraph, parallel fan-out |
| 5 | LLM and Reasoning Layer — model routing, prompt registry |
| 6 | Memory Architecture — RAG on pgvector, hybrid retrieval |
| 7 | Tooling and Sandboxing — tool registry, Docker sandbox |
| 8 | Multi-Agent Systems — 4 specialists, contracts, aggregator |
| 9 | Evaluation Systems — golden dataset, LLM-as-judge |
| 10 | Observability and Tracing — OTel spans in agent_events table |
| 11 | Security Architecture — threat model, RBAC, audit trail |
| 12 | Reliability Engineering — retries, circuit breakers, idempotency |
| 13 | Infrastructure — provisioning and deployment |
| 14 | Data Engineering — ingestion pipeline, schema design |
| 15 | Governance and Compliance — audit logs, explainability |
| 16 | Economics and Cost Control — per-agent cost via continuous aggregates |
| 17 | Developer Experience — prompt playground, trace viewer |
| 18 | CI/CD for AI — prompt versioning, eval gates, canary releases |
| 19 | Human in the Loop — approval queue, escalation, feedback |
| 20 | Continuous Learning — drift detection from continuous aggregates |

---

## Project Structure

```
backend/
  api/              REST endpoints (webhook, reviews, economics, HITL)
  agents/           4 specialist agents (security, quality, tests, docs)
  config/           Settings, environment
  data/             Ingestion pipeline, embedding, freshness
  database/         Postgres async engine + vector pool
  economics/        Cost repository, budget caps
  job_queue/        ARQ worker, job definitions
  memory/           VectorMemoryClient (pgvector + hybrid search)
  observability/    Events spine, OTel traces
  orchestrator/     LangGraph graph, nodes, engine
  reliability/      Circuit breakers, retries
  tools/            Tool registry, Docker sandbox

docs/
  adr/              Architecture Decision Records (ADR-001 to ADR-002)

scripts/
  migrations/       2026-06-vector-init.sql — idempotent schema DDL

eval/               Golden dataset + regression configs
frontend/           Next.js dashboard
prompts/            Versioned prompt files per agent
```

---

## Key Design Decisions

- pgvector replaces external vector databases alongside Postgres (structured) — one connection, one backup
- Redis stays for the ARQ job queue (right tool for that job)
- `agent_events` table is the single source of truth for traces, costs, and audit
- Continuous aggregates keep the dashboard fast at any scale — no full table scans
- DiskANN / HNSW index over `code_chunks` gives fast similarity retrieval
- HITL threshold is confidence-weighted — low-confidence findings queue for human review

---


